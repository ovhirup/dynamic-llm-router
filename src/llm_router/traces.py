"""Trace engineering (#137): structured job traces, append-only event ledger, replay, and root-cause view.

Provides:
  1. OpenTelemetry-compatible span trees per job/wave (`<state>/logs/traces/<trace_id>.jsonl`, mode `0600`)
     with `trace_id`, `span_id`, `parent_span_id`, `kind` (`job` / `wave` / `unit` / `worker_attempt` /
     `test_run` / `judge` / `merge_queue`), `side_effect` (`read_only` | `mutating`), `inputs`, `outputs`,
     `status` (`ok` | `failed`), `worker`, `tokens`, and timing.
  2. Append-only event ledger (`<state>/logs/traces/events.jsonl`, mode `0600`) recording worker, model,
     effort, mode, prompt SHA-256, inputs, outputs, exit code, tests result, tokens, and timing.
  3. Safe replay (`replay_trace(policy, trace_id, until_span_id, live_runner)`): serves cached results
     for earlier spans (never re-running `mutating` steps for real) and executes only the target `until_span_id`
     live so prompt/routing fixes can be tested cheaply.
  4. Causal root-cause walk (`root_cause(spans)`): walks backward along `depends_on` / `parent_span_id`
     links from any failed span to find the earliest failing or defective upstream span.
  5. Strict secret & private-path redaction (`~/.codex`, `auth.json`, `.env`, vendor tokens) before writing.
"""
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from . import config, security

SPAN_KINDS = ('job', 'wave', 'unit', 'worker_attempt', 'tool', 'test_run', 'judge', 'merge_queue')
SIDE_EFFECTS = ('read_only', 'mutating')
PRIVATE_PATH_RE = re.compile(r'(?:~|/Users/[^/\s]+|/home/[^/\s]+)/\.codex(?:/\S*)?|auth\.json\b')


def traces_dir(policy=None):
    return config.policy_path(policy or {}, 'run_job', 'traces_dir', 'logs/traces')


def trace_path(policy, trace_id):
    safe = re.sub(r'[^A-Za-z0-9._-]+', '-', str(trace_id or 'trace'))
    return traces_dir(policy) / f'{safe}.jsonl'


def event_ledger_path(policy=None):
    return traces_dir(policy) / 'events.jsonl'


def _scrub(value):
    if isinstance(value, str):
        text = security.redact(value)
        return PRIVATE_PATH_RE.sub(security.REDACTED, text)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def new_span_id():
    return uuid.uuid4().hex[:16]


def record_span(policy, trace_id, name, kind='worker_attempt', span_id=None, parent_span_id=None,
                side_effect='read_only', status='ok', worker=None, inputs=None, outputs=None,
                depends_on=(), tokens=None, error=None, started_at=None, finished_at=None):
    """Append one scrubbed span to `<traces_dir>/<trace_id>.jsonl` (`0600`)."""
    if kind not in SPAN_KINDS:
        raise ValueError(f'unknown span kind {kind!r}')
    if side_effect not in SIDE_EFFECTS:
        raise ValueError(f'side_effect must be one of {SIDE_EFFECTS}')
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    span = _scrub({
        'trace_id': str(trace_id),
        'span_id': str(span_id or new_span_id()),
        'parent_span_id': str(parent_span_id) if parent_span_id else None,
        'name': str(name),
        'kind': kind,
        'side_effect': side_effect,
        'status': str(status),
        'worker': worker,
        'inputs': inputs or {},
        'outputs': outputs or {},
        'depends_on': list(depends_on or ()),
        'tokens': int(tokens) if type(tokens) in (int, float) else None,
        'error': str(error) if error else None,
        'started_at': str(started_at or now),
        'finished_at': str(finished_at or now),
    })
    path = trace_path(policy, trace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(span, sort_keys=True) + '\n')
    os.chmod(path, 0o600)
    return span


def append_event(policy, trace_id, worker, prompt='', model=None, effort=None, mode='headless',
                 inputs=None, outputs=None, exit_code=0, tests_status=None, tokens=None,
                 elapsed_seconds=None, now=None):
    """Append one immutable worker call event to `<traces_dir>/events.jsonl` (`0600`)."""
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    prompt_hash = hashlib.sha256(str(prompt or '').encode()).hexdigest()[:16]
    event = _scrub({
        'at': stamp,
        'trace_id': str(trace_id),
        'worker': str(worker),
        'model': model or str(worker),
        'effort': effort or 'medium',
        'mode': str(mode),
        'prompt_sha256': prompt_hash,
        'inputs': inputs or {},
        'outputs': outputs or {},
        'exit_code': int(exit_code) if exit_code is not None else None,
        'tests_status': tests_status,
        'tokens': int(tokens) if type(tokens) in (int, float) else None,
        'elapsed_seconds': float(elapsed_seconds) if type(elapsed_seconds) in (int, float) else None,
    })
    path = event_ledger_path(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(event, sort_keys=True) + '\n')
    os.chmod(path, 0o600)
    return event


def load_trace(policy, trace_id):
    """Return ordered list of spans for `trace_id`."""
    path = trace_path(policy, trace_id)
    if not path.is_file():
        return []
    spans = []
    for line in path.read_text(errors='replace').splitlines():
        if line.strip():
            try:
                spans.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return spans


def root_cause(spans):
    """Walk back from failed spans along `depends_on` and `parent_span_id` to find the root cause span."""
    by_id = {s['span_id']: s for s in (spans or ()) if isinstance(s, dict) and s.get('span_id')}
    failed = [s for s in (spans or ()) if isinstance(s, dict) and s.get('status') not in ('ok', 'passed')]
    if not failed:
        return {'status': 'healthy', 'root_cause_span': None, 'chain': []}

    leaf = failed[-1]
    chain = [leaf['span_id']]
    current = leaf
    visited = {leaf['span_id']}
    while True:
        upstream_ids = list(current.get('depends_on') or ())
        if current.get('parent_span_id'):
            upstream_ids.append(current['parent_span_id'])
        failing_upstreams = [
            by_id[uid] for uid in upstream_ids
            if uid in by_id and uid not in visited and by_id[uid].get('status') not in ('ok', 'passed')
        ]
        if not failing_upstreams:
            break
        nxt = failing_upstreams[0]
        visited.add(nxt['span_id'])
        chain.append(nxt['span_id'])
        current = nxt

    chain.reverse()
    cause = by_id[chain[0]]
    return {
        'status': 'failed',
        'root_cause_span': cause,
        'chain': chain,
        'explanation': f"Root cause at span {cause['span_id']} ({cause['name']}, kind={cause['kind']}): {cause.get('error') or cause.get('status')}",
    }


def replay_trace(policy, trace_id, until_span_id, live_runner=None):
    """Replay `trace_id` up to `until_span_id`: earlier spans (and mutating spans before target)
    serve cached outputs without re-executing; only `until_span_id` runs live via `live_runner(span)`.
    """
    spans = load_trace(policy, trace_id)
    if not spans:
        raise ValueError(f'no trace found for {trace_id!r}')
    steps = []
    found = False
    for span in spans:
        sid = span.get('span_id')
        if sid == until_span_id or span.get('name') == until_span_id:
            found = True
            live_out = live_runner(span) if live_runner is not None else span.get('outputs')
            steps.append({
                'span_id': sid,
                'name': span.get('name'),
                'action': 're_ran_live',
                'side_effect': span.get('side_effect'),
                'outputs': _scrub(live_out),
            })
            break
        steps.append({
            'span_id': sid,
            'name': span.get('name'),
            'action': 'cached_skip_mutating' if span.get('side_effect') == 'mutating' else 'cached_read_only',
            'side_effect': span.get('side_effect'),
            'outputs': span.get('outputs'),
        })
    if not found:
        raise ValueError(f'span {until_span_id!r} not found in trace {trace_id!r}')
    return {'trace_id': trace_id, 'until': until_span_id, 'steps': steps}
