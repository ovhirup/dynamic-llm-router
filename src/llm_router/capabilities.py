"""Sourced capability claims (docs/ROUTER_BRAIN.md (c)): every score must cite where it came from.

capabilities.json maps worker -> task class -> {score, claim, source, verified_at}. A claim without a
source (an https:// URL, an eval:<report id>, or a repo doc path such as docs/workers.md) is rejected.
"""
import copy
import json
import re
from datetime import date
from pathlib import Path

DEFAULT_PATH = Path(__file__).with_name('capabilities.json')
_DOC_PATH = re.compile(r'^[A-Za-z0-9_.\-]+(/[A-Za-z0-9_.\-]+)*$')
_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_cache = {}


def _valid_source(source):
    if not isinstance(source, str) or not source.strip():
        return False
    source = source.strip()
    if source.startswith('https://'):
        return len(source) > len('https://')
    if source.startswith('eval:'):
        return bool(source[len('eval:'):].strip())
    return bool(_DOC_PATH.match(source)) and '..' not in source.split('/')


def _valid_date(value):
    if not isinstance(value, str) or not _ISO_DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_entry(entry, where='entry'):
    """Raise ValueError unless entry is a complete, sourced claim. Returns entry."""
    if not isinstance(entry, dict):
        raise ValueError(f'{where}: claim must be an object')
    score = entry.get('score')
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.0 <= score <= 1.0:
        raise ValueError(f'{where}: score must be a number in [0, 1]')
    claim = entry.get('claim')
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError(f'{where}: claim must be a non-empty string')
    if not _valid_source(entry.get('source')):
        raise ValueError(f'{where}: unsourced claim; source must be an https:// URL, eval:<id> or repo doc path')
    if not _valid_date(entry.get('verified_at')):
        raise ValueError(f'{where}: verified_at must be a YYYY-MM-DD date')
    return entry


def validate_table(table):
    if not isinstance(table, dict) or not isinstance(table.get('workers'), dict):
        raise ValueError('capabilities: top-level "workers" object is required')
    for worker, classes in table['workers'].items():
        if not isinstance(classes, dict):
            raise ValueError(f'capabilities.{worker}: must map task class to claim')
        for task_class, entry in classes.items():
            validate_entry(entry, f'capabilities.{worker}.{task_class}')
    return table


def load_capabilities(path=None):
    """Load and validate capabilities.json (packaged copy by default); raises ValueError if invalid."""
    path = Path(path) if path is not None else DEFAULT_PATH
    try:
        table = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'capabilities: invalid JSON in {path}: {exc}') from exc
    return validate_table(table)


def _default_table():
    if 'default' not in _cache:
        _cache['default'] = load_capabilities()
    return _cache['default']


def score_for(worker, task_class, table=None):
    """Validated score for worker/task_class, or None when the pair is not listed."""
    table = _default_table() if table is None else table
    entry = ((table.get('workers') or {}).get(worker) or {}).get(task_class)
    if entry is None:
        return None
    return float(validate_entry(entry, f'capabilities.{worker}.{task_class}')['score'])


def update_claim(table, worker, task_class, score, claim, source, verified_at):
    """Validate and set one claim in table (mutated and returned). Unsourced updates raise ValueError."""
    entry = {'score': score, 'claim': claim, 'source': source, 'verified_at': verified_at}
    validate_entry(entry, f'capabilities.{worker}.{task_class}')
    if not isinstance(table, dict):
        raise ValueError('capabilities: table must be an object')
    workers = table.setdefault('workers', {})
    workers.setdefault(worker, {})[task_class] = copy.deepcopy(entry)
    return table
