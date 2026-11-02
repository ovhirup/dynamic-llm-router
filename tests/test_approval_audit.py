"""Tests for approval audit log (who/when/what) to job log and Obsidian vault (#85)."""
import json
from pathlib import Path
import unittest

from llm_router import pr_gate
from test_job_dispatch import JobTests


class ApprovalAuditLogTests(unittest.TestCase):
    setUp = JobTests.setUp
    tearDown = JobTests.tearDown
    select = JobTests.select
    worker = JobTests.worker
    job = JobTests.job

    def test_approve_and_deny_write_who_when_what_to_job_log_and_vault(self):
        self.policy['run_job']['vault_dir'] = str(self.base / 'vault')
        self.policy['pr_gate'] = {
            'approvals_file': str(self.base / 'approvals.jsonl'),
            'gate_dir': str(self.base / 'gate'),
            'reviews_dir': str(self.base / 'reviews'),
        }
        self.worker("open('out.txt','w').write('ok\\n')")
        done = self.job(test_cmd='test -f out.txt')
        self.assertTrue(done['ok'], done)
        job_id = done['job_id']

        pr_gate.review(job_id, policy=self.policy)
        secret = 'sk-' + 's' * 30
        pr_gate.approve(job_id, 'push', sha=done['commit'][:12], note=f'LGTM {secret}', policy=self.policy)
        pr_gate.deny(job_id, 'merge', note='Wait for CI', policy=self.policy)

        job_json = json.loads((self.base / 'jobs' / f'{job_id}.json').read_text())
        audit = job_json.get('approval_audit') or []
        self.assertEqual(len(audit), 2)
        self.assertEqual(audit[0]['who'], 'owner')
        self.assertEqual(audit[0]['what']['type'], 'approval')
        self.assertEqual(audit[0]['what']['action'], 'push')
        self.assertEqual(audit[0]['what']['sha'], done['commit'])
        self.assertNotIn(secret, json.dumps(audit))
        self.assertEqual(audit[1]['what']['type'], 'denial')
        self.assertEqual(audit[1]['what']['action'], 'merge')

        vault_global = (self.base / 'vault' / 'approvals-audit.md').read_text()
        vault_job = (self.base / 'vault' / 'jobs' / f'{job_id}-approvals.md').read_text()
        for text in (vault_global, vault_job):
            self.assertIn('who: `owner`', text)
            self.assertIn('`approval` `push`', text)
            self.assertIn('`denial` `merge`', text)
            self.assertNotIn(secret, text)


if __name__ == '__main__':
    unittest.main()
