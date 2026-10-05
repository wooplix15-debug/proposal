"""Manually refresh the repository's local project data from the source Google Sheet.

This script is only for an intentional data refresh. The deployed app never runs it
and does not contact Google Sheets while analyzing or generating proposals.
"""
import csv
import io
import json
from datetime import date
from pathlib import Path

import requests

SHEET_ID = '1GnpSpbL_B1s6wDOO5NQVml75Fiz56l4bPF1s9F7brEw'
PRODUCTS_GID = '1746892041'
PROJECTS_GID = '189646703'
OUTPUT = Path(__file__).resolve().with_name('project_delivery_data.json')


def read_sheet(gid):
    url = f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={gid}'
    response = requests.get(url, headers={'User-Agent': 'WooplixProposalAgent/1.0'}, timeout=20)
    response.raise_for_status()
    reader = csv.DictReader(io.StringIO(response.content.decode('utf-8-sig')))
    if not reader.fieldnames:
        raise ValueError(f'Sheet tab {gid} has no readable column headers.')
    return list(reader)


def duration_label(value):
    value = (value or '').strip()
    if not value:
        return None
    return value.replace('—', '–').replace('−', '–')


def main():
    products = []
    for number, row in enumerate(read_sheet(PRODUCTS_GID), 2):
        name = (row.get('Zoho Product') or '').strip()
        if not name:
            continue
        products.append({
            'record_id': f'product:{number}',
            'product': name,
            'category': (row.get('Category') or '').strip(),
            'actual_working_days': duration_label(row.get('Standard Days')),
            'scope': (row.get('Typical Proposal Scope') or '').strip(),
            'complexity': (row.get('Complexity Level') or '').strip(),
            'typical_users': (row.get('Typical User Range') or '').strip(),
            'data_migration': (row.get('Data Migration') or '').strip(),
            'notes': (row.get('Notes') or '').strip(),
        })

    projects = []
    for number, row in enumerate(read_sheet(PROJECTS_GID), 2):
        products_used = (row.get('Zoho Products Used') or '').strip()
        scope = (row.get('Main Scope Delivered') or '').strip()
        if not (products_used or scope):
            continue
        # Do not copy the sheet's Project / Client Reference into the public repository.
        projects.append({
            'record_id': f'project:{number}',
            'products': products_used,
            'delivered_scope': scope,
            'complexity': (row.get('Complexity') or '').strip(),
            'users_or_departments': (row.get('Users / Departments') or '').strip(),
            'migration_or_integration': (row.get('Migration / Integration') or '').strip(),
            'actual_working_days': duration_label(row.get('Actual Working Days')),
            'team_size': (row.get('Team Size') or '').strip(),
            'future_estimate_notes': (row.get('Important Notes for Future Estimate') or '').strip(),
        })

    payload = {
        'schema_version': 1,
        'updated_from_sheet_on': date.today().isoformat(),
        'source_sheet': f'https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit',
        'products': products,
        'completed_projects': projects,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    timed = sum(bool(x['actual_working_days']) for x in products)
    print(f'Updated {OUTPUT.name}: {len(products)} product rows, {timed} with actual times, '
          f'{len(projects)} completed-project rows. Client/project reference names are excluded.')


if __name__ == '__main__':
    main()
