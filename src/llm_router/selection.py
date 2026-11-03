"""Worker selection: validate live evidence, then score eligible allowances deterministically.

Rules (same safety model for every worker):
- A metered worker is eligible only with fresh evidence whose every required window is below its
  usage_threshold_percent. Unknown, stale, malformed or future-dated evidence means ineligible.
- consult="fallback" workers use fresh cache at any rank; live reads remain fallback-only.
- Workers in `never` are never candidates. Unmetered workers are eligible only when listed in
  unmetered_workers. `developer` is eligible only for draft-size tasks when enabled with an endpoint.
"""
from copy import deepcopy
from datetime import datetime, timezone
import http.client
import json
import math
import os
import re
import urllib.request

from . import claude_account, config, cooldown, decision_router, quota_service, week_curve

LOCAL_WORKERS = ('developer', 'aider-ollama', 'nativ', config.ORCHESTRATOR)  # never displaced by week-curve pacing
NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}')
HEALTH_TIMEOUT_SECONDS = 2
HEALTH_MAX_BYTES = 1 << 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _default_opener(url, timeout):
    """GET url without proxies or redirects; return (status, body bytes)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    with opener.open(urllib.request.Request(url, headers={'Accept': 'application/json'}), timeout=timeout) as response:
        return response.status, response.read(HEALTH_MAX_BYTES + 1)


def local_model_config(policy, worker):
    """{'base_url', 'model'} for a worker from workers.<name> or run_task.local_models.<name>."""
    found = dict((policy.get('run_task') or {}).get('local_models', {}).get(worker) or {})
    opts = (policy.get('workers') or {}).get(worker) or {}
    if isinstance(opts, dict):
        found.update({k: opts[k] for k in ('base_url', 'model') if k in opts})
    return found


def check_local_worker(policy, worker, opener=None):
    """(True, None) when the worker's local server lists its model at <base_url>/v1/models.

    opener(url, timeout) -> (status, body bytes); raising means unreachable. Workers without a
    base_url are not probed. Returns (False, 'server_not_running') for an unreachable server,
    non-200 or invalid JSON, and (False, 'model_not_present') when the configured model is absent.
    """
    local = local_model_config(policy, worker)
    base_url = local.get('base_url')
    if not base_url:
        return True, None
    if not config.valid_base_url(base_url):
        return False, 'invalid_base_url'
    try:
        status, body = (opener or _default_opener)(base_url.rstrip('/') + '/v1/models', HEALTH_TIMEOUT_SECONDS)
        if status != 200 or len(body) > HEALTH_MAX_BYTES:
            return False, 'server_not_running'
        data = json.loads(body)
    except (OSError, ValueError, http.client.HTTPException):
        return False, 'server_not_running'
    model = local.get('model')
    if not model:
        return True, None
    listed = data.get('data') if isinstance(data, dict) else None
    if not isinstance(listed, list):
        return False, 'server_not_running'
    ids = [entry.get('id') for entry in listed if isinstance(entry, dict) and isinstance(entry.get('id'), str)]
    if any(i == model or i.endswith('/' + model) or model.endswith('/' + i) for i in ids):
        return True, None
    return False, 'model_not_present'


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


HEALTH_WORKERS = ('aider-ollama', 'nativ')
OLLAMA_DEFAULT = 'http://127.0.0.1:11434'


def _worker_cfg(policy, worker):
    cfg = (policy.get('workers') or {}).get(worker)
    return cfg if isinstance(cfg, dict) else {}


def configured_model(policy, worker):
    """The local model from workers.<w>.model, else `--model [ollama_chat/]<m>` in its command."""
    model = _worker_cfg(policy, worker).get('model')
    if not isinstance(model, str) or not model:
        argv = ((policy.get('run_task') or {}).get('commands') or {}).get(worker) or []
        model = None
        for i, arg in enumerate(argv):
            if arg == '--model' and i + 1 < len(argv) and isinstance(argv[i + 1], str):
                model = argv[i + 1]
            elif isinstance(arg, str) and arg.startswith('--model='):
                model = arg.split('=', 1)[1]
        if not model:
            return None
    if worker == 'aider-ollama':
        for prefix in ('ollama_chat/', 'ollama/'):
            if model.startswith(prefix):
                return model[len(prefix):]
    return model


def health_url(policy, worker):
    base = _worker_cfg(policy, worker).get('base_url')
    if worker == 'aider-ollama':
        base = base or os.environ.get('OLLAMA_API_BASE') or OLLAMA_DEFAULT
        return base.rstrip('/') + '/api/tags'
    base = (base or '').rstrip('/')
    base = base[:-3] if base.endswith('/v1') else base
    return base + '/v1/models' if base else None


def wants_health_check(policy, worker, health_opener):
    """Probe only when asked: an injected opener, health_check: true, or a configured base_url."""
    if worker not in HEALTH_WORKERS:
        return False
    cfg = _worker_cfg(policy, worker)
    return health_opener is not None or cfg.get('health_check') is True or bool(cfg.get('base_url'))


def health_reason(policy, worker, health_opener=None, timeout=3):
    """None when the local server lists the configured model, else 'server_unreachable' / 'model_not_present'.

    health_opener(url, timeout) returns (status, body); body is bytes, str or already-parsed JSON.
    """
    if worker == 'nativ':
        ok, why = check_local_worker(policy, worker, health_opener)
        if ok:
            return None
        return 'server_unreachable' if why in ('server_not_running', 'invalid_base_url') else why
    url = health_url(policy, worker)
    if not url:
        return 'server_unreachable'
    try:
        status, body = (health_opener or _default_opener)(url, timeout)
        if status != 200:
            return 'server_unreachable'
        payload = json.loads(body) if isinstance(body, (bytes, str)) else body
    except Exception:
        return 'server_unreachable'
    if not isinstance(payload, dict):
        return 'server_unreachable'
    key, field = ('models', 'name') if worker == 'aider-ollama' else ('data', 'id')
    rows = payload.get(key)
    names = {row.get(field) for row in rows if isinstance(row, dict)} if isinstance(rows, list) else set()
    model = configured_model(policy, worker)
    if model is None:
        return None if names else 'model_not_present'
    if worker == 'aider-ollama' and ':' not in model:
        names |= {n.split(':', 1)[0] for n in names if isinstance(n, str) and n.endswith(':latest')}
    return None if model in names else 'model_not_present'


def select(task_chars, policy=None, cached_only=False, now=None, io=None, consult_all=False,
           consult_cursor=False, threshold_overrides=None, claude_profile_loader=None, cooldowns=None,
           task_class=None, grok_bot_evidence=None, health_opener=None, role=None,
           task_text='', decision_model=None):
    """Return {'worker', 'reason', 'eligible', 'meters', 'reasons', 'sources', 'versions', ...}.

    Workers cooling down after a quota error (see cooldown.py) are skipped until their reset.
    task_class walks class_order.<task_class> instead of worker_order; grok-bot ends every class
    order and is never quota-gated, so a class job is handed back to the orchestrator, not refused.
    Workers sharing the grok-bot pool are paced by week_curve.gate(): an unknown pool is tried last
    among cloud workers and ahead-of-curve drops one rank; neither bans.
    Local aider-ollama / nativ workers are probed (health_reason) when health_opener is given or the
    worker sets health_check: true or base_url; Nativ also uses check_local_worker() for OpenAI-style
    /v1/models probes. A down server or a missing model makes them ineligible.
    """
    policy = policy or config.load_policy()
    if role is not None:
        from . import roles
        roles.role_order(policy, role)
    clock_injected = now is not None
    now = now or datetime.now(timezone.utc)
    cooled = cooldown.active(now, cooldowns)
    io = io if io is not None else quota_service.default_io(policy)
    never = set(policy.get('never') or [])
    unmetered = set(policy.get('unmetered_workers') or [])
    overrides = threshold_overrides or {}
    eligible, meters, reasons, sources, versions, warnings = [], {}, {}, {}, {}, {}
    evidences = {}
    order = config.worker_order(policy, task_class)
    grok_budget, grok_verdict, grok_why = week_curve.gate(policy, now, grok_bot_evidence)
    grok_family = week_curve.pool_members(policy)
    paced = grok_family if grok_verdict != week_curve.FIRST_FIT else set()
    enabled = None
    if 'setup' in policy:
        from . import setup_profile
        enabled = set(setup_profile.validate_setup(policy['setup'])['enabled_workers'])
    for worker in order:
        if enabled is not None and worker not in enabled:
            reasons[worker] = 'setup_disabled'
            continue
        if worker == config.ORCHESTRATOR:
            reasons[worker] = 'orchestrator_fallback'
            eligible.append(worker)
            continue
        if config.blocked_worker(policy, worker):
            reasons[worker] = 'never'
            continue
        if worker in (policy.get('ineligible') or {}):
            reasons[worker] = 'ineligible'
            continue
        if worker in cooled:
            reasons[worker] = 'cooldown_until_' + cooled[worker].isoformat()
            continue
        if worker == 'claude':
            account = claude_account.check(policy, claude_profile_loader)
            if account is not None and account != 'personal':
                reasons[worker] = f'claude_account_{account}'
                continue
        if wants_health_check(policy, worker, health_opener):
            unhealthy = health_reason(policy, worker, health_opener)
            if unhealthy is not None:
                reasons[worker] = unhealthy
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
        # a paced grok-family worker is not a first choice, so it must not stop a fallback read
        cache_only_fallback = (consult == 'fallback' and any(w not in paced for w in eligible)
                               and not consult_all and not (consult_cursor and worker == 'cursor'))
        try:
            observation, sources[worker] = _observation(
                policy, worker, io, now, cached_only or cache_only_fallback)
        except (TypeError, ValueError, KeyError, AttributeError):
            observation, sources[worker] = None, 'cache'
        if sources[worker] == 'live' and not clock_injected:
            now = datetime.now(timezone.utc)  # a live read is stamped after `now`; don't call it future-dated
        evidence = quota_service.evidence_of(observation)
        try:
            validate_evidence(worker, evidence)
        except (SelectionError, TypeError, ValueError, KeyError, AttributeError):
            evidence = None
        else:
            evidences[worker] = deepcopy(evidence)
        try:
            reason = evidence_reason(worker, evidence, now)
        except (SelectionError, TypeError, ValueError, KeyError, AttributeError):
            reason = 'quota_unknown'
        if cache_only_fallback and reason not in ('quota_available', 'quota_exhausted'):
            reason = 'not_consulted'
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
    if grok_why:
        reasons.update({w: grok_why for w in eligible if w in grok_family})
    from . import allocation_policy, usage_snapshot
    effective_policy = deepcopy(policy)
    for name, limit in overrides.items():
        effective_policy.setdefault('quota', {}).setdefault(name, {})['usage_threshold_percent'] = limit
    if 'developer' in eligible:
        # Developer's existing draft-only gate is intrinsically unmetered.
        effective_policy['unmetered_workers'] = list(dict.fromkeys(
            list(effective_policy.get('unmetered_workers') or []) + ['developer']))
    snapshot = usage_snapshot.build_snapshot(
        effective_policy, now, order, eligible, reasons, evidences,
        pacing={'members': sorted(grok_family), 'verdict': grok_verdict})
    allocation = allocation_policy.allocate(snapshot, role)
    eligible = allocation['order']
    worker = eligible[0] if eligible else None
    reason = ('handoff_to_orchestrator' if worker == config.ORCHESTRATOR else 'preferred_eligible_worker' if worker
              else 'no_eligible_worker' if order else 'no_workers_configured')
    shadow = decision_router.score_candidates(
        task_text, eligible, meters=meters, task_class=task_class,
        selected_worker=worker if worker != config.ORCHESTRATOR else None,
        model=decision_model,
    )
    return {'worker': worker, 'reason': reason, 'task_class': task_class,
            'allocation': allocation, 'usage_snapshot': snapshot, 'evidences': evidences,
            'hint': None if order else 'run llm-router init',
            'eligible': eligible, 'meters': meters, 'reasons': reasons, 'sources': sources,
            'cli_versions': versions, 'version_warnings': warnings,
            'never': sorted(never), 'ineligible': policy.get('ineligible') or {},
            'cooldowns': {name: until.isoformat() for name, until in cooled.items()},
            'grok_bot': dict(grok_budget, verdict=grok_verdict),
            'decision_shadow': shadow}


def read_meter(policy, worker, now=None, io=None):
    """A forced worker reads only its own meter (fresh cache, else one live read)."""
    return quota_service.read_meter(policy, worker, now=now, io=io)


def select_for_worker(task_chars, worker, policy=None, role=None, **kwargs):
    """Allocate one explicit/pinned candidate through every ordinary hard gate.

    Only that worker's meter is consulted. Preference fit may be overridden, but
    the allocator's judge/advisor local prohibition and all quota/reserve gates
    remain authoritative. Grok Bot is only a manual handback candidate.
    """
    policy = policy or config.load_policy()
    copied = deepcopy(policy)
    config.materialise_workers(copied)
    if worker not in config.known_workers(copied):
        raise config.ConfigError('unknown worker override')
    if config.blocked_worker(copied, worker):
        raise config.ConfigError('worker override is disabled or banned')
    copied['worker_order'] = [worker]
    if role is not None:
        copied.setdefault('role_order', {})[role] = [worker]
    kwargs.pop('task_class', None)
    return select(task_chars, policy=copied, role=role, task_class=None, **kwargs)
