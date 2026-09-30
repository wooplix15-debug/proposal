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


# --------------------------------------------------------------------------- DOCX (house style)
def build_docx(data, output):
    from datetime import datetime
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor

    INK = RGBColor(0x11, 0x11, 0x11)

    def ordinal_day(d):
        day = d.day
        suf = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return f"{day}{suf} {d.strftime('%b, %Y')}"

    def h(text, size=13, before=10, after=4):
        p = doc.add_paragraph()
        r = p.add_run(text); r.bold = True; r.font.size = Pt(size); r.font.color.rgb = INK
        p.paragraph_format.space_before = Pt(before); p.paragraph_format.space_after = Pt(after)
        return p

    def sub(text):
        p = doc.add_paragraph()
        r = p.add_run(text); r.bold = True; r.font.size = Pt(11); r.font.color.rgb = INK
        p.paragraph_format.space_before = Pt(6); p.paragraph_format.space_after = Pt(2)
        return p

    def bullet(text):
        p = doc.add_paragraph(str(text), style="List Bullet")
        for r in p.runs:
            r.font.size = Pt(10.5)
        p.paragraph_format.space_after = Pt(1)
        return p

    def para(text, size=10.5):
        p = doc.add_paragraph()
        r = p.add_run(str(text)); r.font.size = Pt(size)
        p.paragraph_format.space_after = Pt(6)
        return p

    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)
    client = data.get("client", {}) or {}
    client_name = client.get("company_name") or "Client"

    if os.path.exists(LOGO_PATH):
        lp = doc.add_paragraph()
        lp.add_run().add_picture(LOGO_PATH, width=Inches(2.0))
        lp.paragraph_format.space_after = Pt(2)
    para(f"{COMPANY_NAME}  |  {COMPANY_TAGLINE}", size=9)

    tbl = doc.add_table(rows=1, cols=3)
    tbl.style = "Table Grid"
    for idx, (label, value) in enumerate([
        ("PRESENTED BY:", COMPANY_NAME),
        ("PRESENTED TO:", client_name),
        ("DATE:", ordinal_day(datetime.now())),
    ]):
        cell = tbl.cell(0, idx)
        cell.paragraphs[0].clear()
        lr = cell.paragraphs[0].add_run(label); lr.bold = True; lr.font.size = Pt(9)
        vr = cell.add_paragraph().add_run(value); vr.font.size = Pt(10)

    if client.get("project_name"):
        h(client["project_name"], size=16, before=12)

    h("Project Introduction", 14)
    para(data.get("project_introduction") or "")

    h("Project Scope", 14)
    for prod in data.get("scope", []) or []:
        if prod.get("product"):
            h(prod["product"], size=12, before=8, after=2)
        for area in prod.get("areas", []) or []:
            sub(area.get("area", ""))
            for task in area.get("tasks", []) or []:
                if str(task).strip():
                    bullet(task)

    h("Pre-requisites", 14)
    para("To begin the implementation, we will require:")
    for item in data.get("prerequisites", []) or []:
        if str(item).strip():
            bullet(item)

    h("Deliverables", 14)
    for item in data.get("deliverables", []) or []:
        if str(item).strip():
            bullet(item)

    for item in data.get("open_points", []) or []:
        if str(item).strip():
            if not any(p.text.startswith("Points to be Finalized") for p in doc.paragraphs):
                h("Points to be Finalized During Discovery", 14)
            bullet(item)

    tl = data.get("timeline") or None
    if tl:
        h("Indicative Timeline", 14)
        for ph in tl.get("phases", []) or []:
            bullet(f"{ph.get('phase','')} – {ph.get('duration','')}")
        if tl.get("overall"):
            para(f"Overall: {tl['overall']}")

    com = data.get("commercials") or None
    if com:
        h("Commercials", 14)
        ct = doc.add_table(rows=1, cols=3)
        ct.style = "Table Grid"
        for c, label in zip(ct.rows[0].cells, ["Item", "Amount", "Basis"]):
            c.paragraphs[0].clear()
            r = c.paragraphs[0].add_run(label); r.bold = True; r.font.size = Pt(9.5)
        for it in com.get("items", []) or []:
            row = ct.add_row().cells
            row[0].text = str(it.get("item", ""))
            row[1].text = str(it.get("amount", ""))
            row[2].text = str(it.get("basis", ""))
        if com.get("total"):
            para(f"Total: {com['total']}")
        if com.get("note"):
            para(com["note"], size=9.5)

    doc.add_paragraph()
    para(f"{COMPANY_NAME}  |  {COMPANY_EMAIL}  |  {COMPANY_WEBSITE}", size=9)
    doc.save(output)
    return output


# --------------------------------------------------------------------------- HTML + PDF (dompdf)
def _logo_data_uri():
    if not os.path.exists(LOGO_PATH):
        return ""
    with open(LOGO_PATH, "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode()


def build_html(data):
    """Render the house-style proposal as a standalone HTML string (logo embedded)."""
    from datetime import datetime

    def ordinal_day(d):
        day = d.day
        suf = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        return f"{day}{suf} {d.strftime('%b, %Y')}"

    def esc(x):
        return (str(x if x is not None else "")
                .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    client = data.get("client", {}) or {}
    client_name = client.get("company_name") or "Client"
    project = client.get("project_name") or "Proposal"
    # header/footer run through CSS content strings: strip chars that would break them
    hdr = re.sub(r'["\\\r\n]', " ", project)[:60]

    parts = []
    parts.append(f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 22mm 18mm 18mm 18mm; }}
body {{ font-family:'DejaVu Sans', sans-serif; font-size:10pt; color:#111; line-height:1.4; }}
.hdr {{ position:fixed; top:-15mm; left:0; right:0; font-size:7.5pt; color:#555;
        border-bottom:0.6pt solid #D0D0D0; padding-bottom:2mm; }}
.ftr {{ position:fixed; bottom:-13mm; left:0; right:0; font-size:7.5pt; color:#555;
        border-top:0.6pt solid #D0D0D0; padding-top:2mm; }}
.hdr .r, .ftr .r {{ float:right; }}
.pg:before {{ content: counter(page); }}
.tagline {{ font-size:8.5pt; color:#555; margin:2px 0 8px 0; }}
table.cover {{ width:100%; border-collapse:collapse; margin:6px 0 10px 0; }}
table.cover td {{ border:0.6pt solid #D0D0D0; padding:5px 6px; vertical-align:top; font-size:9pt; }}
table.cover .lbl {{ font-weight:bold; font-size:8.5pt; }}
h1.title {{ font-size:17pt; margin:6px 0 4px 0; }}
h2 {{ font-size:13pt; margin:14px 0 4px 0; }}
h3.prod {{ font-size:12pt; margin:10px 0 2px 0; }}
p.area {{ font-weight:bold; font-size:10.5pt; margin:7px 0 2px 0; }}
p.body {{ margin:0 0 6px 0; }}
ul {{ margin:2px 0 6px 0; padding-left:16px; }}
li {{ font-size:10pt; margin:0 0 2px 0; }}
table.comm {{ width:100%; border-collapse:collapse; margin:6px 0; }}
table.comm th {{ background:#EDEDED; border:0.6pt solid #D0D0D0; padding:4px 6px; font-size:9pt; text-align:left; }}
table.comm td {{ border:0.6pt solid #D0D0D0; padding:4px 6px; font-size:9pt; }}
.note {{ font-size:8.5pt; color:#555; }}
</style></head><body>""")

    parts.append(f'<div class="hdr"><span>{esc(hdr)} — Proposal</span>'
                 f'<span class="r">{esc(COMPANY_NAME)} &nbsp;|&nbsp; Page <span class="pg"></span></span></div>')
    parts.append('<div class="ftr"><span>enquiry@wooplix.com</span>'
                 '<span class="r">https://www.wooplix.com/</span></div>')

    logo = _logo_data_uri()
    if logo:
        parts.append(f'<img src="{logo}" style="width:52mm;" alt="Wooplix">')
    parts.append(f'<div class="tagline">{esc(COMPANY_NAME)} &nbsp;|&nbsp; {esc(COMPANY_TAGLINE)}</div>')

    parts.append('<table class="cover"><tr>'
                 f'<td style="width:34%"><span class="lbl">PRESENTED BY:</span><br>{esc(COMPANY_NAME)}</td>'
                 f'<td style="width:36%"><span class="lbl">PRESENTED TO:</span><br>{esc(client_name)}</td>'
                 f'<td style="width:30%"><span class="lbl">DATE:</span><br>{ordinal_day(datetime.now())}</td>'
                 '</tr></table>')

    parts.append(f'<h1 class="title">{esc(project)}</h1>')

    parts.append('<h2>Project Introduction</h2>')
    parts.append(f'<p class="body">{esc(data.get("project_introduction"))}</p>')

    parts.append('<h2>Project Scope</h2>')
    for prod in data.get("scope", []) or []:
        if prod.get("product"):
            parts.append(f'<h3 class="prod">{esc(prod["product"])}</h3>')
        for area in prod.get("areas", []) or []:
            parts.append(f'<p class="area">{esc(area.get("area", ""))}</p>')
            tasks = [t for t in (area.get("tasks", []) or []) if str(t).strip()]
            if tasks:
                parts.append('<ul>' + "".join(f'<li>{esc(t)}</li>' for t in tasks) + '</ul>')

    parts.append('<h2>Pre-requisites</h2>')
    parts.append('<p class="body">To begin the implementation, we will require:</p>')
    pre = [x for x in (data.get("prerequisites", []) or []) if str(x).strip()]
    if pre:
        parts.append('<ul>' + "".join(f'<li>{esc(x)}</li>' for x in pre) + '</ul>')

    parts.append('<h2>Deliverables</h2>')
    dl = [x for x in (data.get("deliverables", []) or []) if str(x).strip()]
    if dl:
        parts.append('<ul>' + "".join(f'<li>{esc(x)}</li>' for x in dl) + '</ul>')

    op = [x for x in (data.get("open_points") or []) if str(x).strip()]
    if op:
        parts.append('<h2>Points to be Finalized During Discovery</h2>')
        parts.append('<ul>' + "".join(f'<li>{esc(x)}</li>' for x in op) + '</ul>')

    tl = data.get("timeline") or None
    if tl:
        parts.append('<h2>Indicative Timeline</h2>')
        ph = [p for p in (tl.get("phases", []) or []) if str(p.get("phase", "")).strip()]
        if ph:
            parts.append('<ul>' + "".join(
                f'<li>{esc(p.get("phase",""))} – {esc(p.get("duration",""))}</li>' for p in ph) + '</ul>')
        if tl.get("overall"):
            parts.append(f'<p class="body">Overall: {esc(tl["overall"])}</p>')

    com = data.get("commercials") or None
    if com:
        parts.append('<h2>Commercials</h2>')
        rows = ['<table class="comm"><tr><th>Item</th><th>Amount</th><th>Basis</th></tr>']
        for it in com.get("items", []) or []:
            rows.append(f'<tr><td>{esc(it.get("item",""))}</td><td>{esc(it.get("amount",""))}</td>'
                        f'<td>{esc(it.get("basis",""))}</td></tr>')
        rows.append('</table>')
        parts.append("".join(rows))
        if com.get("total"):
            parts.append(f'<p class="body">Total: {esc(com["total"])}</p>')
        if com.get("note"):
            parts.append(f'<p class="note">{esc(com["note"])}</p>')

    parts.append('</body></html>')
    return "".join(parts)


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
