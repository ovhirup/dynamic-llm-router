"""Tests for role-based routing (`planner`, `coder`, `searcher`, `judge`, `advisor`) and advisor escalation (#135)."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import cli, roles


class RoleBasedRoutingTests(unittest.TestCase):
    def test_role_to_worker_choice_follows_live_budget_with_fallback(self):
        policy = load_policy(
            worker_order=['claude', 'codex', 'grok-build', 'cursor'],
            unmetered_workers=['claude', 'codex', 'grok-build', 'cursor'],
        )
        eligible = ['claude', 'codex', 'grok-build', 'cursor', 'aider-ollama']
        meters = {'claude': 20, 'codex': 15, 'grok-build': 10, 'cursor': 10}

        self.assertEqual(roles.select_for_role('planner', policy, eligible, meters)['worker'], 'claude')
        self.assertEqual(roles.select_for_role('judge', policy, eligible, meters)['worker'], 'claude')
        self.assertEqual(roles.select_for_role('coder', policy, eligible, meters)['worker'], 'codex')
        self.assertEqual(roles.select_for_role('searcher', policy, eligible, meters)['worker'], 'aider-ollama')

        # When Claude is over threshold (85%), planner/judge/advisor fall back to grok-build
        exhausted_claude = dict(meters, claude=90)
        self.assertEqual(roles.select_for_role('planner', policy, eligible, exhausted_claude)['worker'], 'grok-build')
        self.assertEqual(roles.select_for_role('judge', policy, eligible, exhausted_claude)['worker'], 'grok-build')
        self.assertEqual(roles.select_for_role('advisor', policy, eligible, exhausted_claude)['worker'], 'grok-build')

        # Judge and advisor never route to local models even if only local is eligible
        only_local = ['aider-ollama', 'developer']
        self.assertEqual(roles.select_for_role('judge', policy, only_local, {})['worker'], 'grok-bot')

    def test_advisor_triggers_on_repeated_errors_loops_and_uncertain_done(self):
        self.assertFalse(roles.needs_advisor_escalation(['AssertionError: 1 != 2'])['escalate'])
        repeated = roles.needs_advisor_escalation(['AssertionError: 1 != 2', 'AssertionError: 1 != 2'])
        self.assertTrue(repeated['escalate'])
        self.assertIn('repeated_error', repeated['reason'])
        self.assertTrue(roles.needs_advisor_escalation(loop_detected=True)['escalate'])
        self.assertTrue(roles.needs_advisor_escalation(uncertain_done=True)['escalate'])

        prompt = roles.build_advisor_prompt('Fix parser', 'Tried regex change twice', ['AssertionError: 1 != 2'])
        self.assertIn('## Advisor Escalation', prompt)
        self.assertIn('AssertionError: 1 != 2', prompt)

        judge_prompt = roles.build_judge_prompt('+x = 1', scope=['src/a.py'], test_summary='10 passed')
        self.assertIn('Assume the implementation is wrong', judge_prompt)
        self.assertIn('VERDICT: approve', judge_prompt)

    def test_role_usage_logging_and_cli_select_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = load_policy()
            policy['run_job']['role_usage_file'] = str(Path(tmp) / 'role_usage.jsonl')
            entry = roles.log_role_usage(policy, 'planner', 'claude', tokens=1450, job_id='job-1')
            self.assertEqual(entry['role'], 'planner')
            self.assertEqual(entry['worker'], 'claude')
            self.assertEqual(entry['tokens'], 1450)
            saved = json.loads(Path(policy['run_job']['role_usage_file']).read_text().splitlines()[0])
            self.assertEqual(saved['tokens'], 1450)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['select', '--cached-only', '--role', 'planner'])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['role'], 'planner')
        self.assertIn('role_pick', payload)


if __name__ == '__main__':
    unittest.main()
