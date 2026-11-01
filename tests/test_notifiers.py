"""Tests for Slack and email notifier plugins (#78)."""
import json
import unittest
from pathlib import Path

from llm_router import notifiers


class NotifierPluginTests(unittest.TestCase):
    def test_packaged_presets_match_manifests_and_default_disabled(self):
        root = Path(__file__).resolve().parents[1] / 'src/llm_router/examples/plugins'
        self.assertEqual(json.loads((root / 'slack-notifier.json').read_text()), notifiers.SLACK_MANIFEST)
        self.assertEqual(json.loads((root / 'email-notifier.json').read_text()), notifiers.EMAIL_MANIFEST)
        self.assertFalse(notifiers.SLACK_MANIFEST['enabled'])
        self.assertFalse(notifiers.EMAIL_MANIFEST['enabled'])

    def test_job_and_daily_summaries_redact_secrets(self):
        secret = 'sk-' + 'e' * 30
        job_payload = notifiers.format_job_summary({
            'job_id': 'repo-20261031-120000',
            'task': 'fix.md',
            'status': 'committed',
            'ok': True,
            'workers': ['claude'],
            'tests_status': 'passed',
            'branch': 'router/fix',
            'commit': '1234567890abcdef',
            'warnings': [f'leaked {secret}'],
        })
        self.assertNotIn(secret, job_payload['slack_text'])
        self.assertNotIn(secret, job_payload['email_body'])
        self.assertIn('repo-20261031-120000', job_payload['subject'])

        daily = notifiers.format_daily_summary(
            [{'worker': 'codex', 'used': '25%', 'freshness': 'fresh'}],
            open_alerts=[{'message': f'alert with {secret}'}],
        )
        self.assertNotIn(secret, daily['slack_text'])
        self.assertIn('codex: 25% (fresh)', daily['slack_text'])

    def test_dispatch_respects_opt_in_and_calls_sender_when_enabled(self):
        payload = notifiers.format_job_summary({'job_id': 'j1', 'status': 'committed'})
        disabled = notifiers.dispatch_notification(notifiers.SLACK_MANIFEST, payload)
        self.assertFalse(disabled['sent'])
        self.assertEqual(disabled['reason'], 'plugin_disabled')

        delivered = []
        enabled = dict(notifiers.EMAIL_MANIFEST, enabled=True)
        res = notifiers.dispatch_notification(
            enabled, payload, sender=lambda name, p: delivered.append((name, p)) or {'status': 'queued'})
        self.assertTrue(res['sent'])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], 'email-notifier')


if __name__ == '__main__':
    unittest.main()
