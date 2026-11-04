"""Tests for permissioned tool gateway, permission ladder, worker permission levels, and failure classes (#146)."""
import unittest

from helpers import load_policy
from llm_router import tool_gateway
from test_job_dispatch import (
    job_setup,
    job_teardown,
    job_select,
    job_worker,
    run_job_fixture,
)


class PermissionedToolGatewayTests(unittest.TestCase):
    setUp = job_setup
    tearDown = job_teardown
    select = job_select
    worker = job_worker
    job = run_job_fixture

    def test_ladder_allows_read_write_commit_and_refuses_push_merge_and_secrets_without_approval(self):
        policy = load_policy()
        for auto_action, target in [
            ('read', 'src/llm_router/cli.py'),
            ('worktree_write', 'src/llm_router/cli.py'),
            ('git_commit', 'router/branch'),
        ]:
            with self.subTest(action=auto_action):
                res = tool_gateway.authorize_action(auto_action, policy=policy, target=target)
                self.assertTrue(res['authorized'])
                self.assertEqual(res['permission_level'], 'auto')

        for gated_action, target in [
            ('push', 'origin/router-branch'),
            ('merge', 'main'),
            ('pr', 'main'),
            ('read', '~/.codex/auth.json'),
            ('edit', 'config.json'),
            ('edit', 'repos.json'),
            ('delete', 'branch'),
            ('git_commit', 'git push --force'),
        ]:
            with self.subTest(action=gated_action, target=target):
                with self.assertRaises(tool_gateway.GatewayRefused):
                    tool_gateway.authorize_action(gated_action, policy=policy, target=target, approved=False)
                approved_res = tool_gateway.authorize_action(gated_action, policy=policy, target=target, approved=True)
                self.assertTrue(approved_res['authorized'])
                self.assertTrue(approved_res['approved_by_human'])

    def test_worker_permission_levels_and_failure_classification_in_run_job(self):
        policy = load_policy()
        for w in ('claude', 'codex', 'grok-build', 'cursor'):
            prof = tool_gateway.resolve_worker_permission_level(policy, w)
            self.assertEqual(prof['level'], 'worktree_bounded')
            self.assertNotEqual(prof['level'], 'full_access')

        # 1. Failing attempt is tagged with failure_class and recovery; second attempt passes
        capture = self.base / 'p.txt'
        self.worker(
            "import sys; open(%r,'a').write(sys.stdin.read()); open('n.txt','a').write('x\\n')"
            % str(capture)
        )
        result = self.job(test_cmd='echo "FileNotFoundError: missing fixture"; test $(wc -l < n.txt) -ge 2')
        self.assertTrue(result['ok'], result)
        self.assertIn('codex', result['worker_permissions'])
        self.assertEqual(result['worker_permissions']['codex']['level'], 'worktree_bounded')
        self.assertEqual(result['attempts'][0]['permission_level'], 'worktree_bounded')
        self.assertEqual(result['attempts'][0]['failure_class'], 'missing_context')
        self.assertIsNotNone(result['attempts'][0]['recovery'])
        self.assertIsNone(result['attempts'][1]['failure_class'])

    def test_structured_tool_result_bounds_output_and_tags_failure_class(self):
        obs = tool_gateway.structured_tool_result(
            'pytest -q',
            {'returncode': 1, 'timed_out': True, 'stalled': False, 'elapsed_seconds': 60.0,
             'stdout': 'x' * 5000, 'stderr': 'timed out'},
            max_chars=200,
        )
        self.assertFalse(obs['ok'])
        self.assertEqual(len(obs['stdout_tail']), 200)
        self.assertEqual(obs['failure_class'], 'flaky_env')


if __name__ == '__main__':
    unittest.main()
