"""Allocation decisions use fixed inputs, no real providers or settings (#208)."""
from copy import deepcopy
from datetime import timedelta
import unittest

from helpers import load_policy
from test_usage_snapshot import NOW, evidence, snapshot


def allocate(data, role=None):
    from llm_router.allocation_policy import allocate as policy_allocate
    return policy_allocate(data, role)


class AllocationPolicyTests(unittest.TestCase):
    def test_deterministic_pure_and_configured_tie_break(self):
        data = snapshot(order=('claude', 'codex'))
        original = deepcopy(data)
        first = allocate(data)
        self.assertEqual(first, allocate(data))
        self.assertEqual(first['order'], ['claude', 'codex'])
        self.assertEqual(data, original)
        self.assertEqual(first['worker'], 'claude')
        self.assertIn('score', first['scores']['claude'])

    def test_exhausted_primary_blocks_despite_weekly_headroom_until_reset(self):
        data = snapshot()
        provider = data['providers']['codex']
        provider['windows']['primary'] = dict(provider['windows']['weekly'], kind='5h', percent_used=100)
        provider['required_windows'].append('primary')
        self.assertEqual(allocate(data)['order'], ['claude'])
        self.assertIn('exhausted', allocate(data)['excluded']['codex'])
        provider['windows']['primary']['expires_at'] = (NOW - timedelta(seconds=1)).isoformat()
        self.assertIn('exhausted', allocate(data)['excluded']['codex'])

    def test_passed_reset_needs_new_reading_then_fresh_snapshot_unblocks(self):
        data = snapshot()
        window = data['providers']['codex']['windows']['weekly']
        window['reset_at'] = (NOW - timedelta(seconds=1)).isoformat()
        self.assertIn('reset_passed_needs_new_reading', allocate(data)['excluded']['codex'])
        window.update(evidence(used=0, hours=168)['windows']['weekly'])
        self.assertIn('codex', allocate(data)['order'])

    def test_expiring_allowance_preferred_at_equal_role_fit_and_usage(self):
        data = snapshot()
        data['providers']['claude']['windows']['weekly']['reset_at'] = (NOW + timedelta(hours=1)).isoformat()
        result = allocate(data)
        self.assertEqual(result['order'], ['claude', 'codex'])
        self.assertGreater(result['scores']['claude']['urgency'], result['scores']['codex']['urgency'])

    def test_reserve_floor_kept_and_explicit_hard_rule_cannot_bypass_threshold(self):
        data = snapshot(used=75)
        data['providers']['codex']['reserve_percent'] = 30
        self.assertIn('reserve', allocate(data)['excluded']['codex'])
        self.assertIn('reserve', allocate(data, 'judge')['excluded']['codex'])
        data['providers']['codex']['hard_reserve_percent'] = 10
        self.assertIn('codex', allocate(data, 'judge')['order'])
        data['providers']['codex']['windows']['weekly']['percent_used'] = 90
        self.assertIn('threshold', allocate(data, 'judge')['excluded']['codex'])

    def test_bank_default_ignored_and_explicit_rule_recommends_without_capacity(self):
        data = snapshot(used=100)
        data['providers']['codex']['banked_resets'] = [
            {'id': 'reset-1', 'count': 1, 'expires_at': (NOW + timedelta(hours=2)).isoformat()}]
        self.assertEqual(allocate(data)['actions'], [])
        data['reset_rule'] = {'name': 'recommend_before_expiry', 'within_hours': 24}
        result = allocate(data)
        self.assertEqual(result['order'], [])
        self.assertEqual(result['actions'][0]['reset_id'], 'reset-1')
        self.assertEqual(result['actions'][0]['action'], 'action_required')
        data['providers']['codex']['banked_resets'][0]['expires_at'] = (NOW - timedelta(seconds=1)).isoformat()
        self.assertEqual(allocate(data)['actions'], [])

    def test_unknown_reset_rule_rejected(self):
        data = snapshot()
        data['reset_rule'] = {'name': 'auto_redeem', 'within_hours': 24}
        with self.assertRaises(ValueError):
            allocate(data)

    def test_role_fit_changes_order_and_judge_never_local(self):
        data = snapshot(order=('codex', 'claude', 'aider-ollama'))
        data['workers']['aider-ollama']['unmetered'] = True
        data['workers']['aider-ollama']['local'] = True
        self.assertEqual(allocate(data, 'coder')['worker'], 'codex')
        self.assertEqual(allocate(data, 'planner')['worker'], 'claude')
        self.assertNotIn('aider-ollama', allocate(data, 'judge')['order'])
        self.assertIn('role', allocate(data, 'judge')['excluded']['aider-ollama'])

    def test_unknown_stale_and_future_required_evidence_fail_closed(self):
        for key, value in [('percent_used', None), ('observed_at', (NOW + timedelta(minutes=1)).isoformat()),
                           ('expires_at', (NOW - timedelta(seconds=1)).isoformat())]:
            with self.subTest(key=key):
                data = snapshot()
                data['providers']['codex']['windows']['weekly'][key] = value
                self.assertNotIn('codex', allocate(data)['order'])

    def test_independent_mapped_pools_and_shared_capacity_not_summed(self):
        from llm_router.usage_snapshot import build_snapshot
        policy = load_policy(worker_order=['cursor', 'codex'])
        policy['allocation'] = {'worker_pools': {
            'cursor': {'provider': 'cursor-account', 'pool': 'models'},
            'codex': {'provider': 'cursor-account', 'pool': 'models'}}}
        data = build_snapshot(policy, NOW, ['cursor', 'codex'], ['cursor', 'codex'], {},
                              {'cursor': evidence(20), 'codex': evidence(20)})
        data['providers']['cursor-account']['pools']['other'] = {
            'windows': {'monthly': dict(data['providers']['cursor-account']['pools']['models']['windows']['weekly'],
                                       kind='monthly', percent_used=100)}, 'required_windows': ['monthly']}
        self.assertEqual(allocate(data)['order'], ['cursor', 'codex'])
        self.assertEqual(allocate(data)['scores']['cursor']['headroom'],
                         allocate(data)['scores']['codex']['headroom'])
        data['providers']['cursor-account']['pools']['models']['windows']['weekly']['percent_used'] = 100
        self.assertEqual(allocate(data)['order'], [])

    def test_on_demand_disabled_unknown_and_exhausted_cap_block(self):
        data = snapshot(order=('cursor',))
        data['workers']['cursor']['pool'] = 'on_demand'
        provider = data['providers']['cursor']
        provider['pools']['on_demand'] = {'windows': {}, 'required_windows': []}
        for on_demand in (None, {'enabled': False, 'used': 0, 'cap': 10},
                          {'enabled': True, 'used': 10, 'cap': 10},
                          {'enabled': True, 'used': 0, 'cap': None}):
            with self.subTest(on_demand=on_demand):
                provider['on_demand'] = on_demand
                self.assertNotIn('cursor', allocate(data)['order'])
        provider['on_demand'] = {'enabled': True, 'used': 1, 'cap': 10}
        self.assertEqual(allocate(data)['order'], ['cursor'])

    def test_missing_mapping_fails_closed_and_gate_never_overridden(self):
        data = snapshot()
        data['workers']['codex']['provider'] = 'missing'
        self.assertNotIn('codex', allocate(data)['order'])
        data['workers']['claude'].update(gate_eligible=False, gate_reason='never')
        self.assertEqual(allocate(data)['excluded']['claude'], 'never')

    def test_unmetered_and_serial_fallback_and_pacing_overlay(self):
        data = snapshot(order=('grok-build', 'codex', 'aider-ollama', 'grok-bot'))
        for name in ('aider-ollama', 'grok-bot'):
            data['workers'][name]['unmetered'] = True
        data['pacing'] = {'members': ['grok-build'], 'verdict': 'try_last'}
        result = allocate(data)
        self.assertEqual(result['order'], ['codex', 'grok-build', 'aider-ollama', 'grok-bot'])
        self.assertIn('pacing', result['reasons']['grok-build'])
        data['pacing']['verdict'] = 'ineligible'
        self.assertNotIn('grok-build', allocate(data)['order'])
