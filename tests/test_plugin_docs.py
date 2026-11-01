"""Tests for contributor plugin docs and packaged example manifest (#80)."""
import json
import unittest
from pathlib import Path

from llm_router import security

ROOT = Path(__file__).resolve().parents[1]


class PluginDocsTests(unittest.TestCase):
    def test_plugin_docs_cover_capabilities_consent_and_example(self):
        doc = ROOT / 'docs' / 'plugins.md'
        self.assertTrue(doc.is_file())
        text = doc.read_text()
        for capability in ('input_source', 'context', 'notifier', 'output_sink'):
            self.assertIn(capability, text)
        self.assertIn('mcp_stdio', text)
        self.assertIn('sample-plugin', text)
        self.assertIn('0600', text)

    def test_sample_plugin_manifest_is_valid_opt_in_and_secret_free(self):
        path = ROOT / 'src' / 'llm_router' / 'examples' / 'plugins' / 'sample-plugin.json'
        self.assertTrue(path.is_file())
        manifest = json.loads(path.read_text())
        self.assertEqual(manifest['name'], 'sample-plugin')
        self.assertEqual(manifest['capabilities'], ['context', 'notifier'])
        self.assertEqual(manifest['transport'], 'mcp_stdio')
        self.assertFalse(manifest['enabled'])
        self.assertTrue(security.lint_commands({manifest['name']: manifest['command']}, allowed=set()))
        self.assertEqual(list(security.find_secrets(path.read_text())), [])


if __name__ == '__main__':
    unittest.main()
