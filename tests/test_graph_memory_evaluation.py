"""Tests for graph memory evaluation (`docs/graph-memory-evaluation.md`) (#51)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class GraphMemoryEvaluationTests(unittest.TestCase):
    def test_graph_memory_evaluation_doc_exists_and_records_decision(self):
        doc = ROOT / 'docs' / 'graph-memory-evaluation.md'
        self.assertTrue(doc.is_file())
        text = doc.read_text()
        for term in ('Graphiti', 'Letta', 'Obsidian', '16 GB', 'Decision'):
            self.assertIn(term, text)


if __name__ == '__main__':
    unittest.main()
