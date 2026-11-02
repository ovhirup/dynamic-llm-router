"""Opt-in setup routing respects every allocation gate and read-only plan (#210)."""
import argparse
import contextlib
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import load_policy
from llm_router import allocation_cli, cli, config, roles, selection, setup_profile
from llm_router.setup_wizard import build_profile
from test_allocation_selection import observation
from test_session_preferences import policy
from test_usage_snapshot import NOW, snapshot


def configured(workers=('codex',), orchestrators=('codex',), preferred='codex', **kwargs):
    return policy(workers=workers, orchestrators=orchestrators, preferred=preferred, **kwargs)


def run(argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(argv)
    return code, output.getvalue()


class SetupRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cfg = self.base / 'config'
        self.cfg.mkdir()
        self.state = self.base / 'state'
        patch = mock.patch.dict(os.environ, {'LLM_ROUTER_CONFIG_DIR': str(self.cfg)}, clear=False)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop('LLM_ROUTER_CONFIG', None)
        state = mock.patch.object(config, 'state_dir', return_value=self.state)
        state.start()
        self.addCleanup(state.stop)

    def write_profile(self):
        data = configured()
        fragment = {key: data[key] for key in ('setup', 'worker_order', 'quota', 'unmetered_workers',
                                               'workers', 'class_order', 'role_order', 'allocation')}
        fragment['quota'] = {'codex': {'enabled': True}}
        fragment['workers'] = {}
        setup_profile.write_profile(fragment, self.cfg / 'setup.json', now=NOW)
        return fragment

    def test_profile_autoload_leaves_legacy_file_unchanged_and_path_precedence(self):
        legacy = self.cfg / 'config.json'
        legacy.write_text('{"worker_order":["cursor"],"unmetered_workers":["cursor"]}')
        previous = legacy.read_bytes()
        self.write_profile()
        self.assertEqual(config.load_policy()['worker_order'], ['codex'])
        self.assertEqual(legacy.read_bytes(), previous)
        explicit = self.cfg / 'chosen.json'
        explicit.write_text('{"worker_order":["claude"],"unmetered_workers":["claude"]}')
        os.environ['LLM_ROUTER_CONFIG'] = str(explicit)
        self.assertEqual(config.load_policy()['worker_order'], ['claude'])
        self.assertEqual(config.load_policy(legacy)['worker_order'], ['cursor'])
        self.assertEqual(config.config_file(), explicit)
        self.assertEqual(legacy.read_bytes(), previous)

    def test_invalid_setup_rejected_when_loading_merged_defaults(self):
        (self.cfg / 'setup.json').write_text(json.dumps({'setup': {'version': 99}}))
        with self.assertRaises(config.ConfigError):
            config.load_policy()

    def test_disabled_candidates_and_auto_appended_bot_are_filtered_everywhere(self):
        configured_policy = configured(workers=(), orchestrators=('codex',))
        configured_policy['worker_order'] = ['codex']
        self.assertEqual(config.worker_order(configured_policy), [])
        self.assertEqual(config.worker_order(configured_policy, 'feature'), [])
        self.assertTrue(config.blocked_worker(configured_policy, 'codex'))
        result = selection.select(10000, policy=configured_policy, io={}, now=NOW, task_class='feature')
        self.assertEqual(result['eligible'], [])
        self.assertEqual(roles.role_order(configured_policy, 'coder'), [])
        self.assertIsNone(roles.select_for_role('coder', configured_policy, eligible=[])['worker'])
        with self.assertRaises(config.ConfigError):
            selection.select_for_worker(10000, 'codex', policy=configured_policy, io={}, now=NOW)

    def test_setup_disable_guard_precedes_special_bot_branch(self):
        configured_policy = configured()
        with mock.patch.object(config, 'worker_order', return_value=['grok-bot']):
            result = selection.select(10000, policy=configured_policy, io={}, now=NOW)
        self.assertNotIn('grok-bot', result['eligible'])
        self.assertEqual(result['reasons']['grok-bot'], 'setup_disabled')

    def test_profile_without_bot_has_no_fallback_when_class_worker_is_exhausted(self):
        configured_policy = configured()
        result = selection.select(10000, policy=configured_policy, now=NOW, task_class='feature',
                                  io={'codex': {'load': lambda: observation('codex', used=100),
                                                'observe': lambda p: self.fail('live observer'), 'save': lambda row: None}})
        self.assertIsNone(result['worker'])
        self.assertNotIn('grok-bot', result['eligible'])
        self.assertIsNone(roles.select_for_role('judge', configured_policy, eligible=[])['worker'])
        self.assertEqual(roles.select_for_role('judge', config.defaults(), eligible=[])['worker'], 'grok-bot')

    def test_one_worker_allocation_keeps_reserve_and_reads_only_that_provider(self):
        configured_policy = configured(workers=('codex', 'cursor'))
        configured_policy['allocation']['providers']['codex']['reserve_percent'] = 20
        previous = deepcopy(configured_policy)
        calls = []
        io_map = {name: {'load': lambda name=name: (calls.append(name) or observation(name, used=81)),
                         'observe': lambda p: self.fail('live observer'), 'save': lambda row: None}
                  for name in ('codex', 'cursor')}
        result = selection.select_for_worker(10000, 'codex', policy=configured_policy, io=io_map, now=NOW,
                                             role='coder', cached_only=True)
        self.assertIsNone(result['worker'])
        self.assertEqual(result['allocation']['excluded']['codex'], 'reserve_floor')
        self.assertEqual(calls, ['codex'])
        self.assertEqual(configured_policy, previous)

    def test_one_worker_role_fit_override_keeps_judge_local_prohibition(self):
        configured_policy = configured(workers=('aider-ollama',), orchestrators=('cli',), preferred='cli')
        previous = deepcopy(configured_policy)
        result = selection.select_for_worker(10000, 'aider-ollama', policy=configured_policy,
                                             io={}, now=NOW, role='judge')
        self.assertIsNone(result['worker'])
        self.assertEqual(result['allocation']['excluded']['aider-ollama'], 'role_not_allowed')
        self.assertEqual(configured_policy, previous)

    def test_read_only_pin_is_explained_and_does_not_bypass_reserve(self):
        configured_policy = configured(worker_overrides={
            'default': {'worker': 'codex', 'reason': 'explicit bulk-work preference'}, 'roles': {}})
        configured_policy['allocation']['providers']['codex']['reserve_percent'] = 20
        args = argparse.Namespace(size='full', role='coder', task_class=None, cached_only=True)
        original = selection.select
        def collect(chars, **kwargs):
            return original(chars, now=NOW, io={'codex': {'load': lambda: observation('codex', used=81),
                                                        'observe': lambda p: self.fail('observer'), 'save': lambda row: None}},
                            **kwargs)
        with mock.patch.object(selection, 'select', side_effect=collect):
            result = allocation_cli.select_result(args, configured_policy)
        self.assertIsNone(result['worker'])
        self.assertIn('bulk-work preference', result['worker_override']['reason'])
        self.assertEqual(result['allocation']['excluded']['codex'], 'reserve_floor')
        self.assertFalse(self.state.exists())

    def test_plan_explain_has_default_disabled_capability_and_pin_reasons_without_audit(self):
        configured_policy = configured(orchestrators=('codex', 'grok-bot'), preferred='grok-bot',
            worker_overrides={'default': {'worker': 'codex', 'reason': 'explicit bulk-work preference'}, 'roles': {}})
        original = selection.select
        def collect(chars, **kwargs):
            return original(chars, now=NOW, io={'codex': {'load': lambda: observation('codex'),
                                                        'observe': lambda p: self.fail('observer'), 'save': lambda row: None}},
                            **kwargs)
        with mock.patch.object(config, 'load_policy', return_value=configured_policy):
            with mock.patch.object(selection, 'select', side_effect=collect):
                code, output = run(['plan', '--explain', '--cached-only'])
        self.assertEqual(code, 0)
        self.assertIn('bulk-work preference', output)
        self.assertIn('phone', output)
        self.assertIn('disabled', output.lower())
        self.assertIn('reset', output.lower())
        self.assertFalse(self.state.exists())

    def test_offline_explain_and_overrides_stay_entirely_offline(self):
        path = self.base / 'captured.json'
        path.write_text(json.dumps(snapshot()))
        with mock.patch.object(config, 'load_policy', side_effect=AssertionError('config read')):
            with mock.patch('llm_router.quota_service.default_io', side_effect=AssertionError('observer read')):
                with mock.patch.object(config, 'state_dir', side_effect=AssertionError('state IO')):
                    code, output = run(['plan', '--snapshot', str(path), '--explain', '--worker', 'claude',
                                        '--orchestrator', 'cli', '--json'])
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(result['worker'], 'claude')
        self.assertEqual(result['order'], ['claude'])
        self.assertIn('not captured', json.dumps(result['explanation']))
        self.assertEqual(result['orchestrator_preview']['orchestrator'], 'cli')

    def test_offline_unknown_worker_reports_exclusion(self):
        path = self.base / 'captured.json'
        path.write_text(json.dumps(snapshot()))
        code, output = run(['plan', '--snapshot', str(path), '--worker', 'uncaptured-worker', '--json'])
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertIsNone(result['worker'])
        self.assertIn('uncaptured-worker', result['excluded'])
