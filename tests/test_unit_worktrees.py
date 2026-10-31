import copy
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import load_policy
from llm_router import cli, job_dispatch as jd, task_dag
from llm_router import unit_worktrees as uw
from test_task_dispatch import repo


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True).stdout.strip()


def commit(cwd, name, text='x\n'):
    (Path(cwd) / name).write_text(text)
    git(cwd, 'add', name)
    git(cwd, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', f'add {name}')


UNITS = [
    {'id': 'api', 'prompt': 'do api', 'scope': ['src/api/']},
    {'id': 'ui', 'prompt': 'do ui', 'scope': ['src/ui/']},
    {'id': 'docs', 'prompt': 'do docs', 'scope': ['docs/'], 'deps': ['api']},
]


class UnitWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.base = base
        self.repo = repo(base / 'main-repo').resolve()
        (self.repo / 'local-notes.txt').write_text('user work in progress\n')
        policy = copy.deepcopy(load_policy())
        policy['run_job'].update(worktree_root=str(base / 'wt'), lock_dir=str(base / 'locks'),
                                 jobs_dir=str(base / 'jobs'), units_dir=str(base / 'units'))
        self.policy = policy
        self.units = task_dag.parse_units(UNITS)
        self.assign = {'api': 'codex', 'ui': 'claude', 'docs': 'codex'}

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, **kw):
        kw.setdefault('policy', self.policy)
        return uw.create_batch(self.repo, kw.pop('units', self.units), kw.pop('assign', self.assign), **kw)

    def branches(self):
        return set(git(self.repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/dlr/').split())

    def test_create_one_worktree_per_unit_from_same_base(self):
        before = jd.main_state(self.repo)
        head = git(self.repo, 'rev-parse', 'HEAD')
        manifest = self.create(batch_id='b1')
        self.assertEqual(jd.main_state(self.repo), before)  # main checkout untouched
        self.assertEqual([u['id'] for u in manifest['units']], ['api', 'ui', 'docs'])  # wave order
        self.assertEqual([u['wave'] for u in manifest['units']], [0, 0, 1])
        self.assertEqual(manifest['base_sha'], head)
        root = self.base / 'wt' / 'units-b1'
        self.assertEqual(manifest['worktree_root'], str(root))
        paths = [u['worktree'] for u in manifest['units']]
        self.assertEqual(len(set(paths)), 3)
        for unit in manifest['units']:
            wt = Path(unit['worktree'])
            self.assertEqual(wt.parent, root)
            self.assertEqual(unit['base_sha'], head)
            self.assertEqual(git(wt, 'rev-parse', 'HEAD'), head)
            self.assertEqual(git(wt, 'symbolic-ref', '--short', 'HEAD'), unit['branch'])
        saved = json.loads((self.base / 'units' / 'b1.json').read_text())
        self.assertEqual(saved, manifest)
        for key in ('id', 'worker', 'wave', 'branch', 'worktree', 'base_sha'):
            self.assertIn(key, saved['units'][0])

    def test_branch_naming_and_explicit_base(self):
        first = git(self.repo, 'rev-parse', 'HEAD')
        commit(self.repo, 'later.txt')
        manifest = self.create(batch_id='b1', base=first)
        self.assertEqual({u['branch'] for u in manifest['units']},
                         {'dlr/api-codex', 'dlr/ui-claude', 'dlr/docs-codex'})
        self.assertTrue(all(u['base_sha'] == first for u in manifest['units']))

    def test_collision_adds_batch_suffix_and_never_overwrites(self):
        git(self.repo, 'branch', 'dlr/api-codex')
        old = git(self.repo, 'rev-parse', 'dlr/api-codex')
        commit(self.repo, 'later.txt')
        manifest = self.create(batch_id='b2')
        api = manifest['units'][0]
        self.assertEqual(api['branch'], 'dlr/api-codex-b2')
        self.assertEqual(git(self.repo, 'rev-parse', 'dlr/api-codex'), old)
        git(self.repo, 'branch', 'dlr/ui-claude-b3')
        git(self.repo, 'branch', 'dlr/ui-claude')
        with self.assertRaises(uw.Refused):
            self.create(batch_id='b3')
        self.assertFalse((self.base / 'units' / 'b3.json').exists())

    def test_refuses_grok_bot_and_unknown_workers(self):
        for worker in ('grok-bot', 'evil;rm', None):
            with self.subTest(worker=worker), self.assertRaises(uw.Refused):
                self.create(assign=dict(self.assign, ui=worker))
        self.assertEqual(self.branches(), set())
        with self.assertRaises(uw.Refused):
            uw.branch_name(self.repo, 'api', 'grok-bot', 'b1')

    def test_refuses_unsafe_unit_ids(self):
        for uid in ('../x', 'a b', 'A', '-x', 'a..b'):
            with self.subTest(uid=uid), self.assertRaises(uw.Refused):
                uw.branch_name(self.repo, uid, 'codex', 'b1')
        with self.assertRaises(uw.Refused):
            self.create(units=[{'id': '../evil', 'prompt': 'p', 'scope': ['a']}], assign={'../evil': 'codex'})

    def test_partial_failure_rolls_back(self):
        real = uw._git
        calls = []

        def flaky(cwd, *args, check=False):
            if args[:2] == ('worktree', 'add'):
                calls.append(args)
                if len(calls) == 3:
                    raise jd.Refused('git worktree failed: boom')
            return real(cwd, *args, check=check)

        before = jd.main_state(self.repo)
        with mock.patch.object(uw, '_git', flaky), self.assertRaises(jd.Refused):
            self.create(batch_id='b1')
        self.assertEqual(self.branches(), set())
        self.assertFalse((self.base / 'wt' / 'units-b1').exists())
        self.assertFalse((self.base / 'units' / 'b1.json').exists())
        self.assertNotIn('units-b1', git(self.repo, 'worktree', 'list'))
        self.assertEqual(jd.main_state(self.repo), before)

    def test_list_and_finished_units(self):
        manifest = self.create(batch_id='b1')
        listed = uw.list_batches(policy=self.policy, repo=self.repo)['batches']
        self.assertEqual([b['batch_id'] for b in listed], ['b1'])
        self.assertTrue(all(u['worktree_exists'] for u in listed[0]['units']))
        self.assertEqual(uw.list_batches(policy=self.policy, repo=self.base)['batches'], [])
        self.assertEqual(uw.finished_units('b1', self.policy), [])
        docs, api = Path(manifest['units'][2]['worktree']), Path(manifest['units'][0]['worktree'])
        commit(docs, 'd.txt')
        commit(api, 'a.txt')
        (Path(manifest['units'][1]['worktree']) / 'dirty.txt').write_text('wip')
        done = uw.finished_units('b1', self.policy)
        self.assertEqual([u['id'] for u in done], ['api', 'docs'])  # manifest order
        self.assertEqual(done[0]['head'], git(api, 'rev-parse', 'HEAD'))

    def test_remove_deletes_unworked_branches_keeps_committed(self):
        manifest = self.create(batch_id='b1')
        commit(Path(manifest['units'][0]['worktree']), 'a.txt')
        (Path(manifest['units'][1]['worktree']) / 'dirty.txt').write_text('wip')  # removed with --force
        report = uw.remove_batch(self.repo, 'b1', policy=self.policy)
        self.assertTrue(report['ok'])
        self.assertEqual({u['worktree_state'] for u in report['units']}, {'pruned'})
        self.assertEqual({u['id']: u['branch_state'] for u in report['units']},
                         {'api': 'kept', 'ui': 'deleted', 'docs': 'deleted'})
        self.assertEqual(self.branches(), {'dlr/api-codex'})
        self.assertFalse((self.base / 'wt' / 'units-b1').exists())
        self.assertEqual(json.loads((self.base / 'units' / 'b1.json').read_text())['state'], 'removed')
        again = uw.remove_batch(self.repo, 'b1', policy=self.policy)  # idempotent
        self.assertEqual({u['worktree_state'] for u in again['units']}, {'missing'})

    def test_remove_keep_and_all_branches(self):
        manifest = self.create(batch_id='b1')
        commit(Path(manifest['units'][0]['worktree']), 'a.txt')
        uw.remove_batch(self.repo, 'b1', keep_branches=True, policy=self.policy)
        self.assertEqual(len(self.branches()), 3)
        self.create(batch_id='b2')
        uw.remove_batch(self.repo, 'b2', all_branches=True, policy=self.policy)
        self.assertEqual(self.branches(), {'dlr/api-codex', 'dlr/ui-claude', 'dlr/docs-codex'})
        with self.assertRaises(uw.Refused):
            uw.remove_batch(self.base, 'b1', policy=self.policy)  # wrong repo
        with self.assertRaises(uw.Refused):
            uw.remove_batch(self.repo, '../b1', policy=self.policy)

    def test_cli_create_list_remove(self):
        units_file = self.base / 'units.json'
        units_file.write_text(json.dumps({'units': UNITS, 'assign': self.assign}))

        def run(*argv):
            out = io.StringIO()
            with redirect_stdout(out), mock.patch.object(cli.config, 'load_policy', return_value=self.policy):
                code = cli.main(list(argv))
            return code, json.loads(out.getvalue())

        code, made = run('units', 'create', '--repo', str(self.repo), '--units-file', str(units_file))
        self.assertEqual(code, 0)
        self.assertEqual(len(made['units']), 3)
        code, listed = run('units', 'list', '--repo', str(self.repo))
        self.assertEqual([b['batch_id'] for b in listed['batches']], [made['batch_id']])
        code, _ = run('units', 'remove', '--repo', str(self.repo), '--batch', made['batch_id'])
        self.assertEqual(code, 0)
        self.assertEqual(self.branches(), set())
        bad = self.base / 'bad.json'
        bad.write_text(json.dumps([dict(UNITS[0], workers=['grok-bot'])]))
        code, refused = run('units', 'create', '--repo', str(self.repo), '--units-file', str(bad))
        self.assertEqual(code, 3)
        self.assertIn('grok-bot', refused['refused'])


if __name__ == '__main__':
    unittest.main()
