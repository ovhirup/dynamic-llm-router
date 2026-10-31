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
import re
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


def blocked_worker(policy, name):
    from . import security
    if name in (policy.get('never') or []):
        return True
    argv = policy['run_task']['commands'].get(name, [])
    if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
        return False
    executable = argv[0]
    for key in BINARY_DEFAULTS:
        executable = executable.replace('{' + key + '}', binary(policy, key))
    return Path(executable).name in security.shim_names(policy.get('never') or [])


def materialise_workers(policy):
    from . import security
    run = policy['run_task']
    if not isinstance(run.get('stackwich', {}), dict):
        raise ConfigError('run_task.stackwich must be an object')
    definitions = policy.get('workers', {})
    if not isinstance(definitions, dict):
        raise ConfigError('workers must be an object')
    for name, opts in definitions.items():
        if name.startswith('_'):
            continue
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name) or not isinstance(opts, dict):
            raise ConfigError('workers names must match [a-z][a-z0-9_-]{0,31}')
        if 'command' in opts:
            run['commands'][name] = opts['command']
        if name not in run['commands']:
            raise ConfigError(f'{name}: command required')
        for field, internal, default in [('prompt_delivery', 'prompt_delivery', 'stdin'),
                                        ('cwd', 'cwd', '.'), ('env', 'env', {}),
                                        ('timeout_seconds', 'timeouts_seconds', 900),
                                        ('stall_seconds', 'stall_seconds', 300),
                                        ('concurrency', 'concurrency', None),
                                        ('success', 'success', {}), ('stackwich', 'stackwich', {})]:
            run.setdefault(internal, {})[name] = opts.get(field, run.get(internal, {}).get(name, default))
        if 'quota' in opts:
            if not isinstance(opts['quota'], dict):
                raise ConfigError(f'{name}: quota must be an object')
            quota = deep_merge({'observer': 'llm_router.observers.command:observe', 'enabled': True,
                                'window': 'monthly', 'percent_is': 'used', 'timeout_seconds': 20,
                                'hard_timeout_seconds': 40, 'freshness_seconds': 1800,
                                'usage_threshold_percent': 85}, opts['quota'])
            policy.setdefault('quota', {})[name] = quota
    stackwich = run.setdefault('stackwich', {})
    if not isinstance(stackwich, dict):
        raise ConfigError('run_task.stackwich must be an object')
    for name, options in stackwich.items():
        if name not in run['commands'] and name != 'developer':
            raise ConfigError('run_task.stackwich names unknown worker')
        if not isinstance(options, dict) or set(options) - {'enabled', 'supported'} or any(
                type(value) is not bool for value in options.values()):
            raise ConfigError('stackwich requires only boolean enabled and supported options')
    caps = run.setdefault('concurrency', {})
    if not isinstance(caps, dict):
        raise ConfigError('run_task.concurrency must be an object')
    for name, cap in caps.items():
        if name.startswith('_'):
            continue
        if name not in run['commands'] and name != 'developer':
            raise ConfigError('run_task.concurrency names unknown worker')
        if cap is not None and (type(cap) is not int or not 1 <= cap <= 64):
            raise ConfigError(f'{name}: concurrency must be null or an integer 1-64')
    for name in list(run['commands']) + ['developer']:
        if not name.startswith('_'):
            stackwich[name] = dict({'enabled': False, 'supported': False}, **stackwich.get(name, {}))
    for name in run['commands']:
        if name.startswith('_'):
            continue
        mode = run.get('prompt_delivery', {}).get(name, 'stdin')
        if mode not in ('stdin', 'file', 'pointer', 'arg'):
            raise ConfigError(f'{name}: invalid prompt_delivery')
        cwd = run.get('cwd', {}).get(name, '.')
        if not isinstance(cwd, str) or Path(cwd).is_absolute() or '..' in Path(cwd).parts:
            raise ConfigError(f'{name}: cwd must stay inside the checkout')
        env = run.get('env', {}).get(name, {})
        if not isinstance(env, dict) or ('allow' in env and (not isinstance(env['allow'], list) or
                not all(isinstance(v, str) for v in env['allow']))):
            raise ConfigError(f'{name}: invalid env allowlist')
        setting = env.get('set', {})
        if not isinstance(setting, dict):
            raise ConfigError(f'{name}: env.set must be an object')
        for key, value in setting.items():
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) or not isinstance(value, str):
                raise ConfigError(f'{name}: invalid env.set entry')
            if re.search(r'KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|PRIVATE', key, re.I) or list(security.find_secrets(value)):
                raise ConfigError(f'{name}: env.set secrets forbidden; pass secrets through env.allow')
        raw_success = run.get('success', {}).get(name, {})
        if not isinstance(raw_success, dict):
            raise ConfigError(f'{name}: success must be an object')
        success = deep_merge({'exit_codes': [0], 'require_regex': None, 'fail_regex': None,
                              'require_changes': False}, raw_success)
        if not isinstance(success['exit_codes'], list) or not success['exit_codes'] or not all(type(v) is int for v in success['exit_codes']):
            raise ConfigError(f'{name}: invalid success.exit_codes')
        for field in ('require_regex', 'fail_regex'):
            try:
                if success[field] is not None:
                    re.compile(success[field])
            except (TypeError, re.error):
                raise ConfigError(f'{name}: invalid success.{field}') from None
        if type(success['require_changes']) is not bool:
            raise ConfigError(f'{name}: success.require_changes must be boolean')
        run.setdefault('success', {})[name] = success
        for field in ('timeouts_seconds', 'stall_seconds'):
            value = run.setdefault(field, {}).setdefault(name, 900 if field == 'timeouts_seconds' else 300)
            if type(value) not in (int, float) or not 0 < value < float('inf'):
                raise ConfigError(f'{name}: invalid {field}')
    for name, opts in (policy.get('quota') or {}).items():
        if not isinstance(opts, dict) or opts.get('observer') != 'llm_router.observers.command:observe':
            continue
        try:
            security.lint_commands({name: opts.get('command')}, allowed=set())
            regex = re.compile(opts['regex'])
            if 'percent' not in regex.groupindex:
                raise ValueError('regex needs named percent group')
            if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', opts.get('window', 'monthly')):
                raise ValueError('invalid window')
            if opts.get('percent_is', 'used') not in ('used', 'remaining'):
                raise ValueError('invalid percent_is')
            for field in ('timeout_seconds', 'hard_timeout_seconds', 'freshness_seconds'):
                value = opts[field]
                if type(value) not in (int, float) or not 0 < value < float('inf'):
                    raise ValueError('invalid ' + field)
        except (ValueError, TypeError, KeyError, re.error) as error:
            raise ConfigError(f'{name}: invalid quota command: {error}') from None
    return policy


def validate(policy):
    from . import quota_service, security
    order = policy.get('worker_order')
    materialise_workers(policy)
    if not isinstance(order, list) or not all(isinstance(n, str) for n in order) or len(set(order)) != len(order):
        raise ConfigError('worker_order must be a list of unique worker names')
    unknown = [name for name in order if name not in known_workers(policy)]
    if unknown:
        raise ConfigError(f'worker_order names unknown workers: {unknown} (add run_task.commands entries)')
    never = policy.get('never') or []
    if not isinstance(never, list) or not all(isinstance(n, str) for n in never):
        raise ConfigError('never must be a list of worker names')
    banned = [name for name in order if blocked_worker(policy, name)]
    if banned:
        raise ConfigError(f'worker_order contains workers listed in never: {banned}')
    commands = {k: v for k, v in policy['run_task']['commands'].items() if not k.startswith('_')}
    try:
        security.lint_commands(commands, prompt_delivery=policy['run_task'].get('prompt_delivery', {}))
    except security.LintError as error:
        raise ConfigError(str(error)) from None
    for name, argv in commands.items():
        if not argv[0] or (not Path(argv[0]).is_absolute() and ('/' in argv[0] or '\\' in argv[0])):
            raise ConfigError(f'{name}: executable must be a bare name or absolute path')
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
    names = [w for w in policy['worker_order'] if w in quota]
    return [w for w in names if not w.startswith('_') and quota[w].get('enabled', True)]
