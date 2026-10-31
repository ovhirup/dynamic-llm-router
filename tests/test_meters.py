import unittest
from datetime import datetime, timedelta, timezone

from llm_router import meters

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat().replace('+00:00', 'Z')


class MeterRecordTests(unittest.TestCase):
    def test_parses_fields(self):
        ev = {'source_worker': 'codex', 'windows': {'weekly': {
            'percent_used': 42, 'observed_at': iso(NOW - timedelta(minutes=5)),
            'reset_at': iso(NOW + timedelta(days=2))}}}
        [r] = meters.from_evidence(ev, source='codex-cli', now=NOW)
        self.assertEqual((r.worker, r.window, r.percent_used, r.source, r.status),
                         ('codex', 'weekly', 42, 'codex-cli', 'ok'))
        self.assertEqual(r.reset_at, NOW + timedelta(days=2))
        self.assertEqual(r.read_at, NOW - timedelta(minutes=5))
        d = r.to_dict()
        self.assertEqual(set(d), {'worker', 'window', 'percent_used', 'reset_at', 'read_at', 'source', 'status'})

    def test_stale_after_30_min_is_unknown(self):
        ev = {'source_worker': 'claude', 'windows': {
            'session': {'percent_used': 10, 'observed_at': iso(NOW - timedelta(minutes=31))},
            'weekly': {'percent_used': 20, 'observed_at': iso(NOW - timedelta(minutes=30))}}}
        recs = {r.window: r for r in meters.from_evidence(ev, now=NOW)}
        self.assertEqual(recs['session'].status, 'unknown')
        self.assertIsNone(recs['session'].percent_used)
        self.assertEqual(recs['weekly'].status, 'ok')
        self.assertEqual(recs['weekly'].percent_used, 20)

    def test_missing_or_bad_values_are_unknown(self):
        ev = {'windows': {'a': {'percent_used': 'x', 'observed_at': iso(NOW)},
                          'b': {'percent_used': 5},
                          'c': {'percent_used': 5, 'observed_at': '2026-10-31T09:00:00'}}}
        recs = meters.from_evidence(ev, worker='cursor', now=NOW)
        self.assertEqual([r.status for r in recs], ['unknown'] * 3)
        self.assertTrue(all(r.worker == 'cursor' for r in recs))

    def test_non_dict_evidence(self):
        self.assertEqual(meters.from_evidence(None), [])
        self.assertEqual(meters.from_evidence({'windows': None}), [])


if __name__ == '__main__':
    unittest.main()
