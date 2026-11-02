"""Small dispatch adapter: refresh decisions, retain explanations, publish private sidecars.

The scorer remains pure. Sidecar IO is restricted to the explicit output directory chosen by its caller. Full usage snapshots and prompts are never
included in dispatch logs or sidecars.
"""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import tempfile

from . import security

PLAN_FIELDS = ('as_of', 'role', 'worker', 'order', 'excluded', 'scores', 'actions', 'reasons')


def minimal_plan(selection, order, as_of, override=None, considered=()):
    """Describe the actual dispatch order after pins and already-considered skips."""
    source = selection.get('allocation')
    if source is None and override is None:
        return None
    source = source or {}
    plan = {key: deepcopy(source[key]) for key in PLAN_FIELDS if key in source}
    plan.setdefault('as_of', as_of)
    plan.setdefault('role', None)
    plan.setdefault('excluded', {})
    plan.setdefault('scores', {})
    plan.setdefault('actions', [])
    plan.setdefault('reasons', {})
    plan['scored_order'] = list(source.get('order') or [])
    for name in plan['scored_order']:
        if name in considered:
            plan['excluded'].setdefault(name, 'already_considered_or_excluded')
            plan['reasons'][name] = 'already_considered_or_excluded'
    plan['order'] = list(order)
    plan['worker'] = order[0] if order else None
    plan['dispatch_reason'] = override or ('allocated' if order else 'no_unconsidered_eligible_worker')
    if override is not None and order:
        plan['reasons'][order[0]] = override
    if not order:
        plan['stop_reason'] = selection.get('reason') or 'no_unconsidered_eligible_worker'
    return security.redact_obj(plan)


def choices(initial, refresh, initial_order, *, forced=None, pinned=False, exclude=(),
            allowed=(), as_of=None, on_plan=None):
    """Yield (worker, selection) once per considered worker.

    Allocation-bearing automatic routes refresh before every subsequent candidate,
    including after slot skips. Legacy selectors retain their fixed initial list.
    Explicit workers never broaden to other meters/workers. An internal Claude pin
    may refresh fallback after its first attempt; its safety protocol stays in the
    caller's existing launch checks.
    """
    dynamic = 'allocation' in initial and (forced is None or pinned)
    considered, allowed = set(exclude), set(allowed)
    current, first, remaining = initial, True, list(initial_order)
    while True:
        names = remaining if first or not dynamic else current.get('eligible', [])
        names = list(dict.fromkeys(name for name in names if name != 'grok-bot'
                                  and name not in considered and (not allowed or name in allowed)))
        override = ('stackwich_pinned' if pinned else 'forced') if first and forced is not None else None
        if pinned and not dynamic and not first:
            override = 'legacy eligible failover after stackwich pin'
        plan = minimal_plan(current, names, as_of, override, considered)
        if plan is not None and on_plan is not None:
            on_plan(plan)
        if not names:
            return
        name = names[0]
        considered.add(name)  # full/local-busy slots must never be considered forever
        selected = dict(current, eligible=names)
        if plan is not None:
            selected['allocation'] = plan
        yield name, selected
        first = False
        if dynamic:
            current = refresh()
        else:
            remaining = names[1:]
            if not remaining:
                return


def remember(summary, plan):
    """Retain a minimal redacted current plan and its nonduplicate history."""
    if plan is None:
        return
    plan = security.redact_obj({key: deepcopy(value) for key, value in plan.items()
                                if key in PLAN_FIELDS + ('scored_order', 'dispatch_reason', 'stop_reason')})
    summary['allocation'] = plan
    history = summary.setdefault('allocation_history', [])
    if plan not in history:
        history.append(deepcopy(plan))


def merge_result(summary, result):
    """Carry task decisions into the job before any early return/handoff."""
    for plan in result.get('allocation_history') or []:
        remember(summary, plan)
    if result.get('allocation') is not None:
        remember(summary, result['allocation'])


def write_sidecar(output_dir, stamp, summary):
    """Atomically publish the minimal redacted decision before worker invocation."""
    if summary.get('allocation') is None:
        return None
    output_dir = Path(output_dir)
    payload = security.redact_obj({'allocation': summary['allocation'],
                                   'allocation_history': summary.get('allocation_history') or []})
    destination = output_dir / f'{stamp}-allocation.json'
    descriptor, temporary = tempfile.mkstemp(prefix='.allocation-', suffix='.tmp', dir=output_dir)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write('\n')
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return str(destination)


def allocation_fields(data):
    """Validate and whitelist plans from persisted jobs/sidecars; omit malformed data."""
    if not isinstance(data, dict):
        return {}

    def clean(plan):
        if not isinstance(plan, dict) or any(key not in plan for key in PLAN_FIELDS):
            return None
        if (not isinstance(plan['as_of'], str)
                or plan['role'] is not None and not isinstance(plan['role'], str)
                or plan['worker'] is not None and not isinstance(plan['worker'], str)
                or not isinstance(plan['order'], list)
                or any(not isinstance(name, str) for name in plan['order'])
                or any(not isinstance(plan[key], dict) for key in ('excluded', 'scores', 'reasons'))
                or not isinstance(plan['actions'], list)):
            return None
        result = {key: deepcopy(plan[key]) for key in PLAN_FIELDS}
        component_keys = ('score', 'urgency', 'role_fit', 'headroom', 'reason')
        if any(not isinstance(value, dict) for value in result['scores'].values()):
            return None
        result['scores'] = {name: {key: value for key, value in score.items() if key in component_keys}
                            for name, score in result['scores'].items()}
        for score in result['scores'].values():
            for key, value in score.items():
                if key == 'reason':
                    if not isinstance(value, str):
                        return None
                elif type(value) not in (int, float) or not math.isfinite(value):
                    return None
        if any(not isinstance(value, str) for key in ('reasons', 'excluded')
               for value in result[key].values()):
            return None
        if any(not isinstance(action, dict) for action in result['actions']):
            return None
        result['actions'] = [{key: value for key, value in action.items()
                              if key in ('provider', 'reset_id', 'action', 'reason')}
                             for action in result['actions']]
        if any(not isinstance(value, str) for action in result['actions'] for value in action.values()):
            return None
        for key in ('scored_order', 'dispatch_reason', 'stop_reason'):
            if key in plan:
                value = plan[key]
                if key == 'scored_order':
                    if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
                        return None
                elif not isinstance(value, str):
                    return None
                result[key] = deepcopy(value)
        return security.redact_obj(result)

    current = clean(data.get('allocation'))
    history = data.get('allocation_history', [])
    if current is None or not isinstance(history, list):
        return {}
    history = [clean(plan) for plan in history]
    if any(plan is None for plan in history):
        return {}
    return {'allocation': current, 'allocation_history': history}


def read_sidecar(output_dir, stamp):
    """Read the newest numeric job attempt; malformed current data fails soft."""
    import re
    pattern = re.compile(re.escape(stamp) + r'-a(\d+)-allocation\.json')
    candidates = []
    for path in Path(output_dir).glob(f'{stamp}-a*-allocation.json'):
        match = pattern.fullmatch(path.name)
        if match:
            candidates.append((int(match[1]), path))
    if not candidates:
        return {}
    attempt, path = max(candidates, key=lambda item: item[0])
    try:
        fields = allocation_fields(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return {}
    if fields:
        fields['_allocation_attempt'] = attempt
    return fields


def plan_unit(unit, policy):
    """Allocate an automatic full-size coding unit, preserving capability constraints."""
    from . import scheduler, selection, task_dag
    chars = (policy.get('developer') or {}).get('draft_max_chars', 4000) + 1
    result = selection.select(chars, policy=policy, cached_only=True, role='coder')
    source = result['allocation']
    plan = minimal_plan(result, source['order'], source['as_of'])
    allowed = unit.get('workers')
    order = []
    for worker in plan['order']:
        if worker not in task_dag.PARALLEL_POOL:
            reason = 'not_in_parallel_pool'
        elif allowed is not None and worker not in allowed:
            reason = 'unit_worker_restriction'
        elif scheduler.capability(policy, worker, 'code') <= 0:
            reason = 'unit_capability'
        else:
            order.append(worker)
            continue
        plan['excluded'][worker] = reason
        plan['reasons'][worker] = reason
    plan.update(order=order, worker=order[0] if order else None, role='coder')
    return plan
