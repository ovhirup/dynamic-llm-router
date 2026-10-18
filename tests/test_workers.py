"""Bring-any-worker regressions: fixture output and fake runners/executables only."""
import copy
import contextlib
import io
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from helpers import load_policy
from llm_router import cli, config, job_dispatch, quota_service, security, selection, task_dispatch as td
from llm_router.observers import claude, command, common
from test_task_dispatch import repo

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)
SCREEN = b'Current session XXX 12% used\nCurrent week (all models) XXX 34% used\n'


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = repo(self.base / 'repo')
        self.task = self.base / 'task.md'
        self.task.write_text('fixture task text')

    def policy(self, opts=None, **kw):
        return load_policy(worker_order=['sample'], workers={'sample': dict({'command': ['fake-worker']}, **(opts or {}))},
                           unmetered_workers=['sample'], run_task={'ledger': str(self.base / 'ledger.csv'),
                           'shim_dir': str(self.base / 'shims'), 'block_log': str(self.base / 'blocked.log')}, **kw)

    def run_worker(self, policy, runner, **kw):
        return td.run_task(self.task, self.repo, policy=policy, runner=runner, worker='sample',
                           output_root=self.base / 'out', **kw)

    def test_delivery_modes_and_warning(self):
        for mode in ('stdin', 'file', 'pointer', 'arg'):
            with self.subTest(mode=mode):
                placeholder = {'stdin': None, 'file': '{prompt_file}', 'pointer': '{prompt_pointer}', 'arg': '{prompt}'}[mode]
                opts = {'command': ['fake-worker'] + ([placeholder] if placeholder else []), 'prompt_delivery': mode}
                seen_files = []
                def runner(argv, cwd, *args, **kw):
                    if mode == 'stdin':
                        self.assertIn('fixture task text', kw['input_text'])
                    elif mode == 'arg':
                        self.assertIn('fixture task text', argv[-1])
                    else:
                        file = Path(argv[-1]) if mode == 'file' else next((self.repo / '.git/router-prompts').iterdir())
                        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                        self.assertIn('fixture task text', file.read_text())
                        self.assertNotIn('fixture task text', ' '.join(argv))
                        seen_files.append(file)
                    return {'returncode': 0, 'timed_out': False, 'stdout': 'done', 'stderr': '', 'elapsed_seconds': 0}
                result = self.run_worker(self.policy(opts), runner)
                self.assertTrue(result['ok'])
                self.assertEqual(any('process list' in w for w in result['warnings']), mode == 'arg')
                self.assertTrue(all(not p.exists() for p in seen_files))
        with self.assertRaises(config.ConfigError):
            self.policy({'command': ['fake-worker', '{prompt}']})

    def test_cwd_repo_and_escape(self):
        (self.repo / 'sub').mkdir()
        seen = []
        def runner(argv, cwd, *args, **kw):
            seen.append((argv, cwd))
            return {'returncode': 0, 'timed_out': False, 'stdout': '', 'stderr': '', 'elapsed_seconds': 0}
        self.run_worker(self.policy({'cwd': 'sub', 'command': ['fake-worker', '{cwd}', '{repo}']}), runner)
        self.assertEqual(seen[0], (['fake-worker', str(self.repo / 'sub'), str(self.repo)], self.repo / 'sub'))
        for cwd in ('../outside', '/outside'):
            with self.assertRaises(config.ConfigError):
                self.policy({'cwd': cwd})
        (self.repo / 'escape').symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(td.Refused, 'inside the checkout'):
            self.run_worker(self.policy({'cwd': 'escape'}), runner, allow_dirty=True)
        with self.assertRaisesRegex(config.ConfigError, 'bare name'):
            self.policy({'command': ['./fake-worker']})

    def test_environment_allow_and_claude_prefixes(self):
        policy = self.policy({'env': {'allow': ['KEPT_*', 'ANTHROPIC_*'], 'set': {'MODE': 'fixture'}}})
        with patch.dict(os.environ, {'KEPT_VALUE': 'yes', 'OTHER_VALUE': 'no', 'ANTHROPIC_TEST': 'yes',
                                     'CLAUDE_TEST': 'yes', 'LITELLM_TEST': 'no', 'HOME': '/fixture',
                                     'LC_TEST': 'C'}, clear=True):
            env, _ = td.worker_environment(policy, 'sample')
            self.assertEqual(env['KEPT_VALUE'], 'yes')
            self.assertNotIn('OTHER_VALUE', env)
            self.assertNotIn('ANTHROPIC_TEST', env)
            self.assertEqual(env['HOME'], '/fixture')
            self.assertEqual(env['LC_TEST'], 'C')
            self.assertEqual(env['MODE'], 'fixture')
            self.assertEqual(env['PATH'].split(os.pathsep)[0], str(self.base / 'shims'))
            env, _ = td.worker_environment(policy, 'claude')
            self.assertIn('ANTHROPIC_TEST', env)
            self.assertIn('CLAUDE_TEST', env)
            self.assertNotIn('LITELLM_TEST', env)
            env, _ = td.worker_environment(policy)
            self.assertNotIn('CLAUDE_TEST', env)
        for settings in ({'API_' + 'TOK' + 'EN': 'fixture'}, {'MODE': 'sk-' + 'x' * 30}):
            with self.assertRaisesRegex(config.ConfigError, 'secrets forbidden'):
                self.policy({'env': {'set': settings}})
        env, _ = td.worker_environment(self.policy({'env': {'set': {'PATH': '/fixture/bin'}}}), 'sample')
        self.assertEqual(env['PATH'].split(os.pathsep)[0], str(self.base / 'shims'))

    def test_success_checks(self):
        for settings, output, reason in [({'fail_regex': 'BAD'}, 'BAD', 'fail_regex'),
                                          ({'require_regex': 'GOOD'}, 'other', 'require_regex_missing'),
                                          ({'require_changes': True}, 'done', 'no_changes'),
                                          ({'exit_codes': [2]}, 'done', 'exit_code')]:
            def runner(*args, **kw):
                return {'returncode': 0, 'timed_out': False, 'stdout': output, 'stderr': '', 'elapsed_seconds': 0}
            result = self.run_worker(self.policy({'success': settings}), runner)
            self.assertFalse(result['ok'])
            self.assertEqual(result['attempts'][0]['success_check'], reason)
        policy = self.policy({'success': {'require_changes': True, 'require_regex': 'GOOD'}})
        def edit(*args, **kw):
            (self.repo / 'README.md').write_text('changed')
            return {'returncode': 0, 'timed_out': False, 'stdout': 'GOOD', 'stderr': '', 'elapsed_seconds': 0}
        self.assertTrue(self.run_worker(policy, edit)['ok'])
        def no_edit(*args, **kw):
            return {'returncode': 0, 'timed_out': False, 'stdout': 'GOOD', 'stderr': '', 'elapsed_seconds': 0}
        result = self.run_worker(policy, no_edit, allow_dirty=True)
        self.assertEqual(result['attempts'][0]['success_check'], 'no_changes')

    def test_never_by_basename_and_alias(self):
        for executable in ('claude', '/fixture/bin/claude-code'):
            with self.assertRaisesRegex(config.ConfigError, 'never'):
                self.policy({'command': [executable]}, never=['claude'])
            policy = self.policy({'command': [executable]})
            policy['never'] = ['claude']
            with self.assertRaisesRegex(td.Refused, '^sample is in never; remove it from never to use it$'):
                self.run_worker(policy, lambda *a, **kw: self.fail('worker ran'))
            self.assertNotIn('sample', td.forceable(policy))

    def test_claude_pointer_and_command(self):
        policy = load_policy(worker_order=[], run_task={'prompt_delivery': {'claude': 'pointer'}})
        argv = td.build_argv(policy, 'claude', self.repo, Path('/fixture/task.md'))
        self.assertIn('--permission-mode', argv)
        self.assertTrue(argv[-1].startswith('Your full task is in the file'))

    def test_empty_order_and_force_matrix(self):
        policy = self.policy()
        policy['worker_order'] = []
        policy['ineligible'] = {'sample': 'fixture'}
        selected = selection.select(10000, policy=policy, io={})
        self.assertEqual((selected['worker'], selected['reason']), (None, 'no_workers_configured'))
        self.assertIn('init', selected['hint'])
        calls = []
        def runner(*args, **kw):
            calls.append(1)
            return {'returncode': 1, 'timed_out': False, 'stdout': '', 'stderr': '', 'elapsed_seconds': 0}
        result = self.run_worker(policy, runner)
        self.assertEqual((result['reason'], len(calls), len(result['attempts'])), ('forced', 1, 1))
        policy['unmetered_workers'] = []
        with self.assertRaisesRegex(td.Refused, 'meter unknown or stale: forced full-size work must be paced'):
            self.run_worker(policy, runner, meter=lambda w: {'meters': {w: None}})
        with self.assertRaisesRegex(td.Refused, 'meter at 85% >= 85%: refusing new full-size work'):
            self.run_worker(policy, runner, meter=lambda w: {'meters': {w: 85}})
        draft = self.run_worker(policy, runner, size='draft', meter=lambda w: {'meters': {w: None}})
        self.assertTrue(any('draft-size run not paced' in w for w in draft['warnings']))

    def test_meter_default_io_scope_and_forced(self):
        policy = load_policy(worker_order=[])
        self.assertEqual(config.metered_workers(policy), [])
        self.assertEqual(quota_service.default_io(policy), {})
        with patch('llm_router.observers.observe_isolated', return_value=common.unknown('fixture')) as observe:
            with patch('llm_router.observers.common.load_cache', return_value=None):
                quota_service.read_meter(policy, 'codex')
        observe.assert_called_once_with(policy, 'codex')

    def test_auto_unmetered_warning_and_checkout_root(self):
        (self.repo / 'sub').mkdir()
        seen = []
        def runner(argv, cwd, *args, **kw):
            seen.append((argv, cwd))
            return {'returncode': 0, 'timed_out': False, 'stdout': '', 'stderr': '', 'elapsed_seconds': 0}
        result = td.run_task(self.task, self.repo / 'sub', policy=self.policy({'command': ['fake-worker', '{repo}']}),
                             runner=runner, output_root=self.base / 'out')
        self.assertEqual(seen[0], (['fake-worker', str(self.repo)], self.repo))
        self.assertIn('sample is unmetered: run not paced', result['warnings'])

    def test_cli_never_message_and_run_job_preflight(self):
        policy = load_policy(worker_order=[], never=['claude'])
        message = 'claude is in never; remove it from never to use it'
        with self.assertRaisesRegex(td.Refused, '^' + message + '$'):
            job_dispatch.run_job(self.repo, self.task, worker='claude', policy=policy)
        for action in ('run-task', 'run-job'):
            output = io.StringIO()
            args = [action, '--worker', 'claude', '--task-file', str(self.task),
                    '--cwd' if action == 'run-task' else '--repo', str(self.repo)]
            with patch('llm_router.config.load_policy', return_value=policy), contextlib.redirect_stdout(output):
                code = cli.main(args)
            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.getvalue())['refused'], message)

    def test_forced_fallback_outside_order_reads_own_meter(self):
        policy = load_policy(worker_order=[], quota={'cursor': {'enabled': True}})
        row = {'percent_used': 10, 'observed_at': NOW.isoformat(),
               'expires_at': (NOW + timedelta(minutes=10)).isoformat()}
        observation = {'evidence': {'source_worker': 'cursor', 'required_windows': ['monthly'],
                                   'windows': {'monthly': row}}}
        with patch('llm_router.observers.common.load_cache', return_value=None), \
             patch('llm_router.observers.common.save_cache'), \
             patch('llm_router.observers.observe_isolated', return_value=observation) as read:
            result = quota_service.read_meter(policy, 'cursor', now=NOW)
        self.assertEqual(result['meters'], {'cursor': 10})
        read.assert_called_once_with(policy, 'cursor')
        policy['quota']['cursor'].pop('enabled')
        self.assertIn('cursor', quota_service.default_io(policy, only='cursor'))
        policy['ineligible'] = {'cursor': 'fixture'}
        calls = []
        def runner(*args, **kw):
            calls.append(1)
            return {'returncode': 0, 'timed_out': False, 'stdout': '', 'stderr': '', 'elapsed_seconds': 0}
        result = td.run_task(self.task, self.repo, worker='cursor', policy=policy,
                             meter=lambda worker: {'meters': {worker: 10}}, runner=runner,
                             output_root=self.base / 'out')
        self.assertEqual((result['worker'], result['reason'], len(calls)), ('cursor', 'forced', 1))
        import csv
        with config.policy_path(policy, 'run_task', 'ledger', 'logs/ledger.csv').open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[-1]['used_percent'], '10')

    def test_auto_ineligible_and_generic_requires_unmetered(self):
        policy = self.policy(ineligible={'sample': 'fixture'})
        self.assertIsNone(selection.select(10000, policy=policy, io={})['worker'])
        with self.assertRaisesRegex(config.ConfigError, 'unmetered_workers'):
            load_policy(worker_order=['sample'], workers={'sample': {'command': ['fake-worker']}})


class ObserverTests(unittest.TestCase):
    def policy(self):
        return load_policy(worker_order=['sample'], workers={'sample': {'command': ['fake-worker'], 'quota': {
            'command': ['fake-meter'], 'regex': r'used (?P<percent>\d+)%'}}})

    def test_command_cases(self):
        for code, text, reason in [(0, 'used 23%', None), (0, 'none', 'no_match'),
                                    (0, 'used 1% used 2%', 'multiple_matches'),
                                    (0, 'used 101%', 'out_of_range'), (None, '', 'timeout'),
                                    (1, '', 'command_failed')]:
            result = command.observe(self.policy(), now=NOW, worker='sample', runner=lambda *a: (code, text))
            self.assertEqual(result['reason'], reason)
            if reason is None:
                self.assertEqual(result['evidence']['windows']['monthly']['percent_used'], 23)
        policy = self.policy()
        policy['quota']['sample'].update(percent_is='remaining', regex=r'(?P<percent>\d+) (?P<reset>\S+)')
        result = command.observe(policy, now=NOW, worker='sample', runner=lambda *a: (0, '70 2026-10-19T00:00:00Z'))
        self.assertEqual(result['evidence']['windows']['monthly']['percent_used'], 30)
        for quota in ({'regex': '('}, {'regex': 'none'}, {'percent_is': 'guess'}, {'window': 'bad window'}, {'timeout_seconds': 0}):
            with self.assertRaises(config.ConfigError):
                load_policy(worker_order=[], workers={'sample': {'command': ['fake-worker'], 'quota': {
                    'command': ['fake-meter'], 'regex': r'(?P<percent>\d+)', **quota}}})

    def test_claude_fixture_and_fake_tui(self):
        ev = claude.parse_screen(SCREEN, NOW, 1800)
        self.assertEqual(ev['required_windows'], ['session', 'weekly'])
        for text in (b'other', SCREEN + SCREEN, SCREEN.replace(b'34%', b'134%')):
            self.assertIsNone(claude.parse_screen(text, NOW, 1800))
        policy = load_policy(worker_order=[])
        self.assertEqual(claude.observe(policy, now=NOW, capture=lambda t: b'other')['reason'], 'parse_failed')
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'fake-tui'
            script.write_text('#!' + sys.executable + '\nimport sys\nfor line in sys.stdin:\n if "/usage" in line:\n  print(' + repr(SCREEN.decode()) + ', flush=True)\n')
            script.chmod(0o700)
            policy['binaries']['claude'] = str(script)
            with patch.object(claude, 'WARMUP_SECONDS', 0.1), patch.object(claude, 'STEPS', [(b'/usage\r', 1.0)]):
                result = claude.observe(policy, now=NOW)
            self.assertEqual(result['status'], 'ok')


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / 'cfg'
        self.env = patch.dict(os.environ, {'LLM_ROUTER_CONFIG_DIR': str(self.target)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def init(self, **kw):
        return cli.init_config(which=lambda name: '/fixture/' + name, is_tty=False, **kw)

    def test_noninteractive_dry_run_and_backup(self):
        with self.assertRaisesRegex(config.ConfigError, '--workers is required'):
            self.init()
        report = self.init(workers='aider,opencode', dry_run=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(report['config']['worker_order'], ['aider', 'opencode'])
        self.assertIn('process list', ' '.join(report['warnings']))
        report = self.init(workers='codex,aider', non_interactive=True)
        self.assertEqual(len(report['written']), 3)
        self.assertEqual((self.target / 'config.json').stat().st_mode & 0o777, 0o600)
        (self.target / 'repos.json').write_text('preserved')
        old = (self.target / 'config.json').read_text()
        with self.assertRaises(config.ConfigError):
            self.init(workers='cursor')
        report = self.init(workers='cursor', force=True, meters='cursor')
        self.assertEqual(Path(report['backup']).read_text(), old)
        self.assertEqual((self.target / 'repos.json').read_text(), 'preserved')

    def test_interactive_claude_default_no_and_explicit_yes(self):
        answers = iter(['', '', '', ''])
        prompts = []
        def answer(prompt):
            prompts.append(prompt)
            return next(answers)
        report = cli.init_config(which=lambda name: '/fixture/' + name if name in ('codex', 'claude', 'cursor-agent') else None,
                                 is_tty=True, input_fn=answer, output_fn=lambda s: None, dry_run=True)
        self.assertNotIn('claude', report['config']['worker_order'])
        self.assertTrue(any(p == 'Claude Code detected. Add it? [y/N] ' for p in prompts))
        answers = iter(['claude', 'y', 'y'])
        report = cli.init_config(which=lambda name: '/fixture/claude' if name == 'claude' else None,
                                 is_tty=True, input_fn=lambda p: next(answers), output_fn=lambda s: None, dry_run=True)
        self.assertEqual(report['config']['worker_order'], ['claude'])
        self.assertTrue(report['config']['quota']['claude']['enabled'])
        report = self.init(workers='claude', dry_run=True)
        self.assertEqual(report['config']['unmetered_workers'], ['claude'])

    def test_detection_controlled_path_no_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / 'claude'
            fake.write_text('#!/bin/sh\nexit 99\n')
            fake.chmod(0o700)
            with patch.dict(os.environ, {'PATH': tmp}):
                report = cli.init_config(workers='', non_interactive=True, dry_run=True)
            self.assertEqual(report['detected'], ['claude'])
            self.assertEqual(report['config']['worker_order'], [])

    def test_doctor_generic_override_and_ci(self):
        policy = load_policy(worker_order=[], workers={'codex': {'command': [sys.executable], 'prompt_delivery': 'arg'}})
        with patch('llm_router.cli.paths_report', return_value={'binaries': {}}):
            report = cli.doctor(policy)
        self.assertFalse(any('binary' in p for p in report['problems']))
        self.assertTrue(any('process list' in w for w in report['warnings']))
        workflow = Path(__file__).resolve().parents[1] / '.github/workflows/ci.yml'
        self.assertIn('actions/checkout@v7', workflow.read_text())
        self.assertIn('actions/setup-python@v7', workflow.read_text())


class PromptExpansionTests(unittest.TestCase):
    def test_arg_prompt_is_not_re_expanded(self):
        from llm_router import task_dispatch as td
        policy = load_policy(worker_order=[], workers={'echoer': {'command': [sys.executable, '-c', 'pass', '{prompt}'],
                                                                  'prompt_delivery': 'arg'}})
        argv = td.build_argv(policy, 'echoer', Path('/tmp'), prompt='keep {cwd} and {repo} literal')
        self.assertEqual(argv[-1], 'keep {cwd} and {repo} literal')


class StackwichWorkerTests(unittest.TestCase):
    setUp = WorkerTests.setUp
    policy = WorkerTests.policy
    run_worker = WorkerTests.run_worker

    def test_unsupported_worker_runs_without_protocol(self):
        seen = []
        def runner(argv, cwd, *args, **kwargs):
            seen.append(kwargs['input_text'])
            return {'returncode': 0, 'timed_out': False, 'elapsed_seconds': 0,
                    'stdout': '', 'stderr': ''}
        result = self.run_worker(self.policy({'stackwich': {'enabled': True}}), runner)
        self.assertEqual(result['stackwich']['status'], 'unsupported')
        self.assertTrue(any('unsupported' in warning for warning in result['warnings']))
        self.assertNotIn('Stackwich verification required', seen[0])

    def test_supported_failure_does_not_fail_over(self):
        seen = []
        def runner(argv, cwd, *args, **kwargs):
            seen.append(kwargs['input_text'])
            return {'returncode': 1, 'timed_out': False, 'elapsed_seconds': 0,
                    'stdout': '', 'stderr': ''}
        policy = self.policy({'stackwich': {'enabled': True, 'supported': True}})
        result = td.run_task(self.task, self.repo, policy=policy, runner=runner,
                            select=lambda *a, **k: {'eligible': ['sample', 'codex']},
                            output_root=self.base / 'out', stackwich_cycle=2)
        self.assertEqual(len(result['attempts']), 1)
        self.assertIn('"cycles": 2', seen[0])
        self.assertEqual(result['stackwich']['status'], 'required')
