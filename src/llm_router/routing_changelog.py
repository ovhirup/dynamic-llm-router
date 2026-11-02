"""Human-readable routing change log in the Obsidian vault (#72).

Appends dated, redacted, human-readable entries to `<vault_dir>/routing-changelog.md` whenever
routing weights, capability claims, or worker rankings are updated (after passing the evaluation
harness gate). Every entry records:
  - Date & timestamp
  - Summary of the change (`before` -> `after`)
  - Why it changed (e.g. `higher pass rate`, `lower quota %`, `drift demotion`)
  - Evidence / eval report citation (`source`)
"""
from datetime import datetime, timezone
import fcntl
import re

from . import config, security

HEADER = '# Routing Change Log\n\nHuman-readable audit log of validated routing and weight updates.\n\n'
_ENTRY_RE = re.compile(r'^## (?P<stamp>.+?) — (?P<title>.+?)\s*$', re.M)


def changelog_path(policy=None, vault_dir=None):
    if vault_dir is not None:
        from pathlib import Path
        return Path(vault_dir) / 'routing-changelog.md'
    return config.policy_path(policy or {}, 'run_job', 'vault_dir', 'vault') / 'routing-changelog.md'


def append_routing_change(title, before, after, reason, source='', policy=None, vault_dir=None, now=None):
    """Append one redacted markdown entry to `<vault_dir>/routing-changelog.md`."""
    stamp = (now or datetime.now(timezone.utc)).strftime('%Y-%m-%d %H:%M UTC')
    clean_title = security.redact(str(title or 'Routing update').strip())
    clean_before = security.redact(str(before or 'unspecified').strip())
    clean_after = security.redact(str(after or 'unspecified').strip())
    clean_reason = security.redact(str(reason or 'validated in eval harness').strip())
    clean_source = security.redact(str(source or 'eval_harness').strip())

    block = '\n'.join([
        f'## {stamp} — {clean_title}',
        f'- **Before:** `{clean_before}`',
        f'- **After:** `{clean_after}`',
        f'- **Why:** {clean_reason}',
        f'- **Evidence:** `{clean_source}`',
        '',
    ])
    path = changelog_path(policy=policy, vault_dir=vault_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(HEADER)
        handle.write(block + '\n')
    return path


def read_routing_changes(policy=None, vault_dir=None):
    """Parse entries from `<vault_dir>/routing-changelog.md`."""
    path = changelog_path(policy=policy, vault_dir=vault_dir)
    if not path.is_file():
        return []
    text = path.read_text(errors='replace')
    matches = list(_ENTRY_RE.finditer(text))
    entries = []
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        entries.append({
            'timestamp': match.group('stamp'),
            'title': match.group('title'),
            'body': body,
        })
    return entries
