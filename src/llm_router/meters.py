"""Unified meter record: one common shape for every worker's quota reading (issue #21).

A record is: worker, window, percent_used, reset_at, read_at, source. Readings older than
STALE_AFTER (30 min) are reported as unknown: percent_used becomes None and status 'unknown'.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

STALE_AFTER = timedelta(minutes=30)


def _parse(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def _percent(value):
    if type(value) in (int, float) and 0 <= value:
        return value
    return None


@dataclass(frozen=True)
class MeterRecord:
    worker: str
    window: str
    percent_used: object  # int/float, or None when unknown
    reset_at: object      # aware datetime or None
    read_at: object       # aware datetime or None
    source: str
    status: str = 'ok'    # 'ok' or 'unknown'

    def to_dict(self):
        out = asdict(self)
        for key in ('reset_at', 'read_at'):
            if out[key] is not None:
                out[key] = out[key].isoformat()
        return out


def with_freshness(record, now=None, stale_after=STALE_AFTER):
    """Return the record, or an 'unknown' copy if it has no read time, no percent, or is too old."""
    now = now or datetime.now(timezone.utc)
    if record.read_at is None or record.percent_used is None or now - record.read_at > stale_after:
        return MeterRecord(record.worker, record.window, None, record.reset_at, record.read_at,
                           record.source, 'unknown')
    return record


def from_evidence(evidence, worker=None, source='observer', now=None, stale_after=STALE_AFTER):
    """Parse an observer evidence dict ({'source_worker', 'windows': {name: row}}) into records."""
    if not isinstance(evidence, dict):
        return []
    worker = worker or evidence.get('source_worker') or 'unknown'
    records = []
    for window, row in sorted((evidence.get('windows') or {}).items()):
        row = row if isinstance(row, dict) else {}
        record = MeterRecord(worker=worker, window=window, percent_used=_percent(row.get('percent_used')),
                             reset_at=_parse(row.get('reset_at')),
                             read_at=_parse(row.get('read_at') or row.get('observed_at')),
                             source=row.get('source') or source)
        records.append(with_freshness(record, now, stale_after))
    return records


# --- Manual meters (issue #23): for workers with no readable meter (Cursor, Grok Bot) ---------------
# File shape: {"cursor": {"monthly": {"percent_used": 40, "reset_at": "...Z", "read_at": "...Z"}}, ...}
import json as _json
import os as _os
from pathlib import Path as _Path


def manual_path():
    base = _os.environ.get('LLM_ROUTER_STATE_DIR') or _os.path.expanduser('~/.local/state/llm-router')
    return _Path(base) / 'state' / 'manual_meters.json'


def _load_manual_file(path):
    try:
        data = _json.loads(_Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


MANUAL_STALE_AFTER = timedelta(hours=24)  # manual readings are daily, not every 30 min


def load_manual(path=None, now=None, stale_after=MANUAL_STALE_AFTER):
    """MeterRecords (source='manual') for every worker/window in the manual file; stale -> unknown."""
    records = []
    for worker, windows in sorted(_load_manual_file(path or manual_path()).items()):
        if isinstance(windows, dict):
            records += from_evidence({'windows': windows}, worker=worker, source='manual',
                                     now=now, stale_after=stale_after)
    return records


def set_manual(worker, window, percent_used, reset_at=None, path=None, now=None):
    """Write one manual reading (read_at = now). Returns the stored row."""
    if type(percent_used) not in (int, float) or not 0 <= percent_used <= 100:
        raise ValueError('percent_used must be a number from 0 to 100')
    now = now or datetime.now(timezone.utc)
    path = _Path(path or manual_path())
    data = _load_manual_file(path)
    row = {'percent_used': percent_used, 'read_at': now.isoformat()}
    if reset_at:
        parsed = _parse(reset_at)
        if parsed is None:
            raise ValueError('reset_at must be an ISO time with a timezone')
        row['reset_at'] = parsed.isoformat()
    data.setdefault(worker, {})[window] = row
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(_json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(path)
    return row
