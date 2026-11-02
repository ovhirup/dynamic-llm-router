"""Plugin manifest validation and registry (#74).

External productivity tools plug into DLR via a declarative manifest:
  {
    "name": "hark",
    "capabilities": ["input_source", "context"],
    "transport": "mcp_stdio",
    "command": ["hark-mcp"],
    "description": "Local dictation and meeting transcript search",
    "enabled": false
  }
All plugins are opt-in (`enabled: False` by default) and linted against inline secrets.
"""
import copy
import json
from pathlib import Path
import re

from . import security

NAME = re.compile(r'[a-z][a-z0-9_-]{0,31}')
CAPABILITIES = ('input_source', 'context', 'notifier', 'output_sink')
TRANSPORTS = ('mcp_stdio', 'command', 'builtin')


class PluginError(ValueError):
    """Invalid plugin manifest or registry operation."""


def validate_manifest(manifest):
    """Validate and normalise a plugin manifest dict; raise PluginError on any defect."""
    if not isinstance(manifest, dict):
        raise PluginError('plugin manifest must be a JSON object')
    allowed = {'name', 'capabilities', 'transport', 'command', 'description', 'enabled', 'permissions'}
    extra = set(manifest) - allowed
    if extra:
        raise PluginError(f'unknown manifest keys: {sorted(extra)}')
    name = manifest.get('name')
    if type(name) is not str or not NAME.fullmatch(name):
        raise PluginError(f'plugin name must match [a-z][a-z0-9_-]{{0,31}}: {name!r}')
    caps = manifest.get('capabilities')
    if type(caps) is not list or not caps or len(set(caps)) != len(caps) or \
            not all(type(c) is str and c in CAPABILITIES for c in caps):
        raise PluginError(f'{name}: capabilities must be a non-empty unique subset of {CAPABILITIES}')
    transport = manifest.get('transport', 'mcp_stdio')
    if transport not in TRANSPORTS:
        raise PluginError(f'{name}: transport must be one of {TRANSPORTS}')
    command = manifest.get('command')
    if transport in ('mcp_stdio', 'command'):
        if type(command) is not list or not command or not all(type(a) is str and a.strip() for a in command):
            raise PluginError(f'{name}: command must be a non-empty list of strings')
        try:
            security.lint_commands({name: command}, allowed=set())
        except security.LintError as error:
            raise PluginError(str(error)) from None
    elif command is not None and (type(command) is not list or not all(type(a) is str for a in command)):
        raise PluginError(f'{name}: invalid command')
    description = manifest.get('description', '')
    if type(description) is not str or len(description) > 500:
        raise PluginError(f'{name}: description must be a string up to 500 chars')
    if list(security.find_secrets(description)):
        raise PluginError(f'{name}: secrets forbidden in manifest description')
    enabled = manifest.get('enabled', False)
    if type(enabled) is not bool:
        raise PluginError(f'{name}: enabled must be a boolean')
    permissions = manifest.get('permissions', [])
    if type(permissions) is not list or not all(type(p) is str and p for p in permissions):
        raise PluginError(f'{name}: permissions must be a list of strings')
    return {
        'name': name,
        'capabilities': list(caps),
        'transport': transport,
        'command': list(command) if command is not None else [],
        'description': description,
        'enabled': enabled,
        'permissions': list(permissions),
    }


class PluginRegistry:
    """In-memory registry of validated plugin manifests."""

    def __init__(self, manifests=()):
        self._items = {}
        for item in manifests:
            self.register(item)

    def register(self, manifest):
        validated = validate_manifest(manifest)
        name = validated['name']
        if name in self._items:
            raise PluginError(f'duplicate plugin {name!r}')
        self._items[name] = validated
        return copy.deepcopy(validated)

    def get(self, name):
        item = self._items.get(name)
        return copy.deepcopy(item) if item is not None else None

    def enable(self, name):
        if name not in self._items:
            raise PluginError(f'unknown plugin {name!r}')
        self._items[name]['enabled'] = True
        return copy.deepcopy(self._items[name])

    def disable(self, name):
        if name not in self._items:
            raise PluginError(f'unknown plugin {name!r}')
        self._items[name]['enabled'] = False
        return copy.deepcopy(self._items[name])

    def list_plugins(self, capability=None, enabled_only=False):
        if capability is not None and capability not in CAPABILITIES:
            raise PluginError(f'unknown capability {capability!r}; choose from {CAPABILITIES}')
        out = []
        for item in self._items.values():
            if enabled_only and not item['enabled']:
                continue
            if capability is not None and capability not in item['capabilities']:
                continue
            out.append(copy.deepcopy(item))
        return out


def discover_manifests(directory):
    """Load and validate every `*.json` manifest in `directory` (sorted by filename)."""
    root = Path(directory)
    if not root.is_dir():
        return []
    manifests = []
    for path in sorted(root.glob('*.json')):
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise PluginError(f'{path.name}: invalid JSON ({error})') from None
        manifests.append(validate_manifest(raw))
    return manifests


def load_registry(policy=None, directory=None):
    """Build a PluginRegistry from `policy['plugins']` (list or name->manifest dict) plus optional directory."""
    registry = PluginRegistry()
    if directory is not None:
        for item in discover_manifests(directory):
            registry.register(item)
    raw = (policy or {}).get('plugins') or []
    if isinstance(raw, dict):
        items = [dict(v, name=k) if isinstance(v, dict) and 'name' not in v else v for k, v in raw.items()
                 if not str(k).startswith('_')]
    elif isinstance(raw, list):
        items = raw
    else:
        raise PluginError('plugins must be a list or object of manifests')
    for item in items:
        registry.register(item)
    return registry
