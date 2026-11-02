"""Live selection adapts raw evidence into the same reproducible allocation (#208)."""
from copy import deepcopy
from datetime import timedelta
import unittest

from helpers import load_policy
from llm_router import config, selection
from test_usage_snapshot import NOW, evidence


def observation(worker, used=20, hours=24):
    raw = evidence(used, hours)
    raw['source_worker'] = worker
    return {'evidence': raw}


def fake_io(cache, calls):
    return {name: {'load': lambda name=name: cache.get(name),
                   'observe': lambda policy, name=name: calls.append(name), 'save': lambda row: None}
            for name in ('codex', 'claude', 'grok-build', 'cursor')}


class AllocationSelectionTests(unittest.TestCase):
    def policy(self, order=('codex', 'claude'), **kwargs):
        policy = load_policy(worker_order=list(order))
        policy['claude_account_check'] = {'enabled': False}
        for worker in order:
            if worker != 'grok-bot':
                policy.setdefault('quota', {}).setdefault(worker, {}).update(enabled=True)
        policy.update(kwargs)
        return policy

    def select(self, policy, cache, **kwargs):
        calls = []
        result = selection.select(10000, policy=policy, now=NOW, io=fake_io(cache, calls), **kwargs)
        return result, calls

    def test_dynamic_order_retains_raw_required_windows_and_is_reproducible(self):
        policy = self.policy()
        cache = {'codex': observation('codex', hours=72), 'claude': observation('claude', hours=1)}
        raw = cache['codex']['evidence']
        raw['windows'].update(evidence(10, hours=5, window='primary')['windows'])
        raw['required_windows'].append('primary')
        before = deepcopy((policy, cache))
        result, calls = self.select(policy, cache)
        again, _ = self.select(policy, cache)
        self.assertEqual(result['worker'], 'claude')
        self.assertEqual(result['allocation'], again['allocation'])
        self.assertEqual(result['evidences']['codex'], raw)
        self.assertEqual(result['usage_snapshot']['providers']['codex']['windows']['primary']['kind'], '5h')
        self.assertEqual((policy, cache), before)
        self.assertEqual(calls, [])

    def test_role_changes_allocation_order(self):
        policy = self.policy()
        cache = {name: observation(name) for name in ('codex', 'claude')}
        coder, _ = self.select(policy, cache, role='coder')
        planner, _ = self.select(policy, cache, role='planner')
        self.assertEqual(coder['worker'], 'codex')
        self.assertEqual(planner['worker'], 'claude')
        self.assertEqual(planner['eligible'], planner['allocation']['order'])
        with self.assertRaises(config.ConfigError):
            self.select(policy, cache, role='invalid')

    def test_fallback_uses_fresh_cache_without_running_observer(self):
        policy = self.policy(order=('codex', 'cursor'))
        policy['quota']['cursor']['consult'] = 'fallback'
        cache = {'codex': observation('codex', used=70), 'cursor': observation('cursor', used=5)}
        result, calls = self.select(policy, cache)
        self.assertEqual(result['worker'], 'cursor')
        self.assertEqual(result['reasons']['cursor'], 'quota_available')
        self.assertEqual(result['sources']['cursor'], 'cache')
        self.assertEqual(calls, [])

    def test_missing_stale_or_malformed_fallback_cache_never_runs_observer(self):
        policy = self.policy(order=('codex', 'cursor'))
        policy['quota']['cursor']['consult'] = 'fallback'
        stale = observation('cursor')
        stale['evidence']['windows']['weekly']['expires_at'] = (NOW - timedelta(seconds=1)).isoformat()
        for row in (None, stale, {'evidence': {'windows': 'bad'}}):
            with self.subTest(row=row):
                result, calls = self.select(policy, {'codex': observation('codex'), 'cursor': row})
                self.assertEqual(result['worker'], 'codex')
                self.assertEqual(result['reasons']['cursor'], 'not_consulted')
                self.assertEqual(calls, [])

    def test_malformed_raw_evidence_is_omitted_without_crashing(self):
        policy = self.policy(order=('codex',))
        rows = [{'source_worker': 'codex', 'required_windows': [{}], 'windows': {}},
                {'source_worker': 'codex', 'required_windows': ['weekly'], 'windows': []},
                {'source_worker': 'codex', 'required_windows': ['weekly'],
                 'windows': {'weekly': {'percent_used': float('nan')}}}]
        for row in rows:
            with self.subTest(row=row):
                result, calls = self.select(policy, {'codex': {'evidence': row}}, cached_only=True)
                self.assertEqual(result['eligible'], [])
                self.assertEqual(result['reasons']['codex'], 'quota_unknown')
                self.assertNotIn('codex', result['evidences'])
                self.assertEqual(calls, [])

    def test_existing_exhaustion_threshold_never_and_cooldown_gates_survive(self):
        policy = self.policy(order=('codex',))
        for used, reason in ((100, 'quota_exhausted'), (90, 'over_threshold')):
            result, _ = self.select(policy, {'codex': observation('codex', used=used)})
            self.assertEqual(result['eligible'], [])
            self.assertEqual(result['reasons']['codex'], reason)
        policy['never'] = ['codex']
        result, calls = self.select(policy, {'codex': observation('codex')})
        self.assertEqual(result['allocation']['excluded']['codex'], 'never')
        self.assertEqual(calls, [])
        policy['never'] = []
        result, _ = self.select(policy, {'codex': observation('codex')},
                                cooldowns={'codex': {'until': (NOW + timedelta(hours=1)).isoformat()}})
        self.assertTrue(result['allocation']['excluded']['codex'].startswith('cooldown_until_'))

    def test_threshold_override_is_frozen_without_reimposing_default(self):
        policy = self.policy(order=('codex',))
        policy['allocation'] = {'providers': {'codex': {'reserve_percent': 5}}}
        result, _ = self.select(policy, {'codex': observation('codex', used=90)},
                                threshold_overrides={'codex': 95})
        self.assertEqual(result['worker'], 'codex')
        self.assertEqual(result['usage_snapshot']['workers']['codex']['threshold_percent'], 95)
        self.assertEqual(policy['quota']['codex']['usage_threshold_percent'], 85)

    def test_week_curve_overlay_applies_once_and_orchestrator_remains_last(self):
        policy = self.policy(order=('grok-build', 'codex', 'claude', 'grok-bot'))
        policy['grok_bot'] = {'billing': {'mode': 'separate'}}
        cache = {name: observation(name) for name in ('grok-build', 'codex', 'claude')}
        result, _ = self.select(policy, cache,
                                grok_bot_evidence={'percent_used': 40,
                                                   'reset_at': (NOW + timedelta(days=6)).isoformat()})
        self.assertEqual(result['eligible'], ['codex', 'grok-build', 'claude', 'grok-bot'])
        self.assertEqual(result['allocation']['order'], result['eligible'])
        self.assertIn('pacing:drop_rank', result['allocation']['reasons']['grok-build'])
