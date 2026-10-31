"""Worker-agnostic handoff notes (#41-#45): every worker leaves a note before the job moves on.

A note is markdown with a front-matter-ish header, stored at
<state_dir>/<run_job.handoffs_dir>/<job_id>/<UTC-ts>-<worker>.md, never inside the worktree being
committed. Workers write NOTE_FILE at the worktree root; the router collects (and removes) it, validates
it against the diff, and synthesises a `source: router` note from git state when it is missing or invalid.
The Claude-only prompt helpers stay in claude_handoff and are re-exported here.
"""
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess

from . import claude_handoff, config, security
from .claude_handoff import build_prompt, clean, continuation_limit, effort, inspect  # noqa: F401

NOTE_FILE = '.llm-router-handoff.md'
SECTIONS = ('Goal', 'Files touched', 'Diff summary', 'Test status', 'Decisions', 'Next steps',
            'Gotchas', 'Meter', 'Switch reason')
REQUIRED = ('Goal', 'Files touched', 'Test status', 'Next steps', 'Switch reason')
HEADER_KEYS = ('job_id', 'worker', 'created_at', 'source', 'switch_reason', 'branch', 'worktree')
DEFAULT_THRESHOLD = 85
TAIL_CHARS = 2000

WORKER_INSTRUCTIONS = (
    f'Handoff note: at each checkpoint (after every commit-worthy step) and when you stop, write or update '
    f'`{NOTE_FILE}` at the checkout root (the router collects it; it is never committed). Use these '
    'markdown sections: ' + ', '.join(f'## {name}' for name in SECTIONS if name not in ('Meter', 'Switch reason')) +
    '. List one changed path per line under Files touched. Another worker may continue from it.')

_SECTION = re.compile(r'^## (.+?)\s*$', re.M)


def _git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, timeout=120)


def job_dir(policy, job_id):
    return config.policy_path(policy, 'run_job', 'handoffs_dir', 'handoffs') / job_id


def threshold(policy):
    return (policy.get('run_job') or {}).get('handoff_threshold_percent', DEFAULT_THRESHOLD)


def threshold_crossed(policy, percent):
    return type(percent) in (int, float) and percent >= threshold(policy)


def meter_percent(worker):
    """The worker's most-used window from its cached meter (never a live read), or None."""
    from . import quota_service
    from .observers import common
    observation = common.load_cache(quota_service.cache_path(worker))
    return quota_service.peak(quota_service.evidence_of(observation))[0]


# ---- schema -------------------------------------------------------------------------

def render(note):
    lines = ['---']
    lines += [f'{key}: {note[key]}' for key in HEADER_KEYS if note.get(key) not in (None, '')]
    lines += ['---', '']
    for name in SECTIONS:
        lines += [f'## {name}', (note.get('sections', {}).get(name) or '').strip(), '']
    return '\n'.join(lines)


def parse(text):
    note: dict = {'sections': {}}
    body = text or ''
    if body.startswith('---\n'):
        head, sep, rest = body[4:].partition('\n---')
        if sep:
            for line in head.splitlines():
                key, colon, value = line.partition(':')
                if colon and key.strip() in HEADER_KEYS:
                    note[key.strip()] = value.strip()
            body = rest.split('\n', 1)[1] if '\n' in rest else ''
    matches = list(_SECTION.finditer(body))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        note['sections'][match.group(1).strip()] = body[match.end():end].strip()
    return note


def files_listed(note):
    paths = []
    for line in (note.get('sections', {}).get('Files touched') or '').splitlines():
        line = line.strip().lstrip('-*').strip().strip('`').strip()
        if line and line.lower() not in ('none', '(none)'):
            paths.append(line)
    return paths


def validate_note(note, diff_paths):
    """[] when every required section is non-empty and every listed file is in the diff."""
    if not note:
        return ['note missing']
    sections = note.get('sections') or {}
    errors = [f'section {name!r} missing or empty' for name in REQUIRED if not (sections.get(name) or '').strip()]
    diff_paths = set(diff_paths)
    errors += [f'file {path!r} not in the diff' for path in files_listed(note) if path not in diff_paths]
    return errors


# ---- storage ------------------------------------------------------------------------

def write_note(directory, note, now=None):
    now = now or datetime.now(timezone.utc)
    note = dict(note, created_at=note.get('created_at') or now.isoformat(timespec='seconds'))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    worker = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(note.get('worker') or 'unknown'))
    base = f'{now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")}-{worker}'
    path, n = directory / f'{base}.md', 1
    while path.exists():
        n += 1
        path = directory / f'{base}-{n}.md'
    path.write_text(security.redact(render(note)))
    path.chmod(0o600)
    return path


def read_latest(directory):
    notes = sorted(Path(directory).glob('*.md')) if Path(directory).is_dir() else []
    if not notes:
        return None
    note = parse(notes[-1].read_text())
    note['path'] = str(notes[-1])
    return note


def render_for_prompt(note):
    if not note:
        return ''
    who = note.get('worker') or 'the previous worker'
    return (f'Handoff: you continue a job started by {who} in this same worktree and branch '
            f'(switch reason: {note.get("switch_reason") or "unknown"}; note source: {note.get("source") or "worker"}). '
            'Do not start over; build on the existing changes. Latest handoff note:\n\n' + render(note).strip())


# ---- router side --------------------------------------------------------------------

def collect_worker_note(worktree):
    """Read and remove the worker's NOTE_FILE so it never lands in the commit; None when absent."""
    path = Path(worktree) / NOTE_FILE
    if path.is_symlink():  # never follow a link out of the checkout
        path.unlink()
        return None
    if not path.is_file():
        return None
    text = path.read_text(errors='replace')
    path.unlink()
    return parse(text)


def _tail(path, chars=TAIL_CHARS):
    try:
        return Path(path).read_text(errors='replace')[-chars:].strip() if path else ''
    except OSError:
        return ''


def synthesise(worktree, base_sha, worker, switch_reason, diff_paths, goal='', test_status=None,
               test_log=None, meter=None):
    """A router-written note from git state, for a missing or invalid worker note."""
    stat = _git(worktree, 'diff', '--stat', base_sha).stdout.strip() if base_sha else ''
    commits = _git(worktree, 'log', '--oneline', '-5', f'{base_sha}..HEAD').stdout.strip() if base_sha else ''
    tail = _tail(test_log)
    tests = (test_status or 'not_run') + (f'\nLast failure (log tail):\n{tail}' if tail else '')
    return {'worker': worker, 'source': 'router', 'switch_reason': switch_reason,
            'sections': {'Goal': (goal or '').strip() or '(see task)',
                         'Files touched': '\n'.join(sorted(diff_paths)) or 'none',
                         'Diff summary': stat or 'no changes',
                         'Test status': tests,
                         'Decisions': 'Recent commits:\n' + commits if commits else 'none recorded (router note)',
                         'Next steps': 'Continue the task from the current worktree state; make the tests pass.',
                         'Gotchas': 'The previous worker left no valid handoff note; this one was written by the router.',
                         'Meter': f'{meter:g}%' if type(meter) in (int, float) else 'unknown',
                         'Switch reason': switch_reason}}


def checkpoint(policy, job_id, worktree, base_sha, worker, switch_reason, diff_paths, worker_note=None,
               goal='', test_status=None, test_log=None, meter=None, branch=None, now=None):
    """Validate the worker's collected note (else synthesise one), stamp router facts, write it.

    Returns (note, path). The worker's note is copied, never mutated."""
    note = parse(render(worker_note)) if worker_note else None
    errors = validate_note(note, diff_paths)
    if errors or note is None:
        note = synthesise(worktree, base_sha, worker, switch_reason, diff_paths, goal, test_status,
                          test_log, meter)
        note['sections']['Gotchas'] += '\nWorker note rejected: ' + '; '.join(errors)
    else:
        note['source'] = 'worker'
        sections = note['sections']
        sections['Switch reason'] = switch_reason
        sections.setdefault('Meter', f'{meter:g}%' if type(meter) in (int, float) else 'unknown')
        if not (sections.get('Diff summary') or '').strip() and base_sha:
            sections['Diff summary'] = _git(worktree, 'diff', '--stat', base_sha).stdout.strip() or 'no changes'
    note.update(job_id=job_id, worker=worker, switch_reason=switch_reason, branch=branch,
                worktree=str(worktree))
    path = write_note(job_dir(policy, job_id), note, now)
    note['path'] = str(path)
    return note, path


__all__ = ['NOTE_FILE', 'SECTIONS', 'REQUIRED', 'WORKER_INSTRUCTIONS', 'job_dir', 'threshold',
           'threshold_crossed', 'meter_percent', 'render', 'parse', 'validate_note', 'write_note',
           'read_latest', 'render_for_prompt', 'collect_worker_note', 'synthesise', 'checkpoint',
           'build_prompt', 'clean', 'effort', 'inspect', 'continuation_limit', 'claude_handoff']
