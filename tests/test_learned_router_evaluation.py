"""Tests for learned router evaluation (`docs/learned-router-evaluation.md`) (#56)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LearnedRouterEvaluationTests(unittest.TestCase):
    def test_learned_router_doc_exists_and_records_routellm_comparison_and_decision(self):
        doc = ROOT / 'docs' / 'learned-router-evaluation.md'
        self.assertTrue(doc.is_file())
        text = doc.read_text()
        for term in ('RouteLLM', '16 GB', 'UCB1', 'Decision', 'escalation_threshold'):
            self.assertIn(term, text)


if __name__ == '__main__':
    unittest.main()
