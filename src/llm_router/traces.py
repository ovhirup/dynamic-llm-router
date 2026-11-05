"""Trace engineering (#137) and job phase traces (#236): structured job traces, append-only event ledger, replay, and root-cause view.

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
  6. Phase spans (`plan`, `dispatch`, `test`, `judge`, `fix`, `approval_wait`) carry worker, model,
     tokens, cost estimate, and exit status. Local JSONL is the default export.
  7. `report_traces` prints p50/p95 latency and cost per worker per task class.
  8. `scan_trace_files` runs the leak gate over every file in the traces directory.
"""
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from . import config, security

SPAN_KINDS = ('job', 'wave', 'unit', 'worker_attempt', 'tool', 'test_run', 'judge', 'merge_queue',
              'plan', 'dispatch', 'test', 'fix', 'approval_wait')
PHASE_NAMES = ('plan', 'dispatch', 'test', 'judge', 'fix', 'approval_wait')
OPEN_APPROVAL = ('pending', 'not_required')
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


def _metric(value):
    if type(value) in (int, float) and type(value) is not bool:
        return value
    return None


def record_span(policy, trace_id, name, kind='worker_attempt', span_id=None, parent_span_id=None,
                side_effect='read_only', status='ok', worker=None, inputs=None, outputs=None,
                depends_on=(), tokens=None, error=None, started_at=None, finished_at=None,
                model=None, cost_estimate=None, exit_status=None, task_class=None):
    """Append one scrubbed span to `<traces_dir>/<trace_id>.jsonl` (`0600`)."""
    if kind not in SPAN_KINDS:
        raise ValueError(f'unknown span kind {kind!r}')
    if side_effect not in SIDE_EFFECTS:
        raise ValueError(f'side_effect must be one of {SIDE_EFFECTS}')
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    cost = _metric(cost_estimate)
    span = _scrub({
        'trace_id': str(trace_id),
        'span_id': str(span_id or new_span_id()),
        'parent_span_id': str(parent_span_id) if parent_span_id else None,
        'name': str(name),
        'kind': kind,
        'side_effect': side_effect,
        'status': str(status),
        'worker': worker,
        'model': None if model is None else str(model),
        'inputs': inputs or {},
        'outputs': outputs or {},
        'depends_on': list(depends_on or ()),
        'tokens': int(tokens) if type(tokens) in (int, float) and type(tokens) is not bool else None,
        'cost_estimate': None if cost is None else float(cost),
        'exit_status': exit_status,
        'task_class': None if task_class is None else str(task_class),
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


def scan_trace_files(policy, terms=None, allow=(), max_bytes=1_000_000):
    """Run the leak gate (`security.scan_text`) over every file in the traces directory."""
    root = traces_dir(policy)
    findings, scanned = [], 0
    if root.is_dir():
        for path in sorted(p for p in root.rglob('*') if p.is_file() and not p.is_symlink()):
            try:
                if path.stat().st_size > max_bytes:
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if b'\0' in data[:8192]:
                continue
            scanned += 1
            rel = path.relative_to(root)
            for number, kind, masked in security.scan_text(data.decode('utf-8', 'replace'), terms, allow):
                findings.append({'file': f'{rel}:{number}', 'kind': kind, 'match': masked})
    return {'status': 'blocked' if findings else 'clean', 'findings': findings, 'files_scanned': scanned}


def _aware(value):
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def parse_since(text, now=None):
    """A relative window (`30m`, `12h`, `7d`) or an ISO date, as the start of the report window."""
    raw = str(text or '').strip()
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    match = re.fullmatch(r'(\d+)([mhd])', raw)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = {'m': timedelta(minutes=amount), 'h': timedelta(hours=amount), 'd': timedelta(days=amount)}[unit]
        return current - delta
    stamp = _aware(raw)
    if stamp is None:
        raise ValueError('use --since like 30m, 12h, 7d, or an ISO date')
    return stamp


def percentile(values, percent):
    """Linear percentile of a list of numbers. One sample is both p50 and p95."""
    ordered = sorted(value for value in values if type(value) in (int, float) and type(value) is not bool)
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (percent / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return float(ordered[low]) * (1.0 - weight) + float(ordered[high]) * weight


def _format_metric(value, digits):
    if value is None:
        return '-'
    return f'{value:.{digits}f}'


def iter_spans(policy):
    """Every span in the traces directory, skipping the append-only event ledger."""
    root = traces_dir(policy)
    if not root.is_dir():
        return
    for path in sorted(root.glob('*.jsonl')):
        if path.name == 'events.jsonl':
            continue
        try:
            text = path.read_text(errors='replace')
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                span = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(span, dict):
                yield span


def _in_report(span, cutoff):
    phase = span.get('name') if span.get('name') in PHASE_NAMES else span.get('kind')
    if phase not in PHASE_NAMES:
        return False
    if phase == 'approval_wait' and span.get('exit_status') in OPEN_APPROVAL:
        return False
    finished = _aware(span.get('finished_at')) or _aware(span.get('started_at'))
    started = _aware(span.get('started_at'))
    if finished is None or started is None or finished < cutoff:
        return False
    return (finished - started).total_seconds() >= 0


def report_traces(policy, since, now=None):
    """p50/p95 latency and cost per worker per task class. Approval wait is a separate phase."""
    cutoff = parse_since(since, now=now)
    groups = {}
    for span in iter_spans(policy):
        if not _in_report(span, cutoff):
            continue
        started = _aware(span.get('started_at'))
        finished = _aware(span.get('finished_at')) or started
        latency = (finished - started).total_seconds()
        worker = span.get('worker') or 'unknown'
        task_class = span.get('task_class') or 'unset'
        phase = span.get('name') if span.get('name') in PHASE_NAMES else span.get('kind')
        # Owner wait is its own phase so it does not inflate worker p95.
        group_phase = 'approval_wait' if phase == 'approval_wait' else ''
        bucket = groups.setdefault((str(worker), str(task_class), group_phase), {'latency': [], 'cost': []})
        bucket['latency'].append(latency)
        cost = _metric(span.get('cost_estimate'))
        if cost is not None:
            bucket['cost'].append(float(cost))
    rows = []
    for worker, task_class, phase in sorted(groups):
        bucket = groups[(worker, task_class, phase)]
        rows.append({
            'worker': worker,
            'task_class': task_class,
            'phase': phase,
            'p50_latency_s': _format_metric(percentile(bucket['latency'], 50), 3),
            'p95_latency_s': _format_metric(percentile(bucket['latency'], 95), 3),
            'p50_cost': _format_metric(percentile(bucket['cost'], 50), 6),
            'p95_cost': _format_metric(percentile(bucket['cost'], 95), 6),
        })
    lines = [f'p50/p95 latency and cost per worker per task class (since {cutoff.isoformat(timespec="seconds")})']
    if not rows:
        lines.append('no spans')
    for row in rows:
        phase = ' phase=approval_wait' if row['phase'] == 'approval_wait' else ''
        lines.append(
            f"worker={row['worker']} task_class={row['task_class']}{phase} "
            f"p50_latency_s={row['p50_latency_s']} p95_latency_s={row['p95_latency_s']} "
            f"p50_cost={row['p50_cost']} p95_cost={row['p95_cost']}")
    return {'since': cutoff.isoformat(timespec='seconds'), 'rows': rows, 'text': '\n'.join(lines) + '\n'}
