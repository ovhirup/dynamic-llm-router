"""Tests for Claude token meter via Claude Code mod hook (#102)."""
import importlib
from pathlib import Path
import sys
import unittest

from llm_router import claude_token_meter, handoff
from test_job_dispatch import (
    job_setup,
    job_teardown,
    job_select,
    job_worker,
    run_job_fixture,
)


class ClaudeTokenMeterTests(unittest.TestCase):
    setUp = job_setup
    tearDown = job_teardown
    select = job_select
    worker = job_worker
    job = run_job_fixture

    def test_falls_back_to_unmetered_when_file_missing(self):
        missing = self.base / 'nonexistent_meter.json'
        reading = claude_token_meter.read_claude_token_meter(self.policy, path=missing)
        self.assertEqual(reading['status'], 'unmetered')
        self.assertIsNone(reading['percent_used'])
        self.assertIsNone(handoff.meter_percent('claude', policy=self.policy))

    def test_persists_across_hot_reload_and_accumulates_turns(self):
        meter_file = self.base / 'claude_meter.json'
        self.policy.setdefault('quota', {})['claude_token_meter_file'] = str(meter_file)
        claude_token_meter.record_turn_usage(
            {'input_tokens': 10_000, 'output_tokens': 5_000, 'cache_read_input_tokens': 25_000},
            policy=self.policy, budget=100_000,
        )
        # Simulate module hot-reload: counters live on disk, never in a module global.
        importlib.reload(claude_token_meter)
        second = claude_token_meter.record_turn_usage(
            {'input_tokens': 20_000, 'output_tokens': 10_000, 'cache_creation_input_tokens': 15_000},
            policy=self.policy, budget=100_000,
        )
        self.assertEqual(second['total_tokens'], 85_000)
        self.assertEqual(second['percent_used'], 85.0)
        self.assertEqual(len(second['turns']), 2)
        self.assertEqual(meter_file.stat().st_mode & 0o777, 0o600)

    def test_meter_at_or_above_85_percent_triggers_router_handoff_note(self):
        meter_file = self.base / 'claude_meter.json'
        self.policy.setdefault('quota', {})['claude_token_meter_file'] = str(meter_file)
        self.policy['run_job']['handoffs_dir'] = str(self.base / 'handoffs')
        self.policy['unmetered_workers'].append('claude')
        self.policy['run_task']['commands']['claude'] = [
            sys.executable, '-c',
            "import sys; sys.stdin.read(); open('out.txt','w').write('ok\\n'); print('- [x] done')",
        ]
        claude_token_meter.record_turn_usage(
            {'input_tokens': 50_000, 'output_tokens': 20_000, 'cache_read_input_tokens': 18_000},
            policy=self.policy, budget=100_000,
        )
        result = self.job(
            test_cmd='test -f out.txt',
            select=lambda *a, **k: {'eligible': ['claude'], 'meters': {}, 'reason': 'test'},
        )
        self.assertTrue(result['ok'], result)
        self.assertTrue(any('claude meter at 88% (>= 85%): handoff note written' in w for w in result['warnings']),
                        result['warnings'])
        self.assertTrue(any(h['switch_reason'] == 'meter_threshold 88%' for h in result['handoffs']),
                        result['handoffs'])
        note_path = Path(result['handoffs'][0]['path'])
        self.assertTrue(note_path.is_file())


if __name__ == '__main__':
    unittest.main()
