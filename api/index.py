"""Vercel API for turning one or more requirement files into a ZIP result folder."""
import io
import json
import hashlib
import hmac
import os
import re
import requests
import shutil
import tempfile
import time
import zipfile
import base64
import subprocess
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
import software_planning_agent as software_plan_agent
import document_agent_common as document_common
import requirements_agent
import ux_agent
import implementation_agent
import qa_agent
import documentation_agent
import handover_agent
DOCUMENT_AGENTS = {
    "requirements": requirements_agent, "ux": ux_agent,
    "implementation": implementation_agent, "qa": qa_agent,
    "documentation": documentation_agent, "handover": handover_agent,
}

app = FastAPI(title="Wooplix Proposal Agent")
MAX_FILES = 5
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_BATCH_BYTES = 15 * 1024 * 1024
ALLOWED_SUFFIXES = {".txt", ".md", ".doc", ".docx", ".pdf"}
ACCESS_CODE_HASHES = {
    "b9fa57a691ff45781b4c62ba2f4941acc502bd963cd8e86d0f6710afea12c0b9",
    "9590833a07b965f154c5331f61a864cfa3f8450ee7ddd4f5fc954935063a3732",
    "d5ed32e6e248d51f745900318b030c975143e59ead3a6162d252ddb0043cc29f",
    "def5ef14f587f888b99cbb95f6b05239e864175b035492183ee75b4452033db1",
    "870c78edb19f602e40fde3c5f1134e261f6c30196cc5122ad4f8ace039cc1e74",
    "4c619d3c510f64e6cdc8e130c6fa6af7c8b53cc8c7cf8d5a2c5666348081c0e4",
    "0d1a37a5b67548e950509058f01a0b51244ecf70020cb5629f8984705225838d",
    "a7945f22e9a56c3a107d128b844b20b9dd50d0b894bec245281ce064e0c67df7",
    "d9c086b9b04f83e1217ec6f3c1b598b489ea0eb2518bc29e7a905dde5d355cff",
    "16456c3461762eb2265c5207250ed9eb92c9f000996a83fc14a60d2bdcc82c15",
}
ACCESS_COOKIE = "wooplix_access"
ACCESS_SESSION_SECONDS = 60 * 60 * 24 * 30


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "Client"


def _apply_explicit_project_name(proposal, context):
    client = proposal.setdefault("client", {})
    if not isinstance(client, dict):
        client = proposal["client"] = {}
    requested_name = str(context.get("requested_project_name") or "").strip()
    if requested_name:
        client["project_name"] = requested_name
        return
    generated_name = str(client.get("project_name") or "").strip()
    normalize = lambda value: " ".join(re.findall(r"[a-z0-9]+", value.casefold()))
    candidate = normalize(generated_name)
    source = normalize(str(context.get("requirement") or ""))
    if candidate and candidate not in source:
        client["project_name"] = ""


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


@app.get("/api/config")
@app.get("/api/index.py/config")
def public_config():
    """Return only Supabase's public browser credentials, never server secrets."""
    return {
        "supabase_url": os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "").strip(),
        "supabase_publishable_key": os.environ.get("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", "").strip(),
    }


def _access_secret():
    secret = os.environ.get("APP_ACCESS_SECRET", "").strip()
    if len(secret) < 32:
        raise HTTPException(503, "App access is not configured. Contact the app administrator.")
    return secret.encode("utf-8")


def _new_access_session():
    expires = int(time.time()) + ACCESS_SESSION_SECONDS
    payload = base64.urlsafe_b64encode(str(expires).encode()).decode().rstrip("=")
    signature = hmac.new(_access_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _has_access(request: Request):
    token = request.cookies.get(ACCESS_COOKIE, "")
    try:
        payload, signature = token.split(".", 1)
        expected = hmac.new(_access_secret(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        expires = int(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode())
        return expires > int(time.time())
    except (ValueError, UnicodeDecodeError, HTTPException):
        return False


def _require_allowed_user(request: Request):
    """Require a valid signed access-code session before using generation APIs."""
    if not _has_access(request):
        raise HTTPException(401, "Enter an access code to use this app.")


@app.get("/api/access")
@app.get("/api/index.py/access")
def check_access(request: Request):
    _access_secret()
    return {"allowed": _has_access(request)}


@app.post("/api/access/login")
@app.post("/api/index.py/access/login")
async def access_login(request: Request):
    _access_secret()
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "Enter a valid access code.") from exc
    code = str(body.get("code", "")).strip() if isinstance(body, dict) else ""
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if not any(hmac.compare_digest(code_hash, saved) for saved in ACCESS_CODE_HASHES):
        raise HTTPException(401, "That access code is not valid. Check it and try again.")
    from fastapi.responses import JSONResponse
    response = JSONResponse({"allowed": True})
    response.set_cookie(
        ACCESS_COOKIE, _new_access_session(), max_age=ACCESS_SESSION_SECONDS,
        httponly=True, secure=True, samesite="strict", path="/",
    )
    return response


@app.post("/api/access/logout")
@app.post("/api/index.py/access/logout")
def access_logout():
    from fastapi.responses import JSONResponse
    response = JSONResponse({"allowed": False})
    response.delete_cookie(ACCESS_COOKIE, path="/", httponly=True, secure=True, samesite="strict")
    return response


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
async def analyze_requirement(request: Request,
                              files: Optional[List[UploadFile]] = File(None),
                              text: Optional[str] = Form(None),
                              project_name: Optional[str] = Form(None),
                              completed_files: Optional[List[UploadFile]] = File(None),
                              completed_text: Optional[str] = Form(None)):
    _require_allowed_user(request)
    project_name = (project_name or "").strip()
    if len(project_name) > 120:
        raise HTTPException(400, "Project name must be 120 characters or fewer.")
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
                       'crm': crm, 'analysis': analysis,
                       'requested_project_name': project_name}
            reviews.append(dict(analysis, source=row['source'], review_token=workflow.seal(context)))
    except workflow.AnalysisServiceError as exc:
        print(f'Analysis service unavailable: {type(exc).__name__}')
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        print(f'Analysis input failed: {type(exc).__name__}')
        raise HTTPException(400, f"We could not read the requirement structure: {exc}") from exc
    except Exception as exc:
        print(f'Analysis failed: {type(exc).__name__}')
        raise HTTPException(502, 'The requirements could not be analyzed. Please try again.') from exc
    return {'reviews': reviews, 'notices': notices,
            'evidence_note': ''}


@app.post("/api/generate")
@app.post("/api/index.py/generate")
@app.post("/generate")
@app.post("/api/index.py")
async def generate(request: Request, reviews: str = Form(...)):
    _require_allowed_user(request)
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
    except (RuntimeError, workflow.AnalysisServiceError) as exc:
        raise HTTPException(503, str(exc)) from exc

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
                    _apply_explicit_project_name(proposal, context)
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
    _require_allowed_user(request)
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
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

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


@app.post("/api/software-plan/analyze")
@app.post("/api/index.py/software-plan/analyze")
async def analyze_software_plan(request: Request,
                                files: Optional[List[UploadFile]] = File(None),
                                text: Optional[str] = Form(None),
                                project_name: Optional[str] = Form(None)):
    """Analyze a software brief separately from Proposal and BRD analysis."""
    _require_allowed_user(request)
    if not agent.GROQ_API_KEY:
        raise HTTPException(503, "Set GROQ_API_KEY before analyzing software requirements.")
    if len(files or []) > MAX_FILES:
        raise HTTPException(400, "Upload at most five requirement files.")
    if len((project_name or "").strip()) > 120:
        raise HTTPException(400, "Project name must be 120 characters or fewer.")
    with tempfile.TemporaryDirectory(prefix="wooplix-software-analysis-") as work:
        sources = await _extract_uploads(files, work, "software-requirement")
    if text and text.strip():
        sources.append({"source": "Pasted requirement", "text": text.strip()})
    if not sources:
        raise HTTPException(400, "Upload a requirement or paste its text.")
    brief = "\n\n".join(f"SOURCE: {row['source']}\n{row['text']}" for row in sources)
    if len(brief) > 60000:
        raise HTTPException(413, "Please keep the combined requirements under 60,000 characters.")
    try:
        analysis = software_plan_agent.analyze(brief, project_name or "")
        context = {"software_plan": True, "requirement": brief, "analysis": analysis,
                   "source": ", ".join(row["source"] for row in sources)}
        analysis["review_token"] = workflow.seal(context)
        return {"reviews": [analysis], "notices": [], "evidence_note": ""}
    except (ValueError, workflow.AnalysisServiceError) as exc:
        raise HTTPException(503 if isinstance(exc, workflow.AnalysisServiceError) else 400, str(exc)) from exc
    except Exception as exc:
        print(f"Software planning analysis failed: {type(exc).__name__}")
        raise HTTPException(502, "The software requirements could not be analyzed. Please try again.") from exc


def _build_software_plan_pdf(documents: list[dict], target: Path, host: str | None = None) -> None:
    _render_html_pdf(software_plan_agent.build_bundle_html(documents), target, host)


def _render_html_pdf(rendered_html: str, target: Path, host: str | None = None) -> None:
    if os.environ.get("VERCEL"):
        base = host or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL")
        token = os.environ.get("PDF_RENDER_TOKEN")
        if not base or not token:
            raise RuntimeError("The PDF renderer is not configured.")
        url = base if base.startswith("http") else f"https://{base}"
        response = requests.post(f"{url.rstrip('/')}/api/pdf.php", json={"html": rendered_html},
                                 headers={"Authorization": f"Bearer {token}"}, timeout=240)
        response.raise_for_status()
        target.write_bytes(response.content)
        return
    php = shutil.which("php")
    autoload = Path(__file__).resolve().parents[1] / "vendor" / "autoload.php"
    if not php or not autoload.exists():
        raise RuntimeError("PDF rendering needs PHP and the installed Dompdf package.")
    with tempfile.TemporaryDirectory(prefix="wooplix-software-pdf-") as work:
        html_path = Path(work) / "document.html"
        html_path.write_text(rendered_html, encoding="utf-8")
        result = subprocess.run([php, str(Path(__file__).resolve().parents[1] / "html_to_pdf.php"),
                                 str(html_path), str(target)], capture_output=True, text=True, timeout=240)
        if result.returncode or not target.exists() or target.stat().st_size == 0:
            raise RuntimeError("Could not render the software planning PDF.")


def _render_software_pack_pdf(document: dict, target: Path, host: str | None = None) -> None:
    """Reuse the existing Proposal and BRD HTML/PDF renderers for those files."""
    kind, content = document.get("kind"), document.get("content", {})
    if kind == "proposal":
        if not os.environ.get("VERCEL"):
            agent.build_pdf(content, str(target))
            return
        rendered_html = agent.build_html(content)
    elif kind == "brd":
        if not os.environ.get("VERCEL"):
            brd_agent.build_pdf(content, str(target))
            return
        rendered_html = brd_agent.build_html(content)
    else:
        _build_software_plan_pdf([document], target, host)
        return
    base = host or os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") or os.environ.get("VERCEL_URL")
    token = os.environ.get("PDF_RENDER_TOKEN")
    if not base or not token:
        raise RuntimeError("The PDF renderer is not configured.")
    url = base if base.startswith("http") else f"https://{base}"
    response = requests.post(f"{url.rstrip('/')}/api/pdf.php", json={"html": rendered_html},
                             headers={"Authorization": f"Bearer {token}"}, timeout=240)
    response.raise_for_status()
    target.write_bytes(response.content)


@app.post("/api/software-plan/generate")
@app.post("/api/index.py/software-plan/generate")
async def generate_software_plan(request: Request, reviews: str = Form(...)):
    """Generate the eight-document software delivery pack from a reviewed brief."""
    _require_allowed_user(request)
    if not agent.GROQ_API_KEY:
        raise HTTPException(503, "Set GROQ_API_KEY before generating the software planning pack.")
    try:
        reviewed = json.loads(reviews)
        if not isinstance(reviewed, list) or len(reviewed) != 1:
            raise ValueError("Analyze one software project at a time.")
        row = reviewed[0]
        context = workflow.unseal(row["review_token"])
        if not context.get("software_plan"):
            raise ValueError("This review is not for the software planning agent. Analyze the requirement again.")
        documents = software_plan_agent.build_pack(context["analysis"], row.get("answers", {}),
                                                   source_name=context.get("source", ""))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except workflow.AnalysisServiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        print(f"Software planning generation failed: {type(exc).__name__}")
        raise HTTPException(502, "The planning documents could not be created. Please try again.") from exc

    requested_format = (request.query_params.get("format") or "pdf").lower()
    if requested_format not in {"pdf", "docx", "zip"}:
        raise HTTPException(400, "Choose PDF, Word or ZIP format.")
    project_stem = _safe_name(context["analysis"].get("project_name") or "Software_Planning_Pack")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    try:
        with tempfile.TemporaryDirectory(prefix="wooplix-software-pack-") as work:
            root = Path(work)
            all_paths = []
            pdf_paths = []
            docx_paths = []
            manifest_files = []
            for index, document in enumerate(documents, 1):
                kind = document.get("kind", "document")
                stem = f"{index:02d}-{kind}"
                docx_path = root / f"{stem}.docx"
                pdf_path = root / f"{stem}.pdf"
                json_path = root / f"{stem}.json"
                if kind == "proposal":
                    agent.build_docx(document["content"], str(docx_path))
                elif kind == "brd":
                    brd_agent.build_docx(document["content"], str(docx_path))
                else:
                    software_plan_agent.build_docx(document, str(docx_path))
                json_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
                all_paths.extend([docx_path, json_path])
                if requested_format != "docx":
                    _render_software_pack_pdf(document, pdf_path, host)
                    all_paths.append(pdf_path)
                    pdf_paths.append(pdf_path)
                docx_paths.append(docx_path)
                manifest_files.append({"name": pdf_path.name, "word": docx_path.name, "type": kind})
            summary_path = root / "run-summary.json"
            summary_path.write_text(json.dumps({"status": "pack_ready", "artifact_count": len(documents),
                                                "files": manifest_files}, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            archive = io.BytesIO()
            selected = pdf_paths if requested_format == "pdf" else docx_paths if requested_format == "docx" else all_paths + [summary_path]
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                for path in selected:
                    bundle.write(path, arcname=path.name)
            archive.seek(0)
            filename = f"Wooplix_Software_Planning_Pack_{project_stem}_{requested_format.upper()}.zip"
            return StreamingResponse(archive, media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{filename}"',
                         "X-Proposal-Filename": filename, "X-Proposal-Client": project_stem,
                         "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Filename, X-Proposal-Client"})
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Software planning export failed: {type(exc).__name__}")
        raise HTTPException(502, "Could not export the software planning pack. Check PDF/DOCX support and try again.") from exc


@app.post("/api/documents/{kind}/analyze")
@app.post("/api/index.py/documents/{kind}/analyze")
async def analyze_document(kind: str, request: Request,
                           files: Optional[List[UploadFile]] = File(None),
                           text: Optional[str] = Form(None),
                           project_name: Optional[str] = Form(None)):
    _require_allowed_user(request)
    builder = DOCUMENT_AGENTS.get(kind)
    if builder is None:
        raise HTTPException(404, "Choose a supported document type.")
    if not agent.GROQ_API_KEY:
        raise HTTPException(503, "Document analysis is unavailable. Please contact the administrator.")
    if len(files or []) > MAX_FILES:
        raise HTTPException(400, "Upload at most five requirement files.")
    if len(project_name or "") > 120:
        raise HTTPException(400, "Project name must be 120 characters or fewer.")
    with tempfile.TemporaryDirectory(prefix="wooplix-document-analysis-") as work:
        sources = await _extract_uploads(files, work, "requirement")
    if text and text.strip():
        sources.append({"source": "Pasted requirement", "text": text.strip()})
    if not sources:
        raise HTTPException(400, "Upload a requirement or paste its text.")
    brief = "\n\n".join(row["text"] for row in sources)
    try:
        analysis = builder.analyze(brief, project_name or "")
        analysis["source"] = ", ".join(row["source"] for row in sources)
        analysis["review_token"] = workflow.seal({"document_kind": kind, "analysis": analysis})
        return {"reviews": [analysis], "notices": [], "evidence_note": ""}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except workflow.AnalysisServiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:
        print(f"Document analysis failed ({kind}): {type(exc).__name__}")
        raise HTTPException(502, "Analysis could not finish. Please try again.") from exc


@app.post("/api/documents/{kind}/generate")
@app.post("/api/index.py/documents/{kind}/generate")
async def generate_document(kind: str, request: Request, reviews: str = Form(...)):
    _require_allowed_user(request)
    builder = DOCUMENT_AGENTS.get(kind)
    if builder is None:
        raise HTTPException(404, "Choose a supported document type.")
    fmt = (request.query_params.get("format") or "pdf").lower()
    if fmt not in {"pdf", "docx", "zip"}:
        raise HTTPException(400, "Choose PDF, Word or ZIP format.")
    if not agent.GROQ_API_KEY:
        raise HTTPException(503, "Document generation is unavailable. Please contact the administrator.")
    try:
        rows = json.loads(reviews)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError("Analyze one document at a time.")
        row = rows[0]
        context = workflow.unseal(row["review_token"])
        if context.get("document_kind") != kind:
            raise ValueError("This review belongs to a different document. Analyze the requirement again.")
        document = builder.build_document(context["analysis"], row.get("answers", {}))
        stem = "Wooplix_" + kind.title() + "_" + _safe_name(document.get("project_name") or "Document")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        with tempfile.TemporaryDirectory(prefix="wooplix-document-") as work:
            word = Path(work) / (stem + ".docx")
            pdf = Path(work) / (stem + ".pdf")
            if fmt in {"docx", "zip"}:
                document_common.build_docx(document, str(word))
            if fmt in {"pdf", "zip"}:
                _render_html_pdf(document_common.build_html(document), pdf, host)
            if fmt == "zip":
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
                    archive.write(word, word.name)
                    archive.write(pdf, pdf.name)
                payload, mime = buf.getvalue(), "application/zip"
            else:
                payload = (pdf if fmt == "pdf" else word).read_bytes()
                mime = "application/pdf" if fmt == "pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        filename = stem + "." + fmt
        return StreamingResponse(io.BytesIO(payload), media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{filename}"',
                     "X-Proposal-Filename": filename,
                     "X-Proposal-Client": document.get("project_name") or builder.TITLE,
                     "Access-Control-Expose-Headers": "Content-Disposition, X-Proposal-Filename, X-Proposal-Client"})
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except workflow.AnalysisServiceError as exc:
        raise HTTPException(503, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        print(f"Document generation failed ({kind}): {type(exc).__name__}")
        raise HTTPException(502, "The document could not be created or exported. Please try again.") from exc
