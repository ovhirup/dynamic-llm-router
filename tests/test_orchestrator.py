"""Tests for dynamic orchestrator detection, job recording, budget-aware handoff, and CLI status (#120)."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import cli, orchestrator
from test_job_dispatch import (
    job_setup,
    job_teardown,
    job_select,
    job_worker,
    run_job_fixture,
)


class DynamicOrchestratorTests(unittest.TestCase):
    setUp = job_setup
    tearDown = job_teardown
    select = job_select
    worker = job_worker
    job = run_job_fixture

    def test_detects_orchestrator_from_flag_and_environment(self):
        self.assertEqual(
            orchestrator.detect_orchestrator(explicit='cursor'),
            {'orchestrator': 'cursor', 'reason': 'explicit_flag'},
        )
        for env, expected in [
            ({'LLM_ROUTER_ORCHESTRATOR': 'claude'}, 'claude'),
            ({'CURSOR_TRACE_ID': 'trace-1'}, 'cursor'),
            ({'CLAUDECODE': '1'}, 'claude'),
            ({'CODEX_SANDBOX': 'seatbelt'}, 'codex'),
            ({'GROK_BUILD_SESSION': 'g1'}, 'grok-build'),
            ({}, 'grok-bot'),
        ]:
            with self.subTest(env=env):
                self.assertEqual(orchestrator.detect_orchestrator(env=env)['orchestrator'], expected)

    def test_job_records_calling_orchestrator_in_job_json(self):
        self.worker("open('out.txt','w').write('ok\\n')")
        for tool in ('cursor', 'claude', 'codex', 'grok-build', 'grok-bot'):
            with self.subTest(tool=tool):
                result = self.job(test_cmd='test -f out.txt', orchestrator=tool)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['orchestrator'], tool)
                self.assertEqual(result['orchestrator_reason'], 'explicit_flag')
                saved = json.loads((self.base / 'jobs' / f"{result['job_id']}.json").read_text())
                self.assertEqual(saved['orchestrator'], tool)

    def test_simulated_low_quota_produces_handoff_note_and_next_eligible_orchestrator(self):
        policy = load_policy(
            worker_order=['cursor', 'claude', 'codex', 'grok-build'],
            unmetered_workers=['cursor', 'claude', 'codex', 'grok-build'],
        )
        with tempfile.TemporaryDirectory() as tmp:
            report = orchestrator.check_budget_handoff(
                policy,
                orchestrator_name='cursor',
                meters={'cursor': 90, 'claude': 20},
                eligible=['claude', 'codex'],
                goal='Finish overnight wave',
                handoffs_dir=tmp,
            )
            self.assertTrue(report['handoff_needed'])
            self.assertEqual(report['orchestrator'], 'cursor')
            self.assertEqual(report['next_orchestrator'], 'claude')
            self.assertIsNotNone(report['handoff_note'])
            note_text = Path(report['handoff_note']).read_text()
            self.assertIn('Hand off orchestration to `claude`', note_text)

    def test_cli_orchestrator_status_prints_current_reason_and_fallback_chain(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['orchestrator', 'status', '--orchestrator', 'cursor'])
        self.assertEqual(code, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data['orchestrator'], 'cursor')
        self.assertEqual(data['reason'], 'explicit_flag')
        self.assertIn('grok-bot', data['fallback_chain'])
        self.assertNotIn('cursor', data['fallback_chain'])


if __name__ == '__main__':
    unittest.main()
