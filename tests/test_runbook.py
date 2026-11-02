"""Tests for the quota-drought runbook (`docs/runbook-all-workers-out.md`) (#33)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RunbookTests(unittest.TestCase):
    def test_runbook_exists_is_under_one_page_and_covers_steps_and_reset_table(self):
        path = ROOT / 'docs' / 'runbook-all-workers-out.md'
        self.assertTrue(path.is_file())
        text = path.read_text()
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 60, 'runbook must stay under one page')
        self.assertIn('llm-router status', text)
        self.assertIn('mechanical', text)
        self.assertIn('Grok Bot', text)
        self.assertIn('Saturdays 10:49 IST', text)
        self.assertIn('| Worker / Pool |', text)


if __name__ == '__main__':
    unittest.main()
