"""Per-launch allocation, changing failover order and reviewable explanations (#208)."""
from copy import deepcopy
import json
from pathlib import Path
import stat
import unittest
from unittest import mock

from helpers import FakeWorkerHarness
from llm_router import job_dispatch, task_dispatch as td
import test_job_dispatch
import test_task_dispatch
from test_usage_snapshot import NOW


def selected(order, meters=None, role=None):
    return {'worker': order[0] if order else None, 'eligible': list(order),
            'meters': meters or {name: 10 for name in order}, 'reason': 'preferred_eligible_worker',
            'allocation': {'as_of': NOW.isoformat(), 'role': role, 'worker': order[0] if order else None,
                           'order': list(order), 'scores': {}, 'excluded': {}, 'actions': [],
                           'reasons': {name: 'fixed snapshot score' for name in order}},
            'usage_snapshot': {'private_prompt': 'must never be logged'}}


class AllocationDispatchTests(unittest.TestCase):
    setUp = test_task_dispatch.TaskDispatchTests.setUp
    tearDown = test_task_dispatch.TaskDispatchTests.tearDown
    run_task = test_task_dispatch.TaskDispatchTests.run_task
    fake_runner = test_task_dispatch.TaskDispatchTests.fake_runner

    def test_refresh_changes_failover_and_records_actual_plan_and_meters(self):
        self.policy['quota']['cursor']['enabled'] = True
        selections = [selected(['codex', 'grok-build']), selected(['cursor', 'grok-build'], {'cursor': 12})]
        select_calls, launches = [], []
        def select(chars, **kwargs):
            select_calls.append(kwargs)
            return deepcopy(selections.pop(0))
        result = self.run_task(select=select, runner=self.fake_runner(['quota', 'edit'], launches), stamp='fixed')
        self.assertEqual([run['worker'] for run in result['attempts']], ['codex', 'cursor'])
        self.assertEqual(len(select_calls), 2)
        self.assertEqual(len(result['allocation_history']), 2)
        for run in result['attempts']:
            self.assertEqual(run['allocation']['worker'], run['worker'])
        self.assertEqual(result['allocation']['worker'], 'cursor')
        self.assertIn(',cursor,', self.ledger.read_text())
        self.assertIn(',12,', self.ledger.read_text())

    def test_refresh_with_no_remaining_allowance_stops_without_launch(self):
        replies = [selected(['codex', 'grok-build']), selected([])]
        calls = []
        result = self.run_task(select=lambda *args, **kwargs: replies.pop(0),
                               runner=self.fake_runner(['quota', 'edit'], calls))
        self.assertEqual(len(calls), 1)
        self.assertEqual([run['worker'] for run in result['attempts']], ['codex'])
        self.assertEqual(result['allocation']['order'], [])
        self.assertFalse(result['ok'])

    def test_initial_orchestrator_handoff_keeps_allocation_without_invocation(self):
        launches = []
        result = self.run_task(task_class='feature',
                               select=lambda *args, **kwargs: selected(['grok-bot']),
                               runner=self.fake_runner([], launches))
        self.assertEqual(result['status'], 'handoff_to_orchestrator')
        self.assertEqual(result['allocation']['worker'], 'grok-bot')
        self.assertEqual(len(result['allocation_history']), 1)
        self.assertEqual(launches, [])

    def test_full_slots_are_considered_once_before_refresh(self):
        choices, calls = [], []
        def select(*args, **kwargs):
            choices.append(True)
            return selected(['codex', 'grok-build'])
        def acquire(policy, name):
            return ('full', None) if name == 'codex' else ('uncapped', None)
        with mock.patch('llm_router.task_dispatch.acquire_slot', side_effect=acquire) as acquire_slot:
            result = self.run_task(select=select, runner=self.fake_runner(['edit'], calls))
        self.assertEqual(result['worker'], 'grok-build')
        self.assertEqual([call.args[1] for call in acquire_slot.call_args_list], ['codex', 'grok-build'])
        self.assertEqual(len(choices), 2)

    def test_legacy_selector_keeps_one_call_and_fixed_hops(self):
        select_calls, launches = [], []
        def select(*args, **kwargs):
            select_calls.append(True)
            return {'eligible': ['codex', 'grok-build'], 'meters': {'codex': 10, 'grok-build': 10}}
        result = self.run_task(select=select, runner=self.fake_runner(['quota', 'edit'], launches))
        self.assertEqual([run['worker'] for run in result['attempts']], ['codex', 'grok-build'])
        self.assertEqual(len(select_calls), 1)
        self.assertNotIn('allocation', result)

    def test_forced_worker_reads_only_own_meter_and_logs_override(self):
        meters, launches = [], []
        def meter(name):
            meters.append(name)
            return {'eligible': [name], 'meters': {name: 10}}
        result = self.run_task(worker='codex', meter=meter,
                               runner=self.fake_runner(['edit'], launches))
        self.assertEqual(meters, ['codex'])
        self.assertEqual(result['allocation']['order'], ['codex'])
        self.assertEqual(result['allocation']['worker'], 'codex')
        self.assertIn('forced', result['allocation']['reasons']['codex'])

    def test_role_scoring_is_not_undone_by_legacy_role_order(self):
        calls = []
        def select(*args, **kwargs):
            self.assertEqual(kwargs['role'], 'planner')
            return selected(['codex', 'claude'], role='planner')
        result = self.run_task(role='planner', select=select, runner=self.fake_runner(['edit'], calls))
        self.assertEqual(result['worker'], 'codex')
        self.assertEqual(result['allocation']['role'], 'planner')

    def test_sidecar_is_atomic_private_minimal_and_redacted_before_invocation(self):
        launches = []
        selection = selected(['codex'])
        fabricated = 'sk-' + 'aB3d' * 8
        selection['allocation']['reasons']['codex'] = 'score explanation ' + fabricated
        runner = self.fake_runner(['edit'], launches)
        def inspect(argv, cwd, timeout, grace, **kwargs):
            path = self.out / 'fixed-allocation.json'
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            raw = path.read_text()
            data = json.loads(raw)
            self.assertEqual(data['allocation']['worker'], 'codex')
            self.assertEqual(len(data['allocation_history']), 1)
            self.assertNotIn(fabricated, raw)
            self.assertNotIn('private_prompt', raw)
            self.assertEqual(list(self.out.glob('*.tmp')), [])
            return runner(argv, cwd, timeout, grace, **kwargs)
        result = self.run_task(select=lambda *args, **kwargs: deepcopy(selection), runner=inspect, stamp='fixed')
        self.assertTrue(result['ok'])
        self.assertEqual(result['allocation']['worker'], 'codex')


class JobAllocationPersistenceTests(unittest.TestCase):
    setUp = test_job_dispatch.JobTests.setUp
    tearDown = test_job_dispatch.JobTests.tearDown

    def test_job_summary_and_nested_run_persist_allocation(self):
        FakeWorkerHarness('ok').install(self.policy, 'codex')
        result = job_dispatch.run_job(self.repo, self.task, policy=self.policy,
                                      select=lambda *args, **kwargs: selected(['codex']),
                                      test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['allocation']['worker'], 'codex')
        self.assertEqual(len(result['allocation_history']), 1)
        self.assertEqual(result['attempts'][0]['runs'][0]['allocation']['worker'], 'codex')
        persisted = json.loads((self.base / 'jobs' / (result['job_id'] + '.json')).read_text())
        self.assertEqual(persisted['allocation'], result['allocation'])
