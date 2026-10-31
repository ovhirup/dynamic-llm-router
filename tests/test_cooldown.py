"""Quota/429 cooldowns: a failed worker is skipped by select() until its meter resets (frozen clock)."""
from datetime import datetime, timedelta, timezone
import json
import unittest

from helpers import load_policy
from llm_router import config, cooldown, selection

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
RESET = NOW + timedelta(hours=3)


def observation(reset_at, percent=100):
    window = {'percent_used': percent, 'observed_at': (NOW - timedelta(minutes=1)).isoformat(),
              'expires_at': (NOW + timedelta(minutes=10)).isoformat()}
    if reset_at is not None:
        window['reset_at'] = reset_at.isoformat()
    return {'evidence': {'source_worker': 'codex', 'required_windows': ['five_hour'],
                         'windows': {'five_hour': window}}}


class CooldownTests(unittest.TestCase):
    def setUp(self):
        cooldown.path().unlink(missing_ok=True)
        self.addCleanup(cooldown.path().unlink, missing_ok=True)
        self.policy = load_policy()
        self.policy['worker_order'] = ['codex', 'grok-build']
        self.policy['unmetered_workers'] = ['codex', 'grok-build']

    def select(self, now):
        return selection.select(10000, policy=self.policy, io={}, now=now)

    def test_quota_error_cools_until_meter_reset_and_persists(self):
        until = cooldown.record('codex', now=NOW, observation=observation(RESET))
        self.assertEqual(until, RESET)
        self.assertTrue(str(cooldown.path()).startswith(str(config.state_dir())))
        saved = json.loads(cooldown.path().read_text())
        self.assertEqual(saved['codex']['until'], RESET.isoformat())
        self.assertEqual(saved['codex']['reason'], 'quota_error')

    def test_select_skips_cooled_worker_with_reason(self):
        cooldown.record('codex', now=NOW, observation=observation(RESET))
        result = self.select(NOW + timedelta(hours=2, minutes=59))
        self.assertEqual(result['worker'], 'grok-build')
        self.assertEqual(result['reasons']['codex'], 'cooldown_until_' + RESET.isoformat())
        self.assertEqual(result['cooldowns'], {'codex': RESET.isoformat()})

    def test_worker_eligible_again_after_reset(self):
        cooldown.record('codex', now=NOW, observation=observation(RESET))
        result = self.select(RESET)
        self.assertEqual(result['worker'], 'codex')
        self.assertEqual(result['reasons']['codex'], 'unmetered')
        self.assertEqual(result['cooldowns'], {})

    def test_unknown_reset_cools_for_one_hour(self):
        for obs in (None, observation(None), observation(NOW - timedelta(minutes=5))):
            with self.subTest(obs=obs):
                self.assertEqual(cooldown.record('codex', now=NOW, observation=obs or {}), NOW + timedelta(hours=1))
        self.assertEqual(self.select(NOW + timedelta(minutes=59))['worker'], 'grok-build')
        self.assertEqual(self.select(NOW + timedelta(hours=1))['worker'], 'codex')

    def test_falls_back_to_most_used_window_reset_when_not_exhausted(self):
        self.assertEqual(cooldown.record('codex', now=NOW, observation=observation(RESET, percent=90)), RESET)

    def test_corrupt_state_is_ignored(self):
        cooldown.path().parent.mkdir(parents=True, exist_ok=True)
        cooldown.path().write_text('not json')
        self.assertEqual(self.select(NOW)['worker'], 'codex')
        cooldown.path().write_text(json.dumps({'codex': {'until': 'garbage'}, 'grok-build': 5}))
        self.assertEqual(self.select(NOW)['worker'], 'codex')


if __name__ == '__main__':
    unittest.main()


IST = timezone(timedelta(hours=5, minutes=30))


class ResetTextTests(unittest.TestCase):
    NOW = datetime(2026, 10, 31, 10, tzinfo=timezone.utc)

    def setUp(self):
        cooldown.path().unlink(missing_ok=True)
        self.addCleanup(cooldown.path().unlink, missing_ok=True)

    def test_codex_usage_limit_message(self):
        text = "You've hit your usage limit. Upgrade or try again later; limit resets Nov 4, 12:17 PM."
        self.assertEqual(cooldown.reset_from_text(text, self.NOW, IST), datetime(2026, 11, 4, 12, 17, tzinfo=IST))

    def test_retry_after_seconds(self):
        self.assertEqual(cooldown.reset_from_text('429 Too Many Requests. Retry-After: 120', self.NOW),
                         self.NOW + timedelta(seconds=120))

    def test_year_rollover_and_no_match(self):
        now = datetime(2026, 12, 31, 10, tzinfo=timezone.utc)
        self.assertEqual(cooldown.reset_from_text('resets Jan 2, 9:00 AM', now, IST),
                         datetime(2027, 1, 2, 9, tzinfo=IST))
        self.assertIsNone(cooldown.reset_from_text("You're out of usage", now, IST))
        self.assertIsNone(cooldown.reset_from_text('', now, IST))

    def test_record_prefers_text_then_falls_back(self):
        until = cooldown.record('codex', now=self.NOW, observation={}, text='usage limit, resets Nov 4, 12:17 PM', zone=IST)
        self.assertEqual(until, datetime(2026, 11, 4, 12, 17, tzinfo=IST))
        self.assertIn('codex', cooldown.active(self.NOW + timedelta(days=3)))
        self.assertNotIn('codex', cooldown.active(datetime(2026, 11, 4, 12, 18, tzinfo=IST)))
        self.assertEqual(cooldown.record('cursor', now=self.NOW, observation={}, text="You're out of usage"),
                         self.NOW + timedelta(hours=1))

    def test_quota_pattern_matches_live_messages(self):
        import re
        pattern = re.compile(load_policy()['run_task']['quota_error_pattern'])
        for text in ("You're out of usage. Switch to Auto or upgrade.", 'HTTP 429 Too Many Requests',
                     'usage limit, resets Nov 4, 12:17 PM'):
            self.assertTrue(pattern.search(text), text)
        self.assertFalse(pattern.search('Ran 4290 tests'))
