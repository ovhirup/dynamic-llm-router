"""Token burn-down across models (#128): live usage dashboard, time-series store, and burn-rate projections.

Provides:
  1. Time-series snapshot store (`<state>/state/usage_snapshots.jsonl`, mode `0600`) recording
     per-worker readings (`worker`, `window`, `percent_used`, `remaining_percent`, `reset_at`,
     `source`, `collected_at`).
  2. Burn-rate estimator (`compute_burn_rate(snapshots, worker, now)`): computes `percent_per_hour`
     over recent readings and `projected_days_until_empty` (`remaining_percent / (percent_per_hour * 24)`).
  3. `usage_report(policy, now=None)`: collects every configured worker's meter (failing soft to
     `unknown` when missing or stale), computes remaining allowance, burn rate, and which worker
     DLR would pick next given live remaining budget.
  4. `render_usage_table(report)`: human-readable ASCII table for `llm-router usage` (and `--json`
     for machine consumers).
"""
from datetime import datetime, timezone
import fcntl
import json
import os

from . import config, quota_service, security, selection


def snapshots_path(policy=None):
    return config.policy_path(policy or {}, 'quota', 'usage_snapshots_file', 'state/usage_snapshots.jsonl')


def _parse_ts(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def record_snapshot(policy, rows, now=None):
    """Append current worker meter rows to `<state>/state/usage_snapshots.jsonl` (`0600`)."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    path = snapshots_path(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    written = []
    with os.fdopen(fd, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        for row in rows or ():
            if not isinstance(row, dict) or not row.get('worker'):
                continue
            used = row.get('percent_used')
            entry = security.redact_obj({
                'worker': str(row['worker']),
                'window': str(row.get('window') or 'weekly'),
                'percent_used': float(used) if type(used) in (int, float) else None,
                'remaining_percent': round(max(0.0, 100.0 - float(used)), 2) if type(used) in (int, float) else None,
                'reset_at': row.get('reset_at') or row.get('resets'),
                'source': row.get('source') or row.get('freshness') or 'unknown',
                'collected_at': row.get('collected_at') or stamp,
            })
            handle.write(json.dumps(entry, sort_keys=True) + '\n')
            written.append(entry)
    os.chmod(path, 0o600)
    return written


def load_snapshots(policy=None, worker=None):
    """Load historical usage snapshots (optionally filtered to `worker`)."""
    path = snapshots_path(policy)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors='replace').splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and (worker is None or row.get('worker') == worker):
            out.append(row)
    return out


def compute_burn_rate(snapshots, worker, current_used=None, now=None):
    """Compute `{'percent_per_hour', 'projected_days_until_empty'}` from recent snapshots for `worker`."""
    points = []
    for item in snapshots or ():
        if not isinstance(item, dict) or item.get('worker') != worker:
            continue
        used = item.get('percent_used')
        ts = _parse_ts(item.get('collected_at'))
        if type(used) in (int, float) and ts is not None:
            points.append((ts, float(used)))
    if type(current_used) in (int, float) and now is not None:
        now_ts = _parse_ts(now)
        if now_ts is not None and (not points or points[-1][0] < now_ts):
            points.append((now_ts, float(current_used)))
    points.sort(key=lambda p: p[0])
    if len(points) < 2:
        return {'percent_per_hour': None, 'projected_days_until_empty': None}
    first_ts, first_used = points[0]
    last_ts, last_used = points[-1]
    hours = (last_ts - first_ts).total_seconds() / 3600.0
    if hours <= 0 or last_used < first_used:
        return {'percent_per_hour': 0.0, 'projected_days_until_empty': None}
    rate = round((last_used - first_used) / hours, 4)
    remaining = max(0.0, 100.0 - last_used)
    days = round(remaining / (rate * 24.0), 2) if rate > 0 else None
    return {'percent_per_hour': rate, 'projected_days_until_empty': days}


def usage_report(policy=None, now=None, io=None, snapshots=None):
    """Build the live usage and burn-down report across all configured workers."""
    policy = policy or config.load_policy()
    now_dt = _parse_ts(now) or datetime.now(timezone.utc)
    sel = selection.select(0, policy=policy, cached_only=True, now=now_dt, io=io)
    status_rows = {r['worker']: r for r in quota_service.status_rows(policy, now=now_dt, io=io)}
    history = snapshots if snapshots is not None else load_snapshots(policy)
    workers_out = []
    seen = list(dict.fromkeys(list(policy.get('worker_order') or []) + list(status_rows)))
    for worker in seen:
        srow = status_rows.get(worker) or {}
        used = sel.get('meters', {}).get(worker)
        if used is None and isinstance(srow.get('used'), str) and srow['used'].endswith('%'):
            try:
                used = float(srow['used'][:-1])
            except ValueError:
                used = None
        remaining = round(max(0.0, 100.0 - float(used)), 2) if type(used) in (int, float) else None
        burn = compute_burn_rate(history, worker, current_used=used, now=now_dt)
        workers_out.append({
            'worker': worker,
            'used_percent': used,
            'remaining_percent': remaining,
            'freshness': srow.get('freshness') or sel.get('reasons', {}).get(worker) or 'unknown',
            'resets': srow.get('resets') or '-',
            'eligible': worker in (sel.get('eligible') or []),
            'burn_rate_percent_per_hour': burn['percent_per_hour'],
            'projected_days_until_empty': burn['projected_days_until_empty'],
        })
    return security.redact_obj({
        'updated_at': now_dt.isoformat(timespec='seconds'),
        'next_worker': sel.get('worker'),
        'next_worker_reason': sel.get('reason'),
        'eligible_workers': sel.get('eligible') or [],
        'workers': workers_out,
    })


def render_usage_table(report):
    """Render `usage_report()` as a clean human-readable table."""
    lines = [
        f"DLR Token Burn-Down ({report.get('updated_at')})",
        f"Next worker: {report.get('next_worker') or 'none'} ({report.get('next_worker_reason')})",
        '',
        'WORKER        USED     REMAIN   STATUS              RESETS                     BURN/HR    DAYS LEFT',
        '------------  -------  -------  ------------------  -------------------------  ---------  ---------',
    ]
    for row in report.get('workers') or ():
        used = f"{row['used_percent']:g}%" if type(row.get('used_percent')) in (int, float) else 'unknown'
        rem = f"{row['remaining_percent']:g}%" if type(row.get('remaining_percent')) in (int, float) else 'unknown'
        rate = f"{row['burn_rate_percent_per_hour']:g}%/h" if type(row.get('burn_rate_percent_per_hour')) in (int, float) else '-'
        days = f"{row['projected_days_until_empty']:g}d" if type(row.get('projected_days_until_empty')) in (int, float) else '-'
        lines.append(
            f"{str(row.get('worker', '')):<12}  {used:<7}  {rem:<7}  "
            f"{str(row.get('freshness', 'unknown'))[:18]:<18}  {str(row.get('resets', '-'))[:25]:<25}  "
            f"{rate:<9}  {days}"
        )
    return '\n'.join(lines)
