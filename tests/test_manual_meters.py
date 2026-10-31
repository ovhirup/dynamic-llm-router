import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llm_router import meters

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)


class ManualMeterTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / 'state' / 'manual_meters.json'

    def tearDown(self):
        self.dir.cleanup()

    def test_set_and_load(self):
        meters.set_manual('cursor', 'monthly', 40, '2026-11-03T00:00:00Z', path=self.path, now=NOW)
        meters.set_manual('grok-bot', 'weekly', 70.5, path=self.path, now=NOW)
        recs = {r.worker: r for r in meters.load_manual(self.path, now=NOW + timedelta(minutes=10))}
        self.assertEqual(recs['cursor'].percent_used, 40)
        self.assertEqual(recs['cursor'].source, 'manual')
        self.assertEqual(recs['cursor'].reset_at, datetime(2026, 11, 3, tzinfo=timezone.utc))
        self.assertEqual(recs['grok-bot'].status, 'ok')

    def test_stale_manual_is_unknown(self):
        meters.set_manual('cursor', 'monthly', 40, path=self.path, now=NOW)
        [r] = meters.load_manual(self.path, now=NOW + timedelta(hours=25))
        self.assertEqual(r.status, 'unknown')

    def test_validation_and_missing_file(self):
        with self.assertRaises(ValueError):
            meters.set_manual('cursor', 'monthly', 140, path=self.path)
        with self.assertRaises(ValueError):
            meters.set_manual('cursor', 'monthly', 10, 'tomorrow', path=self.path)
        self.assertEqual(meters.load_manual(self.path), [])


if __name__ == '__main__':
    unittest.main()
