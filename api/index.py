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
import proposal_workflow as workflow
import project_records
from proposal_scope import finalize_client_content
import business_requirements_agent as brd_agent

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
@app.get("/api/index.py/health")
@app.get("/health")
@app.get("/api/index.py")
def health():
    return {"ok": True, "configured": bool(os.environ.get("GROQ_API_KEY")), "max_files": MAX_FILES}


async def _extract_uploads(files, work, prefix):
    rows = []
    total = 0
    for index, upload in enumerate(files or [], 1):
        if not upload.filename:
            continue
        name = Path(upload.filename).name
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise HTTPException(400, f"{name}: use TXT, MD, DOC, DOCX, or PDF.")
        raw = await upload.read(MAX_FILE_BYTES + 1)
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES or total > MAX_BATCH_BYTES:
            raise HTTPException(413, "Each file must be under 4 MB and each group under 15 MB.")
        path = Path(work) / f"{prefix}-{index}{suffix}"
        path.write_bytes(raw)
        try:
            content = agent.extract_requirement(str(path))
        except (Exception, SystemExit) as exc:
            raise HTTPException(400, f"Could not read {name}. Try DOCX or a text-based PDF.") from exc
        if not content.strip():
            raise HTTPException(400, f"{name}: no readable text found. Scanned PDFs need text extraction first.")
        rows.append({'source': name, 'text': content})
    return rows


@app.post("/api/analyze")
@app.post("/api/index.py/analyze")
@app.post("/analyze")
async def analyze_requirement(files: Optional[List[UploadFile]] = File(None),
                              text: Optional[str] = Form(None),
                              completed_files: Optional[List[UploadFile]] = File(None),
                              completed_text: Optional[str] = Form(None)):
    if not agent.GROQ_API_KEY:
        raise HTTPException(503, "Set GROQ_API_KEY before analyzing requirements.")
    if len(files or []) > MAX_FILES or len(completed_files or []) > MAX_FILES:
        raise HTTPException(400, "Upload at most five requirements and five completed project records.")
    with tempfile.TemporaryDirectory(prefix='wooplix-analysis-') as work:
        requirements = await _extract_uploads(files, work, 'requirement')
        completed = [dict(row, kind='completed_project')
                     for row in await _extract_uploads(completed_files, work, 'completed')]
    if text and text.strip():
        requirements.append({'source': 'Pasted requirement', 'text': text.strip()})
    if completed_text and completed_text.strip():
        completed.append({'source': 'Pasted completed project record', 'text': completed_text.strip(),
                          'kind': 'completed_project'})
    if not requirements or len(requirements) > MAX_FILES:
        raise HTTPException(400, "Supply between one and five requirements.")
    # Explicit limit: do not silently drop evidence from the comparison.
    if sum(len(x['text']) for x in requirements + completed) > 60000:
        raise HTTPException(413, "Please shorten the requirement and delivery records to 60,000 characters in total.")
    project_data, notices = project_records.load_project_records()
    completed = project_data + completed
    if sum(len(x['text']) for x in requirements + completed) > 60000:
        raise HTTPException(413, 'The completed project library is too large. Use fewer or shorter records.')
    crm = ''
    reviews = []
    try:
        for row in requirements:
            analysis = workflow.analyze(row['text'], completed, crm, project_data)
            context_record_ids = set(analysis.get('context_record_ids', []))
            context_record_ids.update(x['record_id'] for x in analysis.get('duration_estimates', [])
                                      if x.get('record_id'))
            context_sources = set(analysis.get('context_sources', []))
            relevant_completed = [x for x in completed
                                  if x.get('record_id') in context_record_ids
                                  or (x.get('source') in context_sources and not x.get('record_id'))]
            relevant_timing = [x for x in project_data if x.get('record_id') in context_record_ids]
            context = {'requirement': row['text'], 'source': row['source'],
                       'completed': relevant_completed, 'timing_sources': relevant_timing,
                       'crm': crm, 'analysis': analysis}
            reviews.append(dict(analysis, source=row['source'], review_token=workflow.seal(context)))
    except Exception as exc:
        print(f'Analysis failed: {type(exc).__name__}')
        raise HTTPException(502, 'Could not analyze the requirements. Please try again.') from exc
    return {'reviews': reviews, 'notices': notices,
            'evidence_note': ''}


@app.post("/api/generate")
@app.post("/api/index.py/generate")
@app.post("/generate")
@app.post("/api/index.py")
async def generate(request: Request, reviews: str = Form(...)):
    if not agent.GROQ_API_KEY:
        raise HTTPException(503, "Set GROQ_API_KEY before generating proposals.")
    try:
        reviewed = json.loads(reviews)
        if not isinstance(reviewed, list) or not 1 <= len(reviewed) <= MAX_FILES:
            raise ValueError('Supply one to five analyzed requirements.')
        prepared = []
        for row in reviewed:
            context = workflow.unseal(row['review_token'])
            req, reference = workflow.prepare_draft(context, row.get('answers', {}))
            prepared.append((req, context['source'], reference, context['analysis'], row.get('answers', {})))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc

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
            item_count = len(prepared)
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for index, (requirement, source_name, reference_text, analysis, answers) in enumerate(prepared, start=1):
                    proposal = agent.draft_proposal(requirement, reference_text,
                                                    analysis.get('requirement_sections', []))
                    if not agent._looks_like_proposal(proposal):
                        raise HTTPException(status_code=502, detail=f"Could not create a proposal from {source_name}. Please try again.")
                    proposal = workflow.apply_actual_delivery_timeline(proposal, analysis, answers)
                    proposal = workflow.apply_requested_cost_breakdown(proposal, analysis)
                    proposal = finalize_client_content(proposal, analysis, answers, requirement)
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
        raise HTTPException(status_code=502, detail="Proposal generation failed. Please try again.") from exc

    if item_count == 1 and requested_format == 'pdf' and not single_pdf_bytes:
        raise HTTPException(502, 'Could not render the PDF. Please try again or select Word.')

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


@app.post("/api/brd/generate")
@app.post("/api/index.py/brd/generate")
@app.post("/brd/generate")
async def generate_brd(request: Request, reviews: str = Form(...)):
    """Dedicated Business Requirements Document agent and export path."""
    try:
        reviewed = json.loads(reviews)
        if not isinstance(reviewed, list) or not 1 <= len(reviewed) <= MAX_FILES:
            raise ValueError("Supply one to five analyzed requirements.")
        prepared = []
        for row in reviewed:
            context = workflow.unseal(row["review_token"])
            answers = row.get("answers", {})
            document = brd_agent.build_document(
                context["requirement"], context["analysis"], answers,
                source_name=context.get("source", ""),
            )
            prepared.append((document, answers))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc

    requested_format = (request.query_params.get("format") or "pdf").lower()
    if requested_format not in {"pdf", "docx", "zip"}:
        raise HTTPException(400, "Choose PDF, Word or ZIP format.")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    files = []
    with tempfile.TemporaryDirectory(prefix="wooplix-brd-") as work:
        for index, (document, _) in enumerate(prepared, start=1):
            stem = _safe_name(document["project_name"])
            if stem == "Project_name_to_be_confirmed":
                stem = "Business_Requirements"
            root = Path(work) / f"brd-{index}"
            root.mkdir()
            pdf_path = root / f"Wooplix_Business_Requirements_{stem}.pdf"
            docx_path = root / f"Wooplix_Business_Requirements_{stem}.docx"
            json_path = root / f"Wooplix_Business_Requirements_{stem}.json"
            json_path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
            brd_agent.build_docx(document, str(docx_path))
            try:
                if os.environ.get("VERCEL"):
                    base = host or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL")
                    token = os.environ.get("PDF_RENDER_TOKEN")
                    if not base or not token:
                        raise RuntimeError("PDF renderer is not configured")
                    url = base if base.startswith("http") else f"https://{base}"
                    response = requests.post(
                        f"{url.rstrip('/')}/api/pdf.php",
                        json={"html": brd_agent.build_html(document)},
                        headers={"Authorization": f"Bearer {token}"}, timeout=240,
                    )
                    response.raise_for_status()
                    pdf_path.write_bytes(response.content)
                else:
                    brd_agent.build_pdf(document, str(pdf_path))
            except Exception as exc:
                raise HTTPException(502, "Could not create the Business Requirements PDF.") from exc
            files.append((pdf_path, docx_path, json_path))

        if len(files) == 1 and requested_format in {"pdf", "docx"}:
            selected = files[0][0] if requested_format == "pdf" else files[0][1]
            media_type = ("application/pdf" if requested_format == "pdf" else
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
            return StreamingResponse(
                io.BytesIO(selected.read_bytes()), media_type=media_type,
                headers={
                    "Content-Disposition": f'attachment; filename="{selected.name}"',
                    "X-Proposal-Filename": selected.name,
                    "X-Proposal-Client": "Business Requirements",
                    "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Filename, X-Proposal-Client",
                },
            )

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for pdf_path, docx_path, json_path in files:
                for path in (pdf_path, docx_path, json_path):
                    bundle.write(path, arcname=f"Wooplix_Business_Requirements/{path.name}")
        archive.seek(0)
        name = f"Wooplix_Business_Requirements_{len(files)}_files.zip"
        return StreamingResponse(
            archive, media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "X-Proposal-Filename": name,
                "X-Proposal-Client": "Business Requirements",
                "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Filename, X-Proposal-Client",
            },
        )
