"""Tests for adaptive routing weights (#69): UCB1 exploration and safety floor."""
from datetime import datetime, timezone
import unittest

from helpers import load_policy
from llm_router import adaptive, scheduler

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


class AdaptiveWeightTests(unittest.TestCase):
    def test_under_sampled_worker_gets_exploration_bonus(self):
        veteran = adaptive.compute_weight(prior=0.8, runs=20, passed=16, total_runs=21)
        newcomer = adaptive.compute_weight(prior=0.8, runs=1, passed=1, total_runs=21)
        self.assertGreater(newcomer['exploration_bonus'], veteran['exploration_bonus'])
        self.assertGreater(newcomer['weight'], veteran['weight'])
        self.assertFalse(newcomer['floored'])

    def test_safety_floor_caps_degraded_worker_and_suppresses_bonus(self):
        degraded = adaptive.compute_weight(prior=0.9, runs=5, passed=0, total_runs=25, safety_floor=0.25)
        self.assertTrue(degraded['floored'])
        self.assertEqual(degraded['weight'], 0.25)
        self.assertEqual(degraded['exploration_bonus'], 0.0)
        self.assertEqual(degraded['empirical_pass_rate'], 0.0)

    def test_rank_workers_demotes_floored_and_keeps_grok_bot_last(self):
        ranked = adaptive.rank_workers(
            ['codex', 'claude', 'cursor', 'grok-bot'],
            priors={'codex': 0.9, 'claude': 0.9, 'cursor': 0.8},
            stats={
                'codex': {'runs': 6, 'passed': 1},   # 16.7% < 0.25 -> floored
                'claude': {'runs': 6, 'passed': 6},  # 100%
                'cursor': {'runs': 2, 'passed': 2},  # 100% + exploration bonus
            },
        )
        self.assertEqual(ranked['order'][-1], 'grok-bot')
        self.assertEqual(ranked['order'][-2], 'codex')
        self.assertTrue(ranked['weights']['codex']['floored'])
        self.assertIn(ranked['order'][0], ('claude', 'cursor'))

    def test_scheduler_assign_uses_adaptive_weights_when_enabled(self):
        from test_scheduler import evidence
        policy = load_policy(
            worker_order=['codex', 'claude'],
            quota={'codex': {'usage_threshold_percent': 80}, 'claude': {'usage_threshold_percent': 80}},
            scheduler={
                'capabilities': {'codex': {'feature': 0.9}, 'claude': {'feature': 0.85}},
                'adaptive': {
                    'enabled': True,
                    'safety_floor': 0.25,
                    'stats': {
                        'feature': {
                            'codex': {'runs': 5, 'passed': 0},
                            'claude': {'runs': 5, 'passed': 5},
                        }
                    },
                },
            },
        )
        ev = {'codex': evidence('codex', 10), 'claude': evidence('claude', 10)}
        decision = scheduler.assign(policy, 'feature', ev, NOW)
        self.assertEqual(decision['worker'], 'claude')
        self.assertEqual(decision['scores']['codex']['capability'], 0.25)
        self.assertGreater(decision['scores']['claude']['capability'], 0.9)


if __name__ == '__main__':
    unittest.main()
