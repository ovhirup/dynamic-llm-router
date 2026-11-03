"""Decision-model router (#114): score workers and task classes with one-pass P(yes) (DecisionRAG).

Runs in shadow mode by default:
  - Scores eligible candidate workers (never `grok-bot`, never blocked/cooled/over-quota workers) and
    task classes (`feature`, `bugfix`, `review`, `mechanical`, `draft`) with a single shared-context
    P(yes) decision pass (or local lexical fallback when no external decision model is configured).
  - Hard gates (`worker_order`, quota, cooldowns, and `grok-bot` excluded from the parallel pool)
    always win.
  - Logs `shadow_pick`, `p_yes`, `predicted_class`, and `agrees_with_selected` on `selection.select()`,
    `run_job` results, and `eval_harness` reports (`shadow_agreement_rate`).
"""
import re

from . import config

CLASS_KEYWORDS = {
    'bugfix': {'fix', 'bug', 'error', 'crash', 'regression', 'fail', 'broken', 'traceback', 'exception'},
    'review': {'review', 'audit', 'inspect', 'verdict', 'approve', 'reject', 'diff'},
    'mechanical': {'rename', 'format', 'lint', 'typo', 'bump', 'cleanup', 'mechanical', 'move'},
    'draft': {'draft', 'wording', 'copy', 'summary', 'note', 'readme', 'doc', 'docs'},
    'feature': {'add', 'implement', 'create', 'build', 'support', 'feature', 'introduce', 'new'},
}
WORKER_AFFINITY = {
    'claude': {'feature': 0.92, 'bugfix': 0.90, 'review': 0.94, 'mechanical': 0.72, 'draft': 0.80},
    'codex': {'feature': 0.82, 'bugfix': 0.86, 'review': 0.78, 'mechanical': 0.88, 'draft': 0.70},
    'grok-build': {'feature': 0.85, 'bugfix': 0.84, 'review': 0.82, 'mechanical': 0.86, 'draft': 0.74},
    'cursor': {'feature': 0.80, 'bugfix': 0.78, 'review': 0.72, 'mechanical': 0.84, 'draft': 0.75},
    'aider-ollama': {'feature': 0.45, 'bugfix': 0.48, 'review': 0.25, 'mechanical': 0.82, 'draft': 0.85},
    'developer': {'feature': 0.20, 'bugfix': 0.20, 'review': 0.20, 'mechanical': 0.65, 'draft': 0.92},
}
WORD_RE = re.compile(r'[a-z][a-z0-9_-]{1,30}')


def _words(text):
    return set(WORD_RE.findall((text or '').lower()))


def local_fallback_model(task_text, candidates, meters=None):
    """Local deterministic P(yes) scorer when no external decision model is configured."""
    words = _words(task_text)
    meters = meters or {}
    class_scores = {}
    for cls in config.TASK_CLASSES:
        hits = len(words & CLASS_KEYWORDS.get(cls, set()))
        base = 0.55 if cls == 'feature' else 0.35
        class_scores[cls] = min(0.99, round(base + 0.12 * hits, 4))
    best_class = max(config.TASK_CLASSES, key=lambda c: class_scores[c])

    worker_scores = {}
    for worker in candidates:
        if worker == config.ORCHESTRATOR:
            continue
        affinity = (WORKER_AFFINITY.get(worker) or {}).get(best_class, 0.65)
        used = meters.get(worker)
        headroom_factor = (1.0 - (float(used) / 100.0) * 0.25) if type(used) in (int, float) else 0.95
        worker_scores[worker] = min(0.99, max(0.01, round(affinity * headroom_factor, 4)))
    return {'class_p_yes': class_scores, 'worker_p_yes': worker_scores, 'source': 'local_fallback'}


def score_candidates(task_text, eligible_workers, meters=None, task_class=None,
                     selected_worker=None, model=None):
    """Score only `eligible_workers` (excluding `grok-bot`) with one-pass P(yes).

    Hard gates (`eligible_workers`) are strictly respected: a worker not in `eligible_workers`
    is never scored or picked, and `grok-bot` is never in the candidate pool.
    """
    pool = [w for w in (eligible_workers or ()) if w != config.ORCHESTRATOR]
    if model is not None:
        try:
            raw = model(task_text, pool, meters=meters or {})
        except Exception:
            raw = None
    else:
        raw = None
    if not isinstance(raw, dict) or 'worker_p_yes' not in raw:
        raw = local_fallback_model(task_text, pool, meters=meters)
    class_p = {c: round(float((raw.get('class_p_yes') or {}).get(c, 0.5)), 4) for c in config.TASK_CLASSES}
    predicted_class = task_class or max(config.TASK_CLASSES, key=lambda c: class_p[c])
    worker_p = {
        w: round(float((raw.get('worker_p_yes') or {}).get(w, 0.5)), 4)
        for w in pool
    }
    ranked = sorted(pool, key=lambda w: (-worker_p[w], pool.index(w)))
    shadow_pick = ranked[0] if ranked else None
    agrees = (shadow_pick == selected_worker) if (shadow_pick is not None and selected_worker is not None) else None
    return {
        'mode': 'shadow',
        'source': raw.get('source', 'decision_model'),
        'predicted_class': predicted_class,
        'class_p_yes': class_p,
        'worker_p_yes': worker_p,
        'ranked_workers': ranked,
        'shadow_pick': shadow_pick,
        'selected_worker': selected_worker,
        'agrees_with_selected': agrees,
    }


def agreement_rate(records):
    """Fraction of records where `shadow_pick == selected_worker` (or None when no comparable records)."""
    comparable = [r for r in (records or ()) if isinstance(r, dict) and type(r.get('agrees_with_selected')) is bool]
    if not comparable:
        return None
    return round(sum(1 for r in comparable if r['agrees_with_selected']) / len(comparable), 4)
