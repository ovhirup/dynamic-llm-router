"""Test helpers: every test runs against temp config/state dirs and fake workers only.

Importing this module points LLM_ROUTER_CONFIG_DIR / LLM_ROUTER_STATE_DIR at a throwaway directory,
so no test can read or write the real user config, caches or ledger, and no real agent CLI is run.
"""
import atexit
import copy
import os
import shutil
import tempfile

_ROOT = tempfile.mkdtemp(prefix='llm-router-tests-')
atexit.register(shutil.rmtree, _ROOT, True)
os.environ['LLM_ROUTER_CONFIG_DIR'] = os.path.join(_ROOT, 'config')
os.environ['LLM_ROUTER_STATE_DIR'] = os.path.join(_ROOT, 'state')
os.environ.pop('LLM_ROUTER_CONFIG', None)

from llm_router import config  # noqa: E402

# A private term used only in tests (any made-up word works).
TERM = 'Zephyrquill'


def load_policy(**override):
    policy = copy.deepcopy(config.load_policy(override=override or None))
    return policy
