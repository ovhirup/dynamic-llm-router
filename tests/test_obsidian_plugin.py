"""Tests for Obsidian vault as a plugin (#77)."""
import json
import tempfile
import unittest
from pathlib import Path

from llm_router import obsidian_plugin


class ObsidianVaultPluginTests(unittest.TestCase):
    def test_manifest_matches_packaged_preset(self):
        preset = Path(__file__).resolve().parents[1] / 'src/llm_router/examples/plugins/obsidian-vault.json'
        self.assertEqual(json.loads(preset.read_text()), obsidian_plugin.MANIFEST)
        self.assertEqual(obsidian_plugin.MANIFEST['capabilities'], ['context', 'output_sink'])
        self.assertFalse(obsidian_plugin.MANIFEST['enabled'])

    def test_write_search_and_read_rules_redact_secrets(self):
        secret = 'sk-' + 'd' * 30
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'rules.md').write_text(f'- Never leak {secret}\n- Run tests first\n')
            rules = obsidian_plugin.read_rules(tmp)
            self.assertNotIn(secret, rules)
            self.assertIn('Run tests first', rules)

            note_path = obsidian_plugin.write_note(
                tmp, 'jobs', 'job-20261031-100000', f'# Outcome\nPassed with token {secret}\n')
            self.assertTrue(note_path.is_file())
            self.assertNotIn(secret, note_path.read_text())

            hits = obsidian_plugin.search_notes(tmp, 'Outcome')
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]['path'], 'jobs/job-20261031-100000.md')
            self.assertNotIn(secret, hits[0]['excerpt'])

    def test_rejects_unknown_folder_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            for folder, name in [('other', 'ok'), ('jobs', '../escape'), ('jobs', '/abs')]:
                with self.subTest(folder=folder, name=name):
                    with self.assertRaises(ValueError):
                        obsidian_plugin.write_note(tmp, folder, name, 'x')


if __name__ == '__main__':
    unittest.main()
