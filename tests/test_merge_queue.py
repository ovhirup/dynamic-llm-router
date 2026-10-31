import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import load_policy
from llm_router import cli, job_dispatch as jd, merge_queue as mq, pr_gate, task_dag
from llm_router import unit_worktrees as uw
from test_task_dispatch import repo
from test_unit_worktrees import commit, git

UNITS = [
    {'id': 'api', 'prompt': 'do api', 'scope': ['src/api/']},
    {'id': 'ui', 'prompt': 'do ui', 'scope': ['src/ui/', 'README.md']},
]


class MergeQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.base = base
        self.repo = repo(base / 'main-repo').resolve()
        policy = copy.deepcopy(load_policy())
        policy['run_job'].update(worktree_root=str(base / 'wt'), lock_dir=str(base / 'locks'),
                                 jobs_dir=str(base / 'jobs'), units_dir=str(base / 'units'),
                                 merge_queue_dir=str(base / 'mq'), repo_config=str(base / 'repos.json'))
        self.policy = policy
        self.set_test_cmd('true')
        self.units = task_dag.parse_units(UNITS)

    def tearDown(self):
        self.tmp.cleanup()

    def set_test_cmd(self, command):
        (self.base / 'repos.json').write_text(json.dumps({str(self.repo): {'test_cmd': command}}))

    def create(self, assign):
        manifest = uw.create_batch(self.repo, self.units, assign, policy=self.policy, batch_id='b1')
        return {u['id']: Path(u['worktree']) for u in manifest['units']}

    def queue(self):
        return mq.queue_batch('b1', policy=self.policy)

    def entry(self, result, unit):
        return next(e for e in result['entries'] if e['unit'] == unit)

    def mkfile(self, wt, rel, text='x\n'):
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        commit(wt, rel, text)

    def test_claude_in_scope_passing_tests_is_ready_for_human(self):
        wts = self.create({'api': 'claude', 'ui': 'claude'})
        before = jd.main_state(self.repo)
        self.mkfile(wts['api'], 'src/api/a.py')
        with mock.patch.object(pr_gate, 'push', create=True) as push, \
                mock.patch.object(pr_gate, 'merge', create=True) as merge:
            result = self.queue()
        push.assert_not_called()
        merge.assert_not_called()
        self.assertEqual(jd.main_state(self.repo), before)
        self.assertEqual(result['evaluated'], ['api'])  # ui has no commits: not finished
        e = self.entry(result, 'api')
        self.assertEqual(e['status'], 'ready_for_human')
        self.assertEqual(e['tests']['status'], 'passed')
        self.assertEqual(e['tests']['command'], 'true')
        self.assertEqual(e['diff_scope'], {'status': 'ok', 'out_of_scope': [], 'changed': ['src/api/a.py']})
        self.assertFalse(e['review_required'])
        self.assertEqual(e['review'], {'status': 'skipped'})
        self.assertEqual(e['reasons'], [])
        for key in ('batch_id', 'unit', 'worker', 'branch', 'base_sha', 'head', 'scope', 'evaluated_at'):
            self.assertIn(key, e)
        self.assertTrue(result['ok'])

    def test_codex_needs_review_until_approved(self):
        wts = self.create({'api': 'codex', 'ui': 'grok-build'})
        self.mkfile(wts['api'], 'src/api/a.py')
        self.mkfile(wts['ui'], 'README.md', 'new\n')
        result = self.queue()
        api, ui = self.entry(result, 'api'), self.entry(result, 'ui')
        self.assertTrue(api['review_required'])
        self.assertEqual(api['status'], 'needs_review')
        self.assertEqual(api['reasons'], ['review_pending'])
        self.assertFalse(ui['review_required'])
        self.assertEqual(ui['status'], 'ready_for_human')
        self.assertFalse(result['ok'])
        with self.assertRaises(jd.Refused):
            mq.record_review('b1', 'api', 'maybe', policy=self.policy)
        with self.assertRaises(jd.Refused):
            mq.record_review('b1', 'ui', 'approve', policy=self.policy)
        rec = mq.record_review('b1', 'api', 'approve', notes='lgtm', policy=self.policy)
        self.assertEqual(rec['status'], 'ready_for_human')
        self.assertEqual(rec['review']['verdict'], 'approve')
        status = mq.queue_status('b1', policy=self.policy)['queues'][0]
        self.assertEqual(self.entry(status, 'api')['status'], 'ready_for_human')
        self.assertEqual(self.queue()['evaluated'], [])  # same heads: not re-evaluated, review kept
        rejected = mq.record_review('b1', 'api', 'reject', policy=self.policy)
        self.assertEqual((rejected['status'], rejected['reasons']), ('blocked', ['review_rejected']))

    def test_review_required_workers(self):
        for worker in ('codex', 'cursor', 'local', 'aider-x'):
            self.assertTrue(mq.review_required(worker))
        for worker in ('claude', 'grok-build'):
            self.assertFalse(mq.review_required(worker))

    def test_out_of_scope_blocks(self):
        wts = self.create({'api': 'claude', 'ui': 'claude'})
        self.mkfile(wts['api'], 'src/api/a.py')
        self.mkfile(wts['api'], 'src/ui/sneaky.py')
        e = self.entry(self.queue(), 'api')
        self.assertEqual(e['status'], 'blocked')
        self.assertIn('diff_out_of_scope', e['reasons'])
        self.assertEqual(e['diff_scope']['status'], 'fail')
        self.assertEqual(e['diff_scope']['out_of_scope'], ['src/ui/sneaky.py'])

    def test_scope_helper(self):
        self.assertEqual(mq.out_of_scope({'src/api', 'src/apix/a', 'README.md', 'src/api/b/c'},
                                         ['src/api/', 'README.md']), ['src/apix/a'])
        self.assertEqual(mq.out_of_scope({'README.md.bak', 'src/a'}, ['README.md', 'src']), ['README.md.bak', 'src/a'])

    def test_failing_tests_block(self):
        self.set_test_cmd('exit 3')
        wts = self.create({'api': 'claude', 'ui': 'claude'})
        self.mkfile(wts['api'], 'src/api/a.py')
        result = self.queue()
        e = self.entry(result, 'api')
        self.assertEqual((e['status'], e['tests']['status']), ('blocked', 'failed'))
        self.assertIn('tests_failed', e['reasons'])
        self.assertFalse(result['ok'])

    def test_no_tests_never_counts_as_pass(self):
        (self.base / 'repos.json').write_text('{}')
        wts = self.create({'api': 'claude', 'ui': 'claude'})
        self.mkfile(wts['api'], 'src/api/a.py')
        with mock.patch.object(mq, 'detect_test_cmd', return_value=None):
            e = self.entry(self.queue(), 'api')
        self.assertEqual((e['status'], e['tests']['status']), ('blocked', 'no_tests'))

    def test_missing_worktree_is_refused(self):
        wts = self.create({'api': 'claude', 'ui': 'claude'})
        self.mkfile(wts['api'], 'src/api/a.py')
        git(self.repo, 'worktree', 'remove', '--force', str(wts['api']))
        e = self.entry(self.queue(), 'api')
        self.assertEqual(e['status'], 'blocked')
        self.assertIn('worktree_missing', e['reasons'])
        self.assertEqual(e['tests']['status'], 'refused')
        self.assertFalse(wts['api'].exists())  # no checkout invented

    def test_persistence_and_head_change_reevaluates(self):
        wts = self.create({'api': 'codex', 'ui': 'claude'})
        self.mkfile(wts['api'], 'src/api/a.py')
        first = self.entry(self.queue(), 'api')
        mq.record_review('b1', 'api', 'approve', policy=self.policy)
        path = self.base / 'mq' / 'b1.json'
        saved = json.loads(path.read_text())
        saved['entries']['api']['human_decision'] = {'decision': 'yes', 'by': 'human'}
        path.write_text(json.dumps(saved))
        again = self.queue()
        self.assertEqual(again['evaluated'], [])
        self.assertEqual(self.entry(again, 'api')['status'], 'ready_for_human')
        self.mkfile(wts['api'], 'src/api/b.py')
        changed = self.queue()
        self.assertEqual(changed['evaluated'], ['api'])
        e = self.entry(changed, 'api')
        self.assertNotEqual(e['head'], first['head'])
        self.assertEqual(e['status'], 'needs_review')  # review was for the old head
        self.assertEqual(e['human_decision'], {'decision': 'yes', 'by': 'human'})
        self.assertEqual(json.loads(path.read_text())['entries']['api']['head'], e['head'])

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(cli.config, 'load_policy', return_value=self.policy):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_cli_queue_and_status(self):
        wts = self.create({'api': 'claude', 'ui': 'codex'})
        self.mkfile(wts['api'], 'src/api/a.py')
        code, result = self.run_cli('units', 'queue', '--batch', 'b1')
        self.assertEqual(code, 0)
        self.assertEqual(self.entry(result, 'api')['status'], 'ready_for_human')
        self.mkfile(wts['ui'], 'src/ui/u.py')
        code, result = self.run_cli('units', 'queue', '--batch', 'b1')
        self.assertEqual(code, 1)  # ui needs review
        code, status = self.run_cli('units', 'queue-status')
        self.assertEqual(code, 0)
        self.assertEqual(len(status['queues'][0]['entries']), 2)
        code, refused = self.run_cli('units', 'queue', '--batch', 'nope')
        self.assertEqual(code, 3)
        self.assertFalse(refused['ok'])

    def test_module_never_pushes_or_merges(self):
        source = Path(mq.__file__).read_text()
        for needle in ("'push'", "'merge'", 'gh ', 'pr_gate'):
            self.assertNotIn(needle, source)


if __name__ == '__main__':
    unittest.main()
