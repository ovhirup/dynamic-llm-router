"""Tests for worker watchdog (#131): stalled, looping, drifting, infra failure, and reassignment."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import alerts, cli, watchdog

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.policy = load_policy()
        self.policy['run_job']['heartbeats_dir'] = str(self.base / 'hb')
        self.policy['alerts'] = {
            'file': str(self.base / 'alerts.jsonl'),
            'summary': str(self.base / 'ALERTS.md'),
            'macos_notification': False,
        }

    def test_detects_stalled_looping_drifting_and_infra_failure(self):
        wt = self.base / 'wt'
        wt.mkdir()
        # 1. Stalled job
        watchdog.write_heartbeat(
            self.policy, 'job-stalled', 'codex', wt, pid=99991,
            now=NOW - timedelta(seconds=400),
        )
        hb_stalled = watchdog.read_heartbeat(self.policy, 'job-stalled')
        diag_stalled = watchdog.inspect_job(self.policy, hb_stalled, now=NOW, stall_seconds=300)
        self.assertEqual(diag_stalled['status'], 'stalled')

        # 2. Looping job
        loop_log = self.base / 'loop.log'
        loop_log.write_text('AssertionError: expected 1 got 2\n' * 3)
        watchdog.write_heartbeat(
            self.policy, 'job-loop', 'claude', wt, log_path=loop_log, now=NOW,
        )
        diag_loop = watchdog.inspect_job(
            self.policy, watchdog.read_heartbeat(self.policy, 'job-loop'), now=NOW,
        )
        self.assertEqual(diag_loop['status'], 'looping')
        self.assertIn('AssertionError', diag_loop['detail'])

        # 3. Drifting job
        watchdog.write_heartbeat(
            self.policy, 'job-drift', 'cursor', wt,
            allowed_paths=['src/allowed.py'], now=NOW,
        )
        diag_drift = watchdog.inspect_job(
            self.policy,
            watchdog.read_heartbeat(self.policy, 'job-drift'),
            now=NOW,
            changed_paths_fn=lambda _: {'src/allowed.py', 'secrets/extra.py'},
        )
        self.assertEqual(diag_drift['status'], 'drifting')
        self.assertIn('secrets/extra.py', diag_drift['detail'])

        # 4. Infra failure (quota exhausted mid-job)
        infra_log = self.base / 'infra.log'
        infra_log.write_text('HTTP 429 Too Many Requests: rate limit reached\n')
        watchdog.write_heartbeat(
            self.policy, 'job-infra', 'grok-build', wt, log_path=infra_log, now=NOW,
        )
        diag_infra = watchdog.inspect_job(
            self.policy, watchdog.read_heartbeat(self.policy, 'job-infra'), now=NOW,
        )
        self.assertEqual(diag_infra['status'], 'infra_failure')
        self.assertEqual(diag_infra['detail'], 'quota_exhausted')

    def test_recover_kills_and_reassigns_to_next_eligible_worker_preserving_worktree(self):
        wt = self.base / 'wt'
        wt.mkdir()
        (wt / 'wip.txt').write_text('partial work\n')
        watchdog.write_heartbeat(
            self.policy, 'job-1', 'codex', wt, pid=54321,
            now=NOW - timedelta(seconds=500),
        )
        killed = []
        reassigned = []
        report = watchdog.check_all(
            policy=self.policy,
            now=NOW,
            stall_seconds=300,
            recover=True,
            select_fn=lambda: {'eligible': ['codex', 'claude', 'grok-bot']},
            kill_fn=lambda pid: killed.append(pid),
            re_runner=lambda jid, w, path: reassigned.append((jid, w, path)) or {'ok': True},
        )
        self.assertEqual(killed, [54321])
        self.assertEqual(len(report['actions']), 1)
        action = report['actions'][0]
        self.assertEqual(action['action'], 'reassigned')
        self.assertEqual(action['next_worker'], 'claude')
        self.assertTrue(action['worktree_preserved'])
        self.assertTrue((wt / 'wip.txt').is_file())
        self.assertEqual(reassigned, [('job-1', 'claude', str(wt))])
        # No alert raised when automatic reassignment succeeded
        self.assertEqual(alerts.open_alerts(self.policy), [])

    def test_notifies_only_when_no_eligible_worker_left_and_cli_runs(self):
        wt = self.base / 'wt2'
        wt.mkdir()
        watchdog.write_heartbeat(
            self.policy, 'job-2', 'codex', wt, pid=54322,
            now=NOW - timedelta(seconds=500),
        )
        report = watchdog.check_all(
            policy=self.policy,
            now=NOW,
            stall_seconds=300,
            recover=True,
            select_fn=lambda: {'eligible': ['grok-bot']},
            kill_fn=lambda pid: None,
        )
        self.assertEqual(report['actions'][0]['action'], 'human_attention_required')
        self.assertIsNone(report['actions'][0]['next_worker'])
        self.assertEqual(len(alerts.open_alerts(self.policy)), 1)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['watchdog', '--stall-seconds', '999999'])
        self.assertEqual(code, 0)
        self.assertIn('checked', json.loads(out.getvalue()))


if __name__ == '__main__':
    unittest.main()
