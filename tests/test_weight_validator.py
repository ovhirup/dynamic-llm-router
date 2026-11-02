"""Tests for validating every routing weight change in the eval harness before applying (#71)."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import load_policy
from llm_router import eval_corpus, weight_validator

FIXTURES = Path(__file__).parent / 'fixtures' / 'eval'


class WeightValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        corpus = base / 'corpus.jsonl'
        self.policy = load_policy(
            worker_order=['grok-build', 'codex', 'cursor'],
            unmetered_workers=['grok-build', 'codex', 'cursor'],
            scheduler={'capabilities': {
                'grok-build': {'feature': 0.95},
                'codex': {'feature': 0.85},
                'cursor': {'feature': 0.70},
            }},
        )
        eval_corpus.capture(self.policy, source=FIXTURES / 'jobs', out=corpus)
        self.cases = eval_corpus.load(corpus)
        self.profiles = json.loads((FIXTURES / 'fake_workers.json').read_text())

    def test_winning_weight_change_is_approved_and_applied(self):
        state_file = Path(self.tmp.name) / 'weights.json'
        candidate = {
            'cursor': {'feature': 0.98},
            'codex': {'feature': 0.85},
            'grok-build': {'feature': 0.60},
        }
        verdict = weight_validator.apply_validated_weights(
            self.policy, candidate, self.cases, state_path=state_file, profiles=self.profiles)
        self.assertTrue(verdict['approved'])
        self.assertTrue(verdict['gate']['adopt'])
        self.assertEqual(self.policy['scheduler']['capabilities'], candidate)
        saved = json.loads(state_file.read_text())
        self.assertTrue(saved['approved'])
        self.assertEqual(saved['capabilities'], candidate)

    def test_regressing_or_tied_weight_change_is_rejected_and_not_applied(self):
        state_file = Path(self.tmp.name) / 'weights.json'
        original = dict(self.policy['scheduler']['capabilities'])
        tied = dict(original)
        verdict = weight_validator.apply_validated_weights(
            self.policy, tied, self.cases, state_path=state_file, profiles=self.profiles)
        self.assertFalse(verdict['approved'])
        self.assertFalse(verdict['gate']['adopt'])
        self.assertEqual(self.policy['scheduler']['capabilities'], original)
        self.assertFalse(state_file.exists())


if __name__ == '__main__':
    unittest.main()
