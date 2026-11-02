"""Tests for Jev API, pricing, and data policy research doc (`docs/jev-research.md`) (#58)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class JevResearchDocTests(unittest.TestCase):
    def test_jev_research_doc_covers_api_pricing_and_data_policy(self):
        doc = ROOT / 'docs' / 'jev-research.md'
        self.assertTrue(doc.is_file())
        text = doc.read_text()
        for term in ('API Shape', 'Pricing', 'Data & Privacy Policy', '0.042', 'confidence', 'opt-in'):
            self.assertIn(term, text)


if __name__ == '__main__':
    unittest.main()
