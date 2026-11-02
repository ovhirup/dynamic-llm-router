"""Worker triage block (#116): print and parse Size, Tests, Agents, and Branch before work.

Workers emit a short 4-line triage block before editing:
  Size: small | medium | large — why
  Tests: which suite — why
  Agents: solo | fan-out — why
  Branch: <branch> in <worktree>

DLR guardrails always win:
  - The router always runs the repo's configured/detected test command; triage may add tests
    but never reduce below the required suite, and skipped tests never count as passing.
  - Workers never push; scope stays strictly to the task file.
"""
from pathlib import Path
import re

from . import security

TRIAGE_INSTRUCTIONS = (
    'Triage block: before editing, print a 4-line triage block in this exact format:\n'
    'Size: small | medium | large — <why>\n'
    'Tests: <which suite> — <why>\n'
    'Agents: solo | fan-out — <why>\n'
    'Branch: <branch> in <worktree>\n'
    '(Guardrails: the router always runs the required repo test command and skipped tests never pass; '
    'do not push; keep scope to the task file.)'
)

_FIELD_RE = {
    'size': re.compile(r'^\s*Size:\s*(.+?)\s*$', re.I | re.M),
    'tests': re.compile(r'^\s*Tests:\s*(.+?)\s*$', re.I | re.M),
    'agents': re.compile(r'^\s*Agents:\s*(.+?)\s*$', re.I | re.M),
    'branch': re.compile(r'^\s*Branch:\s*(.+?)\s*$', re.I | re.M),
}
_SPLIT_WHY = re.compile(r'\s*(?:—|--|-)\s*', re.U)


def _split_value_why(raw):
    parts = _SPLIT_WHY.split((raw or '').strip(), maxsplit=1)
    value = security.redact(parts[0].strip())
    why = security.redact(parts[1].strip()) if len(parts) > 1 else ''
    return value, why


def parse_triage(text):
    """Parse a worker's stdout/log for a triage block; return dict or None when absent."""
    if not text:
        return None
    found = {}
    for key, pattern in _FIELD_RE.items():
        matches = pattern.findall(text)
        if matches:
            found[key] = matches[-1].strip()
    if not {'size', 'tests', 'agents', 'branch'} <= set(found):
        return None
    size_val, size_why = _split_value_why(found['size'])
    tests_val, tests_why = _split_value_why(found['tests'])
    agents_val, agents_why = _split_value_why(found['agents'])
    branch_raw = security.redact(found['branch'])
    return {
        'size': size_val.lower(),
        'size_why': size_why,
        'tests': tests_val,
        'tests_why': tests_why,
        'agents': agents_val.lower(),
        'agents_why': agents_why,
        'branch': branch_raw,
        'raw': '\n'.join([
            f"Size: {security.redact(found['size'])}",
            f"Tests: {security.redact(found['tests'])}",
            f"Agents: {security.redact(found['agents'])}",
            f"Branch: {branch_raw}",
        ]),
    }


def extract_from_runs(runs):
    """Extract the latest parsed triage block from a list of worker run records, or None."""
    for run in reversed(runs or ()):
        if isinstance(run, dict) and isinstance(run.get('triage'), dict):
            return run['triage']
        out_path = run.get('output_path') if isinstance(run, dict) else None
        if out_path and Path(out_path).is_file():
            try:
                parsed = parse_triage(Path(out_path).read_text(errors='replace'))
            except OSError:
                parsed = None
            if parsed is not None:
                return parsed
    return None


def format_for_handoff(triage):
    if not isinstance(triage, dict):
        return 'none recorded'
    return triage.get('raw') or (
        f"Size: {triage.get('size')}\n"
        f"Tests: {triage.get('tests')}\n"
        f"Agents: {triage.get('agents')}\n"
        f"Branch: {triage.get('branch')}"
    )
