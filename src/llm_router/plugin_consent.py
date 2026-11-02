"""Per-plugin permission and consent model (#79): explicit opt-in and zero secrets in logs.

Every plugin requires:
1. `manifest['enabled'] is True` (explicit opt-in),
2. The requested permission listed in `manifest['permissions']`, and
3. An explicit user consent grant for `(plugin_name, permission)` in `<state>/plugins/consent.json`
   (or `policy['plugin_consent']`).

All plugin invocation logs are written to `<state>/logs/plugins.jsonl` (`0600`) after passing through
`security.redact_obj` and private-term redaction so credentials and private terms never appear in logs.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
import re

from . import config, security, task_dispatch as td

PERMISSIONS = (
    'read_transcripts',
    'read_vault',
    'write_vault',
    'post_summary',
    'send_summary',
    'web_search',
)


class ConsentRefused(ValueError):
    """Plugin execution refused due to missing opt-in or permission consent."""


def _consent_path(policy):
    return config.policy_path(policy or {}, 'plugins', 'consent_file', 'state/plugin_consent.json')


def _log_path(policy):
    return config.policy_path(policy or {}, 'plugins', 'log_file', 'logs/plugins.jsonl')


def load_consents(policy=None):
    """Return `{plugin_name: {'permissions': [...], 'granted_at': ...}}` from state + policy."""
    path = _consent_path(policy)
    try:
        stored = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    inline = (policy or {}).get('plugin_consent') or {}
    if isinstance(inline, dict):
        for name, perms in inline.items():
            if isinstance(perms, list):
                existing = set((stored.get(name) or {}).get('permissions') or ())
                stored[name] = {
                    'permissions': sorted(existing | {str(p) for p in perms}),
                    'granted_at': (stored.get(name) or {}).get('granted_at') or 'policy',
                }
    return stored


def _save_consents(policy, data):
    path = _consent_path(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(security.redact_obj(data), handle, indent=2, sort_keys=True)
        handle.write('\n')
    os.chmod(path, 0o600)
    return path


def grant_consent(policy, plugin, permissions, now=None):
    """Record explicit user consent for `plugin` and `permissions`."""
    if not plugin or not isinstance(plugin, str):
        raise ValueError('plugin name required')
    if not isinstance(permissions, (list, tuple, set)) or not permissions:
        raise ValueError('permissions must be a non-empty collection')
    invalid = [p for p in permissions if p not in PERMISSIONS]
    if invalid:
        raise ValueError(f'unknown plugin permissions: {invalid}')
    consents = load_consents(policy)
    existing = set((consents.get(plugin) or {}).get('permissions') or ())
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    consents[plugin] = {
        'permissions': sorted(existing | set(permissions)),
        'granted_at': stamp,
    }
    _save_consents(policy, consents)
    return consents[plugin]


def revoke_consent(policy, plugin, permissions=None):
    """Revoke all or selected permissions for `plugin`."""
    consents = load_consents(policy)
    if plugin not in consents:
        return {'permissions': [], 'revoked': True}
    if permissions is None:
        consents.pop(plugin, None)
        _save_consents(policy, consents)
        return {'permissions': [], 'revoked': True}
    remaining = sorted(set(consents[plugin].get('permissions') or ()) - set(permissions))
    if remaining:
        consents[plugin]['permissions'] = remaining
    else:
        consents.pop(plugin, None)
    _save_consents(policy, consents)
    return {'permissions': remaining, 'revoked': True}


def check_permission(policy, manifest, required_permission):
    """Verify opt-in `enabled`, manifest declaration, and user consent for `required_permission`."""
    if not isinstance(manifest, dict) or not manifest.get('name'):
        raise ConsentRefused('invalid plugin manifest')
    name = manifest['name']
    if not manifest.get('enabled', False):
        raise ConsentRefused(f'plugin {name!r} is disabled (opt-in required)')
    declared = manifest.get('permissions') or []
    if required_permission not in declared:
        raise ConsentRefused(f'plugin {name!r} manifest does not declare permission {required_permission!r}')
    consents = load_consents(policy)
    granted = set((consents.get(name) or {}).get('permissions') or ())
    if required_permission not in granted:
        raise ConsentRefused(f'plugin {name!r} lacks user consent for {required_permission!r}')
    return True


def _redact_terms(value, terms):
    if isinstance(value, str):
        out = security.redact(value)
        for term in terms or ():
            if term:
                out = re.sub(re.escape(str(term)), security.REDACTED, out, flags=re.I)
        return out
    if isinstance(value, list):
        return [_redact_terms(item, terms) for item in value]
    if isinstance(value, dict):
        return {k: _redact_terms(v, terms) for k, v in value.items()}
    return value


def log_plugin_invocation(policy, plugin, action, status='ok', detail=None, now=None):
    """Append a redacted audit entry to `<state>/logs/plugins.jsonl` (`0600`) with zero secrets."""
    terms = td.private_terms(policy) if policy else None
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
    entry = _redact_terms({
        'at': stamp,
        'plugin': str(plugin),
        'action': str(action),
        'status': str(status),
        'detail': detail,
    }, terms)
    path = _log_path(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(entry, sort_keys=True) + '\n')
    os.chmod(path, 0o600)
    return entry
