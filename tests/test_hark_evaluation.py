"""Tests for Hark evaluation doc (`docs/hark-input-evaluation.md`) (#19): input source, never a worker."""
from pathlib import Path
import unittest

from helpers import load_policy
from llm_router import config

ROOT = Path(__file__).resolve().parents[1]


class HarkEvaluationTests(unittest.TestCase):
    def test_hark_doc_records_decision_and_hark_is_never_a_worker(self):
        doc = ROOT / 'docs' / 'hark-input-evaluation.md'
        self.assertTrue(doc.is_file())
        text = doc.read_text()
        self.assertIn('hark-mcp', text)
        self.assertIn('Owner Decision Recorded', text)
        self.assertIn('No worker is added for Hark', text)

        policy = load_policy()
        self.assertNotIn('hark', config.known_workers(policy))
        self.assertNotIn('hark', policy.get('worker_order') or [])


if __name__ == '__main__':
    unittest.main()
