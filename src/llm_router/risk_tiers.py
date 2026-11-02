"""Approval gates by price of undo (#105): risk tiers decided in code, not model confidence.

Classifies changed paths and diffs into three deterministic risk tiers:
  - `low`:    only docs (`docs/**`, `*.md`, `LICENSE`, `CHANGELOG.md`) or tests (`tests/**`, `test_*.py`)
  - `medium`: ordinary source files (`src/**`, `pyproject.toml`, etc.)
  - `high`:   config (`config.json`, `repos.json`, `default_policy.json`, `.env*`), security/auth/gate
              (`security.py`, `pr_gate.py`, `claude_account.py`), CI/workflows (`.github/**`), or any diff
              containing destructive/push/merge commands (`git push`, `gh pr merge`, `rm -rf`).

Policy in code decides `approval_required`:
  - `high` and `medium` always require human approval (`approval_required: True`).
  - `low` also defaults to `approval_required: True` unless `policy['pr_gate']['auto_approve_low_risk']`
    is explicitly `True` (off by default).
Once a unit/job passes its gate at a given `head` SHA, `is_frozen(entry, current_head)` keeps it frozen
so it is not re-run when a sibling fails.
"""
from pathlib import Path
import re

TIERS = ('low', 'medium', 'high')

HIGH_RISK_FILENAMES = {
    'config.json',
    'repos.json',
    'default_policy.json',
    'private_terms.txt',
    'security.py',
    'pr_gate.py',
    'claude_account.py',
}
HIGH_RISK_PREFIXES = ('.github/', '.env')
LOW_RISK_DIRS = ('docs', 'tests', 'test', 'vault')
LOW_RISK_SUFFIXES = ('.md', '.txt', '.rst')
HIGH_RISK_DIFF_RE = re.compile(
    r'(?i)(?:git[\s"\',]+push\b|gh[\s"\',]+pr[\s"\',]+(?:merge|review[\s"\',]+--approve)\b|git[\s"\',]+reset[\s"\',]+--hard\b|rm[\s"\',]+-rf\b)'
)


def classify_path(path):
    """Return 'low', 'medium', or 'high' for a single repo-relative path."""
    raw = str(path or '').strip()
    while raw.startswith('./'):
        raw = raw[2:]
    if not raw:
        return 'low'
    name = Path(raw).name
    parts = Path(raw).parts
    if (name in HIGH_RISK_FILENAMES
            or (name.startswith('.env') and name != '.env.example')
            or raw.startswith(HIGH_RISK_PREFIXES)
            or '.github' in parts):
        return 'high'
    if (parts[0] in LOW_RISK_DIRS
            or name.startswith('test_')
            or name.endswith(('_test.py', '.md'))
            or name in ('README.md', 'CHANGELOG.md', 'LICENSE', 'CONTRIBUTING.md')):
        return 'low'
    return 'medium'


def classify_diff(changed_paths, diff_text='', policy=None):
    """Compute `{'risk_tier', 'approval_required', 'reasons', 'path_tiers'}` from paths and diff text.

    Model confidence is never consulted; only code policy determines `risk_tier` and `approval_required`.
    """
    paths = sorted(set(changed_paths or ()))
    path_tiers = {p: classify_path(p) for p in paths}
    reasons = []
    high_paths = [p for p, t in path_tiers.items() if t == 'high']
    med_paths = [p for p, t in path_tiers.items() if t == 'medium']
    if high_paths:
        reasons.append('high_risk_paths: ' + ', '.join(high_paths))
    if HIGH_RISK_DIFF_RE.search(diff_text or ''):
        reasons.append('high_risk_command_in_diff')
    if high_paths or 'high_risk_command_in_diff' in reasons:
        tier = 'high'
    elif med_paths or not paths:
        tier = 'medium'
        if med_paths:
            reasons.append('medium_risk_paths: ' + ', '.join(med_paths))
        else:
            reasons.append('no_changed_paths')
    else:
        tier = 'low'
        reasons.append('docs_or_tests_only')

    auto_low = bool(((policy or {}).get('pr_gate') or {}).get('auto_approve_low_risk', False))
    approval_required = True if tier in ('high', 'medium') else (not auto_low)
    return {
        'risk_tier': tier,
        'approval_required': approval_required,
        'reasons': reasons,
        'path_tiers': path_tiers,
    }


def is_frozen(entry, current_head):
    """True when a gated node already passed at `current_head` and must not be re-run when a sibling fails."""
    if not isinstance(entry, dict) or not current_head:
        return False
    return (entry.get('head') == current_head
            and entry.get('status') in ('ready_for_human', 'needs_review', 'committed')
            and not entry.get('gate_reasons'))
