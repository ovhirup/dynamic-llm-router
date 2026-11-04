"""Tests for undo window, expiry window, and stale-approval handling (#89)."""
from datetime import datetime, timedelta, timezone
import unittest

from llm_router import pr_gate
from test_job_dispatch import (
    git,
    job_setup,
    job_teardown,
    job_select,
    job_worker,
    run_job_fixture,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


class UndoAndStaleApprovalTests(unittest.TestCase):
    setUp = job_setup
    tearDown = job_teardown
    select = job_select
    worker = job_worker
    job = run_job_fixture

    def setUp(self):
        job_setup(self)
        self.policy['pr_gate'] = {
            'approvals_file': str(self.base / 'approvals.jsonl'),
            'gate_dir': str(self.base / 'gate'),
            'reviews_dir': str(self.base / 'reviews'),
            'approval_ttl_hours': 24,
            'undo_window_seconds': 300,
        }
        self.worker("open('out.txt','w').write('ok\\n')", 'grok-build')
        self.done = self.job(test_cmd='test -f out.txt', worker='grok-build')
        self.job_id = self.done['job_id']
        pr_gate.review(self.job_id, policy=self.policy, now=NOW)

    def test_undo_within_window_revokes_approval_and_blocks_push(self):
        rec = pr_gate.approve(self.job_id, 'push', sha=self.done['commit'][:12],
                              policy=self.policy, now=NOW, undo_seconds=300)
        self.assertIn('undo_until', rec)
        status_ok = pr_gate.approval_status(self.policy, self.job_id, 'push', self.done['commit'],
                                            now=NOW + timedelta(seconds=60))
        self.assertTrue(status_ok['ok'])
        self.assertTrue(status_ok['can_undo'])

        undone = pr_gate.undo(self.job_id, 'push', note='accidental tap',
                              policy=self.policy, now=NOW + timedelta(seconds=60))
        self.assertEqual(undone['type'], 'undone')

        status_after = pr_gate.approval_status(self.policy, self.job_id, 'push', self.done['commit'],
                                               now=NOW + timedelta(seconds=61))
        self.assertFalse(status_after['ok'])
        self.assertIn('was undone', status_after['why'])

    def test_undo_after_window_is_refused(self):
        pr_gate.approve(self.job_id, 'push', sha=self.done['commit'][:12],
                        policy=self.policy, now=NOW, undo_seconds=60)
        with self.assertRaisesRegex(pr_gate.Refused, 'undo window for push approval expired'):
            pr_gate.undo(self.job_id, 'push', policy=self.policy, now=NOW + timedelta(seconds=120))

    def test_stale_approval_on_branch_move_or_expiry_is_flagged_and_refused(self):
        pr_gate.approve(self.job_id, 'push', sha=self.done['commit'][:12],
                        hours=1, policy=self.policy, now=NOW)
        # 1. Expired approval -> stale + expired
        expired = pr_gate.approval_status(self.policy, self.job_id, 'push', self.done['commit'],
                                          now=NOW + timedelta(hours=2))
        self.assertFalse(expired['ok'])
        self.assertTrue(expired['stale'])
        self.assertTrue(expired['expired'])

        # 2. Branch head moves -> approval for old SHA is flagged stale
        wt = self.done['worktree']
        git(wt, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
            'commit', '--allow-empty', '-m', 'second commit')
        new_head = git(wt, 'rev-parse', 'HEAD')
        self.assertNotEqual(new_head, self.done['commit'])
        stale = pr_gate.approval_status(self.policy, self.job_id, 'push', new_head,
                                        now=NOW + timedelta(minutes=5))
        self.assertFalse(stale['ok'])
        self.assertTrue(stale['stale'])
        self.assertIn('older commit', stale['why'])


if __name__ == '__main__':
    unittest.main()
