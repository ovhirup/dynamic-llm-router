"""Adaptive routing weights (#69): UCB1 bandit exploration with a hard safety floor.

Combines a prior capability weight in [0, 1] with empirical pass-rate evidence and a bounded
upper-confidence-bound (UCB1) exploration bonus:
  mean = (prior + passed) / (1 + runs)
  bonus = c * sqrt(ln(total_runs + 1) / (runs + 1))
Workers whose empirical pass rate (once `runs >= min_runs`) falls below `safety_floor` are
clamped to `safety_floor` (`floored: True`) and never receive an exploration bonus, so a
degraded worker cannot outrank a healthy one. `grok-bot` (orchestrator fallback) always stays last.
"""
import math

from . import config

SAFETY_FLOOR = 0.25
EXPLORATION_C = 0.2
MIN_RUNS = 3


def compute_weight(prior=1.0, runs=0, passed=0, total_runs=0, safety_floor=SAFETY_FLOOR,
                   c=EXPLORATION_C, min_runs=MIN_RUNS):
    """Return {'weight', 'empirical_pass_rate', 'exploration_bonus', 'floored'}."""
    floor = min(max(float(safety_floor), 0.0), 1.0)
    prior = min(max(float(prior), floor), 1.0)
    runs = max(0, int(runs or 0))
    passed = min(max(0, int(passed or 0)), runs)
    total = max(runs, int(total_runs or 0))
    empirical = (passed / runs) if runs > 0 else None
    if empirical is not None and runs >= max(1, int(min_runs)) and empirical < floor:
        return {'weight': round(floor, 4), 'empirical_pass_rate': round(empirical, 4),
                'exploration_bonus': 0.0, 'floored': True}
    blended = (prior + passed) / (1 + runs)
    bonus = float(c) * math.sqrt(math.log(total + 1) / (runs + 1)) if total > 0 and c > 0 else 0.0
    weight = min(1.0, max(floor, blended + bonus))
    return {'weight': round(weight, 4),
            'empirical_pass_rate': round(empirical, 4) if empirical is not None else None,
            'exploration_bonus': round(bonus, 4),
            'floored': False}


def rank_workers(workers, priors=None, stats=None, safety_floor=SAFETY_FLOOR,
                 c=EXPLORATION_C, min_runs=MIN_RUNS):
    """Rank workers by adaptive weight (ties preserve input order); `grok-bot` always stays last."""
    priors = priors or {}
    stats = stats or {}
    total_runs = sum(max(0, int((stats.get(w) or {}).get('runs', 0)))
                     for w in workers if w != config.ORCHESTRATOR)
    details = {}
    pool = []
    tail = []
    for index, worker in enumerate(workers):
        if worker == config.ORCHESTRATOR:
            tail.append(worker)
            continue
        entry = stats.get(worker) or {}
        info = compute_weight(
            prior=priors.get(worker, 1.0),
            runs=entry.get('runs', 0),
            passed=entry.get('passed', 0),
            total_runs=total_runs,
            safety_floor=safety_floor,
            c=c,
            min_runs=min_runs,
        )
        details[worker] = info
        pool.append((worker, index, info))
    pool.sort(key=lambda item: (item[2]['floored'], -item[2]['weight'], item[1]))
    return {'order': [w for w, _, _ in pool] + tail, 'weights': details, 'total_runs': total_runs}
