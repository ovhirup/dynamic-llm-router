"""Tests for Claude guard mod (#101): secrets, pushes, destructive git, and --plugin-dir wiring."""
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import claude_guard, task_dispatch as td


class ClaudeGuardTests(unittest.TestCase):
    def test_blocks_secrets_reads_and_allows_env_example(self):
        for forbidden in ('.env', '.env.local', '~/.codex/auth.json', '~/.config/llm-router/config.json',
                          'keys/AuthKey_123.p8', '/tmp/auth.json'):
            with self.subTest(path=forbidden):
                verdict = claude_guard.evaluate_tool_call('Read', {'file_path': forbidden})
                self.assertFalse(verdict['allow'], forbidden)
                self.assertIsNotNone(verdict['reason'])

        for allowed in ('.env.example', 'src/llm_router/cli.py', 'tests/test_cli.py'):
            with self.subTest(path=allowed):
                verdict = claude_guard.evaluate_tool_call('Read', {'file_path': allowed})
                self.assertTrue(verdict['allow'], allowed)

    def test_blocks_pushes_destructive_git_and_outside_rm_rf(self):
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / 'worktree'
            wt.mkdir()
            for cmd in (
                'git push origin head',
                'git push --force',
                'git reset --hard HEAD~1',
                'git merge main',
                'gh pr merge 42 --squash',
                'gh pr review 42 --approve',
                'rm -rf /tmp/other',
                f'rm -rf {wt}',
                'cat ~/.codex/config.toml',
            ):
                with self.subTest(cmd=cmd):
                    verdict = claude_guard.evaluate_tool_call('Bash', {'command': cmd}, worktree=wt)
                    self.assertFalse(verdict['allow'], cmd)

            safe_rm = claude_guard.evaluate_tool_call('Bash', {'command': 'rm -rf build/cache'}, worktree=wt)
            self.assertTrue(safe_rm['allow'], safe_rm)
            safe_test = claude_guard.evaluate_tool_call('Bash', {'command': 'python -m pytest -q'}, worktree=wt)
            self.assertTrue(safe_test['allow'], safe_test)

    def test_version_check_and_plugin_dir_cli_wiring(self):
        self.assertTrue(claude_guard.version_supported('2.1.287 (Claude Code)'))
        self.assertTrue(claude_guard.version_supported('2.1.309'))
        self.assertFalse(claude_guard.version_supported('2.1.286'))
        self.assertFalse(claude_guard.version_supported('unknown'))

        policy = load_policy()
        policy['run_task']['claude_guard_mod'] = True
        warnings = []
        argv_ok = td.build_argv(policy, 'claude', '/tmp', claude_version='2.1.309', warnings=warnings)
        self.assertIn('--plugin-dir', argv_ok)
        self.assertEqual(warnings, [])

        warnings_old = []
        argv_old = td.build_argv(policy, 'claude', '/tmp', claude_version='2.1.200', warnings=warnings_old)
        self.assertNotIn('--plugin-dir', argv_old)
        self.assertEqual(len(warnings_old), 1)
        self.assertIn('< 2.1.287', warnings_old[0])

    def test_blocked_attempts_written_to_job_log_and_mod_files_exist(self):
        self.assertTrue((claude_guard.plugin_dir() / 'plugin.json').is_file())
        self.assertTrue((claude_guard.plugin_dir() / 'guard.mjs').is_file())
        self.assertIn('.catch(', (claude_guard.plugin_dir() / 'guard.mjs').read_text())
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / 'guard.log'
            claude_guard.record_blocked_attempt(log_file, 'blocked access to ~/.codex', tool_name='Read')
            text = log_file.read_text()
            self.assertIn('claude-guard blocked', text)
            self.assertIn('~/.codex', text)


if __name__ == '__main__':
    unittest.main()
