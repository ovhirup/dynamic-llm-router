"""First-run setup uses fake detection, fake bounded runners and temp profiles (#210)."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import load_policy
from llm_router import config

MARKER = 'DLR_SETUP_OK'


def fake_which(installed):
    return lambda name, **kwargs: '/fake/' + name if name in installed else None


def answers(enabled=None, *, manual=False, consent=True, overrides=False):
    enabled = enabled or {}
    def read(prompt):
        if 'available in chat' in prompt:
            return 'y' if manual else 'n'
        if 'enable as worker' in prompt:
            name = prompt.split(':', 1)[0]
            return 'y' if enabled.get(name, {}).get('worker') else 'n'
        if 'enable as orchestrator' in prompt:
            name = prompt.split(':', 1)[0]
            return 'y' if enabled.get(name, {}).get('orchestrator') else 'n'
        if 'run tiny smoke' in prompt:
            return 'y' if consent else 'n'
        if 'Paste the exact reply' in prompt:
            return MARKER
        if 'Configure per-role overrides' in prompt:
            return 'y' if overrides else 'n'
        return ''
    return read


class SetupWizardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'setup.json'
        self.calls = []
        self.messages = []

    def runner(self, argv, cwd, timeout, grace, **kwargs):
        cwd = Path(cwd)
        self.assertTrue((cwd / '.git').is_dir())
        self.assertLessEqual(timeout, 30)
        self.assertLessEqual(grace, 2)
        self.assertFalse(cwd.is_relative_to(self.output.parent))
        self.calls.append((list(argv), cwd, kwargs.get('input_text')))
        return {'returncode': 0, 'timed_out': False, 'stdout': MARKER + '\n', 'stderr': '', 'elapsed_seconds': 0.1}

    def run_setup(self, enabled=None, *, installed=(), read=None, **kwargs):
        from llm_router import setup_wizard
        return setup_wizard.run_setup(self.output, input_fn=read or answers(enabled),
                                       output_fn=self.messages.append, which=fake_which(installed),
                                       runner=self.runner, env={'PATH': '/fake'}, **kwargs)

    def test_detection_is_not_auth_and_local_requires_both_binaries(self):
        from llm_router.setup_wizard import detect_tools
        detected = detect_tools(which=fake_which(('codex', 'ollama')), env={'PATH': '/fake'})
        self.assertTrue(detected['codex']['installed'])
        self.assertEqual(detected['codex']['auth'], 'unknown')
        self.assertFalse(detected['aider-ollama']['installed'])
        self.assertIsNone(detected['grok-bot']['installed'])
        detected = detect_tools(which=fake_which(('ollama', 'aider')))
        self.assertTrue(detected['aider-ollama']['installed'])

    def test_disabled_tools_never_smoke_and_rationales_are_visible(self):
        result = self.run_setup(installed=('claude', 'codex', 'grok', 'cursor-agent', 'ollama', 'aider'))
        self.assertEqual(self.calls, [])
        self.assertEqual(result['config']['setup']['enabled_workers'], [])
        self.assertEqual(result['config']['setup']['enabled_orchestrators'], [])
        text = '\n'.join(self.messages)
        self.assertIn('shared weekly', text)
        self.assertIn('phone', text)
        self.assertIn('local', text.lower())
        self.assertEqual(Path(result['written']), self.output.resolve())

    def test_worker_and_orchestrator_are_independent_and_smoked_once(self):
        result = self.run_setup({'codex': {'orchestrator': True}}, installed=('codex',))
        setup = result['config']['setup']
        self.assertEqual(setup['enabled_workers'], [])
        self.assertEqual(setup['enabled_orchestrators'], ['codex'])
        self.assertEqual(setup['preferred_orchestrator'], 'codex')
        self.assertEqual(setup['switch_policy']['mode'], 'always_ask')
        self.assertEqual(result['config']['quota']['codex']['enabled'], True)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('exec', self.calls[0][0])
        self.assertEqual(self.calls[0][0][-1], '-')
        self.assertIn(MARKER, self.calls[0][2])
        self.assertFalse(self.calls[0][1].exists())

    def test_declined_smoke_keeps_requested_tool_enabled_but_unverified(self):
        requested = {'codex': {'worker': True, 'orchestrator': True}}
        result = self.run_setup(requested, installed=('codex',), read=answers(requested, consent=False))
        self.assertEqual(self.calls, [])
        self.assertEqual(result['config']['worker_order'], ['codex'])
        setup = result['config']['setup']
        self.assertEqual(setup['enabled_orchestrators'], ['codex'])
        tool = setup['tools']['codex']
        self.assertEqual((tool['smoke']['passed'], tool['smoke']['status'], tool['auth']), (False, 'unverified', 'unknown'))
        self.assertEqual(tool['smoke']['reason'], 'consent_declined')
        self.assertNotIn('codex', result['config']['unmetered_workers'])  # still metered, never silently unpaced

    def test_all_tools_enabled_with_every_smoke_declined_stay_enabled_regression_212(self):
        from llm_router import setup_profile
        requested = {name: {'worker': True, 'orchestrator': True} for name in setup_profile.WORKERS}
        installed = ('claude', 'codex', 'grok', 'cursor-agent', 'ollama', 'aider')
        result = self.run_setup(requested, installed=installed, read=answers(requested, manual=True, consent=False))
        self.assertEqual(self.calls, [])
        setup = result['config']['setup']
        self.assertEqual(setup['enabled_workers'], list(setup_profile.WORKERS))
        self.assertEqual(setup['enabled_orchestrators'], list(setup_profile.WORKERS))
        self.assertEqual(setup['preferred_orchestrator'], 'grok-bot')
        for name in setup_profile.WORKERS:
            with self.subTest(tool=name):
                self.assertEqual(result['smoke'][name]['status'], 'unverified')
                self.assertEqual(setup['tools'][name]['auth'], 'unknown')
        self.assertEqual(result['config']['unmetered_workers'], ['aider-ollama'])
        saved = json.loads(self.output.read_text())
        self.assertEqual(setup_profile.validate_profile(saved)['setup']['enabled_workers'], list(setup_profile.WORKERS))
        self.assertTrue(any('unverified' in line for line in self.messages))
        self.assertFalse(any('excluded' in line for line in self.messages))

    def test_failed_smoke_records_failure_but_keeps_tool_enabled(self):
        requested = {'codex': {'worker': True}}
        self.runner = lambda argv, cwd, timeout, grace, **kwargs: {
            'returncode': 1, 'timed_out': False, 'stdout': '', 'stderr': 'not signed in'}
        result = self.run_setup(requested, installed=('codex',))
        setup = result['config']['setup']
        self.assertEqual(setup['enabled_workers'], ['codex'])
        tool = setup['tools']['codex']
        self.assertEqual((tool['smoke']['passed'], tool['smoke']['status'], tool['auth']), (False, 'failed', 'unknown'))

    def test_manual_grok_bot_is_user_verified_and_preferred_without_a_cli(self):
        requested = {'grok-bot': {'worker': True, 'orchestrator': True}}
        result = self.run_setup(requested, read=answers(requested, manual=True))
        self.assertEqual(self.calls, [])
        setup = result['config']['setup']
        self.assertEqual(setup['tools']['grok-bot']['auth'], 'user_verified')
        self.assertEqual(setup['preferred_orchestrator'], 'grok-bot')
        self.assertEqual(result['config']['worker_order'], ['grok-bot'])
        self.assertNotIn('grok-bot', result['config'].get('workers', {}))

    def test_safe_packaged_file_pointer_and_local_model_routes(self):
        from llm_router.setup_wizard import smoke_tool
        for name in ('grok-build', 'cursor', 'aider-ollama'):
            with self.subTest(worker=name):
                result = smoke_tool(name, config.defaults(), runner=self.runner, env={'PATH': '/fake'})
                self.assertTrue(result['passed'])
                argv, cwd, stdin = self.calls[-1]
                self.assertNotIn(MARKER, ' '.join(argv))
                self.assertIsNone(stdin)
                self.assertFalse(cwd.exists())
                if name == 'aider-ollama':
                    self.assertEqual(Path(argv[0]).name, 'aider')
                    self.assertIn('ollama_chat/qwen2.5-coder:14b', argv)

    def test_echo_only_user_json_timeouts_errors_and_claude_quota_do_not_verify(self):
        from llm_router.setup_wizard import smoke_tool
        cases = [({'stdout': 'Reply exactly ' + MARKER}, 'codex'),
                 ({'stdout': json.dumps({'type': 'user', 'message': {'content': MARKER}})}, 'claude'),
                 ({'timed_out': True, 'stdout': MARKER}, 'codex'),
                 ({'returncode': 1, 'stdout': MARKER}, 'codex'),
                 ({'stdout': json.dumps({'type': 'result', 'is_error': True, 'result': MARKER})}, 'claude'),
                 ({'stdout': MARKER, 'stderr': "You've hit your usage limit"}, 'claude')]
        for extra, worker in cases:
            with self.subTest(worker=worker, case=list(extra)):
                def failed(*args, **kwargs):
                    return {'returncode': 0, 'timed_out': False, 'stdout': '', 'stderr': '', **extra}
                result = smoke_tool(worker, config.defaults(), runner=failed, env={'PATH': '/fake'})
                self.assertFalse(result['passed'])
                self.assertEqual(result['auth'], 'unknown')
                self.assertNotIn('stdout', result)
                self.assertNotIn('stderr', result)

    def test_reserves_and_both_role_pin_types_with_reasons(self):
        enabled = {'codex': {'worker': True, 'orchestrator': True},
                   'grok-bot': {'orchestrator': True}}
        base = answers(enabled, manual=True, overrides=True)
        def read(prompt):
            if 'Reserve for codex' in prompt:
                return '25'
            if 'Worker pin for coder' in prompt:
                return 'codex'
            if 'Orchestrator pin for judge' in prompt:
                return 'grok-bot'
            if 'Reason for' in prompt:
                return 'explicit capability choice'
            return base(prompt)
        result = self.run_setup(enabled, installed=('codex',), read=read)
        data = result['config']
        self.assertEqual(data['allocation']['providers']['codex']['reserve_percent'], 25)
        self.assertEqual(data['setup']['worker_overrides']['roles']['coder']['worker'], 'codex')
        self.assertEqual(data['setup']['orchestrator_overrides']['roles']['judge']['orchestrator'], 'grok-bot')
        self.assertTrue(data['setup']['worker_overrides']['roles']['coder']['reason'])

    def test_cli_orchestrator_requires_explicit_enablement_and_no_cloud_smoke(self):
        result = self.run_setup({'cli': {'orchestrator': True}})
        self.assertEqual(result['config']['setup']['enabled_orchestrators'], ['cli'])
        self.assertEqual(result['config']['worker_order'], [])
        self.assertEqual(self.calls, [])
