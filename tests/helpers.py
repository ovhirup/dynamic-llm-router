"""Test helpers: every test runs against temp config/state dirs and fake workers only.

Importing this module points LLM_ROUTER_CONFIG_DIR / LLM_ROUTER_STATE_DIR at a throwaway directory,
so no test can read or write the real user config, caches or ledger, and no real agent CLI is run.
"""
import atexit
import copy
import os
import json
from pathlib import Path
import shutil
import tempfile
import unittest

_ROOT = tempfile.mkdtemp(prefix='llm-router-tests-')
atexit.register(shutil.rmtree, _ROOT, True)
os.environ['LLM_ROUTER_CONFIG_DIR'] = os.path.join(_ROOT, 'config')
os.environ['LLM_ROUTER_STATE_DIR'] = os.path.join(_ROOT, 'state')
os.environ.pop('LLM_ROUTER_CONFIG', None)

Path(os.environ['LLM_ROUTER_CONFIG_DIR']).mkdir(parents=True)
Path(os.environ['LLM_ROUTER_CONFIG_DIR'], 'config.json').write_text(json.dumps({'worker_order': ['codex', 'grok-build', 'cursor', 'developer']}))

from llm_router import config, cooldown  # noqa: E402

# A quota-error test persists a cooldown; clear it so no other test inherits a cooled worker.
_run = unittest.TestCase.run


def _run_without_cooldowns(self, result=None):
    cooldown.path().unlink(missing_ok=True)
    return _run(self, result)


unittest.TestCase.run = _run_without_cooldowns

# A private term used only in tests (any made-up word works).
TERM = 'Zephyrquill'


def load_policy(**override):
    policy = copy.deepcopy(config.load_policy(override=override or None))
    return policy
