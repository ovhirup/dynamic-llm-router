"""Tests for weekly model-news digest from X bookmarks and release notes (#53)."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from llm_router import model_news

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class ModelNewsDigestTests(unittest.TestCase):
    def test_writes_weekly_digest_and_extracts_sourced_candidate_claims(self):
        secret = 'sk-' + 'm' * 30
        items = [
            {
                'title': 'Claude Opus 5.5 prompting guide for hard bug fixes',
                'text': f'Medium effort default saves tokens on bugfix tasks. Key {secret}.',
                'url': 'https://x.com/charliejhills/status/2103807220941304246',
            },
            {
                'title': 'Qwen2.5-Coder 14B release notes for local Ollama',
                'text': 'Improved mechanical refactor accuracy on 16GB Apple Silicon.',
                'url': 'https://ollama.com/library/qwen2.5-coder',
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            report = model_news.write_weekly_digest(items, vault_dir=tmp, now=NOW)
            self.assertEqual(report['week'], '2026-W44')
            self.assertEqual(report['items'], 2)
            path = Path(report['path'])
            self.assertTrue(path.is_file())
            text = path.read_text()
            self.assertNotIn(secret, text)
            self.assertIn('## X Bookmarks (1)', text)
            self.assertIn('## Release Notes (1)', text)
            self.assertIn('candidate_requires_eval_harness', text)
            workers = {c['worker'] for c in report['candidate_claims']}
            self.assertIn('claude', workers)
            self.assertIn('aider-ollama', workers)

    def test_unsourced_items_are_rejected(self):
        for bad in ({'title': 'No url', 'text': 'claim'}, {'url': 'https://x.com/1', 'text': '   '}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    model_news.normalize_item(bad)


if __name__ == '__main__':
    unittest.main()
