"""`llm-router setup`, logged dispatch overrides and orchestrator switching from the CLI (#210)."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (isolates config/state dirs)
from llm_router import cli, config, job_dispatch, orchestrator, session_preferences, setup_profile, task_dispatch
from test_session_preferences import policy
from test_setup_wizard import answers, fake_which
from llm_router.setup_wizard import build_profile
from test_usage_snapshot import NOW

MARKER = 'DLR_SETUP_OK'


def run(argv):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = cli.main(argv)
    return code, output.getvalue(), errors.getvalue()


def fragment(mode='always_ask', preferred='grok-bot', workers=('codex', 'claude'),
             orchestrators=('codex', 'grok-bot', 'cli'), **overrides):
    """The exact profile the wizard would write (not merged with packaged defaults)."""
    merged = policy(mode=mode, preferred=preferred, workers=workers, orchestrators=orchestrators, **overrides)
    return build_profile(merged['setup']['tools'], preferred_orchestrator=preferred,
                         switch_policy=merged['setup']['switch_policy'], **overrides)


class SetupCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cfg = self.base / 'config'
        self.cfg.mkdir()
        self.state = self.base / 'state'
        env = mock.patch.dict(os.environ, {'LLM_ROUTER_CONFIG_DIR': str(self.cfg),
                                           'LLM_ROUTER_STATE_DIR': str(self.state)}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ('LLM_ROUTER_CONFIG', 'LLM_ROUTER_ORCHESTRATOR', 'LLM_ROUTER_USER_AGENT', 'TERM_PROGRAM'):
            os.environ.pop(key, None)
        for _, keys in orchestrator.ENV_DETECTORS:
            for key in keys:
                os.environ.pop(key, None)
        collect = mock.patch.object(session_preferences, '_collect', return_value={})
        collect.start()
        self.addCleanup(collect.stop)

    def audit(self):
        path = self.state / 'logs' / 'setup-decisions.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def write_profile(self, **kwargs):
        setup_profile.write_profile(fragment(**kwargs), self.cfg / 'setup.json', now=NOW)

    # -- config layering -------------------------------------------------------------------------

    def test_setup_profile_overlays_legacy_settings_without_editing_them(self):
        legacy = self.cfg / 'config.json'
        legacy.write_text(json.dumps({'worker_order': ['cursor'], 'unmetered_workers': ['cursor'],
                                      'binaries': {'codex': '/opt/bin/codex'}, 'timezone': 'UTC'}))
        before = legacy.read_bytes()
        self.write_profile()
        loaded = config.load_policy()
        self.assertEqual(loaded['worker_order'], ['claude', 'codex'])
        self.assertEqual(loaded['binaries']['codex'], '/opt/bin/codex')
        self.assertEqual(loaded['timezone'], 'UTC')
        self.assertEqual(loaded['unmetered_workers'], ['cursor'])
        self.assertEqual(legacy.read_bytes(), before)

    def test_legacy_never_and_disabled_meters_win_over_the_setup_profile(self):
        legacy = self.cfg / 'config.json'
        legacy.write_text(json.dumps({'never': ['claude'], 'quota': {'codex': {'enabled': False}}}))
        self.write_profile(worker_overrides={'default': None,
                                             'roles': {'coder': {'worker': 'claude', 'reason': 'design'}}})
        loaded = config.load_policy()
        self.assertEqual(loaded['worker_order'], ['codex'])
        self.assertNotIn('claude', json.dumps(loaded['class_order']) + json.dumps(loaded['role_order']))
        self.assertTrue(config.blocked_worker(loaded, 'claude'))
        self.assertFalse(loaded['quota']['codex']['enabled'])

    # -- setup command ---------------------------------------------------------------------------

    def test_setup_requires_an_interactive_terminal_and_writes_nothing(self):
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=False):
            code, output, _ = run(['setup'])
        self.assertEqual(code, 2)
        self.assertIn('interactive', json.loads(output)['config_error'])
        self.assertFalse((self.cfg / 'setup.json').exists())

    def test_setup_runs_before_legacy_config_is_loaded_and_reports_summary(self):
        from llm_router import setup_wizard
        result = {'config': {'setup': {'enabled_workers': ['codex'], 'enabled_orchestrators': ['grok-bot'],
                                       'preferred_orchestrator': 'grok-bot',
                                       'switch_policy': {'mode': 'always_ask', 'target': None}}},
                  'detected': {}, 'smoke': {'codex': {'passed': True, 'status': 'passed'}},
                  'written': str(self.cfg / 'setup.json'), 'backup': None}
        with mock.patch.object(cli.sys.stdin, 'isatty', return_value=True), \
                mock.patch.object(config, 'load_policy', side_effect=AssertionError('legacy read')), \
                mock.patch.object(setup_wizard, 'run_setup', return_value=result) as wizard:
            code, output, _ = run(['setup', '--output', str(self.base / 'chosen.json'), '--dry-run'])
        self.assertEqual(code, 0)
        self.assertEqual(wizard.call_args.args[0], str(self.base / 'chosen.json'))
        self.assertTrue(wizard.call_args.kwargs['dry_run'])
        summary = json.loads(output)
        self.assertEqual(summary['enabled_workers'], ['codex'])
        self.assertEqual(summary['preferred_orchestrator'], 'grok-bot')
        self.assertEqual(summary['smoke'], {'codex': 'passed'})

    def test_wizard_dry_run_smokes_but_never_writes(self):
        from llm_router import setup_wizard
        messages = []
        runner = lambda argv, cwd, timeout, grace, **kw: {'returncode': 0, 'timed_out': False,
                                                         'stdout': MARKER + '\n', 'stderr': ''}
        output = self.base / 'setup.json'
        result = setup_wizard.run_setup(output, input_fn=answers({'codex': {'worker': True}}),
                                        output_fn=messages.append, which=fake_which(('codex',)),
                                        runner=runner, env={'PATH': '/fake'}, dry_run=True)
        self.assertFalse(output.exists())
        self.assertIsNone(result['written'])
        self.assertEqual(result['config']['setup']['enabled_workers'], ['codex'])
        self.assertTrue(any('dry run' in line.lower() for line in messages))

    def test_wizard_shows_recommended_default_next_to_each_tool(self):
        from llm_router import setup_wizard
        messages = []
        setup_wizard.run_setup(self.base / 'setup.json', input_fn=answers(), output_fn=messages.append,
                               which=fake_which(('codex',)), runner=None, env={'PATH': '/fake'}, dry_run=True)
        bot = [line for line in messages if line.startswith('grok-bot: recommended')]
        self.assertEqual(len(bot), 1)
        self.assertIn('orchestrator yes', bot[0])
        self.assertIn('worker last resort', bot[0])
        self.assertTrue(any(line.startswith('codex: recommended') and 'worker yes' in line for line in messages))

    def test_accepting_defaults_keeps_grok_bot_as_last_resort_worker(self):
        from llm_router import setup_wizard
        def read(prompt):
            if 'available in chat' in prompt or 'run tiny smoke' in prompt:
                return 'y'
            return MARKER if 'Paste the exact reply' in prompt else ''
        result = setup_wizard.run_setup(self.base / 'setup.json', input_fn=read, output_fn=lambda line: None,
                                        which=fake_which(()), runner=None, env={'PATH': '/fake'}, dry_run=True)
        setup = result['config']['setup']
        self.assertEqual((setup['enabled_workers'], setup['preferred_orchestrator']), (['grok-bot'], 'grok-bot'))

    # -- worker overrides --------------------------------------------------------------------------

    def test_explicit_worker_override_is_logged_with_its_reason(self):
        task = self.base / 'task.md'
        task.write_text('# t\n')
        with mock.patch.object(task_dispatch, 'run_task', return_value={'ok': True}) as run_task:
            code, output, _ = run(['run-task', '--task-file', str(task), '--cwd', str(self.base),
                                   '--worker', 'codex', '--override-reason', 'needs codex sandbox'])
        self.assertEqual(code, 0)
        self.assertEqual(run_task.call_args.kwargs['worker'], 'codex')
        self.assertEqual(json.loads(output)['worker_override']['reason'], 'needs codex sandbox')
        [event] = self.audit()
        self.assertEqual((event['event'], event['worker'], event['source'], event['reason']),
                         ('worker_override', 'codex', 'explicit_flag', 'needs codex sandbox'))

    def test_saved_role_pin_drives_dispatch_and_is_logged(self):
        self.write_profile(worker_overrides={'default': None,
                                             'roles': {'coder': {'worker': 'claude', 'reason': 'design-heavy repo'}}})
        task = self.base / 'task.md'
        task.write_text('# t\n')
        with mock.patch.object(job_dispatch, 'run_job', return_value={'ok': True, 'status': 'committed'}) as run_job:
            code, output, _ = run(['run-job', '--repo', str(self.base), '--task-file', str(task), '--role', 'coder'])
        self.assertEqual(code, 0)
        self.assertEqual(run_job.call_args.kwargs['worker'], 'claude')
        events = [e for e in self.audit() if e['event'] == 'worker_override']
        self.assertEqual([(e['worker'], e['source'], e['reason']) for e in events],
                         [('claude', 'role_pin', 'design-heavy repo')])

    def test_legacy_dispatch_without_overrides_is_unchanged_and_unlogged(self):
        task = self.base / 'task.md'
        task.write_text('# t\n')
        with mock.patch.object(job_dispatch, 'run_job', return_value={'ok': True, 'status': 'committed'}) as run_job:
            code, output, _ = run(['run-job', '--repo', str(self.base), '--task-file', str(task)])
        self.assertEqual(code, 0)
        self.assertIsNone(run_job.call_args.kwargs['worker'])
        self.assertIsNone(run_job.call_args.kwargs['orchestrator_decision'])
        self.assertEqual(self.audit(), [])
        self.assertNotIn('worker_override', json.loads(output))

    def test_disabled_worker_override_is_refused_before_dispatch(self):
        self.write_profile()
        task = self.base / 'task.md'
        task.write_text('# t\n')
        with mock.patch.object(task_dispatch, 'run_task') as run_task:
            code, output, _ = run(['run-task', '--task-file', str(task), '--cwd', str(self.base), '--worker', 'cursor'])
        self.assertEqual(code, 2)
        self.assertIn('disabled', json.loads(output)['config_error'])
        run_task.assert_not_called()

    # -- orchestrator switching -------------------------------------------------------------------

    def job(self, env=None, tty=False, answer=None, extra=()):
        task = self.base / 'task.md'
        task.write_text('# t\n')
        prompts = []
        def read(prompt=''):
            prompts.append(prompt)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        with mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(cli, '_interactive', return_value=tty), \
                mock.patch('builtins.input', side_effect=read), \
                mock.patch.object(job_dispatch, 'run_job', return_value={'ok': True, 'status': 'committed'}) as run_job:
            code, output, errors = run(['run-job', '--repo', str(self.base), '--task-file', str(task), *extra])
        return code, run_job.call_args.kwargs['orchestrator_decision'], prompts, errors

    def test_caller_is_proposed_but_not_switched_without_a_terminal(self):
        self.write_profile(preferred='grok-bot')
        code, decision, prompts, _ = self.job(env={'CODEX_THREAD_ID': 't'})
        self.assertEqual(code, 0)
        self.assertEqual(prompts, [])
        self.assertEqual((decision['orchestrator'], decision['proposed']), ('grok-bot', 'codex'))
        self.assertTrue(decision['confirmation_required'])
        [event] = self.audit()
        self.assertFalse(event['switched'])

    def test_interactive_confirmation_switches_to_caller_and_logs_it(self):
        self.write_profile(preferred='grok-bot')
        code, decision, prompts, errors = self.job(env={'CODEX_THREAD_ID': 't'}, tty=True, answer='yes')
        self.assertEqual(code, 0)
        self.assertEqual(prompts, [''])  # the question goes to stderr so stdout stays JSON
        self.assertIn('keep current', errors)
        self.assertIn('Recommendation', errors)
        self.assertEqual(decision['orchestrator'], 'codex')
        self.assertTrue(decision['switched'])
        [event] = self.audit()
        self.assertEqual((event['previous'], event['orchestrator'], event['switched']), ('grok-bot', 'codex', True))

    def test_cancelled_prompt_keeps_current_without_a_traceback(self):
        self.write_profile(preferred='grok-bot')
        code, decision, _, _ = self.job(env={'CODEX_THREAD_ID': 't'}, tty=True, answer=EOFError())
        self.assertEqual(code, 0)
        self.assertEqual(decision['orchestrator'], 'grok-bot')
        self.assertFalse(decision['switched'])

    def test_explicit_orchestrator_flag_is_logged_with_reason(self):
        code, decision, _, _ = self.job(extra=('--orchestrator', 'codex', '--override-reason', 'bot is offline'))
        self.assertEqual(decision['orchestrator'], 'codex')
        self.assertEqual(orchestrator.resolve('codex', decision)['reason'], 'explicit_flag')
        [event] = self.audit()
        self.assertEqual((event['event'], event['orchestrator'], event['reason']),
                         ('orchestrator_override', 'codex', 'bot is offline'))

    def test_one_job_orchestrator_override_does_not_change_the_saved_current(self):
        self.write_profile(preferred='grok-bot')
        code, decision, _, _ = self.job(extra=('--orchestrator', 'codex'))
        self.assertEqual((code, decision['orchestrator'], decision['switched']), (0, 'codex', False))
        code, output, _ = run(['orchestrator', 'status'])
        self.assertEqual(json.loads(output)['session']['current'], 'grok-bot')

    def test_cli_and_local_orchestrators_need_a_setup_profile(self):
        task = self.base / 'task.md'
        task.write_text('# t\n')
        with mock.patch.object(job_dispatch, 'run_job') as run_job:
            code, output, _ = run(['run-job', '--repo', str(self.base), '--task-file', str(task), '--orchestrator', 'cli'])
        self.assertEqual(code, 2)
        self.assertIn('setup profile', json.loads(output)['config_error'])
        run_job.assert_not_called()

    def test_job_summary_records_the_resolved_orchestrator_decision(self):
        decision = {'orchestrator': 'cli', 'reason': 'saved fixed orchestrator policy', 'source': 'always_use',
                    'switched': True, 'audit_path': '/x', 'recommendation': {'orchestrator': 'cli', 'reason': 'r',
                                                                            'budgets': {}, 'actions': []}}
        detected = orchestrator.resolve(None, decision)
        self.assertEqual(detected, {'orchestrator': 'cli', 'reason': 'saved fixed orchestrator policy'})
        self.assertEqual(orchestrator.resolve('codex', None)['reason'], 'explicit_flag')
        compact = orchestrator.decision_summary(decision)
        self.assertEqual(compact['source'], 'always_use')
        self.assertNotIn('budgets', json.dumps(compact))

    def test_switch_command_confirms_and_persists_current(self):
        self.write_profile(preferred='grok-bot')
        with mock.patch.object(cli, '_interactive', return_value=False):
            code, output, _ = run(['orchestrator', 'switch', 'codex', '--reason', 'grok weekly low'])
        self.assertEqual(code, 3)
        self.assertFalse(json.loads(output)['switched'])
        code, output, _ = run(['orchestrator', 'switch', 'codex', '--reason', 'grok weekly low', '--yes'])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)['switched'])
        self.assertEqual(self.audit()[-1]['orchestrator'], 'codex')
        code, output, _ = run(['orchestrator', 'status'])
        self.assertEqual(json.loads(output)['session']['current'], 'codex')
        with mock.patch.object(cli, '_interactive', return_value=True), \
                mock.patch('builtins.input', return_value='use recommended'):
            code, output, _ = run(['orchestrator', 'switch', 'cli', '--reason', 'try terminal'])
        self.assertEqual(code, 0)  # an accepted recommendation is a confirmed switch
        self.assertEqual((self.audit()[-1]['orchestrator'], self.audit()[-1]['switched']), ('grok-bot', True))

    def test_prefer_command_saves_switch_policy_with_backup(self):
        legacy = self.cfg / 'config.json'
        legacy.write_text('{}')
        code, output, _ = run(['orchestrator', 'prefer', '--mode', 'always_accept_caller'])
        self.assertEqual(code, 2)
        self.assertIn('llm-router setup', json.loads(output)['config_error'])
        self.write_profile()
        code, output, _ = run(['orchestrator', 'prefer', '--mode', 'always_use', '--target', 'cli'])
        self.assertEqual(code, 0)
        saved = json.loads((self.cfg / 'setup.json').read_text())['setup']
        self.assertEqual(saved['switch_policy'], {'mode': 'always_use', 'target': 'cli'})
        self.assertTrue(json.loads(output)['backup'])
        self.assertTrue(Path(json.loads(output)['backup']).exists())
        self.assertEqual(legacy.read_text(), '{}')
        code, output, _ = run(['orchestrator', 'prefer', '--mode', 'always_use', '--target', 'cursor'])
        self.assertEqual(code, 2)
        code, output, _ = run(['orchestrator', 'prefer', '--mode', 'always_ask', '--preferred', 'codex'])
        saved = json.loads((self.cfg / 'setup.json').read_text())['setup']
        self.assertEqual((saved['switch_policy']['mode'], saved['preferred_orchestrator']), ('always_ask', 'codex'))


if __name__ == '__main__':
    unittest.main()
