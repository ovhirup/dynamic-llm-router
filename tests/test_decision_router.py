"""Tests for Decision-model router (#114): one-pass P(yes) scoring, hard gates, and shadow agreement."""
import unittest

from helpers import load_policy
from llm_router import decision_router, eval_harness, selection
from test_eval import EvalFixture


class DecisionModelRouterTests(unittest.TestCase):
    def test_hard_gates_exclude_grok_bot_and_ineligible_workers(self):
        shadow = decision_router.score_candidates(
            'Fix a crash bug in parser.py when input has spaces',
            eligible_workers=['codex', 'cursor', 'grok-bot'],
            selected_worker='codex',
        )
        self.assertEqual(shadow['mode'], 'shadow')
        self.assertEqual(shadow['source'], 'local_fallback')
        self.assertEqual(shadow['predicted_class'], 'bugfix')
        self.assertNotIn('grok-bot', shadow['worker_p_yes'])
        self.assertNotIn('grok-bot', shadow['ranked_workers'])
        self.assertEqual(shadow['shadow_pick'], 'codex')
        self.assertTrue(shadow['agrees_with_selected'])

    def test_custom_model_failure_falls_back_to_local_scorer(self):
        def broken_model(*args, **kwargs):
            raise RuntimeError('model endpoint offline')

        shadow = decision_router.score_candidates(
            'Implement new feature for token meter',
            eligible_workers=['claude', 'codex'],
            selected_worker='claude',
            model=broken_model,
        )
        self.assertEqual(shadow['source'], 'local_fallback')
        self.assertEqual(shadow['shadow_pick'], 'claude')
        self.assertTrue(shadow['agrees_with_selected'])

    def test_selection_includes_decision_shadow_while_preserving_worker_choice(self):
        policy = load_policy(worker_order=['cursor', 'claude'], unmetered_workers=['cursor', 'claude'])
        result = selection.select(1000, policy=policy, io={}, task_text='Review the diff and audit tests')
        # Hard gate: worker_order picks cursor first even when shadow prefers claude for review
        self.assertEqual(result['worker'], 'cursor')
        self.assertEqual(result['decision_shadow']['shadow_pick'], 'claude')
        self.assertFalse(result['decision_shadow']['agrees_with_selected'])


class EvalShadowAgreementTests(EvalFixture):
    def test_eval_output_records_shadow_picks_and_agreement_rate(self):
        report = eval_harness.run(self.cases, self.specs, profiles=self.profiles)
        for name in ('baseline', 'cursor-first'):
            entry = report['policies'][name]
            self.assertIn('shadow_agreement_rate', entry['metrics'])
            self.assertIsNotNone(entry['metrics']['shadow_agreement_rate'])
            self.assertTrue(all('shadow_pick' in s for s in entry['scores']))


if __name__ == '__main__':
    unittest.main()
