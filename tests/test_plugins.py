"""Tests for plugin manifest validation and registry (#74)."""
import json
import tempfile
import unittest
from pathlib import Path

from llm_router import plugins


class PluginManifestTests(unittest.TestCase):
    def test_valid_manifest_defaults_to_disabled(self):
        manifest = plugins.validate_manifest({
            'name': 'hark',
            'capabilities': ['input_source', 'context'],
            'transport': 'mcp_stdio',
            'command': ['hark-mcp'],
            'description': 'Dictation and meeting transcripts',
        })
        self.assertEqual(manifest['name'], 'hark')
        self.assertFalse(manifest['enabled'])
        self.assertEqual(manifest['capabilities'], ['input_source', 'context'])

    def test_invalid_manifests_raise_plugin_error(self):
        secret = 'sk-' + 'a' * 30
        cases = [
            'not-a-dict',
            {'name': 'BadName', 'capabilities': ['context'], 'command': ['x']},
            {'name': 'ok', 'capabilities': [], 'command': ['x']},
            {'name': 'ok', 'capabilities': ['unknown_cap'], 'command': ['x']},
            {'name': 'ok', 'capabilities': ['context', 'context'], 'command': ['x']},
            {'name': 'ok', 'capabilities': ['context'], 'transport': 'http', 'command': ['x']},
            {'name': 'ok', 'capabilities': ['context'], 'transport': 'mcp_stdio', 'command': []},
            {'name': 'ok', 'capabilities': ['context'], 'command': ['x', f'--api-key={secret}']},
            {'name': 'ok', 'capabilities': ['context'], 'command': ['x'], 'description': secret},
            {'name': 'ok', 'capabilities': ['context'], 'command': ['x'], 'extra_key': 1},
        ]
        for bad in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(plugins.PluginError):
                    plugins.validate_manifest(bad)

    def test_registry_register_filter_and_enable_disable(self):
        reg = plugins.PluginRegistry([
            {'name': 'hark', 'capabilities': ['input_source'], 'command': ['hark-mcp']},
            {'name': 'vault', 'capabilities': ['context', 'output_sink'], 'transport': 'builtin', 'enabled': True},
        ])
        with self.assertRaises(plugins.PluginError):
            reg.register({'name': 'hark', 'capabilities': ['input_source'], 'command': ['hark-mcp']})
        self.assertEqual([p['name'] for p in reg.list_plugins()], ['hark', 'vault'])
        self.assertEqual([p['name'] for p in reg.list_plugins(enabled_only=True)], ['vault'])
        self.assertEqual([p['name'] for p in reg.list_plugins(capability='input_source')], ['hark'])
        reg.enable('hark')
        self.assertEqual([p['name'] for p in reg.list_plugins(enabled_only=True)], ['hark', 'vault'])
        reg.disable('vault')
        self.assertEqual([p['name'] for p in reg.list_plugins(enabled_only=True)], ['hark'])

    def test_discover_manifests_and_load_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'slack.json').write_text(json.dumps({
                'name': 'slack-notify',
                'capabilities': ['notifier'],
                'transport': 'command',
                'command': ['notify-slack'],
            }))
            reg = plugins.load_registry(
                policy={'plugins': {'obsidian': {'capabilities': ['context'], 'transport': 'builtin'}}},
                directory=tmp,
            )
            self.assertEqual([p['name'] for p in reg.list_plugins()], ['slack-notify', 'obsidian'])


if __name__ == '__main__':
    unittest.main()
