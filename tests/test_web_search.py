"""Tests for opt-in `web_search` tool for local workers and Router Brain collector (#94)."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import web_search

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


class WebSearchToolTests(unittest.TestCase):
    def test_disabled_by_default_and_evaluates_candidates(self):
        policy = load_policy()
        self.assertFalse(web_search.is_enabled(policy))
        self.assertTrue(web_search.CANDIDATE_EVALUATION['searchlight']['selected_default'])
        self.assertFalse(web_search.CANDIDATE_EVALUATION['searchlight']['requires_api_key'])
        with self.assertRaisesRegex(web_search.WebSearchRefused, 'disabled by default'):
            web_search.search_and_collect('what changed this week', policy=policy)

    def test_opted_in_search_stores_to_raw_with_url_and_date_and_cites_sources(self):
        secret = 'sk-' + 'k' * 30
        policy = load_policy(web_search={'enabled': True, 'provider': 'searchlight'})

        def fake_fetcher(query):
            return [
                {
                    'title': 'Claude Code 2.1 Release Notes',
                    'url': 'https://example.com/releases/claude-2-1',
                    'snippet': f'Added plugin hooks for headless runs. Redact {secret}.',
                },
                {
                    'title': 'MLX 0.4 Update',
                    'url': 'https://example.com/releases/mlx-0-4',
                    'snippet': 'Faster 4-bit KV cache on Apple Silicon M4.',
                },
            ]

        with tempfile.TemporaryDirectory() as tmp:
            res = web_search.search_and_collect(
                'what changed this week',
                policy=policy,
                vault_root=tmp,
                fetcher=fake_fetcher,
                now=NOW,
            )
            raw_file = Path(res['raw_path'])
            self.assertTrue(raw_file.is_file())
            self.assertEqual(raw_file.parent.name, 'raw')
            saved = json.loads(raw_file.read_text())
            self.assertEqual(saved['fetched_at'], '2026-11-01T10:00:00+00:00')
            self.assertEqual(len(saved['results']), 2)
            self.assertEqual(saved['results'][0]['source_url'], 'https://example.com/releases/claude-2-1')
            self.assertNotIn(secret, raw_file.read_text())
            self.assertIn('[Claude Code 2.1 Release Notes](https://example.com/releases/claude-2-1)',
                          res['cited_context'])
            self.assertIn('2026-11-01T10:00:00+00:00', res['cited_context'])


if __name__ == '__main__':
    unittest.main()
