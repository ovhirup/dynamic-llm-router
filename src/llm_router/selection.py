"""Worker selection: walk worker_order, read each meter (fresh cache first), pick the first eligible.

Rules (same safety model for every worker):
- A metered worker is eligible only with fresh evidence whose every required window is below its
  usage_threshold_percent. Unknown, stale, malformed or future-dated evidence means ineligible.
- consult="fallback" workers are read only when no earlier worker is eligible (slow screen reads).
- Workers in `never` are never candidates. Unmetered workers are eligible only when listed in
  unmetered_workers. `developer` is eligible only for draft-size tasks when enabled with an endpoint.
"""
from datetime import datetime, timezone
import math
import re

from . import claude_account, config, quota_service

NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}')


class SelectionError(ValueError):
    pass


def _timestamp(value):
    if type(value) is not str or len(value) > 40:
        raise SelectionError('bad timestamp')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, OverflowError):
        raise SelectionError('bad timestamp') from None
    if result.tzinfo is None:
        raise SelectionError('naive timestamp')
    return result


def validate_evidence(worker, record):
    """Raise SelectionError unless record is well-formed evidence for this worker."""
    if type(record) is not dict or set(record) != {'source_worker', 'required_windows', 'windows'}:
        raise SelectionError('bad evidence shape')
    if record['source_worker'] != worker:
        raise SelectionError('evidence is for another worker')
    required, windows = record['required_windows'], record['windows']
    if type(required) is not list or len(required) > 8 or len(set(required)) != len(required) or \
            not all(type(n) is str and NAME.fullmatch(n) for n in required):
        raise SelectionError('bad required windows')
    if type(windows) is not dict or len(windows) > 8:
        raise SelectionError('bad windows')
    for name, reading in windows.items():
        if type(name) is not str or not NAME.fullmatch(name) or type(reading) is not dict:
            raise SelectionError('bad window')
        if not set(reading) <= {'percent_used', 'observed_at', 'expires_at', 'reset_at'}:
            raise SelectionError('bad window keys')
        if 'percent_used' in reading:
            used = reading['percent_used']
            if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
                raise SelectionError('bad percent')
        dates = {k: _timestamp(reading[k]) for k in ('observed_at', 'expires_at', 'reset_at')
                 if k in reading and reading[k] is not None}
        if 'observed_at' in dates and 'expires_at' in dates and not dates['observed_at'] < dates['expires_at']:
            raise SelectionError('expiry before observation')
    return record


def evidence_reason(worker, record, now):
    """quota_available / quota_unknown / quota_incomplete / quota_exhausted / quota_future / quota_expired."""
    if record is None:
        return 'quota_unknown'
    try:
        validate_evidence(worker, record)
    except SelectionError:
        return 'quota_unknown'
    required = record['required_windows']
    if not required or any(name not in record['windows'] or not
                           {'percent_used', 'observed_at', 'expires_at'} <= record['windows'][name].keys()
                           for name in required):
        return 'quota_incomplete'
    readings = [record['windows'][name] for name in required]
    if any(_timestamp(r['observed_at']) <= now < _timestamp(r['expires_at']) and r['percent_used'] >= 100
           for r in readings):
        return 'quota_exhausted'
    if any(now < _timestamp(r['observed_at']) for r in readings):
        return 'quota_future'
    if any(now >= _timestamp(r['expires_at']) for r in readings):
        return 'quota_expired'
    return 'quota_available'


def peak(record):
    if not isinstance(record, dict):
        return None
    values = [row.get('percent_used') for row in (record.get('windows') or {}).values() if isinstance(row, dict)]
    values = [v for v in values if type(v) in (int, float)]
    return max(values) if values else None


def _observation(policy, worker, io, now, cached_only):
    """(observation, source) from a fresh/exhausted cache, else one live read (unless cached_only)."""
    cached = io[worker]['load']()
    evidence = quota_service.evidence_of(cached)
    if cached_only or quota_service.is_fresh(evidence, now) or quota_service.exhausted_until(evidence, now):
        return cached, 'cache'
    try:
        observation = io[worker]['observe'](policy)
    except Exception:
        observation = None
    if quota_service.evidence_of(observation) is not None:
        io[worker]['save'](observation)
    return observation, 'live'


def developer_ready(policy, task_chars):
    options = policy.get('developer') or {}
    return bool(options.get('enabled') and options.get('endpoint')) and task_chars <= options.get('draft_max_chars', 0)


def select(task_chars, policy=None, cached_only=False, now=None, io=None, consult_all=False,
           consult_cursor=False, threshold_overrides=None, claude_profile_loader=None):
    """Return {'worker', 'reason', 'eligible', 'meters', 'reasons', 'sources', 'versions', ...}."""
    policy = policy or config.load_policy()
    clock_injected = now is not None
    now = now or datetime.now(timezone.utc)
    io = io if io is not None else quota_service.default_io(policy)
    never = set(policy.get('never') or [])
    unmetered = set(policy.get('unmetered_workers') or [])
    overrides = threshold_overrides or {}
    eligible, meters, reasons, sources, versions, warnings = [], {}, {}, {}, {}, {}
    for worker in policy['worker_order']:
        if config.blocked_worker(policy, worker):
            reasons[worker] = 'never'
            continue
        if worker in (policy.get('ineligible') or {}):
            reasons[worker] = 'ineligible'
            continue
        if worker == 'claude':
            account = claude_account.check(policy, claude_profile_loader)
            if account is not None and account != 'personal':
                reasons[worker] = f'claude_account_{account}'
                continue
        if worker == 'developer':
            ok = developer_ready(policy, task_chars)
            reasons[worker] = 'draft_fallback' if ok else 'developer_disabled_or_task_too_large'
            eligible += [worker] if ok else []
            continue
        quota = config.quota_cfg(policy, worker)
        if quota is None or worker not in io:
            ok = worker in unmetered
            reasons[worker] = 'unmetered' if ok else ('observer_disabled' if quota is not None else 'no_meter')
            eligible += [worker] if ok else []
            continue
        consult = quota.get('consult', 'always')
        if consult == 'fallback' and eligible and not consult_all and not (consult_cursor and worker == 'cursor'):
            reasons[worker] = 'not_consulted'
            continue
        observation, sources[worker] = _observation(policy, worker, io, now, cached_only)
        if sources[worker] == 'live' and not clock_injected:
            now = datetime.now(timezone.utc)  # a live read is stamped after `now`; don't call it future-dated
        evidence = quota_service.evidence_of(observation)
        reason = evidence_reason(worker, evidence, now)
        meters[worker] = peak(evidence) if reason in ('quota_available', 'quota_exhausted') else None
        if isinstance(observation, dict):
            versions[worker] = observation.get('cli_version')
            if observation.get('version_warning'):
                warnings[worker] = observation['version_warning']
        limit = overrides.get(worker, quota.get('usage_threshold_percent', 85))
        if reason == 'quota_available' and any(
                evidence['windows'][name]['percent_used'] >= limit for name in evidence['required_windows']):
            reason = 'over_threshold'
        reasons[worker] = reason
        if reason == 'quota_available':
            eligible.append(worker)
    worker = eligible[0] if eligible else None
    return {'worker': worker, 'reason': 'preferred_eligible_worker' if worker else 'no_eligible_worker' if policy['worker_order'] else 'no_workers_configured',
            'hint': None if policy['worker_order'] else 'run llm-router init',
            'eligible': eligible, 'meters': meters, 'reasons': reasons, 'sources': sources,
            'cli_versions': versions, 'version_warnings': warnings,
            'never': sorted(never), 'ineligible': policy.get('ineligible') or {}}


def read_meter(policy, worker, now=None, io=None):
    """A forced worker reads only its own meter (fresh cache, else one live read)."""
    return quota_service.read_meter(policy, worker, now=now, io=io)
