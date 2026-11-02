"""Tests for worker quality drift detection (#70)."""
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import alerts, drift


class DriftDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.policy = load_policy()
        self.policy['alerts'] = {
            'file': str(base / 'alerts.jsonl'),
            'summary': str(base / 'ALERTS.md'),
            'macos_notification': False,
        }

    def test_steady_worker_has_no_drift(self):
        history = [1.0, 0.9, 1.0, 0.95, 0.9, 0.9, 1.0, 0.85, 0.9, 0.95]
        report = drift.detect_drift('claude', history, policy=self.policy, task_class='feature')
        self.assertFalse(report['drifted'])
        self.assertEqual(report['multiplier'], 1.0)
        self.assertEqual(alerts.open_alerts(self.policy), [])

    def test_sharp_drop_from_baseline_flags_drift_and_raises_alert(self):
        # 6 strong baseline runs (mean 0.95), then 5 degraded runs (mean 0.4) -> drop 0.55 >= 0.25
        history = [0.95, 0.95, 0.95, 0.95, 0.95, 0.95, 0.4, 0.4, 0.4, 0.4, 0.4]
        report = drift.detect_drift('codex', history, policy=self.policy, task_class='bugfix')
        self.assertTrue(report['drifted'])
        self.assertIn('quality_drop', report['reason'])
        self.assertEqual(report['multiplier'], drift.DEMOTION_FACTOR)
        open_alerts = alerts.open_alerts(self.policy)
        self.assertEqual(len(open_alerts), 1)
        self.assertIn('codex:bugfix', open_alerts[0]['message'])

    def test_below_quality_floor_without_long_baseline_flags_drift(self):
        history = [{'passed': False}, {'passed': False}, {'passed': True}, {'passed': False}, {'passed': False}]
        report = drift.detect_drift('cursor', history, policy=self.policy)
        self.assertTrue(report['drifted'])
        self.assertIn('below_quality_floor', report['reason'])
        self.assertEqual(report['recent_mean'], 0.2)


if __name__ == '__main__':
    unittest.main()
