"""Outcome feedback loop: finished jobs and merge-queue reviews update per-worker, per-class scorecards.

One file per worker at <scorecards_dir>/<worker>.json (default: <state>/scorecards). Top-level fields are the
worker's totals across all classes; `classes.<task_class>` holds the same fields for that class alone.
See docs/ROUTER_BRAIN.md section (g).
"""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
import re
import tempfile

from . import config

DEFAULT_CLASS = 'feature'
VERDICTS = ('approve', 'reject')


def scorecards_dir(policy):
    return config.policy_path(policy, 'feedback', 'scorecards_dir', 'scorecards')


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _safe_name(worker):
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', str(worker)).strip('.-')
    return name or 'unknown'


def _empty():
    return {'runs': 0, 'passed': 0, 'pass_rate': 0.0, 'tokens_total': 0, 'tokens_mean': 0.0,
            'elapsed_seconds_total': 0.0, 'elapsed_seconds_mean': 0.0,
            'reviews': 0, 'review_approvals': 0, 'review_rejections': 0, 'updated_at': None}


@contextmanager
def _locked(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / '.lock', 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _load(path, worker):
    try:
        card = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        card = {}
    if not isinstance(card, dict):
        card = {}
    merged = {**_empty(), **{k: v for k, v in card.items() if k != 'classes'}}
    merged['worker'] = worker
    merged['classes'] = {name: {**_empty(), **stats} for name, stats in (card.get('classes') or {}).items()
                         if isinstance(stats, dict)}
    return merged


def _save(path, card):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix='.tmp')
    with os.fdopen(fd, 'w') as handle:
        json.dump(card, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _apply_run(stats, passed, tokens, elapsed, rejections, stamp):
    stats['runs'] += 1
    stats['passed'] += 1 if passed else 0
    stats['pass_rate'] = round(stats['passed'] / stats['runs'], 4)
    stats['tokens_total'] += int(tokens or 0)
    stats['tokens_mean'] = round(stats['tokens_total'] / stats['runs'], 2)
    stats['elapsed_seconds_total'] = round(stats['elapsed_seconds_total'] + float(elapsed or 0), 1)
    stats['elapsed_seconds_mean'] = round(stats['elapsed_seconds_total'] / stats['runs'], 2)
    stats['review_rejections'] += int(rejections or 0)
    stats['updated_at'] = stamp


def _apply_review(stats, verdict, stamp):
    stats['reviews'] += 1
    key = 'review_approvals' if verdict == 'approve' else 'review_rejections'
    stats[key] += 1
    stats['updated_at'] = stamp


def _update(policy, worker, task_class, apply):
    directory = scorecards_dir(policy)
    path = directory / f'{_safe_name(worker)}.json'
    stamp = _now()
    with _locked(directory):
        card = _load(path, worker)
        apply(card, stamp)
        apply(card['classes'].setdefault(task_class, _empty()), stamp)
        card['updated_at'] = stamp
        _save(path, card)
    return card


def _elapsed(summary):
    try:
        start = datetime.fromisoformat(summary['started_at'])
        end = datetime.fromisoformat(summary['finished_at'])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return max((end - start).total_seconds(), 0.0)


def _run_tokens(run):
    for key in ('tokens', 'tokens_total', 'total_tokens'):
        if isinstance(run.get(key), (int, float)):
            return run[key]
    usage = run.get('usage') or {}
    return usage.get('total_tokens') or (usage.get('input_tokens') or 0) + (usage.get('output_tokens') or 0)


def _job_outcomes(summary):
    """[(worker, passed, tokens, elapsed_seconds)]: the last worker carries the job's outcome; workers it failed
    over from count as failed runs unless every one of their hops was quota/auth unavailability."""
    workers = [w for w in summary.get('workers') or [] if w]
    if not workers:
        return []
    hops = summary.get('hops') or []
    tokens, elapsed, available = {}, {}, {}
    for attempt in summary.get('attempts') or []:
        for run in attempt.get('runs') or []:
            if run.get('worker'):
                tokens[run['worker']] = tokens.get(run['worker'], 0) + (_run_tokens(run) or 0)
    for hop in hops:
        worker = hop.get('worker')
        elapsed[worker] = elapsed.get(worker, 0.0) + float(hop.get('elapsed_seconds') or 0)
        available[worker] = available.get(worker, False) or not hop.get('unavailable')
    final = workers[-1]
    if not tokens.get(final) and isinstance(summary.get('tokens'), (int, float)):
        tokens[final] = summary['tokens']
    if final not in elapsed:
        elapsed[final] = _elapsed(summary)
    out = []
    for worker in workers:
        if worker != final and worker in available and not available[worker]:
            continue
        passed = bool(summary.get('ok')) if worker == final else False
        out.append((worker, passed, tokens.get(worker, 0), elapsed.get(worker, 0.0)))
    return out


def record_job_feedback(policy, summary, task_class=None):
    """Fold a finished run_job summary into its workers' scorecards; returns {worker: scorecard}."""
    policy = policy or config.load_policy()
    cls = summary.get('task_class') or task_class or DEFAULT_CLASS
    rejections = summary.get('review_rejections') or 0
    final = (summary.get('workers') or [None])[-1]
    updated = {}
    for worker, passed, tokens, elapsed in _job_outcomes(summary):
        rejected = rejections if worker == final else 0
        updated[worker] = _update(policy, worker, cls,
                                  lambda stats, stamp: _apply_run(stats, passed, tokens, elapsed, rejected, stamp))
    return updated


def record_review_feedback(policy, entry):
    """Fold a recorded merge-queue review verdict into the reviewed worker's scorecard; returns it (or None)."""
    policy = policy or config.load_policy()
    worker = entry.get('worker')
    verdict = (entry.get('review') or {}).get('verdict')
    if not worker or verdict not in VERDICTS:
        return None
    cls = entry.get('task_class') or entry.get('class') or DEFAULT_CLASS
    return _update(policy, worker, cls, lambda stats, stamp: _apply_review(stats, verdict, stamp))


def load_scorecards(policy=None):
    """{worker: scorecard} for every worker with a recorded scorecard."""
    policy = policy or config.load_policy()
    directory = scorecards_dir(policy)
    cards = {}
    for path in sorted(directory.glob('*.json')) if directory.exists() else []:
        try:
            card = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(card, dict):
            cards[card.get('worker') or path.stem] = card
    return cards
