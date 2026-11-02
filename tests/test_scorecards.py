"""Worker scorecards per task class feed scheduler.capability."""
import json
import tempfile
import unittest
from pathlib import Path

from helpers import load_policy
from llm_router import scheduler, scorecards


class ScorecardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'scorecards'
        self.policy = load_policy(run_job={'scorecards_dir': str(self.dir)})

    def test_scorecards_dir_uses_policy_path(self):
        self.assertEqual(scorecards.scorecards_dir(self.policy), self.dir)
        default = load_policy()
        self.assertEqual(scorecards.scorecards_dir(default).parts[-2:], ('vault', 'scorecards'))

    def test_unknown_worker_has_empty_scorecard_and_no_score(self):
        self.assertEqual(scorecards.load_scorecard(self.policy, 'codex'), {})
        self.assertIsNone(scorecards.class_score(self.policy, 'codex', 'code'))

    def test_records_outcomes_across_workers_and_classes(self):
        scorecards.record_outcome(self.policy, 'codex', 'code', True, tokens=100, elapsed_seconds=10)
        scorecards.record_outcome(self.policy, 'codex', 'code', False, tokens=300, review_fixes=2)
        scorecards.record_outcome(self.policy, 'codex', 'docs', True)
        scorecards.record_outcome(self.policy, 'claude', 'code', True, elapsed_seconds=4)

        code = scorecards.load_scorecard(self.policy, 'codex')['classes']['code']
        self.assertEqual((code['runs'], code['passed'], code['review_fixes']), (2, 1, 2))
        self.assertEqual(code['pass_rate'], 0.5)
        self.assertEqual(code['tokens_mean'], 200)
        self.assertEqual(code['elapsed_seconds_mean'], 10)
        self.assertEqual(scorecards.class_score(self.policy, 'codex', 'code'), 0.5)
        self.assertEqual(scorecards.class_score(self.policy, 'codex', 'docs'), 1.0)
        self.assertEqual(scorecards.class_score(self.policy, 'claude', 'code'), 1.0)
        self.assertIsNone(scorecards.class_score(self.policy, 'claude', 'docs'))

    def test_persists_json_and_markdown(self):
        scorecards.record_outcome(self.policy, 'codex', 'code', True, tokens=50, elapsed_seconds=2.5)
        data = json.loads((self.dir / 'codex.json').read_text())
        self.assertEqual(data['worker'], 'codex')
        self.assertEqual(data['classes']['code']['runs'], 1)
        md = (self.dir / 'codex.md').read_text()
        self.assertIn('| task_class | runs | passed | pass_rate | review_fixes | tokens_mean | '
                      'elapsed_seconds_mean |', md)
        self.assertIn('| code | 1 | 1 | 1.00 | 0 | 50 | 2.50 |', md)

    def test_capability_reads_empirical_pass_rate(self):
        for passed in (True, True, True, False):
            scorecards.record_outcome(self.policy, 'codex', 'code', passed)
        self.assertEqual(scheduler.capability(self.policy, 'codex', 'code'), 0.75)
        self.assertEqual(scheduler.capability(self.policy, 'codex', 'docs'), 1.0)

    def test_explicit_capability_overrides_scorecard(self):
        scorecards.record_outcome(self.policy, 'codex', 'code', False)
        p = load_policy(run_job={'scorecards_dir': str(self.dir)},
                        scheduler={'capabilities': {'codex': {'code': 0.9}}})
        self.assertEqual(scheduler.capability(p, 'codex', 'code'), 0.9)
        self.assertEqual(scheduler.capability(self.policy, 'codex', 'code'), 0.0)


if __name__ == '__main__':
    unittest.main()
