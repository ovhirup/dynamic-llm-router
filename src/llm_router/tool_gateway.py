"""Permissioned tool gateway and 6-layer harness permission ladder (#146).

Separates `propose`, `authorize`, and `execute` across a 5-step permission ladder encoded in code
and policy (`default_policy.json` `tool_gateway`):
  - `read`:            `auto`
  - `worktree_write`:  `auto` (bounded to the job worktree)
  - `git_commit`:      `auto` (local worktree branch only, never `main`)
  - `publish_or_cfg`:  `needs_human_approval` (`push`, `merge`, `pr`, `config.json`, `repos.json`, `.env`, `~/.codex`)
  - `destructive`:     `always_human` (`delete`, `force_push`, `reset_hard`, `rm_rf_outside`)

Also provides:
  - `resolve_worker_permission_level(policy, worker)`: maps each worker's CLI flags/sandbox (`codex`
    `workspace-write` + `approval_policy=never`, `claude` `acceptEdits`, `cursor` `--workspace`,
    `grok-build` `--cwd`) to its resolved permission profile (`worktree_bounded`, never `full_access`)
    so every worker launch records its resolved permission level in the job record.
  - `classify_failure(result, tests=None)`: classifies failed checks into one of
    `missing_context` | `wrong_tool` | `missing_capability` | `flaky_env` | `model_error`
    with a concrete recovery path.
  - `structured_tool_result(command, raw_result, max_chars=2000)`: wraps router-run command output
    into a bounded, redacted observation dict with `failure_class`.
"""
import re

from . import security

ACTION_CLASSES = ('read', 'worktree_write', 'git_commit', 'publish_or_cfg', 'destructive')
AUTHORIZATION_LEVELS = ('auto', 'needs_human_approval', 'always_human')
FAILURE_CLASSES = ('missing_context', 'wrong_tool', 'missing_capability', 'flaky_env', 'model_error')

DEFAULT_PERMISSION_LADDER = {
    'read': 'auto',
    'worktree_write': 'auto',
    'git_commit': 'auto',
    'publish_or_cfg': 'needs_human_approval',
    'destructive': 'always_human',
}

PUBLISH_ACTIONS = {'push', 'merge', 'pr', 'approve', 'config_edit', 'secrets_read', 'codex_home'}
DESTRUCTIVE_ACTIONS = {'delete', 'force_push', 'reset_hard', 'rm_rf_outside', 'branch_delete'}


class GatewayRefused(ValueError):
    """Action refused by the permissioned tool gateway."""


def ladder_for(policy=None):
    custom = ((policy or {}).get('tool_gateway') or {}).get('ladder') or {}
    merged = dict(DEFAULT_PERMISSION_LADDER)
    for key, val in custom.items():
        if key in ACTION_CLASSES and val in AUTHORIZATION_LEVELS:
            merged[key] = val
    # Hard safety invariant: publish_or_cfg and destructive can NEVER be downgraded to 'auto'.
    merged['publish_or_cfg'] = 'needs_human_approval'
    merged['destructive'] = 'always_human'
    return merged


def classify_action(action, target=''):
    act = str(action or '').strip().lower()
    tgt = str(target or '').strip().lower()
    if act in DESTRUCTIVE_ACTIONS or '--force' in tgt or 'reset --hard' in tgt:
        return 'destructive'
    if (act in PUBLISH_ACTIONS
            or tgt in ('main', 'master', 'config.json', 'repos.json', '.env')
            or '/.codex' in tgt or '~/.codex' in tgt or '/.config/llm-router' in tgt):
        return 'publish_or_cfg'
    if act in ('git_commit', 'commit'):
        return 'git_commit'
    if act in ('write', 'edit', 'worktree_write'):
        return 'worktree_write'
    return 'read'


def authorize_action(action, policy=None, target='', approved=False):
    """Separate propose -> authorize -> execute. Raises `GatewayRefused` when unapproved."""
    ladder = ladder_for(policy)
    action_class = classify_action(action, target=target)
    required = ladder[action_class]
    if required in ('needs_human_approval', 'always_human') and not approved:
        raise GatewayRefused(
            f'gateway refused {action!r} (class={action_class}, level={required}): explicit human approval required')
    return {
        'action': str(action),
        'target': security.redact(str(target)),
        'action_class': action_class,
        'permission_level': required,
        'authorized': True,
        'approved_by_human': bool(approved),
    }


def resolve_worker_permission_level(policy, worker):
    """Resolve and validate the sandbox/permission profile for `worker` (never `full_access` by default)."""
    if worker == 'developer':
        return {'worker': worker, 'level': 'draft_only', 'sandbox': 'http_draft', 'max_auto_class': 'read'}
    argv = ((policy or {}).get('run_task') or {}).get('commands', {}).get(worker) or []
    joined = ' '.join(str(a) for a in argv)
    if 'danger-full-access' in joined or '--dangerously-skip-permissions' in joined:
        level = 'elevated_unrestricted'
    else:
        level = 'worktree_bounded'
    sandbox = (
        'workspace-write' if 'workspace-write' in joined
        else 'acceptEdits' if 'acceptEdits' in joined
        else 'workspace-scoped' if ('--workspace' in joined or '--cwd' in joined)
        else 'worktree-cwd'
    )
    return {
        'worker': str(worker),
        'level': level,
        'sandbox': sandbox,
        'max_auto_class': 'git_commit',
        'human_required_classes': ['publish_or_cfg', 'destructive'],
    }


def classify_failure(run_record=None, tests=None):
    """Classify a failed worker run or test run into one of the 5 harness failure classes."""
    run_record = run_record or {}
    tests = tests or {}
    combined = ' '.join([
        str(run_record.get('failure_reason') or ''),
        str(run_record.get('reason') or ''),
        str(tests.get('tail') or ''),
        str(tests.get('status') or ''),
    ]).lower()

    if run_record.get('timed_out') or run_record.get('stalled') or tests.get('status') == 'timeout' or \
            re.search(r'\b(connection reset|econnrefused|temporary failure|timed out|broken pipe)\b', combined):
        return {
            'failure_class': 'flaky_env',
            'recovery': 'retry_or_failover_with_fresh_worktree_env',
        }
    if run_record.get('quota_error') or run_record.get('auth_error') or \
            re.search(r'\b(not found on path|command not found|no module named|importerror|model_not_present)\b', combined):
        return {
            'failure_class': 'missing_capability',
            'recovery': 'switch_to_next_capable_worker_with_budget',
        }
    if re.search(r'\b(filenotfounderror|no such file|keyerror|undefined name|nameerror)\b', combined):
        return {
            'failure_class': 'missing_context',
            'recovery': 'attach_repo_map_and_context_navigator_files',
        }
    if re.search(r'\b(invalid option|unrecognized arguments|usage:|forbidden argument|blocked)\b', combined):
        return {
            'failure_class': 'wrong_tool',
            'recovery': 'use_configured_test_or_git_contract_command',
        }
    return {
        'failure_class': 'model_error',
        'recovery': 'retry_with_converging_expected_saw_feedback_or_escalate_to_advisor',
    }


def structured_tool_result(command, raw_result, max_chars=2000):
    """Convert a raw `run_bounded` dict into a bounded, redacted, structured observation."""
    raw_result = raw_result or {}
    stdout = security.redact((raw_result.get('stdout') or '')[-max_chars:])
    stderr = security.redact((raw_result.get('stderr') or '')[-max_chars:])
    ok = raw_result.get('returncode') == 0 and not raw_result.get('timed_out')
    classification = None if ok else classify_failure(
        {'timed_out': raw_result.get('timed_out'), 'stalled': raw_result.get('stalled'),
         'failure_reason': stderr or stdout},
    )
    return {
        'command': security.redact(str(command or '')),
        'ok': bool(ok),
        'returncode': raw_result.get('returncode'),
        'timed_out': bool(raw_result.get('timed_out')),
        'stalled': bool(raw_result.get('stalled')),
        'elapsed_seconds': raw_result.get('elapsed_seconds'),
        'stdout_tail': stdout,
        'stderr_tail': stderr,
        'failure_class': classification['failure_class'] if classification else None,
        'recovery': classification['recovery'] if classification else None,
    }
