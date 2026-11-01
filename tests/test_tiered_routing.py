"""Tests for tiered model routing inside workers (#91): decision ladder, 100K guard, Explore.md, and eval comparison."""
import unittest

from llm_router import tiered_routing


class TieredModelRoutingTests(unittest.TestCase):
    def test_decision_ladder_maps_steps_and_escalates_on_low_confidence(self):
        self.assertEqual(tiered_routing.classify_step('diff_scope')[0], 'code_rule')
        self.assertEqual(tiered_routing.classify_step('search')[0], 'cheap')
        self.assertEqual(tiered_routing.classify_step('test_triage')[0], 'cheap')
        self.assertEqual(tiered_routing.classify_step('code')[0], 'normal')
        self.assertEqual(tiered_routing.classify_step('plan')[0], 'strong')
        self.assertEqual(tiered_routing.classify_step('code', confidence=0.3)[0], 'strong')
        self.assertEqual(tiered_routing.classify_step('code', irreversible=True)[0], 'human')
        self.assertEqual(tiered_routing.classify_step('push')[0], 'human')

    def test_cheap_tier_100k_token_guard_and_opt_in_explore_agent(self):
        ok_tokens = tiered_routing.check_cheap_tier_prompt('short question + 2 file snippets')
        self.assertLess(ok_tokens, 100)
        oversized = 'x' * (tiered_routing.CHEAP_MAX_CHARS + 100)
        with self.assertRaisesRegex(tiered_routing.TierGuardError, '100000 token limit'):
            tiered_routing.check_cheap_tier_prompt(oversized)

        agent_md = tiered_routing.explore_agent_path()
        self.assertTrue(agent_md.is_file())
        text = agent_md.read_text()
        self.assertIn('model: haiku', text)
        self.assertIn('effort: low', text)

        opts = tiered_routing.claude_tiered_options('normal', advisor_opus=True)
        self.assertEqual(opts['env'], {'CLAUDE_CODE_SUBAGENT_MODEL': 'haiku'})
        self.assertEqual(opts['flags'], ['--model', 'sonnet', '--effort', 'medium', '--advisor', 'opus'])

    def test_harness_compares_tiered_vs_single_model_on_same_tasks(self):
        tasks = [
            {
                'task_id': 't1',
                'passed': True,
                'steps': [
                    {'kind': 'diff_scope', 'tokens': 2000},
                    {'kind': 'search', 'tokens': 15000},
                    {'kind': 'plan', 'tokens': 8000},
                    {'kind': 'code', 'tokens': 20000},
                    {'kind': 'test_triage', 'tokens': 5000},
                ],
            },
            {
                'task_id': 't2',
                'passed': True,
                'steps': [
                    {'kind': 'search', 'tokens': 12000},
                    {'kind': 'code', 'tokens': 18000},
                ],
            },
        ]
        report = tiered_routing.compare_tiered_vs_single_model(tasks)
        self.assertEqual(report['cases'], 2)
        self.assertEqual(report['tiered']['pass_rate'], 1.0)
        self.assertEqual(report['single_model']['pass_rate'], 1.0)
        self.assertLess(report['tiered']['quota_percent_total'], report['single_model']['quota_percent_total'])
        self.assertGreater(report['quota_saved_percent'], 0)
        self.assertTrue(report['beats_single_model'])


if __name__ == '__main__':
    unittest.main()
