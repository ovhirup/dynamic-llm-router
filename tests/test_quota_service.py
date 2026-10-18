import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from llm_router import alerts
from helpers import load_policy, TERM
from llm_router import quota_service as qs
from llm_router import selection
from llm_router import task_dispatch as td
from test_task_dispatch import repo

NOW = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)


def iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def evidence(worker, windows, observed=NOW, fresh_for=600):
    return {'source_worker': worker, 'required_windows': list(windows),
            'windows': {name: {'percent_used': pct, 'observed_at': iso(observed),
                               'expires_at': iso(observed + timedelta(seconds=fresh_for)), 'reset_at': iso(reset)}
                        for name, (pct, reset) in windows.items()}}


class FakeIO:
    """In-memory observers + caches. `readings[worker]` is what a live read returns (or an Exception)."""

    def __init__(self, readings, cache=None):
        self.readings, self.cache, self.calls = readings, dict(cache or {}), []

    def io(self):
        def make(worker):
            def observe(policy):
                self.calls.append(worker)
                value = self.readings.get(worker)
                if isinstance(value, Exception):
                    raise value
                return value
            return {'observe': observe, 'load': lambda: self.cache.get(worker),
                    'save': lambda obs: self.cache.__setitem__(worker, obs)}
        return {w: make(w) for w in ('codex', 'grok-build', 'cursor')}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        policy = copy.deepcopy(load_policy(timezone='Asia/Kolkata', quota={'grok-build': {'enabled': True},
                                                                            'cursor': {'enabled': True}}))
        policy['quota_refresh'].update(state_file=str(base / 'state.json'), lock_file=str(base / 'refresh.lock'))
        policy['alerts'] = {'file': str(base / 'alerts.jsonl'), 'summary': str(base / 'ALERTS.md'),
                            'macos_notification': False}
        self.base, self.policy = base, policy
        reset = NOW + timedelta(days=5)
        self.codex = {'evidence': evidence('codex', {'primary': (2, NOW + timedelta(hours=4)), 'weekly': (81, reset)}),
                      'cli_version': '0.130.0', 'version_warning': None}
        self.grok = {'evidence': evidence('grok-build', {'weekly': (67, NOW + timedelta(days=3))}),
                     'cli_version': '1.0.50', 'version_warning': None}
        self.cursor_exhausted = {'evidence': evidence('cursor', {'included': (100, datetime(2026, 11, 2, 18, 30, tzinfo=timezone.utc))},
                                                      observed=NOW - timedelta(hours=3)),
                                 'cli_version': '2026.10.16-e3cb207', 'version_warning': None}

    def tearDown(self):
        self.tmp.cleanup()


class RefreshTests(Base):
    def test_once_refreshes_due_skips_exhausted_cursor_and_alerts_codex_warn(self):
        fake = FakeIO({'codex': self.codex, 'grok-build': self.grok}, {'cursor': self.cursor_exhausted})
        report = qs.refresh(self.policy, once=True, now=NOW, io=fake.io())
        self.assertEqual(fake.calls, ['codex', 'grok-build'])
        self.assertEqual(report['workers']['codex'], {'action': 'refreshed', 'percent_used': 81, 'window': 'weekly'})
        self.assertTrue(report['workers']['cursor']['why'].startswith('exhausted until 03 Nov 00:00'))
        keys = [a['key'] for a in alerts.open_alerts(self.policy)]
        self.assertIn('codex:warn:2026-10-22T10:00Z', keys)
        self.assertFalse(any(k.startswith('grok-build:') for k in keys))
        self.assertIn('| codex | 81% |', (self.base / 'ALERTS.md').read_text())

    def test_intervals_and_not_due(self):
        fake = FakeIO({'codex': self.codex, 'grok-build': self.grok}, {'cursor': self.cursor_exhausted})
        qs.refresh(self.policy, now=NOW, io=fake.io())
        fake.calls.clear()
        report = qs.refresh(self.policy, now=NOW + timedelta(seconds=301), io=fake.io())
        self.assertEqual(fake.calls, ['codex'])  # grok interval 900s not reached
        self.assertEqual(report['workers']['grok-build']['action'], 'not_due')
        fake.calls.clear()
        qs.refresh(self.policy, now=NOW + timedelta(seconds=10), worker='grok-build', io=fake.io())
        self.assertEqual(fake.calls, ['grok-build'])  # --worker forces just that one

    def test_backoff_doubles_and_resets(self):
        fake = FakeIO({'codex': RuntimeError('boom'), 'grok-build': {'evidence': None}}, {'cursor': self.cursor_exhausted})
        r1 = qs.refresh(self.policy, now=NOW, io=fake.io())
        self.assertEqual(r1['workers']['codex'], {'action': 'failed', 'failures': 1, 'retry_in_seconds': 300,
                                                 'reason': 'observer_error'})
        r2 = qs.refresh(self.policy, now=NOW + timedelta(seconds=301), io=fake.io())
        self.assertEqual(r2['workers']['codex']['retry_in_seconds'], 600)
        r3 = qs.refresh(self.policy, now=NOW + timedelta(seconds=400), io=fake.io())
        self.assertEqual(r3['workers']['codex']['action'], 'not_due')
        fake.readings['codex'] = self.codex
        r4 = qs.refresh(self.policy, now=NOW + timedelta(seconds=1000), io=fake.io())
        self.assertEqual(r4['workers']['codex']['action'], 'refreshed')
        self.assertEqual(qs.load_state(self.policy)['codex']['failures'], 0)
        before = qs.load_state(self.policy)['grok-build']['failures']
        for _ in range(8):
            qs.refresh(self.policy, worker='grok-build', now=NOW, io=fake.io())
        self.assertEqual(qs.load_state(self.policy)['grok-build']['failures'], before + 8)
        self.assertEqual(qs.refresh(self.policy, worker='grok-build', now=NOW, io=fake.io())
                         ['workers']['grok-build']['retry_in_seconds'], 3600)

    def test_lock_prevents_overlap(self):
        with qs.RefreshLock(Path(self.policy['quota_refresh']['lock_file'])):
            with self.assertRaises(qs.AlreadyRunning):
                qs.refresh(self.policy, now=NOW, io=FakeIO({}).io())

    def test_cursor_reread_after_reset(self):
        fake = FakeIO({'cursor': {'evidence': evidence('cursor', {'included': (3, NOW + timedelta(days=40))},
                                                       observed=NOW + timedelta(days=17))}},
                      {'cursor': self.cursor_exhausted})
        qs.refresh(self.policy, worker='cursor', now=NOW + timedelta(days=17), io=fake.io())
        self.assertEqual(fake.calls, ['cursor'])

    def test_version_change_and_stale_alerts_dedupe(self):
        fake = FakeIO({'codex': self.codex, 'grok-build': self.grok}, {'cursor': self.cursor_exhausted})
        qs.refresh(self.policy, now=NOW, io=fake.io())
        fake.readings['grok-build'] = dict(self.grok, cli_version='1.0.51')
        qs.refresh(self.policy, worker='grok-build', now=NOW + timedelta(minutes=1), io=fake.io())
        qs.refresh(self.policy, worker='grok-build', now=NOW + timedelta(minutes=2), io=fake.io())
        keys = [a['key'] for a in alerts.open_alerts(self.policy)]
        self.assertEqual(keys.count('grok-build:version:1.0.51'), 1)
        fake.readings['codex'] = RuntimeError('down')
        for minutes in (40, 45):
            qs.refresh(self.policy, worker='codex', now=NOW + timedelta(minutes=minutes), io=fake.io())
        stale = [k for k in (a['key'] for a in alerts.open_alerts(self.policy)) if k.startswith('codex:stale')]
        self.assertEqual(len(stale), 1)
        self.assertEqual(alerts.open_alerts(self.policy)[0]['created_at'][:10], '2026-10-17')

    def test_threshold_levels_and_new_reset_period(self):
        high = {'evidence': evidence('codex', {'weekly': (90, NOW + timedelta(days=5))})}
        fake = FakeIO({'codex': high}, {})
        self.policy['quota']['grok-build']['enabled'] = self.policy['quota']['cursor']['enabled'] = False
        qs.refresh(self.policy, now=NOW, io=fake.io())
        levels = [a['level'] for a in alerts.open_alerts(self.policy) if a['kind'] == 'threshold']
        self.assertEqual(levels, ['stop'])
        fake.readings['codex'] = {'evidence': evidence('codex', {'weekly': (75, NOW + timedelta(days=12))},
                                                       observed=NOW + timedelta(days=7))}
        qs.refresh(self.policy, worker='codex', now=NOW + timedelta(days=7), io=fake.io())
        levels = [a['level'] for a in alerts.open_alerts(self.policy) if a['kind'] == 'threshold']
        self.assertEqual(levels, ['stop', 'warn'])

    def test_freshness_must_cover_interval(self):
        bad = copy.deepcopy(self.policy)
        bad['quota']['codex']['freshness_seconds'] = 300
        with self.assertRaisesRegex(ValueError, 'freshness'):
            qs.validate(bad)


class AlertTests(Base):
    def test_ack_and_redaction_and_banner_flag(self):
        token = 'ghp_' + 'a' * 36
        shown = []
        first = alerts.raise_alert(self.policy, 'k1', 'test', f'leaked {token}', now=NOW)
        self.assertIsNone(alerts.raise_alert(self.policy, 'k1', 'test', 'again', now=NOW))
        alerts.raise_alert(self.policy, 'k2', 'test', 'two', now=NOW, notifier=lambda *a: shown.append(a))
        self.assertEqual(shown, [])  # banner off by default
        self.policy['alerts']['macos_notification'] = True
        alerts.raise_alert(self.policy, 'k3', 'test', 'three', now=NOW, notifier=lambda *a: shown.append(a))
        self.assertEqual(len(shown), 1)
        self.assertNotIn(token, (self.base / 'alerts.jsonl').read_text())
        self.assertEqual(alerts.ack(self.policy, [first['id']]), [first['id']])
        self.assertEqual(sorted(a['key'] for a in alerts.open_alerts(self.policy)), ['k2', 'k3'])
        self.assertEqual(len(alerts.ack(self.policy)), 2)
        self.assertEqual(alerts.open_alerts(self.policy), [])

    def test_banner_uses_osascript_notification_only(self):
        seen = []
        alerts.macos_banner('t', 'm "quoted"', runner=lambda argv, **kw: seen.append(argv))
        self.assertEqual(seen[0][:2], ['/usr/bin/osascript', '-e'])
        self.assertTrue(seen[0][2].startswith('display notification "m \\"quoted\\"" with title "t"'))


class MeterTests(Base):
    def test_read_meter_uses_fresh_cache_then_single_live_read(self):
        fake = FakeIO({'grok-build': self.grok}, {'codex': self.codex})
        result = qs.read_meter(self.policy, 'codex', now=NOW + timedelta(seconds=60), io=fake.io())
        self.assertEqual((result['meters'], result['source'], fake.calls), ({'codex': 81}, 'cache', []))
        result = qs.read_meter(self.policy, 'grok-build', now=NOW, io=fake.io())
        self.assertEqual((result['meters'], result['source'], fake.calls), ({'grok-build': 67}, 'live', ['grok-build']))
        stale = qs.read_meter(self.policy, 'codex', now=NOW + timedelta(hours=1), io=FakeIO({}, {'codex': self.codex}).io())
        self.assertEqual(stale['meters'], {'codex': None})

    def test_exhausted_cursor_not_reread(self):
        fake = FakeIO({}, {'cursor': self.cursor_exhausted})
        result = qs.read_meter(self.policy, 'cursor', now=NOW, io=fake.io())
        self.assertEqual((result['meters'], result['eligible'], fake.calls), ({'cursor': 100}, [], []))

    def test_forced_run_task_reads_only_its_meter(self):
        target = repo(self.base / 't')
        task = self.base / 'task.md'
        task.write_text('x')
        seen = []
        with self.assertRaisesRegex(td.Refused, 'refusing new full-size'):
            td.run_task(task, target, worker='cursor', policy=self.policy,
                        meter=lambda w: seen.append(w) or {'meters': {w: 100}, 'eligible': [], 'reason': 'x'})
        self.assertEqual(seen, ['cursor'])

    def test_select_prefers_fresh_cache_only_with_real_observers(self):
        calls = []
        fake = FakeIO({'codex': None, 'grok-build': {'evidence': None}, 'cursor': {'evidence': None}})
        result = selection.select(0, policy=self.policy, now=NOW, io=fake.io())
        self.assertEqual(fake.calls, ['codex', 'grok-build', 'cursor'])  # cursor consulted only as fallback
        self.assertEqual(result['eligible'], [])
        self.assertEqual(result['reasons']['codex'], 'quota_unknown')
        fake = FakeIO({}, {'codex': self.codex, 'grok-build': self.grok})
        result = selection.select(0, policy=self.policy, now=NOW + timedelta(seconds=60), io=fake.io())
        self.assertEqual((result['worker'], result['eligible'], fake.calls), ('codex', ['codex', 'grok-build'], []))
        self.assertEqual(result['reasons']['cursor'], 'not_consulted')
        over = selection.select(0, policy=self.policy, now=NOW + timedelta(seconds=60), io=fake.io(),
                                threshold_overrides={'codex': 80})
        self.assertEqual((over['worker'], over['reasons']['codex']), ('grok-build', 'over_threshold'))

    def test_status_rows_and_table(self):
        fake = FakeIO({}, {'codex': self.codex, 'grok-build': self.grok, 'cursor': self.cursor_exhausted})
        rows = {r['worker']: r for r in qs.status_rows(self.policy, now=NOW + timedelta(minutes=2), io=fake.io())}
        self.assertEqual(rows['codex']['used'], '81%')
        self.assertEqual(rows['codex']['eligible'], 'yes')
        self.assertEqual(rows['codex']['freshness'], 'fresh (2m)')
        self.assertEqual(rows['cursor']['eligible'], 'no (exhausted)')
        self.assertEqual(rows['cursor']['resets'], '03 Nov 00:00 IST')
        self.assertTrue(rows['cursor']['freshness'].startswith('held (exhausted)'))
        table = qs.status_table(self.policy, now=NOW + timedelta(minutes=2), io=fake.io())
        self.assertIn('open alerts: 0', table)
        later = {r['worker']: r for r in qs.status_rows(self.policy, now=NOW + timedelta(hours=2), io=fake.io())}
        self.assertTrue(later['codex']['eligible'].startswith('no (stale)'))


class AgentTests(Base):
    def test_render_is_valid_plist_with_once_every_5_minutes(self):
        import plistlib
        data = plistlib.loads(qs.render_agent(self.policy, program=['/opt/bin/llm-router']).encode())
        self.assertEqual(data['ProgramArguments'], ['/opt/bin/llm-router', 'quota', 'refresh', '--once'])
        self.assertEqual(data['StartInterval'], 300)
        self.assertEqual(data['Label'], 'dev.llm-router.quota')
        self.assertIn('PATH', data['EnvironmentVariables'])
        self.assertNotIn('__', json.dumps(data))

    def test_install_is_dry_run_without_yes(self):
        ran = []
        self.policy['launch_agent']['target_dir'] = str(self.base / 'LaunchAgents')
        dry = qs.install_agent(self.policy, runner=lambda *a, **k: ran.append(a), program=['/opt/bin/llm-router'])
        self.assertTrue(dry['dry_run'])
        self.assertEqual(ran, [])
        self.assertFalse((self.base / 'LaunchAgents').exists())
        self.assertTrue(dry['would_run'].startswith(f'launchctl bootstrap gui/{os.getuid()} '))

    def test_install_and_uninstall_with_yes_use_launchctl(self):
        ran = []
        runner = lambda argv, **kw: ran.append(argv) or type('R', (), {'returncode': 0})()
        self.policy['launch_agent']['target_dir'] = str(self.base / 'LaunchAgents')
        done = qs.install_agent(self.policy, yes=True, runner=runner, program=['/opt/bin/llm-router'])
        self.assertTrue(Path(done['wrote']).exists())
        self.assertEqual(ran[0][:2], ['launchctl', 'bootstrap'])
        qs.uninstall_agent(self.policy, yes=True, runner=runner)
        self.assertEqual(ran[1][:2], ['launchctl', 'bootout'])
        self.assertFalse(Path(done['wrote']).exists())



class ResetPeriodTests(unittest.TestCase):
    def test_reset_jitter_shares_one_dedupe_period(self):
        self.assertEqual(qs.reset_period('2026-10-22T08:36:36Z'),
                         qs.reset_period('2026-10-22T08:36:37Z'))
        self.assertEqual(qs.reset_period('2026-10-22T08:36:36Z'), '2026-10-22T09:00Z')
        self.assertNotEqual(qs.reset_period('2026-10-22T08:36:36Z'),
                            qs.reset_period('2026-10-29T08:36:36Z'))

if __name__ == '__main__':
    unittest.main()
