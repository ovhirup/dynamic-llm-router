"""Prompt files must not be written through a symlink or a hard link."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import load_policy
from llm_router import job_dispatch as jd
from llm_router import task_dispatch as td
from llm_router import unit_review as ur
from test_task_dispatch import repo

KEEP = b'KEEP'
STAMP = '20260101-000000'
WORKER = 'grok-build'


class PromptNoFollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = repo(self.base / 'repo')
        self.task = self.base / 'task.md'
        self.task.write_text('fixture task\n')
        self.policy = load_policy(run_task={
            'ledger': str(self.base / 'ledger.csv'),
            'shim_dir': str(self.base / 'shims'),
            'block_log': str(self.base / 'block.log'),
        })

    def git_dir(self):
        done = subprocess.run(
            ['git', '-C', str(self.repo), 'rev-parse', '--absolute-git-dir'],
            check=True, capture_output=True, text=True)
        return Path(done.stdout.strip())

    def prompt_names(self):
        """Fresh name the fixed writer uses, and the old fixed `{stamp}-{worker}.md` name."""
        return f'{STAMP}-{WORKER}-deadbeef.md', f'{STAMP}-{WORKER}.md'

    def pin_name(self):
        pinned, _old = self.prompt_names()
        return mock.patch.object(td, '_prompt_name', return_value=pinned, create=True)

    def run_job(self):
        calls = []

        def runner(*args, **kwargs):
            calls.append(1)
            return {'returncode': 0, 'timed_out': False, 'stdout': 'done', 'stderr': '',
                    'elapsed_seconds': 0}

        with self.pin_name():
            summary = td.run_task(
                self.task, self.repo, worker=WORKER, policy=self.policy, runner=runner,
                output_root=self.base / 'out', stamp=STAMP,
                meter=lambda name: {'meters': {name: 10}})
        return summary, calls

    def assert_refused_job(self, summary, calls, needle):
        self.assertFalse(summary['ok'])
        self.assertEqual(calls, [])
        self.assertIn(needle, summary.get('failure_reason') or '')
        self.assertIn(needle, summary.get('error') or '')
        self.assertIn(needle, summary['attempts'][0].get('error') or '')

    def plant_prompt_files(self, kind):
        prompts = self.git_dir() / 'router-prompts'
        prompts.mkdir(mode=0o700)
        victims = {}
        for name in self.prompt_names():
            victim = self.base / f'victim-{kind}-{name}'
            victim.write_bytes(KEEP)
            link = prompts / name
            if kind == 'symlink':
                link.symlink_to(victim)
            else:
                os.link(victim, link)
            victims[name] = victim
        return victims

    def plant_prompts_dir_symlink(self):
        outside = self.base / 'outside'
        outside.mkdir()
        for name in self.prompt_names():
            (outside / name).write_bytes(KEEP)
        (outside / 'sentinel.txt').write_bytes(b'SENTINEL')
        before = sorted(path.name for path in outside.iterdir())
        mode = outside.stat().st_mode
        (self.git_dir() / 'router-prompts').symlink_to(outside)
        return outside, before, mode

    def assert_outside_unchanged(self, outside, before, mode):
        self.assertEqual(sorted(path.name for path in outside.iterdir()), before)
        self.assertEqual(outside.stat().st_mode, mode)
        for name in self.prompt_names():
            self.assertEqual((outside / name).read_bytes(), KEEP)
        self.assertEqual((outside / 'sentinel.txt').read_bytes(), b'SENTINEL')

    def test_run_task_refuses_prompts_directory_symlink(self):
        outside, before, mode = self.plant_prompts_dir_symlink()
        summary, calls = self.run_job()
        self.assert_outside_unchanged(outside, before, mode)
        self.assert_refused_job(summary, calls, 'symlink')

    def test_run_task_refuses_prompt_file_symlink(self):
        victims = self.plant_prompt_files('symlink')
        summary, calls = self.run_job()
        for victim in victims.values():
            self.assertEqual(victim.read_bytes(), KEEP)
        self.assert_refused_job(summary, calls, 'symlink')

    def test_run_task_refuses_prompt_file_hard_link(self):
        victims = self.plant_prompt_files('hardlink')
        summary, calls = self.run_job()
        for victim in victims.values():
            self.assertEqual(victim.read_bytes(), KEEP)
        self.assert_refused_job(summary, calls, 'hard link')

    def test_write_prompt_file_refuses_prompts_directory_symlink(self):
        outside, before, mode = self.plant_prompts_dir_symlink()
        refused = self.capture_write()
        self.assert_outside_unchanged(outside, before, mode)
        self.assert_refusal(refused, 'symlink')

    def test_write_prompt_file_refuses_prompt_file_symlink(self):
        victims = self.plant_prompt_files('symlink')
        refused = self.capture_write()
        for victim in victims.values():
            self.assertEqual(victim.read_bytes(), KEEP)
        self.assert_refusal(refused, 'symlink')

    def test_write_prompt_file_refuses_prompt_file_hard_link(self):
        victims = self.plant_prompt_files('hardlink')
        refused = self.capture_write()
        for victim in victims.values():
            self.assertEqual(victim.read_bytes(), KEEP)
        self.assert_refusal(refused, 'hard link')

    def capture_write(self):
        refused = []
        with self.pin_name():
            try:
                td.write_prompt_file(self.repo, STAMP, WORKER, 'secret prompt')
            except td.Refused as error:
                refused.append(error)
        return refused

    def assert_refusal(self, refused, needle):
        self.assertEqual(len(refused), 1)
        self.assertIn(needle, str(refused[0]))

    def plant_review(self, kind):
        victim = self.base / f'review-victim-{kind}'
        victim.write_bytes(KEEP)
        path = self.git_dir() / ur.PROMPT_NAME
        if kind == 'symlink':
            path.symlink_to(victim)
        else:
            os.link(victim, path)
        return victim

    def capture_review_write(self):
        refused = []
        try:
            ur._write_prompt(self.repo, 'review prompt')
        except td.Refused as error:
            refused.append(error)
        return refused

    def test_review_write_refuses_symlink(self):
        victim = self.plant_review('symlink')
        refused = self.capture_review_write()
        self.assertEqual(victim.read_bytes(), KEEP)
        self.assert_refusal(refused, 'symlink')

    def test_review_write_refuses_hard_link(self):
        victim = self.plant_review('hardlink')
        refused = self.capture_review_write()
        self.assertEqual(victim.read_bytes(), KEEP)
        self.assert_refusal(refused, 'hard link')

    def stronger_review(self):
        sha = subprocess.run(
            ['git', '-C', str(self.repo), 'rev-parse', 'HEAD'],
            check=True, capture_output=True, text=True).stdout.strip()
        summary = {
            'job_id': 'job1', 'base_sha': sha, 'commit': sha, 'branch': 'job/job1',
            'tests': [{'command': 'true', 'status': 'passed', 'returncode': 0, 'tail': 'ok'}],
            'tests_status': 'passed', 'attempts': [{'worker': 'codex'}],
            'workers': ['codex'], 'allowed_paths': ['README.md'],
        }

        def reviewer(*args, **kwargs):
            return {'status': 'handoff_to_orchestrator'}

        return jd._stronger_review(summary, self.repo, ['README.md'], reviewer, self.policy)

    def test_stronger_review_refuses_symlink(self):
        victim = self.plant_review('symlink')
        review = self.stronger_review()
        self.assertEqual(victim.read_bytes(), KEEP)
        self.assertEqual(review['status'], 'error')
        self.assertIn('symlink', review.get('notes') or '')

    def test_stronger_review_refuses_hard_link(self):
        victim = self.plant_review('hardlink')
        review = self.stronger_review()
        self.assertEqual(victim.read_bytes(), KEEP)
        self.assertEqual(review['status'], 'error')
        self.assertIn('hard link', review.get('notes') or '')

    def test_default_reviewer_refuses_symlink(self):
        victim = self.plant_review('symlink')
        result = ur.default_reviewer(
            'grok-bot', {'worktree': str(self.repo)}, 'review prompt', self.policy)
        self.assertEqual(victim.read_bytes(), KEEP)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('symlink', result.get('reason') or '')

    def test_default_reviewer_refuses_hard_link(self):
        victim = self.plant_review('hardlink')
        result = ur.default_reviewer(
            'grok-bot', {'worktree': str(self.repo)}, 'review prompt', self.policy)
        self.assertEqual(victim.read_bytes(), KEEP)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('hard link', result.get('reason') or '')
