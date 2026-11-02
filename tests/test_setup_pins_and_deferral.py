"""#212 (a) a switch is logged only after job checks pass; (b) units run honours setup pins."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from llm_router import allocation_dispatch, config, job_dispatch, wave_runner as wr
from test_job_role import JobRoleTests
from test_session_preferences import policy as setup_policy
from test_wave_runner import UNITS, FakeRunner, repo


def decision(orchestrator='codex'):
    return {'orchestrator': orchestrator, 'reason': 'caller switch confirmed', 'source': 'always_ask',
            'current': 'grok-bot', 'proposed': orchestrator, 'confirmed': True, 'switched': False,
            'deferred': True, 'persisted': False,
            'recommendation': {'orchestrator': 'grok-bot', 'reason': 'preferred', 'budgets': {}, 'actions': []}}


def recorder(calls):
    def record(pending):
        calls.append(pending)
        return dict(pending, deferred=False, persisted=True, switched=True, audit_path='/audit.jsonl')
    return record


class DeferredSwitchRunJobTests(unittest.TestCase):
    setUp = JobRoleTests.setUp
    select = JobRoleTests.select
    job = JobRoleTests.job

    def test_refused_job_never_records_the_switch(self):
        calls = []
        with self.assertRaises(job_dispatch.Refused):
            job_dispatch.run_job(self.base / 'not-a-repo', self.task, policy=self.policy, select=self.select,
                                 orchestrator_decision=decision(), record_decision=recorder(calls))
        with self.assertRaises(job_dispatch.Refused):
            self.job(branch='bad..name', orchestrator_decision=decision(), record_decision=recorder(calls))
        self.assertEqual(calls, [])

    def test_accepted_job_records_the_switch_once_and_keeps_the_recorded_decision(self):
        calls = []
        result = self.job(orchestrator_decision=decision(), record_decision=recorder(calls))
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['orchestrator'], 'codex')
        self.assertEqual(result['orchestrator_decision']['audit_path'], '/audit.jsonl')
        self.assertTrue(result['orchestrator_decision']['switched'])


class UnitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = repo(self.base / 'main-repo').resolve()
        (self.base / 'repos.json').write_text(json.dumps({str(self.repo): {'test_cmd': 'true'}}))
        self.state = self.base / 'state'
        state = mock.patch.object(config, 'state_dir', return_value=self.state)
        state.start()
        self.addCleanup(state.stop)

    def paths(self, policy):
        policy = copy.deepcopy(policy)
        policy['run_job'].update(worktree_root=str(self.base / 'wt'), lock_dir=str(self.base / 'locks'),
                                 jobs_dir=str(self.base / 'jobs'), units_dir=str(self.base / 'units'),
                                 merge_queue_dir=str(self.base / 'mq'), repo_config=str(self.base / 'repos.json'))
        return policy

    def pinned(self, pin='claude'):
        overrides = {'default': None, 'roles': {'coder': {'worker': pin, 'reason': 'design-heavy repo'}}}
        return self.paths(setup_policy(workers=('claude', 'codex'), worker_overrides=overrides))

    def run_batch(self, policy, **kwargs):
        kwargs.setdefault('unit_runner', FakeRunner())
        kwargs.setdefault('acquire_slot', lambda policy, worker: ('uncapped', None))
        return wr.run_batch(self.repo, UNITS, policy=policy, batch_id='b1', **kwargs)

    def audit(self):
        path = self.state / 'logs' / 'setup-decisions.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    @staticmethod
    def plan(order=('codex', 'claude'), excluded=None):
        return lambda unit, policy: {'order': list(order), 'worker': order[0] if order else None,
                                     'excluded': dict(excluded or {}), 'reasons': {w: 'eligible' for w in order},
                                     'role': 'coder', 'as_of': '2026-11-02T00:00:00+00:00',
                                     'scores': {}, 'actions': []}

    # (a) units run
    def test_bad_units_never_record_the_switch(self):
        calls = []
        with self.assertRaises(wr.Refused):
            wr.run_batch(self.repo, [{'id': 'x'}], policy=self.paths(setup_policy()),
                         orchestrator_decision=decision(), record_decision=recorder(calls))
        with self.assertRaises(wr.Refused):
            self.run_batch(self.paths(setup_policy()), assignment={'nope': 'codex'},
                           orchestrator_decision=decision(), record_decision=recorder(calls))
        self.assertEqual(calls, [])

    def test_accepted_batch_records_the_switch_once(self):
        calls = []
        result = self.run_batch(self.paths(setup_policy()), assignment={'api': 'codex', 'ui': 'codex', 'docs': 'codex'},
                                orchestrator_decision=decision(), record_decision=recorder(calls))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['orchestrator_decision']['audit_path'], '/audit.jsonl')

    # (b) pins
    def test_role_pin_assigns_every_automatic_unit_and_is_logged_once(self):
        runner = FakeRunner()
        with mock.patch.object(allocation_dispatch, 'plan_unit', side_effect=self.plan()):
            result = self.run_batch(self.pinned(), unit_runner=runner)
        self.assertEqual({worker for _, worker, _ in runner.calls}, {'claude'})
        for plan in result['allocations'].values():
            self.assertEqual(plan['worker'], 'claude')
            self.assertEqual(plan['pin'], {'worker': 'claude', 'source': 'role_pin', 'reason': 'design-heavy repo'})
        events = [e for e in self.audit() if e['event'] == 'worker_override']
        self.assertEqual([(e['worker'], e['source'], e['role']) for e in events], [('claude', 'role_pin', 'coder')])

    def test_ineligible_pinned_worker_defers_instead_of_silently_falling_back(self):
        runner = FakeRunner()
        with mock.patch.object(allocation_dispatch, 'plan_unit',
                               side_effect=self.plan(order=('codex',), excluded={'claude': 'reserve_floor'})):
            result = self.run_batch(self.pinned(), unit_runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(result['deferred']), len(UNITS))
        self.assertIn('pinned worker claude not eligible: reserve_floor', result['deferred'][0]['reason'])

    def test_explicit_assignment_beats_the_pin_and_logs_nothing(self):
        runner = FakeRunner()
        self.run_batch(self.pinned(), unit_runner=runner, assignment={'api': 'codex', 'ui': 'codex', 'docs': 'codex'})
        self.assertEqual({worker for _, worker, _ in runner.calls}, {'codex'})
        self.assertEqual(self.audit(), [])


if __name__ == '__main__':
    unittest.main()
