"""Regression coverage for run-job's role propagation (#208)."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import FakeWorkerHarness, load_policy
from llm_router import cli, config, job_dispatch
from test_task_dispatch import repo


class JobRoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = repo(self.base / 'repo')
        self.task = self.base / 'task.md'
        self.task.write_text('Create out.txt.\n')
        self.policy = load_policy()
        self.policy['role_order'] = {'judge': ['grok-build', 'codex']}
        self.policy['run_task'].update(
            ledger=str(self.base / 'ledger.csv'), output_dir=str(self.base / 'out'),
            private_terms_file=str(self.base / 'terms.txt'), block_log=str(self.base / 'block.log'),
            shim_dir=str(self.base / 'shims'))
        self.policy['run_job'].update(
            worktree_root=str(self.base / 'worktrees'), lock_dir=str(self.base / 'locks'),
            jobs_dir=str(self.base / 'jobs'), repo_config=str(self.base / 'fake-repos.json'),
            caffeinate=False)
        self.policy['alerts'] = {'file': str(self.base / 'alerts.jsonl'),
                                 'summary': str(self.base / 'alerts.md'), 'macos_notification': False}
        (self.base / 'terms.txt').write_text('')
        for worker in ('codex', 'grok-build', 'cursor'):
            FakeWorkerHarness('ok').install(self.policy, worker)

    def select(self, chars, **kwargs):
        return {'eligible': ['codex', 'grok-build'],
                'meters': {'codex': 10, 'grok-build': 10}, 'reason': 'test'}

    def job(self, **kwargs):
        return job_dispatch.run_job(self.repo, self.task, policy=self.policy,
                                    select=self.select, test_cmd='test -f out.txt', **kwargs)

    def test_cli_forwards_run_job_role(self):
        out = io.StringIO()
        with mock.patch('llm_router.job_dispatch.run_job', return_value={'ok': True}) as run_job:
            with contextlib.redirect_stdout(out):
                code = cli.main(['run-job', '--repo', str(self.repo), '--task-file', str(self.task),
                                 '--role', 'judge'])
        self.assertEqual(code, 0)
        self.assertEqual(run_job.call_args.kwargs['role'], 'judge')
        self.assertTrue(json.loads(out.getvalue())['ok'])

    def test_run_job_role_changes_actual_worker_and_is_recorded(self):
        result = self.job(role='judge')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['workers'], ['grok-build'])
        self.assertEqual(result['role'], 'judge')

    def test_invalid_role_refused_before_worktree_creation(self):
        with self.assertRaisesRegex(config.ConfigError, 'unknown role'):
            self.job(role='invalid')
        self.assertFalse((self.base / 'worktrees').exists())

    def test_explicit_worker_is_not_overridden_by_role(self):
        result = self.job(role='judge', worker='codex')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['workers'], ['codex'])
        self.assertEqual(result['role'], 'judge')

    def test_role_does_not_add_an_unselected_orchestrator(self):
        self.policy['role_order']['judge'] = ['claude']
        result = self.job(role='judge')
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(result['workers'], [])
        self.assertTrue(any('no eligible worker' in warning for warning in result['warnings']))
