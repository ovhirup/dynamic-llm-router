import json
import tempfile
import unittest
from pathlib import Path

from llm_router import feedback, merge_queue as mq
import test_job_dispatch as tjd
import test_merge_queue as tmq

# Reuse the job / merge-queue fixtures without subclassing, so their own tests are not re-run here.
JT, MQ = tjd.JobTests, tmq.MergeQueueTests


class JobFeedbackTests(unittest.TestCase):
    """run_job feeds every finished job into <scorecards_dir>/<worker>.json."""
    tearDown, select, worker, job = JT.tearDown, JT.select, JT.worker, JT.job

    def setUp(self):
        JT.setUp(self)
        self.policy['feedback'] = {'scorecards_dir': str(self.base / 'scorecards')}

    def card(self, worker):
        return json.loads((self.base / 'scorecards' / f'{worker}.json').read_text())

    def test_pass_updates_scorecard(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='test -f out.txt', task_class='feature')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['task_class'], 'feature')
        card = self.card('codex')
        self.assertEqual((card['runs'], card['passed'], card['pass_rate']), (1, 1, 1.0))
        for key in ('tokens_total', 'tokens_mean', 'elapsed_seconds_mean', 'review_rejections', 'updated_at'):
            self.assertIn(key, card)
        self.assertGreaterEqual(card['elapsed_seconds_mean'], 0)
        self.assertEqual(card['classes']['feature']['passed'], 1)
        self.assertEqual(feedback.load_scorecards(self.policy)['codex']['runs'], 1)

    def test_fail_updates_scorecard(self):
        self.worker("pass")  # writes nothing, so the tests fail on every attempt
        result = self.job(test_cmd='test -f out.txt', worker='codex')
        self.assertFalse(result['ok'], result)
        card = self.card('codex')
        self.assertEqual((card['runs'], card['passed'], card['pass_rate']), (1, 0, 0.0))
        self.assertIn('feature', card['classes'])  # default class when none is given

    def test_pass_then_fail_rolls_pass_rate(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        self.job(test_cmd='test -f out.txt', worker='codex')
        self.worker("pass")
        self.job(test_cmd='test -f out.txt', worker='codex')
        card = self.card('codex')
        self.assertEqual((card['runs'], card['passed'], card['pass_rate']), (2, 1, 0.5))


class ReviewFeedbackTests(unittest.TestCase):
    """merge_queue.record_review feeds approve/reject verdicts into the reviewed worker's scorecard."""
    tearDown, set_test_cmd, create, queue, mkfile = MQ.tearDown, MQ.set_test_cmd, MQ.create, MQ.queue, MQ.mkfile

    def setUp(self):
        MQ.setUp(self)
        self.policy['feedback'] = {'scorecards_dir': str(self.base / 'scorecards')}

    def test_review_rejection_and_approval_update_scorecard(self):
        wts = self.create({'api': 'codex', 'ui': 'grok-build'})
        self.mkfile(wts['api'], 'src/api/a.py')
        self.queue()
        mq.record_review('b1', 'api', 'reject', policy=self.policy)
        card = feedback.load_scorecards(self.policy)['codex']
        self.assertEqual((card['reviews'], card['review_rejections'], card['review_approvals']), (1, 1, 0))
        self.assertEqual(card['classes']['feature']['review_rejections'], 1)
        self.assertEqual(card['runs'], 0)  # a review is not a run
        mq.record_review('b1', 'api', 'approve', policy=self.policy)
        card = json.loads((self.base / 'scorecards' / 'codex.json').read_text())
        self.assertEqual((card['reviews'], card['review_rejections'], card['review_approvals']), (2, 1, 1))


class SummaryFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.policy = {'feedback': {'scorecards_dir': str(Path(self.tmp.name) / 'sc')}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_failover_and_review_rejections_from_summary(self):
        summary = {'ok': True, 'workers': ['codex', 'grok-build'], 'task_class': 'bugfix', 'review_rejections': 2,
                   'attempts': [{'attempt': 1, 'runs': [{'worker': 'codex', 'tokens': 100},
                                                        {'worker': 'grok-build', 'tokens': 50}]}],
                   'hops': [{'worker': 'codex', 'elapsed_seconds': 4, 'unavailable': False},
                            {'worker': 'grok-build', 'elapsed_seconds': 6, 'unavailable': False}]}
        feedback.record_job_feedback(self.policy, summary)
        cards = feedback.load_scorecards(self.policy)
        self.assertEqual((cards['codex']['passed'], cards['codex']['tokens_total']), (0, 100))
        grok = cards['grok-build']['classes']['bugfix']
        self.assertEqual((grok['passed'], grok['tokens_mean'], grok['elapsed_seconds_mean'], grok['review_rejections']),
                         (1, 50.0, 6.0, 2))

    def test_quota_only_hop_is_not_scored(self):
        summary = {'ok': True, 'workers': ['codex', 'cursor'],
                   'hops': [{'worker': 'codex', 'unavailable': True}, {'worker': 'cursor', 'unavailable': False}]}
        feedback.record_job_feedback(self.policy, summary, task_class='mechanical')
        cards = feedback.load_scorecards(self.policy)
        self.assertNotIn('codex', cards)
        self.assertIn('mechanical', cards['cursor']['classes'])


if __name__ == '__main__':
    unittest.main()
