"""Router Brain: Obsidian vault skeleton and redacted job outcomes (docs/ROUTER_BRAIN.md section c).

init_vault() materialises jobs/, handoffs/, scorecards/, research/ and rules.md idempotently.
write_job_outcome() writes <vault>/jobs/<job_id>.md and mirrors handoff notes, with redaction.
"""
import json
from pathlib import Path
import re

from . import config, security

VAULT_DIRS = ('jobs', 'handoffs', 'scorecards', 'research')
RULES_FILE = 'rules.md'
DEFAULT_VAULT_DIR = 'vault'
FRONTMATTER_KEYS = ('job_id', 'repo', 'task', 'status', 'ok', 'workers', 'tests_status', 'branch', 'commit',
                    'started_at', 'finished_at')

DEFAULT_RULES = """# Standing rules

Always loaded for every DLR worker and routing decision. Keep this list between 10 and 15 rules.

1. Never push, merge, or open a PR without explicit human approval.
2. Tests must pass with 0 failures and 0 skipped before a unit is reported done.
3. Stay within the declared file scope of the unit; out-of-scope edits are refused by the merge queue.
4. Cite a source (eval run id or URL) for every capability claim; unsourced claims are ignored.
5. Redact secrets, tokens, API keys, and personal data from logs, notes, and handoffs.
6. Report what was observed (command and actual output), never what was intended.
7. Never overwrite or delete user edits in the vault or the main checkout.
8. Work only inside the assigned git worktree; never touch the main checkout's tree, index, or branch.
9. Write a handoff note at every checkpoint and when stopping, so another worker can continue.
10. Prefer the cheapest worker that meets the task class's measured pass rate; escalate only on evidence.
11. Route on measured scorecards and eval results, never on hype or vendor claims alone.
12. Stop and escalate after the same verification fails twice; do not loop blind.
13. Do not bump dependencies, edit CI, or change auth/permissions without human sign-off.
"""


def vault_dir(policy=None):
    """Vault path from brain.vault_dir or run_job.vault_dir (default vault under state dir)."""
    if not policy:
        return Path(DEFAULT_VAULT_DIR)
    explicit = (policy.get('brain') or {}).get('vault_dir')
    if explicit:
        return config.resolve(explicit)
    if policy.get('run_job') is not None or policy.get('version') is not None:
        return config.policy_path(policy, 'run_job', 'vault_dir', 'vault')
    return Path(DEFAULT_VAULT_DIR)


def init_vault(path=None, policy=None):
    """Create the vault skeleton; return created paths. Never overwrites existing files."""
    root = Path(path).expanduser() if path is not None else vault_dir(policy)
    if root.exists() and not root.is_dir():
        raise NotADirectoryError(f'vault path is not a directory: {root}')
    created = []
    if not root.exists():
        root.mkdir(parents=True)
        created.append(root)
    for name in VAULT_DIRS:
        sub = root / name
        if not sub.is_dir():
            sub.mkdir(parents=True)
            created.append(sub)
    rules = root / RULES_FILE
    try:
        with rules.open('x') as fh:
            fh.write(DEFAULT_RULES)
        created.append(rules)
    except FileExistsError:
        pass
    return created


def _private_terms(policy):
    path = config.policy_path(policy, 'run_task', 'private_terms_file', 'private_terms.txt')
    return security.load_terms(path) or []


def _scrubber(policy):
    terms = sorted(_private_terms(policy), key=len, reverse=True)
    pattern = re.compile('|'.join(re.escape(term) for term in terms), re.IGNORECASE) if terms else None

    def scrub(text):
        text = security.redact('' if text is None else str(text))
        return pattern.sub(security.REDACTED, text) if pattern else text
    return scrub


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def _yaml_value(value):
    if isinstance(value, list):
        return '[' + ', '.join(json.dumps(str(item)) for item in value) + ']'
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    return json.dumps(str(value))


def render_outcome(summary, scrub, mirrored=()):
    lines = ['---']
    for key in FRONTMATTER_KEYS:
        value = summary.get(key)
        value = [scrub(item) for item in value] if isinstance(value, list) else (
            value if isinstance(value, bool) or value is None else scrub(value))
        lines.append(f'{key}: {_yaml_value(value)}')
    lines += ['---', '', f'# Job {scrub(summary.get("job_id"))}', '', '## Summary', '']
    lines += [f'- Status: {scrub(summary.get("status"))} (ok: {bool(summary.get("ok"))})',
              f'- Task: {scrub(summary.get("task"))}',
              f'- Branch: {scrub(summary.get("branch"))}',
              f'- Commit: {scrub(summary.get("commit") or "none")}',
              f'- Workers: {scrub(", ".join(summary.get("workers") or []) or "none")}']
    if summary.get('reason'):
        lines.append(f'- Reason: {scrub(summary["reason"])}')
    lines += ['', '## Hops', '']
    hops = summary.get('hops') or []
    lines += [f'- cycle {hop.get("cycle")}: {scrub(hop.get("worker"))} -> {scrub(hop.get("reason"))}'
              f' (ok: {bool(hop.get("ok"))})' for hop in hops] or ['- none']
    lines += ['', '## Tests', '', f'- Status: {scrub(summary.get("tests_status"))}',
              f'- Command: {scrub(summary.get("test_command") or "none")}', '', '## Diff stat', '']
    diff = (summary.get('diff_stat') or '').strip()
    lines += ['```', scrub(diff), '```'] if diff else ['- none']
    lines += ['', '## Handoffs', '']
    lines += [f'- [[{scrub(Path(path).stem)}]] ({scrub(path)})' for path in mirrored] or ['- none']
    lines += ['', '## Warnings', '']
    lines += [f'- {scrub(warning)}' for warning in summary.get('warnings') or []] or ['- none']
    return '\n'.join(lines) + '\n'


def write_job_outcome(policy, summary):
    """Write <vault>/jobs/<job_id>.md and mirror handoff notes; returns the note path, or None if refused."""
    vault = vault_dir(policy)
    for forbidden in filter(None, (summary.get('repo'), summary.get('worktree'))):
        if _inside(vault, forbidden):
            summary.setdefault('warnings', []).append('vault_dir is inside the target repository: outcome note skipped')
            return None
    scrub = _scrubber(policy)
    job_id = Path(str(summary['job_id'])).name
    mirrored = []
    handoffs = [entry.get('path') for entry in summary.get('handoffs') or [] if entry.get('path')]
    if handoffs:
        target = vault / 'handoffs' / job_id
        target.mkdir(parents=True, exist_ok=True)
        for source in handoffs:
            source = Path(source)
            try:
                text = source.read_text()
            except OSError:
                continue
            dest = target / source.name
            dest.write_text(scrub(text))
            mirrored.append(str(dest))
    note = vault / 'jobs' / f'{job_id}.md'
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(render_outcome(summary, scrub, mirrored))
    return note


__all__ = ['vault_dir', 'init_vault', 'write_job_outcome', 'render_outcome', 'FRONTMATTER_KEYS', 'VAULT_DIRS']
