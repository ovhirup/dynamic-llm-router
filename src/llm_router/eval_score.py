"""Evaluation harness, part 2 (#63): scoring rubric, per-policy metrics and the adoption gate.

An outcome is one (case, policy) result: tests_status, changed_paths, reviewer_score (0..1 or None),
tokens, quota_percent and elapsed_seconds. Quality is a weighted mix of tests (must pass), diff scope
(overlap with the baseline's paths, or size when there is no baseline diff) and the reviewer score;
missing parts are dropped and the remaining weights renormalised. Cost and time are reported raw.

Gate: a candidate is adopted only if it does not regress pass rate or quality and is strictly better on
quality, tokens, quota % or time. Ties and missing evidence never adopt.
"""
from statistics import mean, median

WEIGHTS = {'tests': 0.5, 'scope': 0.2, 'reviewer': 0.3}
MAX_FILES = 20  # without a baseline diff, a change touching more files than this loses scope score
EPSILON = 1e-9


def scope_score(changed, expected=None, max_files=MAX_FILES):
    """1.0 for an exact match with the expected paths (Jaccard), else a size penalty; None if no change."""
    changed = set(changed or [])
    if not changed:
        return None
    if expected:
        expected = set(expected)
        return len(changed & expected) / len(changed | expected)
    return 1.0 if len(changed) <= max_files else max_files / len(changed)


def score(outcome, expected_paths=None, weights=None):
    """Score one outcome dict. Returns the outcome's metrics plus `quality` in [0, 1]."""
    weights = weights or WEIGHTS
    passed = outcome.get('tests_status') == 'passed'
    parts = {'tests': 1.0 if passed else 0.0,
             'scope': scope_score(outcome.get('changed_paths'), expected_paths),
             'reviewer': outcome.get('reviewer_score')}
    if parts['reviewer'] is not None:
        parts['reviewer'] = min(1.0, max(0.0, float(parts['reviewer'])))
    if not passed and parts['scope'] is not None:
        parts['scope'] = 0.0  # a change earns no scope credit when its tests fail
    used = {k: v for k, v in parts.items() if v is not None}
    total = sum(weights[k] for k in used)
    quality = sum(weights[k] * v for k, v in used.items()) / total if total else 0.0
    return {'case_id': outcome.get('case_id'), 'worker': outcome.get('worker'), 'passed': passed,
            'quality': round(quality, 4), 'parts': parts, 'tokens': outcome.get('tokens'),
            'quota_percent': outcome.get('quota_percent'),
            'quota_delta_status': outcome.get('quota_delta_status'),
            'quota_delta_reason': outcome.get('quota_delta_reason'),
            'elapsed_seconds': outcome.get('elapsed_seconds')}


def _numbers(rows, key):
    return [r[key] for r in rows if type(r.get(key)) in (int, float)]


def aggregate(scores):
    """Per-policy metrics over a list of score() results."""
    tokens, quota, times = _numbers(scores, 'tokens'), _numbers(scores, 'quota_percent'), _numbers(scores, 'elapsed_seconds')
    return {'cases': len(scores),
            'pass_rate': round(sum(s['passed'] for s in scores) / len(scores), 4) if scores else 0.0,
            'quality_mean': round(mean(s['quality'] for s in scores), 4) if scores else 0.0,
            'tokens_total': sum(tokens) if tokens else None,
            'tokens_mean': round(mean(tokens), 1) if tokens else None,
            'quota_percent_mean': round(mean(quota), 2) if quota else None,
            'elapsed_seconds_median': round(median(times), 1) if times else None,
            'elapsed_seconds_mean': round(mean(times), 1) if times else None}


def _lower(a, b):
    """True when b is strictly lower than a (both measured)."""
    return a is not None and b is not None and b < a - EPSILON


def gate(baseline, candidate):
    """Adoption verdict for candidate vs baseline aggregate metrics."""
    reasons = []
    if candidate['cases'] == 0 or candidate['cases'] != baseline['cases']:
        return {'adopt': False, 'reasons': ['case counts differ or are empty: no comparison']}
    if candidate['pass_rate'] < baseline['pass_rate'] - EPSILON:
        reasons.append('pass rate regressed')
    if candidate['quality_mean'] < baseline['quality_mean'] - EPSILON:
        reasons.append('quality regressed')
    if reasons:
        return {'adopt': False, 'reasons': reasons}
    wins = []
    if candidate['pass_rate'] > baseline['pass_rate'] + EPSILON:
        wins.append('higher pass rate')
    if candidate['quality_mean'] > baseline['quality_mean'] + EPSILON:
        wins.append('higher quality')
    if _lower(baseline['tokens_total'], candidate['tokens_total']):
        wins.append('fewer tokens')
    if _lower(baseline['quota_percent_mean'], candidate['quota_percent_mean']):
        wins.append('lower quota %')
    if _lower(baseline['elapsed_seconds_median'], candidate['elapsed_seconds_median']):
        wins.append('faster')
    return {'adopt': bool(wins), 'reasons': wins or ['no strict improvement over baseline']}
