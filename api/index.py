"""Vercel API for turning one or more requirement files into a ZIP result folder."""
import io
import json
import os
import re
import requests
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wooplix_agent as agent

app = FastAPI(title="Wooplix Proposal Agent")
MAX_FILES = 5
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_BATCH_BYTES = 15 * 1024 * 1024
ALLOWED_SUFFIXES = {".txt", ".md", ".docx", ".pdf"}


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "Client"


def _build_pdf_with_existing_template(proposal, target, host=None):
    """Render the exact same HTML through the same Dompdf options as the CLI."""
    if os.environ.get("VERCEL"):
        base = host or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL")
        token = os.environ.get("PDF_RENDER_TOKEN")
        if not base or not token:
            raise RuntimeError("Vercel PHP/Dompdf renderer is not configured")
        url = base if base.startswith("http") else f"https://{base}"
        url = f"{url.rstrip('/')}/api/pdf.php"
        response = requests.post(
            url,
            json={"html": agent.build_html(proposal)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=240,
        )
        response.raise_for_status()
        Path(target).write_bytes(response.content)
        return target
    return agent.build_pdf(proposal, str(target))


@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def index_page():
    for candidate in [
        Path(__file__).resolve().parents[1] / "public" / "index.html",
        Path(__file__).resolve().parents[1] / "index.html",
    ]:
        if candidate.exists():
            return HTMLResponse(content=candidate.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Wooplix Proposal Agent</h1>")


@app.get("/wooplix_logo.png")
def logo():
    for candidate in [
        Path(__file__).resolve().parents[1] / "public" / "wooplix_logo.png",
        Path(__file__).resolve().parents[1] / "wooplix_logo.png",
    ]:
        if candidate.exists():
            return FileResponse(str(candidate), media_type="image/png")
    raise HTTPException(status_code=404, detail="Logo not found")


@app.get("/api/health")
@app.get("/health")
@app.get("/api/index.py")
def health():
    return {"ok": True, "configured": bool(os.environ.get("GROQ_API_KEY")), "max_files": MAX_FILES}


@app.post("/api/generate")
@app.post("/generate")
@app.post("/api/index.py")
async def generate(request: Request, files: list[UploadFile] = File(...)):
    if not os.environ.get("GROQ_API_KEY"):
        raise HTTPException(status_code=503, detail="The proposal service is not configured yet. Add GROQ_API_KEY in Vercel project settings.")
    if not files or len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Choose between 1 and {MAX_FILES} requirement files.")

    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    archive = io.BytesIO()
    total = 0
    names = set()
    try:
        with tempfile.TemporaryDirectory(prefix="wooplix-") as work:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for index, upload in enumerate(files, start=1):
                    filename = Path(upload.filename or "requirement.txt").name
                    suffix = Path(filename).suffix.lower()
                    if suffix not in ALLOWED_SUFFIXES:
                        raise HTTPException(status_code=400, detail=f"{filename}: use TXT, MD, DOCX, or PDF files.")
                    raw = await upload.read(MAX_FILE_BYTES + 1)
                    total += len(raw)
                    if len(raw) > MAX_FILE_BYTES or total > MAX_BATCH_BYTES:
                        raise HTTPException(status_code=413, detail="Each file must be under 4 MB and the whole folder under 15 MB.")
                    input_path = Path(work) / f"input-{index}{suffix}"
                    input_path.write_bytes(raw)
                    requirement = agent.extract_requirement(str(input_path))
                    if not requirement.strip():
                        raise HTTPException(status_code=400, detail=f"{filename}: no readable text was found.")
                    reference_text = ""
                    if agent.ZOHO_REFRESH_TOKEN and agent.ZOHO_CLIENT_ID and agent.ZOHO_CLIENT_SECRET:
                        try:
                            reference_text = agent.fetch_crm_precedent()
                        except Exception as exc:
                            print(f"Zoho precedent unavailable; continuing without it: {type(exc).__name__}")
                    proposal = agent.draft_proposal(requirement, reference_text)
                    if not agent._looks_like_proposal(proposal):
                        raise HTTPException(status_code=502, detail=f"Could not create a proposal from {filename}. Please try again.")
                    client_name = (proposal.get("client") or {}).get("company_name") or Path(filename).stem
                    stem = _safe_name(client_name)
                    if stem in names:
                        stem = f"{stem}_{index}"
                    names.add(stem)
                    result_dir = Path(work) / f"result-{index}"
                    result_dir.mkdir()
                    docx = result_dir / f"Wooplix_Proposal_{stem}.docx"
                    pdf = result_dir / f"Wooplix_Proposal_{stem}.pdf"
                    json_path = result_dir / f"Wooplix_Proposal_{stem}.json"
                    agent.build_docx(proposal, str(docx))
                    json_path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
                    outputs = [docx, json_path]
                    try:
                        _build_pdf_with_existing_template(proposal, pdf, host=host)
                        if pdf.exists() and pdf.stat().st_size > 0:
                            outputs.append(pdf)
                    except Exception as pdf_err:
                        print(f"PDF build warning: {type(pdf_err).__name__}: {pdf_err}")
                    for path in outputs:
                        bundle.write(path, arcname=f"Wooplix_Results/{path.name}")
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Proposal generation failed: {type(exc).__name__}: {str(exc)[:300]}")
        raise HTTPException(status_code=502, detail=f"Proposal generation failed ({type(exc).__name__}: {str(exc)[:150]}).") from exc
    archive.seek(0)
    return StreamingResponse(archive, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="Wooplix_Results.zip"'})
