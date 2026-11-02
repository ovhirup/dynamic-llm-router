"""Tests for human-readable routing change log in the Obsidian vault (#72)."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from llm_router import routing_changelog


class RoutingChangelogTests(unittest.TestCase):
    def test_appends_and_reads_redacted_human_readable_entries(self):
        secret = 'sk-' + 'j' * 30
        now = datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = routing_changelog.append_routing_change(
                title='Promote claude for bugfix class',
                before='codex > claude',
                after='claude > codex',
                reason=f'Higher pass rate (1.0 vs 0.5); token {secret}',
                source='reports/20261101-claude-vs-codex.json',
                vault_dir=tmp,
                now=now,
            )
            self.assertEqual(path, Path(tmp) / 'routing-changelog.md')
            raw = path.read_text()
            self.assertTrue(raw.startswith('# Routing Change Log'))
            self.assertNotIn(secret, raw)
            self.assertIn('`codex > claude`', raw)
            self.assertIn('`claude > codex`', raw)
            self.assertIn('reports/20261101-claude-vs-codex.json', raw)

            routing_changelog.append_routing_change(
                title='Demote cursor after drift alert',
                before='cursor weight 0.85',
                after='cursor weight 0.42',
                reason='below_quality_floor',
                source='drift:cursor:0.2',
                vault_dir=tmp,
                now=now,
            )
            entries = routing_changelog.read_routing_changes(vault_dir=tmp)
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]['title'], 'Promote claude for bugfix class')
            self.assertEqual(entries[1]['title'], 'Demote cursor after drift alert')


if __name__ == '__main__':
    unittest.main()
