"""Pre-flight prompt limits use fixture runners, never a real worker."""
import unittest

from helpers import load_policy
from llm_router import claude_handoff, task_dispatch as td
import test_workers


class PromptSizeTests(unittest.TestCase):
    setUp = test_workers.WorkerTests.setUp

    def policy(self, **options):
        return load_policy(run_task={'ledger': str(self.base / 'ledger.csv'),
            'shim_dir': str(self.base / 'shims'), 'block_log': str(self.base / 'blocks'), **options})

    def run_task(self, policy, name='codex', **options):
        return td.run_task(self.task, self.repo, policy=policy,
            select=lambda *a, **kw: {'eligible': [name], 'meters': {name: 10}},
            output_root=self.base / 'out', **options)

    def unexpected_run(self, *args, **kwargs):
        self.fail('oversized prompt launched a worker')

    def test_default_cap_refuses_before_launch(self):
        policy = self.policy()
        self.assertEqual(policy['run_task']['max_prompt_chars'], 100000)
        self.task.write_text('x' * 100000)
        with self.assertRaisesRegex(td.Refused, 'assembled prompt.*max_prompt_chars=100000'):
            self.run_task(policy, runner=self.unexpected_run)
        self.assertFalse((self.repo / '.git/router-prompts').exists())

    def test_short_task_and_exact_boundary_allowed(self):
        policy = self.policy()
        expected = policy['run_task']['prompt_prefix'] + '\n\n' + self.task.read_text()
        policy['run_task']['max_prompt_chars'] = len(expected)
        seen = []
        def runner(argv, cwd, *args, **kwargs):
            seen.append(kwargs['input_text'])
            return {'returncode': 0, 'timed_out': False, 'elapsed_seconds': 0,
                    'stdout': 'done', 'stderr': ''}
        result = self.run_task(policy, runner=runner)
        self.assertTrue(result['ok'])
        self.assertEqual(seen, [expected])

    def test_extra_text_and_stackwich_tail_count(self):
        policy = self.policy()
        policy['run_task']['max_prompt_chars'] = len(
            policy['run_task']['prompt_prefix'] + '\n\n' + self.task.read_text())
        with self.assertRaises(td.Refused):
            self.run_task(policy, extra_prompt='extra text', runner=self.unexpected_run)
        policy['run_task']['stackwich']['codex'] = {'enabled': True, 'supported': True}
        with self.assertRaises(td.Refused):
            self.run_task(policy, runner=self.unexpected_run)

    def test_claude_brief_counts_before_prompt_file_written(self):
        policy = self.policy(max_prompt_chars=500)
        policy['run_task']['prompt_delivery']['claude'] = 'pointer'
        with self.assertRaisesRegex(td.Refused, 'max_prompt_chars=500'):
            self.run_task(policy, name='claude', runner=self.unexpected_run)
        self.assertFalse((self.repo / '.git/router-prompts').exists())

    def test_developer_draft_also_refused(self):
        with self.assertRaisesRegex(td.Refused, 'max_prompt_chars=1'):
            self.run_task(self.policy(max_prompt_chars=1), name='developer', size='draft',
                          draft=self.unexpected_run, runner=self.unexpected_run)

    def test_continuation_growth_cannot_bypass_cap(self):
        policy = self.policy()
        initial = claude_handoff.build_prompt(self.task.read_text(), 'medium',
            policy['run_task']['timeouts_seconds']['claude'], prefix=policy['run_task']['prompt_prefix'])
        policy['run_task']['max_prompt_chars'] = len(initial)
        calls = []
        def runner(argv, cwd, *args, **kwargs):
            calls.append(kwargs['input_text'])
            return {'returncode': 0, 'timed_out': False, 'elapsed_seconds': 1,
                    'stdout': '- [ ] ' + 'x' * 1000, 'stderr': ''}
        with self.assertRaisesRegex(td.Refused, 'max_prompt_chars'):
            self.run_task(policy, name='claude', runner=runner)
        self.assertEqual(len(calls), 1)
