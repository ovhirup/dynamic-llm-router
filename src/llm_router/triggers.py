"""Event triggers (#132): start DLR jobs from GitHub issues, CI failures, and PR comments.

Supports both polling and webhook payload ingestion (cloud-friendly, OS-independent):
  - `issue_labeled`: an issue labeled with an allowlisted trigger label (e.g. `dlr`) by an
    allowlisted author creates a task file and dispatches `run-job`.
  - `ci_failed`: a failing CI run on a DLR branch (`router/*` or `cursor/*`) creates a diagnose/fix
    task file on that branch.
  - `pr_comment`: a PR comment mentioning `@dlr` or `/dlr` from an allowlisted author creates a
    follow-up task on that PR's branch.

Rules:
  - Only allowlisted authors and labels can trigger a job.
  - Deduplication (`<state>/state/triggers_seen.json`, mode `0600`) guarantees one event = one job.
  - Least-attention principle: speaks/alerts only when a human must act.
  - Never pushes to `main` or merges.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re

from . import alerts, config, security, selection

EVENT_KINDS = ('issue_labeled', 'ci_failed', 'pr_comment')
MENTION_RE = re.compile(r'(?i)(?:^|\s)(?:@dlr|/dlr|\bdlr\b)')


def triggers_config(policy=None):
    raw = (policy or {}).get('triggers') or {}
    return {
        'enabled': bool(raw.get('enabled', False)),
        'mode': raw.get('mode', 'poll') if raw.get('mode') in ('poll', 'webhook') else 'poll',
        'allowed_authors': [str(a).strip().lower() for a in (raw.get('allowed_authors') or []) if str(a).strip()],
        'allowed_labels': [str(l).strip().lower() for l in (raw.get('allowed_labels') or ['dlr']) if str(l).strip()],
        'branch_prefixes': tuple(raw.get('branch_prefixes') or ('router/', 'cursor/')),
    }


def state_path(policy=None):
    return config.policy_path(policy or {}, 'triggers', 'state_file', 'state/triggers_seen.json')


def tasks_dir(policy=None):
    return config.policy_path(policy or {}, 'triggers', 'tasks_dir', 'logs/trigger-tasks')


def load_seen(policy=None):
    path = state_path(policy)
    if not path.is_file():
        return {'seen': {}, 'dispatched': []}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) and 'seen' in data else {'seen': {}, 'dispatched': []}
    except (OSError, json.JSONDecodeError):
        return {'seen': {}, 'dispatched': []}


def _save_seen(policy, state):
    path = state_path(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        json.dump(security.redact_obj(state), handle, indent=2, sort_keys=True)
        handle.write('\n')
    os.chmod(path, 0o600)


def event_fingerprint(event):
    """Deterministic dedup key for an event dict."""
    if not isinstance(event, dict):
        raise ValueError('event must be a dict')
    explicit = event.get('event_id')
    if explicit:
        return str(explicit)
    key_parts = [
        str(event.get('kind') or ''),
        str(event.get('repo') or ''),
        str(event.get('issue_number') or event.get('pr_number') or event.get('run_id') or ''),
        str(event.get('comment_id') or event.get('head_sha') or event.get('label') or ''),
    ]
    return hashlib.sha256('|'.join(key_parts).encode()).hexdigest()[:16]


def validate_event(policy, event):
    """Return `(True, normalized_kind)` or `(False, refusal_reason)`."""
    cfg = triggers_config(policy)
    if not cfg['enabled']:
        return False, 'triggers_disabled'
    if not isinstance(event, dict):
        return False, 'invalid_event'
    kind = event.get('kind')
    if kind not in EVENT_KINDS:
        return False, f'unsupported_event_kind:{kind}'
    author = str(event.get('author') or '').strip().lower()
    if not cfg['allowed_authors'] or author not in cfg['allowed_authors']:
        return False, f'author_not_allowlisted:{author or "none"}'
    if kind == 'issue_labeled':
        labels = {str(l).strip().lower() for l in (event.get('labels') or [])}
        if event.get('label'):
            labels.add(str(event['label']).strip().lower())
        if not (labels & set(cfg['allowed_labels'])):
            return False, 'label_not_allowlisted'
    elif kind == 'ci_failed':
        branch = str(event.get('branch') or '')
        if not branch.startswith(cfg['branch_prefixes']) or branch in ('main', 'master'):
            return False, f'branch_not_dlr_managed:{branch}'
    elif kind == 'pr_comment':
        body = str(event.get('body') or '')
        branch = str(event.get('branch') or '')
        if not MENTION_RE.search(body):
            return False, 'comment_does_not_mention_dlr'
        if branch in ('main', 'master'):
            return False, 'never_run_on_main'
    return True, kind


def _write_task_file(policy, event_id, event):
    kind = event['kind']
    directory = tasks_dir(policy)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{kind}-{event_id}.md'
    if kind == 'issue_labeled':
        title = security.redact(str(event.get('title') or f"Issue #{event.get('issue_number')}"))
        body = security.redact(str(event.get('body') or ''))
        text = f"# Issue #{event.get('issue_number')}: {title}\n\n{body}\n"
        task_class = 'feature'
    elif kind == 'ci_failed':
        branch = security.redact(str(event.get('branch') or ''))
        log_tail = security.redact(str(event.get('log_tail') or ''))
        text = (f"# Fix CI failure on `{branch}`\n\n"
                f"CI check `{security.redact(str(event.get('check_name') or 'ci'))}` failed.\n\n"
                f"## Failure log\n```\n{log_tail[:2000]}\n```\n")
        task_class = 'bugfix'
    else:
        branch = security.redact(str(event.get('branch') or ''))
        comment = security.redact(str(event.get('body') or ''))
        text = f"# PR #{event.get('pr_number')} follow-up on `{branch}`\n\n{comment}\n"
        task_class = 'feature'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return path, task_class


def handle_event(policy, event, dispatch_fn=None, now=None):
    """Validate, deduplicate, and dispatch a single trigger event."""
    ok, why = validate_event(policy, event)
    if not ok:
        return {'dispatched': False, 'reason': why, 'event_id': None}
    eid = event_fingerprint(event)
    state = load_seen(policy)
    if eid in state['seen']:
        return {'dispatched': False, 'reason': 'duplicate_event', 'event_id': eid,
                'job_id': state['seen'][eid].get('job_id')}
    task_path, task_class = _write_task_file(policy, eid, event)
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    repo = event.get('repo') or '.'
    base = event.get('branch') if event['kind'] in ('ci_failed', 'pr_comment') else event.get('base')
    job_result = None
    if dispatch_fn is not None:
        job_result = dispatch_fn(repo=repo, task_file=str(task_path), base=base, task_class=task_class)
    job_id = (job_result or {}).get('job_id') or f'trigger-{eid}'
    record = {
        'event_id': eid,
        'kind': event['kind'],
        'author': event.get('author'),
        'task_file': str(task_path),
        'task_class': task_class,
        'job_id': job_id,
        'at': stamp,
    }
    state['seen'][eid] = record
    state.setdefault('dispatched', []).append(record)
    _save_seen(policy, state)
    if isinstance(job_result, dict) and job_result.get('status') in ('handoff_to_orchestrator', 'blocked_by_leak_check'):
        alerts.raise_alert(
            policy,
            f'trigger:{eid}:human',
            'trigger',
            f"Triggered job {job_id} needs human attention ({job_result.get('status')})",
            level='warn',
        )
    return {'dispatched': True, 'reason': 'ok', 'event_id': eid, 'job_id': job_id,
            'task_file': str(task_path), 'task_class': task_class}


def status_report(policy=None):
    """Status payload for `llm-router triggers status`."""
    policy = policy or config.load_policy()
    cfg = triggers_config(policy)
    seen = load_seen(policy)
    sel = selection.select(0, policy=policy, cached_only=True)
    return {
        'enabled': cfg['enabled'],
        'mode': cfg['mode'],
        'allowed_authors': cfg['allowed_authors'],
        'allowed_labels': cfg['allowed_labels'],
        'branch_prefixes': list(cfg['branch_prefixes']),
        'seen_events': len(seen.get('seen') or {}),
        'recent_dispatches': (seen.get('dispatched') or [])[-10:],
        'next_worker': sel.get('worker'),
    }
