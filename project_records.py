"""Read product time baselines and completed-project durations from Google Sheets."""
import csv
import io
import json
import os
from pathlib import Path
import requests

SHEET_ID = os.environ.get('ZOHO_PROJECT_SHEET_ID', '1GnpSpbL_B1s6wDOO5NQVml75Fiz56l4bPF1s9F7brEw')
PRODUCTS_GID = os.environ.get('ZOHO_PRODUCTS_GID', '1746892041')
PROJECTS_GID = os.environ.get('ZOHO_PROJECTS_GID', '189646703')


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
            days = (row.get('Standard Days') or '').strip()
            if not product or not days:
                continue
            try:
                value = float(days)
                if value <= 0:
                    raise ValueError()
            except ValueError:
                notices.append(f'Row {number} in Zoho Product Master has an invalid Standard Days value and was skipped.')
                continue
            record = {'record_id': f'product:{number}', 'kind': 'module_baseline', 'product': product,
                      'days': value, 'scope': scope, 'category': (row.get('Category') or '').strip(),
                      'complexity': (row.get('Complexity Level') or '').strip(),
                      'users': (row.get('Typical User Range') or '').strip(),
                      'migration': (row.get('Data Migration') or '').strip()}
            record['source'] = 'Google Sheet · Zoho Product Master'
            record['text'] = json.dumps(record, ensure_ascii=False)
            records.append(record)
    except Exception as exc:
        notices.append(f'Could not read Zoho Product Master live: {type(exc).__name__}.')
    try:
        rows = _sheet_rows(PROJECTS_GID)
        for number, row in enumerate(rows, 2):
            days = (row.get('Actual Working Days') or '').strip()
            scope = (row.get('Main Scope Delivered') or '').strip()
            products = (row.get('Zoho Products Used') or '').strip()
            if not days or not (scope or products):
                continue
            try:
                value = float(days)
                if value <= 0:
                    raise ValueError()
            except ValueError:
                notices.append(f'Row {number} in Past Delivered Projects has invalid Actual Working Days and was skipped.')
                continue
            record = {'record_id': f'project:{number}', 'kind': 'completed_project',
                      'project': (row.get('Project / Client Reference') or '').strip(),
                      'products': products, 'scope': scope, 'days': value,
                      'complexity': (row.get('Complexity') or '').strip(),
                      'users': (row.get('Users / Departments') or '').strip(),
                      'migration': (row.get('Migration / Integration') or '').strip(),
                      'team_size': (row.get('Team Size') or '').strip(),
                      'notes': (row.get('Important Notes for Future Estimate') or '').strip(),
                      'source': 'Google Sheet · Past Delivered Projects'}
            record['text'] = json.dumps(record, ensure_ascii=False)
            records.append(record)
    except Exception as exc:
        notices.append(f'Could not read Past Delivered Projects live: {type(exc).__name__}.')
    return records, notices
