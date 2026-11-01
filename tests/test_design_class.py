"""Tests for `design` task class and design plugin manifest/docs (#90)."""
import json
from pathlib import Path
import unittest

from helpers import load_policy
from llm_router import config, security, selection

ROOT = Path(__file__).resolve().parents[1]


class DesignTaskClassTests(unittest.TestCase):
    def test_design_task_class_is_supported_in_config_and_selection(self):
        self.assertIn('design', config.TASK_CLASSES)
        policy = load_policy(
            worker_order=['claude', 'cursor', 'codex'],
            unmetered_workers=['claude', 'cursor', 'codex'],
            class_order={'design': ['claude', 'cursor', 'codex']},
        )
        self.assertEqual(config.worker_order(policy, 'design'), ['claude', 'cursor', 'codex', 'grok-bot'])
        picked = selection.select(1000, policy=policy, io={}, task_class='design')
        self.assertEqual(picked['worker'], 'claude')
        self.assertEqual(picked['task_class'], 'design')

    def test_design_plugin_manifest_and_docs_exist_and_are_clean(self):
        doc = ROOT / 'docs' / 'design-plugins.md'
        self.assertTrue(doc.is_file())
        text = doc.read_text()
        self.assertIn('Framelink Figma MCP', text)
        self.assertIn('Impeccable', text)

        manifest_path = ROOT / 'src' / 'llm_router' / 'examples' / 'plugins' / 'figma-context.json'
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text())
        self.assertEqual(manifest['name'], 'figma-context')
        self.assertEqual(manifest['capabilities'], ['context'])
        self.assertFalse(manifest['enabled'])
        self.assertTrue(security.lint_commands({manifest['name']: manifest['command']}, allowed=set()))


if __name__ == '__main__':
    unittest.main()
