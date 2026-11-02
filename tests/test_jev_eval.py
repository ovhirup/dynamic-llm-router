"""Tests for comparing Jev vs heuristic on DLR evals (#60)."""
import unittest

from llm_router import jev_eval


class JevVsHeuristicEvalTests(unittest.TestCase):
    def test_heuristic_scores_full_accuracy_on_benchmark_and_tie_keeps_heuristic(self):
        report = jev_eval.compare_jev_vs_heuristic()
        self.assertEqual(report['heuristic']['cases'], 5)
        self.assertEqual(report['heuristic']['class_accuracy'], 1.0)
        self.assertEqual(report['heuristic']['size_accuracy'], 1.0)
        # Identical performance never adopts paid Jev over free local heuristic
        self.assertFalse(report['adopt'])
        self.assertIn('keep heuristic', report['reasons'][0])

    def test_adopts_jev_only_when_strictly_better_and_rejects_regression(self):
        def calibrated_jev(task_text):
            base = jev_eval.default_heuristic_classifier(task_text)
            chosen = base['task_class']
            return dict(base, confidence=0.98, probabilities={chosen: 0.98})

        win_report = jev_eval.compare_jev_vs_heuristic(jev_fn=calibrated_jev)
        self.assertTrue(win_report['adopt'])
        self.assertIn('better probability calibration', win_report['reasons'][0])

        def worse_jev(task_text):
            return {'task_class': 'feature', 'size': 'full', 'confidence': 0.5, 'probabilities': {'feature': 0.5}}

        loss_report = jev_eval.compare_jev_vs_heuristic(jev_fn=worse_jev)
        self.assertFalse(loss_report['adopt'])
        self.assertIn('class accuracy regressed', loss_report['reasons'])


if __name__ == '__main__':
    unittest.main()
