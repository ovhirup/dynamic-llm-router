"""Live stronger-model review for merge-queue units (#113; finishes the #13 path for unit batches).

For every merge-queue entry in `needs_review` (codex/cursor/local/aider workers whose tests and diff-scope
gates passed) this builds a review packet from produced evidence only (unit diff base..head, declared scope,
test command/status/log tail), dispatches it to a reviewer (claude preferred; grok-bot is never run by the
router, so it gets a prompt file handed to the orchestrator), parses the verdict and records it with
merge_queue.record_review, bound to the head that was reviewed.

Never pushes, never merges, never opens a PR: the best outcome is `ready_for_human`, and a human "yes" for main
stays outside this module.
"""
from pathlib import Path
import re

from . import config, merge_queue, security
from . import unit_worktrees as uw
from .job_dispatch import Refused, _git

REVIEWERS = ('claude', 'grok-bot')
PROMPT_NAME = '.llm-router-review-prompt.md'
VERDICT_LINE = re.compile(r'^\s*VERDICT:\s*(approve|reject)\s*$', re.I | re.M)
MAX_DIFF_CHARS = 60_000

REVIEW_RULES = """\
Reviewer rules (binding):
1. Judge only the produced diff and the test output below. Never judge, trust or repeat the worker's stated
   intent, commit message claims or summaries.
2. Name at most the 3 highest-impact defects. For each give file:line evidence from the diff and a local fix.
3. If you patch, patch only those sections, then re-run the affected tests and report their real output.
   Leave patches uncommitted; never commit, push, merge or open a PR. A patched unit is not approved as-is:
   answer reject so the patch is committed and the unit re-gated.
4. A missing required input (diff, scope, test evidence) is a blocker: answer reject and name it. Never invent
   missing evidence. "Command exited 0" alone is not done; the tests must actually exercise the change.
5. Approve only when the diff stays in scope, the tests cover the change, and you found no blocking defect.

End your answer with exactly one line, either:
VERDICT: approve
VERDICT: reject
"""


def _entry(batch_id, unit_id, policy):
    record = merge_queue.load_queue(batch_id, policy)['entries'].get(unit_id)
    if record is None:
        raise Refused(f'unit {unit_id!r} is not in the merge queue for {batch_id}')
    return record


def build_packet(batch_id, unit_id, policy=None):
    """Evidence for one needs_review unit. Missing evidence is a Refused blocker, never filled in."""
    policy = policy or config.load_policy()
    record = _entry(batch_id, unit_id, policy)
    if record.get('status') != 'needs_review':
        raise Refused(f'unit {unit_id!r} is {record.get("status")}, not needs_review')
    repo = Path(uw.load_manifest(batch_id, policy)['repo'])
    missing = [key for key in ('base_sha', 'head', 'scope') if not record.get(key)]
    tests = record.get('tests') or {}
    if tests.get('status') != 'passed' or not tests.get('command'):
        missing.append('passing test evidence')
    if missing:
        raise Refused(f'unit {unit_id!r} review blocked: missing {", ".join(missing)}')
    diff = _git(repo, 'diff', '--no-renames', '--no-color', f"{record['base_sha']}..{record['head']}",
                check=True).stdout
    if not diff.strip():
        raise Refused(f'unit {unit_id!r} review blocked: empty diff')
    log_tail = tests.get('tail', '')
    if not log_tail and tests.get('log') and Path(tests['log']).is_file():
        log_tail = Path(tests['log']).read_text()[-4000:]
    truncated = len(diff) > MAX_DIFF_CHARS
    return {'batch_id': batch_id, 'unit': unit_id, 'worker': record['worker'], 'head': record['head'],
            'base_sha': record['base_sha'], 'branch': record.get('branch'), 'worktree': record.get('worktree'),
            'scope': list(record['scope']), 'changed': (record.get('diff_scope') or {}).get('changed', []),
            'diff': security.redact(diff[:MAX_DIFF_CHARS]), 'diff_truncated': truncated,
            'tests': {'command': tests['command'], 'status': tests['status'],
                      'returncode': tests.get('returncode'), 'tail': security.redact(log_tail)}}


def review_prompt(packet):
    scope = '\n'.join(f'- {p}' for p in packet['scope'])
    changed = '\n'.join(f'- {p}' for p in packet['changed']) or '- (none recorded)'
    tests = packet['tests']
    note = f"\n(diff truncated to {MAX_DIFF_CHARS} chars; treat the unseen part as missing evidence)\n" \
        if packet['diff_truncated'] else ''
    return (f"Review merge-queue unit `{packet['unit']}` (batch {packet['batch_id']}, produced by "
            f"{packet['worker']}) at head {packet['head']} against base {packet['base_sha']}.\n\n"
            f"{REVIEW_RULES}\n"
            f"## Declared scope\n{scope}\n\n## Changed paths\n{changed}\n\n"
            f"## Test evidence\ncommand: {tests['command']}\nstatus: {tests['status']} "
            f"(exit {tests['returncode']})\n```\n{tests['tail']}\n```\n\n"
            f"## Unit diff{note}\n```diff\n{packet['diff']}\n```\n")


def parse_verdict(text):
    """The last `VERDICT: approve|reject` line, or None when absent (never guessed)."""
    found = VERDICT_LINE.findall(text or '')
    return found[-1].lower() if found else None


def _write_prompt(worktree, prompt):
    """Prompt file under the worktree's git dir, so it never shows up as a change in the unit."""
    git_dir = Path(_git(worktree, 'rev-parse', '--absolute-git-dir', check=True).stdout.strip())
    path = git_dir / PROMPT_NAME
    path.write_text(prompt)
    path.chmod(0o600)
    return path


def default_reviewer(name, packet, prompt, policy):
    """claude runs headless in the unit worktree via task_dispatch.run_task; grok-bot is never run by the
    router, so its prompt file is handed to the orchestrator. Returns {'status', 'text'?, 'reason'?, ...}."""
    from . import task_dispatch
    task_file = _write_prompt(Path(packet['worktree']), prompt)
    if name == 'grok-bot':
        return {'status': 'handoff_to_orchestrator', 'task_file': str(task_file)}
    try:
        summary = task_dispatch.run_task(task_file, packet['worktree'], worker='claude', allow_dirty=True,
                                         policy=policy)
    except task_dispatch.Refused as refusal:
        return {'status': 'unavailable', 'reason': str(refusal)}
    output = summary.get('output_path')
    text = Path(output).read_text() if output and Path(output).is_file() else ''
    if not summary.get('ok'):
        return {'status': 'unavailable', 'reason': summary.get('reason') or 'reviewer run failed', 'text': text}
    return {'status': 'done', 'text': text}


def _dirty(worktree):
    return bool(_git(worktree, 'status', '--porcelain').stdout.strip())


def review_unit(batch_id, unit_id, policy=None, reviewers=REVIEWERS, reviewer=None):
    """Dispatch one live review and record its verdict; returns a report (never pushes or merges)."""
    policy = policy or config.load_policy()
    reviewer = reviewer or default_reviewer
    for name in reviewers:
        if name not in REVIEWERS:
            raise Refused(f'reviewer must be one of {REVIEWERS}')
    packet = build_packet(batch_id, unit_id, policy)
    prompt = review_prompt(packet)
    worktree = Path(packet['worktree'])
    tried = []
    for name in reviewers:
        result = reviewer(name, packet, prompt, policy)
        tried.append({'reviewer': name, 'status': result.get('status'), 'reason': result.get('reason')})
        if result.get('status') == 'handoff_to_orchestrator':
            return {'ok': False, 'unit': unit_id, 'status': 'handoff_to_orchestrator', 'reviewer': name,
                    'task_file': result.get('task_file'), 'tried': tried,
                    'next': f'record the verdict: units review --batch {batch_id} --unit {unit_id} '
                            '--verdict approve|reject --reviewer grok-bot'}
        if result.get('status') != 'done':
            continue
        verdict = parse_verdict(result.get('text'))
        if verdict is None:
            return {'ok': False, 'unit': unit_id, 'status': 'no_verdict', 'reviewer': name, 'tried': tried,
                    'reason': 'reviewer output has no VERDICT line; nothing recorded'}
        if _git(worktree, 'rev-parse', 'HEAD').stdout.strip() != packet['head']:
            return {'ok': False, 'unit': unit_id, 'status': 'stale', 'reviewer': name, 'tried': tried,
                    'reason': 'unit head moved during review; re-queue the unit; nothing recorded'}
        notes = security.redact((result.get('text') or '').strip()[-4000:])
        if verdict == 'approve' and worktree.is_dir() and _dirty(worktree):
            verdict, notes = 'reject', ('reviewer left uncommitted patches; commit them and re-queue.\n' + notes)
        entry = merge_queue.record_review(batch_id, unit_id, verdict, notes=notes, policy=policy,
                                          reviewer=name, head=packet['head'])
        return {'ok': entry['status'] == 'ready_for_human', 'unit': unit_id, 'status': entry['status'],
                'verdict': verdict, 'reviewer': name, 'tried': tried, 'entry': entry}
    return {'ok': False, 'unit': unit_id, 'status': 'no_reviewer', 'tried': tried,
            'reason': 'no reviewer was available; review stays pending'}


def review_batch(batch_id, unit_id=None, policy=None, reviewers=REVIEWERS, reviewer=None):
    """Review every needs_review entry of a batch (or one unit)."""
    policy = policy or config.load_policy()
    entries = merge_queue.load_queue(batch_id, policy)['entries']
    if unit_id is not None:
        _entry(batch_id, unit_id, policy)
        units = [unit_id]
    else:
        units = [uid for uid, e in entries.items() if e.get('status') == 'needs_review']
    reports = [review_unit(batch_id, uid, policy, reviewers, reviewer) for uid in units]
    return {'batch_id': batch_id, 'reviewed': reports, 'ok': all(r['ok'] for r in reports)}
