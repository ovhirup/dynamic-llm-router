"""Inspection and offline replay of the same allocation used by dispatch."""
import json
from pathlib import Path

from . import allocation_policy, config, security, selection, session_preferences, setup_profile, usage_snapshot


def register(commands):
    parser = commands.add_parser('plan', help='Explain budget-aware worker order without running a task')
    parser.add_argument('--role', choices=usage_snapshot.ROLES)
    parser.add_argument('--class', dest='task_class', choices=config.TASK_CLASSES)
    parser.add_argument('--size', choices=('draft', 'full'), default='full')
    parser.add_argument('--cached-only', action='store_true')
    parser.add_argument('--snapshot', help='Replay a versioned snapshot or saved plan JSON, entirely offline')
    parser.add_argument('--json', action='store_true', dest='json_output')
    parser.add_argument('--explain', action='store_true', help='Include setup defaults, pins, capabilities and session proposal')
    parser.add_argument('--worker', help='Inspect only an explicit worker; hard allocation gates remain active')
    parser.add_argument('--orchestrator', choices=session_preferences.ORCHESTRATORS)
    parser.add_argument('--override-reason')


def select_result(args, policy):
    chars = 0 if args.size == 'draft' else (policy.get('developer') or {}).get('draft_max_chars', 4000) + 1
    intent = session_preferences.resolve_worker(policy, explicit=getattr(args, 'worker', None),
        role=args.role, reason=getattr(args, 'override_reason', None), audit=False)
    options = {'policy': policy, 'cached_only': args.cached_only, 'task_class': args.task_class, 'role': args.role}
    if intent['worker'] is not None:
        result = selection.select_for_worker(chars, intent['worker'], **options)
        applied = result['worker'] == intent['worker']
        result['worker_override'] = dict(intent, applied=applied)
        if not applied:
            result['worker_override']['blocked_reason'] = result['allocation']['excluded'].get(intent['worker'], result['reason'])
        if intent['worker'] == config.ORCHESTRATOR:
            result['worker_override']['handoff'] = 'manual orchestrator handoff; never a CLI worker launch'
    else:
        result = selection.select(chars, **options)
    if args.role:
        result.update(role=args.role, role_pick={
            'role': args.role, 'worker': result['worker'], 'candidates': result['eligible'],
            'role_order': result['usage_snapshot']['role_order'][args.role], 'reason': result['reason']})
    return result


def _explanation(policy):
    setup = policy.get('setup')
    if setup is None:
        return {'session_preferences': 'legacy detection; no saved setup preferences',
                'worker_default': 'automatic allocation from live eligibility, expiry, role fit and headroom'}
    setup = setup_profile.validate_setup(setup)
    preferred = setup['preferred_orchestrator']
    rationale = (setup['tools'].get(preferred) or {}).get('rationale')
    if not rationale:
        rationale = ('chat, phone and approvals convenience; usage still checked' if preferred == 'grok-bot'
                     else 'explicitly saved preferred orchestrator')
    disabled = {}
    for name in setup_profile.ORCHESTRATORS:
        capabilities = {}
        if name not in setup['enabled_workers']:
            capabilities['worker'] = 'not an execution worker' if name == 'cli' else 'disabled in setup'
        if name not in setup['enabled_orchestrators']:
            capabilities['orchestrator'] = 'disabled in setup'
        if capabilities:
            disabled[name] = capabilities
    return {'session_preferences': 'saved setup profile; preview only, no switch or audit',
            'worker_default': 'automatic allocation unless an explicit, role or default pin is present',
            'preferred_orchestrator': {'orchestrator': preferred, 'reason': rationale},
            'disabled_capabilities': disabled,
            'reserves': {name: settings.get('reserve_percent') for name, settings in
                         ((policy.get('allocation') or {}).get('providers') or {}).items()}}


def _offline_worker(plan, args):
    target = getattr(args, 'worker', None)
    if target is None:
        return
    intent = session_preferences.resolve_worker(config.defaults(), explicit=target,
        role=args.role, reason=getattr(args, 'override_reason', None), audit=False)
    allowed = target in plan['order']
    plan['worker_override'] = dict(intent, applied=allowed)
    if not allowed:
        why = plan['excluded'].get(target, 'worker_not_in_captured_eligible_candidates')
        plan['excluded'][target] = why
        plan['reasons'][target] = why
        plan['worker_override']['blocked_reason'] = why
    else:
        for other in plan['order']:
            if other != target:
                plan['excluded'][other] = 'not chosen by explicit worker override'
    plan['order'] = [target] if allowed else []
    plan['worker'] = target if allowed else None
    if target == config.ORCHESTRATOR:
        plan['worker_override']['handoff'] = 'manual orchestrator handoff; never a CLI worker launch'


def render(plan):
    lines = ['Order: ' + (' -> '.join(plan['order']) or '(no eligible worker)'), 'Snapshot: ' + plan['as_of']]
    for worker in plan['order']:
        lines.append(f"  {worker}: {plan['reasons'][worker]}")
    for worker, reason in plan['excluded'].items():
        lines.append(f'  Excluded {worker}: {reason}')
    for action in plan['actions']:
        lines.append(f"  Action required: {action['provider']} / {action['reset_id']}: {action['reason']}")
    override = plan.get('worker_override')
    if override:
        lines.append(f"Worker override {override['worker']}: {override['source']}; {override['reason']}")
        lines.append('  ' + (override.get('blocked_reason') or override.get('handoff') or 'hard eligibility gates passed'))
    explanation = plan.get('explanation')
    if explanation:
        lines.append('Session preferences: ' + explanation['session_preferences'])
        if explanation.get('worker_default'):
            lines.append('Worker default: ' + explanation['worker_default'])
        preferred = explanation.get('preferred_orchestrator')
        if preferred:
            lines.append(f"Preferred orchestrator {preferred['orchestrator']}: {preferred['reason']}")
        for name, capabilities in explanation.get('disabled_capabilities', {}).items():
            lines.append(name + ': ' + '; '.join(key + ' ' + value for key, value in capabilities.items()))
        for name, reserve in explanation.get('reserves', {}).items():
            lines.append(f'Reserve {name}: {reserve}%; existing hard gates remain active')
        for name, provider in plan['snapshot']['providers'].items():
            for scope, allowance in [('provider', provider)] + list((provider.get('pools') or {}).items()):
                for window, row in allowance.get('windows', {}).items():
                    used = row.get('percent_used')
                    remaining = 100 - used if type(used) in (int, float) else 'unknown'
                    lines.append(f"Captured {name}/{scope}/{window}: {remaining}% remaining; reset {row.get('reset_at') or 'unknown'}")
            for bank in provider.get('banked_resets') or []:
                lines.append(f"Banked reset {bank['id']} expires {bank['expires_at']}; not live capacity")
    session = plan.get('orchestrator_preview')
    if session:
        rec = session['recommendation']
        lines.append(f"Orchestrator preview: current {session['current']}; proposed {session['proposed']}; {session['reason']}")
        lines.append(f"Recommendation: {rec['orchestrator']}; {rec['reason']}")
        for name, budget in rec['budgets'].items():
            lines.append(name + ' budget: ' + budget['status'])
            for window in budget['windows']:
                lines.append(f"  {window['name']}: {window['percent_remaining']}% remaining; reset {window['reset_at'] or 'unknown'}")
        for action in rec['actions']:
            lines.append(f"  {action.get('action')}: {action.get('reset_id')}; {action.get('reason')}")
    return '\n'.join(lines)


def run(args):
    try:
        if args.snapshot:
            if args.task_class:
                raise ValueError('--class cannot change a saved snapshot; collect a new class-specific plan')
            data = json.loads(Path(args.snapshot).expanduser().read_text())
            if isinstance(data, dict) and 'snapshot' in data:
                data = data['snapshot']
            snapshot = usage_snapshot.validate_snapshot(data)
            plan = dict(allocation_policy.allocate(snapshot, args.role), snapshot=snapshot)
            _offline_worker(plan, args)
            if getattr(args, 'explain', False):
                plan['explanation'] = {'session_preferences': 'not captured in snapshot; no live settings or state read'}
            if getattr(args, 'orchestrator', None):
                plan['orchestrator_preview'] = session_preferences.preview(config.defaults(), explicit=args.orchestrator,
                    role=args.role, env={}, reason=getattr(args, 'override_reason', None))
        else:
            policy = config.load_policy(args.config)
            result = select_result(args, policy)
            plan = dict(result['allocation'], snapshot=result['usage_snapshot'])
            if 'worker_override' in result:
                plan['worker_override'] = result['worker_override']
            if getattr(args, 'explain', False) or getattr(args, 'orchestrator', None):
                plan['orchestrator_preview'] = session_preferences.preview(policy,
                    explicit=getattr(args, 'orchestrator', None), role=args.role, selection_result=result,
                    reason=getattr(args, 'override_reason', None))
            if getattr(args, 'explain', False):
                plan['explanation'] = _explanation(policy)
    except (OSError, ValueError, TypeError, KeyError) as error:
        message = 'invalid snapshot JSON' if isinstance(error, json.JSONDecodeError) else security.redact(str(error))
        print(json.dumps({'ok': False, 'error': message}))
        return 2
    print(json.dumps(plan, indent=2) if args.json_output else render(plan))
    return 0
