"""Pure, deterministic allowance scoring; no IO, clocks, or provider mutations (#208)."""
from . import usage_snapshot, week_curve

HARD_ROLES = frozenset(('planner', 'judge', 'advisor'))
LOCAL_WORKERS = usage_snapshot.LOCAL_WORKERS
FALLBACK = 'grok-bot'


def _window_reason(window, now):
    if window is None:
        return 'unknown_required_window'
    reset = usage_snapshot.parse_time(window['reset_at']) if window['reset_at'] is not None else None
    if reset is not None and reset <= now:
        return 'reset_passed_needs_new_reading'
    used = window['percent_used']
    if used is not None and used >= 100:
        return 'exhausted_until_reset' if reset is not None else 'exhausted_unknown_reset'
    if used is None:
        return 'unknown_required_window'
    observed, expiry = window['observed_at'], window['expires_at']
    if observed is None or expiry is None:
        return 'unknown_required_window_freshness'
    if usage_snapshot.parse_time(observed) > now:
        return 'future_required_window'
    if usage_snapshot.parse_time(expiry) <= now:
        return 'stale_required_window'
    return None


def _candidate(worker, provider, now, role):
    """(exclusion reason, score inputs) for a provider and the routed pool only."""
    if provider is None:
        return 'unknown_provider', None
    pool_name = worker['pool']
    pool = None
    if pool_name is not None:
        pool = provider['pools'].get(pool_name)
        if pool is None:
            return 'unknown_pool', None
    demand_headroom = None
    if pool_name == 'on_demand':
        demand = provider['on_demand']
        if not demand or demand.get('enabled') is not True:
            return 'on_demand_disabled_or_unknown', None
        cap, used = demand.get('cap'), demand.get('used')
        if cap is None or used is None:
            return 'on_demand_cap_unknown', None
        if used >= cap:
            return 'on_demand_cap_exhausted', None
        demand_headroom = (cap - used) / cap
    reserve = provider['reserve_percent']
    if role in HARD_ROLES and provider['hard_reserve_percent'] is not None:
        reserve = provider['hard_reserve_percent']
    windows = []
    for allowance in (provider, pool):
        if allowance is not None:
            windows.extend(allowance['windows'].get(name) for name in allowance['required_windows'])
    if not worker['unmetered'] and not windows and demand_headroom is None:
        return 'unknown_required_windows', None
    resets, headrooms = [], []
    # Explicitly unmetered workers keep their existing gate semantics and need no meter.
    for window in (() if worker['unmetered'] else windows):
        reason = _window_reason(window, now)
        if reason:
            return reason, None
        used = window['percent_used']
        if used >= worker['threshold_percent']:
            return 'over_threshold', None
        if 100 - used <= reserve:
            return 'reserve_floor', None
        headrooms.append((100 - used - reserve) / (100 - reserve))
        if window['reset_at'] is not None:
            resets.append(usage_snapshot.parse_time(window['reset_at']))
    if demand_headroom is not None:
        headrooms.append(demand_headroom)
    hours = (min(resets) - now).total_seconds() / 3600 if resets else None
    return None, {'urgency': 1 / (1 + hours) if hours is not None else 0.0,
                  'headroom': max(0.0, min(1.0, min(headrooms))) if headrooms else 1.0}


def _actions(snapshot, now):
    rule = snapshot['reset_rule']
    if rule is None:
        return []
    actions = []
    for provider_id in sorted(snapshot['providers']):
        provider = snapshot['providers'][provider_id]
        for bank in provider['banked_resets'] or []:
            hours = (usage_snapshot.parse_time(bank['expires_at']) - now).total_seconds() / 3600
            if 0 < hours <= rule['within_hours']:
                actions.append({'provider': provider_id, 'reset_id': bank['id'],
                                'action': 'action_required',
                                'reason': 'explicit recommend_before_expiry rule; reset expires soon; '
                                          'fresh confirmed usage is required before capacity changes'})
    return actions


def allocate(snapshot, role=None):
    """Return the chosen order and every score/exclusion from an immutable input.

    Scores are 50*expiry urgency + 40*role fit + 10*spendable headroom.
    Configured order breaks exact ties. Week-curve constraints then overlay that
    order, and the serial orchestrator fallback remains last regardless of score.
    """
    snapshot = usage_snapshot.validate_snapshot(snapshot)
    if role is not None and role not in snapshot['role_order']:
        raise ValueError(f'unknown task role {role!r}')
    now = usage_snapshot.parse_time(snapshot['as_of'])
    role_order = snapshot['role_order'][role] if role is not None else None
    ranked_role = [name for name in (role_order or []) if name != FALLBACK]
    scores, excluded, reasons, eligible = {}, {}, {}, []
    fallback = []
    for name in snapshot['configured_order']:
        worker = snapshot['workers'][name]
        reason = None
        if not worker['gate_eligible']:
            reason = worker['gate_reason']
        elif role_order is not None and (name not in role_order or (
                role in ('judge', 'advisor') and (worker['local'] or name in LOCAL_WORKERS))):
            reason = 'role_not_allowed'
        elif name == FALLBACK:
            fallback.append(name)
            scores[name] = {'score': 0.0, 'urgency': 0.0, 'role_fit': 0.0, 'headroom': 0.0,
                            'reason': 'serial_orchestrator_fallback'}
            reasons[name] = 'serial_orchestrator_fallback'
            continue
        else:
            reason, components = _candidate(worker, snapshot['providers'].get(worker['provider']), now, role)
        if reason is not None:
            excluded[name] = reason
            reasons[name] = reason
            continue
        role_fit = (1 - ranked_role.index(name) / max(len(ranked_role) - 1, 1)) if role is not None else 0.0
        score = 50 * components['urgency'] + 40 * role_fit + 10 * components['headroom']
        why = (f"score={score:.6f}; expiry_urgency={components['urgency']:.6f}; "
               f"role_fit={role_fit:.6f}; spendable_headroom={components['headroom']:.6f}")
        scores[name] = dict(components, role_fit=role_fit, score=score, reason=why)
        reasons[name] = why
        eligible.append(name)
    # Python's stable sort retains configured order for exact ties; no premature rounding.
    ranked = sorted(eligible, key=lambda name: -scores[name]['score'])
    pacing = snapshot['pacing']
    paced = week_curve.pace(ranked, set(pacing['members']), pacing['verdict'], LOCAL_WORKERS | {FALLBACK})
    for name in ranked:
        if name not in paced:
            excluded[name] = 'pacing:ineligible'
            reasons[name] = excluded[name]
        elif name in pacing['members'] and pacing['verdict'] != 'first_fit':
            reasons[name] += '; pacing:' + pacing['verdict']
    order = paced + fallback
    for rank, name in enumerate(order, 1):
        reasons[name] += f'; final_rank={rank}'
    return {'as_of': snapshot['as_of'], 'role': role, 'worker': order[0] if order else None,
            'order': order, 'excluded': excluded, 'scores': scores,
            'actions': _actions(snapshot, now), 'reasons': reasons}
