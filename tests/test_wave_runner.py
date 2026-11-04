import copy
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import load_policy
from llm_router import cli, job_dispatch as jd, pr_gate, task_dispatch
from llm_router import unit_worktrees as uw
from llm_router import wave_runner as wr
from test_task_dispatch import repo
from test_unit_worktrees import commit

UNITS = [
    {'id': 'api', 'prompt': 'do api', 'scope': ['src/api/']},
    {'id': 'ui', 'prompt': 'do ui', 'scope': ['src/ui/']},
    {'id': 'docs', 'prompt': 'do docs', 'scope': ['docs/'], 'deps': ['api']},
]
FILES = {'api': 'src/api/a.py', 'ui': 'src/ui/u.js', 'docs': 'docs/d.md'}


class FakeRunner:
    """Commits one in-scope file per unit; never launches a real worker."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def __call__(self, entry, prompt, policy):
        self.calls.append((entry['id'], entry['worker'], prompt))
        if entry['id'] in self.fail:
            return {'ok': False, 'status': 'failed', 'reason': 'boom'}
        wt = Path(entry['worktree'])
        rel = FILES[entry['id']]
        (wt / rel).parent.mkdir(parents=True, exist_ok=True)
        commit(wt, rel)
        return {'ok': True, 'status': 'ok'}


class WaveRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.base = base
        self.repo = repo(base / 'main-repo').resolve()
        policy = copy.deepcopy(load_policy())
        policy['run_job'].update(worktree_root=str(base / 'wt'), lock_dir=str(base / 'locks'),
                                 jobs_dir=str(base / 'jobs'), units_dir=str(base / 'units'),
                                 merge_queue_dir=str(base / 'mq'), repo_config=str(base / 'repos.json'))
        (base / 'repos.json').write_text(json.dumps({str(self.repo): {'test_cmd': 'true'}}))
        self.policy = policy

    def tearDown(self):
        self.tmp.cleanup()

    def run_batch(self, **kwargs):
        kwargs.setdefault('unit_runner', FakeRunner())
        kwargs.setdefault('acquire_slot', lambda policy, worker: ('uncapped', None))
        return wr.run_batch(self.repo, UNITS, policy=self.policy, batch_id='b1', **kwargs)

    def test_two_waves_run_and_merge_queue_sees_finished_units(self):
        runner = FakeRunner()
        before = jd.main_state(self.repo)
        with mock.patch.object(pr_gate, 'push', create=True) as push, \
                mock.patch.object(pr_gate, 'merge', create=True) as merge:
            result = self.run_batch(assignment={'api': 'claude', 'ui': 'claude', 'docs': 'claude'},
                                    unit_runner=runner)
        push.assert_not_called()
        merge.assert_not_called()
        self.assertEqual(jd.main_state(self.repo), before)
        self.assertTrue(result['ok'])
        self.assertEqual(result['batch_id'], 'b1')
        self.assertEqual([[r['id'] for r in wave] for wave in result['waves']], [['api', 'ui'], ['docs']])
        self.assertEqual([r['wave'] for r in result['waves'][1]], [1])
        self.assertEqual([c[0] for c in runner.calls], ['api', 'ui', 'docs'])
        self.assertIn('src/api/', runner.calls[0][2])
        statuses = {e['unit']: e['status'] for e in result['queue']['entries']}
        self.assertEqual(statuses, {'api': 'ready_for_human', 'ui': 'ready_for_human', 'docs': 'ready_for_human'})
        self.assertEqual(result['queue']['evaluated'], ['docs'])  # api/ui were queued after wave 0
        self.assertEqual(result['deferred'], [])
        json.dumps(result)

    def test_shared_memory_reaches_claude_codex_and_cursor(self):
        shared_memory = (Path(__file__).resolve().parents[1] / 'AGENTS.md').read_text().strip()
        commit(self.repo, 'AGENTS.md', shared_memory + '\n')
        runner = FakeRunner()
        assignment = {'api': 'claude', 'ui': 'codex', 'docs': 'cursor'}

        result = self.run_batch(assignment=assignment, unit_runner=runner)

        self.assertTrue(result['ok'], result)
        self.assertEqual({unit: worker for unit, worker, _ in runner.calls}, assignment)
        for _, worker, prompt in runner.calls:
            with self.subTest(worker=worker):
                self.assertEqual(prompt.count(shared_memory), 1)

    def test_slot_full_defers_unit_and_wave_continues(self):
        def acquire(policy, worker):
            return ('full', None) if worker == 'codex' else ('uncapped', None)
        runner = FakeRunner()
        result = self.run_batch(assignment={'api': 'claude', 'ui': 'codex', 'docs': 'claude'},
                                unit_runner=runner, acquire_slot=acquire)
        self.assertTrue(result['ok'])
        wave0 = {r['id']: r for r in result['waves'][0]}
        self.assertEqual(wave0['ui']['status'], 'deferred_slot_full')
        self.assertTrue(wave0['api']['ok'])
        self.assertEqual([c[0] for c in runner.calls], ['api', 'docs'])
        self.assertIn('ui', [d['id'] for d in result['deferred']])

    def test_slot_handle_is_released_even_when_runner_raises(self):
        handle = mock.Mock()

        def boom(entry, prompt, policy):
            raise RuntimeError('worker crashed')
        result = self.run_batch(assignment={'api': 'claude', 'ui': 'claude', 'docs': 'claude'},
                                unit_runner=boom, acquire_slot=lambda p, w: ('held', handle))
        self.assertFalse(result['ok'])
        self.assertEqual(handle.close.call_count, 2)  # api + ui; docs blocked by failed api
        self.assertEqual(result['waves'][1][0]['status'], 'blocked_by_dependency')

    def test_real_acquire_slot_full_with_cap_zero(self):
        self.policy['run_task']['concurrency'] = {'claude': 0}
        self.policy['run_task']['slot_dir'] = str(self.base / 'slots')
        result = wr.run_batch(self.repo, UNITS, assignment={'api': 'claude', 'ui': 'codex', 'docs': 'codex'},
                              policy=self.policy, batch_id='b1', unit_runner=FakeRunner())
        self.assertEqual(result['waves'][0][0]['status'], 'deferred_slot_full')
        self.assertTrue(result['waves'][0][1]['ok'])

    def test_no_eligible_worker_defers_to_orchestrator_without_worktree(self):
        runner = FakeRunner()
        units = [dict(UNITS[0]), dict(UNITS[1], workers=['codex']), dict(UNITS[2], deps=[])]
        result = wr.run_batch(self.repo, units, policy=self.policy, batch_id='b1',
                              quota_left={'claude': 5}, unit_runner=runner,
                              acquire_slot=lambda p, w: ('uncapped', None))
        self.assertTrue(result['ok'])
        self.assertEqual([(d['id'], d['status']) for d in result['deferred']], [('ui', 'deferred_to_orchestrator')])
        self.assertIn('no parallel-pool worker', result['deferred'][0]['reason'])
        manifest = uw.load_manifest('b1', self.policy)
        self.assertEqual(sorted(u['id'] for u in manifest['units']), ['api', 'docs'])
        self.assertFalse((Path(manifest['worktree_root']) / 'ui').exists())
        self.assertNotIn('ui', [c[0] for c in runner.calls])

    def test_grok_bot_never_gets_a_worktree_or_run(self):
        runner = FakeRunner()
        result = self.run_batch(quota_left={'grok-bot': 5, 'codex': 1}, unit_runner=runner)
        workers = {c[1] for c in runner.calls}
        self.assertNotIn('grok-bot', workers)
        self.assertEqual([c[:2] for c in runner.calls], [('api', 'codex')])
        manifest = uw.load_manifest('b1', self.policy)
        self.assertNotIn('grok-bot', {u['worker'] for u in manifest['units']})
        deferred = {d['id']: d for d in result['deferred']}
        self.assertEqual(deferred['ui']['status'], 'deferred_to_orchestrator')
        self.assertEqual(deferred['ui']['worker'], 'grok-bot')
        # explicit grok-bot assignment is also deferred, never given a worktree
        result = wr.run_batch(self.repo, UNITS[:1], assignment={'api': 'grok-bot'}, policy=self.policy,
                              unit_runner=runner)
        self.assertIsNone(result['batch_id'])
        self.assertEqual(result['deferred'][0]['status'], 'deferred_to_orchestrator')

    def test_dependency_on_deferred_unit_blocks_dependent(self):
        result = self.run_batch(assign=lambda units, left: {'api': None, 'ui': 'claude', 'docs': 'claude'})
        self.assertTrue(result['ok'])
        docs = result['waves'][1][0]
        self.assertEqual((docs['id'], docs['status']), ('docs', 'blocked_by_dependency'))

    def test_runner_failure_flips_ok(self):
        result = self.run_batch(assignment={'api': 'claude', 'ui': 'claude', 'docs': 'claude'},
                                unit_runner=FakeRunner(fail={'ui'}))
        self.assertFalse(result['ok'])
        self.assertEqual({e['unit'] for e in result['queue']['entries']}, {'api', 'docs'})

    def test_default_runner_calls_run_task_on_worktree(self):
        manifest = uw.create_batch(self.repo, UNITS[:1], {'api': 'codex'}, policy=self.policy, batch_id='b1')
        entry = manifest['units'][0]
        with mock.patch.object(task_dispatch, 'run_task', return_value={'ok': True}) as run:
            out = wr.default_unit_runner(entry, 'prompt text', self.policy)
        self.assertTrue(out['ok'])
        args, kwargs = run.call_args
        self.assertEqual(Path(args[1]), Path(entry['worktree']))
        self.assertEqual(Path(args[0]).read_text(), 'prompt text')
        self.assertEqual((kwargs['worker'], kwargs['allow_dirty']), ('codex', True))
        self.assertEqual(jd._git(Path(entry['worktree']), 'status', '--porcelain').stdout, '')

    def test_cli_units_run(self):
        units_file = self.base / 'units.json'
        units_file.write_text(json.dumps({'units': UNITS, 'assign': {'api': 'claude', 'ui': 'claude',
                                                                      'docs': 'claude'}}))
        fake = FakeRunner()
        real = wr.run_batch
        with mock.patch.object(wr, 'run_batch', side_effect=lambda *a, **k: real(
                *a, unit_runner=fake, acquire_slot=lambda p, w: ('uncapped', None), **k)), \
                mock.patch('llm_router.config.load_policy', return_value=self.policy):
            out = io.StringIO()
            with redirect_stdout(out):
                code = cli.main(['units', 'run', '--repo', str(self.repo), '--units-file', str(units_file)])
        self.assertEqual(code, 0, out.getvalue())
        result = json.loads(out.getvalue())
        self.assertTrue(result['ok'])
        self.assertEqual(len(fake.calls), 3)
        fake.fail = {'ui'}
        with mock.patch.object(wr, 'run_batch', side_effect=lambda *a, **k: real(
                *a, unit_runner=fake, acquire_slot=lambda p, w: ('uncapped', None), **k)), \
                mock.patch('llm_router.config.load_policy', return_value=self.policy), redirect_stdout(io.StringIO()):
            assign_file = self.base / 'assign.json'
            assign_file.write_text(json.dumps({'api': 'codex', 'ui': 'codex', 'docs': 'codex'}))
            code = cli.main(['units', 'run', '--repo', str(self.repo), '--units-file', str(units_file),
                             '--assign-file', str(assign_file)])
        self.assertEqual(code, 1)

    def test_module_never_pushes_or_merges(self):
        source = Path(wr.__file__).read_text()
        self.assertIsNone(re.search(r'\b(push|merge)\s*\(|pr_gate|[\'"]push[\'"]|[\'"]merge[\'"]', source))

    def test_continuation_brief_written_and_read_and_skill_file_has_attribution(self):
        skill = Path(__file__).resolve().parents[1] / 'src/llm_router/templates/supervisor_skill.md'
        self.assertTrue(skill.is_file())
        self.assertIn('swyxio/skills', skill.read_text())
        self.assertIn('MIT', skill.read_text())

        result = self.run_batch(assignment={'api': 'claude', 'ui': 'claude', 'docs': 'claude'})
        brief = wr.read_continuation_brief(self.policy, 'b1')
        self.assertIsNotNone(brief)
        self.assertEqual(brief['batch_id'], 'b1')
        self.assertEqual(brief['mode'], 'wave')
        self.assertEqual(brief['retry_state']['succeeded'], ['api', 'docs', 'ui'])
        self.assertEqual(result['continuation_brief'], brief)
        self.assertIsNone(result['stall_notice'])

    def test_timeout_with_retained_output_is_adopted_and_not_redispatched(self):
        calls = []

        def timeout_after_commit(entry, prompt, policy):
            calls.append(entry['id'])
            wt = Path(entry['worktree'])
            rel = FILES[entry['id']]
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            commit(wt, rel)
            return {'ok': False, 'status': 'timeout', 'timed_out': True, 'reason': 'client timeout'}

        result = self.run_batch(
            assignment={'api': 'claude', 'ui': 'claude', 'docs': 'claude'},
            unit_runner=timeout_after_commit,
        )
        self.assertTrue(result['ok'], result)
        wave0 = {r['id']: r for r in result['waves'][0]}
        self.assertEqual(wave0['api']['status'], 'retained_after_timeout')
        self.assertTrue(wave0['api']['ok'])
        # Dependent unit `docs` still runs because `api` retained output was adopted
        self.assertEqual(result['waves'][1][0]['id'], 'docs')
        self.assertEqual(calls, ['api', 'ui', 'docs'])

    def test_all_workers_stopped_emits_stall_notice(self):
        self.policy['alerts'] = {
            'file': str(self.base / 'alerts.jsonl'),
            'summary': str(self.base / 'ALERTS.md'),
            'macos_notification': False,
        }
        result = self.run_batch(
            assignment={'api': 'claude', 'ui': 'claude', 'docs': 'claude'},
            unit_runner=FakeRunner(fail={'api', 'ui', 'docs'}),
        )
        self.assertFalse(result['ok'])
        self.assertIsNotNone(result['stall_notice'])
        self.assertTrue(result['stall_notice']['stalled'])
        self.assertEqual(result['stall_notice']['cause'], 'all_workers_stopped_or_blocked')

        no_worker = wr.run_batch(self.repo, UNITS[:1], assignment={'api': None}, policy=self.policy)
        self.assertIsNotNone(no_worker['stall_notice'])
        self.assertEqual(no_worker['stall_notice']['cause'], 'all_units_deferred_no_eligible_worker')


if __name__ == '__main__':
    unittest.main()
