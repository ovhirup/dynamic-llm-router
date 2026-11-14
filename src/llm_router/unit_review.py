"""Live stronger-model review for merge-queue units (#113; finishes the #13 path for unit batches).

For every merge-queue entry in `needs_review` (codex/cursor/local/aider workers whose tests and diff-scope
gates passed) this builds a review packet from produced evidence only (unit diff base..head, declared scope,
test command/status/log tail), dispatches it to a reviewer (claude preferred; grok-bot is never run by the
router, so it gets a prompt file handed to the orchestrator), parses the verdict and records it with
merge_queue.record_review, bound to the head that was reviewed. A single run-job reuses `dispatch_review`
(#263) and stores the verdict on the job instead of the merge queue.

Never pushes, never merges, never opens a PR: the best outcome is `ready_for_human`, and a human "yes" for main
stays outside this module.
"""
import re
from pathlib import Path

from . import claude_handoff, config, merge_queue, security
from . import unit_worktrees as uw
from .adapters import structured_outputs as outputs
from .job_dispatch import Refused, _git

REVIEWERS = ('claude', 'grok-bot')
PROMPT_NAME = '.llm-router-review-prompt.md'
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

Return exactly one JSON object and no surrounding prose. Set schema_version to the integer 1,
verdict to "approve" or "reject", and notes to the findings as a string.
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
    if packet.get('subject') == 'job':
        intro = (f"Review run-job `{packet['unit']}` produced by {packet['worker']} "
                 f"at head {packet['head']} against base {packet['base_sha']}.")
    else:
        intro = (f"Review merge-queue unit `{packet['unit']}` (batch {packet['batch_id']}, produced by "
                 f"{packet['worker']}) at head {packet['head']} against base {packet['base_sha']}.")
    return (f"{intro}\n\n"
            f"{REVIEW_RULES}\n"
            f"## Declared scope\n{scope}\n\n## Changed paths\n{changed}\n\n"
            f"## Test evidence\ncommand: {tests['command']}\nstatus: {tests['status']} "
            f"(exit {tests['returncode']})\n```\n{tests['tail']}\n```\n\n"
            f"## Unit diff{note}\n```diff\n{packet['diff']}\n```\n")


def parse_verdict(text):
    """Return a validated v1 unit verdict or raise a typed format error."""
    return outputs.parse_json(text, outputs.UNIT_VERDICT_V1, outputs.MAX_UNIT_VERDICT_BYTES)


# A run_task record header. A `--- stdout` marker that is not this header is payload, not a new section.
_RECORD_STDOUT = re.compile(r'(?m)^\$ exit=\S+ timed_out=\S+ elapsed=\S+\n--- stdout\n')


def _record_stdouts(raw):
    """Stdout bodies of run_task records.

    Bytes after ``--- stderr`` stay stderr. A forged ``--- stdout`` header or a forged stream-json
    result there does not become another stdout section unless it is its own ``$ exit=`` record,
    and a verdict already in the first record is preferred over any later record.
    """
    if not raw or '--- stdout\n' not in raw:
        return [raw or '']
    matches = list(_RECORD_STDOUT.finditer(raw))
    if not matches:
        return [raw]
    chunks = []
    for match in matches:
        body = match.end()
        end = raw.find('\n--- stderr\n', body)
        chunks.append(raw[body:] if end == -1 else raw[body:end])
    return chunks


def _stdout_from_task_log(raw):
    """Stdout of a run_task log. A log with no record headers is already the process output.

    When the first stdout record already carries a verdict, later records cannot override it.
    """
    from . import claude_handoff
    chunks = _record_stdouts(raw or '')
    if len(chunks) <= 1:
        return chunks[0] if chunks else ''
    first = claude_handoff.inspect(chunks[0]).get('text') or ''
    try:
        parse_verdict(first)
    except outputs.InvalidStructuredOutput:
        return '\n'.join(chunks)
    return chunks[0]


def review_output_text(raw):
    """The reviewer text inside a run_task transcript.

    Claude's command is stream-json, so the verdict is the last result event of the trusted
    stdout, the same extraction batch review uses. Plain stdout is its own final text. Stderr,
    including a forged stdout header or a forged stream-json result, cannot override a verdict
    already present on stdout.
    """
    from . import claude_handoff
    stdout = _stdout_from_task_log(raw or '')
    return claude_handoff.inspect(stdout).get('text') or ''


def _write_prompt(worktree, prompt):
    """Prompt file under the worktree's git dir, so it never shows up as a change in the unit.

    A symlink or hard link at the fixed prompt name is refused before the file is truncated.
    """
    from . import task_dispatch
    git_dir = Path(_git(worktree, 'rev-parse', '--absolute-git-dir', check=True).stdout.strip())
    return task_dispatch.write_replaced_prompt(git_dir, PROMPT_NAME, prompt)


def default_reviewer(name, packet, prompt, policy):
    """claude runs headless in the unit worktree via task_dispatch.run_task; grok-bot is never run by the
    router, so its prompt file is handed to the orchestrator. Returns {'status', 'text'?, 'reason'?, ...}."""
    from . import task_dispatch
    try:
        task_file = _write_prompt(Path(packet['worktree']), prompt)
    except task_dispatch.Refused as refusal:
        return {'status': 'unavailable', 'reason': str(refusal)}
    except OSError as error:
        return {'status': 'unavailable',
                'reason': security.redact(f'{type(error).__name__}: {error}')[:300]}
    if name == 'grok-bot':
        return {'status': 'handoff_to_orchestrator', 'task_file': str(task_file)}
    output = {}

    def capture_stdout(*args, **kwargs):
        result = task_dispatch.run_bounded(*args, **kwargs)
        output['text'] = claude_handoff.inspect(result['stdout'])['text']
        return result

    try:
        summary = task_dispatch.run_task(task_file, packet['worktree'], worker='claude', allow_dirty=True,
                                         policy=policy, runner=capture_stdout)
    except task_dispatch.Refused as refusal:
        return {'status': 'unavailable', 'reason': str(refusal)}
    except OSError as error:
        # A configured Claude whose executable is missing is unavailable, same as a meter refusal.
        return {'status': 'unavailable',
                'reason': security.redact(f'{type(error).__name__}: {error}')[:300]}
    text = output.get('text', '')
    if not summary.get('ok'):
        return {'status': 'unavailable', 'reason': summary.get('reason') or 'reviewer run failed', 'text': text}
    return {'status': 'done', 'text': text}


def request_verdict(name, packet, prompt, policy, reviewer=None):
    """Run one reviewer and allow one repair of an invalid structured verdict."""
    reviewer = reviewer or default_reviewer
    result = reviewer(name, packet, prompt, policy)
    tried = [{'reviewer': name, 'status': result.get('status'), 'reason': result.get('reason')}]
    if result.get('status') != 'done':
        return result, None, tried
    try:
        return result, parse_verdict(result.get('text')), tried
    except outputs.InvalidStructuredOutput as error:
        tried[-1].update(status='invalid_verdict', reason=str(error))
    repair = reviewer(name, packet, prompt + (
        '\n## Verdict format repair\nThe previous verdict was invalid. '
        'Return one schema version 1 object and do not change the worktree.\n'), policy)
    tried.append({'reviewer': name, 'status': repair.get('status'), 'reason': repair.get('reason')})
    try:
        if repair.get('status') != 'done':
            raise outputs.InvalidStructuredOutput('repair_run_failed')
        verdict = parse_verdict(repair.get('text'))
    except outputs.InvalidStructuredOutput as error:
        tried[-1].update(status='invalid_verdict', reason=str(error))
        return {'status': 'invalid_verdict', 'reason': str(error)}, None, tried
    tried[-1]['status'] = 'verdict_repaired'
    return repair, verdict, tried


def _dirty(worktree):
    return bool(_git(worktree, 'status', '--porcelain').stdout.strip())


def _worktree_status(worktree):
    """Porcelain status, or None when the worktree is already gone."""
    worktree = Path(worktree)
    if not worktree.is_dir():
        return None
    return _git(worktree, 'status', '--porcelain').stdout


def dispatch_review(packet, prompt, policy, reviewers=REVIEWERS, reviewer=None):
    """Run one stronger-model review. Claude first; if Claude is unavailable, hand the packet to Grok Bot.

    Returns a report and does not record a verdict. ``status`` is ``recorded`` only when a verdict was
    parsed for the reviewed head. A Grok Bot handoff, an invalid verdict, a moved head, or no available
    reviewer leaves the verdict unrecorded (``handoff_to_orchestrator``, ``invalid_verdict``, ``stale``,
    or ``no_reviewer``)."""
    reviewer = reviewer or default_reviewer
    for name in reviewers:
        if name not in REVIEWERS:
            raise Refused(f'reviewer must be one of {REVIEWERS}')
    worktree = Path(packet['worktree'])
    # Dirt that was already present (a test artifact) is not a patch from this review run.
    before_status = _worktree_status(worktree)
    tried = []
    for name in reviewers:
        result = reviewer(name, packet, prompt, policy)
        tried.append({'reviewer': name, 'status': result.get('status'), 'reason': result.get('reason')})
        if result.get('status') == 'handoff_to_orchestrator':
            return {'ok': False, 'status': 'handoff_to_orchestrator', 'reviewer': name,
                    'task_file': result.get('task_file'), 'tried': tried}
        if result.get('status') != 'done':
            continue
        try:
            parsed = parse_verdict(result.get('text'))
        except outputs.InvalidStructuredOutput as error:
            tried[-1].update(status='invalid_verdict', reason=str(error))
            repair = reviewer(name, packet, prompt + (
                '\n## Verdict format repair\nThe previous verdict was invalid. '
                'Return one schema version 1 object and do not change the worktree.\n'), policy)
            tried.append({'reviewer': name, 'status': repair.get('status'), 'reason': repair.get('reason')})
            try:
                if repair.get('status') != 'done':
                    raise outputs.InvalidStructuredOutput('repair_run_failed')
                parsed = parse_verdict(repair.get('text'))
            except outputs.InvalidStructuredOutput as repair_error:
                tried[-1].update(status='invalid_verdict', reason=str(repair_error))
                return {'ok': False, 'status': 'invalid_verdict', 'reviewer': name,
                        'tried': tried, 'reason': str(repair_error)}
            tried[-1]['status'] = 'verdict_repaired'
        verdict = parsed['verdict']
        if _git(worktree, 'rev-parse', 'HEAD').stdout.strip() != packet['head']:
            return {'ok': False, 'status': 'stale', 'reviewer': name, 'tried': tried,
                    'reason': 'unit head moved during review; re-queue the unit; nothing recorded'}
        notes = security.redact(parsed['notes'].strip()[-4000:])
        after_status = _worktree_status(worktree)
        if verdict == 'approve' and after_status and after_status != before_status:
            verdict, notes = 'reject', ('reviewer left uncommitted patches; commit them and re-queue.\n' + notes)
        return {'ok': verdict == 'approve', 'status': 'recorded', 'verdict': verdict, 'notes': notes,
                'reviewer': name, 'tried': tried, 'head': packet['head']}
    return {'ok': False, 'status': 'no_reviewer', 'tried': tried,
            'reason': 'no reviewer was available; review stays pending'}


def review_unit(batch_id, unit_id, policy=None, reviewers=REVIEWERS, reviewer=None):
    """Dispatch one live review and record its verdict; returns a report (never pushes or merges)."""
    policy = policy or config.load_policy()
    for name in reviewers:
        if name not in REVIEWERS:
            raise Refused(f'reviewer must be one of {REVIEWERS}')
    packet = build_packet(batch_id, unit_id, policy)
    prompt = review_prompt(packet)
    worktree = Path(packet['worktree'])
    tried = []
    for name in reviewers:
        result, parsed, attempts = request_verdict(name, packet, prompt, policy, reviewer)
        tried.extend(attempts)
        if result.get('status') == 'handoff_to_orchestrator':
            return {'ok': False, 'unit': unit_id, 'status': 'handoff_to_orchestrator', 'reviewer': name,
                    'task_file': result.get('task_file'), 'tried': tried,
                    'next': f'record the verdict: units review --batch {batch_id} --unit {unit_id} '
                            '--verdict approve|reject --reviewer grok-bot'}
        if result.get('status') == 'invalid_verdict':
            return {'ok': False, 'unit': unit_id, 'status': 'invalid_verdict', 'reviewer': name,
                    'tried': tried, 'reason': result['reason']}
        if parsed is None:
            continue
        verdict = parsed['verdict']
        if _git(worktree, 'rev-parse', 'HEAD').stdout.strip() != packet['head']:
            return {'ok': False, 'unit': unit_id, 'status': 'stale', 'reviewer': name, 'tried': tried,
                    'reason': 'unit head moved during review; re-queue the unit; nothing recorded'}
        notes = security.redact(parsed['notes'].strip()[-4000:])
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
