"""Drift detection when a worker's quality drops (#70).

Compares a worker's rolling recent window (`recent_window = 5` outcomes) against its historical
baseline (`min_baseline_runs = 5` earlier outcomes) on pass rate or quality score. When the recent
mean drops by at least `drop_threshold` (`0.25` by default) below the baseline mean, or falls below
an absolute `quality_floor` (`0.40` by default) over `recent_window` runs, `detect_drift` flags
`drifted: True`, raises a deduplicated router alert via `alerts.raise_alert`, and returns a
demoted capability multiplier (`demotion_factor = 0.5`).
"""
from statistics import mean

from . import alerts

DEFAULT_RECENT_WINDOW = 5
DEFAULT_MIN_BASELINE = 5
DEFAULT_DROP_THRESHOLD = 0.25
DEFAULT_QUALITY_FLOOR = 0.40
DEMOTION_FACTOR = 0.5


def _extract_score(entry):
    if type(entry) in (int, float):
        return float(entry)
    if isinstance(entry, bool):
        return 1.0 if entry else 0.0
    if isinstance(entry, dict):
        if type(entry.get('quality')) in (int, float):
            return float(entry['quality'])
        if 'passed' in entry:
            return 1.0 if entry['passed'] else 0.0
        if 'ok' in entry:
            return 1.0 if entry['ok'] else 0.0
    return None


def detect_drift(worker, history, recent_window=DEFAULT_RECENT_WINDOW,
                 min_baseline_runs=DEFAULT_MIN_BASELINE, drop_threshold=DEFAULT_DROP_THRESHOLD,
                 quality_floor=DEFAULT_QUALITY_FLOOR, policy=None, task_class=None, notifier=None):
    """Evaluate a worker's outcome history for quality drift.

    Returns `{'worker', 'task_class', 'drifted', 'reason', 'baseline_mean', 'recent_mean', 'drop', 'multiplier'}`.
    """
    scores = [s for s in (_extract_score(item) for item in (history or ())) if s is not None]
    w_recent = max(2, int(recent_window))
    if len(scores) < w_recent:
        return {
            'worker': worker,
            'task_class': task_class,
            'drifted': False,
            'reason': 'insufficient_history',
            'baseline_mean': None,
            'recent_mean': round(mean(scores), 4) if scores else None,
            'drop': 0.0,
            'multiplier': 1.0,
        }
    recent = scores[-w_recent:]
    earlier = scores[:-w_recent]
    recent_mean = round(mean(recent), 4)
    baseline_mean = round(mean(earlier), 4) if len(earlier) >= max(1, int(min_baseline_runs)) else None
    drop = round((baseline_mean - recent_mean), 4) if baseline_mean is not None else 0.0

    reason = None
    if baseline_mean is not None and drop >= float(drop_threshold):
        reason = f'quality_drop ({baseline_mean:g} -> {recent_mean:g}, drop {drop:g} >= {drop_threshold:g})'
    elif recent_mean < float(quality_floor):
        reason = f'below_quality_floor (recent_mean {recent_mean:g} < {quality_floor:g})'

    drifted = reason is not None
    if drifted and policy is not None:
        scope = f'{worker}:{task_class}' if task_class else str(worker)
        alerts.raise_alert(
            policy,
            f'drift:{scope}:{recent_mean}',
            'drift',
            f'Worker quality drift detected for {scope}: {reason}',
            worker=worker,
            level='warn',
            notifier=notifier,
        )
    return {
        'worker': worker,
        'task_class': task_class,
        'drifted': drifted,
        'reason': reason,
        'baseline_mean': baseline_mean,
        'recent_mean': recent_mean,
        'drop': drop,
        'multiplier': DEMOTION_FACTOR if drifted else 1.0,
    }
