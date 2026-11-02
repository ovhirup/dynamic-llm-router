"""Role-based routing (`planner`, `coder`, `searcher`, `judge`, `advisor`) and advisor escalation (#135).

Splits DLR work across five roles so scarce frontier allowances go to planning and judgment:
  - `planner`:  strongest available worker (`claude > grok-build > codex > cursor > grok-bot`)
  - `judge`:    strongest available worker (`claude > grok-build > codex > grok-bot`; never local)
  - `advisor`:  strongest available worker (`claude > grok-build > codex > grok-bot`; never local)
  - `coder`:    next worker with budget (`codex > grok-build > cursor > claude > aider-ollama > grok-bot`)
  - `searcher`: cheapest/local worker first (`aider-ollama > developer > cursor > codex > claude > grok-bot`)

Provides:
  1. `select_for_role(role, policy=None, eligible=None, meters=None)`: picks the first worker with
     live budget in the role's preference order, falling back cleanly when a preferred worker is out.
  2. `needs_advisor_escalation(errors=(), loop_detected=False, uncertain_done=False)`: triggers
     when repeated errors occur (>= 2 identical/similar errors), a loop is detected, or completion
     is uncertain (`"done?"`).
  3. `build_advisor_prompt(task_text, transcript_summary, errors=())`: concise escalation prompt
     whose guidance is fed back to the `coder`.
  4. `build_judge_prompt(diff_text, scope=(), test_summary='')`: "assume it's wrong" review prompt
     requiring one line per finding (`FILE:LINE — defect — local fix`) and `VERDICT: approve|reject`.
  5. `log_role_usage(policy, role, worker, tokens=None, job_id=None)`: appends role + worker + token
     usage to `<state>/logs/role_usage.jsonl` (`0600`) for burn-down tracking (#128).
"""
from datetime import datetime, timezone
import fcntl
import json
import os
import re

from . import config, security, selection

ROLES = ('planner', 'coder', 'searcher', 'judge', 'advisor')
NEVER_LOCAL_ROLES = ('judge', 'advisor')

DEFAULT_ROLE_ORDER = {
    'planner': ['claude', 'grok-build', 'codex', 'cursor', 'grok-bot'],
    'coder': ['codex', 'grok-build', 'cursor', 'claude', 'aider-ollama', 'grok-bot'],
    'searcher': ['aider-ollama', 'developer', 'cursor', 'codex', 'claude', 'grok-bot'],
    'judge': ['claude', 'grok-build', 'codex', 'grok-bot'],
    'advisor': ['claude', 'grok-build', 'codex', 'grok-bot'],
}

JUDGE_ASSUME_WRONG_RULES = """\
Judge rules (assume the diff has defects until proven otherwise):
1. Assume the implementation is wrong. Check edge cases, scope boundaries, and whether the tests
   genuinely fail without the fix.
2. Output at most one line per finding in the format:
   `path/to/file:line — defect — concrete local fix`
3. End with exactly one line:
   `VERDICT: approve` or `VERDICT: reject`
"""


def role_order(policy, role):
    """Return the configured or default worker preference list for `role`."""
    if role not in ROLES:
        raise config.ConfigError(f'unknown role {role!r}: use one of {", ".join(ROLES)}')
    custom = ((policy or {}).get('role_order') or {}).get(role)
    order = list(custom) if isinstance(custom, list) and custom else list(DEFAULT_ROLE_ORDER[role])
    if role in NEVER_LOCAL_ROLES:
        order = [w for w in order if w not in ('developer', 'aider-ollama', 'local', 'nativ')]
    if 'setup' in (policy or {}):
        from . import setup_profile
        enabled = setup_profile.validate_setup(policy['setup'])['enabled_workers']
        order = [worker for worker in order if worker in enabled]
        if config.ORCHESTRATOR in enabled and config.ORCHESTRATOR not in order:
            order.append(config.ORCHESTRATOR)
    elif config.ORCHESTRATOR not in order:
        order.append(config.ORCHESTRATOR)
    return order


def select_for_role(role, policy=None, eligible=None, meters=None, cached_only=True):
    """Pick the best worker for `role` given live budget (`eligible` and `meters`)."""
    policy = policy or config.load_policy()
    order = role_order(policy, role)
    if eligible is None:
        sel = selection.select(0, policy=policy, cached_only=cached_only)
        eligible = sel.get('eligible') or []
        meters = meters if meters is not None else (sel.get('meters') or {})
    else:
        meters = meters or {}
    profile = 'setup' in policy
    eligible_set = set(eligible) | (set() if profile else {config.ORCHESTRATOR})
    threshold = ((policy.get('run_task') or {}).get('pacing') or {}).get('refuse_full_percent', 85)
    candidates = []
    for worker in order:
        if worker == config.ORCHESTRATOR:
            if not profile or (worker in eligible_set and not config.blocked_worker(policy, worker)):
                candidates.append(worker)
            continue
        if config.blocked_worker(policy, worker):
            continue
        if worker not in eligible_set:
            continue
        used = meters.get(worker)
        if type(used) in (int, float) and used >= threshold:
            continue
        candidates.append(worker)
    chosen = candidates[0] if candidates else None if profile else config.ORCHESTRATOR
    return {
        'role': role,
        'worker': chosen,
        'candidates': candidates,
        'role_order': order,
        'reason': 'handoff_to_orchestrator' if chosen == config.ORCHESTRATOR else
                  'no_eligible_worker' if chosen is None else 'role_budget_match',
    }


def _norm_err(text):
    return re.sub(r'\s+', ' ', str(text or '').strip().lower())[:160]


def needs_advisor_escalation(errors=(), loop_detected=False, uncertain_done=False):
    """Return `{'escalate': bool, 'reason': str | None}`."""
    if loop_detected:
        return {'escalate': True, 'reason': 'loop_detected'}
    if uncertain_done:
        return {'escalate': True, 'reason': 'uncertain_completion'}
    cleaned = [_norm_err(e) for e in (errors or ()) if _norm_err(e)]
    if len(cleaned) >= 2 and (cleaned[-1] == cleaned[-2] or len(cleaned) != len(set(cleaned))):
        return {'escalate': True, 'reason': f'repeated_error: {security.redact(str(errors[-1]))[:120]}'}
    return {'escalate': False, 'reason': None}


def build_advisor_prompt(task_text, transcript_summary, errors=()):
    """Build the concise escalation prompt sent to the `advisor` role when a `coder` is stuck."""
    err_lines = '\n'.join(f'- {security.redact(str(e))}' for e in (errors or ())) or '- (see transcript summary)'
    return (
        '## Advisor Escalation\n'
        'The coding worker is stuck on repeated errors or a loop. Diagnose the root cause and reply '
        'with 3-5 concrete, actionable bullet points that the coder should execute next.\n\n'
        f'### Task\n{security.redact(str(task_text or "").strip())[:1500]}\n\n'
        f'### Repeated Errors\n{err_lines}\n\n'
        f'### Transcript Summary\n{security.redact(str(transcript_summary or "").strip())[:2000]}\n'
    )


def build_judge_prompt(diff_text, scope=(), test_summary=''):
    """Build the 'assume it is wrong' judge prompt with one line per finding."""
    scope_lines = '\n'.join(f'- `{p}`' for p in (scope or ())) or '- (task scope)'
    return (
        f'{JUDGE_ASSUME_WRONG_RULES}\n'
        f'## Declared Scope\n{scope_lines}\n\n'
        f'## Test Summary\n{security.redact(str(test_summary or "passed"))}\n\n'
        f'## Diff\n```diff\n{security.redact(str(diff_text or ""))[:30000]}\n```\n'
    )


def role_usage_path(policy=None):
    return config.policy_path(policy or {}, 'run_job', 'role_usage_file', 'logs/role_usage.jsonl')


def log_role_usage(policy, role, worker, tokens=None, job_id=None, now=None):
    """Append a redacted role usage record to `<state>/logs/role_usage.jsonl` (`0600`)."""
    if role not in ROLES:
        raise config.ConfigError(f'unknown role {role!r}')
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    entry = security.redact_obj({
        'at': stamp,
        'role': role,
        'worker': str(worker),
        'tokens': int(tokens) if type(tokens) in (int, float) else None,
        'job_id': str(job_id) if job_id else None,
    })
    path = role_usage_path(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(entry, sort_keys=True) + '\n')
    os.chmod(path, 0o600)
    return entry
