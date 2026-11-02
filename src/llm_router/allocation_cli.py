"""Inspection and offline replay of the same allocation used by dispatch."""
import json
from pathlib import Path

from . import allocation_policy, config, selection, usage_snapshot


def register(commands):
    parser = commands.add_parser('plan', help='Explain budget-aware worker order without running a task')
    parser.add_argument('--role', choices=usage_snapshot.ROLES)
    parser.add_argument('--class', dest='task_class', choices=config.TASK_CLASSES)
    parser.add_argument('--size', choices=('draft', 'full'), default='full')
    parser.add_argument('--cached-only', action='store_true')
    parser.add_argument('--snapshot', help='Replay a versioned snapshot or saved plan JSON, entirely offline')
    parser.add_argument('--json', action='store_true', dest='json_output')


def select_result(args, policy):
    chars = 0 if args.size == 'draft' else (policy.get('developer') or {}).get('draft_max_chars', 4000) + 1
    result = selection.select(chars, policy=policy, cached_only=args.cached_only,
                              task_class=args.task_class, role=args.role)
    if args.role:
        result.update(role=args.role, role_pick={
            'role': args.role, 'worker': result['worker'], 'candidates': result['eligible'],
            'role_order': result['usage_snapshot']['role_order'][args.role],
            'reason': result['reason']})
    return result


def render(plan):
    lines = ['Order: ' + (' -> '.join(plan['order']) or '(no eligible worker)'),
             'Snapshot: ' + plan['as_of']]
    for worker in plan['order']:
        lines.append(f"  {worker}: {plan['reasons'][worker]}")
    for worker, reason in plan['excluded'].items():
        lines.append(f'  Excluded {worker}: {reason}')
    for action in plan['actions']:
        lines.append(f"  Action required: {action['provider']} / {action['reset_id']}: {action['reason']}")
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
        else:
            policy = config.load_policy(args.config)
            result = select_result(args, policy)
            plan = dict(result['allocation'], snapshot=result['usage_snapshot'])
    except (OSError, ValueError, TypeError, KeyError) as error:
        # JSON decode errors may contain input snippets; emit a fixed diagnostic.
        message = 'invalid snapshot JSON' if isinstance(error, json.JSONDecodeError) else str(error)
        print(json.dumps({'ok': False, 'error': message}))
        return 2
    print(json.dumps(plan, indent=2) if args.json_output else render(plan))
    return 0
