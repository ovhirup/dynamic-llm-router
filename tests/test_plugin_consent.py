"""Tests for per-plugin permission/consent model and secret-free plugin logs (#79)."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import TERM, load_policy
from llm_router import plugin_consent


class PluginConsentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.policy = load_policy()
        self.policy['plugins'] = {
            'consent_file': str(base / 'consent.json'),
            'log_file': str(base / 'plugins.jsonl'),
        }
        terms = base / 'terms.txt'
        terms.write_text(TERM + '\n')
        self.policy['run_task']['private_terms_file'] = str(terms)
        self.manifest = {
            'name': 'hark',
            'capabilities': ['input_source'],
            'transport': 'mcp_stdio',
            'command': ['hark-mcp'],
            'enabled': True,
            'permissions': ['read_transcripts'],
        }

    def test_opt_in_and_consent_required_before_use(self):
        with self.assertRaisesRegex(plugin_consent.ConsentRefused, 'disabled'):
            plugin_consent.check_permission(self.policy, dict(self.manifest, enabled=False), 'read_transcripts')
        with self.assertRaisesRegex(plugin_consent.ConsentRefused, 'does not declare'):
            plugin_consent.check_permission(self.policy, self.manifest, 'write_vault')
        with self.assertRaisesRegex(plugin_consent.ConsentRefused, 'lacks user consent'):
            plugin_consent.check_permission(self.policy, self.manifest, 'read_transcripts')

        plugin_consent.grant_consent(self.policy, 'hark', ['read_transcripts'])
        self.assertTrue(plugin_consent.check_permission(self.policy, self.manifest, 'read_transcripts'))

        plugin_consent.revoke_consent(self.policy, 'hark', ['read_transcripts'])
        with self.assertRaisesRegex(plugin_consent.ConsentRefused, 'lacks user consent'):
            plugin_consent.check_permission(self.policy, self.manifest, 'read_transcripts')

    def test_plugin_log_redacts_secrets_and_private_terms(self):
        secret = 'sk-' + 'f' * 30
        bearer = 'Bearer ' + 'g' * 24
        entry = plugin_consent.log_plugin_invocation(
            self.policy,
            'hark',
            'read_transcripts',
            detail={'note': f'project {TERM} used {secret} and {bearer}'},
        )
        log_file = Path(self.policy['plugins']['log_file'])
        self.assertEqual(log_file.stat().st_mode & 0o777, 0o600)
        raw = log_file.read_text()
        self.assertNotIn(secret, raw)
        self.assertNotIn('g' * 24, raw)
        self.assertNotIn(TERM, raw)
        self.assertEqual(json.loads(raw.splitlines()[0]), entry)


if __name__ == '__main__':
    unittest.main()
