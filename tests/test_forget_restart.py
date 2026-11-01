"""Tests for forget-and-restart policy for long retry loops (#100)."""
import unittest

from llm_router import forget_restart
from test_job_dispatch import JobTests


class ForgetAndRestartTests(unittest.TestCase):
    setUp = JobTests.setUp
    tearDown = JobTests.tearDown
    select = JobTests.select
    worker = JobTests.worker
    job = JobTests.job

    def test_tracker_picks_highest_scoring_candidate_and_discards_history(self):
        tracker = forget_restart.CandidateTracker(forget_after=2)
        tracker.record(
            1, 'codex', '+a = 1\n', changed_paths=['a.py'],
            tests={'status': 'failed', 'tail': '8 passed, 2 failed'},
        )
        tracker.record(
            2, 'grok-build', '+a = 999\n', changed_paths=['a.py'],
            tests={'status': 'failed', 'tail': '1 passed, 9 failed'},
        )
        self.assertFalse(tracker.should_forget(1))
        self.assertTrue(tracker.should_forget(2))
        best = tracker.best()
        self.assertEqual(best['attempt'], 1)
        self.assertEqual(best['worker'], 'codex')
        self.assertGreater(best['score'], 0.7)

        seed_prompt = tracker.build_fresh_seed_prompt()
        self.assertIn('Fresh Restart from Best Candidate (History Discarded)', seed_prompt)
        self.assertIn('attempt 1 (`codex`)', seed_prompt)
        self.assertIn('+a = 1', seed_prompt)
        self.assertNotIn('+a = 999', seed_prompt)

    def test_run_job_triggers_forget_and_restart_after_configured_attempts(self):
        self.policy['run_job']['max_retries'] = 2
        self.policy['run_job']['forget_after_attempts'] = 2
        capture = self.base / 'prompts.txt'
        self.worker(
            "import sys; p=sys.stdin.read(); open(%r,'a').write(p+'\\n=====\\n'); "
            "open('n.txt','a').write('x\\n')" % str(capture)
        )
        result = self.job(test_cmd='echo "1 passed, 1 failed"; test $(wc -l < n.txt) -ge 3')
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['attempts']), 3)
        self.assertEqual(len(result['forget_restarts']), 1)
        self.assertEqual(result['forget_restarts'][0]['after_attempt'], 2)
        third_prompt = capture.read_text().split('=====')[2]
        self.assertIn('Fresh Restart from Best Candidate (History Discarded)', third_prompt)
        self.assertNotIn('Previous attempt did not pass', third_prompt)


if __name__ == '__main__':
    unittest.main()
