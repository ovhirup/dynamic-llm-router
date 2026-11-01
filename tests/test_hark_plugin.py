"""Tests for the Hark reference plugin (#76)."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import TERM, load_policy
from llm_router import config, hark_plugin


class HarkPluginTests(unittest.TestCase):
    def test_hark_is_input_plugin_never_coding_worker(self):
        policy = load_policy()
        self.assertNotIn('hark', config.known_workers(policy))
        self.assertEqual(hark_plugin.MANIFEST['name'], 'hark')
        self.assertEqual(hark_plugin.MANIFEST['capabilities'], ['input_source', 'context'])
        self.assertFalse(hark_plugin.MANIFEST['enabled'])
        preset = Path(__file__).resolve().parents[1] / 'src/llm_router/examples/plugins/hark.json'
        self.assertEqual(json.loads(preset.read_text()), hark_plugin.MANIFEST)

    def test_transcript_to_task_redacts_secrets_and_private_terms(self):
        secret = 'sk-' + 'c' * 30
        record = {
            'id': 'mtg-42',
            'title': f'Fix pacing bug in {TERM}',
            'recorded_at': '2026-10-31T09:00:00Z',
            'transcript': f'Please fix the reset calculation. Token was {secret}.',
            'action_items': ['Update week_curve.py', 'Add frozen-clock unit test'],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = hark_plugin.write_task_from_transcript(
                record, tmp, allowed_paths=['src/llm_router/week_curve.py', 'tests/'], private_terms=[TERM])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            content = path.read_text()
        self.assertNotIn(secret, content)
        self.assertNotIn(TERM, content)
        self.assertIn('## Allowed paths\n- src/llm_router/week_curve.py\n- tests/', content)
        self.assertIn('- Update week_curve.py', content)

    def test_empty_or_invalid_transcript_is_rejected(self):
        for bad in (None, {}, {'title': 'Empty', 'transcript': '   '}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    hark_plugin.transcript_to_task(bad)


if __name__ == '__main__':
    unittest.main()
