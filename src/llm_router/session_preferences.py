"""Pure setup preference previews plus explicit runtime resolution/audit (#210).

No prompt, collector, state access or mutation occurs in preview. Runtime callers
may supply a confirmation callback; its absence always retains a pending current
orchestrator. This module returns worker intent, never launches a worker or spends
banked resets. The dispatcher's quota, ban and protocol gates remain authoritative.
"""
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import math
import os
import stat

from . import config, orchestrator, security, selection, setup_profile, usage_snapshot

ORCHESTRATORS = SUPPORTED_ORCHESTRATORS = setup_profile.ORCHESTRATORS
_AUDIT_FIELDS = ('event', 'reason', 'source', 'role', 'caller', 'current', 'proposed',
                 'orchestrator', 'worker', 'confirmation_required', 'confirmed')


def _reason(value, default):
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise config.ConfigError('an override requires a nonempty reason')
    return value.strip()


def _setup(policy, role=None):
    if role is not None and role not in setup_profile.ROLES:
        raise config.ConfigError('unsupported task role')
    return setup_profile.validate_setup(policy['setup']) if 'setup' in policy else None


def _caller(env, configured):
    environment = os.environ if env is None else env
    override = (environment.get('LLM_ROUTER_ORCHESTRATOR') or '').strip().lower()
    if override in ('cli', 'aider-ollama'):
        return {'orchestrator': override, 'reason': 'env:LLM_ROUTER_ORCHESTRATOR'}
    detected = orchestrator.detect_orchestrator(env=environment)
    if configured and detected['reason'] == 'default_fallback':
        agent = (environment.get('LLM_ROUTER_USER_AGENT') or environment.get('TERM_PROGRAM') or '').lower()
        if 'aider-ollama' in agent:
            return {'orchestrator': 'aider-ollama', 'reason': 'user_agent:aider-ollama'}
        return {'orchestrator': 'cli', 'reason': 'user_agent:cli' if 'cli' in agent else 'default_fallback'}
    return detected


def _when(value):
    try:
        return usage_snapshot.parse_time(value)
    except ValueError:
        return None


def _percent(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100


def _budget(tool, result):
    snap = result.get('usage_snapshot') or {}
    mapped = (snap.get('workers') or {}).get(tool) or {}
    provider_id = mapped.get('provider', tool)
    provider = (snap.get('providers') or {}).get(provider_id) or {}
    pool = (provider.get('pools') or {}).get(mapped.get('pool')) or {}
    now = _when(snap.get('as_of'))
    windows, required_count = [], 0
    for scope, allowance in (('provider', provider), (mapped.get('pool'), pool)):
        for name in allowance.get('required_windows') or []:
            required_count += 1
            row = (allowance.get('windows') or {}).get(name) or {}
            used, reset = row.get('percent_used'), _when(row.get('reset_at'))
            observed, expiry = _when(row.get('observed_at')), _when(row.get('expires_at'))
            current_period = now is not None and (reset is None or reset > now)
            exhausted = current_period and _percent(used) and used >= 100
            fresh = current_period and observed is not None and expiry is not None and observed <= now < expiry
            remaining = 100 - used if _percent(used) and (fresh or exhausted) else None
            windows.append({'name': name, 'scope': scope, 'percent_remaining': remaining,
                            'reset_at': row.get('reset_at'), 'exhausted': bool(exhausted)})
    if tool == 'grok-bot' and not windows:
        grok = result.get('grok_bot') or {}
        used, reset = grok.get('percent_used'), _when(grok.get('reset_at'))
        known = (_percent(used) and grok.get('state') != 'unknown'
                 and (now is None or reset is None or reset > now))
        if known:
            required_count = 1
            windows.append({'name': 'weekly', 'scope': 'grok_bot', 'percent_remaining': 100 - used,
                            'reset_at': grok.get('reset_at'), 'exhausted': used >= 100})
    exhausted = any(row['exhausted'] for row in windows)
    known = bool(required_count) and all(row['percent_remaining'] is not None for row in windows)
    banks = [{key: bank.get(key) for key in ('id', 'count', 'expires_at')}
             for bank in provider.get('banked_resets') or [] if isinstance(bank, dict)]
    return {'status': 'exhausted' if exhausted else 'known' if known else 'unknown',
            'exhausted': exhausted, 'percent_remaining': min(row['percent_remaining'] for row in windows)
            if known else None, 'windows': windows, 'banked_resets': banks, 'provider': provider_id}


def _recommend(setup, result):
    enabled = setup['enabled_orchestrators']
    budgets = {tool: _budget(tool, result) for tool in enabled}
    preferred = setup['preferred_orchestrator']
    order = (result.get('allocation') or {}).get('order', result.get('eligible') or [])
    eligible = [tool for tool in order if tool in enabled and not budgets[tool]['exhausted']]
    chosen = preferred if preferred is not None else next(iter(eligible or enabled), None)
    if chosen is not None and budgets[chosen]['exhausted']:
        chosen = next(iter(eligible), None)
        reason = 'preferred allowance exhausted; next live eligible allocation' if chosen else 'preferred allowance exhausted; no live eligible alternative'
    else:
        reason = 'user preferred; ' + ('usage unknown' if chosen is None or budgets[chosen]['status'] == 'unknown' else 'known live allowance')
        if chosen == 'grok-bot':
            reason += '; chat, phone and approvals convenience'
    providers = {row['provider'] for row in budgets.values()}
    actions = [deepcopy(action) for action in (result.get('allocation') or {}).get('actions') or []
               if action.get('provider') in providers]
    return {'orchestrator': chosen, 'reason': reason, 'budgets': budgets, 'actions': actions}


def preview(policy, explicit=None, role=None, *, env=None, current=None, selection_result=None, reason=None):
    """Pure decision preview from supplied preferences and captured usage; no IO."""
    setup = _setup(policy, role)
    if explicit is not None:
        explicit = str(explicit).strip().lower()
        if explicit not in ORCHESTRATORS:
            raise config.ConfigError('unsupported orchestrator override')
    try:
        detected = _caller(env, setup is not None)
    except config.ConfigError:
        if explicit is None:
            raise
        detected = {'orchestrator': None, 'reason': 'explicit_flag'}
    caller = detected['orchestrator']
    override_reason = _reason(reason, 'explicit --orchestrator override') if explicit else None
    if setup is None:
        chosen = explicit or caller
        return security.redact_obj({'orchestrator': chosen, 'reason': 'explicit_flag' if explicit else detected['reason'],
            'source': 'explicit_flag' if explicit else 'legacy_detection', 'override_reason': override_reason,
            'caller': caller, 'current': current or caller, 'proposed': explicit if explicit != (current or caller) else None,
            'recommendation': {'orchestrator': chosen, 'reason': 'legacy detection; usage unknown', 'budgets': {}, 'actions': []},
            'confirmation_required': False, 'confirmed': False, 'switched': False, 'role': role, 'warnings': []})
    enabled = setup['enabled_orchestrators']
    if explicit is not None and explicit not in enabled:
        raise config.ConfigError('orchestrator override targets a disabled tool')
    current = current if current in enabled else setup['preferred_orchestrator']
    rec = _recommend(setup, selection_result or {})
    chosen, proposed, pending, source = current, None, False, 'preferred'
    why = 'preferred orchestrator; no enabled caller switch'
    pin = setup['orchestrator_overrides']['roles'].get(role)
    switch = setup['switch_policy']
    if explicit is not None:
        chosen, source, why = explicit, 'explicit_flag', override_reason
    elif pin is not None:
        chosen, source, why = pin['orchestrator'], 'role_pin', pin['reason']
    elif switch['mode'] == 'always_use':
        chosen, source, why = switch['target'], 'always_use', 'saved fixed orchestrator policy'
    elif switch['mode'] == 'always_accept_caller' and caller in enabled:
        chosen, source, why = caller, 'always_accept_caller', 'saved policy accepts the calling tool'
    elif switch['mode'] == 'always_ask' and caller in enabled and caller != current:
        proposed, pending, source, why = caller, True, 'always_ask', 'caller switch pending explicit confirmation'
    if not pending and chosen != current:
        proposed = chosen
    warnings = [f'{chosen}: known allowance exhausted; preference retained, see recommendation'] if chosen and rec['budgets'][chosen]['exhausted'] else []
    return security.redact_obj({'orchestrator': chosen, 'reason': why, 'source': source, 'override_reason': override_reason,
        'caller': caller, 'current': current, 'proposed': proposed, 'recommendation': rec,
        'confirmation_required': pending, 'confirmed': False, 'switched': False, 'role': role, 'warnings': warnings})


def _audit_path(policy):
    return config.state_dir() / 'logs' / 'setup-decisions.jsonl'


def _recommendation_id(value):
    """Normalize rich input and minimal wire data to an identifier, never nested metadata."""
    if isinstance(value, dict):
        if 'orchestrator' not in value:
            raise config.ConfigError('audit recommendation requires an orchestrator identifier')
        value = value['orchestrator']
    if value is None or (isinstance(value, str) and value in ORCHESTRATORS):
        return value
    raise config.ConfigError('audit recommendation identifier is unsupported')


def _validate_event(event):
    if not isinstance(event, dict) or event.get('event') not in ('orchestrator_decision', 'orchestrator_override',
                                                                  'worker_override'):
        raise config.ConfigError('unsupported setup audit event')
    for key in _AUDIT_FIELDS:
        if key not in event:
            continue
        value = event[key]
        if key in ('confirmation_required', 'confirmed'):
            if type(value) is not bool:
                raise config.ConfigError('audit confirmation fields must be boolean')
        elif value is not None and not isinstance(value, str):
            raise config.ConfigError('audit metadata must contain scalar identifiers/reasons only')
        if key in ('caller', 'current', 'proposed', 'orchestrator') and value is not None and value not in ORCHESTRATORS:
            raise config.ConfigError('audit orchestrator identifier is unsupported')
        if key == 'role' and value is not None and value not in setup_profile.ROLES:
            raise config.ConfigError('audit role is unsupported')
    _recommendation_id(event.get('recommendation'))


def _regular_audit(descriptor):
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise config.ConfigError('setup audit must be a private regular file')
    except BaseException:
        os.close(descriptor)
        raise


def _latest(handle):
    handle.seek(0)
    latest = None
    for line in handle:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if (not isinstance(event, dict) or event.get('event') != 'orchestrator_decision'
                or not {'orchestrator', 'previous', 'switched', 'at', 'reason'} <= event.keys()
                or event['orchestrator'] not in ORCHESTRATORS
                or event['previous'] not in ORCHESTRATORS + (None,)
                or type(event['switched']) is not bool or _when(event['at']) is None
                or not isinstance(event['reason'], str) or not event['reason'].strip()):
            continue
        try:
            _validate_event(event)
        except config.ConfigError:
            continue
        latest = event
    return latest


def _saved_current(policy):
    try:
        descriptor = os.open(_audit_path(policy), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        _regular_audit(descriptor)
        with os.fdopen(descriptor, 'r', errors='replace') as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            latest = _latest(handle)
        return latest.get('orchestrator') if latest else None
    except OSError:
        return None


def _append(policy, event, now):
    _validate_event(event)
    event = deepcopy(event)
    event['reason'] = _reason(event.get('reason'), 'explicit override')
    clock = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        raise config.ConfigError('audit time must be timezone-aware')
    path = _audit_path(policy)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    _regular_audit(descriptor)
    with os.fdopen(descriptor, 'r+', errors='replace') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise config.ConfigError('setup audit must be a private regular file')
        os.fchmod(handle.fileno(), 0o600)
        latest = _latest(handle)
        previous = latest.get('orchestrator') if latest is not None else event.get('current')
        if event['event'] == 'orchestrator_decision':
            allowed = (policy.get('setup') or {}).get('enabled_orchestrators', ORCHESTRATORS)
            if not event.get('confirmed'):
                event['orchestrator'] = previous if previous in allowed else event.get('current')
            event['current'] = previous
            if event.get('orchestrator') is not None and event['orchestrator'] not in allowed:
                raise config.ConfigError('orchestrator decision targets a disabled tool')
        record = {key: event[key] for key in _AUDIT_FIELDS if key in event}
        record.update(at=clock.astimezone(timezone.utc).isoformat())
        if event['event'] == 'orchestrator_decision':
            record.update(previous=previous, switched=previous != event.get('orchestrator'))
        if 'recommendation' in event:
            record['recommendation'] = _recommendation_id(event['recommendation'])
        record = security.redact_obj(record)
        handle.seek(0, os.SEEK_END)
        handle.write(json.dumps(record, allow_nan=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())
    return str(path), record


def audit_event(policy, event, now=None):
    """Append only whitelisted redacted decision metadata under an exclusive lock."""
    return _append(policy, event, now)[0]


def _collect(policy, setup):
    copied = deepcopy(policy)
    copied.pop('setup', None)
    copied['worker_order'] = [tool for tool in setup['enabled_orchestrators'] if tool in config.known_workers(copied)]
    return selection.select(0, policy=copied, cached_only=True, role=None) if copied['worker_order'] else {}


def _explanation(decision):
    rec = decision['recommendation']
    lines = [f"Current: {decision['current']}; proposed: {decision['proposed']}",
             'Reason: ' + decision['reason'], f"Recommendation: {rec['orchestrator']}; {rec['reason']}"]
    for tool, budget in rec['budgets'].items():
        lines.append(tool + ': ' + budget['status'])
        for window in budget['windows']:
            left = window['percent_remaining']
            lines.append(f"  {window['name']}: {left if left is not None else 'unknown'}% remaining; reset {window['reset_at'] or 'unknown'}")
        for bank in budget['banked_resets']:
            lines.append(f"  banked reset {bank['id']} expires {bank['expires_at']}; not live capacity")
    for action in rec['actions']:
        lines.append(f"  {action.get('action')}: {action.get('reset_id')}; {action.get('reason')}")
    lines.append('Choose yes (proposed), keep current, or use recommended.')
    return security.redact('\n'.join(lines))


def resolve_orchestrator(policy, explicit=None, role=None, *, reason=None, confirm=None, env=None,
                         selection_result=None, now=None, persist=True, force_confirmation=False, one_off=False):
    """one_off: an explicit flag applies to this job only; it is logged as an override and leaves
    the saved current orchestrator unchanged."""
    setup = _setup(policy, role)
    if setup is None and explicit is None:
        return preview(policy, role=role, env=env, reason=reason)
    captured = selection_result if selection_result is not None else _collect(policy, setup) if setup else {}
    decision = preview(policy, explicit, role, env=env, current=_saved_current(policy),
                       selection_result=captured, reason=reason)
    if one_off and explicit is not None:
        path, _ = _append(policy, dict(decision, event='orchestrator_override', reason=decision['override_reason']), now)
        decision.update(confirmed=True, switched=False, audit_path=path, persisted=True)
        return security.redact_obj(decision)
    if force_confirmation and decision['orchestrator'] != decision['current']:
        decision.update(proposed=decision['orchestrator'], orchestrator=decision['current'], confirmation_required=True)
    if not persist:
        decision.update(orchestrator=decision['current'], switched=False, confirmed=False, persisted=False)
        return decision
    if decision['confirmation_required'] and confirm is not None:
        answer = confirm(_explanation(decision))
        answer = 'yes' if answer is True else 'keep current' if answer is False else str(answer).strip().lower()
        if answer in ('yes', 'y', 'accept', 'switch'):
            decision.update(orchestrator=decision['proposed'], confirmed=True, confirmation_required=False)
        elif answer in ('recommended', 'recommendation', 'use recommended', 'r'):
            target = decision['recommendation']['orchestrator']
            if target is not None:
                decision.update(orchestrator=target, confirmed=True, confirmation_required=False)
                decision['reason'] += '; accepted recommendation'
        elif answer in ('keep', 'keep current', 'no', 'n'):
            decision.update(confirmation_required=False)
            decision['reason'] += '; kept current'
    elif not decision['confirmation_required']:
        decision['confirmed'] = True  # explicit/saved policy is prior user consent
    event = dict(decision, event='orchestrator_decision')
    if setup is None and explicit is not None:
        event['reason'] = decision['override_reason']
    path, recorded = _append(policy, event, now)
    decision.update(orchestrator=recorded['orchestrator'], current=recorded['previous'],
                    switched=recorded['switched'], audit_path=path, persisted=True)
    return security.redact_obj(decision)


def resolve_worker(policy, explicit=None, role=None, *, reason=None, now=None, audit=True):
    setup = _setup(policy, role)
    chosen, source, why = explicit, 'explicit_flag', _reason(reason, 'explicit --worker override') if explicit is not None else None
    if explicit is None and setup is not None:
        pin = setup['worker_overrides']['roles'].get(role) or setup['worker_overrides']['default']
        if pin is not None:
            chosen, source, why = pin['worker'], 'role_pin' if role in setup['worker_overrides']['roles'] else 'default_pin', pin['reason']
    if chosen is None:
        return {'worker': None, 'reason': 'automatic allocation', 'source': 'automatic'}
    if setup is not None and chosen not in setup['enabled_workers']:
        raise config.ConfigError('worker override targets a disabled tool')
    result = {'worker': chosen, 'reason': why, 'source': source}
    if audit:
        result['audit_path'] = audit_event(policy, dict(result, event='worker_override', role=role), now)
    return security.redact_obj(result)
