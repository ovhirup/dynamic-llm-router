"""Observer tests: fixtures and fake CLIs only. No real agent CLI is ever launched."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from helpers import load_policy
from llm_router import observers, selection
from llm_router.observers import codex, common, cursor, grok_build

HERE = Path(__file__).resolve().parent
GROK_FIXTURE = (HERE / 'fixtures/grok_usage_screen_66.bin').read_bytes()
CURSOR_FIXTURE = (HERE / 'fixtures/cursor_usage_screen_100.bin').read_bytes()
KOLKATA = ZoneInfo('Asia/Kolkata')
GROK_NOW = datetime(2026, 10, 17, 6, 35, tzinfo=timezone.utc)
CURSOR_NOW = datetime(2026, 10, 17, 7, 40, tzinfo=timezone.utc)


def policy(**quota):
    return load_policy(timezone='Asia/Kolkata', **({'quota': quota} if quota else {}))


class GrokParseTests(unittest.TestCase):
    def parse(self, raw):
        return grok_build.parse_screen(raw, GROK_NOW, 300, KOLKATA)

    def replace(self, old, new):
        self.assertEqual(GROK_FIXTURE.count(old), 1, 'fixture mutation must change exactly one token')
        return GROK_FIXTURE.replace(old, new)

    def test_fixture_returns_only_minimized_weekly_evidence(self):
        result = self.parse(GROK_FIXTURE)
        self.assertEqual(result, {'source_worker': 'grok-build', 'required_windows': ['weekly'], 'windows': {
            'weekly': {'percent_used': 66, 'observed_at': '2026-10-17T06:35:00Z',
                       'reset_at': '2026-10-20T08:21:00Z', 'expires_at': '2026-10-17T06:40:00Z'}}})
        self.assertNotIn('Premium', json.dumps(result))

    def test_reset_is_read_in_the_configured_timezone(self):
        utc = grok_build.parse_screen(GROK_FIXTURE, GROK_NOW, 300, timezone.utc)
        self.assertEqual(utc['windows']['weekly']['reset_at'], '2026-10-20T13:51:00Z')

    def test_mismatches_return_none(self):
        reset = b'Resets: October 20, 13:51'
        start, end = GROK_FIXTURE.index(b'Weekly limit'), GROK_FIXTURE.index(reset) + len(reset)
        weekly = GROK_FIXTURE[start:end]
        cases = {
            'percent spacing': self.replace(b'66%', b'66 %'),
            'percent over 100': self.replace(b'66%', b'166%'),
            'missing reset line': self.replace(reset, b''),
            'two weekly matches': self.replace(weekly, weekly + b'\n' + weekly),
            'reset over eight days away': self.replace(reset, b'Resets: October 26, 13:51'),
            'reset in the past': self.replace(reset, b'Resets: October 16, 13:51'),
            'invalid UTF-8': self.replace(b'66%', b'66%\xff'),
            'missing usage heading': self.replace(b'Usage limit', b'Other limit'),
            'empty capture': b'',
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                self.assertIsNone(self.parse(raw))

    def test_bar_split_by_colour_escape_still_parses(self):
        start = GROK_FIXTURE.index(b'Weekly limit')
        empty = GROK_FIXTURE.index('\u2591'.encode(), start)
        raw = GROK_FIXTURE[:empty] + b'\x1b[2m ' + GROK_FIXTURE[empty:]
        self.assertEqual(self.parse(raw)['windows']['weekly']['percent_used'], 66)

    def test_naive_time_or_bad_freshness_is_none(self):
        self.assertIsNone(grok_build.parse_screen(GROK_FIXTURE, GROK_NOW.replace(tzinfo=None), 300))
        self.assertIsNone(grok_build.parse_screen(GROK_FIXTURE, GROK_NOW, float('nan')))


class GrokObserveTests(unittest.TestCase):
    def test_ok_and_version_warning(self):
        pol = policy(**{'grok-build': {'enabled': True, 'expected_cli_version': '1.0.50',
                                       'freshness_seconds': 1260}})
        for actual in ('1.0.50', '9.9.9'):
            with self.subTest(version=actual):
                capture = Mock(return_value=GROK_FIXTURE)
                result = grok_build.observe(pol, now=GROK_NOW, capture=capture, version=lambda: actual)
                capture.assert_called_once_with(25)
                self.assertEqual(result['status'], 'ok')
                self.assertEqual(result['evidence']['windows']['weekly']['percent_used'], 66)
                self.assertEqual(result['version_warning'] is None, actual == '1.0.50')

    def test_unknown_reasons(self):
        pol = policy()
        missing = grok_build.observe(pol, now=GROK_NOW, capture=Mock(), version=lambda: None)
        self.assertEqual((missing['status'], missing['reason']), ('unknown', 'cli_missing_or_no_version'))
        failed = grok_build.observe(pol, now=GROK_NOW, capture=Mock(side_effect=OSError('x')), version=lambda: '1.0.50')
        self.assertEqual(failed['reason'], 'capture_failed:OSError')
        garbled = grok_build.observe(pol, now=GROK_NOW, capture=lambda s: b'nothing here', version=lambda: '1.0.50')
        self.assertEqual((garbled['evidence'], garbled['reason']), (None, 'parse_failed'))


class CursorTests(unittest.TestCase):
    def test_fixture_parse(self):
        result = cursor.parse_screen(CURSOR_FIXTURE, CURSOR_NOW, 3960, KOLKATA)
        self.assertEqual(sorted(result['windows']), ['api', 'auto', 'included'])
        self.assertEqual({w['percent_used'] for w in result['windows'].values()}, {100})
        self.assertEqual(result['windows']['included']['reset_at'], '2026-11-02T18:30:00Z')
        self.assertNotIn('dashboard', json.dumps(result))

    def test_mismatch_and_observe_unknown(self):
        for old, new in ((b'Resets Nov 3', b'Resets Nov 30'), (b'Monthly plan', b'Weekly plan')):
            self.assertEqual(CURSOR_FIXTURE.count(old), 1)
            self.assertIsNone(cursor.parse_screen(CURSOR_FIXTURE.replace(old, new), CURSOR_NOW, 3960, KOLKATA))
        result = cursor.observe(policy(), now=CURSOR_NOW, capture=lambda s: b'', version=lambda: '2026.10.16-e3cb207')
        self.assertEqual((result['status'], result['reason']), ('unknown', 'parse_failed'))
        ok = cursor.observe(policy(), now=CURSOR_NOW, capture=lambda s: CURSOR_FIXTURE,
                            version=lambda: '2026.10.16-e3cb207')
        self.assertEqual(ok['status'], 'ok')


class FakeAppServer:
    """Popen-compatible fake of `codex app-server` speaking JSON-RPC lines."""

    def __init__(self, result):
        import io
        self.result, self.pid = result, os.getpid() * 0 + 999999
        lines = [json.dumps({'id': 1, 'result': {}}), json.dumps({'id': 2, 'result': result})]
        self.stdout = io.StringIO('\n'.join(lines) + '\n')
        self.stdin = io.StringIO()

    def __call__(self, argv, **kw):
        self.argv = argv
        return self

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class CodexTests(unittest.TestCase):
    def snapshot(self, now, primary=3, weekly=40, allowed=True):
        reset = int((now + timedelta(hours=3)).timestamp())
        return {'ordinaryUsageAllowed': allowed, 'rateLimits': {
            'primary': {'usedPercent': primary, 'windowDurationMins': 300, 'resetsAt': reset},
            'secondary': {'usedPercent': weekly, 'windowDurationMins': 10080, 'resetsAt': reset + 86400}}}

    def test_app_server_evidence(self):
        now = datetime.now(timezone.utc)
        fake = FakeAppServer(self.snapshot(now))
        result = codex.observe(policy(), now=now, runner=fake, version=lambda: '0.130.0')
        self.assertEqual(fake.argv[1:], ['app-server'])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['evidence']['windows']['weekly']['percent_used'], 40)

    def test_usage_not_allowed_is_unknown(self):
        now = datetime.now(timezone.utc)
        result = codex.observe(policy(), now=now, runner=FakeAppServer(self.snapshot(now, allowed=False)),
                               version=lambda: None)
        self.assertEqual(result['status'], 'unknown')


class HardTimeoutTests(unittest.TestCase):
    def test_run_capture_kills_group_even_if_a_grandchild_holds_the_pipe(self):
        started = time.monotonic()
        code, out = common.run_capture(['/bin/sh', '-c', 'sleep 30 & echo started; sleep 30'], 1)
        self.assertIsNone(code)
        self.assertLess(time.monotonic() - started, 5)

    def test_run_capture_missing_binary(self):
        self.assertEqual(common.run_capture(['/nonexistent/cli', '--version'], 1), (None, ''))

    def isolated(self, spec, timeout=None):
        pol = policy(codex={'observer': spec, 'hard_timeout_seconds': 2})
        return observers.observe_isolated(pol, 'codex', timeout=timeout)

    def test_hung_observer_degrades_to_unknown_quickly(self):
        started = time.monotonic()
        result = self.isolated('fake_observers:hang')
        self.assertEqual((result['status'], result['reason']), ('unknown', 'hard_timeout'))
        self.assertLess(time.monotonic() - started, 8)

    def test_isolated_ok_crash_and_garbage(self):
        ok = self.isolated('fake_observers:ok', timeout=20)
        self.assertEqual((ok['status'], ok['cli_version']), ('ok', '1.2.3'))
        crash = self.isolated('fake_observers:crash', timeout=20)
        self.assertEqual(crash['status'], 'unknown')
        self.assertNotIn('/secret/path', json.dumps(crash))
        self.assertEqual(self.isolated('fake_observers:garbage', timeout=20)['reason'], 'observer_returned_nothing')

    def test_unknown_reading_is_never_eligible(self):
        pol = policy()
        io = {'codex': {'observe': lambda p: common.unknown('hard_timeout'), 'load': lambda: None,
                        'save': lambda o: None}}
        result = selection.select(0, policy=pol, io=io)
        self.assertIsNone(result['worker'])
        self.assertEqual(result['reasons']['codex'], 'quota_unknown')


FAKE_TUI = textwrap.dedent('''\
    #!{python}
    import os, sys, time
    mode = sys.argv[1]
    screen = open(sys.argv[2], 'rb').read()
    sys.stdout.write('fake dashboard ready\\n'); sys.stdout.flush()
    if mode == 'hang':
        time.sleep(120)
    buf = b''
    while True:
        chunk = os.read(0, 1024)
        if not chunk:
            break
        buf += chunk
        if b'/usage' in buf and (b'\\r' in buf or b'\\n' in buf):
            os.write(1, screen)
            time.sleep(60)
''')


class PtyCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = Path(self.tmp.name) / 'fake-grok'
        self.script.write_text(FAKE_TUI.replace('{python}', sys.executable))
        self.script.chmod(stat.S_IRWXU)
        self.screen = Path(self.tmp.name) / 'screen.bin'
        self.screen.write_bytes(GROK_FIXTURE)

    def test_capture_from_fake_tui_parses(self):
        raw = common.capture_pty([str(self.script), 'serve', str(self.screen)], 8, [(b'/usage', 0.5), (b'\r', 1.5)], 0.5)
        evidence = grok_build.parse_screen(raw, GROK_NOW, 300, KOLKATA)
        self.assertEqual(evidence['windows']['weekly']['percent_used'], 66)

    def test_hanging_tui_returns_by_deadline(self):
        started = time.monotonic()
        raw = common.capture_pty([str(self.script), 'hang', str(self.screen)], 3, [(b'/usage', 1), (b'\r', 5)], 1)
        self.assertLess(time.monotonic() - started, 8)
        self.assertIsNone(grok_build.parse_screen(raw, GROK_NOW, 300, KOLKATA))

    def test_missing_binary_raises_before_fork(self):
        with self.assertRaises(FileNotFoundError):
            common.capture_pty(['/nonexistent/grok'], 2, [], 0.1)


if __name__ == '__main__':
    unittest.main()
