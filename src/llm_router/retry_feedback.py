"""Converging retry feedback (#103): EXPECTED / SAW / FENCE / DEAD ENDS and same-failure-twice stop.

Computes a normalized failure fingerprint from test output (failing test names + normalized
assertion/exception lines), builds structured retry feedback with four explicit sections:
  - EXPECTED (what the check required)
  - SAW (failing assertions and short log tail)
  - FENCE (files in scope / changed)
  - DEAD ENDS (approaches already tried across earlier attempts and handoff notes)
and stops retrying when two consecutive attempts produce the same failure fingerprint.
"""
import hashlib
import json
from pathlib import Path
import re

from . import security

_FAIL_TEST = re.compile(r'^(?:FAIL|ERROR|FAILED)\s*:?\s*(\S+)', re.M)
_ASSERT_LINE = re.compile(r'^\s*(?:AssertionError|[A-Za-z_.]*(?:Error|Exception))\b.*$', re.M)
_HEX_OR_PATH = re.compile(r'(?:/var/folders/\S+|/tmp/\S+|0x[0-9a-fA-F]+|\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*)')


def normalize_error(text):
    """Strip volatile paths, memory addresses, and timestamps so identical failures hash the same."""
    cleaned = _HEX_OR_PATH.sub('<norm>', text or '')
    cleaned = re.sub(r'\bline \d+\b', 'line <n>', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return security.redact(cleaned)


def failure_fingerprint(tests=None, attempt=None):
    """Deterministic fingerprint dict `{'id', 'failing_tests', 'error_line', 'status'}` for a failed attempt."""
    tests = tests or {}
    attempt = attempt or {}
    tail = tests.get('tail') or ''
    failing_tests = sorted(set(_FAIL_TEST.findall(tail)))
    assertions = [normalize_error(line) for line in _ASSERT_LINE.findall(tail)]
    if not assertions and attempt.get('runs'):
        last_run = attempt['runs'][-1]
        if last_run.get('failure_reason'):
            assertions = [normalize_error(last_run['failure_reason'])]
    if not assertions and tail.strip():
        last_non_empty = [line.strip() for line in tail.splitlines() if line.strip()][-1]
        assertions = [normalize_error(last_non_empty)]
    error_line = assertions[-1][:240] if assertions else (tests.get('status') or 'failed')
    status = tests.get('status') or ('failed' if not attempt.get('ok') else 'unknown')
    raw = json.dumps({'status': status, 'tests': failing_tests, 'error': error_line}, sort_keys=True)
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return {
        'id': digest,
        'status': status,
        'failing_tests': failing_tests,
        'error_line': error_line,
    }


def same_failure_twice(fingerprints):
    """True when the last two failure fingerprints have the same non-empty id."""
    if not isinstance(fingerprints, list) or len(fingerprints) < 2:
        return False
    prev, curr = fingerprints[-2], fingerprints[-1]
    prev_id = prev.get('id') if isinstance(prev, dict) else str(prev)
    curr_id = curr.get('id') if isinstance(curr, dict) else str(curr)
    return bool(prev_id and curr_id and prev_id == curr_id)


def build_retry_prompt(tests, attempt=None, fence_paths=(), dead_ends=(), task_goal=''):
    """Render converging retry feedback with EXPECTED, SAW, FENCE, and DEAD ENDS sections."""
    tests = tests or {}
    attempt = attempt or {}
    fp = failure_fingerprint(tests, attempt)
    cmd = tests.get('command') or 'configured test suite'
    expected_lines = [f'- Test command `{cmd}` exits 0 (`passed` with 0 skipped).']
    if task_goal:
        first_line = next((line.strip('# ').strip() for line in task_goal.splitlines() if line.strip()), '')
        if first_line:
            expected_lines.append(f'- Task goal: {security.redact(first_line)[:200]}')

    saw_lines = [f'- Status: `{tests.get("status") or "failed"}` (fingerprint `{fp["id"]}`)']
    if fp['failing_tests']:
        saw_lines.append('- Failing tests: ' + ', '.join(f'`{t}`' for t in fp['failing_tests']))
    if fp['error_line']:
        saw_lines.append(f'- Error: `{fp["error_line"]}`')
    if not attempt.get('ok', True):
        saw_lines.append('- The worker run failed or made no usable change.')
    tail = (tests.get('tail') or '').strip()
    if tail:
        saw_lines += ['```', security.redact(tail[-1500:]), '```']

    fence_list = [f'- `{p}`' for p in sorted(fence_paths or ())] or ['- (keep changes minimal and within task scope)']
    dead_list = [f'- {security.redact(str(item))}' for item in (dead_ends or ()) if str(item).strip()]
    if not dead_list:
        dead_list = ['- Initial attempt did not pass; do not repeat the same failing edit without fixing the assertion above.']

    sections = [
        '## Previous attempt did not pass',
        'Your earlier changes are still in this checkout. Fix them using the structured feedback below.',
        '',
        '### EXPECTED',
        *expected_lines,
        '',
        '### SAW',
        *saw_lines,
        '',
        '### FENCE',
        *fence_list,
        '',
        '### DEAD ENDS',
        *dead_list,
    ]
    return '\n'.join(sections)


def save_retry_state(directory, job_id, fingerprints, dead_ends):
    """Persist retry fingerprints and dead ends next to handoff notes (`<handoffs_dir>/<job_id>/retry_state.json`)."""
    target = Path(directory) / str(job_id)
    target.mkdir(parents=True, exist_ok=True)
    path = target / 'retry_state.json'
    payload = security.redact_obj({
        'job_id': str(job_id),
        'fingerprints': list(fingerprints),
        'dead_ends': list(dead_ends),
        'same_failure_twice': same_failure_twice(fingerprints),
    })
    path.write_text(json.dumps(payload, indent=2))
    return path
