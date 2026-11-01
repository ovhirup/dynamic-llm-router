"""Validate every routing weight / capability change in the evaluation harness before applying (#71).

No proposed capability or weight table update is applied to a policy or persisted unless the
resulting routing order beats the current baseline in `eval_harness.run()`.
"""
import copy
import json
from pathlib import Path

from . import config, eval_harness


def _order_from_capabilities(base_order, capabilities, task_class='feature'):
    """Rank `base_order` descending by capability score for `task_class` (ties keep `base_order`)."""
    table = capabilities or {}
    scored = []
    tail = []
    for index, worker in enumerate(base_order):
        if worker == config.ORCHESTRATOR:
            tail.append(worker)
            continue
        if worker in table:
            score = float((table[worker] or {}).get(task_class, 0.0))
        else:
            score = 1.0
        scored.append((worker, index, score))
    scored.sort(key=lambda item: (-item[2], item[1]))
    return [w for w, _, _ in scored if _ > 0 or w not in table] + tail


def propose_weight_update(policy, candidate_capabilities, cases, task_class='feature',
                          mode='offline', profiles=None, use_recorded=True, **kwargs):
    """Evaluate `candidate_capabilities` against `policy` in the eval harness.

    Returns {'approved', 'gate', 'report', 'baseline_order', 'candidate_order', 'applied_capabilities'}.
    """
    current_caps = copy.deepcopy(((policy.get('scheduler') or {}).get('capabilities')) or {})
    base_order = list(policy.get('worker_order') or [])
    baseline_order = _order_from_capabilities(base_order, current_caps, task_class=task_class)
    candidate_order = _order_from_capabilities(base_order, candidate_capabilities, task_class=task_class)
    specs = [
        {
            'name': 'baseline',
            'worker_order': baseline_order or base_order,
            'never': list(policy.get('never') or []),
            'config': {'scheduler': {'capabilities': current_caps}},
        },
        {
            'name': 'candidate',
            'worker_order': candidate_order or base_order,
            'never': list(policy.get('never') or []),
            'config': {'scheduler': {'capabilities': copy.deepcopy(candidate_capabilities)}},
        },
    ]
    report = eval_harness.run(cases, specs, mode=mode, profiles=profiles, policy=policy,
                              use_recorded=use_recorded, **kwargs)
    approved = bool(report['gate']['adopt'])
    return {
        'approved': approved,
        'gate': report['gate'],
        'report': report,
        'baseline_order': baseline_order,
        'candidate_order': candidate_order,
        'applied_capabilities': copy.deepcopy(candidate_capabilities) if approved else current_caps,
    }


def apply_validated_weights(policy, candidate_capabilities, cases, state_path=None, **kwargs):
    """Apply `candidate_capabilities` to `policy` (and optional `state_path`) only if approved by harness."""
    verdict = propose_weight_update(policy, candidate_capabilities, cases, **kwargs)
    if verdict['approved']:
        policy.setdefault('scheduler', {})['capabilities'] = copy.deepcopy(candidate_capabilities)
        if state_path is not None:
            target = Path(state_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({
                'approved': True,
                'capabilities': candidate_capabilities,
                'gate': verdict['gate'],
            }, indent=2))
    return verdict
