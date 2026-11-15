"""Live jobs feed for DLR Notch (#99): see every worker's running and recent jobs.

Scans `<state_dir>/logs/jobs/*.json`, `<state_dir>/worktrees/*` (in-flight jobs without a finished
JSON yet), and `<state_dir>/logs/task-output/*.log` to build a redacted, lightweight feed:
  - `job_id`, `repo`, `task`, `worker`, `workers`, `hops` (inline failover switches), `attempt`,
    `started_at`, `finished_at`, `elapsed_seconds`, `state` (`running` / `testing` / `committing` /
    `failed` / `done`), `branch`, `commit`, `log_path`, and `preview` (last 3 redacted output lines).
  - Splits jobs into `running`, `recent` (finished within 30 minutes), and `history`.
  - Never includes secrets or private terms in `preview` or metadata: every string goes through `clean_text`
    (`normalize_text`, then `mask_text`) before it is clipped or output.
  - A running job's `preview` comes from its newest `<stamp>-a<N>-<worker>.events.jsonl` live events file
    (#288) when one exists, as `"<kind>: <text>"` lines; otherwise from the output log as before.
  - A job's workers, hops and attempt come from `worker_chain` over its worker logs, events files and
    allocation sidecar. Worker names must match `_WORKER`; every file is read with `read_bounded`.
"""
from datetime import datetime, timezone
import functools
import json
import os
from pathlib import Path
import re
import stat
import unicodedata

from . import allocation_dispatch, config, security, task_dispatch as td, worker_events

RECENT_WINDOW_SECONDS = 30 * 60
STALE_IN_FLIGHT_SECONDS = 6 * 3600
TESTS_SETTLED_SECONDS = 15
PREVIEW_LINES = 3
TAIL_BYTES = 8192
CLAIM_BYTES = 4096
SIDECAR_BYTES = 256 * 1024
JOB_BYTES = 16 << 20

# Worker output is untrusted. One pipeline cleans every feed string, in this order: `normalize_text` removes
# escape sequences, control, format and surrogate characters and collapses whitespace; then `mask_text` masks
# secrets and private terms on the cleaned text, so no invisible character can hide a term from the mask.
_ESCAPES = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]?')
_IGNORABLE = re.compile('[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180d\u3164\ufe00-\ufe0f\uffa0\U000e0100-\U000e01ef]')


def normalize_text(text):
    """One safe line: escapes removed; Cc, Cf and Cs characters removed except whitespace; default-ignorable
    characters removed; whitespace runs collapsed. Idempotent."""
    text = _ESCAPES.sub('', '' if text is None else str(text))
    kept = ''.join(ch for ch in text if ch.isspace() or unicodedata.category(ch) not in ('Cc', 'Cf', 'Cs'))
    return ' '.join(_IGNORABLE.sub('', kept).split())


@functools.lru_cache(maxsize=256)
def _term_patterns(terms):
    """One case-insensitive pattern per private term; its words may be separated by any whitespace run."""
    patterns = []
    for term in terms:
        words = normalize_text(term).split()
        if words:
            patterns.append(re.compile(r'\s+'.join(re.escape(word) for word in words), re.IGNORECASE))
    return tuple(patterns)


def mask_text(text, terms=()):
    """`text` with secrets (`security.redact`) and every private term replaced by `security.REDACTED`."""
    text = security.redact(text)
    for pattern in _term_patterns(tuple(term for term in (terms or ()) if term)):
        text = pattern.sub(security.REDACTED, text)
    return text


def clean_text(text, terms=()):
    """The one cleaning pipeline for feed strings: `mask_text(normalize_text(text), terms)`."""
    return mask_text(normalize_text(text), terms)


# Dicts whose KEYS come from a sidecar (worker names) rather than from the feed's own schema.
_UNTRUSTED_KEYS = ('excluded', 'scores', 'reasons')
_MAX_DEPTH = 32


def clean_obj(value, terms=(), _keys=False, _depth=0):
    """`clean_text` on every string in `value`, inside lists and dict values; other values are unchanged.

    The feed's own keys are never rewritten (a private term equal to a field name must not break readers).
    Keys are cleaned only in the dicts directly under `excluded`, `scores` and `reasons`, whose keys come
    from a sidecar. Anything nested deeper than any real feed is replaced, so a crafted file cannot exhaust
    the stack."""
    if _depth > _MAX_DEPTH:
        return security.REDACTED
    if isinstance(value, str):
        return clean_text(value, terms)
    if isinstance(value, list):
        return [clean_obj(item, terms, False, _depth + 1) for item in value]
    if isinstance(value, dict):
        return {(clean_text(key, terms) if _keys and isinstance(key, str) else key):
                clean_obj(item, terms, key in _UNTRUSTED_KEYS, _depth + 1) for key, item in value.items()}
    return value


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def read_bounded(path, limit=TAIL_BYTES):
    """`(last <= limit bytes, truncated)` of a regular file, or None.

    Never follows a symlink and never blocks: a FIFO, device, directory or link at `path` gives None. At most
    `limit` bytes are read even if the file grows while it is being read. The output directory is the router's
    own 0700 state directory: only the final component is `O_NOFOLLOW`, a symlinked parent is out of scope, and
    content still passes `clean_text`."""
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        truncated = info.st_size > limit
        if truncated:
            os.lseek(fd, info.st_size - limit, os.SEEK_SET)
        chunks, remaining = [], limit
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b''.join(chunks), truncated
    except OSError:
        return None
    finally:
        os.close(fd)

# An output stamp is the job's start second, plus `-N` when another job claimed that second first (#288).
_STAMP = r'\d{8}-\d{6}(?:-\d+)?'
_ID_RE = re.compile(r'^(?P<repo>.+)-(?P<stamp>\d{8}-\d{6})(?:-\d+)?$')
_LOG_RE = re.compile(r'^(?P<stamp>' + _STAMP + r')-a(?P<attempt>\d+)-(?P<tail>.+)\.log$')
_JOB_STAMP_RE = re.compile(r'^(?P<stamp>' + _STAMP + r')-job\.json$')
_WORKER = r'[a-z][a-z0-9_-]{0,31}'
_WORKER_RE = re.compile(_WORKER)


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
        return datetime.strptime(str(stamp)[:15], '%Y%m%d-%H%M%S').replace(tzinfo=zone)
    except ValueError:
        return None


def parse_output_log_name(filename):
    """Parse `<YYYYMMDD-HHMMSS>[-N]-a<N>-<worker>.log` or `...-tests.log`."""
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
    got = read_bounded(Path(log_path))
    if got is None:
        return []
    raw_lines = got[0].decode('utf-8', 'replace').splitlines()
    if got[1] and raw_lines:
        raw_lines = raw_lines[1:]
    cleaned = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(('--- stdout', '--- stderr', '$ exit=')):
            continue
        line = clean_text(stripped, private_terms)[:200]
        if line:
            cleaned.append(line)
    return cleaned[-max(1, int(lines_count)):]


_EVENTS_RE = re.compile(r'^(?P<stamp>' + _STAMP + r')-a(?P<attempt>\d+)-(?P<worker>' + _WORKER + r')\.events\.jsonl$')


def _events_files(out_dir, stamp):
    """`(attempt, worker, path, mtime)` of each regular `<stamp>-a<N>-<worker>.events.jsonl`, by name."""
    files = []
    if not stamp or not Path(out_dir).is_dir():
        return files
    for path in sorted(Path(out_dir).glob(f'{stamp}-a*.events.jsonl')):
        match = _EVENTS_RE.match(path.name)
        if not match or match.group('stamp') != stamp:
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            files.append((int(match.group('attempt')), match.group('worker'), path, info.st_mtime))
    return files


def latest_events_file(out_dir, stamp):
    """Newest `<stamp>-a<N>-<worker>.events.jsonl` (#288 live events) for a job stamp, or None."""
    best = None
    for attempt, _worker, path, mtime in _events_files(out_dir, stamp):
        key = (attempt, mtime)
        if best is None or key > best[0]:
            best = (key, path)
    return best[1] if best else None


def live_preview(out_dir, stamp, private_terms=None, lines_count=PREVIEW_LINES):
    """`"<kind>: <text>"` lines from a running job's newest events file, or [] when it has none."""
    path = latest_events_file(out_dir, stamp)
    if not path:
        return []
    shown = [f'{record["kind"]}: {record["text"]}' for record in _event_records(path, private_terms or ())]
    return shown[-max(1, int(lines_count)):]


TIMELINE_EVENTS = 20
NOW_LIMIT = 120
_NOW_VERBS = {'command': 'running ', 'test': 'testing ', 'edit': 'editing ', 'message': 'says ',
              'error': 'error: ', 'raw': 'output: '}


def read_claims(out_dir):
    """`[(stamp, job_id or None)]` for every `<stamp>-job.json` claim `run_job` made (#288). A claim that cannot
    be read, is truncated, or names no job gives `None` as its job id; its stamp still counts as claimed."""
    claims = []
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return claims
    found = []
    for path in out_dir.glob('*-job.json'):
        match = _JOB_STAMP_RE.match(path.name)
        if match:
            stamp = match.group('stamp')
            found.append(((stamp[:15], int(stamp[16:] or 1)), stamp, path))
    for _, stamp, path in sorted(found):
        got = read_bounded(path, CLAIM_BYTES)
        job_id = None
        if got is not None and not got[1]:
            try:
                data = json.loads(got[0].decode('utf-8', 'replace'))
                job_id = data.get('job_id') if isinstance(data, dict) else None
            except (ValueError, RecursionError):
                job_id = None
        claims.append((stamp, job_id if isinstance(job_id, str) and job_id else None))
    return claims


def _claimed_stamps(claims):
    """`{job_id: stamp}` from one read of the claims. A job id named by two or more claims maps to None."""
    stamps = {}
    for stamp, job_id in claims:
        if job_id:
            stamps[job_id] = None if job_id in stamps else stamp
    return stamps


def job_output_stamps(out_dir):
    """`{job_id: output stamp}` from the `<stamp>-job.json` claims `run_job` makes (#288)."""
    return _claimed_stamps(read_claims(out_dir))


def _job_stamp(job_id, claimed, claimed_stamps, recorded=None):
    """The output stamp a job's files use, or '' when none can be named without guessing.

    A stamp recorded in the job's JSON wins; then the one claim that names the job; otherwise the id's own
    start stamp, unless some claim already uses that stamp (then another job's output would be shown)."""
    if recorded:
        return str(recorded)
    if job_id in claimed:
        return claimed[job_id] or ''
    match = _ID_RE.match(job_id)
    stamp = match.group('stamp') if match else ''
    return '' if stamp in claimed_stamps else stamp


def _event_records(path, private_terms=(), tail_bytes=TAIL_BYTES):
    """Events from the last `tail_bytes` of one events file, re-redacted."""
    got = read_bounded(path, tail_bytes)
    if got is None:
        return []
    lines = got[0].decode('utf-8', 'replace').splitlines()
    if got[1] and lines:
        lines = lines[1:]
    records = []
    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict) and event.get('kind') in worker_events.KINDS:
            text = _clip(clean_text(event.get('text', ''), private_terms), worker_events.TEXT_LIMIT)
            records.append({'ts': str(event.get('ts') or ''), 'kind': event['kind'], 'text': text})
    return records


def job_events(out_dir, stamp, private_terms=(), limit=TIMELINE_EVENTS):
    """A job's last `limit` live events, oldest first, across every attempt and failover worker."""
    collected = []
    for order, (attempt, worker, path, _mtime) in enumerate(_events_files(out_dir, stamp)):
        for index, record in enumerate(_event_records(path, private_terms)):
            collected.append(((attempt, record['ts'], order, index),
                              {'ts': record['ts'], 'worker': worker, 'attempt': attempt,
                               'kind': record['kind'], 'text': record['text']}))
    collected.sort(key=lambda item: item[0])
    return [event for _, event in collected][-max(1, int(limit)):]


def now_line(events, state):
    """One line on what a running job is doing now, from its newest event, or None."""
    if state == 'testing':
        return 'running tests'
    if not events:
        return None
    last = events[-1]
    text = 'finished' if last['kind'] == 'done' else _NOW_VERBS.get(last['kind'], 'output: ') + last['text']
    return _clip(text, NOW_LIMIT)


def _gate(state, detail='', **extra):
    return {'state': state, 'detail': detail, **extra}


def running_gates(state):
    """Gates of a job still in flight. Only tests are visible live; the rest land with the job's result."""
    later = 'when the job ends'
    if state == 'testing':
        tests = _gate('running', 'tests running')
    elif state == 'committing':
        tests = _gate('pending', 'tests finished; result ' + later)
    else:
        tests = _gate('pending', 'after the worker finishes')
    return {'tests': tests, 'leak_check': _gate('pending', later), 'review': _gate('pending', later),
            'approval': _gate('pending', later)}


def finished_gates(data, policy, approvals, now=None):
    """Gates of a finished job, read-only from its job JSON and the existing pr_gate state."""
    from . import pr_gate
    tests_status = data.get('tests_status')
    tests = {'passed': _gate('passed', 'tests passed'), 'failed': _gate('failed', 'tests failed'),
             'timeout': _gate('failed', 'tests timed out'), 'no_tests': _gate('n/a', 'no tests')
             }.get(tests_status, _gate('n/a', 'not run'))
    leak = data.get('leak_check') if isinstance(data.get('leak_check'), dict) else {}
    if data.get('blocked_by_leak_check') or (leak.get('status') and leak.get('status') != 'clean'):
        leak_gate = _gate('blocked', f'leak check: {leak.get("status") or "blocked"}')
    elif leak.get('status') == 'clean':
        leak_gate = _gate('passed', 'leak check clean')
    else:
        leak_gate = _gate('n/a', 'not run')
    review = data.get('review') if isinstance(data.get('review'), dict) else {}
    reviewer = review.get('reviewer') or 'reviewer'
    if data.get('review_required') is not True:
        review_gate = _gate('n/a', 'not required')
    elif review.get('verdict') == 'approve':
        review_gate = _gate('passed', f'approved by {reviewer}')
    elif review.get('verdict') == 'reject':
        review_gate = _gate('failed', f'rejected by {reviewer}')
    elif review.get('status') == 'error':
        review_gate = _gate('failed', f'review error: {review.get("reason") or "unknown"}')
    else:
        review_gate = _gate('waiting', review.get('next') or f'waiting for {reviewer}')
    commit = data.get('commit')
    if not commit or not (data.get('ok') or data.get('status') == 'committed'):
        approval = _gate('n/a', 'no commit')
    elif data.get('review_required') is True and review.get('verdict') != 'approve':
        approval = _gate('blocked', 'stronger-model review must approve first')
    else:
        job_id = str(data.get('job_id'))
        gate = pr_gate.load_gate(policy, job_id)
        sha = (gate.get('review') or {}).get('sha') or commit
        if (gate.get('merged') or {}).get('sha') == sha:
            approval = _gate('passed', f'merged {sha}', sha=sha)
        else:
            action = 'merge' if (gate.get('pr') or {}).get('pushed_sha') == sha else 'push'
            status = pr_gate.approval_status(policy, job_id, action, sha, now=now, records=approvals)
            if status['ok']:
                approval = _gate('passed', f'{action} approved for {sha}', sha=sha, action=action)
            else:
                approval = _gate('waiting', f'{action} approval needed for {sha}', sha=sha, action=action,
                                 reason=status['why'],
                                 command=f'llm-router approve {job_id} --action {action} --sha {sha}')
    return {'tests': tests, 'leak_check': leak_gate, 'review': review_gate, 'approval': approval}


def _scan_output_logs(out_dir):
    """`{stamp: [(parsed name, path, mtime)]}` for the regular `*.log` files in `out_dir`."""
    by_stamp = {}
    if not out_dir.is_dir():
        return by_stamp
    for path in sorted(out_dir.glob('*.log')):
        parsed = parse_output_log_name(path.name)
        if not parsed:
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            by_stamp.setdefault(parsed['stamp'], []).append((parsed, path, info.st_mtime))
    return by_stamp


def _valid_worker(name):
    return name if isinstance(name, str) and _WORKER_RE.fullmatch(name) else None


def worker_chain(observations):
    """`{workers, worker, hops, attempt}` from `(attempt, worker or None, has_log, mtime)` observations.

    Pairs are `(attempt, worker)`, each with `has_log` (any worker log) and the newest mtime. Pairs sort by
    attempt, then logged before unlogged, then mtime, then name. Consecutive duplicates collapse, and each
    collapse boundary is a hop. The attempt is the highest over every observation, worker or not."""
    pairs = {}
    for attempt, worker, has_log, mtime in observations:
        if worker is None:
            continue
        logged, newest = pairs.get((attempt, worker), (False, mtime))
        pairs[(attempt, worker)] = (logged or has_log, max(newest, mtime))
    ordered = sorted(pairs.items(), key=lambda item: (item[0][0], not item[1][0], item[1][1], item[0][1]))
    workers, hops, prev = [], [], None
    for (attempt, worker), _ in ordered:
        if worker == prev:
            continue
        if prev is not None:
            hops.append({'from': prev, 'to': worker, 'attempt': attempt})
        workers.append(worker)
        prev = worker
    return {'workers': workers, 'worker': workers[-1] if workers else None, 'hops': hops,
            'attempt': max((observation[0] for observation in observations), default=0)}


def _worker_info(log_entries, events_files, sidecar):
    """A job's workers, hops, attempt and newest logs, from its output files (see `worker_chain`)."""
    fields, sidecar_attempt, sidecar_mtime = sidecar
    observations = []
    for meta, _path, mtime in log_entries:
        worker = None if meta['is_tests'] else _valid_worker(meta['worker'])
        if not meta['is_tests'] and worker is None:
            continue  # a log whose name fails the worker pattern is not a worker file
        observations.append((meta['attempt'], worker, True, mtime))
    for attempt, worker, _path, mtime in events_files:
        observations.append((attempt, worker, False, mtime))
    if fields:
        worker = _valid_worker((fields.get('allocation') or {}).get('worker'))
        observations.append((sidecar_attempt, worker, False, sidecar_mtime))
    info = worker_chain(observations)
    attempt = info['attempt']
    worker_logs = sorted((item for item in log_entries if not item[0]['is_tests'] and
                          _valid_worker(item[0]['worker'])), key=lambda item: item[0]['attempt'])
    tests_logs = [path for meta, path, _mtime in log_entries if meta['is_tests'] and meta['attempt'] == attempt]
    return {
        'workers': info['workers'],
        'worker': info['worker'],
        'hops': info['hops'],
        'attempt': attempt,
        'latest_log': str(worker_logs[-1][1]) if worker_logs else None,
        'latest_tests': str(tests_logs[-1]) if tests_logs else None,
    }


def _read_sidecar(out_dir, stamp):
    """`(fields, attempt, mtime)` of the newest regular `<stamp>-a<N>-allocation.json`, or `({}, 0, 0.0)`.

    Unreadable, truncated, oversized or malformed sidecars count as absent."""
    absent = ({}, 0, 0.0)
    if not stamp or not Path(out_dir).is_dir():
        return absent
    pattern = re.compile(r'^' + re.escape(stamp) + r'-a(\d+)-allocation\.json$')
    best = None
    for path in Path(out_dir).glob(f'{stamp}-a*-allocation.json'):
        match = pattern.match(path.name)
        if not match:
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and (best is None or int(match.group(1)) > best[0]):
            best = (int(match.group(1)), path, info.st_mtime)
    if best is None:
        return absent
    attempt, path, mtime = best
    got = read_bounded(path, SIDECAR_BYTES)
    if got is None or got[1]:
        return absent
    try:
        fields = allocation_dispatch.allocation_fields(json.loads(got[0].decode('utf-8', 'replace')))
    except (ValueError, TypeError, RecursionError):  # deeply nested junk must not stop the feed
        return absent
    return (fields, attempt, mtime) if fields else absent


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
    claims = read_claims(out_dir)  # read once: the claim map and the set of claimed stamps come from it
    claimed, claimed_stamps = _claimed_stamps(claims), {stamp for stamp, _ in claims}
    from . import pr_gate
    approvals = pr_gate.approval_records(policy)  # read once per build, not once per job

    all_jobs = []
    seen_ids = set()

    for path in sorted(jobs_dir.glob('*.json')) if jobs_dir.is_dir() else ():
        got = read_bounded(path, JOB_BYTES)
        if got is None or got[1]:
            continue
        try:
            data = json.loads(got[0].decode('utf-8', 'replace'))
        except (ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        jid = str(data.get('job_id') or path.stem)
        seen_ids.add(jid)
        match = _ID_RE.match(jid)
        stamp = _job_stamp(jid, claimed, claimed_stamps, recorded=data.get('output_stamp'))
        sidecar = _read_sidecar(out_dir, stamp)
        info = _worker_info(logs_by_stamp.get(stamp, ()), _events_files(out_dir, stamp), sidecar)
        workers = list(data.get('workers') or info['workers'])
        worker = workers[-1] if workers else info['worker']
        started_dt = _parse_ts(data.get('started_at')) or (_stamp_to_dt(match.group('stamp'), zone) if match
                                                           else None)
        finished_dt = _parse_ts(data.get('finished_at'))
        elapsed = round((finished_dt - started_dt).total_seconds(), 1) if (started_dt and finished_dt) else None
        ok = bool(data.get('ok'))
        status = data.get('status') or ('committed' if ok else 'failed')
        state = 'done' if (ok or status == 'committed') else 'failed'
        log_path = info['latest_log'] or str(path)
        hops = data.get('hops') or info['hops']
        allocation = allocation_dispatch.allocation_fields(data) or sidecar[0]
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
            'now': None,
            'gates': finished_gates({**data, 'job_id': jid}, policy, approvals, now=now_dt),
            '_stamp': stamp,
            '_sort_ts': (finished_dt or started_dt or datetime.min.replace(tzinfo=timezone.utc)).timestamp(),
        })

    for wt in sorted(wt_root.glob('*')) if wt_root.is_dir() else ():
        if not wt.is_dir() or wt.name in seen_ids:
            continue
        match = _ID_RE.match(wt.name)
        if not match:
            continue
        repo_name, stamp = match.group('repo'), _job_stamp(wt.name, claimed, claimed_stamps)
        sidecar = _read_sidecar(out_dir, stamp)
        info = _worker_info(logs_by_stamp.get(stamp, ()), _events_files(out_dir, stamp), sidecar)
        started_dt = _stamp_to_dt(match.group('stamp'), zone)
        state = 'running'
        if info['latest_tests']:
            try:
                tests_mtime = datetime.fromtimestamp(Path(info['latest_tests']).stat().st_mtime, tz=zone)
                state = 'committing' if (now_dt - tests_mtime).total_seconds() > TESTS_SETTLED_SECONDS else 'testing'
            except OSError:
                state = 'testing'
        elapsed = round(max(0.0, (now_dt - started_dt).total_seconds()), 1) if started_dt else None
        all_jobs.append({
            **sidecar[0],
            'job_id': wt.name,
            'repo': repo_name,
            'task': wt.name,
            'worker': info['worker'],
            'workers': info['workers'],
            'hops': info['hops'],
            'attempt': max(1, info['attempt']),
            'started_at': started_dt.isoformat(timespec='seconds') if started_dt else None,
            'finished_at': None,
            'elapsed_seconds': elapsed,
            'state': state,
            'status': state,
            'ok': None,
            'branch': None,
            'commit': None,
            'log_path': info['latest_log'],
            'preview': (live_preview(out_dir, stamp, private_terms=terms) or
                        tail_preview(info['latest_log'], private_terms=terms)),
            'gates': running_gates(state),
            '_stamp': stamp,
            '_sort_ts': (started_dt or now_dt).timestamp(),
        })

    running = [j for j in all_jobs if j['state'] in ('running', 'testing', 'committing')]
    finished = [j for j in all_jobs if j['state'] in ('done', 'failed')]
    running.sort(key=lambda j: -j['_sort_ts'])
    finished.sort(key=lambda j: -j['_sort_ts'])

    now_ts = now_dt.timestamp()
    recent = [j for j in finished if (now_ts - j['_sort_ts']) <= RECENT_WINDOW_SECONDS]
    history = [j for j in finished if (now_ts - j['_sort_ts']) > RECENT_WINDOW_SECONDS]

    for item in running + recent:  # history stays small: no timeline
        item['events'] = job_events(out_dir, item['_stamp'], terms)
    for item in running:
        item['now'] = now_line(item['events'], item['state'])
    for item in all_jobs:
        item.pop('_sort_ts', None)
        item.pop('_stamp', None)

    return clean_obj({
        'updated_at': now_dt.isoformat(timespec='seconds'),
        'running': running,
        'recent': recent,
        'history': history,
    }, terms)
