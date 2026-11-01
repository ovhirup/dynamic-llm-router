"""capabilities.json: every capability claim is scored, described, sourced and dated."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from llm_router import capabilities, scheduler
from llm_router.config import TASK_CLASSES

WORKERS = ('claude', 'codex', 'grok-build', 'cursor', 'aider-ollama', 'developer')
GOOD = {'score': 0.5, 'claim': 'ok', 'source': 'https://example.com/eval', 'verified_at': '2026-11-02'}


class CapabilitiesTest(unittest.TestCase):
    def setUp(self):
        self.table = capabilities.load_capabilities()

    def test_packaged_file_loads_and_covers_every_worker_and_task_class(self):
        for worker in WORKERS:
            for task_class in TASK_CLASSES:
                entry = self.table['workers'][worker][task_class]
                self.assertTrue(0.0 <= entry['score'] <= 1.0)
                self.assertTrue(entry['claim'].strip())
                self.assertTrue(entry['source'].strip())
                self.assertTrue(entry['verified_at'].strip())

    def test_score_for_returns_score_or_none(self):
        self.assertEqual(capabilities.score_for('claude', 'review'),
                         self.table['workers']['claude']['review']['score'])
        self.assertIsNone(capabilities.score_for('claude', 'unknown-kind'))
        self.assertIsNone(capabilities.score_for('nobody', 'feature'))

    def _write(self, table):
        handle = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
        with handle:
            json.dump(table, handle)
        self.addCleanup(Path(handle.name).unlink)
        return handle.name

    def test_unsourced_or_invalid_entries_are_rejected_on_load(self):
        bad_values = [('source', ''), ('source', '   '), ('source', None), ('source', 'trust me bro'),
                      ('claim', ''), ('score', 1.5), ('score', -0.1), ('score', 'high'), ('score', True),
                      ('verified_at', '2026-13-01'), ('verified_at', 'yesterday'), ('verified_at', None)]
        for key, value in bad_values:
            with self.subTest(key=key, value=value):
                table = copy.deepcopy(self.table)
                table['workers']['codex']['bugfix'][key] = value
                with self.assertRaises(ValueError):
                    capabilities.load_capabilities(self._write(table))
        table = copy.deepcopy(self.table)
        del table['workers']['codex']['bugfix']['source']
        with self.assertRaises(ValueError):
            capabilities.load_capabilities(self._write(table))

    def test_accepted_source_forms(self):
        for source in ('https://example.com/x', 'eval:run-42', 'docs/STRATEGY_WORKERS_FALLBACK.md'):
            capabilities.validate_entry(dict(GOOD, source=source))

    def test_update_claim_requires_source(self):
        table = copy.deepcopy(self.table)
        for source in ('', None, '  '):
            with self.assertRaises(ValueError):
                capabilities.update_claim(table, 'codex', 'feature', 0.9, 'better', source, '2026-11-02')
        self.assertEqual(table, self.table)
        capabilities.update_claim(table, 'codex', 'feature', 0.95, 'better', 'eval:run-7', '2026-11-02')
        self.assertEqual(capabilities.score_for('codex', 'feature', table), 0.95)
        capabilities.update_claim(table, 'new-worker', 'draft', 0.3, 'new', 'https://x.test/a', '2026-11-01')
        self.assertEqual(capabilities.score_for('new-worker', 'draft', table), 0.3)

    def test_scheduler_uses_sourced_score_unless_policy_overrides(self):
        policy = {'scheduler': {'capabilities': {'codex': {'feature': 0.2}}}}
        self.assertEqual(scheduler.capability(policy, 'codex', 'feature'), 0.2)
        self.assertEqual(scheduler.capability(policy, 'aider-ollama', 'review'),
                         capabilities.score_for('aider-ollama', 'review'))
        self.assertEqual(scheduler.capability(policy, 'aider-ollama', 'code'), 1.0)
        self.assertEqual(scheduler.capability({}, 'unknown-worker', 'feature'), 1.0)


if __name__ == '__main__':
    unittest.main()
