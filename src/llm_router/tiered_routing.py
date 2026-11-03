"""Tiered model routing inside workers (#91): cheap scouts, normal coders, strong planners.

Decision ladder per step:
  1. `code_rule`:  deterministic rules in code (no model call, 0 tokens).
  2. `cheap`:      Haiku 5.5 (`claude-haiku-4-5` / `haiku`) or local `qwen2.5-coder:14b` for search,
                   routing, extraction, log/test triage, and pass/fail checks.
                   Hard guard: prompts for the cheap tier must stay under `CHEAP_BUDGET_LIMIT = 100_000`
                   (approx. `400_000` chars; Haiku costs 5x above 100K tokens).
  3. `normal`:     Sonnet (`claude-sonnet-4-5` / `sonnet`) for routine coding and unit implementation.
  4. `strong`:     Opus (`claude-opus-4-5` / `opus`) for planning, hard bug diagnosis, or low-confidence
                   escalation.
  5. `human`:      human gate for irreversible actions (`push`, `merge`, `approve`, secrets/config edits).

Never auto-writes user settings; ships opt-in `.claude/agents/Explore.md` (`model: haiku, effort: low,
read-only`) and `CLAUDE_CODE_SUBAGENT_MODEL=haiku` environment helper.
"""
from pathlib import Path

from . import security

TIERS = ('code_rule', 'cheap', 'normal', 'strong', 'human')
CHEAP_BUDGET_LIMIT = 100_000
CHARS_PER_UNIT = 4
CHEAP_MAX_CHARS = CHEAP_BUDGET_LIMIT * CHARS_PER_UNIT

STEP_TO_TIER = {
    'diff_scope': 'code_rule',
    'leak_check': 'code_rule',
    'risk_tier': 'code_rule',
    'search': 'cheap',
    'summarize': 'cheap',
    'classify': 'cheap',
    'test_triage': 'cheap',
    'extract': 'cheap',
    'code': 'normal',
    'implement': 'normal',
    'mechanical': 'cheap',
    'draft': 'cheap',
    'plan': 'strong',
    'hard_bug': 'strong',
    'escalate': 'strong',
    'review': 'strong',
    'push': 'human',
    'merge': 'human',
    'approve': 'human',
}

TIER_MODELS = {
    'code_rule': {'model': None, 'effort': None, 'quota_weight': 0.0},
    'cheap': {'model': 'haiku', 'local_model': 'qwen2.5-coder:14b', 'effort': 'low', 'quota_weight': 0.15},
    'normal': {'model': 'sonnet', 'effort': 'medium', 'quota_weight': 0.50},
    'strong': {'model': 'opus', 'effort': 'medium', 'quota_weight': 1.00},
    'human': {'model': None, 'effort': None, 'quota_weight': 0.0},
}


class TierGuardError(ValueError):
    """Cheap-tier prompt exceeds the 100K-token ceiling."""


def classify_step(step_kind, confidence=1.0, irreversible=False):
    """Map a step kind + confidence to `(tier, metadata)` on the decision ladder."""
    if irreversible or step_kind in ('push', 'merge', 'approve'):
        return 'human', dict(TIER_MODELS['human'], tier='human')
    tier = STEP_TO_TIER.get(str(step_kind or '').strip().lower(), 'normal')
    if tier in ('cheap', 'normal') and type(confidence) in (int, float) and confidence < 0.5:
        tier = 'strong'
    return tier, dict(TIER_MODELS[tier], tier=tier)


def estimate_tokens(text):
    return max(0, (len(text or '') + CHARS_PER_UNIT - 1) // CHARS_PER_UNIT)


def check_cheap_tier_prompt(prompt, max_tokens=CHEAP_BUDGET_LIMIT):
    """Enforce the 100K-token guard on cheap-tier prompts (Haiku 5.5 costs 5x above 100K tokens)."""
    tokens = estimate_tokens(prompt)
    if tokens > max_tokens:
        raise TierGuardError(
            f'cheap-tier prompt has ~{tokens} tokens (> {max_tokens} token limit); '
            'pass only the question + relevant files, never the full transcript')
    return tokens


def claude_tiered_options(tier='normal', advisor_opus=False):
    """Opt-in CLI flags and env vars for a tiered Claude worker run (never modifies user settings)."""
    if tier not in ('cheap', 'normal', 'strong'):
        raise ValueError(f'tier must be cheap, normal, or strong (got {tier!r})')
    info = TIER_MODELS[tier]
    flags = ['--model', info['model'], '--effort', info['effort']]
    if advisor_opus and info['model'] != 'opus':
        flags += ['--advisor', 'opus']
    env = {'CLAUDE_CODE_SUBAGENT_MODEL': 'haiku'}
    return {'tier': tier, 'flags': flags, 'env': env}


def explore_agent_path():
    return Path(__file__).resolve().parent / 'examples' / 'claude' / 'agents' / 'Explore.md'


def log_task_cost(task_id, worker, tier, tokens, quota_percent, pass_status):
    """Build a per-task cost record (logged per task rather than per token) for meters and eval harness."""
    return security.redact_obj({
        'task_id': str(task_id),
        'worker': str(worker),
        'tier': str(tier),
        'tokens': int(tokens) if type(tokens) in (int, float) else 0,
        'quota_percent': round(float(quota_percent), 4) if type(quota_percent) in (int, float) else 0.0,
        'passed': bool(pass_status),
    })


def compare_tiered_vs_single_model(tasks):
    """Compare a tiered run (routing search/triage to `cheap`, coding to `normal`, planning to `strong`)
    against a single-model (`strong` Opus-only) run on the same tasks, reporting quota used and pass rate.
    """
    single_records, tiered_records = [], []
    for item in tasks or ():
        tid = item.get('task_id') or 'task'
        steps = item.get('steps') or [{'kind': 'code', 'tokens': 10_000}]
        passed = item.get('passed', True)
        single_tokens = 0
        single_quota = 0.0
        tiered_tokens = 0
        tiered_quota = 0.0
        for step in steps:
            step_tokens = int(step.get('tokens', 5000))
            tier, meta = classify_step(step.get('kind', 'code'), confidence=step.get('confidence', 1.0))
            single_tokens += step_tokens
            single_quota += (step_tokens / 100_000.0) * TIER_MODELS['strong']['quota_weight'] * 2.0
            if tier != 'code_rule':
                tiered_tokens += step_tokens
                tiered_quota += (step_tokens / 100_000.0) * meta['quota_weight'] * 2.0
        single_records.append(log_task_cost(tid, 'claude', 'strong', single_tokens, single_quota, passed))
        tiered_records.append(log_task_cost(tid, 'claude', 'tiered', tiered_tokens, tiered_quota, passed))

    n = len(tasks or ()) or 1
    single_pass = round(sum(1 for r in single_records if r['passed']) / n, 4)
    tiered_pass = round(sum(1 for r in tiered_records if r['passed']) / n, 4)
    single_q = round(sum(r['quota_percent'] for r in single_records), 4)
    tiered_q = round(sum(r['quota_percent'] for r in tiered_records), 4)
    return {
        'cases': len(tasks or ()),
        'single_model': {'pass_rate': single_pass, 'quota_percent_total': single_q, 'tasks': single_records},
        'tiered': {'pass_rate': tiered_pass, 'quota_percent_total': tiered_q, 'tasks': tiered_records},
        'quota_saved_percent': round(single_q - tiered_q, 4),
        'beats_single_model': tiered_pass >= single_pass and tiered_q < single_q,
    }
