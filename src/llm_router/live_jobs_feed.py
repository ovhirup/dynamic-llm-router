"""Live jobs feed for DLR Notch (#99): see every worker's running and recent jobs.

Scans `<state_dir>/logs/jobs/*.json`, `<state_dir>/worktrees/*` (in-flight jobs without a finished
JSON yet), and `<state_dir>/logs/task-output/*.log` to build a redacted, lightweight feed:
  - `job_id`, `repo`, `task`, `worker`, `workers`, `hops` (inline failover switches), `attempt`,
    `started_at`, `finished_at`, `elapsed_seconds`, `state` (`running` / `testing` / `committing` /
    `failed` / `done`), `branch`, `commit`, `log_path`, and `preview` (last 3 redacted output lines).
  - Splits jobs into `running`, `recent` (finished within 30 minutes), and `history`.
  - Never includes secrets or private terms in `preview` or metadata (`security.redact_obj`).
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from . import allocation_dispatch, config, security, task_dispatch as td

RECENT_WINDOW_SECONDS = 30 * 60
STALE_IN_FLIGHT_SECONDS = 6 * 3600
TESTS_SETTLED_SECONDS = 15
PREVIEW_LINES = 3
TAIL_BYTES = 8192

_ID_RE = re.compile(r'^(?P<repo>.+)-(?P<stamp>\d{8}-\d{6})(?:-\d+)?$')
_LOG_RE = re.compile(r'^(?P<stamp>\d{8}-\d{6})-a(?P<attempt>\d+)-(?P<tail>.+)\.log$')


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


def _stamp_to_dt(stamp, zone=timezone.utc):
    try:
        return datetime.strptime(stamp, '%Y%m%d-%H%M%S').replace(tzinfo=zone)
    except ValueError:
        return None


def parse_output_log_name(filename):
    """Parse `<YYYYMMDD-HHMMSS>-a<N>-<worker>.log` or `...-tests.log`."""
    match = _LOG_RE.match(str(filename or ''))
    if not match:
        return None
    tail = match.group('tail')
    is_tests = tail == 'tests' or tail.endswith('-tests')
    return {
        'stamp': match.group('stamp'),
        'attempt': int(match.group('attempt')),
        'worker': None if is_tests else tail,
        'is_tests': is_tests,
    }


def tail_preview(log_path, lines_count=PREVIEW_LINES, private_terms=None):
    """Read and redact the last `lines_count` non-empty lines from `log_path` (at most final 8 KB)."""
    if not log_path:
        return []
    path = Path(log_path)
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open('rb') as handle:
            if size > TAIL_BYTES:
                handle.seek(size - TAIL_BYTES)
            raw = handle.read().decode('utf-8', 'replace')
    except OSError:
        return []
    raw_lines = raw.splitlines()
    if size > TAIL_BYTES and raw_lines:
        raw_lines = raw_lines[1:]
    cleaned = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(('--- stdout', '--- stderr', '$ exit=')):
            continue
        redacted = security.redact(stripped)
        for term in private_terms or ():
            if term:
                redacted = re.sub(re.escape(str(term)), security.REDACTED, redacted, flags=re.I)
        cleaned.append(redacted[:200])
    return cleaned[-max(1, int(lines_count)):]


def _scan_output_logs(out_dir):
    by_stamp = {}
    if not out_dir.is_dir():
        return by_stamp
    for path in sorted(out_dir.glob('*.log')):
        parsed = parse_output_log_name(path.name)
        if parsed:
            by_stamp.setdefault(parsed['stamp'], []).append((parsed, path))
    return by_stamp


def _worker_info_from_logs(log_entries):
    worker_logs = [(meta, path) for meta, path in log_entries if not meta['is_tests']]
    worker_logs.sort(key=lambda item: item[0]['attempt'])
    workers = []
    hops = []
    prev = None
    for meta, _ in worker_logs:
        w = meta['worker']
        if prev and prev != w:
            hops.append({'from': prev, 'to': w, 'attempt': meta['attempt']})
        if not workers or workers[-1] != w:
            workers.append(w)
        prev = w
    attempt = max((meta['attempt'] for meta, _ in log_entries), default=0)
    latest_log = str(worker_logs[-1][1]) if worker_logs else None
    tests_logs = [path for meta, path in log_entries if meta['is_tests'] and meta['attempt'] == attempt]
    latest_tests = str(tests_logs[-1]) if tests_logs else None
    return {
        'workers': workers,
        'worker': workers[-1] if workers else None,
        'hops': hops,
        'attempt': attempt,
        'latest_log': latest_log,
        'latest_tests': latest_tests,
    }


def build_live_jobs_feed(policy=None, now=None):
    """Build the live jobs feed dict (`running`, `recent`, `history`, `jobs`) from router state."""
    policy = policy or config.load_policy()
    zone = config.tz(policy)
    now_dt = _parse_ts(now) or datetime.now(zone)
    jobs_dir = config.policy_path(policy, 'run_job', 'jobs_dir', 'logs/jobs')
    out_dir = config.policy_path(policy, 'run_task', 'output_dir', 'logs/task-output')
    wt_root = config.policy_path(policy, 'run_job', 'worktree_root', 'worktrees')
    terms = td.private_terms(policy)
    logs_by_stamp = _scan_output_logs(out_dir)

    all_jobs = []
    seen_ids = set()

    for path in sorted(jobs_dir.glob('*.json')) if jobs_dir.is_dir() else ():
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        jid = str(data.get('job_id') or path.stem)
        seen_ids.add(jid)
        match = _ID_RE.match(jid)
        stamp = match.group('stamp') if match else ''
        info = _worker_info_from_logs(logs_by_stamp.get(stamp, ()))
        workers = list(data.get('workers') or info['workers'])
        worker = workers[-1] if workers else info['worker']
        started_dt = _parse_ts(data.get('started_at')) or (_stamp_to_dt(stamp, zone) if stamp else None)
        finished_dt = _parse_ts(data.get('finished_at'))
        elapsed = round((finished_dt - started_dt).total_seconds(), 1) if (started_dt and finished_dt) else None
        ok = bool(data.get('ok'))
        status = data.get('status') or ('committed' if ok else 'failed')
        state = 'done' if (ok or status == 'committed') else 'failed'
        log_path = info['latest_log'] or str(path)
        hops = data.get('hops') or info['hops']
        allocation = (allocation_dispatch.allocation_fields(data)
                      or allocation_dispatch.read_sidecar(out_dir, stamp))
        allocation.pop('_allocation_attempt', None)
        all_jobs.append({
            **allocation,
            'job_id': jid,
            'repo': Path(str(data.get('repo') or (match.group('repo') if match else jid))).name,
            'task': data.get('task') or jid,
            'worker': worker,
            'workers': workers,
            'hops': hops,
            'attempt': max(len(data.get('attempts') or ()), info['attempt'], 1),
            'started_at': started_dt.isoformat(timespec='seconds') if started_dt else None,
            'finished_at': finished_dt.isoformat(timespec='seconds') if finished_dt else None,
            'elapsed_seconds': elapsed,
            'state': state,
            'status': status,
            'ok': ok,
            'branch': data.get('branch'),
            'commit': (data.get('commit') or '')[:7] or None,
            'log_path': log_path,
            'preview': tail_preview(info['latest_log'], private_terms=terms),
            '_sort_ts': (finished_dt or started_dt or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        })

    for wt in sorted(wt_root.glob('*')) if wt_root.is_dir() else ():
        if not wt.is_dir() or wt.name in seen_ids:
            continue
        match = _ID_RE.match(wt.name)
        if not match:
            continue
        repo_name, stamp = match.group('repo'), match.group('stamp')
        info = _worker_info_from_logs(logs_by_stamp.get(stamp, ()))
        started_dt = _stamp_to_dt(stamp, zone)
        state = 'running'
        if info['latest_tests']:
            try:
                tests_mtime = datetime.fromtimestamp(Path(info['latest_tests']).stat().st_mtime, tz=zone)
                state = 'committing' if (now_dt - tests_mtime).total_seconds() > TESTS_SETTLED_SECONDS else 'testing'
            except OSError:
                state = 'testing'
        elapsed = round(max(0.0, (now_dt - started_dt).total_seconds()), 1) if started_dt else None
        allocation = allocation_dispatch.read_sidecar(out_dir, stamp)
        allocation_attempt = allocation.pop('_allocation_attempt', 0)
        if allocation and allocation['allocation']['worker']:
            chosen = allocation['allocation']['worker']
            info['worker'] = chosen
            if not info['workers'] or info['workers'][-1] != chosen:
                info['workers'].append(chosen)
        all_jobs.append({
            **allocation,
            'job_id': wt.name,
            'repo': repo_name,
            'task': wt.name,
            'worker': info['worker'],
            'workers': info['workers'],
            'hops': info['hops'],
            'attempt': max(1, info['attempt'], allocation_attempt),
            'started_at': started_dt.isoformat(timespec='seconds') if started_dt else None,
            'finished_at': None,
            'elapsed_seconds': elapsed,
            'state': state,
            'status': state,
            'ok': None,
            'branch': None,
            'commit': None,
            'log_path': info['latest_log'],
            'preview': tail_preview(info['latest_log'], private_terms=terms),
            '_sort_ts': (started_dt or now_dt).timestamp(),
        })

    running = [j for j in all_jobs if j['state'] in ('running', 'testing', 'committing')]
    finished = [j for j in all_jobs if j['state'] in ('done', 'failed')]
    running.sort(key=lambda j: -j['_sort_ts'])
    finished.sort(key=lambda j: -j['_sort_ts'])

    now_ts = now_dt.timestamp()
    recent = [j for j in finished if (now_ts - j['_sort_ts']) <= RECENT_WINDOW_SECONDS]
    history = [j for j in finished if (now_ts - j['_sort_ts']) > RECENT_WINDOW_SECONDS]

    for item in all_jobs:
        item.pop('_sort_ts', None)

    return security.redact_obj({
        'updated_at': now_dt.isoformat(timespec='seconds'),
        'running': running,
        'recent': recent,
        'history': history,
    })
