"""Automatic waves use allocation at assignment and again immediately before launch."""
from copy import deepcopy
import unittest
from unittest import mock

from llm_router import allocation_dispatch, unit_worktrees, wave_runner
from test_allocation_dispatch import selected
import test_wave_runner


class AllocationWaveTests(unittest.TestCase):
    setUp = test_wave_runner.WaveRunnerTests.setUp
    tearDown = test_wave_runner.WaveRunnerTests.tearDown

    def run_batch(self, **kwargs):
        kwargs.setdefault('unit_runner', test_wave_runner.FakeRunner())
        kwargs.setdefault('acquire_slot', lambda policy, worker: ('uncapped', None))
        return wave_runner.run_batch(self.repo, test_wave_runner.UNITS[:1],
                                      policy=self.policy, batch_id='allocation-wave', **kwargs)

    def test_unit_plan_filters_parallel_pool_restrictions_and_capability(self):
        unit = dict(test_wave_runner.UNITS[0], workers=['codex', 'grok-bot'])
        choice = selected(['claude', 'codex', 'grok-bot'])
        with mock.patch('llm_router.selection.select', return_value=choice) as select:
            plan = allocation_dispatch.plan_unit(unit, self.policy)
        self.assertEqual(plan['order'], ['codex'])
        self.assertIn('unit_worker_restriction', plan['excluded']['claude'])
        self.assertIn('parallel', plan['excluded']['grok-bot'])
        self.assertEqual(select.call_args.kwargs['role'], 'coder')
        self.policy['scheduler'] = {'capabilities': {'codex': {'code': 0}}}
        with mock.patch('llm_router.selection.select', return_value=choice):
            plan = allocation_dispatch.plan_unit(unit, self.policy)
        self.assertIsNone(plan['worker'])
        self.assertEqual(plan['excluded']['codex'], 'unit_capability')

    def test_default_assignment_rechecks_and_persists_actual_worker_before_slot(self):
        runner, slots = test_wave_runner.FakeRunner(), []
        def acquire(policy, worker):
            manifest = unit_worktrees.load_manifest('allocation-wave', policy)
            self.assertEqual(manifest['units'][0]['worker'], worker)
            slots.append(worker)
            return 'uncapped', None
        with mock.patch('llm_router.selection.select', side_effect=[
                selected(['codex']), selected(['cursor'])]) as select:
            result = self.run_batch(unit_runner=runner, acquire_slot=acquire)
        self.assertEqual(select.call_count, 2)
        self.assertEqual([call[1] for call in runner.calls], ['cursor'])
        self.assertEqual(slots, ['cursor'])
        entry = unit_worktrees.load_manifest('allocation-wave', self.policy)['units'][0]
        self.assertEqual(entry['worker'], 'cursor')
        self.assertIn('codex', entry['branch'])  # existing worktree/branch retained
        self.assertEqual(entry['allocation']['worker'], 'cursor')
        self.assertEqual(result['waves'][0][0]['allocation']['worker'], 'cursor')
        self.assertEqual(result['allocations']['api']['worker'], 'cursor')

    def test_new_exhaustion_defers_without_invoking_worker_or_slot(self):
        runner, acquire = test_wave_runner.FakeRunner(), mock.Mock()
        with mock.patch('llm_router.selection.select', side_effect=[selected(['codex']), selected([])]):
            result = self.run_batch(unit_runner=runner, acquire_slot=acquire)
        self.assertEqual(runner.calls, [])
        acquire.assert_not_called()
        self.assertEqual(result['waves'][0][0]['status'], 'deferred_allocation')
        self.assertEqual(result['allocations']['api']['order'], [])

    def test_explicit_assignment_retains_its_contract(self):
        runner = test_wave_runner.FakeRunner()
        with mock.patch('llm_router.selection.select', side_effect=AssertionError('unexpected allocation')):
            result = self.run_batch(assignment={'api': 'codex'}, unit_runner=runner)
        self.assertTrue(result['ok'])
        self.assertEqual([call[1] for call in runner.calls], ['codex'])
        self.assertNotIn('allocations', result)

    def test_injected_quota_retains_its_contract(self):
        runner = test_wave_runner.FakeRunner()
        with mock.patch('llm_router.selection.select', side_effect=AssertionError('unexpected allocation')):
            result = self.run_batch(quota_left={'cursor': 1}, unit_runner=runner)
        self.assertTrue(result['ok'])
        self.assertEqual([call[1] for call in runner.calls], ['cursor'])

    def test_locked_worker_update_preserves_branch_and_refuses_non_pool(self):
        manifest = unit_worktrees.create_batch(self.repo, test_wave_runner.UNITS[:1], {'api': 'codex'},
                                               policy=self.policy, batch_id='allocation-wave')
        original = deepcopy(manifest['units'][0])
        with self.assertRaises(unit_worktrees.Refused):
            unit_worktrees.update_worker('allocation-wave', 'api', 'grok-bot', policy=self.policy)
        entry = unit_worktrees.update_worker('allocation-wave', 'api', 'cursor', policy=self.policy,
                                             allocation=selected(['cursor'])['allocation'])
        self.assertEqual(entry['branch'], original['branch'])
        self.assertEqual(entry['worktree'], original['worktree'])
        self.assertEqual(entry['worker'], 'cursor')
