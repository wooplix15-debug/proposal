#!/usr/bin/env python3
"""Wooplix Proposal Agent — self-contained generator.

Usage:
    python3 wooplix_agent.py REQUIREMENT_FILE [--out DIR] [--model ID] [--skip-crm] [--workdrive]

REQUIREMENT_FILE may be .txt/.md/.docx/.pdf, or "-" to read requirement text from stdin.

Secrets come from environment variables or a `.env` file next to this script:
    GROQ_API_KEY (required)
    GROQ_MODEL (optional, default openai/gpt-oss-120b)
    ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET / ZOHO_REFRESH_TOKEN (optional: CRM precedent + WorkDrive)
    ZOHO_CRM_MODULE (optional, default Deals)
    ZOHO_WORKDRIVE_FOLDER_ID (optional, default root)
"""
import os
import re
import sys
import json
import base64
import shutil
import argparse
import tempfile
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGO_PATH = str(HERE / "wooplix_logo.png")
PHP_SCRIPT = str(HERE / "html_to_pdf.php")

COMPANY_NAME = "Wooplix Technologies Private Limited"
COMPANY_TAGLINE = "Together, We Achieve More"
COMPANY_WEBSITE = "https://www.wooplix.com/"
COMPANY_EMAIL = "enquiry@wooplix.com"


# --------------------------------------------------------------------------- config
def _load_dotenv():
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL") or "openai/gpt-oss-120b"
ZOHO_CLIENT_ID = os.environ.get("ZOHO_CLIENT_ID", "")
ZOHO_CLIENT_SECRET = os.environ.get("ZOHO_CLIENT_SECRET", "")
ZOHO_REFRESH_TOKEN = os.environ.get("ZOHO_REFRESH_TOKEN", "")
ZOHO_CRM_MODULE = os.environ.get("ZOHO_CRM_MODULE") or "Deals"
ZOHO_WORKDRIVE_FOLDER_ID = os.environ.get("ZOHO_WORKDRIVE_FOLDER_ID") or "root"
ZOHO_ACCOUNTS_URL = os.environ.get("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in")


# --------------------------------------------------------------------------- prompt
SYSTEM_PROMPT = r"""
You are the Proposal Solution Architect for Wooplix Technologies Private Limited.

Turn a customer requirement document into a Wooplix proposal written in Wooplix's house style: a precise scope-of-work, not a marketing document.

You may also receive PREVIOUS DEALS from Wooplix's Zoho CRM. Use them only as precedent for how similar work was scoped and, only if they contain usable rates or effort, for commercials. Never invent rates or hours.

HOUSE STYLE — match exactly how Wooplix sends proposals
- Project Introduction: ONE short paragraph. Pattern: "Wooplix Technologies Private Limited (Wooplix) will implement and customize <products> for <purpose> in line with the client's organizational policies and processes." Add at most one more sentence for the key business outcome.
- Project Scope: grouped by product. Under each product list named configuration / implementation areas (for example "User, Role & Access Configuration", "Organization Setup", "Expense Policy Configuration", "Approval Matrix Configuration", "<Product> Integration", "Reports & Export Templates"). Under each area put terse imperative bullets ending with a full stop ("Add and invite all users.", "Configure role-based access rights for data visibility and actions.").
- Carry the client's concrete values through verbatim wherever the requirement gives them: amounts, per-day or per-km rates, limits, bank names, user counts, report names, SLA figures. Never generalise them away.
- Mark genuinely optional items by appending "(If Required)" to the area name or the bullet.
- Pre-requisites: what the CLIENT must provide (credentials, master data, policies, sample templates, approval matrix, sign-offs).
- Deliverables: concrete bullets (configured module, integrations, export templates, UAT, training, post-deployment support with its duration).
- Plain professional English. No marketing adjectives, no emoji, no exclamation marks.
- Only include the sections defined in the JSON structure below. Do not add others.
- Do not state effort hours or pricing unless a Wooplix pricing/effort master or genuine precedent supplies them; otherwise leave commercials null.

NEVER
- Invent client requirements, products, integrations, rates, hours, dates or commitments.
- Mark the proposal APPROVED.
- Use AI-tell words: delve, leverage, cutting-edge, seamless, robust, holistic, synergy, transformative, empower, unlock, "in today's ... landscape", "it is important to note", furthermore/moreover as sentence openers, "in conclusion".

OUTPUT RULES
- Return ONLY valid JSON using the exact structure below.
{
  "client": { "company_name": "", "project_name": "", "contact": "" },
  "project_introduction": "",
  "scope": [
    { "product": "", "areas": [ { "area": "", "tasks": [""] } ] }
  ],
  "prerequisites": [""],
  "deliverables": [""],
  "open_points": [""],
  "commercials": null,
  "timeline": null,
  "status": "DRAFT"
}
- commercials: non-null ONLY when a pricing master or precedent supplies figures, as {"items":[{"item":"","amount":"","basis":""}], "total":"","note":""}; otherwise null.
- timeline: non-null ONLY when the requirement states an expectation or precedent supports one, as {"phases":[{"phase":"","duration":""}], "overall":""}; otherwise null.
- open_points: items to be finalized during discovery; empty list if none.
"""


# --------------------------------------------------------------------------- extraction
def extract_requirement(path_or_dash):
    if path_or_dash == "-":
        return sys.stdin.read().strip()
    p = Path(path_or_dash)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages).strip()
    if suffix in (".txt", ".md"):
        return p.read_text(errors="ignore").strip()
    if suffix == ".docx":
        from docx import Document
        d = Document(str(p))
        chunks = [x.text.strip() for x in d.paragraphs if x.text.strip()]
        for t in d.tables:
            for row in t.rows:
                cs = [c.text.strip() for c in row.cells]
                if any(cs):
                    chunks.append(" | ".join(cs))
        return "\n".join(chunks)
    if suffix == ".doc":
        try:
            from docx import Document
            d = Document(str(p))
            chunks = [x.text.strip() for x in d.paragraphs if x.text.strip()]
            if chunks:
                return "\n".join(chunks)
        except Exception:
            pass
        raw = p.read_bytes()
        text_parts = []
        for m in re.finditer(b'(?:[\x20-\x7e]\x00){4,}', raw):
            try:
                t = m.group(0).decode('utf-16le').strip()
                if len(t) > 3:
                    text_parts.append(t)
            except Exception:
                pass
        for m in re.finditer(b'[\x20-\x7e\n\r\t]{5,}', raw):
            try:
                t = m.group(0).decode('latin1').strip()
                if len(t) > 4 and not any(t.startswith(x) for x in ['WordDocument', 'CompObj', 'SummaryInfo', 'Normal.dotm']):
                    text_parts.append(t)
            except Exception:
                pass
        extracted = "\n".join(dict.fromkeys(text_parts)).strip()
        if extracted:
            return extracted
        return p.read_text(errors="ignore").strip()
    raise SystemExit(f"Unsupported requirement type: {suffix}")


# --------------------------------------------------------------------------- zoho
def _zoho_token():
    import requests
    cid = ZOHO_CLIENT_ID if ZOHO_CLIENT_ID.startswith("1000.") else "1000." + ZOHO_CLIENT_ID
    r = requests.post(ZOHO_ACCOUNTS_URL.rstrip("/") + "/oauth/v2/token", params={
        "refresh_token": ZOHO_REFRESH_TOKEN, "client_id": cid,
        "client_secret": ZOHO_CLIENT_SECRET, "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    tok = r.json()
    return tok["access_token"], (tok.get("api_domain") or "https://www.zohoapis.in").rstrip("/")


def fetch_crm_precedent():
    import requests
    access, api_root = _zoho_token()
    r = requests.get(f"{api_root}/crm/v2/{ZOHO_CRM_MODULE}", params={"per_page": 25, "page": 1},
                     headers={"Authorization": f"Zoho-oauthtoken {access}"}, timeout=60)
    r.raise_for_status()
    recs = r.json().get("data", []) or []
    blocks = []
    for i, rec in enumerate(recs, 1):
        lines = []
        for k, v in rec.items():
            if k.startswith("$") or v in (None, "", [], {}):
                continue
            if isinstance(v, dict):
                v = v.get("name") or v.get("value") or ""
            if isinstance(v, list):
                v = ", ".join(str(x.get("name", x) if isinstance(x, dict) else x) for x in v)
            t = str(v).strip()
            if t:
                lines.append(f"{k.replace('_', ' ').strip()}: {t[:1200]}")
        if lines:
            blocks.append(f"--- PREVIOUS DEAL {i} ---\n" + "\n".join(lines))
    return "\n\n".join(blocks)[:60000]


def upload_workdrive(local_path):
    import requests
    access, api_root = _zoho_token()
    with open(local_path, "rb") as fh:
        r = requests.post(f"{api_root}/workdrive/api/v1/files/upload",
                          headers={"Authorization": f"Zoho-oauthtoken {access}"},
                          data={"folder_id": ZOHO_WORKDRIVE_FOLDER_ID},
                          files={"file": (os.path.basename(local_path), fh)}, timeout=180)
    r.raise_for_status()
    info = (r.json().get("data") or [{}])[0]
    return (info.get("attributes") or {}).get("name", os.path.basename(local_path))


# --------------------------------------------------------------------------- llm
def _first_dict(obj):
    """Return the first proposal-like dict found in obj (handles list-wrapped JSON)."""
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        for item in obj:
            found = _first_dict(item)
            if found:
                return found
    return None


def parse_json_safely(raw):
    t = (raw or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.I)
    t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        s, e = t.find("{") if "{" in t else t.find("["), max(t.rfind("}"), t.rfind("]"))
        obj = json.loads(re.sub(r",\s*([}\]])", r"\1", t[s:e + 1]))
    return _first_dict(obj) or {}


def _looks_like_proposal(d):
    return isinstance(d, dict) and any(
        k in d for k in ("client", "scope", "project_introduction"))


def draft_proposal(req_text, reference_text):
    from groq import Groq
    user_prompt = "CUSTOMER REQUIREMENT DOCUMENT\nAnalyze the following and return the required JSON.\n\n" + req_text[:320000]
    if reference_text:
        user_prompt = ("PREVIOUS DEALS (precedent from Wooplix CRM — use only per the PRECEDENT RULES)\n\n"
                       + reference_text + "\n\n" + user_prompt)
    client = Groq(api_key=GROQ_API_KEY)
    last = {}
    for attempt in range(2):
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
            temperature=0.1, max_completion_tokens=16000,
            response_format={"type": "json_object"},
        )
        last = parse_json_safely(resp.choices[0].message.content)
        if _looks_like_proposal(last):
            return last
    return last


# --------------------------------------------------------------------------- proposal normalizer
NAVY_HEX = "1a365d"
TEAL_HEX = "008080"
LIGHT_BG = "f8fafc"


def _ordinal_day(d=None):
    from datetime import datetime as _dt
    d = d or _dt.now()
    day = d.day
    suf = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suf} {d.strftime('%b, %Y')}"


def normalize_proposal(data):
    """Enrich a raw LLM proposal dict into a fully-structured document model."""
    out = dict(data)
    client = out.get("client") or {}
    client_name  = client.get("company_name") or "Client"
    project_name = client.get("project_name") or "Business Automation & System Implementation"

    # ---- Project overview key-value table ----
    if not out.get("project_overview_table"):
        products = ", ".join(
            p.get("product", "") for p in out.get("scope", []) if p.get("product")
        ) or "Zoho Cloud Suite"
        out["project_overview_table"] = [
            {"parameter": "Client Organization",        "details": client_name},
            {"parameter": "Project Title",              "details": project_name},
            {"parameter": "Solution Partner",           "details": f"{COMPANY_NAME} (Zoho Authorized Partner)"},
            {"parameter": "Target Platforms",           "details": products},
            {"parameter": "Implementation Methodology", "details": "Structured Turnkey Rollout — Discovery → Configuration → Integration → UAT → Go-Live"},
            {"parameter": "Proposal Status",            "details": f"{out.get('status', 'DRAFT')} (Valid for 30 days from date of presentation)"},
        ]

    # ---- Scope — normalize into per-module config tables + extract integrations ----
    norm_scope = []
    integrations = []
    for item in out.get("scope", []) or []:
        prod = item.get("product", "Solution Module")
        overview = item.get("overview") or (
            f"{prod} will be configured and deployed to streamline core operations, "
            f"enforce business controls, and eliminate manual tracking in accordance "
            f"with {client_name}'s standard operating policies."
        )
        cfg_rows = []
        if item.get("configuration_table"):
            cfg_rows = item["configuration_table"]
        else:
            for a in item.get("areas", []) or []:
                area  = a.get("area", "Configuration Area")
                tasks = [str(t).strip() for t in a.get("tasks", []) if str(t).strip()]
                tasks_text = "• " + "\n• ".join(tasks) if tasks else area
                if "integration" in area.lower() or any("integrat" in t.lower() for t in tasks):
                    integrations.append({
                        "interface":     area,
                        "source_system": prod,
                        "target_system": "Target Application",
                        "data_entity":   "Transactional & Master Records",
                        "sync_mode":     "Scheduled Sync / API Webhook",
                        "logic":         "; ".join(tasks),
                    })
                cfg_rows.append({
                    "area":              area,
                    "scope_description": tasks_text,
                    "business_rules":    (
                        "Configured as per organizational policies, role access hierarchy, "
                        "approval matrices, and validation rules."
                    ),
                })
        considerations = item.get("key_considerations") or [
            f"All configurations will be aligned with {client_name}'s designated role matrix and organizational hierarchy.",
            "Custom fields, automation workflows, and email notifications will be reviewed during discovery.",
            "Optional scope items marked '(If Required)' will be confirmed during the initial requirements sprint.",
        ]
        norm_scope.append({
            "product":             prod,
            "overview":            overview,
            "configuration_table": cfg_rows,
            "key_considerations":  considerations,
        })
    out["scope"] = norm_scope

    if not out.get("integrations_table") and integrations:
        out["integrations_table"] = integrations

    # ---- Prerequisites — categorized bullet groups ----
    raw_pre = out.get("prerequisites", []) or []
    if not out.get("categorized_prerequisites"):
        cats = {
            "System Access & Credentials":              [],
            "Master Data & Templates":                  [],
            "Policies, Workflows & Approval Matrices":  [],
            "Project Governance & Sign-Off":             [],
        }
        for p in raw_pre:
            s = str(p).strip()
            low = s.lower()
            if any(k in low for k in ["access", "credential", "api", "key", "password", "login", "server", "endpoint"]):
                cats["System Access & Credentials"].append(s)
            elif any(k in low for k in ["data", "master", "spreadsheet", "sample", "list", "record", "cleansing", "deduplication", "field", "volume"]):
                cats["Master Data & Templates"].append(s)
            elif any(k in low for k in ["policy", "process", "matrix", "approval", "discount", "sla", "rule", "map", "escalation"]):
                cats["Policies, Workflows & Approval Matrices"].append(s)
            else:
                cats["Project Governance & Sign-Off"].append(s)
        out["categorized_prerequisites"] = {k: v for k, v in cats.items() if v}

    # ---- Deliverables — categorized bullet groups ----
    raw_dl = out.get("deliverables", []) or []
    if not out.get("categorized_deliverables"):
        cats = {
            "Application & Workflow Configuration": [],
            "System Integration":                   [],
            "Data Migration & Validation":           [],
            "Testing & User Acceptance (UAT)":       [],
            "Training & Documentation":              [],
            "Post Go-Live Support":                  [],
        }
        for d in raw_dl:
            s = str(d).strip()
            low = s.lower()
            if any(k in low for k in ["support", "stabilization", "hypercare", "go-live"]):
                cats["Post Go-Live Support"].append(s)
            elif any(k in low for k in ["training", "session", "workshop", "user", "document", "guide", "sop"]):
                cats["Training & Documentation"].append(s)
            elif any(k in low for k in ["uat", "test", "testing", "script", "validation"]):
                cats["Testing & User Acceptance (UAT)"].append(s)
            elif any(k in low for k in ["migration", "import", "data load", "historical"]):
                cats["Data Migration & Validation"].append(s)
            elif any(k in low for k in ["integration", "connector", "api", "sync"]):
                cats["System Integration"].append(s)
            else:
                cats["Application & Workflow Configuration"].append(s)
        out["categorized_deliverables"] = {k: v for k, v in cats.items() if v}

    # ---- Timeline — 4-column table with milestone gates ----
    tl = out.get("timeline") or {}
    phases = tl.get("phases", [])
    if not phases:
        tl["phases"] = [
            {"phase": "Phase 1: Discovery & Requirements Sign-off",    "key_activities": "Process walkthroughs, field mappings, workflow charts, master data templates, and architecture validation.", "duration": "1 – 2 Weeks",  "milestone": "Discovery Sign-off & SOW Confirmation"},
            {"phase": "Phase 2: Core System Configuration & Setup",    "key_activities": "Module configuration, role hierarchy, custom fields, pipelines, notification workflows, and approval rules.", "duration": "2 – 3 Weeks",  "milestone": "Baseline Configuration Walkthrough"},
            {"phase": "Phase 3: Integration & Data Migration",         "key_activities": "Third-party connector setup, API testing, historical master data cleansing and initial data load.",            "duration": "2 Weeks",      "milestone": "Integrated Test Environment Ready"},
            {"phase": "Phase 4: UAT & Training",                       "key_activities": "End-to-end scenario validation, defect remediation, admin and user training sessions.",                        "duration": "1 – 2 Weeks",  "milestone": "Formal UAT Sign-off"},
            {"phase": "Phase 5: Go-Live & Post-Launch Support",        "key_activities": "Production cutover, final delta migration, live user guidance, and stabilization support.",                    "duration": "2 Weeks",      "milestone": "Production Deployment & Project Handover"},
        ]
        if not tl.get("overall"):
            tl["overall"] = "6 – 10 Weeks (subject to client feedback turnaround and master data readiness)"
    else:
        tl["phases"] = [
            {
                "phase":          p.get("phase", "Implementation Phase"),
                "key_activities": p.get("key_activities", p.get("description", "Module configuration, validation, and testing.")),
                "duration":       p.get("duration", "TBD"),
                "milestone":      p.get("milestone", p.get("milestone_deliverable", "Milestone Review & Sign-off")),
            }
            for p in phases if isinstance(p, dict)
        ]
    out["timeline"] = tl
    return out


# --------------------------------------------------------------------------- DOCX (house style)
def build_docx(data, output):
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls
    import docx.enum.text

    data = normalize_proposal(data)

    def _shd(cell, fill):
        tcPr = cell._tc.get_or_add_tcPr()
        tcPr.append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill}"/>'))

    def _cell_margins(table, top=120, bot=120, left=150, right=150):
        tblPr = table._tbl.tblPr
        tblPr.append(parse_xml(
            f'<w:tblCellMar {nsdecls("w")}>'
            f'<w:top w:w="{top}" w:type="dxa"/>'
            f'<w:bottom w:w="{bot}" w:type="dxa"/>'
            f'<w:left w:w="{left}" w:type="dxa"/>'
            f'<w:right w:w="{right}" w:type="dxa"/>'
            f'</w:tblCellMar>'
        ))

    def _borders(table, color="cbd5e1", sz="4"):
        tblPr = table._tbl.tblPr
        tblPr.append(parse_xml(
            f'<w:tblBorders {nsdecls("w")}>'
            f'<w:top w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
            f'<w:bottom w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
            f'<w:left w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
            f'<w:right w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
            f'<w:insideH w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
            f'<w:insideV w:val="single" w:sz="{sz}" w:space="0" w:color="{color}"/>'
            f'</w:tblBorders>'
        ))

    def _make_table(ncols, widths, col_names):
        tbl = doc.add_table(rows=1, cols=ncols)
        tbl.style = "Table Grid"
        _cell_margins(tbl)
        _borders(tbl)
        tbl.rows[0]._tr.get_or_add_trPr().append(parse_xml(f'<w:tblHeader {nsdecls("w")}/>'))
        for row in tbl.rows:
            row._tr.get_or_add_trPr().append(parse_xml(f'<w:cantSplit {nsdecls("w")}/>'))
            for i, w in enumerate(widths):
                if i < len(row.cells):
                    row.cells[i].width = Inches(w)
        for i, c in enumerate(tbl.rows[0].cells):
            _shd(c, NAVY_HEX)
            p = c.paragraphs[0]
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after  = Pt(4)
            rn = p.add_run(col_names[i] if i < len(col_names) else "")
            rn.bold = True; rn.font.size = Pt(9); rn.font.color.rgb = RGBColor(0xff, 0xff, 0xff)
        return tbl

    def _add_data_row(tbl, widths, cells_text, zebra=False, bold_first=True):
        r = tbl.add_row()
        r._tr.get_or_add_trPr().append(parse_xml(f'<w:cantSplit {nsdecls("w")}/>'))
        for i, c in enumerate(r.cells):
            c.width = Inches(widths[i])
            if zebra:
                _shd(c, LIGHT_BG)
            p = c.paragraphs[0]
            p.paragraph_format.space_before = Pt(3); p.paragraph_format.space_after = Pt(3)
            rn = p.add_run(str(cells_text[i]) if i < len(cells_text) else "")
            rn.bold = bold_first and i == 0; rn.font.size = Pt(8.5)

    doc = Document()
    for sec in doc.sections:
        sec.top_margin = Inches(0.70); sec.bottom_margin = Inches(0.70)
        sec.left_margin = Inches(0.75); sec.right_margin  = Inches(0.75)
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10)
    doc.styles["Normal"].font.color.rgb = RGBColor(0x1e, 0x29, 0x3b)

    client_data  = data.get("client", {}) or {}
    client_name  = client_data.get("company_name") or "Client"
    project_name = client_data.get("project_name") or "Business Automation & System Implementation"

    # Cover wordmark
    top_p = doc.add_paragraph()
    top_p.add_run(COMPANY_NAME).bold = True
    top_p.runs[0].font.size = Pt(9.5); top_p.runs[0].font.color.rgb = RGBColor(0x1a, 0x36, 0x5d)
    top_p.add_run("   |   Zoho Authorized Premium Partner").font.size = Pt(9)
    top_p.runs[-1].font.color.rgb = RGBColor(0x64, 0x74, 0x8b)
    top_p.paragraph_format.space_after = Pt(24)

    if os.path.exists(LOGO_PATH):
        lp = doc.add_paragraph()
        lp.alignment = docx.enum.text.WD_ALIGN_PARAGRAPH.CENTER
        lp.add_run().add_picture(LOGO_PATH, width=Inches(2.5))
        lp.paragraph_format.space_after = Pt(28)

    for text, sz, color, after in [
        (project_name.upper(), 22, RGBColor(0x1a, 0x36, 0x5d), 6),
        ("PROJECT PROPOSAL & COMPREHENSIVE SCOPE OF WORK", 11, RGBColor(0x00, 0x80, 0x80), 36),
    ]:
        p = doc.add_paragraph()
        p.alignment = docx.enum.text.WD_ALIGN_PARAGRAPH.CENTER
        rn = p.add_run(text)
        rn.bold = True; rn.font.size = Pt(sz); rn.font.color.rgb = color
        p.paragraph_format.space_after = Pt(after)

    pp = doc.add_paragraph()
    pp.alignment = docx.enum.text.WD_ALIGN_PARAGRAPH.CENTER
    r1 = pp.add_run("Prepared for\n"); r1.font.size = Pt(10); r1.font.color.rgb = RGBColor(0x64, 0x74, 0x8b)
    r2 = pp.add_run(client_name); r2.bold = True; r2.font.size = Pt(15); r2.font.color.rgb = RGBColor(0x0f, 0x17, 0x2a)
    pp.paragraph_format.space_after = Pt(40)

    cover_tbl = doc.add_table(rows=1, cols=4)
    cover_tbl.style = "Table Grid"
    _cell_margins(cover_tbl, top=100, bot=100, left=140, right=140)
    _borders(cover_tbl)
    for i, (label, val) in enumerate([
        ("PRESENTED BY:", COMPANY_NAME),
        ("PRESENTED TO:", client_name),
        ("DATE:", _ordinal_day()),
        ("DOCUMENT STATUS:", data.get("status", "DRAFT")),
    ]):
        cell = cover_tbl.cell(0, i); cell.width = Inches(1.75)
        _shd(cell, "f1f5f9")
        p = cell.paragraphs[0]; p.paragraph_format.space_after = Pt(2)
        lr = p.add_run(label + "\n"); lr.bold = True; lr.font.size = Pt(8); lr.font.color.rgb = RGBColor(0x1a, 0x36, 0x5d)
        vr = p.add_run(val); vr.font.size = Pt(9.5); vr.font.color.rgb = RGBColor(0x1e, 0x29, 0x3b)
    doc.add_page_break()

    # helpers
    def section_h(num, title):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(14); p.paragraph_format.space_after = Pt(6)
        rn = p.add_run(f"{num}. {title}")
        rn.bold = True; rn.font.size = Pt(13); rn.font.color.rgb = RGBColor(0x1a, 0x36, 0x5d)

    def module_h(title):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(10); p.paragraph_format.space_after = Pt(4)
        rn = p.add_run(title)
        rn.bold = True; rn.font.size = Pt(11.5); rn.font.color.rgb = RGBColor(0x00, 0x80, 0x80)

    def body_text(text, after=8):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(after); p.paragraph_format.line_spacing = 1.25
        rn = p.add_run(str(text)); rn.font.size = Pt(10)

    def cat_bullets(cat_name, items):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6); p.paragraph_format.space_after = Pt(2)
        rn = p.add_run(f"\u2022 {cat_name}")
        rn.bold = True; rn.font.size = Pt(10.5); rn.font.color.rgb = RGBColor(0x1a, 0x36, 0x5d)
        for it in items:
            ip = doc.add_paragraph(style="List Bullet")
            ip.paragraph_format.space_after = Pt(1)
            ip.add_run(str(it)).font.size = Pt(9.5)

    def info_box(title, items):
        if not items:
            return
        tbl = doc.add_table(rows=1, cols=1); tbl.style = "Table Grid"
        _cell_margins(tbl, top=90, bot=90, left=140, right=140)
        cell = tbl.cell(0, 0); cell.width = Inches(7.0)
        _shd(cell, "f0f7f7")
        tcPr = cell._tc.get_or_add_tcPr()
        tcPr.append(parse_xml(
            f'<w:tcBorders {nsdecls("w")}>'
            f'<w:left w:val="single" w:sz="24" w:space="0" w:color="{TEAL_HEX}"/>'
            f'<w:top w:val="none"/><w:right w:val="none"/><w:bottom w:val="none"/>'
            f'</w:tcBorders>'
        ))
        p = cell.paragraphs[0]; p.paragraph_format.space_after = Pt(3)
        hr = p.add_run(f"Implementation Notes \u2014 {title}\n")
        hr.bold = True; hr.font.size = Pt(9.5); hr.font.color.rgb = RGBColor(0x00, 0x80, 0x80)
        for it in items:
            ip = cell.add_paragraph(style="List Bullet")
            ip.paragraph_format.space_after = Pt(2)
            ip.add_run(str(it)).font.size = Pt(9)
        doc.add_paragraph().paragraph_format.space_after = Pt(4)

    # Section 1
    section_h("1", "Executive Summary & Requirements Analysis")
    body_text(data.get("project_introduction", ""))
    ov_tbl = _make_table(2, [2.2, 4.8], ["Project Parameter", "Specification & Implementation Details"])
    for idx, row in enumerate(data.get("project_overview_table", [])):
        _add_data_row(ov_tbl, [2.2, 4.8], [row.get("parameter", ""), row.get("details", "")], zebra=idx % 2 == 1)
    doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # Section 2 — Scope
    section_h("2", "Detailed Scope of Work & Configuration Matrix")
    body_text("The following section outlines the functional scope, configuration areas, and business logic grouped by product module:", after=8)
    for mi, mod in enumerate(data.get("scope", []), 1):
        prod = mod.get("product", f"Module {mi}")
        module_h(f"2.{mi}  {prod} \u2014 Functional Scope & Configuration Specification")
        p_ov = doc.add_paragraph(); p_ov.paragraph_format.space_after = Pt(4)
        rn = p_ov.add_run(f"Module Purpose:  {mod.get('overview', '')}")
        rn.italic = True; rn.font.size = Pt(9.5); rn.font.color.rgb = RGBColor(0x47, 0x55, 0x69)
        cfg_rows = mod.get("configuration_table", [])
        if cfg_rows:
            w = [1.8, 3.3, 1.9]
            scope_tbl = _make_table(3, w, ["Configuration Area", "Scope & Key Activities", "Business Rules & Logic"])
            for ri, crow in enumerate(cfg_rows):
                r = scope_tbl.add_row()
                r._tr.get_or_add_trPr().append(parse_xml(f'<w:cantSplit {nsdecls("w")}/>'))
                for ci, c in enumerate(r.cells):
                    c.width = Inches(w[ci])
                    if ri % 2 == 1:
                        _shd(c, LIGHT_BG)
                    p = c.paragraphs[0]
                    p.paragraph_format.space_before = Pt(3); p.paragraph_format.space_after = Pt(3)
                    text = [crow.get("area", ""), crow.get("scope_description", ""), crow.get("business_rules", "")][ci]
                    rn = p.add_run(str(text)); rn.font.size = Pt(8.5)
                    if ci == 0:
                        rn.bold = True
            doc.add_paragraph().paragraph_format.space_after = Pt(4)
        info_box(prod, mod.get("key_considerations", []))

    # Section 3 — Integration (optional)
    integ = data.get("integrations_table", [])
    if integ:
        section_h("3", "System Integration & Data Flow Architecture")
        body_text("The following matrix outlines system-to-system integrations, data synchronization directions, and transactional logic:", after=6)
        w = [1.8, 1.4, 1.6, 2.2]
        int_tbl = _make_table(4, w, ["Interface / Flow", "Source \u2192 Target", "Sync Mode / Frequency", "Data Objects & Business Logic"])
        for idx, irow in enumerate(integ):
            _add_data_row(int_tbl, w, [
                irow.get("interface", ""),
                f"{irow.get('source_system','')} \u2192 {irow.get('target_system','')}",
                irow.get("sync_mode", ""), irow.get("logic", ""),
            ], zebra=idx % 2 == 1)
        doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # Section 4 — Prerequisites
    section_h("4", "Client Pre-requisites & Dependencies")
    body_text("To ensure timely project kickoff, the client will provide the following:", after=6)
    for cat, items in data.get("categorized_prerequisites", {}).items():
        cat_bullets(cat, items)
    doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # Section 5 — Deliverables
    section_h("5", "Project Deliverables & Acceptance Criteria")
    body_text("Wooplix Technologies will deliver the following verified artifacts and milestones:", after=6)
    for cat, items in data.get("categorized_deliverables", {}).items():
        cat_bullets(cat, items)
    doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # Section 6 — Open Points
    open_pts = data.get("open_points", []) or []
    if open_pts:
        section_h("6", "Points to be Finalized During Discovery (Open Points)")
        body_text("The following items require collaborative confirmation during initial discovery workshops:", after=4)
        for pt in open_pts:
            ip = doc.add_paragraph(style="List Bullet")
            ip.paragraph_format.space_after = Pt(2)
            ip.add_run(str(pt)).font.size = Pt(9.5)
        doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # Section 7 — Timeline
    tl = data.get("timeline") or {}
    phases = tl.get("phases", [])
    if phases:
        section_h("7", "Indicative Implementation Timeline & Milestone Roadmap")
        body_text("The implementation follows a staged rollout to ensure minimal operational disruption:", after=6)
        w = [2.0, 2.6, 1.1, 1.3]
        tl_tbl = _make_table(4, w, ["Milestone / Phase", "Key Activities & Focus", "Duration", "Milestone Gate Sign-off"])
        for idx, ph in enumerate(phases):
            _add_data_row(tl_tbl, w, [
                ph.get("phase", ""), ph.get("key_activities", ""),
                ph.get("duration", ""), ph.get("milestone", ph.get("milestone_deliverable", "")),
            ], zebra=idx % 2 == 1)
        if tl.get("overall"):
            p = doc.add_paragraph(); p.paragraph_format.space_before = Pt(6)
            p.add_run("Estimated Total Duration: ").bold = True
            p.add_run(str(tl["overall"]))
        doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # Section 8 — Commercials
    com = data.get("commercials")
    if com and isinstance(com, dict) and com.get("items"):
        section_h("8", "Commercials & Professional Investment")
        w = [0.5, 3.5, 1.8, 1.2]
        com_tbl = _make_table(4, w, ["#", "Scope / Deliverable Description", "Basis / Resource Effort", "Investment"])
        for idx, it in enumerate(com["items"], 1):
            _add_data_row(com_tbl, w, [str(idx), it.get("item", ""), it.get("basis", ""), it.get("amount", "")], zebra=idx % 2 == 1)
        if com.get("total"):
            p = doc.add_paragraph(); p.paragraph_format.space_before = Pt(6)
            p.add_run("Total Investment: ").bold = True; p.add_run(str(com["total"]))
        if com.get("note"):
            doc.add_paragraph().add_run(f"Commercial Terms: {com['note']}").italic = True
        doc.add_paragraph().paragraph_format.space_after = Pt(6)

    # Sign-off
    last_sec = "9" if not (com and isinstance(com, dict) and com.get("items")) else "10"
    section_h(last_sec, "Project Governance & Formal Acceptance Sign-Off")
    body_text("By signing below, both parties acknowledge the scope, deliverables, assumptions, and pre-requisites:", after=12)
    sg_tbl = doc.add_table(rows=1, cols=3)
    for row in sg_tbl.rows:
        row.cells[0].width = Inches(3.0)
        row.cells[1].width = Inches(1.0)
        row.cells[2].width = Inches(3.0)
    
    p0 = sg_tbl.cell(0, 0).paragraphs[0]
    p0.add_run("Accepted by Client:\n\n").bold = True
    p0.add_run(f"{client_name}\n\n\n\n").bold = True
    p0.add_run("Authorized Signatory\n").font.size = Pt(10)
    p0.add_run("Name: _______________________\n").font.size = Pt(10)
    p0.add_run("Date: _______________________\n").font.size = Pt(10)
    
    p2 = sg_tbl.cell(0, 2).paragraphs[0]
    p2.add_run(f"For {COMPANY_NAME}:\n\n").bold = True
    p2.add_run("\n\n\n\n")
    p2.add_run("Authorized Signatory\n").font.size = Pt(10)
    p2.add_run("Name: _______________________\n").font.size = Pt(10)
    p2.add_run("Date: _______________________\n").font.size = Pt(10)

    ft = doc.add_paragraph()
    ftr = ft.add_run(f"{COMPANY_NAME}   |   {COMPANY_EMAIL}   |   {COMPANY_WEBSITE}")
    ftr.font.size = Pt(8.5); ftr.font.color.rgb = RGBColor(0x64, 0x74, 0x8b)
    doc.save(output)
    return output


# --------------------------------------------------------------------------- HTML + PDF (dompdf)
def _logo_data_uri():
    if not os.path.exists(LOGO_PATH):
        return ""
    with open(LOGO_PATH, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode()


def build_html(data):
    """Render house-style proposal HTML for Dompdf.
    Dompdf notes: position:fixed on tables works; flex/grid do not.
    """
    data = normalize_proposal(data)

    def esc(x):
        return (str(x if x is not None else "")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    client      = data.get("client", {}) or {}
    client_name = client.get("company_name") or "Client"
    project     = client.get("project_name") or "Proposal & Scope of Work"
    hdr         = re.sub(r'["\\\r\n]', " ", project)[:60]
    logo        = _logo_data_uri()

    out = []
    out.append(f"""<!doctype html><html><head><meta charset="utf-8">
<style>
@page {{ size: A4; margin: 22mm 15mm 20mm 15mm; }}
body {{ font-family: 'DejaVu Sans', sans-serif; font-size: 9pt; color: #1e293b; line-height: 1.5; }}
table.hdr-tbl {{ position: fixed; top: -16mm; left: 0; right: 0; width: 100%;
    border-bottom: 1.5px solid #1a365d; font-size: 7.5pt; color: #64748b; padding-bottom: 2px; }}
table.ftr-tbl {{ position: fixed; bottom: -14mm; left: 0; right: 0; width: 100%;
    border-top: 1.5px solid #1a365d; font-size: 7.5pt; color: #64748b; padding-top: 2px; }}
table.hdr-tbl td, table.ftr-tbl td {{ padding: 1px 2px; }}
.pg:before {{ content: counter(page); }}
.cover-top {{ border-bottom: 2px solid #1a365d; padding-bottom: 4mm; margin-bottom: 14mm; font-size: 8.5pt; color: #1a365d; font-weight: bold; }}
.cover-right {{ float: right; color: #008080; }}
.cover-logo {{ width: 55mm; display: block; margin: 0 auto 10mm auto; }}
.cover-title {{ font-size: 19pt; font-weight: bold; color: #1a365d; line-height: 1.25; margin-bottom: 4mm; text-transform: uppercase; text-align: center; }}
.cover-sub {{ font-size: 10.5pt; font-weight: bold; color: #008080; margin-bottom: 12mm; text-transform: uppercase; text-align: center; }}
.cover-prep {{ font-size: 9pt; color: #64748b; margin-bottom: 2mm; text-align: center; }}
.cover-client {{ font-size: 14pt; font-weight: bold; color: #0f172a; margin-bottom: 6mm; text-align: center; }}
.cover-desc {{ font-size: 9pt; font-style: italic; color: #475569; margin: 0 auto 14mm auto; text-align: center; }}
table.cover-meta {{ width: 100%; border-collapse: collapse; margin-top: 18mm; }}
table.cover-meta td {{ border: 1px solid #cbd5e1; background: #f8fafc; padding: 7px 9px; width: 25%; vertical-align: top; }}
.lbl {{ font-size: 7.5pt; font-weight: bold; color: #1a365d; text-transform: uppercase; display: block; margin-bottom: 2px; }}
.val {{ font-size: 9pt; font-weight: 600; color: #1e293b; }}
h2.sh {{ background-color: #1a365d; color: #fff; padding: 6px 10px; font-size: 11pt; font-weight: bold; border-radius: 3px; margin: 18px 0 8px 0; }}
h3.mh {{ color: #008080; font-size: 10pt; font-weight: bold; border-bottom: 1.5px solid #008080; padding-bottom: 3px; margin: 14px 0 6px 0; }}
p.body {{ font-size: 9pt; margin: 0 0 8px 0; line-height: 1.45; }}
p.desc {{ font-size: 9pt; font-style: italic; color: #475569; margin: 0 0 6px 0; }}
table.dt {{ width: 100%; border-collapse: collapse; margin: 6px 0 12px 0; font-size: 8.5pt; }}
table.dt th {{ background-color: #1a365d; color: #fff; font-weight: bold; text-align: left; padding: 6px 8px; border: 1px solid #1a365d; font-size: 8pt; text-transform: uppercase; }}
table.dt td {{ border: 1px solid #cbd5e1; padding: 5px 8px; vertical-align: top; line-height: 1.35; }}
table.dt tr:nth-child(even) td {{ background-color: #f8fafc; }}
.cat-h {{ font-size: 9.5pt; font-weight: bold; color: #1a365d; margin: 8px 0 2px 0; }}
ul.cat-ul {{ margin: 2px 0 8px 16px; padding: 0; }}
ul.cat-ul li {{ font-size: 9pt; margin-bottom: 3px; }}
.ib {{ background-color: #f0f7f7; border-left: 4px solid #008080; padding: 8px 12px; margin: 8px 0 12px 0; border-radius: 0 3px 3px 0; }}
.ib h4 {{ margin: 0 0 4px 0; font-size: 9pt; color: #008080; font-weight: bold; }}
.ib ul {{ margin: 0; padding-left: 16px; }}
.ib li {{ font-size: 8.5pt; color: #334155; margin-bottom: 2px; }}
table.sign {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
table.sign th {{ background: #1a365d; color: #fff; padding: 6px 10px; font-size: 8.5pt; text-align: left; border: 1px solid #1a365d; }}
table.sign td {{ border: 1px solid #cbd5e1; padding: 10px 12px; width: 50%; vertical-align: top; font-size: 8.5pt; line-height: 2.0; }}
</style></head><body>""")

    out.append(f"""<table class="hdr-tbl"><tr>
<td>{esc(hdr)} &#8212; Project Proposal &amp; SOW</td>
<td style="text-align:right;">{esc(COMPANY_NAME)} &nbsp;|&nbsp; Page <span class="pg"></span></td>
</tr></table>""")
    out.append(f"""<table class="ftr-tbl"><tr>
<td>{esc(COMPANY_EMAIL)} &nbsp;|&nbsp; {esc(COMPANY_WEBSITE)}</td>
<td style="text-align:right;">Confidential &#8212; For Client Review Only</td>
</tr></table>""")

    # Cover
    out.append('<div style="page-break-after: always; padding-top: 12mm;">')
    out.append(f'<div class="cover-top"><span>{esc(COMPANY_NAME)}</span><span class="cover-right">ZOHO AUTHORIZED PREMIUM PARTNER</span></div>')
    if logo:
        out.append(f'<img src="{logo}" class="cover-logo" alt="Wooplix">')
    out.append(f'<div class="cover-title">{esc(project)}</div>')
    out.append('<div class="cover-sub">Project Proposal &amp; Comprehensive Scope of Work</div>')
    out.append('<div class="cover-prep">Prepared for</div>')
    out.append(f'<div class="cover-client">{esc(client_name)}</div>')
    out.append('<div class="cover-desc">A complete implementation roadmap, functional specification matrix, integration architecture, and delivery plan.</div>')
    out.append('<table class="cover-meta"><tr>'
               f'<td><span class="lbl">PRESENTED BY:</span><span class="val">{esc(COMPANY_NAME)}</span></td>'
               f'<td><span class="lbl">PRESENTED TO:</span><span class="val">{esc(client_name)}</span></td>'
               f'<td><span class="lbl">DATE:</span><span class="val">{esc(_ordinal_day())}</span></td>'
               f'<td><span class="lbl">DOCUMENT STATUS:</span><span class="val">{esc(data.get("status","DRAFT"))}</span></td>'
               '</tr></table>')
    out.append('</div>')

    # Sec 1
    out.append('<h2 class="sh">1. Executive Summary &amp; Requirements Analysis</h2>')
    out.append(f'<p class="body">{esc(data.get("project_introduction",""))}</p>')
    out.append('<table class="dt"><tr><th style="width:30%;">Project Parameter</th><th style="width:70%;">Specification &amp; Details</th></tr>')
    for row in data.get("project_overview_table", []):
        out.append(f'<tr><td><strong>{esc(row.get("parameter",""))}</strong></td><td>{esc(row.get("details",""))}</td></tr>')
    out.append('</table>')

    # Sec 2
    out.append('<h2 class="sh">2. Detailed Scope of Work &amp; Configuration Matrix</h2>')
    out.append('<p class="body">The following section outlines the functional scope, configuration areas, and business logic grouped by product module:</p>')
    for mi, mod in enumerate(data.get("scope", []), 1):
        prod = mod.get("product", f"Module {mi}")
        out.append(f'<h3 class="mh">2.{mi}  {esc(prod)} &#8212; Functional Scope &amp; Configuration Specification</h3>')
        if mod.get("overview"):
            out.append(f'<p class="desc"><strong>Module Purpose:</strong>  {esc(mod["overview"])}</p>')
        cfg_rows = mod.get("configuration_table", [])
        if cfg_rows:
            out.append('<table class="dt"><tr><th style="width:25%;">Configuration Area</th><th style="width:45%;">Scope &amp; Key Activities</th><th style="width:30%;">Business Rules &amp; Logic</th></tr>')
            for crow in cfg_rows:
                desc = esc(crow.get("scope_description", "")).replace("\n", "<br>")
                out.append(f'<tr><td><strong>{esc(crow.get("area",""))}</strong></td><td>{desc}</td><td>{esc(crow.get("business_rules",""))}</td></tr>')
            out.append('</table>')
        cons = mod.get("key_considerations", [])
        if cons:
            out.append(f'<div class="ib"><h4>Implementation Notes &#8212; {esc(prod)}</h4><ul>')
            for c in cons:
                out.append(f'<li>{esc(c)}</li>')
            out.append('</ul></div>')

    # Sec 3 — Integration
    integ = data.get("integrations_table", [])
    if integ:
        out.append('<h2 class="sh">3. System Integration &amp; Data Flow Architecture</h2>')
        out.append('<p class="body">The following matrix outlines system-to-system integrations, synchronization directions, and transactional logic:</p>')
        out.append('<table class="dt"><tr><th style="width:24%;">Interface / Flow</th><th style="width:20%;">Source &#8594; Target</th><th style="width:22%;">Sync Mode / Frequency</th><th style="width:34%;">Data Objects &amp; Business Logic</th></tr>')
        for irow in integ:
            out.append(f'<tr><td><strong>{esc(irow.get("interface",""))}</strong></td>'
                       f'<td>{esc(irow.get("source_system",""))} &#8594; {esc(irow.get("target_system",""))}</td>'
                       f'<td>{esc(irow.get("sync_mode",""))}</td><td>{esc(irow.get("logic",""))}</td></tr>')
        out.append('</table>')

    # Sec 4 — Prerequisites
    out.append('<h2 class="sh">4. Client Pre-requisites &amp; Dependencies</h2>')
    out.append('<p class="body">To ensure timely project kickoff, the client will provide the following dependencies:</p>')
    for cat, items in data.get("categorized_prerequisites", {}).items():
        out.append(f'<div class="cat-h">&#8226; {esc(cat)}</div><ul class="cat-ul">')
        for it in items:
            out.append(f'<li>{esc(it)}</li>')
        out.append('</ul>')

    # Sec 5 — Deliverables
    out.append('<h2 class="sh">5. Project Deliverables &amp; Acceptance Criteria</h2>')
    out.append('<p class="body">Wooplix Technologies will deliver the following verified artifacts and milestones:</p>')
    for cat, items in data.get("categorized_deliverables", {}).items():
        out.append(f'<div class="cat-h">&#8226; {esc(cat)}</div><ul class="cat-ul">')
        for it in items:
            out.append(f'<li>{esc(it)}</li>')
        out.append('</ul>')

    # Sec 6 — Open Points
    open_pts = data.get("open_points", []) or []
    if open_pts:
        out.append('<h2 class="sh">6. Points to be Finalized During Discovery</h2>')
        out.append('<p class="body">The following items require collaborative confirmation during initial discovery workshops:</p>')
        out.append('<div class="ib"><ul>')
        for pt in open_pts:
            out.append(f'<li>{esc(pt)}</li>')
        out.append('</ul></div>')

    # Sec 7 — Timeline
    tl = data.get("timeline") or {}
    phases = tl.get("phases", [])
    if phases:
        out.append('<h2 class="sh">7. Indicative Implementation Timeline &amp; Milestone Roadmap</h2>')
        out.append('<p class="body">The implementation follows a staged rollout to ensure minimal operational disruption:</p>')
        out.append('<table class="dt"><tr><th style="width:26%;">Milestone / Phase</th><th style="width:40%;">Key Activities &amp; Focus</th><th style="width:14%;">Duration</th><th style="width:20%;">Milestone Gate Sign-off</th></tr>')
        for ph in phases:
            out.append(f'<tr><td><strong>{esc(ph.get("phase",""))}</strong></td>'
                       f'<td>{esc(ph.get("key_activities",""))}</td>'
                       f'<td>{esc(ph.get("duration",""))}</td>'
                       f'<td>{esc(ph.get("milestone",ph.get("milestone_deliverable","")))}</td></tr>')
        out.append('</table>')
        if tl.get("overall"):
            out.append(f'<p class="body"><strong>Estimated Total Duration:</strong>  {esc(tl["overall"])}</p>')

    # Sec 8 — Commercials
    com = data.get("commercials")
    if com and isinstance(com, dict) and com.get("items"):
        out.append('<h2 class="sh">8. Commercials &amp; Professional Investment</h2>')
        out.append('<table class="dt"><tr><th style="width:6%;">#</th><th style="width:46%;">Scope / Deliverable Description</th><th style="width:24%;">Basis / Resource Effort</th><th style="width:24%;">Investment</th></tr>')
        for idx, it in enumerate(com["items"], 1):
            out.append(f'<tr><td><strong>{idx}</strong></td><td>{esc(it.get("item",""))}</td><td>{esc(it.get("basis",""))}</td><td>{esc(it.get("amount",""))}</td></tr>')
        out.append('</table>')
        if com.get("total"):
            out.append(f'<p class="body"><strong>Total Investment:</strong>  {esc(com["total"])}</p>')
        if com.get("note"):
            out.append(f'<p class="body"><em>Commercial Terms: {esc(com["note"])}</em></p>')

    # Sign-off
    last_sec = "9" if not (com and isinstance(com, dict) and com.get("items")) else "10"
    out.append(f'<h2 class="sh">{last_sec}. Project Governance &amp; Formal Acceptance Sign-Off</h2>')
    out.append('<p class="body">By signing below, both parties acknowledge the scope, deliverables, assumptions, and pre-requisites:</p>')
    out.append('<table style="width: 100%; border-collapse: collapse; margin-top: 24px;"><tr>'
               f'<td style="width: 45%; vertical-align: top; font-size: 9pt;"><strong>Accepted by Client:</strong><br><br><strong>{esc(client_name)}</strong><br><br><br><br>Authorized Signatory<br><br>Name: _______________________<br><br>Date: _______________________</td>'
               '<td style="width: 10%;"></td>'
               f'<td style="width: 45%; vertical-align: top; font-size: 9pt;"><strong>For {esc(COMPANY_NAME)}:</strong><br><br><br><br><br><br>Authorized Signatory<br><br>Name: _______________________<br><br>Date: _______________________</td>'
               '</tr></table>')
    out.append('</body></html>')
    return "".join(out)


def build_pdf(data, output):
    """Build the branded PDF by rendering house-style HTML through dompdf (PHP)."""
    if not os.path.exists(PHP_SCRIPT):
        raise SystemExit(f"html_to_pdf.php not found at {PHP_SCRIPT}")
    php = shutil.which("php")
    if not php:
        raise SystemExit("PHP is not installed or not on PATH. Install it (e.g. 'brew install php') "
                         "or run 'composer install' inside this folder for dompdf.")
    if not os.path.exists(str(HERE / "vendor" / "autoload.php")):
        raise SystemExit("dompdf is not installed. Run 'composer install' inside this folder.")

    html = build_html(data)
    fd, html_path = tempfile.mkstemp(suffix=".html", prefix="wooplix_proposal_", dir=str(HERE))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(html)
        proc = subprocess.run([php, PHP_SCRIPT, html_path, output],
                              capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.exists(output):
            raise SystemExit(f"dompdf failed:\n{proc.stderr.strip()[:800]}")
    finally:
        try:
            os.remove(html_path)
        except OSError:
            pass
    return output


# --------------------------------------------------------------------------- interactive input
def interactive_requirement():
    """Ask the user for a requirement: either a file path or pasted text."""
    print("\n=== Wooplix Proposal Agent ===")
    print("You can give the requirement as a FILE or as TEXT.\n")
    path = input("Enter the requirement FILE path (drag the file here),\n"
                 "or just press Enter to type/paste text: ").strip().strip('"').strip("'")
    if path:
        if not os.path.exists(path):
            raise SystemExit(f"File not found: {path}")
        return extract_requirement(path)

    print("\nPaste your requirement text below.")
    print("When finished, type  END  on a new line and press Enter  (or press Ctrl-D).\n")
    lines = []
    try:
        while True:
            line = input()
            if line.strip().upper() == "END":
                break
            lines.append(line)
    except EOFError:
        pass
    text = "\n".join(lines).strip()
    if not text:
        raise SystemExit("No requirement text entered.")
    return text


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Wooplix Proposal Agent")
    ap.add_argument("requirement", nargs="?", default=None,
                    help="path to .txt/.md/.docx/.pdf, or '-' for stdin text; "
                         "if omitted, the agent asks you interactively")
    ap.add_argument("--out", default=str(HERE / "out"), help="output directory")
    ap.add_argument("--model", default=None, help="override GROQ_MODEL")
    ap.add_argument("--skip-crm", action="store_true", help="do not pull CRM precedent")
    ap.add_argument("--workdrive", action="store_true", help="also upload outputs to Zoho WorkDrive")
    args = ap.parse_args()

    global GROQ_MODEL
    if args.model:
        GROQ_MODEL = args.model
    if not GROQ_API_KEY:
        raise SystemExit("GROQ_API_KEY is not set. Put it in .env next to this script or export it.")

    os.makedirs(args.out, exist_ok=True)
    if args.requirement is None:
        req_text = interactive_requirement()
    else:
        req_text = extract_requirement(args.requirement)
    print(f"[1/4] Requirement: {len(req_text):,} chars")

    reference_text = ""
    if not args.skip_crm and ZOHO_REFRESH_TOKEN and ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET:
        try:
            reference_text = fetch_crm_precedent()
            print(f"[2/4] CRM precedent loaded from '{ZOHO_CRM_MODULE}'.")
        except Exception as e:
            print(f"[2/4] CRM precedent unavailable (continuing without): {str(e)[:200]}")
    else:
        print("[2/4] Skipping CRM precedent.")

    data = draft_proposal(req_text, reference_text)
    client_name = (data.get("client") or {}).get("company_name") or "Client"
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", client_name).strip("_") or "Client"
    print(f"[3/4] Drafted for: {client_name}")

    docx_path = os.path.join(args.out, f"Wooplix_Proposal_{safe}.docx")
    pdf_path = os.path.join(args.out, f"Wooplix_Proposal_{safe}.pdf")
    build_docx(data, docx_path)
    build_pdf(data, pdf_path)
    print(f"[4/4] DOCX: {docx_path}\n      PDF : {pdf_path}")

    if args.workdrive:
        for p in (docx_path, pdf_path):
            try:
                print("      WorkDrive:", upload_workdrive(p))
            except Exception as e:
                print(f"      WorkDrive upload skipped for {os.path.basename(p)}: {str(e)[:200]}")

    json_path = os.path.join(args.out, f"Wooplix_Proposal_{safe}.json")
    with open(json_path, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    print("      JSON:", json_path)


if __name__ == "__main__":
    main()
