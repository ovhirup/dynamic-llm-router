"""Week-curve for the grok-bot orchestrator seat (Phase 1). Pure functions: no I/O, no CLIs, no network.

The grok-bot seat draws on a weekly pool. Workers tagged `shares_pool_with: "grok-bot"` (grok-build is
always treated as tagged) may share that pool. Per docs/STRATEGY_WORKERS_FALLBACK.md the family is paced,
never banned for an unknown meter. Policy `grok_bot.billing.mode` decides how:

- "unknown" (default): pool members are tried last among cloud workers (after every other cloud worker,
  before local workers and the orchestrator fallback).
- "shared":  the pool belongs to the orchestrator seat; pool members are never execution workers.
- "separate": pool members are paced by week_budget() over the grok-bot evidence.

week_budget(evidence, now, reserve=15) paces spending evenly across the 7 days before reset_at:
  spendable    = 100 - reserve                       (85 by default)
  allowed_used = spendable * elapsed fraction of the period
  headroom     = allowed_used - percent_used
  headroom <= 0 before the last 24h -> ahead of curve: drop one rank (0% used is never "ahead")
  in the last 24h the floor drops to 5: on curve while percent_used < 95, ineligible at/above it
  missing reset_at, a bad meter, or a passed reset -> unknown: tried last among cloud workers
  percent_used at 100 -> exhausted, ineligible until reset_at
evidence: {'percent_used': number 0..100, 'reset_at': aware datetime or ISO string}
"""
from datetime import datetime, timedelta
import math

POOL = 'grok-bot'
PERIOD = timedelta(days=7)
LAST_DAY = timedelta(hours=24)
LAST_DAY_FLOOR = 5
DEFAULT_RESERVE = 15
MODES = ('unknown', 'shared', 'separate')
# gate() verdicts for pool members that passed their own meter checks.
FIRST_FIT, TRY_LAST, DROP_RANK, INELIGIBLE = 'first_fit', 'try_last', 'drop_rank', 'ineligible'


def _when(value):
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if type(value) is not str or len(value) > 40:
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, OverflowError):
        return None
    return result if result.tzinfo is not None else None


def _unknown(why):
    return {'state': 'unknown', 'eligible': False, 'reason': why, 'percent_used': None,
            'allowed_used': None, 'headroom': None, 'floor': None, 'reset_at': None}


def week_budget(evidence, now, reserve=DEFAULT_RESERVE):
    """{'state', 'eligible', 'reason', 'percent_used', 'allowed_used', 'headroom', 'floor', 'reset_at'}."""
    if type(reserve) not in (int, float) or not 0 <= reserve < 100:
        return _unknown('bad_reserve')
    if not isinstance(evidence, dict):
        return _unknown('no_evidence')
    used = evidence.get('percent_used')
    if type(used) not in (int, float) or not math.isfinite(used) or not 0 <= used <= 100:
        return _unknown('bad_meter')
    if evidence.get('reset_at') is None:
        return _unknown('missing_reset_at')
    reset = _when(evidence['reset_at'])
    if reset is None:
        return _unknown('bad_reset_at')
    if now >= reset:
        return _unknown('reset_passed_needs_new_reading')
    start = reset - PERIOD
    if now < start:
        return _unknown('reset_beyond_period')
    spendable = 100 - reserve
    elapsed = (now - start) / PERIOD
    allowed = spendable * elapsed
    headroom = allowed - used
    last_day = reset - now <= LAST_DAY
    floor = LAST_DAY_FLOOR if last_day else reserve
    base = {'percent_used': used, 'allowed_used': round(allowed, 4), 'headroom': round(headroom, 4),
            'floor': floor, 'reset_at': reset.isoformat()}
    if used >= 100:
        return dict(base, state='exhausted', eligible=False, reason='exhausted_until_reset')
    if last_day:
        ok = used < 100 - LAST_DAY_FLOOR
        return dict(base, state='last_day', eligible=ok, reason='reserve_released' if ok else 'below_last_day_floor')
    if headroom <= 0 and used > 0:
        return dict(base, state='ahead_of_curve', eligible=False, reason='no_headroom')
    return dict(base, state='on_curve', eligible=True, reason='headroom')


def settings(policy):
    raw = (policy or {}).get('grok_bot') or {}
    mode = (raw.get('billing') or {}).get('mode', 'unknown')
    return {'mode': mode if mode in MODES else 'unknown', 'reserve': raw.get('reserve_percent', DEFAULT_RESERVE),
            'evidence': raw.get('evidence')}


def pool_members(policy):
    """Workers that share the grok-bot pool: grok-build always, plus any tagged shares_pool_with grok-bot."""
    tagged = {name for name, opts in ((policy or {}).get('workers') or {}).items()
              if isinstance(opts, dict) and opts.get('shares_pool_with') == POOL}
    return tagged | {'grok-build'}


def gate(policy, now, evidence=None):
    """(budget, verdict, reason) for every grok-bot pool member under the configured billing mode.

    verdict: FIRST_FIT (keep rank), TRY_LAST (after other cloud workers), DROP_RANK (one place down),
    INELIGIBLE (shared pool, exhausted, or over the last-day floor). Unknown never means banned.
    """
    cfg = settings(policy)
    budget = week_budget(evidence if evidence is not None else cfg['evidence'], now, cfg['reserve'])
    budget['mode'] = cfg['mode']
    if cfg['mode'] == 'unknown':
        return budget, TRY_LAST, 'grok_pool_unknown'
    if cfg['mode'] == 'shared':
        return budget, INELIGIBLE, 'grok_pool_shared'
    why = 'grok_week_curve_' + budget['reason']
    if budget['state'] == 'unknown':
        return budget, TRY_LAST, why
    if budget['state'] == 'ahead_of_curve':
        return budget, DROP_RANK, why
    return budget, FIRST_FIT if budget['eligible'] else INELIGIBLE, None if budget['eligible'] else why


def pace(eligible, members, verdict, local=()):
    """Reorder an eligible list for pool `members` under a gate() verdict; returns a new list.

    TRY_LAST puts members after every other cloud worker but before `local` workers; DROP_RANK swaps
    each member one place down, never below a local worker or another member; INELIGIBLE removes them.
    """
    if verdict == INELIGIBLE:
        return [w for w in eligible if w not in members]
    if verdict == TRY_LAST:
        cloud = [w for w in eligible if w not in members and w not in local]
        return cloud + [w for w in eligible if w in members] + [w for w in eligible if w in local]
    result = list(eligible)
    if verdict == DROP_RANK:
        for i in range(len(result) - 2, -1, -1):
            if result[i] in members and result[i + 1] not in members and result[i + 1] not in local:
                result[i], result[i + 1] = result[i + 1], result[i]
    return result
