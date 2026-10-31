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
