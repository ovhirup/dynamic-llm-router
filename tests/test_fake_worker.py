"""The fake worker harness itself: subprocess CLI, env-var config, in-process runner, policy install."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from helpers import FakeWorkerHarness, fake_worker_argv, load_policy
import fake_worker


class FakeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)

    def subprocess_run(self, argv, env=None, cwd=None):
        full = {k: v for k, v in os.environ.items() if not k.startswith('FAKE_WORKER_')}
        full.update(env or {})
        return subprocess.run(argv, cwd=cwd or self.cwd, env=full, input='the prompt', capture_output=True,
                              text=True, timeout=20)

    def test_subprocess_modes_via_flags(self):
        expected = {'ok': 0, 'fail': 3, 'quota': 1, 'auth': 1, 'no_change': 0}
        for mode, code in expected.items():
            with self.subTest(mode=mode):
                (self.cwd / 'out.txt').unlink(missing_ok=True)
                done = self.subprocess_run(fake_worker_argv(mode) + ['-'])  # router tail is ignored
                self.assertEqual(done.returncode, code, done.stderr)
                self.assertEqual((self.cwd / 'out.txt').exists(), mode == 'ok')
        self.assertIn('Usage limit', self.subprocess_run(fake_worker_argv('quota')).stderr)
        self.assertIn('Failed to authenticate', self.subprocess_run(fake_worker_argv('auth')).stderr)

    def test_subprocess_configured_by_env(self):
        done = self.subprocess_run([sys.executable, fake_worker.PATH],
                                   env={'FAKE_WORKER_MODE': 'ok', 'FAKE_WORKER_FILE': 'x.txt', 'FAKE_WORKER_TEXT': 'yo',
                                        'FAKE_WORKER_EXIT': '7', 'FAKE_WORKER_STDERR': 'custom err'})
        self.assertEqual(done.returncode, 7)
        self.assertEqual((self.cwd / 'x.txt').read_text(), 'yo')
        self.assertIn('custom err', done.stderr)

    def test_flags_override_env(self):
        done = self.subprocess_run(fake_worker_argv('fail', exit=9), env={'FAKE_WORKER_MODE': 'ok'})
        self.assertEqual(done.returncode, 9)
        self.assertFalse((self.cwd / 'out.txt').exists())

    def test_python_dash_m_from_tests_dir(self):
        tests_dir = Path(fake_worker.PATH).parent
        done = self.subprocess_run([sys.executable, '-m', 'fake_worker'], env={'FAKE_WORKER_MODE': 'quota'},
                                   cwd=tests_dir)
        self.assertEqual(done.returncode, 1)
        self.assertIn('Usage limit', done.stderr)

    def test_timeout_mode_sleeps_until_killed(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            subprocess.run(fake_worker_argv('timeout', sleep=30), cwd=self.cwd, input='', capture_output=True,
                           text=True, timeout=1)

    def test_in_process_runner(self):
        slept = []
        run = FakeWorkerHarness('timeout', sleep=5).run(cwd=self.cwd, sleep=slept.append)
        self.assertEqual((run['returncode'], slept), (0, [5.0]))
        run = FakeWorkerHarness('ok', file='a.txt', text='A').run(cwd=self.cwd)
        self.assertEqual(run['returncode'], 0)
        self.assertEqual((self.cwd / 'a.txt').read_text(), 'A')
        run = FakeWorkerHarness().run(cwd=self.cwd, env={'FAKE_WORKER_MODE': 'quota'})
        self.assertEqual(run['returncode'], 0)  # explicit harness mode 'ok' beats env

    def test_install_keeps_prompt_tail_and_rejects_unknown_mode(self):
        policy = load_policy()
        tail = [a for a in policy['run_task']['commands']['codex'] if a in ('-', '{prompt_file}', '{prompt_pointer}')]
        FakeWorkerHarness('quota').install(policy, 'codex')
        self.assertEqual(policy['run_task']['commands']['codex'],
                         [sys.executable, fake_worker.PATH, '--mode', 'quota'] + tail)
        with self.assertRaises(ValueError):
            FakeWorkerHarness('bogus')


if __name__ == '__main__':
    unittest.main()
