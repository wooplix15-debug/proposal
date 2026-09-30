"""Vercel API for turning one or more requirement files into a ZIP result folder."""
import io
import json
import os
import re
import requests
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from typing import Optional, List
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wooplix_agent as agent

app = FastAPI(title="Wooplix Proposal Agent")
MAX_FILES = 5
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_BATCH_BYTES = 15 * 1024 * 1024
ALLOWED_SUFFIXES = {".txt", ".md", ".doc", ".docx", ".pdf"}


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
        Path(__file__).resolve().parents[1] / "index.html",
        Path(__file__).resolve().parents[1] / "public" / "index.html",
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
async def generate(request: Request, files: Optional[List[UploadFile]] = File(None), text: Optional[str] = Form(None)):
    if not os.environ.get("GROQ_API_KEY"):
        raise HTTPException(status_code=503, detail="The proposal service is not configured yet. Add GROQ_API_KEY in Vercel project settings.")
    has_files = files and any(f.filename for f in files)
    has_text = text and text.strip()
    if not has_files and not has_text:
        raise HTTPException(status_code=400, detail="Upload a requirement file or paste requirement text.")
    if has_files and len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"Choose between 1 and {MAX_FILES} requirement files.")

    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    requested_format = (request.query_params.get("format") or "auto").lower()
    archive = io.BytesIO()
    total = 0
    names = set()
    single_pdf_bytes = None
    single_docx_bytes = None
    single_client = ""
    single_stem = ""

    try:
        with tempfile.TemporaryDirectory(prefix="wooplix-") as work:
            # Build list of (requirement_text, source_name) pairs
            requirements = []

            # Process uploaded files
            if has_files:
                for index, upload in enumerate(files, start=1):
                    filename = Path(upload.filename or "requirement.txt").name
                    suffix = Path(filename).suffix.lower()
                    if suffix not in ALLOWED_SUFFIXES:
                        raise HTTPException(status_code=400, detail=f"{filename}: use TXT, MD, DOC, DOCX, or PDF files.")
                    raw = await upload.read(MAX_FILE_BYTES + 1)
                    total += len(raw)
                    if len(raw) > MAX_FILE_BYTES or total > MAX_BATCH_BYTES:
                        raise HTTPException(status_code=413, detail="Each file must be under 4 MB and the whole folder under 15 MB.")
                    input_path = Path(work) / f"input-{index}{suffix}"
                    input_path.write_bytes(raw)
                    requirement = agent.extract_requirement(str(input_path))
                    if not requirement.strip():
                        raise HTTPException(status_code=400, detail=f"{filename}: no readable text was found.")
                    requirements.append((requirement, filename))

            # Process pasted text
            if has_text:
                requirements.append((text.strip(), "Pasted_Requirement"))

            if not requirements:
                raise HTTPException(status_code=400, detail="No requirement content found.")

            item_count = len(requirements)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for index, (requirement, source_name) in enumerate(requirements, start=1):
                    reference_text = ""
                    if agent.ZOHO_REFRESH_TOKEN and agent.ZOHO_CLIENT_ID and agent.ZOHO_CLIENT_SECRET:
                        try:
                            reference_text = agent.fetch_crm_precedent()
                        except Exception as exc:
                            print(f"Zoho precedent unavailable; continuing without it: {type(exc).__name__}")
                    proposal = agent.draft_proposal(requirement, reference_text)
                    if not agent._looks_like_proposal(proposal):
                        raise HTTPException(status_code=502, detail=f"Could not create a proposal from {source_name}. Please try again.")
                    client_name = (proposal.get("client") or {}).get("company_name") or Path(source_name).stem
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

                    if item_count == 1:
                        single_client = client_name
                        single_stem = stem
                        if docx.exists():
                            single_docx_bytes = docx.read_bytes()
                        if pdf.exists() and pdf.stat().st_size > 0:
                            single_pdf_bytes = pdf.read_bytes()

            archive.seek(0)
            zip_payload = archive.read()
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Proposal generation failed: {type(exc).__name__}: {str(exc)[:300]}")
        raise HTTPException(status_code=502, detail=f"Proposal generation failed ({type(exc).__name__}: {str(exc)[:150]}).") from exc

    # Direct PDF response when generating a single proposal (default format)
    if item_count == 1 and requested_format in ("pdf", "auto") and single_pdf_bytes:
        out_name = f"Wooplix_Proposal_{single_stem}.pdf"
        return StreamingResponse(
            io.BytesIO(single_pdf_bytes),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{out_name}"',
                "X-Proposal-Client": single_client,
                "X-Proposal-Filename": out_name,
                "X-Proposal-Type": "pdf",
                "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Client, X-Proposal-Filename, X-Proposal-Type",
            },
        )

    # Direct DOCX response if specifically requested
    if item_count == 1 and requested_format == "docx" and single_docx_bytes:
        out_name = f"Wooplix_Proposal_{single_stem}.docx"
        return StreamingResponse(
            io.BytesIO(single_docx_bytes),
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": f'attachment; filename="{out_name}"',
                "X-Proposal-Client": single_client,
                "X-Proposal-Filename": out_name,
                "X-Proposal-Type": "docx",
                "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Client, X-Proposal-Filename, X-Proposal-Type",
            },
        )

    # Full ZIP bundle (for batch of multiple files, or if requested_format == "zip")
    if item_count == 1:
        out_name = f"Wooplix_Proposal_{single_stem}.zip"
    else:
        out_name = f"Wooplix_Proposals_{item_count}_files.zip"

    return StreamingResponse(
        io.BytesIO(zip_payload),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{out_name}"',
            "X-Proposal-Client": single_client if item_count == 1 else f"{item_count} Proposals",
            "X-Proposal-Filename": out_name,
            "X-Proposal-Type": "zip",
            "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Client, X-Proposal-Filename, X-Proposal-Type",
        },
    )
