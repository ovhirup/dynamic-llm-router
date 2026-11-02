"""Router Brain: the git-tracked Obsidian vault skeleton (docs/ROUTER_BRAIN.md section (c)).

The vault is plain markdown: jobs/, handoffs/, scorecards/, research/ and an always-loaded
rules.md. init_vault() materialises that skeleton idempotently and never overwrites existing
files, so user edits to rules.md or any note are always kept.
"""
from pathlib import Path

VAULT_DIRS = ('jobs', 'handoffs', 'scorecards', 'research')
RULES_FILE = 'rules.md'
DEFAULT_VAULT_DIR = 'vault'

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
    """The vault path from policy['brain']['vault_dir'] (default 'vault'), with ~ expanded."""
    brain = (policy or {}).get('brain') or {}
    return Path(brain.get('vault_dir') or DEFAULT_VAULT_DIR).expanduser()


def init_vault(path=None, policy=None):
    """Create the vault skeleton under path (or vault_dir(policy)); return the list of created paths.

    Idempotent: existing directories and files (including an edited rules.md) are left untouched.
    """
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
