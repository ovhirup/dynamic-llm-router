"""Fixed raw-collector snapshots for the allocation policy (#208)."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import unittest

from helpers import load_policy

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


def evidence(used=20, hours=24, window='weekly'):
    return {'required_windows': [window], 'windows': {window: {
        'percent_used': used, 'observed_at': (NOW - timedelta(minutes=1)).isoformat(),
        'expires_at': (NOW + timedelta(minutes=15)).isoformat(),
        'reset_at': (NOW + timedelta(hours=hours)).isoformat()}}}


def snapshot(order=('codex', 'claude'), used=20, **kwargs):
    from llm_router.usage_snapshot import build_snapshot
    policy = load_policy()
    policy['worker_order'] = list(order)
    return build_snapshot(policy, NOW, list(order), list(order), {},
                          {worker: evidence(used) for worker in order}, **kwargs)


class SnapshotTests(unittest.TestCase):
    def test_normalizes_raw_windows_and_preserves_original_names(self):
        from llm_router.usage_snapshot import build_snapshot
        raw = evidence(window='primary')
        raw['windows'].update(evidence(hours=72)['windows'])
        raw['required_windows'].append('weekly')
        policy = load_policy(worker_order=['codex'])
        before = deepcopy((policy, raw))
        result = build_snapshot(policy, NOW, ['codex'], ['codex'], {}, {'codex': raw})
        self.assertEqual(result['providers']['codex']['windows']['primary']['kind'], '5h')
        self.assertEqual(result['providers']['codex']['windows']['weekly']['kind'], 'weekly')
        self.assertEqual(result['providers']['codex']['required_windows'], ['primary', 'weekly'])
        self.assertEqual((policy, raw), before)
        self.assertEqual(json.loads(json.dumps(result)), result)

    def test_freezes_mappings_roles_and_explicit_metadata(self):
        from llm_router.usage_snapshot import build_snapshot
        policy = load_policy(worker_order=['cursor'])
        policy['allocation'] = {'worker_pools': {'cursor': {'provider': 'cursor-account', 'pool': 'models'}},
                                'providers': {'cursor-account': {'reserve_percent': 25}}}
        result = build_snapshot(policy, NOW, ['cursor'], ['cursor'], {},
                                {'cursor': evidence(window='included')},
                                provider_usage={'cursor-account': {'banked_resets': [
                                    {'id': 'bank-1', 'count': 1, 'expires_at': (NOW + timedelta(hours=4)).isoformat()}]}})
        self.assertEqual(result['workers']['cursor']['provider'], 'cursor-account')
        self.assertEqual(result['providers']['cursor-account']['reserve_percent'], 25)
        pool = result['providers']['cursor-account']['pools']['models']
        self.assertEqual(pool['windows']['included']['kind'], 'monthly')
        self.assertEqual(result['role_order']['coder'][0], 'codex')
        self.assertEqual(result['providers']['cursor-account']['banked_resets'][0]['id'], 'bank-1')

    def test_validation_returns_independent_copy(self):
        from llm_router.usage_snapshot import validate_snapshot
        original = snapshot()
        cloned = validate_snapshot(original)
        cloned['providers']['codex']['windows']['weekly']['percent_used'] = 30
        self.assertEqual(original['providers']['codex']['windows']['weekly']['percent_used'], 20)

    def test_rejects_bad_numeric_time_and_shape(self):
        from llm_router.usage_snapshot import validate_snapshot
        for value in (float('nan'), float('inf'), -1, 101, True):
            with self.subTest(value=value):
                bad = snapshot()
                bad['providers']['codex']['windows']['weekly']['percent_used'] = value
                with self.assertRaises(ValueError):
                    validate_snapshot(bad)
        bad = snapshot()
        bad['as_of'] = '2026-11-02T12:00:00'
        with self.assertRaises(ValueError):
            validate_snapshot(bad)
        bad = snapshot()
        bad['configured_order'].append('codex')
        with self.assertRaises(ValueError):
            validate_snapshot(bad)
        bad = snapshot()
        del bad['providers']['codex']['windows']['weekly']['observed_at']
        with self.assertRaises(ValueError):
            validate_snapshot(bad)


class SharedAllowanceMergeTests(unittest.TestCase):
    def build(self, observations, order=('codex', 'claude'), pool=None, metadata=None, supplied=None):
        from llm_router.usage_snapshot import build_snapshot
        policy = load_policy(worker_order=list(order))
        policy['allocation'] = {
            'worker_pools': {name: {'provider': 'shared', 'pool': pool} for name in order},
            'providers': metadata or {}}
        return build_snapshot(policy, NOW, list(order), list(order), {}, observations,
                              provider_usage=supplied)

    def test_same_time_exhaustion_commutes_for_provider_and_same_pool(self):
        from llm_router.allocation_policy import allocate
        for pool in (None, 'models'):
            with self.subTest(pool=pool):
                rows = {'codex': evidence(100), 'claude': evidence(20)}
                forward = self.build(rows, pool=pool)
                reverse = self.build(rows, order=('claude', 'codex'), pool=pool)
                self.assertEqual(forward['providers'], reverse['providers'])
                self.assertEqual(allocate(forward)['order'], [])
                self.assertEqual(allocate(reverse)['order'], [])
                allowance = forward['providers']['shared']
                allowance = allowance['pools'][pool] if pool else allowance
                self.assertEqual(allowance['windows']['weekly']['percent_used'], 100)

    def test_newer_reset_reading_wins_and_required_windows_are_unioned(self):
        from llm_router.allocation_policy import allocate
        old = evidence(100)
        old['windows']['weekly']['observed_at'] = (NOW - timedelta(minutes=4)).isoformat()
        old['windows']['weekly']['reset_at'] = (NOW - timedelta(minutes=2)).isoformat()
        fresh = evidence(5, hours=168)
        fresh['windows'].update(evidence(10, hours=5, window='primary')['windows'])
        fresh['required_windows'].append('primary')
        rows = {'codex': old, 'claude': fresh}
        forward = self.build(rows)
        reverse = self.build(rows, order=('claude', 'codex'))
        self.assertEqual(forward['providers'], reverse['providers'])
        self.assertEqual(forward['providers']['shared']['required_windows'], ['primary', 'weekly'])
        self.assertEqual(forward['providers']['shared']['windows']['weekly']['percent_used'], 5)
        self.assertEqual(set(allocate(forward)['order']), {'codex', 'claude'})

    def test_same_time_conflicting_reset_kind_or_unknown_remains_ineligible(self):
        from llm_router.allocation_policy import allocate
        for field, value in [('reset_at', (NOW + timedelta(hours=48)).isoformat()),
                             ('kind', 'monthly'), ('percent_used', None), ('observed_at', None)]:
            with self.subTest(field=field):
                ambiguous = evidence(5)
                ambiguous['windows']['weekly'][field] = value
                rows = {'codex': evidence(20), 'claude': ambiguous}
                forward = self.build(rows)
                reverse = self.build(rows, order=('claude', 'codex'))
                self.assertEqual(forward['providers'], reverse['providers'])
                self.assertEqual(allocate(forward)['order'], [])

    def test_equal_time_earliest_expiry_is_retained(self):
        rows = {'codex': evidence(20), 'claude': evidence(40)}
        rows['codex']['windows']['weekly']['expires_at'] = (NOW + timedelta(minutes=2)).isoformat()
        merged = self.build(rows)['providers']['shared']['windows']['weekly']
        self.assertEqual(merged['percent_used'], 40)
        self.assertEqual(merged['expires_at'], (NOW + timedelta(minutes=2)).isoformat())

    def test_stale_config_and_supplied_metadata_cannot_overwrite_exhaustion(self):
        from llm_router.allocation_policy import allocate
        stale = evidence(1)
        stale['windows']['weekly']['observed_at'] = (NOW - timedelta(minutes=10)).isoformat()
        stale['windows'].update(evidence(5, window='primary')['windows'])
        stale['required_windows'].append('primary')
        for pool in (None, 'models'):
            with self.subTest(pool=pool):
                meta = {'pools': {pool: stale}} if pool else stale
                result = self.build({'codex': evidence(100), 'claude': evidence(100)}, pool=pool,
                                    metadata={'shared': meta}, supplied={'shared': meta})
                allowance = result['providers']['shared']
                allowance = allowance['pools'][pool] if pool else allowance
                self.assertEqual(allowance['windows']['weekly']['percent_used'], 100)
                self.assertEqual(allowance['required_windows'], ['primary', 'weekly'])
                self.assertEqual(allocate(result)['order'], [])
