"""Tests for reading app usage meters via `agent-desktop` accessibility tree snapshots (#95)."""
from datetime import datetime, timezone
from pathlib import Path
import unittest

from llm_router.observers import accessibility

FIXTURES = Path(__file__).parent / 'fixtures'
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class AccessibilityMeterTests(unittest.TestCase):
    def test_reads_cursor_and_codex_snapshot_fixtures_into_meter_records(self):
        cursor_raw = (FIXTURES / 'ax_cursor_usage.json').read_text()
        cursor_rec = accessibility.parse_ax_snapshot('cursor', cursor_raw, now=NOW)
        self.assertEqual(cursor_rec.status, 'ok')
        self.assertEqual(cursor_rec.worker, 'cursor')
        self.assertEqual(cursor_rec.window, 'monthly')
        self.assertEqual(cursor_rec.percent_used, 34.0)
        self.assertEqual(cursor_rec.source, 'accessibility')
        self.assertEqual(cursor_rec.reset_at, datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc))

        codex_raw = (FIXTURES / 'ax_codex_usage.json').read_text()
        codex_rec = accessibility.parse_ax_snapshot('codex', codex_raw, now=NOW)
        self.assertEqual(codex_rec.status, 'ok')
        self.assertEqual(codex_rec.worker, 'codex')
        self.assertEqual(codex_rec.window, 'weekly')
        self.assertEqual(codex_rec.percent_used, 42.5)
        self.assertEqual(codex_rec.reset_at, datetime(2026, 11, 4, 6, 47, tzinfo=timezone.utc))

    def test_any_parse_doubt_returns_unknown_and_command_is_read_only(self):
        for bad in (
            'not json',
            '{}',
            '{"children":[{"value":"10% used"},{"value":"50% used"}]}',
            '{"children":[{"value":"140% used"}]}',
        ):
            with self.subTest(bad=bad):
                rec = accessibility.parse_ax_snapshot('cursor', bad, now=NOW)
                self.assertEqual(rec.status, 'unknown')
                self.assertIsNone(rec.percent_used)

        argv = accessibility.build_snapshot_command('cursor')
        self.assertIn('snapshot', argv)
        self.assertTrue(all(a not in accessibility.FORBIDDEN_ACTIONS for a in argv))


if __name__ == '__main__':
    unittest.main()
