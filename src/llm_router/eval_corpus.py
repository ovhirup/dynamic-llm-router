"""Evaluation harness, part 1 (#62): capture a replay corpus from router job logs.

Each run-job writes <state_dir>/logs/jobs/<job_id>.json. A corpus case keeps what is needed to replay the
job against another worker or routing policy (repo, base SHA, task text, test command, size) plus the
recorded outcome as the baseline observation. The corpus is JSONL, redacted, and never holds raw output.
"""
import json
import re
from datetime import datetime
from pathlib import Path

from . import config, handoff, security

CORPUS_VERSION = 1
# diff --stat lines look like ` path/to/file.py | 12 +++--` (binary: `| Bin 0 -> 12 bytes`).
_STAT_LINE = re.compile(r'^\s*(.+?)\s+\|\s+(?:\d+|Bin\b)')


def jobs_dir(policy):
    return config.policy_path(policy, 'run_job', 'jobs_dir', 'logs/jobs')


def corpus_path(policy):
    return config.policy_path(policy, 'run_job', 'eval_corpus', 'logs/eval/corpus.jsonl')


def diff_paths(diff_stat):
    """Changed paths from a `git diff --stat` text (the summary line is skipped)."""
    paths = []
    for line in (diff_stat or '').splitlines():
        match = _STAT_LINE.match(line)
        if match:
            paths.append(match.group(1).strip())
    return paths


def _elapsed(job):
    """Wall time of the job (finished - started), else the sum of worker run times, else None."""
    try:
        start, end = datetime.fromisoformat(job['started_at']), datetime.fromisoformat(job['finished_at'])
        return max(0.0, (end - start).total_seconds())
    except (KeyError, TypeError, ValueError):
        pass
    runs = [run for attempt in job.get('attempts') or [] for run in attempt.get('runs') or []]
    times = [run['elapsed_seconds'] for run in runs if type(run.get('elapsed_seconds')) in (int, float)]
    return float(sum(times)) if times else None


def _tokens(job):
    """Total tokens if any worker run recorded them (`tokens` or `usage.total_tokens`), else None."""
    total, seen = 0, False
    for attempt in job.get('attempts') or []:
        for run in attempt.get('runs') or []:
            value = run.get('tokens', (run.get('usage') or {}).get('total_tokens'))
            if type(value) in (int, float):
                total, seen = total + value, True
    return total if seen else None


def _task_text(job):
    """The task text: recorded on the job, else the Goal of the job's first handoff note."""
    if job.get('task_text'):
        return job['task_text']
    for entry in job.get('handoffs') or []:
        try:
            goal = handoff.parse(Path(entry['path']).read_text()).get('sections', {}).get('Goal', '')
        except (OSError, KeyError, TypeError):
            continue
        if goal and goal != '(see task)':
            return goal
    return None


def case_from_job(job):
    """One corpus case from a job summary dict. `replayable` is False when the job cannot be re-run."""
    paths = diff_paths(job.get('diff_stat'))
    task_text = _task_text(job)
    attempts = [a for a in job.get('attempts') or [] if a.get('worker')]
    case = {
        'version': CORPUS_VERSION, 'case_id': job.get('job_id'), 'repo': job.get('repo'),
        'base_sha': job.get('base_sha'), 'task': job.get('task'), 'task_text': task_text,
        'test_command': job.get('test_command'), 'size': job.get('size') or 'full',
        'started_at': job.get('started_at'),
        'baseline': {
            'workers': list(job.get('workers') or []), 'status': job.get('status'), 'ok': bool(job.get('ok')),
            'tests_status': job.get('tests_status'), 'changed_paths': paths, 'attempts': len(attempts),
            'elapsed_seconds': _elapsed(job), 'tokens': _tokens(job),
            'quota_percent': job.get('quota_percent'), 'reviewer_score': job.get('reviewer_score'),
        },
    }
    missing = [key for key in ('repo', 'base_sha', 'task_text') if not case[key]]
    if job.get('status') in ('refused', 'error', 'no_tests'):
        missing.append(f'status {job.get("status")}')
    case['replayable'] = not missing
    case['not_replayable_because'] = missing
    return security.redact_obj(case)


def capture(policy=None, source=None, out=None, limit=None):
    """Read every job log under `source` (default: the jobs dir) and write the corpus JSONL to `out`."""
    policy = policy or config.load_policy()
    source = Path(source) if source else jobs_dir(policy)
    out = Path(out) if out else corpus_path(policy)
    files = sorted(source.glob('*.json')) if source.is_dir() else []
    cases, skipped = [], []
    for path in files:
        try:
            job = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            skipped.append({'file': path.name, 'why': type(error).__name__})
            continue
        if not isinstance(job, dict) or not job.get('job_id'):
            skipped.append({'file': path.name, 'why': 'not a job summary'})
            continue
        cases.append(case_from_job(job))
    cases.sort(key=lambda case: case.get('started_at') or '')
    if limit:
        cases = cases[-limit:]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(''.join(json.dumps(case, sort_keys=True) + '\n' for case in cases))
    out.chmod(0o600)
    return {'corpus': str(out), 'cases': len(cases), 'replayable': sum(c['replayable'] for c in cases),
            'skipped': skipped}


def load(path):
    """Cases from a corpus JSONL file (blank lines ignored)."""
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
