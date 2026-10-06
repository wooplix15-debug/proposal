"""BRD specifications: preserve source detail without importing sample scope."""
import re


SPECIFICATION_SECTIONS = (
    "Application Responsibilities", "Stakeholders", "Business Process",
    "Fields and Master Data", "Workflows and Business Rules", "Roles and Access",
    "Reports and Dashboards", "Integrations", "Notifications",
    "Scope Boundaries", "Acceptance Criteria", "Business Details",
)


def normalize(value):
    return re.sub(r"\s+", " ", str(value)).strip()


def table_category(title, columns):
    text = (title + " " + " ".join(columns)).casefold()
    headers = {column.casefold() for column in columns}
    # Header signals take priority over a broad module heading.
    if "signature" in " ".join(columns).casefold() or "document title" in headers:
        return None  # The output has its own approval block.
    if "acceptance criterion" in text or "acceptance criteria" in text:
        return "Acceptance Criteria"
    if "stakeholder" in text:
        return "Stakeholders"
    if "field" in headers or any(x in text for x in ("field name", "data type", "mandatory", "crm mapping", "id format")):
        return "Fields and Master Data"
    if any(x in text for x in ("level 1", "level1", "authority", "approval", "discount range")):
        return "Workflows and Business Rules"
    if any(x in text for x in ("executive", "view-only", "sales manager", "owner/admin", "security requirement")):
        return "Roles and Access"
    if "source" in text and "target" in text:
        return "Integrations"
    if "report" in headers or any(x in text for x in ("kpi", "widget", "metric", "dashboard")):
        return "Reports and Dashboards"
    if "primary function" in text or "application" in " ".join(columns).casefold():
        return "Application Responsibilities"
    if any(x in text for x in ("notification", "alert", "email template", "recipients")):
        return "Notifications"
    if any(x in text for x in ("workflow", "trigger", "automation")):
        return "Workflows and Business Rules"
    if any(x in text for x in ("process", "step", "stage", "activity type")):
        return "Business Process"
    return "Business Details"


def source_tables(requirement):
    """Read ordered pipe tables emitted by the shared DOCX extractor.

    Preserve source headers, rows and qualifiers verbatim. A heading stays
    attached to its table, so similarly named fields in different modules
    are never silently merged.
    """
    tables, lines, context, index = [], requirement.splitlines(), "Business details", 0
    while index < len(lines):
        line = lines[index].strip()
        if " | " not in line:
            if line and (re.match(r"^\d+(?:\.\d+)*[.)]?\s+", line)
                         or (len(line) <= 85 and not line.endswith(".") and ":" not in line)):
                context = re.sub(r"^\s*#+\s*", "", line)
            index += 1
            continue
        columns = [normalize(cell) for cell in line.split(" | ")]
        rows = []
        index += 1
        while index < len(lines) and " | " in lines[index]:
            cells = [normalize(cell) for cell in lines[index].split(" | ")]
            if len(cells) != len(columns):
                break
            if not all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                rows.append(cells)
            index += 1
        category = table_category(context, columns)
        if category and rows and 2 <= len(columns) <= 8:
            tables.append({"section": category, "title": context,
                           "columns": columns, "rows": rows, "origin": "source"})
    return tables


def statement_tables(requirements, covered_categories):
    """Retain explicit name/detail statements even when an AI call is unavailable."""
    grouped = {}
    for row in requirements:
        statement = row["requirement"]
        if ":" not in statement:
            continue
        name, detail = [part.strip() for part in statement.split(":", 1)]
        if not name or not detail or len(name) > 100:
            continue
        area = row["area"].casefold()
        if any(term in area for term in ("workflow", "automation", "rule")):
            category = "Workflows and Business Rules"
        elif any(term in area for term in ("email", "notification", "communication", "alert")):
            category = "Notifications"
        elif any(term in area for term in ("report", "dashboard", "analytics")):
            category = "Reports and Dashboards"
        elif any(term in area for term in ("role", "permission", "security")):
            category = "Roles and Access"
        else:
            continue
        if category not in covered_categories:
            grouped.setdefault((category, row["area"]), []).append([row["id"], name, detail])
    return [{"section": category, "title": area,
             "columns": ["ID", "Requirement", "Business detail"],
             "rows": rows, "origin": "source"}
            for (category, area), rows in grouped.items()]


def validated_model_tables(tables, source, requirements):
    """Allow reorganized source text, IDs and explicitly unresolved cells only.

    The model cannot invent thresholds, field types, roles, report formulas
    or approval routes. Its cells must occur in the submitted source/answers.
    """
    normalized = normalize(source).casefold()
    ids = {row["id"] for row in requirements}
    result = []
    if not isinstance(tables, list):
        return result
    for table in tables[:24]:
        if not isinstance(table, dict) or table.get("section") not in SPECIFICATION_SECTIONS:
            continue
        columns, rows = table.get("columns"), table.get("rows")
        evidence = table.get("evidence", [])
        if not isinstance(columns, list) or not 2 <= len(columns) <= 6 or not isinstance(rows, list):
            continue
        accepted = []
        for index, row in enumerate(rows[:100]):
            if not isinstance(row, list) or len(row) != len(columns):
                continue
            if not isinstance(evidence, list) or index >= len(evidence):
                continue
            excerpt = normalize(evidence[index]).casefold()
            if not excerpt or len(excerpt) > 4000 or excerpt not in normalized:
                continue
            cells = [normalize(cell) for cell in row]
            if all(not cell or cell == "To be confirmed" or cell in ids
                   or cell.casefold() in excerpt for cell in cells):
                if any(cell and cell != "To be confirmed" and cell not in ids for cell in cells):
                    accepted.append(cells)
        if accepted:
            result.append({"section": table["section"],
                           "title": normalize(table.get("title", table["section"]))[:160],
                           "columns": [normalize(x)[:80] for x in columns],
                           "rows": accepted, "origin": "organized"})
    return result
