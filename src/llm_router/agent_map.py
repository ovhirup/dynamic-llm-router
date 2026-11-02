"""Shared agent map (`AGENTS.md`, `CLAUDE.md`, `docs/decisions.md`, `docs/learnings.md`) (#133).

Provides:
  1. `load_repo_map(repo_root)`: reads `AGENTS.md` (and optional `docs/decisions.md` / `docs/learnings.md`)
     from `repo_root`, redacting any secret shapes, so `run-job` and `units run` automatically
     include the repository map in every worker prompt.
  2. `init_map(repo_root, force=False)`: scaffolds `AGENTS.md`, `CLAUDE.md`, `docs/decisions.md`,
     and `docs/learnings.md` in any target repository (never overwriting existing files unless `force=True`).
"""
from pathlib import Path

from . import security

MAX_MAP_CHARS = 4000

DEFAULT_AGENTS_MD = """\
# Shared Agent Map

Whichever coding tool receives the prompt starts from this map.

## Where things live

- **Standing decisions:** [`docs/decisions.md`](docs/decisions.md)
- **Project learnings:** [`docs/learnings.md`](docs/learnings.md)
- **Source code:** `src/`
- **Tests:** `tests/`

## How to run tests

- Run `python -m pytest -q` (0 skipped required).

## Guardrails

- Never push to `main`, merge, or touch secrets without human approval.
"""

DEFAULT_CLAUDE_MD = """\
# Claude Code Instructions

Read and follow [`AGENTS.md`](AGENTS.md) for the repository map, standing decisions ([`docs/decisions.md`](docs/decisions.md)), learnings ([`docs/learnings.md`](docs/learnings.md)), and test commands.
"""

DEFAULT_DECISIONS_MD = """\
# Standing Decisions

- Never push to `main`, merge, or approve without explicit human approval.
- Tests must run and pass with 0 skipped.
- No secrets or private terms in code, commits, or logs.
"""

DEFAULT_LEARNINGS_MD = """\
# Learnings

One-topic lessons from past jobs in this repository.
"""

SCAFFOLD_FILES = (
    ('AGENTS.md', DEFAULT_AGENTS_MD),
    ('CLAUDE.md', DEFAULT_CLAUDE_MD),
    ('docs/decisions.md', DEFAULT_DECISIONS_MD),
    ('docs/learnings.md', DEFAULT_LEARNINGS_MD),
)


def init_map(repo_root, force=False):
    """Create `AGENTS.md`, `CLAUDE.md`, `docs/decisions.md`, and `docs/learnings.md` in `repo_root`."""
    root = Path(repo_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    for rel, content in SCAFFOLD_FILES:
        target = root / rel
        if target.exists() and not force:
            skipped.append(str(target))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append(str(target))
    return {'repo': str(root), 'written': written, 'skipped_existing': skipped}


def load_repo_map(repo_root, max_chars=MAX_MAP_CHARS):
    """Read `AGENTS.md` (plus `docs/decisions.md` and `docs/learnings.md` when present) for prompt injection."""
    root = Path(repo_root).expanduser().resolve()
    parts = []
    for rel in ('AGENTS.md', 'docs/decisions.md', 'docs/learnings.md'):
        path = root / rel
        if path.is_file() and not path.is_symlink():
            try:
                text = path.read_text(errors='replace').strip()
            except OSError:
                continue
            if text:
                parts.append(f'### `{rel}`\n{security.redact(text)}')
    if not parts:
        return ''
    combined = '## Repository Agent Map\n\n' + '\n\n'.join(parts)
    return combined[:max_chars]
