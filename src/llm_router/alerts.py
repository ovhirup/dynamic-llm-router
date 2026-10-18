"""Append-only, deduplicated router alerts: <state>/logs/alerts.jsonl plus a short ALERTS.md.

Never sends email or chat. An optional macOS banner (osascript display notification) is off by default.
"""
import fcntl
import hashlib
import json
import subprocess

from . import config, security


def _cfg(policy):
    return policy.get('alerts', {})


def _path(policy, key, default):
    return config.policy_path(policy, 'alerts', key, default)


def _now_local(policy, now=None):
    zone = config.tz(policy)
    return (now or config.now_local(policy)).astimezone(zone)


def read_all(policy):
    path = _path(policy, 'file', 'logs/alerts.jsonl')
    alerts, acked = {}, set()
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return [], acked
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get('type') == 'ack':
            acked.add(record.get('id'))
        elif record.get('type') == 'alert':
            alerts[record['id']] = record
    return list(alerts.values()), acked


def open_alerts(policy):
    alerts, acked = read_all(policy)
    return [a for a in alerts if a['id'] not in acked]


def _append(policy, records):
    path = _path(policy, 'file', 'logs/alerts.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + '\n')


def raise_alert(policy, key, kind, message, worker=None, level='info', now=None, notifier=None):
    """Record an alert unless one with the same dedupe key already exists. Returns it or None."""
    existing, _ = read_all(policy)
    if any(a.get('key') == key for a in existing):
        return None
    record = {'type': 'alert', 'id': hashlib.sha256(key.encode()).hexdigest()[:12], 'key': key, 'kind': kind,
              'level': level, 'worker': worker, 'message': security.redact(message),
              'created_at': _now_local(policy, now).isoformat(timespec='seconds')}
    _append(policy, [record])
    if _cfg(policy).get('macos_notification'):
        (notifier or macos_banner)(f'Router {level}', record['message'])
    return record


def ack(policy, ids=None, now=None):
    """Acknowledge the given alert ids (or all open ones). Returns the acknowledged ids."""
    pending = [a['id'] for a in open_alerts(policy)]
    chosen = pending if not ids else [i for i in ids if i in pending]
    stamp = _now_local(policy, now).isoformat(timespec='seconds')
    _append(policy, [{'type': 'ack', 'id': i, 'at': stamp} for i in chosen])
    return chosen


def macos_banner(title, message, runner=subprocess.run):
    """Notification Centre banner only (no window). Used only when alerts.macos_notification is true."""
    script = 'display notification %s with title %s' % (json.dumps(message[:180]), json.dumps(title))
    try:
        runner(['/usr/bin/osascript', '-e', script], stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def job_alert(policy, summary, now=None, notifier=None):
    """Called by run-job: failed or leak-blocked jobs raise one alert each."""
    status = summary.get('status')
    if status == 'committed':
        return None
    level = 'stop' if status == 'blocked_by_leak_check' else 'warn'
    findings = len((summary.get('leak_check') or {}).get('findings') or [])
    message = (f'run-job {summary.get("job_id")} {status}: branch {summary.get("branch")}, tests '
               f'{summary.get("tests_status")}, worktree {summary.get("worktree_state")}'
               + (f', {findings} leak finding(s)' if findings else ''))
    return raise_alert(policy, f'job:{summary.get("job_id")}:{status}', 'job', message,
                       level=level, now=now, notifier=notifier)


def write_summary(policy, rows, now=None):
    """Short ALERTS.md: meter table + open alerts."""
    lines = ['# Router alerts and meters', '', f'Updated {_now_local(policy, now).strftime("%Y-%m-%d %H:%M %Z").strip()}.', '',
             '| Meter | Used | Fresh | Resets | Eligible |', '|---|---|---|---|---|']
    for row in rows:
        lines.append(f'| {row["worker"]} | {row["used"]} | {row["freshness"]} | {row["resets"]} | {row["eligible"]} |')
    pending = open_alerts(policy)
    lines += ['', f'## Open alerts ({len(pending)})', '']
    lines += [f'- `{a["id"]}` {a.get("created_at", "")[:16]} **{a["level"]}** {a["message"]}' for a in pending] or ['- none']
    lines += ['', 'Acknowledge: `llm-router alerts ack [--id ID ...]`.']
    path = _path(policy, 'summary', 'logs/ALERTS.md')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(security.redact('\n'.join(lines) + '\n'))
    return path
