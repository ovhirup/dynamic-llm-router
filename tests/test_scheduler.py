"""Scheduler scoring: capability x quota headroom x week-curve pace; grok-bot only as last resort."""
from datetime import datetime, timedelta, timezone
import unittest

from helpers import load_policy
from llm_router import scheduler

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def stamp(delta):
    return (NOW + delta).isoformat()


def evidence(worker, used, weekly_used=None, reset_in=None):
    windows = {'primary': {'percent_used': used, 'observed_at': stamp(timedelta(minutes=-1)),
                           'expires_at': stamp(timedelta(minutes=10))}}
    required = ['primary']
    if weekly_used is not None:
        windows['weekly'] = {'percent_used': weekly_used, 'observed_at': stamp(timedelta(minutes=-1)),
                             'expires_at': stamp(timedelta(minutes=10)),
                             'reset_at': stamp(reset_in) if reset_in is not None else None}
        required.append('weekly')
    return {'source_worker': worker, 'required_windows': required, 'windows': windows}


def policy(order, **kw):
    builtin = ('codex', 'claude', 'grok-build', 'cursor')
    quota = {w: {'usage_threshold_percent': 80} for w in order if w in builtin}
    workers = {w: {'command': ['fake-' + w]} for w in order if w not in builtin}
    kw.setdefault('unmetered_workers', list(workers))
    return load_policy(worker_order=order, quota=quota, workers=workers, **kw)


class SchedulerTests(unittest.TestCase):
    def test_score_is_product_of_three_factors(self):
        p = policy(['codex'])
        # 40% used of an 80% threshold -> headroom 0.5; half the week left with 40% used -> pace 60/50 = 1.2
        result = scheduler.assign(p, 'code', {'codex': evidence('codex', 40, 40, timedelta(days=3.5))}, NOW)
        row = result['scores']['codex']
        self.assertEqual(result['worker'], 'codex')
        self.assertAlmostEqual(row['headroom'], 0.5)
        self.assertAlmostEqual(row['pace'], 1.2)
        self.assertAlmostEqual(row['score'], 0.6)

    def test_highest_score_wins_over_worker_order(self):
        p = policy(['codex', 'claude'])
        result = scheduler.assign(p, 'code', {'codex': evidence('codex', 70), 'claude': evidence('claude', 10)}, NOW)
        self.assertEqual(result['worker'], 'claude')
        self.assertEqual(result['reason'], 'best_score')

    def test_pace_prefers_quota_that_would_otherwise_expire(self):
        p = policy(['codex', 'claude'])
        # Same headroom; codex is ahead of the week curve, claude behind it with reset close.
        result = scheduler.assign(p, 'code', {
            'codex': evidence('codex', 20, 20, timedelta(days=6)),
            'claude': evidence('claude', 20, 20, timedelta(days=1))}, NOW)
        self.assertLess(result['scores']['codex']['pace'], 1)
        self.assertEqual(result['scores']['claude']['pace'], 2.0)
        self.assertEqual(result['worker'], 'claude')

    def test_pace_defaults_to_one_without_weekly_reset(self):
        self.assertEqual(scheduler.pace(evidence('codex', 10), NOW), 1.0)
        self.assertEqual(scheduler.pace(evidence('codex', 10, 50, None), NOW), 1.0)
        self.assertEqual(scheduler.pace(None, NOW), 1.0)

    def test_capability_table_excludes_unlisted_kinds(self):
        p = policy(['codex', 'claude'], scheduler={'capabilities': {'codex': {'docs': 0.5}}})
        ev = {'codex': evidence('codex', 0), 'claude': evidence('claude', 60)}
        self.assertEqual(scheduler.assign(p, 'code', ev, NOW)['worker'], 'claude')
        self.assertEqual(scheduler.capability(p, 'codex', 'code'), 0.0)
        self.assertEqual(scheduler.capability(p, 'claude', 'code'), 1.0)

    def test_unknown_over_threshold_or_exhausted_quota_has_no_headroom(self):
        p = policy(['codex'])
        self.assertEqual(scheduler.headroom(p, 'codex', None, NOW), 0.0)
        self.assertEqual(scheduler.headroom(p, 'codex', evidence('codex', 85), NOW), 0.0)
        self.assertEqual(scheduler.headroom(p, 'codex', evidence('codex', 100), NOW), 0.0)

    def test_unmetered_local_worker_has_full_headroom(self):
        p = policy(['local'], unmetered_workers=['local'])
        result = scheduler.assign(p, 'code', {}, NOW)
        self.assertEqual(result['worker'], 'local')
        self.assertEqual(result['scores']['local']['score'], 1.0)

    def test_grok_bot_is_never_in_parallel_pool(self):
        p = policy(['grok-bot', 'codex'], unmetered_workers=['grok-bot'])
        self.assertEqual(scheduler.pool(p), ['codex'])
        result = scheduler.assign(p, 'code', {'codex': evidence('codex', 79)}, NOW)
        self.assertEqual(result['worker'], 'codex')
        self.assertNotIn('grok-bot', result['scores'])

    def test_grok_bot_only_when_no_capable_worker_has_quota(self):
        p = policy(['codex', 'claude', 'grok-bot'], scheduler={'capabilities': {'claude': {'docs': 1}}})
        result = scheduler.assign(p, 'code', {'codex': evidence('codex', 95), 'claude': evidence('claude', 0)}, NOW)
        self.assertEqual(result['worker'], 'grok-bot')
        self.assertEqual(result['reason'], 'no_capable_worker_with_quota')

    def test_no_fallback_when_grok_bot_not_configured_or_ineligible(self):
        ev = {'codex': evidence('codex', 95)}
        self.assertIsNone(scheduler.assign(policy(['codex']), 'code', ev, NOW)['worker'])
        p = policy(['codex', 'grok-bot'], ineligible={'grok-bot': 'paused'})
        self.assertIsNone(scheduler.assign(p, 'code', ev, NOW)['worker'])


if __name__ == '__main__':
    unittest.main()
