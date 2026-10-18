"""Configuration: packaged defaults + user config, XDG paths, timezone and binary discovery.

Nothing personal lives in the package. User settings come from (first match wins):
  $LLM_ROUTER_CONFIG (a JSON file)  or  <config_dir>/config.json
where <config_dir> is $LLM_ROUTER_CONFIG_DIR, else $XDG_CONFIG_HOME/llm-router, else ~/.config/llm-router.
Runtime files (caches, logs, ledger, worktrees) live under <state_dir>: $LLM_ROUTER_STATE_DIR, else
$XDG_STATE_HOME/llm-router, else ~/.local/state/llm-router.
"""
import copy
from datetime import datetime, tzinfo
from importlib import resources
import json
import os
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

APP = 'llm-router'
# Placeholder name -> default executable looked up on PATH.
BINARY_DEFAULTS = {'codex': 'codex', 'grok': 'grok', 'cursor': 'cursor-agent', 'claude': 'claude'}
# Worker name -> binary placeholder it runs.
WORKER_BINARY = {'codex': 'codex', 'grok-build': 'grok', 'cursor': 'cursor', 'claude': 'claude'}
# Keys whose relative paths live in the config dir (user-authored); everything else is state.
CONFIG_KEYS = {'private_terms_file', 'repo_config'}


class ConfigError(ValueError):
    """The merged configuration is invalid."""


def _xdg(env, fallback):
    base = os.environ.get(env)
    return Path(base).expanduser() if base else Path.home() / fallback


def config_dir():
    override = os.environ.get('LLM_ROUTER_CONFIG_DIR')
    return Path(override).expanduser() if override else _xdg('XDG_CONFIG_HOME', '.config') / APP


def state_dir():
    override = os.environ.get('LLM_ROUTER_STATE_DIR')
    return Path(override).expanduser() if override else _xdg('XDG_STATE_HOME', '.local/state') / APP


def config_file():
    override = os.environ.get('LLM_ROUTER_CONFIG')
    return Path(override).expanduser() if override else config_dir() / 'config.json'


def resolve(value, kind='state'):
    """Absolute or ~ paths stay; relative paths go under the state dir (or config dir)."""
    path = Path(os.path.expanduser(str(value)))
    if path.is_absolute():
        return path
    return (config_dir() if kind == 'config' else state_dir()) / path


def policy_path(policy, section, key, default):
    value = (policy.get(section) or {}).get(key, default)
    return resolve(value, 'config' if key in CONFIG_KEYS else 'state')


def defaults():
    text = resources.files('llm_router').joinpath('default_policy.json').read_text()
    return json.loads(text)


def deep_merge(base, override):
    """Dicts merge recursively; lists and scalars replace. Keys starting with '_' are comments."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_user_config(path=None):
    path = Path(path) if path else config_file()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        raise ConfigError(f'{path}: invalid JSON ({error.msg} at line {error.lineno})') from None
    if not isinstance(data, dict):
        raise ConfigError(f'{path}: top level must be an object')
    return data


def known_workers(policy):
    return set(k for k in policy['run_task']['commands'] if not k.startswith('_')) | {'developer'}


def validate(policy):
    from . import quota_service, security
    order = policy.get('worker_order')
    if not isinstance(order, list) or not order or len(set(order)) != len(order):
        raise ConfigError('worker_order must be a non-empty list of unique worker names')
    unknown = [name for name in order if name not in known_workers(policy)]
    if unknown:
        raise ConfigError(f'worker_order names unknown workers: {unknown} (add run_task.commands entries)')
    never = policy.get('never') or []
    if not isinstance(never, list) or not all(isinstance(n, str) for n in never):
        raise ConfigError('never must be a list of worker names')
    banned = [name for name in order if name in never]
    if banned:
        raise ConfigError(f'worker_order contains workers listed in never: {banned}')
    commands = {k: v for k, v in policy['run_task']['commands'].items() if not k.startswith('_')}
    security.lint_commands(commands)
    for name in order:
        if name == 'developer':
            continue
        quota = (policy.get('quota') or {}).get(name)
        if quota is None and name not in policy.get('unmetered_workers', []):
            raise ConfigError(f'{name}: no quota observer configured; add quota.{name} or list it in '
                              'unmetered_workers (unmetered workers are never paced)')
    tz(policy)
    quota_service.validate(policy)
    return policy


def load_policy(path=None, override=None):
    """Packaged defaults deep-merged with the user's config (and an optional in-memory override)."""
    policy = deep_merge(defaults(), load_user_config(path))
    if override:
        policy = deep_merge(policy, override)
    return validate(policy)


def tz(policy=None) -> tzinfo:
    name = (policy or {}).get('timezone') or 'local'
    if name == 'local':
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except Exception:
        raise ConfigError(f'unknown timezone {name!r}') from None


def now_local(policy=None):
    return datetime.now(tz(policy))


def binary(policy, placeholder):
    """Configured absolute path, else the default name on PATH, else the bare name (fails at exec)."""
    configured = ((policy or {}).get('binaries') or {}).get(placeholder)
    if configured:
        return os.path.expanduser(configured)
    default = BINARY_DEFAULTS.get(placeholder, placeholder)
    return shutil.which(default) or default


def quota_cfg(policy, worker):
    return (policy.get('quota') or {}).get(worker)


def metered_workers(policy):
    """Workers with an enabled quota observer, in worker_order first."""
    quota = policy.get('quota') or {}
    names = [w for w in policy['worker_order'] if w in quota] + [w for w in quota if w not in policy['worker_order']]
    return [w for w in names if not w.startswith('_') and quota[w].get('enabled', True)]
