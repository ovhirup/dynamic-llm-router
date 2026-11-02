"""Validate and atomically store the separate opt-in setup profile (#210).

No legacy settings, repository maps or authentication files are read. The only
existing content read by the writer is its guarded setup-profile destination,
for an exclusive private backup before replacement.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from . import config

WORKERS = ('claude', 'codex', 'grok-build', 'cursor', 'aider-ollama', 'grok-bot')
CLOUD_WORKERS = WORKERS[:4]
ORCHESTRATORS = WORKERS + ('cli',)
ROLES = ('planner', 'coder', 'searcher', 'judge', 'advisor')
SWITCH_MODES = ('always_ask', 'always_accept_caller', 'always_use')


def profile_path():
    return config.config_dir() / 'setup.json'


def preferred_config_path(explicit=None, env=None):
    """Resolve a path without reading legacy contents or authentication state."""
    environment = os.environ if env is None else env
    if explicit:
        return Path(explicit).expanduser()
    if environment.get('LLM_ROUTER_CONFIG'):
        return Path(environment['LLM_ROUTER_CONFIG']).expanduser()
    proposed = profile_path()
    return proposed if proposed.is_file() else config.config_file()


def _object(value, label):
    if not isinstance(value, dict):
        raise config.ConfigError(f'{label} must be an object')


def _names(value, known, label):
    if (not isinstance(value, list) or any(not isinstance(name, str) or name not in known for name in value)
            or len(set(value)) != len(value)):
        raise config.ConfigError(f'{label} must contain unique supported identifiers')


def _pins(value, enabled, field, label):
    _object(value, label)
    if set(value) != {field, 'reason'} or value[field] not in enabled:
        raise config.ConfigError(f'{label} must target an enabled {field}')
    if not isinstance(value['reason'], str) or not value['reason'].strip():
        raise config.ConfigError(f'{label} requires a reason')


def validate_setup(setup):
    """Validate only setup capability/pin fields, also when called from a merged policy."""
    _object(setup, 'setup')
    setup = deepcopy(setup)
    fields = {'version', 'enabled_workers', 'enabled_orchestrators', 'preferred_orchestrator',
              'switch_policy', 'worker_overrides', 'orchestrator_overrides', 'tools'}
    if set(setup) != fields or type(setup['version']) is not int or setup['version'] != 1:
        raise config.ConfigError('unsupported or incomplete setup profile version')
    workers, orchestrators = setup['enabled_workers'], setup['enabled_orchestrators']
    _names(workers, WORKERS, 'enabled_workers')
    _names(orchestrators, ORCHESTRATORS, 'enabled_orchestrators')
    canonical = [worker for worker in WORKERS if worker in workers]
    if workers != canonical:
        raise config.ConfigError('enabled_workers must retain the canonical candidate order')
    preferred = setup['preferred_orchestrator']
    if preferred is not None and preferred not in orchestrators:
        raise config.ConfigError('preferred orchestrator must be enabled')
    switch = setup['switch_policy']
    _object(switch, 'switch_policy')
    if set(switch) != {'mode', 'target'} or switch['mode'] not in SWITCH_MODES:
        raise config.ConfigError('invalid switch policy mode')
    if switch['mode'] == 'always_use':
        if switch['target'] not in orchestrators:
            raise config.ConfigError('fixed orchestrator must be enabled')
    elif switch['target'] is not None:
        raise config.ConfigError('only always_use accepts a fixed target')
    worker_overrides, orchestrator_overrides = setup['worker_overrides'], setup['orchestrator_overrides']
    _object(worker_overrides, 'worker_overrides')
    _object(orchestrator_overrides, 'orchestrator_overrides')
    if set(worker_overrides) != {'default', 'roles'} or set(orchestrator_overrides) != {'roles'}:
        raise config.ConfigError('invalid override sections')
    if worker_overrides['default'] is not None:
        _pins(worker_overrides['default'], workers, 'worker', 'default worker pin')
    for override, enabled, field in ((worker_overrides, workers, 'worker'),
                                     (orchestrator_overrides, orchestrators, 'orchestrator')):
        _object(override['roles'], field + ' role overrides')
        for role, pin in override['roles'].items():
            if role not in ROLES:
                raise config.ConfigError('unsupported role override')
            _pins(pin, enabled, field, role + ' ' + field + ' pin')
    tools = setup['tools']
    _object(tools, 'tools')
    if any(name not in ORCHESTRATORS for name in tools):
        raise config.ConfigError('unsupported tool identifier')
    for name, tool in tools.items():
        _object(tool, name + ' tool')
        caps = tool.get('requested_capabilities')
        _object(caps, name + ' requested_capabilities')
        if set(caps) != {'worker', 'orchestrator'} or any(type(value) is not bool for value in caps.values()):
            raise config.ConfigError('requested capabilities must be independent booleans')
        if name == 'cli' and caps['worker']:
            raise config.ConfigError('cli is not an execution worker')
        smoke = tool.get('smoke')
        _object(smoke, name + ' smoke')
        if type(smoke.get('passed')) is not bool:
            raise config.ConfigError('smoke.passed must be boolean')
    for name in set(workers) | set(orchestrators):
        tool = tools.get(name)
        if tool is None or not tool['smoke']['passed']:
            raise config.ConfigError('enabled tools require a successful smoke')
        if name in workers and not tool['requested_capabilities']['worker']:
            raise config.ConfigError('worker was not explicitly enabled')
        if name in orchestrators and not tool['requested_capabilities']['orchestrator']:
            raise config.ConfigError('orchestrator was not explicitly enabled')
        expected = 'not_required' if name == 'cli' else 'user_verified' if name == 'grok-bot' else 'verified'
        if tool.get('auth') != expected:
            raise config.ConfigError('enabled tool has no verified smoke/auth label')
    return setup


def validate_profile(fragment):
    """Validate a copied setup-profile fragment without loading any personal settings."""
    _object(fragment, 'profile')
    result = deepcopy(fragment)
    required = {'setup', 'worker_order', 'quota', 'unmetered_workers', 'workers',
                'class_order', 'role_order', 'allocation'}
    if set(result) != required:
        raise config.ConfigError('setup profile has missing or unsupported sections')
    setup = validate_setup(result['setup'])
    result['setup'] = setup
    workers, tools = setup['enabled_workers'], setup['tools']
    if result['worker_order'] != workers:
        raise config.ConfigError('worker_order must retain the canonical enabled candidate order')
    quota = result['quota']
    _object(quota, 'quota')
    for name, opts in quota.items():
        if name not in CLOUD_WORKERS or opts != {'enabled': True}:
            raise config.ConfigError('setup quota enables only packaged cloud observers; hard gates are unchanged')
    _names(result['unmetered_workers'], ('aider-ollama',), 'unmetered_workers')
    if result['unmetered_workers'] and not (tools.get('aider-ollama', {}).get('smoke') or {}).get('passed'):
        raise config.ConfigError('local unmetered routing requires a successful local smoke')
    _object(result['workers'], 'workers')
    if set(result['workers']) - {'aider-ollama'}:
        raise config.ConfigError('setup may only compose the packaged local aider worker')
    _object(result['class_order'], 'class_order')
    _object(result['role_order'], 'role_order')
    if set(result['class_order']) != set(config.TASK_CLASSES) or set(result['role_order']) != set(ROLES):
        raise config.ConfigError('setup must freeze all class and role candidate lists')
    for label, orders in (('class', result['class_order']), ('role', result['role_order'])):
        for name, order in orders.items():
            _names(order, workers, label + ' ' + name)
    allocation = result['allocation']
    _object(allocation, 'allocation')
    if set(allocation) != {'providers'}:
        raise config.ConfigError('setup allocation only supplies provider reserves')
    _object(allocation['providers'], 'allocation.providers')
    for name, settings in allocation['providers'].items():
        if name not in WORKERS or not isinstance(settings, dict) or set(settings) != {'reserve_percent'}:
            raise config.ConfigError('invalid provider reserve')
        reserve = settings['reserve_percent']
        if type(reserve) not in (int, float) or not math.isfinite(reserve) or not 0 <= reserve < 100:
            raise config.ConfigError('reserve_percent must be finite and in 0..100, excluding 100')
    return result


def _destination(path):
    """Guard names before inspecting or reading a destination or resolving its parent."""
    path = Path(path).expanduser()
    name = path.name.lower()
    forbidden = (name.startswith('.env') or name in ('config.json', 'repos.json')
                 or name.startswith(('config.json.', 'repos.json.'))
                 or re.search(r'(?:^|[._-])(?:secrets?|credentials?|tokens?|auth|private[_-]?terms)(?:[._-]|$)', name)
                 or name in ('id_rsa', 'id_ed25519'))
    if forbidden or '.codex' in path.parts or path.suffix.lower() != '.json':
        raise config.ConfigError('setup destination is a protected settings or secret path')
    if path.is_symlink():
        raise config.ConfigError('setup destination must not be a symlink')
    parent = path.absolute().parent.resolve()
    if '.codex' in parent.parts:
        raise config.ConfigError('setup destination resolves into a protected directory')
    if path.exists() and not path.is_file():
        raise config.ConfigError('setup destination must be a regular file')
    return parent / path.name


def _open_directory(parent):
    """Anchor writes to a directory descriptor; never follow raced directory symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(parent.anchor, flags)
    try:
        for part in parent.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def write_profile(fragment, path=None, now=None):
    """Back up a guarded setup profile, then atomically replace it with mode 0600."""
    destination = _destination(path if path is not None else profile_path())
    data = validate_profile(fragment)
    config.validate(config.deep_merge(config.defaults(), data))
    clock = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
        raise config.ConfigError('backup timestamp must be timezone-aware')
    stamp = clock.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    parent_fd = _open_directory(destination.parent)
    temporary = None
    backup = None
    try:
        try:
            existing = os.open(destination.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            with os.fdopen(existing, 'rb') as handle:
                status = os.fstat(handle.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise config.ConfigError('setup destination must be an unlinked regular profile file')
                previous = handle.read()
            backup_name = destination.name + '.backup-' + stamp + '-' + uuid.uuid4().hex + '.json'
            backup_fd = os.open(backup_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=parent_fd)
            with os.fdopen(backup_fd, 'wb') as handle:
                handle.write(previous)
                handle.flush()
                os.fsync(handle.fileno())
            backup = str(destination.parent / backup_name)
        temporary = '.' + destination.name + '.' + uuid.uuid4().hex + '.tmp'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent_fd)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(data, handle, indent=2, allow_nan=False)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary = None
        return {'path': str(destination), 'backup': backup}
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)
