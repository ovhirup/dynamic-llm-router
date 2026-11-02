"""Week-curve for the grok-bot seat: frozen clock, fake meters only, no real CLIs and no network.

Strategy (docs/STRATEGY_WORKERS_FALLBACK.md): the grok family is paced, never banned. Unknown means
try last among cloud workers; ahead of the curve drops one rank.
"""
from datetime import datetime, timedelta, timezone
import unittest

from helpers import load_policy
from llm_router import selection, week_curve

RESET = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
DAY0 = RESET - timedelta(days=7)
ON_CURVE = {'percent_used': 0, 'reset_at': RESET.isoformat()}


def at(days, hours=0):
    return DAY0 + timedelta(days=days, hours=hours)


def fresh(worker, used, now):
    return {'source_worker': worker, 'required_windows': ['weekly'], 'windows': {'weekly': {
        'percent_used': used, 'observed_at': (now - timedelta(minutes=1)).isoformat(),
        'expires_at': (now + timedelta(hours=1)).isoformat(),
        'reset_at': (RESET if now < RESET else now + timedelta(days=7)).isoformat()}}}


def fake_io(used, now):
    return {w: {'load': (lambda w=w: {'evidence': fresh(w, used, now)}),
                'observe': (lambda policy, w=w: {'evidence': fresh(w, used, now)}), 'save': lambda o: None}
            for w in ('codex', 'grok-build', 'cursor')}


def grok_policy(mode, order=('grok-build', 'codex', 'claude'), **kw):
    return load_policy(worker_order=list(order), unmetered_workers=['claude', 'sample'], grok_bot={'billing': {'mode': mode}},
                       quota={'grok-build': {'enabled': True}}, **kw)


class WeekBudgetTests(unittest.TestCase):
    def test_day0_at_zero_allows(self):
        result = week_curve.week_budget({'percent_used': 0, 'reset_at': RESET}, at(0))
        self.assertTrue(result['eligible'], result)

    def test_default_spendable_is_85_times_elapsed(self):
        result = week_curve.week_budget({'percent_used': 0, 'reset_at': RESET}, at(3, 12))
        self.assertEqual(result['allowed_used'], 42.5)
        self.assertEqual(result['floor'], 15)

    def test_day1_at_40_is_ahead_of_curve(self):
        result = week_curve.week_budget({'percent_used': 40, 'reset_at': RESET}, at(1))
        self.assertEqual((result['state'], result['reason']), ('ahead_of_curve', 'no_headroom'))

    def test_last_24h_at_80_releases_reserve_to_95(self):
        result = week_curve.week_budget({'percent_used': 80, 'reset_at': RESET}, at(6, 12))
        self.assertTrue(result['eligible'], result)
        self.assertEqual(result['floor'], 5)
        self.assertFalse(week_curve.week_budget({'percent_used': 95, 'reset_at': RESET}, at(6, 12))['eligible'])
        self.assertEqual(week_curve.week_budget({'percent_used': 80, 'reset_at': RESET}, at(5))['state'],
                         'ahead_of_curve')

    def test_bad_evidence_is_unknown(self):
        for bad in (None, {}, {'percent_used': 0}, {'percent_used': 'x', 'reset_at': RESET},
                    {'percent_used': 101, 'reset_at': RESET}, {'percent_used': float('nan'), 'reset_at': RESET},
                    {'percent_used': 1, 'reset_at': 'nope'}):
            self.assertEqual(week_curve.week_budget(bad, at(0))['state'], 'unknown', bad)

    def test_exhausted_stays_ineligible_until_reset(self):
        self.assertFalse(week_curve.week_budget({'percent_used': 100, 'reset_at': RESET}, at(6, 23))['eligible'])


SEPARATE = {'grok_bot': {'billing': {'mode': 'separate'}}}
CLOUD_AND_LOCAL = ['grok-build', 'codex', 'claude', 'developer', 'grok-bot']
LOCAL = ('developer', 'grok-bot')


def budget(used, now, reset_at=RESET):
    return week_curve.week_budget({'percent_used': used, 'reset_at': reset_at}, now)


def gated_order(evidence, now):
    _, verdict, reason = week_curve.gate(SEPARATE, now, evidence)
    return verdict, reason, week_curve.pace(CLOUD_AND_LOCAL, {'grok-build'}, verdict, LOCAL)


class FrozenClockEdgeTests(unittest.TestCase):
    """Exact boundaries of the 7-day window, the mid-week curve, the last 24h and the reset itself."""

    # Day 0: exact start of the window.
    def test_day0_exact_start_zero_used_is_on_curve(self):
        result = budget(0, DAY0)
        self.assertEqual((result['state'], result['eligible'], result['reason']), ('on_curve', True, 'headroom'))
        self.assertEqual(result['allowed_used'], 0)
        self.assertEqual(gated_order({'percent_used': 0, 'reset_at': RESET}, DAY0)[:2], (week_curve.FIRST_FIT, None))

    def test_day0_exact_start_any_usage_is_ahead_of_curve(self):
        for used in (0.01, 1, 50):
            result = budget(used, DAY0)
            self.assertEqual((result['state'], result['eligible']), ('ahead_of_curve', False), used)
        verdict, reason, order = gated_order({'percent_used': 1, 'reset_at': RESET}, DAY0)
        self.assertEqual((verdict, reason), (week_curve.DROP_RANK, 'grok_week_curve_no_headroom'))
        self.assertEqual(order, ['codex', 'grok-build', 'claude', 'developer', 'grok-bot'])

    def test_before_window_start_is_unknown_and_tried_last(self):
        for now in (DAY0 - timedelta(seconds=1), DAY0 - timedelta(days=3)):
            result = budget(0, now)
            self.assertEqual((result['state'], result['reason']), ('unknown', 'reset_beyond_period'), now)
            self.assertFalse(result['eligible'])
            verdict, reason, order = gated_order({'percent_used': 0, 'reset_at': RESET}, now)
            self.assertEqual((verdict, reason), (week_curve.TRY_LAST, 'grok_week_curve_reset_beyond_period'))
            self.assertEqual(order, ['codex', 'claude', 'grok-build', 'developer', 'grok-bot'])

    # Mid-week: at(3, 12) allows exactly 85 * 0.5 = 42.5.
    def test_midweek_exact_curve_boundary(self):
        below = budget(42.4, at(3, 12))
        self.assertEqual((below['state'], below['eligible'], below['allowed_used']), ('on_curve', True, 42.5))
        self.assertEqual(gated_order({'percent_used': 42.4, 'reset_at': RESET}, at(3, 12))[0], week_curve.FIRST_FIT)
        on_line = budget(42.5, at(3, 12))
        self.assertEqual((on_line['state'], on_line['eligible'], on_line['headroom']), ('ahead_of_curve', False, 0))
        self.assertEqual(gated_order({'percent_used': 42.5, 'reset_at': RESET}, at(3, 12))[0], week_curve.DROP_RANK)

    # Last 24h: the reserve is released from 15 to 5 exactly at reset - 24h.
    def test_last_day_boundary_releases_reserve(self):
        just_before = budget(85, RESET - week_curve.LAST_DAY - timedelta(seconds=1))
        self.assertEqual((just_before['state'], just_before['floor'], just_before['eligible']),
                         ('ahead_of_curve', 15, False))
        exact = budget(85, RESET - week_curve.LAST_DAY)
        self.assertEqual((exact['state'], exact['floor'], exact['eligible'], exact['reason']),
                         ('last_day', 5, True, 'reserve_released'))

    def test_last_day_floor_is_95_percent(self):
        for now in (RESET - week_curve.LAST_DAY, RESET - timedelta(seconds=1)):
            for used in (85, 94, 94.99):
                result = budget(used, now)
                self.assertEqual((result['eligible'], result['reason']), (True, 'reserve_released'), (now, used))
            for used in (95, 99.9):
                result = budget(used, now)
                self.assertEqual((result['state'], result['eligible'], result['reason']),
                                 ('last_day', False, 'below_last_day_floor'), (now, used))
        verdict, reason, order = gated_order({'percent_used': 95, 'reset_at': RESET}, RESET - week_curve.LAST_DAY)
        self.assertEqual((verdict, reason), (week_curve.INELIGIBLE, 'grok_week_curve_below_last_day_floor'))
        self.assertNotIn('grok-build', order)

    # After reset: a reading taken before the reset says nothing about the new period.
    def test_at_and_after_reset_is_unknown_and_tried_last(self):
        for now in (RESET, RESET + timedelta(seconds=1), RESET + timedelta(days=2)):
            for used in (0, 50, 100):
                result = budget(used, now)
                self.assertEqual((result['state'], result['eligible'], result['reason']),
                                 ('unknown', False, 'reset_passed_needs_new_reading'), (now, used))
            verdict, reason, order = gated_order({'percent_used': 100, 'reset_at': RESET}, now)
            self.assertEqual((verdict, reason), (week_curve.TRY_LAST, 'grok_week_curve_reset_passed_needs_new_reading'))
            self.assertEqual(order, ['codex', 'claude', 'grok-build', 'developer', 'grok-bot'])

    # Missing or unusable reset_at: unknown, never banned.
    def test_missing_reset_at_is_unknown_and_tried_last(self):
        naive = RESET.replace(tzinfo=None)
        cases = {None: 'missing_reset_at', '': 'bad_reset_at', naive: 'bad_reset_at',
                 naive.isoformat(): 'bad_reset_at'}
        for reset_at, why in cases.items():
            result = budget(10, at(3), reset_at)
            self.assertEqual((result['state'], result['eligible'], result['reason']), ('unknown', False, why), reset_at)
            verdict, reason, order = gated_order({'percent_used': 10, 'reset_at': reset_at}, at(3))
            self.assertEqual((verdict, reason), (week_curve.TRY_LAST, 'grok_week_curve_' + why), reset_at)
            self.assertEqual(order, ['codex', 'claude', 'grok-build', 'developer', 'grok-bot'])
        no_key = week_curve.week_budget({'percent_used': 10}, at(3))
        self.assertEqual((no_key['state'], no_key['reason']), ('unknown', 'missing_reset_at'))

    def test_after_reset_selection_tries_pool_last_without_banning(self):
        now = RESET + timedelta(hours=1)
        picked = selection.select(10, policy=grok_policy('separate'), now=now, io=fake_io(0, now),
                                  grok_bot_evidence={'percent_used': 50, 'reset_at': RESET.isoformat()})
        self.assertEqual(picked['reasons']['grok-build'], 'grok_week_curve_reset_passed_needs_new_reading')
        self.assertEqual(picked['grok_bot']['verdict'], week_curve.TRY_LAST)
        self.assertEqual(picked['eligible'][-1], 'grok-build')


class PaceTests(unittest.TestCase):
    def test_try_last_goes_after_cloud_before_local(self):
        order = ['grok-build', 'codex', 'sample', 'claude', 'developer', 'grok-bot']
        paced = week_curve.pace(order, {'grok-build', 'sample'}, week_curve.TRY_LAST, ('developer', 'grok-bot'))
        self.assertEqual(paced, ['codex', 'claude', 'grok-build', 'sample', 'developer', 'grok-bot'])

    def test_drop_rank_moves_one_place_only(self):
        order = ['grok-build', 'codex', 'claude', 'grok-bot']
        paced = week_curve.pace(order, {'grok-build'}, week_curve.DROP_RANK, ('grok-bot',))
        self.assertEqual(paced, ['codex', 'grok-build', 'claude', 'grok-bot'])
        alone = week_curve.pace(['grok-build', 'grok-bot'], {'grok-build'}, week_curve.DROP_RANK, ('grok-bot',))
        self.assertEqual(alone, ['grok-build', 'grok-bot'])


class SelectionPacingTests(unittest.TestCase):
    def test_unknown_default_tries_pool_last_and_does_not_ban(self):
        policy = grok_policy('unknown', order=('grok-build', 'sample', 'codex', 'claude'),
                             workers={'sample': {'command': ['fake-worker'], 'shares_pool_with': 'grok-bot'}})
        del policy['grok_bot']['billing']['mode']
        self.assertEqual(week_curve.settings(policy)['mode'], 'unknown')
        picked = selection.select(10, policy=policy, now=at(0), io=fake_io(0, at(0)))
        self.assertEqual(picked['worker'], 'codex')
        self.assertEqual(picked['eligible'], ['codex', 'claude', 'grok-build', 'sample'])
        self.assertEqual(picked['reasons']['grok-build'], 'grok_pool_unknown')
        self.assertEqual(picked['reasons']['sample'], 'grok_pool_unknown')

    def test_unknown_pool_is_still_used_when_it_is_the_only_cloud_worker(self):
        picked = selection.select(10, policy=grok_policy('unknown', order=('grok-build',)), now=at(0),
                                  io=fake_io(0, at(0)))
        self.assertEqual(picked['worker'], 'grok-build')

    def test_unknown_pool_still_respects_its_own_meter(self):
        picked = selection.select(10, policy=grok_policy('unknown'), now=at(0), io=fake_io(90, at(0)))
        self.assertEqual(picked['reasons']['grok-build'], 'over_threshold')
        self.assertNotIn('grok-build', picked['eligible'])

    def test_separate_missing_reset_at_tries_last(self):
        picked = selection.select(10, policy=grok_policy('separate'), now=at(0), io=fake_io(0, at(0)),
                                  grok_bot_evidence={'percent_used': 0})
        self.assertEqual(picked['eligible'], ['codex', 'claude', 'grok-build'])
        self.assertEqual(picked['reasons']['grok-build'], 'grok_week_curve_missing_reset_at')

    def test_separate_ahead_of_curve_drops_one_rank(self):
        picked = selection.select(10, policy=grok_policy('separate', class_order={'bugfix': ['grok-build', 'codex']}),
                                  now=at(1), io=fake_io(0, at(1)), task_class='bugfix',
                                  grok_bot_evidence={'percent_used': 40, 'reset_at': RESET.isoformat()})
        self.assertEqual(picked['worker'], 'codex')
        self.assertEqual(picked['eligible'], ['codex', 'grok-build', 'grok-bot'])
        self.assertEqual(picked['reasons']['grok-build'], 'grok_week_curve_no_headroom')
        self.assertEqual(picked['grok_bot']['verdict'], week_curve.DROP_RANK)

    def test_paced_pool_does_not_block_fallback_consult(self):
        policy = load_policy(worker_order=['grok-build', 'cursor'], grok_bot={'billing': {'mode': 'unknown'}},
                             quota={'grok-build': {'enabled': True}, 'cursor': {'enabled': True, 'consult': 'fallback'}})
        picked = selection.select(10, policy=policy, now=at(0), io=fake_io(0, at(0)))
        self.assertEqual(picked['worker'], 'cursor')
        self.assertEqual(picked['eligible'], ['cursor', 'grok-build'])

    def test_separate_on_curve_keeps_first_fit(self):
        picked = selection.select(10, policy=grok_policy('separate'), now=at(0), io=fake_io(0, at(0)),
                                  grok_bot_evidence=ON_CURVE)
        self.assertEqual(picked['worker'], 'grok-build')
        self.assertEqual(picked['eligible'], ['grok-build', 'codex', 'claude'])

    def test_separate_exhausted_is_ineligible_not_refused(self):
        policy = grok_policy('separate', class_order={'feature': ['grok-build']})
        picked = selection.select(10, policy=policy, now=at(6, 23), io=fake_io(0, at(6, 23)), task_class='feature',
                                  grok_bot_evidence={'percent_used': 100, 'reset_at': RESET})
        self.assertEqual(picked['worker'], 'grok-bot')
        self.assertEqual(picked['reasons']['grok-build'], 'grok_week_curve_exhausted_until_reset')

    def test_shared_mode_never_returns_grok_build_even_at_zero(self):
        picked = selection.select(10, policy=grok_policy('shared', order=('grok-build',)), now=at(0),
                                  io=fake_io(0, at(0)), grok_bot_evidence=ON_CURVE)
        self.assertIsNone(picked['worker'])
        self.assertEqual(picked['reasons']['grok-build'], 'grok_pool_shared')


if __name__ == '__main__':
    unittest.main()
