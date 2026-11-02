"""Tests for docs/workers.md per-worker ranking, Claude safety, and local model RAM notes (#32)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkersDocTests(unittest.TestCase):
    def test_workers_doc_covers_ranking_claude_safety_and_local_ram_notes(self):
        doc = ROOT / 'docs' / 'workers.md'
        self.assertTrue(doc.is_file())
        text = doc.read_text()
        for worker in ('claude', 'codex', 'grok-build', 'cursor', 'nativ', 'aider-ollama', 'grok-bot'):
            self.assertIn(worker, text)
        for cls in ('feature', 'bugfix', 'review', 'mechanical', 'draft'):
            self.assertIn(cls, text)
        self.assertIn('Claude account safety', text)
        self.assertIn('personal', text)
        self.assertIn('16 GB', text)
        self.assertIn('qwen2.5-coder:14b', text)
        self.assertIn('local_busy', text)


if __name__ == '__main__':
    unittest.main()
