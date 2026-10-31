"""Unit scheduler: score each parallel worker by capability x quota headroom x week-curve pace.

score = capability * headroom * pace
- capability: policy scheduler.capabilities[worker][unit_kind] in [0, 1]. A worker with no entry is
  capable of every kind (1.0); a listed worker is capable only of the kinds it lists.
- headroom: smallest (threshold - used) / threshold over the required windows, clamped to [0, 1].
  Evidence that is not quota_available (unknown, stale, exhausted, ...) means 0. Unmetered workers get 1.
- pace: on the weekly window with a reset_at, remaining budget / budget a linear week curve leaves for
  the rest of the week, clamped to [0, 2]. Behind the curve (unused quota will expire) scores above 1;
  ahead of it scores below 1. No weekly reset known means 1.

grok-bot is never in the parallel pool: it is chosen for a unit only when no pool worker is both
capable and has headroom left.
"""
from datetime import timedelta

from . import config, selection

FALLBACK = 'grok-bot'
WEEK = timedelta(days=7)
WEEKLY = 'weekly'


def capability(policy, worker, unit_kind):
    table = ((policy.get('scheduler') or {}).get('capabilities') or {})
    if worker not in table:
        return 1.0
    value = (table[worker] or {}).get(unit_kind, 0)
    return min(max(float(value), 0.0), 1.0) if type(value) in (int, float) else 0.0


def headroom(policy, worker, evidence, now):
    quota = config.quota_cfg(policy, worker)
    if quota is None:
        return 1.0 if worker in (policy.get('unmetered_workers') or []) else 0.0
    if selection.evidence_reason(worker, evidence, now) != 'quota_available':
        return 0.0
    limit = quota.get('usage_threshold_percent', 85)
    if limit <= 0:
        return 0.0
    used = max(evidence['windows'][name]['percent_used'] for name in evidence['required_windows'])
    return min(max((limit - used) / limit, 0.0), 1.0)


def pace(evidence, now):
    reading = ((evidence or {}).get('windows') or {}).get(WEEKLY)
    if not isinstance(reading, dict) or reading.get('reset_at') is None or 'percent_used' not in reading:
        return 1.0
    try:
        reset = selection._timestamp(reading['reset_at'])
    except selection.SelectionError:
        return 1.0
    left = min(max((reset - now) / WEEK, 0.0), 1.0)
    if left == 0:
        return 1.0
    return min(max((100 - reading['percent_used']) / (100 * left), 0.0), 2.0)


def pool(policy):
    """Parallel workers in worker_order: never grok-bot, never blocked or ineligible ones."""
    ineligible = policy.get('ineligible') or {}
    return [w for w in policy['worker_order']
            if w != FALLBACK and w not in ineligible and not config.blocked_worker(policy, w)]


def assign(policy, unit_kind, evidences, now):
    """Return {'worker', 'reason', 'scores'} for one unit; evidences maps worker -> evidence or None."""
    scores = {}
    for worker in pool(policy):
        evidence = evidences.get(worker)
        cap, room = capability(policy, worker, unit_kind), headroom(policy, worker, evidence, now)
        scores[worker] = {'capability': cap, 'headroom': room, 'pace': pace(evidence, now),
                          'score': 0.0}
        scores[worker]['score'] = cap * room * scores[worker]['pace']
    ranked = [w for w in scores if scores[w]['score'] > 0]
    if ranked:
        best = max(ranked, key=lambda w: scores[w]['score'])  # ties keep worker_order
        return {'worker': best, 'reason': 'best_score', 'scores': scores}
    if FALLBACK in policy['worker_order'] and FALLBACK not in (policy.get('ineligible') or {}) \
            and not config.blocked_worker(policy, FALLBACK):
        return {'worker': FALLBACK, 'reason': 'no_capable_worker_with_quota', 'scores': scores}
    return {'worker': None, 'reason': 'no_capable_worker_with_quota', 'scores': scores}
