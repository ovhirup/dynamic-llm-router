"""Router brain: redacted markdown job outcomes in the Obsidian vault (docs/ROUTER_BRAIN.md section c).

Layout: <vault_dir>/jobs/<job_id>.md (one outcome note per run-job) and <vault_dir>/handoffs/<job_id>/
(copies of the job's handoff notes). vault_dir is `brain.vault_dir` or `run_job.vault_dir` (default
`vault` under the state dir). Never writes inside the target repository or the job's worktree; every
line of text goes through security.redact and the private-terms mask first.
"""
import json
from pathlib import Path
import re

from . import config, security

FRONTMATTER_KEYS = ('job_id', 'repo', 'task', 'status', 'ok', 'workers', 'tests_status', 'branch', 'commit',
                    'started_at', 'finished_at')


def vault_dir(policy):
    explicit = (policy.get('brain') or {}).get('vault_dir')
    if explicit:
        return config.resolve(explicit)
    return config.policy_path(policy, 'run_job', 'vault_dir', 'vault')


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


__all__ = ['vault_dir', 'write_job_outcome', 'render_outcome', 'FRONTMATTER_KEYS']
