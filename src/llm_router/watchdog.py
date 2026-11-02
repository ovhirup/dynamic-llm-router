"""Watchdog for stalled, looping, or drifting workers (#131).

Monitors running DLR jobs:
  1. Per-job heartbeat file (`<state>/logs/heartbeats/<job_id>.json`, mode `0600`) recording
     `job_id`, `worker`, `pid`, `worktree`, `allowed_paths`, `log_path`, `updated_at`.
  2. Detects four failure modes:
     - `stalled`: no heartbeat or log update for `stall_seconds` (default 300s).
     - `looping`: >= `max_identical_lines` (default 3) repeated identical command/error lines in the output log.
     - `drifting`: modified/untracked files in `worktree` fall outside `allowed_paths`.
     - `infra_failure`: worker output log shows auth expiration or quota exhaustion mid-job.
  3. Recovery (`recover_job`): terminates the stuck process group (preserving the worktree and logs for
     review, never pushing), selects the next eligible worker from live budget (`selection.select`),
     and notifies (`alerts.raise_alert`) only when no eligible worker remains and a human must act.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal

from . import alerts, config, security, selection, task_dag, task_dispatch as td

DEFAULT_STALL_SECONDS = 300
DEFAULT_LOOP_THRESHOLD = 3


def heartbeats_dir(policy=None):
    return config.policy_path(policy or {}, 'run_job', 'heartbeats_dir', 'logs/heartbeats')


def heartbeat_path(policy, job_id):
    return heartbeats_dir(policy) / f'{job_id}.json'


def write_heartbeat(policy, job_id, worker, worktree, pid=None, allowed_paths=None, log_path=None, now=None):
    """Write or update the per-job heartbeat file (`0600`)."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    path = heartbeat_path(policy, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = security.redact_obj({
        'job_id': str(job_id),
        'worker': str(worker),
        'pid': int(pid) if pid is not None else None,
        'worktree': str(worktree),
        'allowed_paths': list(allowed_paths) if allowed_paths else None,
        'log_path': str(log_path) if log_path else None,
        'updated_at': stamp,
    })
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(payload, handle, indent=2)
        handle.write('\n')
    os.chmod(path, 0o600)
    return path


def read_heartbeat(policy, job_id):
    path = heartbeat_path(policy, job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _parse_ts(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def detect_looping(log_text, threshold=DEFAULT_LOOP_THRESHOLD):
    """Return `(True, repeated_line)` when `threshold` consecutive non-trivial lines are identical."""
    lines = [re.sub(r'\s+', ' ', l.strip()) for l in (log_text or '').splitlines() if len(l.strip()) >= 8]
    if len(lines) < threshold:
        return False, None
    streak = 1
    for idx in range(1, len(lines)):
        if lines[idx] == lines[idx - 1]:
            streak += 1
            if streak >= threshold:
                return True, security.redact(lines[idx][:200])
        else:
            streak = 1
    return False, None


def detect_drifting(changed_paths, allowed_paths):
    """Return `(True, out_of_scope_list)` when `allowed_paths` is set and edits escape it."""
    if not allowed_paths:
        return False, []
    outside = sorted(
        p for p in (changed_paths or ())
        if not any(s == p or task_dag._covers(s, p) for s in allowed_paths)
    )
    return bool(outside), outside


def inspect_job(policy, heartbeat, now=None, changed_paths_fn=None, stall_seconds=DEFAULT_STALL_SECONDS,
                loop_threshold=DEFAULT_LOOP_THRESHOLD):
    """Inspect one running job's heartbeat, output log, and worktree diff.

    Returns `{'job_id', 'worker', 'status': 'healthy'|'stalled'|'looping'|'drifting'|'infra_failure', 'detail'}`.
    """
    now_dt = _parse_ts(now) or datetime.now(timezone.utc)
    job_id = heartbeat.get('job_id')
    worker = heartbeat.get('worker')
    hb_ts = _parse_ts(heartbeat.get('updated_at'))
    log_path = Path(heartbeat.get('log_path') or '/nonexistent')
    log_text = ''
    if log_path.is_file():
        try:
            log_text = log_path.read_text(errors='replace')[-8000:]
            mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone.utc)
            if hb_ts is None or mtime > hb_ts:
                hb_ts = mtime
        except OSError:
            pass

    quota_re = re.compile(policy['run_task']['quota_error_pattern'])
    if log_text and (td.AUTH_ERROR.search(log_text) or quota_re.search(log_text)):
        kind = 'auth_expired' if td.AUTH_ERROR.search(log_text) else 'quota_exhausted'
        return {'job_id': job_id, 'worker': worker, 'status': 'infra_failure', 'detail': kind}

    looping, line = detect_looping(log_text, threshold=loop_threshold)
    if looping:
        return {'job_id': job_id, 'worker': worker, 'status': 'looping', 'detail': line}

    worktree = heartbeat.get('worktree')
    allowed = heartbeat.get('allowed_paths')
    if allowed and worktree and Path(worktree).is_dir():
        if changed_paths_fn is None:
            from . import job_dispatch
            changed_paths_fn = job_dispatch.changed_paths
        changed = changed_paths_fn(Path(worktree))
        drifting, outside = detect_drifting(changed, allowed)
        if drifting:
            return {'job_id': job_id, 'worker': worker, 'status': 'drifting',
                    'detail': 'out_of_scope: ' + ', '.join(outside)}

    if hb_ts is not None and (now_dt - hb_ts).total_seconds() >= float(stall_seconds):
        elapsed = round((now_dt - hb_ts).total_seconds(), 1)
        return {'job_id': job_id, 'worker': worker, 'status': 'stalled',
                'detail': f'no heartbeat or log output for {elapsed}s'}

    return {'job_id': job_id, 'worker': worker, 'status': 'healthy', 'detail': None}


def recover_job(policy, heartbeat, diagnosis, select_fn=None, kill_fn=None, re_runner=None):
    """Kill the stuck worker (preserving worktree/logs), reassign to next eligible worker, or alert human."""
    job_id = heartbeat.get('job_id')
    failed_worker = heartbeat.get('worker')
    pid = heartbeat.get('pid')
    if pid:
        killer = kill_fn or (lambda p: td._kill_group(p, signal.SIGTERM))
        killer(pid)

    sel = (select_fn or (lambda: selection.select(0, policy=policy, cached_only=True)))()
    eligible = [w for w in (sel.get('eligible') or []) if w != failed_worker and w != config.ORCHESTRATOR]
    next_worker = eligible[0] if eligible else None
    action = 'reassigned' if next_worker else 'human_attention_required'
    reassigned_result = None
    if next_worker and re_runner is not None:
        reassigned_result = re_runner(job_id, next_worker, heartbeat.get('worktree'))
    if not next_worker:
        alerts.raise_alert(
            policy,
            f'watchdog:{job_id}:{diagnosis["status"]}',
            'watchdog',
            f"Watchdog stopped {job_id} ({failed_worker}, {diagnosis['status']}): no eligible worker left for reassignment",
            worker=failed_worker,
            level='warn',
        )
    return {
        'job_id': job_id,
        'stopped_worker': failed_worker,
        'status': diagnosis['status'],
        'detail': diagnosis.get('detail'),
        'action': action,
        'next_worker': next_worker,
        'worktree_preserved': True,
        'reassigned_result': reassigned_result,
    }


def check_all(policy=None, now=None, stall_seconds=DEFAULT_STALL_SECONDS, loop_threshold=DEFAULT_LOOP_THRESHOLD,
              recover=False, select_fn=None, kill_fn=None, re_runner=None):
    """Scan all heartbeat files under `<state>/logs/heartbeats/` and optionally recover unhealthy jobs."""
    policy = policy or config.load_policy()
    directory = heartbeats_dir(policy)
    files = sorted(directory.glob('*.json')) if directory.is_dir() else []
    checked, actions = [], []
    for path in files:
        try:
            hb = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        diag = inspect_job(policy, hb, now=now, stall_seconds=stall_seconds, loop_threshold=loop_threshold)
        checked.append(diag)
        if recover and diag['status'] != 'healthy':
            actions.append(recover_job(policy, hb, diag, select_fn=select_fn, kill_fn=kill_fn, re_runner=re_runner))
    return {'checked': checked, 'actions': actions, 'ok': all(c['status'] == 'healthy' for c in checked)}
