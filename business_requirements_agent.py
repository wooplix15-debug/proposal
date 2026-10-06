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
                  if re.match(rf"^\s*(?:\d+[.)]\s*)?{section}\b", line, re.I)), None)
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

    # Group requirements once, with stable IDs, so readers can move from module
    # scope to a specific requirement without repeating the same lists in every
    # section of the BRD.
    requirements_by_area = []
    by_area_ids = {}
    for row in requirements:
        by_area_ids.setdefault(row["area"], []).append(row["id"])
    for name, ids in by_area_ids.items():
        requirements_by_area.append({"name": name, "requirement_ids": ids})

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
    return html.escape(str(value or ""), quote=True)


def _functional_rows(doc):
    detailed_ids = {cell for table in doc.get("specification_tables", [])
                    for row in table["rows"] for cell in row
                    if re.fullmatch(r"BR-\d+", str(cell))}
    return [row for row in doc["requirements"]
            if row["area"].casefold() != "training" and row["id"] not in detailed_ids]


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

    def section(title):
        out.append(f"<h2>{_e(title)}</h2>")

    def list_table(rows, first="Requirement"):
        out.append(f"<table><thead><tr><th style='width:12%'>ID</th><th style='width:24%'>Business area</th><th>{_e(first)}</th></tr></thead><tbody>")
        for row in rows:
            out.append(f"<tr><td>{_e(row['id'])}</td><td>{_e(row['area'])}</td><td>{_e(row['requirement'])}</td></tr>")
        out.append("</tbody></table>")

    def reference_table(rows):
        out.append("<table><thead><tr><th>Business area</th><th>Requirement IDs</th></tr></thead><tbody>")
        for group in rows:
            ids = [row["id"] for row in doc["requirements"]
                   if row["area"] == group["area"] and row["requirement"] in group["items"]]
            if ids:
                out.append(f"<tr><td>{_e(group['area'])}</td><td>{_e(', '.join(ids))}</td></tr>")
        out.append("</tbody></table>")

    def specification_tables(category):
        tables = [table for table in doc.get("specification_tables", [])
                  if table["section"] == category]
        for table in tables:
            out.append(f"<h3>{_e(table['title'])}</h3><table><thead><tr>")
            out.extend(f"<th>{_e(column)}</th>" for column in table["columns"])
            out.append("</tr></thead><tbody>")
            for row in table["rows"]:
                out.append("<tr>" + "".join(f"<td>{_e(cell)}</td>" for cell in row) + "</tr>")
            out.append("</tbody></table>")
        return bool(tables)

    section("Project Overview")
    out.append(f"<p>{_e(doc['summary'])}</p>")
    if doc["objectives"]:
        section("Business Objectives")
        out.append("<ul>" + "".join(f"<li>{_e(item)}</li>" for item in doc["objectives"]) + "</ul>")
    if doc["current_state"]:
        section("Current Business Context")
        out.append("<ul>" + "".join(f"<li>{_e(item)}</li>" for item in doc["current_state"]) + "</ul>")
    if doc["stakeholders"]:
        section("Stakeholders")
        out.append("<ul>" + "".join(f"<li>{_e(item)}</li>" for item in doc["stakeholders"]) + "</ul>")
    section("Scope")
    out.append("<p>The following business areas and requirements are included because they appear in the submitted requirement.</p>")
    out.append("<table><thead><tr><th>Business area</th><th>Included requirements</th></tr></thead><tbody>")
    for area in doc["requirements_by_area"]:
        out.append(f"<tr><td>{_e(area['name'])}</td><td>{_e(', '.join(area['requirement_ids']))}</td></tr>")
    out.append("</tbody></table>")
    for category in ("Application Responsibilities", "Business Process", "Scope Boundaries"):
        if any(table["section"] == category for table in doc.get("specification_tables", [])):
            section(category)
            specification_tables(category)
    if doc["process_views"]:
        section("Business Process View")
        for view in doc["process_views"]:
            out.append(f"<h3>{_e(view['area'])}</h3><p>{_e(view['description'])}</p>")
            out.append(f"<p class='muted'>Requirements: {_e(', '.join(view['requirement_ids']))}</p>")
    section("Functional Requirements")
    out.append("<p>Requirement statements are based on the submitted request. IDs can be used during review and implementation.</p>")
    list_table(_functional_rows(doc))
    for title, key, category in (("Business Rules", "business_rules", "Workflows and Business Rules"),
                                ("Fields and Master Data", "data_requirements", "Fields and Master Data"),
                                ("Integrations", "integration_requirements", "Integrations"),
                                ("Reports and Analytics", "reporting_requirements", "Reports and Dashboards"),
                                ("Roles, Access and Security", "access_requirements", "Roles and Access")):
        if doc[key] or any(t["section"] == category for t in doc.get("specification_tables", [])):
            section(title)
            if not specification_tables(category):
                reference_table(doc[key])
    for category in ("Stakeholders", "Notifications", "Business Details"):
        if any(table["section"] == category for table in doc.get("specification_tables", [])):
            section(category)
            specification_tables(category)
    if doc["training_requirements"]:
        section("Training")
        list_table([row for row in doc["requirements"] if row["area"].casefold() == "training"], "Training requirement")
    if doc["commercial_categories"]:
        section("Commercial Items Requested")
        out.append("<p>Pricing was requested for the following items. No amounts have been added.</p><ul>")
        out.extend(f"<li>{_e(x)}</li>" for x in doc["commercial_categories"])
        out.append("</ul>")
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
    else:
        section("Acceptance Review Plan")
        out.append("<p>Proposed review: demonstrate the requirements listed in Scope and the detailed specifications for each business area. Detailed pass criteria and reviewers are to be confirmed before acceptance. Status: pending client review.</p>")
    out.append('<div class="approval">')
    section("Review and Approval")
    out.append("<p>Approval confirms that the requirements listed above reflect the agreed business need.</p>")
    out.append("<table><thead><tr><th>Reviewer</th><th>Organization / role</th><th>Signature</th><th>Date</th></tr></thead><tbody><tr><td></td><td>Client</td><td></td><td></td></tr><tr><td></td><td>Implementation partner</td><td></td><td></td></tr></tbody></table>")
    out.append("</div></body></html>")
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
    r = p.add_run(doc["title"])
    r.bold = True
    r.font.size = Pt(20)
    r.font.color.rgb = RGBColor(0, 43, 73)
    table = report.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for label, value in (("Project", doc["project_name"]), ("Status", doc["status"]),
                         ("Prepared by", doc["prepared_by"]), ("Date", doc["date"])):
        cells = table.add_row().cells
        cells[0].text, cells[1].text = label, str(value)
        cells[0]._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:fill="edf3f8"/>'))

    def heading(value):
        h = report.add_heading(value, level=1)
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
            report.add_heading(spec["title"], level=2)
            table = report.add_table(rows=1, cols=len(spec["columns"]))
            table.style = "Table Grid"
            for cell, label in zip(table.rows[0].cells, spec["columns"]):
                cell.text = label
            for values in spec["rows"]:
                for cell, value in zip(table.add_row().cells, values):
                    cell.text = value
        return bool(tables)

    heading("Project Overview")
    report.add_paragraph(doc["summary"])
    if doc["objectives"]:
        heading("Business Objectives")
        bullets(doc["objectives"])
    if doc["current_state"]:
        heading("Current Business Context")
        bullets(doc["current_state"])
    if doc["stakeholders"]:
        heading("Stakeholders")
        bullets(doc["stakeholders"])
    heading("Scope")
    report.add_paragraph("The following business areas and requirements are included because they appear in the submitted requirement.")
    scope_table = report.add_table(rows=1, cols=2)
    scope_table.style = "Table Grid"
    scope_table.rows[0].cells[0].text, scope_table.rows[0].cells[1].text = "Business area", "Included requirement IDs"
    for area in doc["requirements_by_area"]:
        cells = scope_table.add_row().cells
        cells[0].text, cells[1].text = area["name"], ", ".join(area["requirement_ids"])
    for category in ("Application Responsibilities", "Business Process", "Scope Boundaries"):
        if any(table["section"] == category for table in doc.get("specification_tables", [])):
            heading(category)
            specification_tables(category)
    if doc["process_views"]:
        heading("Business Process View")
        for view in doc["process_views"]:
            report.add_heading(view["area"], level=2)
            report.add_paragraph(view["description"])
            report.add_paragraph("Requirements: " + ", ".join(view["requirement_ids"]))
    heading("Functional Requirements")
    report.add_paragraph("Requirement statements below preserve the reviewed request. Each BRD ID can be used to discuss scope and record approval.")
    def requirement_table(rows, third_label="Requirement"):
        table = report.add_table(rows=1, cols=3)
        table.style = "Table Grid"
        for cell, label in zip(table.rows[0].cells, ("ID", "Business area", third_label)):
            cell.text = label
        for row in rows:
            cells = table.add_row().cells
            cells[0].text, cells[1].text, cells[2].text = row["id"], row["area"], row["requirement"]

    def reference_table(rows):
        table = report.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Business area", "Requirement IDs"
        for group in rows:
            ids = [row["id"] for row in doc["requirements"]
                   if row["area"] == group["area"] and row["requirement"] in group["items"]]
            if ids:
                cells = table.add_row().cells
                cells[0].text, cells[1].text = group["area"], ", ".join(ids)

    requirement_table(_functional_rows(doc))
    for title, key, category in (("Business Rules", "business_rules", "Workflows and Business Rules"),
                                ("Fields and Master Data", "data_requirements", "Fields and Master Data"),
                                ("Integrations", "integration_requirements", "Integrations"),
                                ("Reports and Analytics", "reporting_requirements", "Reports and Dashboards"),
                                ("Roles, Access and Security", "access_requirements", "Roles and Access")):
        if doc[key] or any(t["section"] == category for t in doc.get("specification_tables", [])):
            heading(title)
            if not specification_tables(category):
                reference_table(doc[key])
    for category in ("Stakeholders", "Notifications", "Business Details"):
        if any(table["section"] == category for table in doc.get("specification_tables", [])):
            heading(category)
            specification_tables(category)
    if doc["training_requirements"]:
        heading("Training")
        requirement_table([row for row in doc["requirements"] if row["area"].casefold() == "training"], "Training requirement")
    if doc["commercial_categories"]:
        heading("Commercial Items Requested")
        report.add_paragraph("Pricing was requested for the following items. No amounts have been added.")
        bullets(doc["commercial_categories"])
    if doc["open_decisions"]:
        heading("Questions and Decisions")
        table = report.add_table(rows=1, cols=2)
        table.style = "Table Grid"
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Question", "Answer or status"
        for row in doc["open_decisions"]:
            cells = table.add_row().cells
            cells[0].text, cells[1].text = row["question"], row["answer"]
    if any(t["section"] == "Acceptance Criteria" for t in doc.get("specification_tables", [])):
        heading("Acceptance Criteria")
        specification_tables("Acceptance Criteria")
    elif doc["acceptance_criteria"]:
        heading("Acceptance Criteria")
        bullets(doc["acceptance_criteria"])
    else:
        heading("Acceptance Review Plan")
        report.add_paragraph("Proposed review: demonstrate the requirements listed in Scope and the detailed specifications for each business area. Detailed pass criteria and reviewers are to be confirmed before acceptance. Status: pending client review.")
    heading("Review and Approval")
    report.add_paragraph("Approval confirms that the requirements listed above reflect the agreed business need.")
    signoff = report.add_table(rows=1, cols=4)
    signoff.style = "Table Grid"
    for cell, label in zip(signoff.rows[0].cells, ("Reviewer", "Organization / role", "Signature", "Date")):
        cell.text = label
    for role in ("Client", "Implementation partner"):
        row = signoff.add_row().cells
        row[1].text = role
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
