"""Read actual product delivery times and completed-project records from Google Sheets."""
import csv
import io
import json
import os
from pathlib import Path
import re
import requests

SHEET_ID = os.environ.get('ZOHO_PROJECT_SHEET_ID', '1GnpSpbL_B1s6wDOO5NQVml75Fiz56l4bPF1s9F7brEw')
PRODUCTS_GID = os.environ.get('ZOHO_PRODUCTS_GID', '1746892041')
PROJECTS_GID = os.environ.get('ZOHO_PROJECTS_GID', '189646703')


def _parse_days(value):
    """Keep sheet durations readable, including ranges and per-unit estimates."""
    value = (value or '').strip()
    if not value:
        return None
    cleaned = value.replace('–', '-').replace('—', '-').replace('−', '-')
    match = re.fullmatch(
        r'\s*(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:working\s*)?days?\s*',
        cleaned, flags=re.IGNORECASE)
    if match:
        low, high = float(match.group(1)), float(match.group(2))
        if low <= 0 or high < low:
            return None
        return {'days': f'{match.group(1)}–{match.group(2)}',
                'days_min': low, 'days_max': high}

    match = re.fullmatch(
        r'\s*(\d+(?:\.\d+)?)\s*(?:working\s*)?days?\s*(?:per|/)\s*(.+?)\s*',
        cleaned, flags=re.IGNORECASE)
    if match:
        value = float(match.group(1))
        if value <= 0:
            return None
        return {'days': f'{match.group(1)} day per {match.group(2).strip()}',
                'days_min': None, 'days_max': None, 'duration_basis': f'per {match.group(2).strip()}'}

    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:working\s*)?days?\s*',
                         cleaned, flags=re.IGNORECASE)
    if match:
        value = float(match.group(1))
        if value <= 0:
            return None
        return {'days': value, 'days_min': value, 'days_max': value}

    # Some sheet cells store actual working days as a bare number (for example `1`).
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*', cleaned)
    if match:
        value = float(match.group(1))
        if value <= 0:
            return None
        return {'days': value, 'days_min': value, 'days_max': value}
    return None


def _sheet_rows(gid):
    url = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}'
    response = requests.get(url, headers={'User-Agent': 'WooplixProposalAgent/1.0'}, timeout=12)
    response.raise_for_status()
    content = response.content[:2_000_000].decode('utf-8-sig')
    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        raise ValueError('The sheet is empty or not publicly readable.')
    return list(reader)


def load_live_sheet_records():
    """Read the user's public Google Sheet on every analysis; blank times stay unknown."""
    records, notices = [], []
    try:
        rows = _sheet_rows(PRODUCTS_GID)
        for number, row in enumerate(rows, 2):
            product = (row.get('Zoho Product') or '').strip()
            scope = (row.get('Typical Proposal Scope') or '').strip()
            duration = _parse_days(row.get('Standard Days'))
            if not product:
                continue
            record = {'record_id': f'product:{number}', 'kind': 'module_baseline',
                      'duration_type': 'actual_product_delivery_time', 'product': product,
                      'scope': scope, 'category': (row.get('Category') or '').strip(),
                      'complexity': (row.get('Complexity Level') or '').strip(),
                      'users': (row.get('Typical User Range') or '').strip(),
                      'migration': (row.get('Data Migration') or '').strip(),
                      'notes': (row.get('Notes') or '').strip()}
            if duration:
                record.update(duration)
            elif (row.get('Standard Days') or '').strip():
                notices.append(f'Row {number} in Zoho Product Master has an unrecognized Standard Days value; its product scope is still available for comparison.')
            record['source'] = 'Google Sheet · Zoho Product Master'
            record['text'] = json.dumps(record, ensure_ascii=False)
            records.append(record)
    except Exception as exc:
        notices.append(f'Could not read Zoho Product Master live: {type(exc).__name__}.')
    try:
        rows = _sheet_rows(PROJECTS_GID)
        for number, row in enumerate(rows, 2):
            duration = _parse_days(row.get('Actual Working Days'))
            scope = (row.get('Main Scope Delivered') or '').strip()
            products = (row.get('Zoho Products Used') or '').strip()
            if not (scope or products):
                continue
            record = {'record_id': f'project:{number}', 'kind': 'completed_project',
                      'project': (row.get('Project / Client Reference') or '').strip(),
                      'products': products, 'scope': scope,
                      'complexity': (row.get('Complexity') or '').strip(),
                      'users': (row.get('Users / Departments') or '').strip(),
                      'migration': (row.get('Migration / Integration') or '').strip(),
                      'team_size': (row.get('Team Size') or '').strip(),
                      'notes': (row.get('Important Notes for Future Estimate') or '').strip(),
                      'source': 'Google Sheet · Past Delivered Projects'}
            if duration:
                record.update(duration)
            elif (row.get('Actual Working Days') or '').strip():
                notices.append(f'Row {number} in Past Delivered Projects has an unrecognized Actual Working Days value; its delivery details are still available for comparison.')
            record['text'] = json.dumps(record, ensure_ascii=False)
            records.append(record)
    except Exception as exc:
        notices.append(f'Could not read Past Delivered Projects live: {type(exc).__name__}.')
    return records, notices
