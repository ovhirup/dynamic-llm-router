"""Tests for reuse-before-build open-source scan (`docs/reuse-scan.md`) (#139)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReuseScanDocTests(unittest.TestCase):
    def test_reuse_scan_doc_exists_and_covers_all_required_rows_and_verdicts(self):
        doc = ROOT / 'docs' / 'reuse-scan.md'
        self.assertTrue(doc.is_file())
        text = doc.read_text()
        for item in ('#137', '#131', '#133', '#135', '#128', 'OpenTelemetry', 'AGENTS.md',
                     'DSPy', 'RouteLLM', 'E2B', 'aider-ollama', 'Verdict'):
            self.assertIn(item, text)


if __name__ == '__main__':
    unittest.main()
