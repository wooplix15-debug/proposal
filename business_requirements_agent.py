"""Independent agent for creating a source-grounded Business Requirements Document."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import wooplix_agent as brand
from brd_structure import SPECIFICATION_SECTIONS, source_tables, statement_tables, validated_model_tables


def _section_items(sections):
    """Assign stable BRD IDs to every distinct source requirement."""
    rows = []
    seen = set()
    number = 1
    for section in sections or []:
        product = str(section.get("product") or "Business requirement").strip()
        for requirement in section.get("requirements") or []:
            text = re.sub(r"\s+", " ", str(requirement)).strip()
            key = (product.casefold(), text.casefold())
            if not text or key in seen:
                continue
            seen.add(key)
            rows.append({"id": f"BR-{number:03}", "area": product, "requirement": text})
            number += 1
    return rows


def _source_list(requirement, section, next_headers):
    """Read literal list items beneath one source heading."""
    lines = requirement.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.match(rf"^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?{section}\b", line, re.I)), None)
    if start is None:
        return []
    result = []
    for line in lines[start + 1:]:
        numbered_heading = re.match(r"^\s*\d+(?:\.\d+)*[.)]?\s+[A-Z]", line)
        known_heading = any(re.match(rf"^\s*(?:\d+[.)]\s*)?{header}\b", line, re.I)
                            for header in next_headers)
        if numbered_heading or known_heading:
            break
        text = re.sub(r"^\s*(?:[-•*]|\d+[.)])\s*", "", line).strip()
        if text:
            result.append(text)
    return list(dict.fromkeys(result))


def _acceptance_table_rows(requirement):
    """Read acceptance criteria from flattened DOCX/PDF tables when present."""
    lines = requirement.splitlines()
    header = next((i for i, line in enumerate(lines)
                   if "acceptance criterion" in line.casefold()
                   and "verified by" in line.casefold()), None)
    if header is None:
        return []
    criteria = []
    for line in lines[header + 1:]:
        low = line.casefold()
        if any(marker in low for marker in ("name & designation", "name and designation")):
            break
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 3 or not cells[0].isdigit():
            continue
        criterion = re.sub(r"\s+", " ", cells[1]).strip()
        if criterion:
            criteria.append(criterion)
    return list(dict.fromkeys(criteria))


def _combine_explicit_shared_areas(requirement, sections):
    """Merge one shared source checklist when it explicitly names two products."""
    raw = requirement.casefold()
    pairs = [
        ("Zoho Marketing Automation", "Zoho Campaigns",
         bool(re.search(r"zoho\s+marketing\s+automation\s*[,/&]+\s*(?:zoho\s+)?campaign", raw))),
        ("Zoho Creator", "Zoho Backstage",
         bool(re.search(r"zoho\s+creator\s*[,/&]+\s*(?:zoho\s+)?backstage", raw))),
    ]
    combined, consumed = [], set()
    for section in sections:
        product = str(section.get("product") or "").strip()
        if product in consumed:
            continue
        shared_pair = next((pair for pair in pairs if pair[2] and product in pair[:2]), None)
        if shared_pair:
            names = shared_pair[:2]
            peers = [row for row in sections if row.get("product") in names]
            values = [list(dict.fromkeys(str(item).strip() for item in peer.get("requirements", [])
                                        if str(item).strip())) for peer in peers]
            if len(peers) == 2 and values[0] == values[1]:
                combined.append({"product": " & ".join(names), "requirements": values[0]})
                consumed.update(names)
                continue
        combined.append(section)
    return combined


def _draft_brief(sections, answers, fallback_summary):
    """Dedicated BRD-agent pass: explain groupings without creating new scope."""
    try:
        from groq import Groq, RateLimitError

        system = """You are Wooplix's Business Requirements Document agent. This is a
separate requirements-analysis workflow, not the proposal writer. Use only the
reviewed requirement checklist and user answers supplied below. Do not use external
product knowledge. Do not add features, software products, integrations, roles,
workflows, rules, metrics or commitments. Preserve uncertainty as uncertainty.
The answers input includes the complete original document and the question text
with each reviewed answer. Read those details, including tables and scope boundaries.
Produce a detailed BRD specification, not merely a summary checklist. Cover only
relevant sections: application responsibilities, stakeholders, business process,
fields/master data, workflows/business rules, roles/access, reports/dashboards,
integrations, notifications, scope boundaries and acceptance criteria.
Keep future/optional/excluded items explicitly qualified. Never turn a suggestion
or feasibility condition into an agreed commitment. Do not copy sample client scope.
In specification table cells use EXACT excerpts from the source or reviewed answers,
existing BR IDs, or 'To be confirmed'. Never invent field types, mandatory flags,
thresholds, report formulas, permissions, owners, integrations or sequences.
Provide an evidence array with one exact source excerpt per table row. All factual
cells in that row must occur within that excerpt. Keep the original relationships
between fields and values; do not join unrelated facts from different modules.
Do not reproduce tables already supplied in the source; they will be preserved.
Return JSON with one concise factual overview, specification_tables and process_views. Each process_view
must use an exact existing business-area heading and include only a short explanation
of activities explicitly listed for that area. Add requirement_ids from the exact
IDs supplied. Cross-application relationships can be described only when an explicit
requirement states them. If the source does not define a sequence, do not make one.
Schema: {"overview":"", "process_views":[{"area":"","description":"",
"requirement_ids":["BR-001"]}], "specification_tables":[{"section":"Fields and Master Data",
"title":"Lead fields", "columns":["Field","Required","Mapping"],
"rows":[["EXACT SOURCE EXCERPT","To be confirmed","EXACT SOURCE EXCERPT"]],
"evidence":["Exact contiguous source passage supporting this row"]}]}.
Valid section names: """ + ", ".join(SPECIFICATION_SECTIONS)
        user = json.dumps({"requirements": [
            {"id": item["id"], "area": item["area"], "statement": item["requirement"]}
            for item in _section_items(sections)],
            "answers": answers}, ensure_ascii=False)
        models = list(dict.fromkeys([brand.GROQ_MODEL, "openai/gpt-oss-120b",
                                     "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]))
        keys = brand.GROQ_API_KEYS or ([brand.GROQ_API_KEY] if brand.GROQ_API_KEY else [])
        for model in models:
            for key in keys:
                try:
                    kwargs = {"model": model,
                              "messages": [{"role": "system", "content": system},
                                           {"role": "user", "content": user}],
                              "temperature": 0.1, "max_completion_tokens": 6500,
                              "response_format": {"type": "json_object"}}
                    if "gpt-oss" in model:
                        kwargs["reasoning_effort"] = "low"
                    response = Groq(api_key=key, timeout=45, max_retries=0).chat.completions.create(**kwargs)
                    text = response.choices[0].message.content or "{}"
                    data = json.loads(text)
                    by_area = {}
                    for item in _section_items(sections):
                        by_area.setdefault(item["area"], set()).add(item["id"])
                    views = []
                    for view in data.get("process_views", []):
                        area = str(view.get("area", "")).strip()
                        ids = view.get("requirement_ids", [])
                        if (area in by_area and isinstance(ids, list) and ids
                                and set(ids) <= by_area[area]):
                            views.append({"area": area,
                                          "description": str(view.get("description", ""))[:700],
                                          "requirement_ids": ids})
                    source = answers.get("source_requirement", "") if isinstance(answers, dict) else ""
                    source += "\n" + json.dumps(answers.get("decisions", []), ensure_ascii=False)
                    return {"overview": str(data.get("overview") or fallback_summary)[:1600],
                            "process_views": views,
                            "specification_tables": validated_model_tables(
                                data.get("specification_tables", []), source, _section_items(sections))}
                except RateLimitError:
                    break
                except Exception:
                    continue
    except Exception:
        pass
    return {"overview": fallback_summary, "process_views": []}


def build_document(requirement, analysis, answers, source_name=""):
    """Make a source-grounded BRD with useful detail and no empty boilerplate."""
    questions = analysis.get("questions") or []
    if set(answers) != {q["id"] for q in questions}:
        raise ValueError("Answer each question or choose Leave open.")
    for answer in answers.values():
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 4000:
            raise ValueError("Enter each answer (up to 4000 characters).")

    sections = _combine_explicit_shared_areas(
        requirement, analysis.get("requirement_sections") or [])
    boundaries = []
    scoped_sections = []
    for section in sections:
        included = []
        for item in section.get("requirements", []):
            # Keep explicit future/excluded scope out of the committed checklist.
            if re.search(r"\b(?:not required (?:in|for|during) (?:the |this |current )?phase|out of scope|excluded from (?:this |the |current )?scope|future phase|future scope|future enhancement|optional feature)\b",
                         str(item), re.I):
                boundaries.append([str(section.get("product", "Business")), str(item)])
            else:
                included.append(item)
        if included:
            scoped_sections.append(dict(section, requirements=included))
    sections = scoped_sections
    requirements = _section_items(sections)
    decisions = [{"question": q["question"], "answer": answers[q["id"]]}
                 for q in questions]
    brief = _draft_brief(sections, {"source_requirement": requirement,
                                  "decisions": decisions},
                         str(analysis.get("summary") or "").strip())
    specifications = source_tables(requirement)
    # Source tables are authoritative. Model tables supplement uncovered
    # categories, rather than duplicating or replacing supplied definitions.
    source_categories = {table["section"] for table in specifications}
    specifications += statement_tables(requirements, source_categories)
    source_categories.update(table["section"] for table in specifications)
    specifications += [table for table in brief.get("specification_tables", [])
                       if table["section"] not in source_categories]
    if boundaries:
        specifications.append({"section": "Scope Boundaries", "title": "Qualified scope",
                               "columns": ["Business area", "Source condition"],
                               "rows": boundaries, "origin": "source"})
    activities = []
    for section in sections:
        product = str(section.get("product") or "").strip()
        items = list(dict.fromkeys(str(x).strip() for x in section.get("requirements", []) if str(x).strip()))
        if items:
            activities.append({"area": product, "items": items})

    def matches(*terms):
        return [{"area": row["area"], "items": [item for item in row["items"]
                if any(term in item.casefold() for term in terms)]}
                for row in activities if any(any(term in item.casefold() for term in terms)
                                               for item in row["items"])]

    # Preserve stated objectives; only use this heading if it exists in the source.
    objectives = _source_list(requirement, "Main Objective", ["Cost Proposal", "Zoho CRM", "Training"])
    objectives += _source_list(requirement, "Business Objectives", ["Current Business", "Scope of Work"])
    purpose = _source_list(requirement, "Purpose", ["Scope", "Target Audience"])
    scope_items = _source_list(requirement, "Scope", ["Target Audience", "Current State Summary", "Business Context"])
    current_state = _source_list(requirement, "Current State Summary", ["Business Objectives", "Scope"])
    stakeholders = _source_list(requirement, "Target Audience", ["Current State Summary", "Business Objectives"])
    acceptance_criteria = (_source_list(requirement, "Go-Live Acceptance Criteria", ["Document Sign-Off"])
                           or _source_list(requirement, "Acceptance Criteria", ["Document Sign-Off"])
                           or _acceptance_table_rows(requirement))
    # Use the reviewed, source-filtered checklist as the authority for every product.
    areas = [{"name": row["area"], "requirements": row["items"]} for row in activities]
    open_decisions = []
    for question in questions:
        answer = answers[question["id"]].strip()
        if answer.casefold() in {"leave open for discovery", "confirm during discovery", ""}:
            open_decisions.append({"question": question["question"], "answer": "To be confirmed"})
        else:
            open_decisions.append({"question": question["question"], "answer": answer})

    requirements_by_area = [{"name": name} for name in dict.fromkeys(
        row["area"] for row in requirements)]

    return {
        "title": "Business Requirements Document",
        "project_name": (Path(source_name).stem.replace("_", " ").strip()
                         if source_name and Path(source_name).stem.casefold() not in {"pasted requirement", "requirement"}
                         else "Project name to be confirmed"),
        "prepared_for": "",
        "prepared_by": brand.COMPANY_NAME,
        "date": brand._ordinal_day(datetime.now(ZoneInfo("Asia/Kolkata"))),
        "status": "Draft for business review",
        "summary": brief["overview"],
        "purpose": purpose,
        "scope_items": scope_items,
        "process_views": brief["process_views"],
        "specification_tables": specifications,
        "objectives": objectives,
        "current_state": current_state,
        "stakeholders": stakeholders,
        "acceptance_criteria": acceptance_criteria,
        "areas": areas,
        "requirements": requirements,
        # These are tagged copies of source requirements, never newly authored
        # requirements. Rendering skips a category if it has no matching item.
        "business_rules": matches("rule", "duplicate", "same company", "approval", "mandatory", "validation", "billable / non-billable", "planned vs actual"),
        "data_requirements": matches("data", "record", "field", "duplicate", "event-wise", "batch-wise", "timesheet", "work notes", "master", "migration", "cleansing"),
        "integration_requirements": matches("integration", "integrat"),
        "reporting_requirements": matches("report", "dashboard", "analytics", "metric", "kpi"),
        "access_requirements": matches("role", "permission", "access", "security"),
        "requirements_by_area": requirements_by_area,
        "training_requirements": next((x["items"] for x in activities if x["area"].casefold() == "training"), []),
        "commercial_categories": analysis.get("commercial_categories") or [],
        "open_decisions": open_decisions,
        "source_name": Path(source_name).name if source_name else "",
        "activity_map": activities,
    }


def _e(value):
    return html.escape(_plain(value), quote=True)


def _plain(value):
    return re.sub(r"\bBR-\d+\b", "", str(value or "")).strip()


def _specs_for(doc, categories):
    return [table for table in doc.get("specification_tables", [])
            if table["section"] in categories]


def _nonempty_areas(doc):
    return [area for area in doc["requirements_by_area"]
            if any(row["area"] == area["name"] for row in doc["requirements"])]


def build_html(doc):
    """Render a concise BRD; omit sections unsupported by the source request."""
    main = brand._logo_data_uri()
    badge = brand._badge_data_uri()
    main_img = f'<img src="{main}" style="position:absolute;left:0;top:0;height:38px" alt="Wooplix">' if main else ""
    badge_img = f'<img src="{badge}" style="position:absolute;right:0;top:0;height:26px" alt="Partner">' if badge else ""
    out = [f"""<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size:A4; margin:27mm 15mm 22mm; }}
body {{ font-family:'DejaVu Sans',sans-serif; font-size:9pt; line-height:1.45; color:#1e293b; }}
.header {{ position:fixed; top:-20mm; width:100%; z-index:1000; }} .header td:last-child {{text-align:right}}
.approval {{page-break-inside:avoid}}
.footer {{position:fixed;bottom:-8mm;width:100%;border-top:1px solid #cbd5e1;font-size:7pt;color:#64748b}}
.footer td {{padding-top:3mm}} h1 {{font-size:19pt;color:#002b49;margin:12mm 0 8mm}}
h2 {{page-break-after:avoid;background:#1a365d;color:#fff;font-size:11pt;padding:7px 10px;margin:18px 0 8px}}
h3 {{page-break-after:avoid;color:#008080;font-size:10pt;border-bottom:1px solid #008080;padding-bottom:3px;margin:13px 0 5px}}
p {{margin:4px 0 8px}} table {{width:100%;border-collapse:collapse;margin:6px 0 12px;font-size:8.5pt}}
thead {{display:table-header-group}} tr {{page-break-inside:avoid}} th {{background:#1a365d;color:white;text-align:left;font-size:8pt;padding:6px;border:1px solid #1a365d}}
td {{padding:5px 7px;vertical-align:top;border:1px solid #cbd5e1}} tbody tr:nth-child(even) td {{background:#f8fafc}}
.meta td:first-child {{background:#edf3f8;font-weight:bold;width:30%}} .muted {{color:#64748b;font-size:8pt}}
</style></head><body><div class="header">{main_img}{badge_img}</div>
<table class="footer"><tr><td><b>{_e(brand.COMPANY_NAME)}</b> · Confidential</td><td align="center">{_e(brand.COMPANY_EMAIL)}</td><td align="right">{_e(brand.COMPANY_WEBSITE)}</td></tr></table>
<h1>{_e(doc['title'])}</h1>
<table class="meta"><tbody>"""]
    for label, value in (("Project", doc["project_name"]), ("Status", doc["status"]),
                         ("Prepared by", doc["prepared_by"]), ("Date", doc["date"])):
        out.append(f"<tr><td>{_e(label)}</td><td>{_e(value)}</td></tr>")
    out.append("</tbody></table>")

    chapter_number = 0

    def section(title):
        nonlocal chapter_number
        chapter_number += 1
        out.append(f"<h2>{chapter_number}. {_e(title)}</h2>")

    def list_table(rows, first="Requirement"):
        out.append(f"<table><thead><tr><th style='width:8%'>No.</th><th>{_e(first)}</th></tr></thead><tbody>")
        for number, row in enumerate(rows, 1):
            out.append(f"<tr><td>{number}</td><td>{_e(row['requirement'])}</td></tr>")
        out.append("</tbody></table>")

    def specification_tables(category):
        tables = [table for table in doc.get("specification_tables", [])
                  if table["section"] == category]
        for table in tables:
            visible_columns = [i for i, name in enumerate(table["columns"])
                               if str(name).strip().casefold() not in
                               {"id", "brd id", "requirement id", "requirement ids", "traceability id"}]
            if not visible_columns:
                continue
            out.append(f"<h3>{_e(table['title'])}</h3><table><thead><tr>")
            out.extend(f"<th>{_e(table['columns'][i])}</th>" for i in visible_columns)
            out.append("</tr></thead><tbody>")
            for row in table["rows"]:
                out.append("<tr>" + "".join(f"<td>{_e(row[i])}</td>" for i in visible_columns) + "</tr>")
            out.append("</tbody></table>")
        return bool(tables)

    section("Executive Summary")
    out.append(f"<p>{_e(doc['summary'])}</p>")
    if doc.get("purpose"):
        section("Purpose")
        out.append("<ul>" + "".join(f"<li>{_e(item)}</li>" for item in doc["purpose"]) + "</ul>")
    if doc["current_state"] or doc["objectives"] or doc["stakeholders"]:
        section("Business Context & Objectives")
    if doc["current_state"]:
        out.append("<h3>Current State</h3><ul>" + "".join(f"<li>{_e(item)}</li>" for item in doc["current_state"]) + "</ul>")
    if doc["objectives"]:
        out.append("<h3>Business Objectives</h3><ul>" + "".join(f"<li>{_e(item)}</li>" for item in doc["objectives"]) + "</ul>")
    if doc["stakeholders"]:
        out.append("<h3>Stakeholders</h3><ul>" + "".join(f"<li>{_e(item)}</li>" for item in doc["stakeholders"]) + "</ul>")
    section("Project Scope")
    out.append("<ul>")
    scope_values = doc.get("scope_items") or [area["name"] for area in doc["requirements_by_area"]]
    for item in scope_values:
        out.append(f"<li>{_e(item)}</li>")
    out.append("</ul>")
    if _specs_for(doc, {"Scope Boundaries"}):
        out.append("<h3>Scope Conditions</h3>")
        specification_tables("Scope Boundaries")
    has_architecture = bool(_specs_for(doc, {"Application Responsibilities", "Business Process"}))
    if has_architecture:
        section("Solution Architecture")
    for category in ("Application Responsibilities",):
        if _specs_for(doc, {category}):
            out.append("<h3>Application Overview</h3>")
            specification_tables(category)
    if _specs_for(doc, {"Business Process"}):
        out.append("<h3>Business Process Flow</h3>")
        specification_tables("Business Process")
    if _specs_for(doc, {"Fields and Master Data"}):
        section("Master Data Requirements")
        specification_tables("Fields and Master Data")

    section("Functional Requirements by Module")
    rows_by_area = {}
    for row in doc["requirements"]:
        rows_by_area.setdefault(row["area"], []).append(row)
    for area in _nonempty_areas(doc):
        name = area["name"]
        out.append(f"<h3>{_e(name)}</h3>")
        process_view = next((view for view in doc["process_views"] if view["area"] == name), None)
        if process_view:
            out.append(f"<h4>Business Objective</h4><p>{_e(process_view['description'])}</p>")
        rows = rows_by_area.get(name, [])
        if rows:
            out.append("<table><thead><tr><th style='width:8%'>No.</th><th>Business Requirement</th></tr></thead><tbody>")
            for number, row in enumerate(rows, 1):
                out.append(f"<tr><td>{number}</td><td>{_e(row['requirement'])}</td></tr>")
            out.append("</tbody></table>")
    for title, categories in (
        ("Approval Workflow & Authorization Matrix", ("Workflows and Business Rules", "Roles and Access")),
        ("Dashboard Specifications", ("Reports and Dashboards",)),
        ("Integrations", ("Integrations",)),
        ("Alerting & Notification System", ("Notifications",)),
        ("Additional Business Details", ("Business Details",)),
    ):
        tables = _specs_for(doc, categories)
        if tables:
            section(title)
            for category in categories:
                specification_tables(category)
    if _specs_for(doc, {"Stakeholders"}):
        section("Stakeholder Roles")
        specification_tables("Stakeholders")
    if doc["training_requirements"]:
        section("Training Requirements")
        list_table([row for row in doc["requirements"] if row["area"].casefold() == "training"], "Training requirement")
    if doc["open_decisions"]:
        section("Questions and Decisions")
        out.append("<table><thead><tr><th>Question</th><th>Answer or status</th></tr></thead><tbody>")
        for row in doc["open_decisions"]:
            out.append(f"<tr><td>{_e(row['question'])}</td><td>{_e(row['answer'])}</td></tr>")
        out.append("</tbody></table>")
    if any(t["section"] == "Acceptance Criteria" for t in doc.get("specification_tables", [])):
        section("Acceptance Criteria")
        specification_tables("Acceptance Criteria")
    elif doc["acceptance_criteria"]:
        section("Acceptance Criteria")
        out.append("<ul>" + "".join(f"<li>{_e(item)}</li>" for item in doc["acceptance_criteria"]) + "</ul>")
    out.append("</body></html>")
    return "".join(out)


def build_docx(doc, output):
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    report = Document()
    section = report.sections[0]
    section.header_distance = Inches(0.35)
    section.footer_distance = Inches(0.35)
    section.left_margin = section.right_margin = Inches(0.7)
    section.top_margin = section.bottom_margin = Inches(0.75)
    normal = report.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)
    normal.font.color.rgb = RGBColor(0x1e, 0x29, 0x3b)
    header = section.header
    logo_table = header.add_table(rows=1, cols=2, width=Inches(7.1))
    if Path(brand.LOGO_MAIN_PATH).exists():
        logo_table.cell(0, 0).paragraphs[0].add_run().add_picture(brand.LOGO_MAIN_PATH, width=Inches(2.0))
    if Path(brand.LOGO_BADGE_PATH).exists():
        p_badge = logo_table.cell(0, 1).paragraphs[0]
        p_badge.alignment = 2
        p_badge.add_run().add_picture(brand.LOGO_BADGE_PATH, width=Inches(1.65))
    footer = section.footer.paragraphs[0]
    footer.alignment = 1
    footer.add_run(f"{brand.COMPANY_NAME}  ·  Confidential  ·  {brand.COMPANY_WEBSITE}").font.size = Pt(8)
    p = report.add_paragraph()
    p.alignment = 1
    r = p.add_run(_plain(doc["title"]))
    r.bold = True
    r.font.size = Pt(20)
    r.font.color.rgb = RGBColor(0, 43, 73)
    table = report.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in (("Project", doc["project_name"]), ("Status", doc["status"]),
                         ("Prepared by", doc["prepared_by"]), ("Date", doc["date"])):
        cells = table.add_row().cells
        cells[0].text, cells[1].text = label, _plain(value)
        cells[0]._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="edf3f8"/>'))

    chapter_number = 0

    def heading(value):
        nonlocal chapter_number
        chapter_number += 1
        h = report.add_heading(f"{chapter_number}. {value}", level=1)
        for run in h.runs:
            run.font.color.rgb = RGBColor(0x1a, 0x36, 0x5d)
        return h

    def bullets(values):
        for value in values:
            report.add_paragraph(str(value), style="List Bullet")

    def specification_tables(category):
        tables = [table for table in doc.get("specification_tables", [])
                  if table["section"] == category]
        for spec in tables:
            report.add_heading(_plain(spec["title"]), level=2)
            visible_columns = [i for i, name in enumerate(spec["columns"])
                               if str(name).strip().casefold() not in
                               {"id", "brd id", "requirement id", "requirement ids", "traceability id"}]
            if not visible_columns:
                continue
            table = report.add_table(rows=1, cols=len(visible_columns))
            table.style = "Table Grid"
            for cell, index in zip(table.rows[0].cells, visible_columns):
                cell.text = _plain(spec["columns"][index])
            for values in spec["rows"]:
                for cell, index in zip(table.add_row().cells, visible_columns):
                    cell.text = _plain(values[index])
        return bool(tables)

    heading("Executive Summary")
    report.add_paragraph(_plain(doc["summary"]))
    if doc.get("purpose"):
        heading("Purpose")
        bullets([_plain(item) for item in doc["purpose"]])
    if doc["current_state"] or doc["objectives"] or doc["stakeholders"]:
        heading("Business Context & Objectives")
    if doc["current_state"]:
        report.add_heading("Current State", level=2)
        bullets([_plain(item) for item in doc["current_state"]])
    if doc["objectives"]:
        report.add_heading("Business Objectives", level=2)
        bullets([_plain(item) for item in doc["objectives"]])
    if doc["stakeholders"]:
        report.add_heading("Stakeholders", level=2)
        bullets([_plain(item) for item in doc["stakeholders"]])
    heading("Project Scope")
    scope_values = doc.get("scope_items") or [area["name"] for area in doc["requirements_by_area"]]
    bullets([_plain(item) for item in scope_values])
    if _specs_for(doc, {"Scope Boundaries"}):
        report.add_heading("Scope Conditions", level=2)
        specification_tables("Scope Boundaries")
    has_architecture = bool(_specs_for(doc, {"Application Responsibilities", "Business Process"}))
    if has_architecture:
        heading("Solution Architecture")
    if _specs_for(doc, {"Application Responsibilities"}):
        report.add_heading("Application Overview", level=2)
        specification_tables("Application Responsibilities")
    if _specs_for(doc, {"Business Process"}):
        report.add_heading("Business Process Flow", level=2)
        specification_tables("Business Process")
    if _specs_for(doc, {"Fields and Master Data"}):
        heading("Master Data Requirements")
        specification_tables("Fields and Master Data")

    heading("Functional Requirements by Module")
    rows_by_area = {}
    for row in doc["requirements"]:
        rows_by_area.setdefault(row["area"], []).append(row)
    for area in _nonempty_areas(doc):
        name = area["name"]
        report.add_heading(_plain(name), level=2)
        process_view = next((view for view in doc["process_views"] if view["area"] == name), None)
        if process_view:
            report.add_heading("Business Objective", level=3)
            report.add_paragraph(_plain(process_view["description"]))
        rows = rows_by_area.get(name, [])
        if rows:
            table = report.add_table(rows=1, cols=2)
            table.style = "Table Grid"
            table.rows[0].cells[0].text = "No."
            table.rows[0].cells[1].text = "Business Requirement"
            for number, row in enumerate(rows, 1):
                cells = table.add_row().cells
                cells[0].text, cells[1].text = str(number), _plain(row["requirement"])

    for title, categories in (
        ("Approval Workflow & Authorization Matrix", ("Workflows and Business Rules", "Roles and Access")),
        ("Dashboard Specifications", ("Reports and Dashboards",)),
        ("Integrations", ("Integrations",)),
        ("Alerting & Notification System", ("Notifications",)),
        ("Additional Business Details", ("Business Details",)),
    ):
        if _specs_for(doc, categories):
            heading(title)
            for category in categories:
                specification_tables(category)
    if _specs_for(doc, {"Stakeholders"}):
        heading("Stakeholder Roles")
        specification_tables("Stakeholders")

    if doc["training_requirements"]:
        heading("Training Requirements")
        rows = [row for row in doc["requirements"] if row["area"].casefold() == "training"]
        table = report.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "No.", "Training Requirement"
        for number, row in enumerate(rows, 1):
            cells = table.add_row().cells
            cells[0].text, cells[1].text = str(number), _plain(row["requirement"])
    if doc["open_decisions"]:
        heading("Questions and Decisions")
        table = report.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Question", "Answer or status"
        for row in doc["open_decisions"]:
            cells = table.add_row().cells
            cells[0].text, cells[1].text = _plain(row["question"]), _plain(row["answer"])
    if any(t["section"] == "Acceptance Criteria" for t in doc.get("specification_tables", [])):
        heading("Acceptance Criteria")
        specification_tables("Acceptance Criteria")
    elif doc["acceptance_criteria"]:
        heading("Acceptance Criteria")
        bullets([_plain(item) for item in doc["acceptance_criteria"]])
    # Match the Wooplix table palette and repeat column labels across pages.
    for index, table in enumerate(report.tables):
        if index:
            first = table.rows[0]
            first._tr.get_or_add_trPr().append(parse_xml(f'<w:tblHeader {nsdecls("w")}/>'))
            for cell in first.cells:
                cell._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="1a365d"/>'))
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.bold = True
                        run.font.color.rgb = RGBColor(255, 255, 255)
        for row in table.rows:
            row._tr.get_or_add_trPr().append(parse_xml(f'<w:cantSplit {nsdecls("w")}/>'))
    report.save(output)
    return output


def build_pdf(doc, output):
    php = shutil.which("php")
    script = Path(brand.PHP_SCRIPT)
    if not php or not (Path(brand.HERE) / "vendor" / "autoload.php").exists():
        raise RuntimeError("The PDF renderer is unavailable.")
    fd, html_path = tempfile.mkstemp(suffix=".html", prefix="wooplix_brd_", dir=str(Path(output).parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(build_html(doc))
        result = subprocess.run([php, str(script), html_path, str(output)],
                                capture_output=True, text=True, timeout=240)
        if result.returncode or not Path(output).exists():
            raise RuntimeError("Could not render the Business Requirements Document PDF.")
    finally:
        try:
            os.unlink(html_path)
        except OSError:
            pass
    return output
