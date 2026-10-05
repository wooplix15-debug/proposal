"""Load actual delivery history bundled with the proposal app."""
import copy
import json
from pathlib import Path
import re

DATA_FILE = Path(__file__).resolve().with_name('project_delivery_data.json')
_RECORD_CACHE = None


def _parse_days(value):
    """Read single days, ranges, bare numeric day values, and per-unit times."""
    if value is None:
        return None
    cleaned = str(value).strip().replace('–', '-').replace('—', '-').replace('−', '-')
    if not cleaned:
        return None
    match = re.fullmatch(
        r'(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:working\s*)?days?',
        cleaned, flags=re.IGNORECASE)
    if match:
        low, high = float(match.group(1)), float(match.group(2))
        if low <= 0 or high < low:
            return None
        return {'days': f'{match.group(1)}–{match.group(2)}',
                'days_min': low, 'days_max': high}

    match = re.fullmatch(
        r'(\d+(?:\.\d+)?)\s*(?:working\s*)?days?\s*(?:per|/)\s*(.+)',
        cleaned, flags=re.IGNORECASE)
    if match:
        number = float(match.group(1))
        if number <= 0:
            return None
        unit = match.group(2).strip()
        return {'days': f'{match.group(1)} day per {unit}', 'days_min': None,
                'days_max': None, 'duration_basis': f'per {unit}'}

    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(?:(?:working\s*)?days?)?', cleaned,
                         flags=re.IGNORECASE)
    if match:
        number = float(match.group(1))
        if number <= 0:
            return None
        return {'days': number, 'days_min': number, 'days_max': number}
    return None


def _make_product_record(row):
    duration = _parse_days(row.get('actual_working_days'))
    record = {
        'record_id': row['record_id'],
        'kind': 'module_baseline',
        'duration_type': 'actual_product_delivery_time',
        'product': row.get('product', ''),
        'scope': row.get('scope', ''),
        'category': row.get('category', ''),
        'complexity': row.get('complexity', ''),
        'users': row.get('typical_users', ''),
        'migration': row.get('data_migration', ''),
        'notes': row.get('notes', ''),
        'source': 'Bundled project delivery data · Product actual times',
    }
    if duration:
        record.update(duration)
    elif row.get('actual_working_days'):
        raise ValueError(f"Unrecognized actual time for {row.get('product', 'a product')}.")
    record['text'] = json.dumps(record, ensure_ascii=False)
    return record


def _make_project_record(row):
    duration = _parse_days(row.get('actual_working_days'))
    record = {
        'record_id': row['record_id'],
        'kind': 'completed_project',
        'products': row.get('products', ''),
        'scope': row.get('delivered_scope', ''),
        'complexity': row.get('complexity', ''),
        'users': row.get('users_or_departments', ''),
        'migration': row.get('migration_or_integration', ''),
        'team_size': row.get('team_size', ''),
        'notes': row.get('future_estimate_notes', ''),
        'source': 'Bundled project delivery data · Completed projects',
    }
    if duration:
        record.update(duration)
    elif row.get('actual_working_days'):
        raise ValueError(f"Unrecognized actual time for completed-project row {row['record_id']}.")
    record['text'] = json.dumps(record, ensure_ascii=False)
    return record


def load_project_records():
    """Return the bundled, versioned data file; no runtime Google Sheets request is made."""
    global _RECORD_CACHE
    if _RECORD_CACHE is None:
        try:
            payload = json.loads(DATA_FILE.read_text(encoding='utf-8'))
            if payload.get('schema_version') != 1:
                raise ValueError('Unsupported project data file version.')
            records = [_make_product_record(row) for row in payload.get('products', [])]
            records.extend(_make_project_record(row) for row in payload.get('completed_projects', []))
            _RECORD_CACHE = (records, [])
        except Exception as exc:
            _RECORD_CACHE = ([], [f'Could not read bundled project data: {type(exc).__name__}.'])
    records, notices = _RECORD_CACHE
    return copy.deepcopy(records), list(notices)
