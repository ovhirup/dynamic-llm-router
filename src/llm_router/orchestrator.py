"""Dynamic orchestrator (#120): whoever gets the prompt orchestrates, with budget-aware handoff.

1. Detects the active orchestrator tool (`cursor`, `claude`, `codex`, `grok-build`, `grok-bot`)
   from an explicit `--orchestrator` flag, `LLM_ROUTER_ORCHESTRATOR` env var, or the calling
   process environment (`CURSOR_*`, `CLAUDECODE` / `CLAUDE_CODE_*`, `CODEX_*`, `GROK_*`), falling
   back to `grok-bot`.
2. Records `orchestrator` and `orchestrator_source` on every `run-job` and `units run` summary.
3. Monitors the active orchestrator's live quota/meter against `handoff_threshold_percent` (default 85%):
   when the orchestrator's meter is at or above threshold (or exhausted/cooled down), writes a
   markdown handoff note and selects the next eligible orchestrator following `worker_order` + `grok-bot`.
4. Exposes `status_report(policy, ...)` for `llm-router orchestrator status`.
"""
from datetime import datetime, timezone
import os
from pathlib import Path

from . import config, handoff, selection

SUPPORTED_ORCHESTRATORS = ('cursor', 'claude', 'codex', 'grok-build', 'grok-bot')
ENV_DETECTORS = (
    ('cursor', ('CURSOR_TRACE_ID', 'CURSOR_SESSION_ID', 'CURSOR_AGENT', 'CURSOR_WORKSPACE')),
    ('claude', ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_SESSION_ID')),
    ('codex', ('CODEX_SANDBOX', 'CODEX_THREAD_ID', 'CODEX_HOME_SESSION')),
    ('grok-build', ('GROK_BUILD_SESSION', 'GROK_SESSION_ID')),
    ('grok-bot', ('GROK_BOT_SESSION',)),
)


def detect_orchestrator(explicit=None, env=None):
    """Return `{'orchestrator': name, 'reason': why}` from explicit arg, environment, or default."""
    if explicit:
        norm = str(explicit).strip().lower()
        if norm not in SUPPORTED_ORCHESTRATORS:
            raise config.ConfigError(
                f'unknown orchestrator {explicit!r}; choose from {", ".join(SUPPORTED_ORCHESTRATORS)}')
        return {'orchestrator': norm, 'reason': 'explicit_flag'}
    environ = env if env is not None else os.environ
    override = (environ.get('LLM_ROUTER_ORCHESTRATOR') or '').strip().lower()
    if override:
        if override not in SUPPORTED_ORCHESTRATORS:
            raise config.ConfigError(
                f'unknown LLM_ROUTER_ORCHESTRATOR {override!r}; choose from {", ".join(SUPPORTED_ORCHESTRATORS)}')
        return {'orchestrator': override, 'reason': 'env:LLM_ROUTER_ORCHESTRATOR'}
    user_agent = (environ.get('LLM_ROUTER_USER_AGENT') or environ.get('TERM_PROGRAM') or '').strip().lower()
    for name in SUPPORTED_ORCHESTRATORS:
        if name in user_agent:
            return {'orchestrator': name, 'reason': f'user_agent:{name}'}
    for name, keys in ENV_DETECTORS:
        for key in keys:
            if environ.get(key):
                return {'orchestrator': name, 'reason': f'env:{key}'}
    return {'orchestrator': config.ORCHESTRATOR, 'reason': 'default_fallback'}


def fallback_chain(policy, current=None):
    """Ordered fallback chain of orchestrators: `worker_order` tools + `grok-bot` last, excluding `current`."""
    order = [w for w in (policy.get('worker_order') or []) if w in SUPPORTED_ORCHESTRATORS]
    for tool in SUPPORTED_ORCHESTRATORS:
        if tool not in order and tool != config.ORCHESTRATOR:
            order.append(tool)
    if config.ORCHESTRATOR not in order:
        order.append(config.ORCHESTRATOR)
    return [w for w in order if w != current]


def check_budget_handoff(policy, orchestrator_name=None, meters=None, eligible=None,
                         goal='', handoffs_dir=None, now=None):
    """Check whether `orchestrator_name` is low on budget (`>= handoff_threshold_percent`).

    When low on budget, writes a markdown handoff note and returns the next eligible orchestrator.
    """
    detected = detect_orchestrator(orchestrator_name)
    current = detected['orchestrator']
    meters = meters if meters is not None else {}
    used = meters.get(current)
    if used is None:
        used = handoff.meter_percent(current)
    limit = handoff.threshold(policy)
    chain = fallback_chain(policy, current=current)
    if eligible is not None:
        eligible_set = set(eligible) | {config.ORCHESTRATOR}
        candidates = [w for w in chain if w in eligible_set]
    else:
        candidates = [w for w in chain if not config.blocked_worker(policy, w)]
    next_orch = candidates[0] if candidates else config.ORCHESTRATOR
    low_budget = type(used) in (int, float) and used >= limit
    note_path = None
    if low_budget:
        target_dir = Path(handoffs_dir) if handoffs_dir is not None else \
            config.policy_path(policy, 'run_job', 'handoffs_dir', 'handoffs') / 'orchestrator'
        note = {
            'job_id': 'orchestrator-handoff',
            'worker': current,
            'source': 'router',
            'switch_reason': f'orchestrator_low_budget ({used:g}% >= {limit}%)',
            'sections': {
                'Goal': goal or f'Continue orchestration from {current} -> {next_orch}.',
                'Files touched': 'none',
                'Diff summary': 'orchestrator handoff',
                'Test status': 'not_run',
                'Decisions': f'Current orchestrator `{current}` reached {used:g}% usage (>= {limit}%).',
                'Next steps': f'Hand off orchestration to `{next_orch}`.',
                'Gotchas': 'Never push to main, merge, or touch secrets without human approval.',
                'Meter': f'{used:g}%',
                'Switch reason': f'orchestrator_low_budget ({used:g}% >= {limit}%)',
            },
        }
        note_path = str(handoff.write_note(target_dir, note, now=now or datetime.now(timezone.utc)))
    return {
        'orchestrator': current,
        'reason': detected['reason'],
        'meter_percent': used,
        'threshold_percent': limit,
        'handoff_needed': low_budget,
        'next_orchestrator': next_orch if low_budget else None,
        'fallback_chain': candidates,
        'handoff_note': note_path,
    }


def status_report(policy=None, explicit=None, env=None, cached_only=True, meters=None):
    """Status payload for `llm-router orchestrator status`."""
    policy = policy or config.load_policy()
    detected = detect_orchestrator(explicit=explicit, env=env)
    sel = selection.select(0, policy=policy, cached_only=cached_only)
    merged_meters = dict(sel.get('meters') or {})
    if meters:
        merged_meters.update(meters)
    budget = check_budget_handoff(
        policy,
        orchestrator_name=detected['orchestrator'],
        meters=merged_meters,
        eligible=sel.get('eligible'),
    )
    return {
        'orchestrator': detected['orchestrator'],
        'reason': detected['reason'],
        'meter_percent': budget['meter_percent'],
        'threshold_percent': budget['threshold_percent'],
        'handoff_needed': budget['handoff_needed'],
        'next_orchestrator': budget['next_orchestrator'],
        'fallback_chain': budget['fallback_chain'],
        'handoff_note': budget['handoff_note'],
        'eligible_workers': sel.get('eligible') or [],
    }
