"""Obsidian vault plugin (#77): context provider and output sink over the Router Brain vault.

Exposes the plain-markdown Obsidian vault (`jobs/`, `handoffs/`, `scorecards/`, `research/`,
`rules.md`) as a DLR plugin with `context` and `output_sink` capabilities. All paths are
confined to `vault_dir` and all written/returned content is passed through `security.redact`.
"""
from pathlib import Path
import re

from . import security

ALLOWED_FOLDERS = ('jobs', 'handoffs', 'scorecards', 'research')
SAFE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}')

MANIFEST = {
    'name': 'obsidian-vault',
    'capabilities': ['context', 'output_sink'],
    'transport': 'builtin',
    'command': [],
    'description': 'Obsidian vault Router Brain plugin (context reader + redacted markdown output sink).',
    'enabled': False,
    'permissions': ['read_vault', 'write_vault'],
}


def _safe_root(vault_dir):
    return Path(vault_dir).expanduser().resolve()


def read_rules(vault_dir):
    """Return redacted `rules.md` from `vault_dir`, or '' when absent."""
    root = _safe_root(vault_dir)
    path = root / 'rules.md'
    if not path.is_file() or path.is_symlink():
        return ''
    return security.redact(path.read_text(errors='replace'))


def search_notes(vault_dir, query, folders=ALLOWED_FOLDERS, limit=5):
    """Case-insensitive keyword search across markdown notes in `vault_dir`."""
    root = _safe_root(vault_dir)
    needle = (query or '').strip().lower()
    if not needle or not root.is_dir():
        return []
    hits = []
    for folder in folders:
        if folder not in ALLOWED_FOLDERS:
            continue
        sub = (root / folder).resolve()
        if not sub.is_relative_to(root) or not sub.is_dir():
            continue
        for note in sorted(sub.rglob('*.md')):
            if note.is_symlink() or not note.is_file():
                continue
            text = note.read_text(errors='replace')
            if needle in text.lower() or needle in note.name.lower():
                rel = note.relative_to(root).as_posix()
                hits.append({
                    'path': rel,
                    'excerpt': security.redact(text[:400]),
                })
                if len(hits) >= max(1, int(limit)):
                    return hits
    return hits


def write_note(vault_dir, folder, name, content):
    """Write a redacted markdown note inside `vault_dir/<folder>/<name>.md`; refuses path traversal."""
    if folder not in ALLOWED_FOLDERS:
        raise ValueError(f'folder must be one of {ALLOWED_FOLDERS}')
    stem = name[:-3] if str(name).endswith('.md') else str(name)
    if not SAFE_NAME.fullmatch(stem) or '..' in stem:
        raise ValueError(f'unsafe note name {name!r}')
    root = _safe_root(vault_dir)
    target_dir = (root / folder).resolve()
    if not target_dir.is_relative_to(root):
        raise ValueError('vault folder escapes vault root')
    target_dir.mkdir(parents=True, exist_ok=True)
    path = (target_dir / f'{stem}.md').resolve()
    if not path.is_relative_to(target_dir):
        raise ValueError('note path escapes vault folder')
    path.write_text(security.redact(str(content)))
    return path
