"""Tests for Router Brain collect / update / audit wiki with verified notes (#93)."""
from datetime import date
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import alerts, brain_wiki


class BrainWikiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        self.policy = load_policy()
        self.policy['alerts'] = {
            'file': str(Path(self.tmp.name) / 'alerts.jsonl'),
            'summary': str(Path(self.tmp.name) / 'ALERTS.md'),
            'macos_notification': False,
        }

    def test_collect_writes_immutable_redacted_raw_record(self):
        secret = 'sk-' + 'h' * 30
        raw_path = brain_wiki.collect(self.vault, 'job-20261031-100000', {'note': f'used {secret}'})
        self.assertTrue(raw_path.is_file())
        self.assertNotIn(secret, raw_path.read_text())
        with self.assertRaises(FileExistsError):
            brain_wiki.collect(self.vault, 'job-20261031-100000', {'note': 'overwrite'})

    def test_update_conflict_raises_alert_without_overwriting_claim(self):
        first = brain_wiki.update_wiki_note(
            self.vault, 'swift-worker', 'Claude is best for Swift',
            source='https://example.com/eval-1', verified_at='2026-10-31', policy=self.policy)
        self.assertEqual(first['status'], 'updated')

        conflict = brain_wiki.update_wiki_note(
            self.vault, 'swift-worker', 'Local model is best for Swift',
            source='https://example.com/eval-2', verified_at='2026-11-01', policy=self.policy)
        self.assertEqual(conflict['status'], 'conflict')
        self.assertEqual(conflict['meta']['claim'], 'Claude is best for Swift')
        self.assertIn('Local model is best for Swift', conflict['meta']['conflict'])
        open_alerts = alerts.open_alerts(self.policy)
        self.assertTrue(any('swift-worker' in a['key'] for a in open_alerts))

    def test_audit_marks_stale_and_orphan_notes_unverified_and_routing_ignores_them(self):
        brain_wiki.collect(self.vault, 'job-1', {'ok': True})
        brain_wiki.update_wiki_note(
            self.vault, 'fresh-note', 'Claude leads feature class',
            source='raw/job-1.json', verified_at='2026-10-25')
        brain_wiki.update_wiki_note(
            self.vault, 'stale-note', 'Old ranking claim',
            source='https://example.com/old', verified_at='2026-09-01')
        brain_wiki.update_wiki_note(
            self.vault, 'orphan-note', 'Claim with missing raw file',
            source='raw/missing-job.json', verified_at='2026-10-28')

        report = brain_wiki.audit_wiki(self.vault, now=date(2026, 11, 1), max_age_days=30)
        self.assertEqual(report['verified'], ['fresh-note'])
        self.assertEqual(report['stale'], ['stale-note'])
        self.assertEqual(report['orphans'], ['orphan-note'])

        verified = brain_wiki.read_verified_notes(self.vault, now=date(2026, 11, 1), max_age_days=30)
        self.assertEqual([n['topic'] for n in verified], ['fresh-note'])


if __name__ == '__main__':
    unittest.main()
