"""Pluggable quota observers.

An observer is any callable `observe(policy, now=None, **kw) -> observation`, named in config as
`quota.<worker>.observer = "package.module:function"`. An observation is
`{'evidence': {...} | None, 'status': 'ok' | 'unknown', 'reason': str | None, 'cli_version', 'version_warning'}`.

`observe_isolated` runs the observer in a child Python process in its own session with a hard
wall-clock timeout, so a hung CLI can never block quota refresh or selection: on timeout the whole
process group is killed and the reading is 'unknown'.
"""
import importlib
import json
import os
import sys

from .. import config
from . import common


def load_observer(spec):
    if not isinstance(spec, str) or ':' not in spec:
        raise ValueError(f'observer must be "module:function", got {spec!r}')
    module, _, name = spec.partition(':')
    return getattr(importlib.import_module(module), name)


def observe_inline(policy, worker):
    quota = config.quota_cfg(policy, worker) or {}
    try:
        result = load_observer(quota.get('observer'))(policy, worker=worker)
    except TypeError:
        result = load_observer(quota.get('observer'))(policy)
    return normalise(result)


def normalise(result):
    if not isinstance(result, dict):
        return common.unknown('observer_returned_nothing')
    evidence = result.get('evidence')
    out = {'evidence': evidence if isinstance(evidence, dict) else None,
           'status': 'ok' if isinstance(evidence, dict) else 'unknown',
           'reason': result.get('reason') if not isinstance(evidence, dict) else None,
           'cli_version': result.get('cli_version'), 'version_warning': result.get('version_warning')}
    if out['status'] == 'unknown' and not out['reason']:
        out['reason'] = 'no_evidence'
    return out


def hard_timeout(policy, worker):
    quota = config.quota_cfg(policy, worker) or {}
    value = quota.get('hard_timeout_seconds')
    if not common.valid_timeout(value):
        value = float(quota.get('capture_timeout_seconds') or quota.get('app_server_timeout_seconds') or 30) + 20
    return value


def observe_isolated(policy, worker, timeout=None, runner=common.run_capture):
    """Run the observer in `python -m llm_router.observers._child` with a hard timeout."""
    timeout = timeout or hard_timeout(policy, worker)
    env = dict(os.environ)
    env['PYTHONPATH'] = os.pathsep.join(p for p in sys.path if p)
    payload = json.dumps({'policy': policy, 'worker': worker})
    code, out = runner([sys.executable, '-m', 'llm_router.observers._child'], timeout, env=env,
                       input_text=payload)
    if code is None:
        return common.unknown('hard_timeout', timeout_seconds=timeout)
    try:
        return normalise(json.loads(out.strip().splitlines()[-1]))
    except (ValueError, IndexError):
        return common.unknown('observer_crashed', returncode=code)
