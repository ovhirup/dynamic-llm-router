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
