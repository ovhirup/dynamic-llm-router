import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from llm_router import meters

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)


def rec(worker, pct, status='ok', window='weekly'):
    return meters.MeterRecord(worker, window, pct, NOW, NOW, 'test', status)


class MeterAlertTests(unittest.TestCase):
    def test_levels(self):
        self.assertIsNone(meters.alert_level(rec('a', 84.9)))
        self.assertEqual(meters.alert_level(rec('a', 85)), 'warn')
        self.assertEqual(meters.alert_level(rec('a', 100)), 'exhausted')
        self.assertEqual(meters.alert_level(rec('a', None, 'unknown')), 'unknown')

    def test_alerts_sorted_exhausted_first(self):
        out = meters.alerts([rec('b', 90), rec('a', 10), rec('c', 100), rec('d', None, 'unknown')])
        self.assertEqual([(lvl, r.worker) for lvl, r in out], [('exhausted', 'c'), ('warn', 'b'), ('unknown', 'd')])

    def test_summary_text(self):
        text = meters.daily_summary([rec('codex', 90), rec('claude', 20)], zone=timezone.utc)
        self.assertIn('- claude weekly: 20%', text)
        self.assertIn('WARN: codex weekly at or above 85%', text)
        self.assertEqual(meters.daily_summary([]).splitlines()[-1], 'Alerts: none')

    def test_cli_set_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / 'm.json')
            self.assertEqual(meters.main(['set', '--manual-file', path, '--worker', 'cursor', '--window', 'monthly',
                                          '--percent', '100']), 0)
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = meters.main(['summary', '--manual-file', path])
            self.assertEqual(code, 1)
            self.assertIn('EXHAUSTED: cursor monthly', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
