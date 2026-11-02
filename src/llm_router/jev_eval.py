"""Compare Jev vs heuristic classifier on DLR eval cases (#60).

Parent: #57 (Jev decision layer spike).
Evaluates two task-class/escalation classifiers (`heuristic` vs `jev` or a candidate classifier)
against labeled DLR benchmark cases (`case_id`, `task_text`, `expected_class`, `expected_size`),
reporting:
  - `class_accuracy`
  - `size_accuracy`
  - `brier_score` (calibration error: lower is better)
  - `escalation_rate`
  - `adopt`: `True` only when the candidate strictly improves accuracy (or matches accuracy with
    strictly better calibration / lower Brier score) without regressing.
"""
from statistics import mean

from . import config

EPSILON = 1e-9

BENCHMARK_CASES = [
    {
        'case_id': 'eval-feature-1',
        'task_text': '# Add bot-dispatch command\nImplement bot-dispatch --class --size in cli.py and job_dispatch.py.',
        'expected_class': 'feature',
        'expected_size': 'full',
    },
    {
        'case_id': 'eval-bugfix-1',
        'task_text': '# Fix crash bug in parser\nFix regression where parse() raises ValueError on empty input.',
        'expected_class': 'bugfix',
        'expected_size': 'full',
    },
    {
        'case_id': 'eval-review-1',
        'task_text': '# Review merge-queue diff\nInspect the diff, audit test coverage, and output VERDICT: approve or reject.',
        'expected_class': 'review',
        'expected_size': 'full',
    },
    {
        'case_id': 'eval-mechanical-1',
        'task_text': '# Rename helper and format imports\nMechanical cleanup and typo fix across helper modules.',
        'expected_class': 'mechanical',
        'expected_size': 'full',
    },
    {
        'case_id': 'eval-draft-1',
        'task_text': '# Draft short release summary\nWrite a short draft note summarizing the wording changes.',
        'expected_class': 'draft',
        'expected_size': 'draft',
    },
]

_CUES = {
    'bugfix': ('fix', 'bug', 'error', 'crash', 'regression', 'fail', 'broken'),
    'review': ('review', 'audit', 'inspect', 'verdict', 'approve', 'reject'),
    'mechanical': ('rename', 'format', 'lint', 'typo', 'cleanup', 'mechanical'),
    'draft': ('draft', 'wording', 'short', 'summary', 'note'),
    'feature': ('add', 'implement', 'create', 'build', 'support', 'feature'),
}


def default_heuristic_classifier(task_text):
    lower = (task_text or '').lower()
    raw = {}
    for cls in config.TASK_CLASSES:
        hits = sum(1 for w in _CUES.get(cls, ()) if w in lower)
        raw[cls] = 1.5 * hits + (0.3 if cls == 'feature' else 0.1)
    total = sum(raw.values()) or 1.0
    probs = {cls: round(raw[cls] / total, 4) for cls in config.TASK_CLASSES}
    chosen = max(config.TASK_CLASSES, key=lambda c: probs[c])
    conf = min(0.95, round(max(probs[chosen], 0.50 + 0.15 * sum(1 for w in _CUES[chosen] if w in lower)), 4))
    size = 'draft' if chosen == 'draft' else 'full'
    return {
        'task_class': chosen,
        'size': size,
        'confidence': conf,
        'probabilities': probs,
        'escalated': conf < 0.70,
    }


def score_classifier(cases, classifier_fn):
    """Evaluate `classifier_fn(task_text)` across `cases`."""
    rows = []
    for case in cases or ():
        pred = classifier_fn(case['task_text'])
        expected_cls = case['expected_class']
        expected_size = case.get('expected_size') or ('draft' if expected_cls == 'draft' else 'full')
        class_ok = pred.get('task_class') == expected_cls
        size_ok = (pred.get('size') or 'full') == expected_size
        probs = pred.get('probabilities') or {}
        p_true = float(probs.get(expected_cls, pred.get('confidence', 0.5) if class_ok else 1.0 - float(pred.get('confidence', 0.5))))
        brier = (1.0 - min(1.0, max(0.0, p_true))) ** 2
        rows.append({
            'case_id': case.get('case_id'),
            'expected_class': expected_cls,
            'predicted_class': pred.get('task_class'),
            'class_correct': class_ok,
            'size_correct': size_ok,
            'confidence': round(float(pred.get('confidence', 0.0)), 4),
            'brier': round(brier, 4),
            'escalated': bool(pred.get('escalated')),
        })
    n = len(rows) or 1
    return {
        'cases': len(rows),
        'class_accuracy': round(sum(1 for r in rows if r['class_correct']) / n, 4),
        'size_accuracy': round(sum(1 for r in rows if r['size_correct']) / n, 4),
        'brier_score': round(mean(r['brier'] for r in rows), 4) if rows else 1.0,
        'escalation_rate': round(sum(1 for r in rows if r['escalated']) / n, 4),
        'predictions': rows,
    }


def compare_jev_vs_heuristic(cases=None, heuristic_fn=None, jev_fn=None):
    """Compare `jev_fn` against `heuristic_fn` on `cases` and return the adoption gate verdict."""
    cases = list(cases if cases is not None else BENCHMARK_CASES)
    heuristic_fn = heuristic_fn or default_heuristic_classifier
    jev_fn = jev_fn or heuristic_fn
    base = score_classifier(cases, heuristic_fn)
    cand = score_classifier(cases, jev_fn)

    reasons = []
    if cand['class_accuracy'] < base['class_accuracy'] - EPSILON:
        reasons.append('class accuracy regressed')
    if cand['size_accuracy'] < base['size_accuracy'] - EPSILON:
        reasons.append('size accuracy regressed')
    if reasons:
        return {'adopt': False, 'reasons': reasons, 'heuristic': base, 'jev': cand}

    wins = []
    if cand['class_accuracy'] > base['class_accuracy'] + EPSILON:
        wins.append('higher class accuracy')
    if cand['size_accuracy'] > base['size_accuracy'] + EPSILON:
        wins.append('higher size accuracy')
    if cand['brier_score'] < base['brier_score'] - EPSILON:
        wins.append('better probability calibration (lower Brier score)')
    return {
        'adopt': bool(wins),
        'reasons': wins or ['jev did not strictly beat the zero-cost local heuristic; keep heuristic'],
        'heuristic': base,
        'jev': cand,
    }
