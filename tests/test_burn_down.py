"""Tests for token burn-down across models (#128): snapshots, burn rate, CLI table + --json, and soft failure."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import burn_down, cli

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def fake_io(worker, used, now=NOW):
    ev = {
        'source_worker': worker,
        'required_windows': ['weekly'],
        'windows': {
            'weekly': {
                'percent_used': used,
                'observed_at': (now - timedelta(minutes=2)).isoformat(),
                'expires_at': (now + timedelta(hours=1)).isoformat(),
                'reset_at': (now + timedelta(days=5)).isoformat(),
            }
        },
    }
    return {'load': lambda: {'evidence': ev}, 'observe': lambda p: {'evidence': ev}, 'save': lambda o: None}


class BurnDownTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.policy = load_policy(
            worker_order=['claude', 'codex', 'cursor'],
            quota={
                'claude': {'enabled': True},
                'codex': {'enabled': True},
                'cursor': {'enabled': True},
                'usage_snapshots_file': str(self.base / 'snapshots.jsonl'),
            },
        )

    def test_snapshot_store_and_burn_rate_projection(self):
        burn_down.record_snapshot(
            self.policy,
            [{'worker': 'claude', 'percent_used': 10.0, 'collected_at': (NOW - timedelta(hours=5)).isoformat()}],
            now=NOW - timedelta(hours=5),
        )
        burn_down.record_snapshot(
            self.policy,
            [{'worker': 'claude', 'percent_used': 20.0, 'collected_at': NOW.isoformat()}],
            now=NOW,
        )
        history = burn_down.load_snapshots(self.policy, worker='claude')
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]['remaining_percent'], 80.0)
        rate = burn_down.compute_burn_rate(history, 'claude', now=NOW)
        # 10% burned in 5 hours = 2.0%/h; 80% remaining / (2.0 * 24) = 1.67 days
        self.assertEqual(rate['percent_per_hour'], 2.0)
        self.assertEqual(rate['projected_days_until_empty'], 1.67)

    def test_usage_report_fails_soft_on_missing_collectors_and_picks_next_worker(self):
        io = {
            'claude': fake_io('claude', 30.0),
            'codex': {'load': lambda: None, 'observe': lambda p: None, 'save': lambda o: None},
            'cursor': fake_io('cursor', 15.0),
        }
        burn_down.record_snapshot(
            self.policy,
            [{'worker': 'claude', 'percent_used': 20.0, 'collected_at': (NOW - timedelta(hours=5)).isoformat()}],
            now=NOW - timedelta(hours=5),
        )
        report = burn_down.usage_report(self.policy, now=NOW, io=io)
        # Equal expiry: allocation prefers Cursor's greater headroom over configured first-fit.
        self.assertEqual(report['next_worker'], 'cursor')
        by_worker = {w['worker']: w for w in report['workers']}
        self.assertEqual(by_worker['claude']['used_percent'], 30.0)
        self.assertEqual(by_worker['claude']['remaining_percent'], 70.0)
        self.assertEqual(by_worker['claude']['burn_rate_percent_per_hour'], 2.0)
        # Missing codex collector fails soft (unknown/stale) without crashing routing
        self.assertIsNone(by_worker['codex']['used_percent'])
        self.assertFalse(by_worker['codex']['eligible'])

        table = burn_down.render_usage_table(report)
        self.assertIn('Next worker: cursor', table)
        self.assertIn('30%', table)
        self.assertIn('2%/h', table)

    def test_cli_usage_human_table_and_json(self):
        out_table = io.StringIO()
        with contextlib.redirect_stdout(out_table):
            code = cli.main(['usage'])
        self.assertEqual(code, 0)
        self.assertIn('DLR Token Burn-Down', out_table.getvalue())

        out_json = io.StringIO()
        with contextlib.redirect_stdout(out_json):
            code_json = cli.main(['usage', '--json'])
        self.assertEqual(code_json, 0)
        parsed = json.loads(out_json.getvalue())
        self.assertIn('workers', parsed)
        self.assertIn('next_worker', parsed)


if __name__ == '__main__':
    unittest.main()
