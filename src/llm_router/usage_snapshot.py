"""Versioned, JSON-compatible usage evidence for deterministic allocation (#208).

This module only adapts supplied policy and observations. It never reads settings,
collects usage, changes a provider, or guesses that a reset replenished an allowance.
"""
from copy import deepcopy
from datetime import datetime, timezone
import math

SCHEMA_VERSION = 1
LOCAL_WORKERS = frozenset(('developer', 'aider-ollama', 'nativ', 'local'))
WINDOW_KINDS = {'primary': '5h', 'session': '5h', '5h': '5h', 'weekly': 'weekly',
                'monthly': 'monthly', 'included': 'monthly', 'auto': 'monthly', 'api': 'monthly'}
ROLES = ('planner', 'coder', 'searcher', 'judge', 'advisor')


def parse_time(value):
    """Parse an aware time, raising ValueError rather than accepting local time."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError('timestamp must include a timezone')
        return parsed
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        raise ValueError('timestamp must be an aware ISO time') from error


def _stamp(value):
    return parse_time(value).astimezone(timezone.utc).isoformat() if value is not None else None


def _number(value, label, low=0, high=None, nullable=False):
    if value is None and nullable:
        return
    if (type(value) not in (int, float) or not math.isfinite(value) or value < low
            or (high is not None and value > high)):
        raise ValueError(f'{label} must be a finite number in range {low}..{high}')


def _names(value, label):
    if (not isinstance(value, list) or any(not isinstance(name, str) or not name for name in value)
            or len(set(value)) != len(value)):
        raise ValueError(f'{label} must be a list of unique nonempty names')


def _object(value, label):
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key for key in value):
        raise ValueError(f'{label} must be an object with nonempty string keys')


def _window(name, raw):
    if not isinstance(raw, dict):
        raise ValueError(f'window {name} must be an object')
    kind = raw.get('kind', WINDOW_KINDS.get(name, 'unknown'))
    if kind not in ('5h', 'weekly', 'monthly', 'unknown'):
        raise ValueError(f'window {name} has unknown kind')
    _number(raw.get('percent_used'), f'window {name} percent_used', high=100, nullable=True)
    return {'kind': kind, 'percent_used': raw.get('percent_used'),
            'observed_at': _stamp(raw.get('observed_at', raw.get('read_at'))),
            'expires_at': _stamp(raw.get('expires_at')), 'reset_at': _stamp(raw.get('reset_at'))}


def _windows(raw):
    _object(raw, 'windows')
    return {name: _window(name, row) for name, row in raw.items()}


def _allowance(raw):
    _object(raw, 'allowance')
    return {'windows': _windows(raw.get('windows', {})),
            'required_windows': deepcopy(raw.get('required_windows', []))}


def _merge_window(name, first, second):
    """Commutative same-allowance merge: newest observation, conservative ties.

    At equal observation times an exhausted reading cannot become spare capacity.
    Conflicting reset/kind or missing usage/freshness becomes unknown. A source
    with no observation time cannot establish that it supersedes another source.
    """
    first, second = _window(name, first), _window(name, second)
    a, b = first['observed_at'], second['observed_at']
    if a is not None and b is not None and a != b:
        return first if parse_time(a) > parse_time(b) else second
    observed = a if a == b else None
    kind = first['kind'] if first['kind'] == second['kind'] else 'unknown'
    reset = first['reset_at'] if first['reset_at'] == second['reset_at'] else None
    expiries = (first['expires_at'], second['expires_at'])
    expiry = min(expiries, key=parse_time) if all(value is not None for value in expiries) else None
    known = (observed is not None and first['kind'] == second['kind']
             and first['reset_at'] == second['reset_at']
             and first['percent_used'] is not None and second['percent_used'] is not None)
    return {'kind': kind,
            'percent_used': max(first['percent_used'], second['percent_used']) if known else None,
            'observed_at': observed, 'expires_at': expiry, 'reset_at': reset}


def _merge(base, extra):
    """Merge metadata, conservatively combining usage from every source.

    Used for worker observations, configured provider metadata and explicit usage
    overrides alike. Non-usage metadata keeps its explicit override semantics.
    """
    for key, value in extra.items():
        if key == 'windows':
            current = _windows(base.get(key, {}))
            incoming = _windows(value)
            base[key] = {name: _merge_window(name, current[name], incoming[name])
                         if name in current and name in incoming else
                         deepcopy(current[name] if name in current else incoming[name])
                         for name in sorted(set(current) | set(incoming))}
        elif key == 'required_windows':
            current = base.get(key, [])
            _names(current, 'required_windows')
            _names(value, 'required_windows')
            base[key] = sorted(set(current) | set(value))
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = deepcopy(value)


def build_snapshot(policy, as_of, configured_order, eligible, reasons, evidences, *,
                   provider_usage=None, pacing=None):
    """Freeze raw observations and supplied allocation metadata without any IO.

    Worker mappings live in allocation.worker_pools; provider metadata can be
    supplied by allocation.providers or provider_usage (the latter takes priority).
    A mapped pool's observation belongs only to that pool, not every sibling pool.
    """
    from . import roles
    _names(configured_order, 'configured_order')
    allocation = policy.get('allocation') or {}
    mappings = allocation.get('worker_pools') or {}
    _object(mappings, 'worker_pools')
    metadata = deepcopy(allocation.get('providers') or {})
    _object(metadata, 'providers')
    if provider_usage is not None:
        _object(provider_usage, 'provider_usage')
        _merge(metadata, provider_usage)
    candidates, providers = {}, {}
    for name in configured_order:
        mapping = mappings.get(name) or {}
        _object(mapping, f'worker mapping {name}')
        provider_id, pool = mapping.get('provider', name), mapping.get('pool')
        quota = (policy.get('quota') or {}).get(name) or {}
        threshold = quota.get('usage_threshold_percent', 85)
        _number(threshold, f'{name} threshold', high=100)
        candidates[name] = {'provider': provider_id, 'pool': pool,
                            'unmetered': name in (policy.get('unmetered_workers') or []),
                            'local': name in LOCAL_WORKERS, 'gate_eligible': name in eligible,
                            'gate_reason': reasons.get(name, 'eligible' if name in eligible else 'ineligible'),
                            'threshold_percent': threshold}
        provider = providers.setdefault(provider_id, {
            'windows': {}, 'required_windows': [], 'pools': {},
            'reserve_percent': max(15, 100 - threshold), 'hard_reserve_percent': None,
            'banked_resets': None, 'on_demand': None})
        provider['reserve_percent'] = max(provider['reserve_percent'], 100 - threshold)
        raw = evidences.get(name)
        if raw is not None:
            allowance = _allowance(raw)
            if pool is None:
                _merge(provider, allowance)
            else:
                target = provider['pools'].setdefault(pool, {'windows': {}, 'required_windows': []})
                _merge(target, allowance)
    for provider_id, raw in metadata.items():
        _object(raw, f'provider {provider_id}')
        provider = providers.setdefault(provider_id, {
            'windows': {}, 'required_windows': [], 'pools': {}, 'reserve_percent': 15,
            'hard_reserve_percent': None, 'banked_resets': None, 'on_demand': None})
        _merge(provider, raw)
    for provider in providers.values():
        provider['windows'] = _windows(provider['windows'])
        provider['pools'] = {name: _allowance(raw) for name, raw in provider['pools'].items()}
        if provider['banked_resets'] is not None:
            provider['banked_resets'] = [dict(bank, expires_at=_stamp(bank.get('expires_at')))
                                         for bank in provider['banked_resets']]
    result = {'schema_version': SCHEMA_VERSION, 'as_of': parse_time(as_of).astimezone(timezone.utc).isoformat(),
              'configured_order': list(configured_order), 'workers': candidates, 'providers': providers,
              'role_order': {role: roles.role_order(policy, role) for role in ROLES},
              'reset_rule': deepcopy(allocation.get('reset_rule')),
              'pacing': deepcopy(pacing) if pacing is not None else {'members': [], 'verdict': 'first_fit'}}
    return validate_snapshot(result)


def _validate_allowance(allowance, label):
    _object(allowance, label)
    if 'windows' not in allowance or 'required_windows' not in allowance:
        raise ValueError(f'{label} requires windows and required_windows')
    _object(allowance['windows'], label + '.windows')
    _names(allowance['required_windows'], label + '.required_windows')
    for name, window in allowance['windows'].items():
        _object(window, label + '.' + name)
        required = ('kind', 'percent_used', 'observed_at', 'expires_at', 'reset_at')
        if any(key not in window for key in required):
            raise ValueError(f'{label}.{name} missing window fields')
        if window['kind'] not in ('5h', 'weekly', 'monthly', 'unknown'):
            raise ValueError(f'{label}.{name} has unknown window kind')
        _number(window['percent_used'], label + '.' + name + '.percent_used', high=100, nullable=True)
        for key in ('observed_at', 'expires_at', 'reset_at'):
            if window[key] is not None:
                window[key] = _stamp(window[key])


def validate_snapshot(snapshot):
    """Return a validated independent copy; malformed/ambiguous data raises ValueError."""
    _object(snapshot, 'snapshot')
    result = deepcopy(snapshot)
    required = ('schema_version', 'as_of', 'configured_order', 'workers', 'providers',
                'role_order', 'reset_rule', 'pacing')
    if any(key not in result for key in required):
        raise ValueError('snapshot missing required fields')
    if type(result['schema_version']) is not int or result['schema_version'] != SCHEMA_VERSION:
        raise ValueError('unsupported snapshot schema_version')
    result['as_of'] = parse_time(result['as_of']).astimezone(timezone.utc).isoformat()
    _names(result['configured_order'], 'configured_order')
    _object(result['workers'], 'workers')
    _object(result['providers'], 'providers')
    if set(result['workers']) != set(result['configured_order']):
        raise ValueError('workers must match configured_order')
    for name, worker in result['workers'].items():
        _object(worker, name)
        fields = ('provider', 'pool', 'unmetered', 'gate_eligible', 'gate_reason', 'threshold_percent', 'local')
        if any(key not in worker for key in fields):
            raise ValueError(f'{name} missing worker fields')
        if not isinstance(worker['provider'], str) or not worker['provider']:
            raise ValueError(f'{name} requires provider')
        if worker['pool'] is not None and (not isinstance(worker['pool'], str) or not worker['pool']):
            raise ValueError(f'{name} pool must be a nonempty name or null')
        for key in ('unmetered', 'gate_eligible', 'local'):
            if type(worker[key]) is not bool:
                raise ValueError(f'{name}.{key} must be boolean')
        if not isinstance(worker['gate_reason'], str):
            raise ValueError(f'{name} gate_reason must be text')
        _number(worker['threshold_percent'], name + '.threshold_percent', high=100)
    for name, provider in result['providers'].items():
        _validate_allowance(provider, name)
        fields = ('pools', 'reserve_percent', 'hard_reserve_percent', 'banked_resets', 'on_demand')
        if any(key not in provider for key in fields):
            raise ValueError(f'{name} missing provider fields')
        _object(provider['pools'], name + '.pools')
        for pool_name, pool in provider['pools'].items():
            _validate_allowance(pool, name + '.' + pool_name)
        for key in ('reserve_percent', 'hard_reserve_percent'):
            value = provider[key]
            _number(value, name + '.' + key, high=100, nullable=key == 'hard_reserve_percent')
            if value == 100:
                raise ValueError(f'{name}.{key} must be below 100')
        banks = provider['banked_resets']
        if banks is not None:
            if not isinstance(banks, list):
                raise ValueError(f'{name} banked_resets must be a list or null')
            ids = []
            for bank in banks:
                _object(bank, name + '.banked_reset')
                if any(key not in bank for key in ('id', 'count', 'expires_at')):
                    raise ValueError(f'{name} missing banked reset fields')
                ids.append(bank['id'])
                if type(bank['count']) is not int or bank['count'] <= 0:
                    raise ValueError('banked reset count must be a positive integer')
                bank['expires_at'] = _stamp(bank['expires_at'])
                if bank['expires_at'] is None:
                    raise ValueError('banked reset requires expiry')
            _names(ids, name + '.banked_reset_ids')
        demand = provider['on_demand']
        if demand is not None:
            _object(demand, name + '.on_demand')
            if demand.get('enabled') is not None and type(demand['enabled']) is not bool:
                raise ValueError('on_demand.enabled must be boolean or null')
            for key in ('cap', 'used'):
                _number(demand.get(key), name + '.on_demand.' + key, nullable=True)
    _object(result['role_order'], 'role_order')
    if set(result['role_order']) != set(ROLES):
        raise ValueError('role_order must contain all five supported roles')
    for role, order in result['role_order'].items():
        _names(order, role + '.role_order')
    rule = result['reset_rule']
    if rule is not None:
        _object(rule, 'reset_rule')
        if rule.get('name') != 'recommend_before_expiry' or 'within_hours' not in rule:
            raise ValueError('unknown reset_rule; only explicit recommend_before_expiry is supported')
        _number(rule['within_hours'], 'reset_rule.within_hours')
    pacing = result['pacing']
    _object(pacing, 'pacing')
    _names(pacing.get('members'), 'pacing.members')
    if pacing.get('verdict') not in ('first_fit', 'try_last', 'drop_rank', 'ineligible'):
        raise ValueError('unknown pacing verdict')
    return result
