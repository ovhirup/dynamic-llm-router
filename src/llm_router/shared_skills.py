"""Shared Agent Skills and baseline Claude Code plugins per task class (#96).

Because plugins and skills consume prompt context tokens, DLR enables them per task class rather
than globally:
  - `feature`:    `['superpowers-tdd', 'context7', 'code-simplifier']`
  - `bugfix`:     `['superpowers-tdd', 'code-review']`
  - `review`:     `['code-review']`
  - `mechanical`: `['code-simplifier']`
  - `draft`:      `[]` (zero plugin context overhead for short drafts)

Shared skills live in one packaged folder (`src/llm_router/examples/skills/`) and can be loaded
by any worker (`claude`, `codex`, `cursor`, `grok-build`).
"""
from pathlib import Path

from . import config, security

BASELINE_PLUGINS = (
    'superpowers-tdd',
    'code-review',
    'context7',
    'code-simplifier',
    'playwright',
)

DEFAULT_CLASS_PLUGINS = {
    'feature': ['superpowers-tdd', 'context7', 'code-simplifier'],
    'bugfix': ['superpowers-tdd', 'code-review'],
    'review': ['code-review'],
    'mechanical': ['code-simplifier'],
    'draft': [],
}


def skills_dir(custom_dir=None):
    if custom_dir is not None:
        return Path(custom_dir).expanduser().resolve()
    return Path(__file__).resolve().parent / 'examples' / 'skills'


def plugins_for_class(policy, task_class):
    """Return the list of enabled skills/plugins for `task_class` (per-class, never global)."""
    if task_class is None:
        return []
    if task_class not in config.TASK_CLASSES and task_class != 'design':
        raise config.ConfigError(f'unknown task class {task_class!r}')
    configured = ((policy or {}).get('class_plugins') or {}).get(task_class)
    if isinstance(configured, list):
        return [str(p) for p in configured if str(p) in BASELINE_PLUGINS]
    if task_class == 'design':
        return ['playwright', 'code-review']
    return list(DEFAULT_CLASS_PLUGINS.get(task_class, ()))


def load_skill_text(name, directory=None):
    path = skills_dir(directory) / f'{name}.md'
    if not path.is_file():
        return ''
    return security.redact(path.read_text(errors='replace').strip())


def render_class_skills(policy, task_class, directory=None):
    """Render only the skills assigned to `task_class` so unrelated plugins never bloat tokens."""
    names = plugins_for_class(policy, task_class)
    if not names:
        return ''
    blocks = []
    for name in names:
        text = load_skill_text(name, directory=directory)
        if text:
            blocks.append(f'### Skill: `{name}`\n{text}')
        else:
            blocks.append(f'### Plugin: `{name}` (enabled for `{task_class}`)')
    return '## Active Task-Class Skills\n\n' + '\n\n'.join(blocks)


def compare_per_class_vs_global_tokens(task_class, policy=None, directory=None):
    """Compare prompt token overhead of loading skills per task class vs loading all plugins globally."""
    global_text = '\n\n'.join(
        load_skill_text(name, directory=directory) or f'Plugin: {name}'
        for name in BASELINE_PLUGINS
    )
    scoped_text = render_class_skills(policy, task_class, directory=directory)
    global_tokens = max(1, (len(global_text) + 3) // 4)
    scoped_tokens = (len(scoped_text) + 3) // 4
    return {
        'task_class': task_class,
        'global_tokens': global_tokens,
        'scoped_tokens': scoped_tokens,
        'tokens_saved': global_tokens - scoped_tokens,
        'equal_or_lower_tokens': scoped_tokens <= global_tokens,
    }
