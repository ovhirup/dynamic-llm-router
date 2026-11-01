"""Local merge queue for finished task-DAG units (#40): tests + diff-scope gate (#12) + review flag (#13).

Reads unit_worktrees.finished_units() and records one gate result per unit in
<state>/logs/merge-queue/<batch id>.json. The best status is `ready_for_human`: a human "yes" (outside this
module) decides integration. Never pushes, never merges, never opens a PR, never touches the main checkout.
"""
from datetime import datetime
import json
from pathlib import Path

from . import config, task_dag
from . import unit_worktrees as uw
from .job_dispatch import Refused, RepoLock, _git, _path, detect_test_cmd, repo_settings, run_tests

REVIEWED_WORKERS = ('codex', 'cursor', 'local')
VERDICTS = ('approve', 'reject')


def _queue_path(policy, batch_id):
    if not task_dag.UNIT_ID.fullmatch(batch_id or ''):
        raise Refused(f'bad batch id: {batch_id!r}')
    return _path(policy, 'merge_queue_dir', 'logs/merge-queue') / f'{batch_id}.json'


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def load_queue(batch_id, policy=None):
    policy = policy or config.load_policy()
    try:
        return json.loads(_queue_path(policy, batch_id).read_text())
    except FileNotFoundError:
        return {'batch_id': batch_id, 'entries': {}}


def review_required(worker):
    return worker in REVIEWED_WORKERS or str(worker).startswith('aider')


def out_of_scope(paths, scope):
    """Changed paths not covered by any scope entry (exact match or under a trailing-'/' directory)."""
    return sorted(p for p in paths if not any(s == p or task_dag._covers(s, p) for s in scope))


def diff_paths(repo, base_sha, head):
    done = _git(repo, 'diff', '--name-only', '-z', '--no-renames', f'{base_sha}..{head}', check=True)
    return set(filter(None, done.stdout.split('\0')))


def _test_command(policy, repo):
    """repos.json test_cmd, else one detected from the source repo (never from the worker's worktree)."""
    settings = repo_settings(policy, repo)
    command = settings.get('test_cmd') or detect_test_cmd(repo, repo)
    timeout = settings.get('test_timeout_seconds', policy['run_job']['test_timeout_seconds'])
    return command, timeout


def _status(record):
    """Recompute status + reasons from the gate fields. Any failed gate blocks; a missing review needs review."""
    reasons = list(record.get('gate_reasons', []))
    review = record['review']
    if record['review_required'] and review.get('status') == 'recorded' and review.get('verdict') == 'reject':
        reasons.append('review_rejected')
    if reasons:
        status = 'blocked'
    elif record['review_required'] and not (review.get('status') == 'recorded' and review.get('verdict') == 'approve'):
        status, reasons = 'needs_review', ['review_pending']
    else:
        status = 'ready_for_human'
    record.update(status=status, reasons=reasons)
    return record


def evaluate_unit(policy, repo, batch_id, entry, log_dir, test_runner=None):
    record = {'batch_id': batch_id, 'unit': entry['id'], 'worker': entry['worker'], 'branch': entry['branch'],
              'base_sha': entry['base_sha'], 'head': entry['head'], 'scope': entry['scope'],
              'worktree': entry['worktree'], 'evaluated_at': _now()}
    reasons = []
    paths = diff_paths(repo, entry['base_sha'], entry['head'])
    outside = out_of_scope(paths, entry['scope'])
    record['diff_scope'] = {'status': 'fail' if outside else 'ok', 'out_of_scope': outside,
                            'changed': sorted(paths)}
    if outside:
        reasons.append('diff_out_of_scope')
    worktree = Path(entry['worktree'])
    if not worktree.is_dir():
        record['tests'] = {'status': 'refused', 'command': None, 'reason': 'unit worktree was removed'}
        reasons.append('worktree_missing')
    elif _git(worktree, 'rev-parse', 'HEAD').stdout.strip() != entry['head']:
        record['tests'] = {'status': 'refused', 'command': None, 'reason': 'worktree HEAD is not the branch head'}
        reasons.append('worktree_head_mismatch')
    else:
        command, timeout = _test_command(policy, repo)
        log_dir.mkdir(parents=True, exist_ok=True)
        kwargs = {'runner': test_runner} if test_runner else {}
        tests = run_tests(policy, worktree, command, timeout, log_dir / f"{entry['id']}-{entry['head'][:12]}.log",
                          **kwargs)
        record['tests'] = tests
        if tests['status'] != 'passed':  # no_tests / failed / timeout never count as pass
            reasons.append(f"tests_{tests['status']}")
    record['gate_reasons'] = reasons
    record['review_required'] = review_required(entry['worker'])
    record['review'] = {'status': 'pending'} if record['review_required'] else {'status': 'skipped'}
    return record


def queue_batch(batch_id, policy=None, test_runner=None):
    """Evaluate every finished unit not already recorded at its current head; returns the report."""
    policy = policy or config.load_policy()
    manifest = uw.load_manifest(batch_id, policy)
    repo = Path(manifest['repo'])
    path = _queue_path(policy, batch_id)
    log_dir = path.parent / batch_id
    with RepoLock(_path(policy, 'lock_dir', 'logs/locks'), repo):
        queue = load_queue(batch_id, policy)
        queue.update(batch_id=batch_id, repo=str(repo))
        evaluated = []
        for entry in uw.finished_units(batch_id, policy):
            old = queue['entries'].get(entry['id'])
            if old and old.get('head') == entry['head']:
                continue
            record = evaluate_unit(policy, repo, batch_id, entry, log_dir, test_runner)
            if old and 'human_decision' in old:
                record['human_decision'] = old['human_decision']
            queue['entries'][entry['id']] = _status(record)
            evaluated.append(entry['id'])
        queue['updated_at'] = _now()
        uw._save(path, queue)
    entries = [queue['entries'][uid] for uid in evaluated]
    return {'batch_id': batch_id, 'queue_file': str(path), 'evaluated': evaluated,
            'entries': list(queue['entries'].values()),
            'ok': all(e['status'] == 'ready_for_human' for e in entries)}


def record_review(batch_id, unit_id, verdict, notes='', policy=None, reviewer=None, head=None):
    """Record a review verdict (approve|reject) for a queued unit at its current head; returns the entry.
    head: the head that was reviewed; refused when the queued head has moved since (stale review)."""
    policy = policy or config.load_policy()
    if verdict not in VERDICTS:
        raise Refused(f'verdict must be one of {VERDICTS}')
    path = _queue_path(policy, batch_id)
    manifest = uw.load_manifest(batch_id, policy)
    with RepoLock(_path(policy, 'lock_dir', 'logs/locks'), Path(manifest['repo'])):
        queue = load_queue(batch_id, policy)
        record = queue['entries'].get(unit_id)
        if record is None:
            raise Refused(f'unit {unit_id!r} is not in the merge queue for {batch_id}')
        if not record['review_required']:
            raise Refused(f'unit {unit_id!r} does not need review (worker {record["worker"]})')
        if head is not None and head != record['head']:
            raise Refused(f'unit {unit_id!r} head moved since review ({head[:12]} != {record["head"][:12]}); '
                          're-queue and review again')
        record['review'] = {'status': 'recorded', 'verdict': verdict, 'notes': notes, 'reviewer': reviewer,
                            'head': record['head'], 'recorded_at': _now()}
        _status(record)
        queue['updated_at'] = _now()
        uw._save(path, queue)
    return record


def queue_status(batch_id=None, policy=None):
    policy = policy or config.load_policy()
    if batch_id:
        queues = [load_queue(batch_id, policy)]
    else:
        directory = _path(policy, 'merge_queue_dir', 'logs/merge-queue')
        queues = []
        for path in sorted(directory.glob('*.json')) if directory.exists() else []:
            try:
                queues.append(json.loads(path.read_text()))
            except json.JSONDecodeError:
                continue
    return {'queues': [{'batch_id': q.get('batch_id'), 'repo': q.get('repo'), 'updated_at': q.get('updated_at'),
                        'entries': list(q.get('entries', {}).values())} for q in queues]}
