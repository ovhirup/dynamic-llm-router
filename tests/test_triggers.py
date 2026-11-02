"""Tests for proactive event triggers (#132): allowlists, deduplication, and `llm-router triggers status`."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import alerts, cli, triggers


class EventTriggersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.policy = load_policy()
        self.policy['triggers'] = {
            'enabled': True,
            'mode': 'webhook',
            'allowed_authors': ['ovhirup'],
            'allowed_labels': ['dlr'],
            'state_file': str(self.base / 'triggers_seen.json'),
            'tasks_dir': str(self.base / 'tasks'),
        }
        self.policy['alerts'] = {
            'file': str(self.base / 'alerts.jsonl'),
            'summary': str(self.base / 'ALERTS.md'),
            'macos_notification': False,
        }

    def test_allowlist_blocks_untrusted_authors_labels_and_main_branch(self):
        for bad_event, expected_reason in [
            ({'kind': 'issue_labeled', 'author': 'stranger', 'labels': ['dlr'], 'issue_number': 1},
             'author_not_allowlisted:stranger'),
            ({'kind': 'issue_labeled', 'author': 'ovhirup', 'labels': ['bug'], 'issue_number': 1},
             'label_not_allowlisted'),
            ({'kind': 'ci_failed', 'author': 'ovhirup', 'branch': 'main', 'run_id': 9},
             'branch_not_dlr_managed:main'),
            ({'kind': 'pr_comment', 'author': 'ovhirup', 'branch': 'cursor/12-fix', 'body': 'looks good'},
             'comment_does_not_mention_dlr'),
            ({'kind': 'pr_comment', 'author': 'ovhirup', 'branch': 'main', 'body': '@dlr please fix'},
             'never_run_on_main'),
        ]:
            with self.subTest(bad_event=bad_event):
                res = triggers.handle_event(self.policy, bad_event)
                self.assertFalse(res['dispatched'])
                self.assertEqual(res['reason'], expected_reason)

    def test_dedup_ensures_one_event_equals_one_job_across_all_three_triggers(self):
        calls = []

        def fake_dispatch(**kwargs):
            calls.append(kwargs)
            return {'job_id': f'job-{len(calls)}', 'status': 'committed', 'ok': True}

        events = [
            {'kind': 'issue_labeled', 'author': 'ovhirup', 'labels': ['dlr'],
             'issue_number': 42, 'title': 'Add feature X', 'body': 'Do X'},
            {'kind': 'ci_failed', 'author': 'ovhirup', 'branch': 'cursor/42-feature-x',
             'run_id': 1001, 'check_name': 'pytest', 'log_tail': 'AssertionError'},
            {'kind': 'pr_comment', 'author': 'ovhirup', 'branch': 'cursor/42-feature-x',
             'pr_number': 150, 'comment_id': 555, 'body': '@dlr also update docs'},
        ]
        for ev in events:
            first = triggers.handle_event(self.policy, ev, dispatch_fn=fake_dispatch)
            self.assertTrue(first['dispatched'], first)
            self.assertTrue(Path(first['task_file']).is_file())
            # Replay the exact same event: must be deduplicated
            dup = triggers.handle_event(self.policy, ev, dispatch_fn=fake_dispatch)
            self.assertFalse(dup['dispatched'])
            self.assertEqual(dup['reason'], 'duplicate_event')
            self.assertEqual(dup['job_id'], first['job_id'])

        self.assertEqual(len(calls), 3)
        self.assertEqual(alerts.open_alerts(self.policy), [])  # least-attention: quiet on success

    def test_cli_triggers_status_outputs_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['triggers', 'status'])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertIn('enabled', payload)
        self.assertIn('allowed_labels', payload)
        self.assertIn('seen_events', payload)


if __name__ == '__main__':
    unittest.main()
