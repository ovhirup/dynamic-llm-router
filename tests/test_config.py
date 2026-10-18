import json
import re
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from helpers import load_policy
from llm_router import config, selection, task_dispatch as td


class PathTests(unittest.TestCase):
    def test_xdg_and_overrides(self):
        saved = {k: os.environ.get(k) for k in ('LLM_ROUTER_CONFIG_DIR', 'LLM_ROUTER_STATE_DIR', 'XDG_CONFIG_HOME',
                                                'XDG_STATE_HOME', 'LLM_ROUTER_CONFIG')}
        try:
            for key in saved:
                os.environ.pop(key, None)
            os.environ['XDG_CONFIG_HOME'] = '/x/cfg'
            os.environ['XDG_STATE_HOME'] = '/x/state'
            self.assertEqual(config.config_dir(), Path('/x/cfg/llm-router'))
            self.assertEqual(config.state_dir(), Path('/x/state/llm-router'))
            del os.environ['XDG_CONFIG_HOME'], os.environ['XDG_STATE_HOME']
            self.assertEqual(config.config_dir(), Path.home() / '.config/llm-router')
            self.assertEqual(config.state_dir(), Path.home() / '.local/state/llm-router')
            os.environ['LLM_ROUTER_CONFIG'] = '/y/my.json'
            self.assertEqual(config.config_file(), Path('/y/my.json'))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_relative_paths_resolve_to_state_or_config(self):
        policy = load_policy()
        self.assertEqual(config.policy_path(policy, 'run_task', 'ledger', 'x'), config.state_dir() / 'logs/ledger.csv')
        self.assertEqual(config.policy_path(policy, 'run_task', 'private_terms_file', 'x'),
                         config.config_dir() / 'private_terms.txt')
        self.assertEqual(config.policy_path(policy, 'run_job', 'repo_config', 'x'), config.config_dir() / 'repos.json')
        self.assertEqual(config.resolve('/abs/file'), Path('/abs/file'))


class MergeAndValidateTests(unittest.TestCase):
    def write(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'config.json'
        path.write_text(json.dumps(data))
        return path

    def test_user_config_deep_merges(self):
        policy = config.load_policy(self.write({'run_task': {'pacing': {'warn_percent': 60}}, 'timezone': 'UTC'}))
        self.assertEqual(policy['run_task']['pacing'], {'warn_percent': 60, 'refuse_full_percent': 85})
        self.assertIn('codex', policy['run_task']['commands'])
        self.assertEqual(str(config.tz(policy)), 'UTC')

    def test_defaults_contain_no_personal_data(self):
        text = json.dumps(config.defaults()).lower()
        for needle in ('/users/', '/home/', '.local.json'):
            self.assertNotIn(needle, text)
        emails = re.findall(r'[\w.+-]+@[\w.-]+', text)
        self.assertEqual(emails, ['router@localhost'])
        self.assertEqual(config.defaults()['never'], [])
        self.assertEqual(config.defaults()['worker_order'], [])
        self.assertNotIn('claude', config.defaults()['worker_order'])  # Claude adapter is off by default

    def test_validation_errors(self):
        bad = [
               ({'worker_order': ['codex', 'codex']}, 'unique'),
               ({'worker_order': ['nope']}, 'unknown workers'),
               ({'worker_order': ['codex', 'claude'], 'never': ['claude']}, 'never'),
               ({'timezone': 'Mars/Olympus'}, 'timezone'),
               ({'worker_order': ['codex'], 'quota': {'codex': {'freshness_seconds': 10}}}, 'freshness')]
        for data, message in bad:
            with self.subTest(data=data):
                with self.assertRaisesRegex(ValueError, message):
                    config.load_policy(self.write(data))
        with self.assertRaisesRegex(config.ConfigError, 'invalid JSON'):
            path = self.write({})
            path.write_text('{nope')
            config.load_policy(path)

    def test_claude_opt_in_as_unmetered(self):
        policy = config.load_policy(self.write({'worker_order': ['codex', 'claude'], 'unmetered_workers': ['claude']}))
        io = {'codex': {'observe': lambda p: None, 'load': lambda: None, 'save': lambda o: None}}
        result = selection.select(10_000, policy=policy, io=io)
        self.assertEqual((result['worker'], result['reasons']['claude']), ('claude', 'unmetered'))
        self.assertIn('claude', td.forceable(policy))

    def test_binary_discovery(self):
        policy = load_policy(binaries={'codex': '~/bin/codex'})
        self.assertEqual(config.binary(policy, 'codex'), os.path.expanduser('~/bin/codex'))
        self.assertEqual(config.binary(load_policy(binaries={'grok': None}), 'grok').split('/')[-1], 'grok')


class SelectionTests(unittest.TestCase):
    NOW = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)

    def ev(self, worker, pct, fresh=600, observed=None):
        observed = observed or self.NOW
        iso = lambda d: d.isoformat().replace('+00:00', 'Z')
        return {'evidence': {'source_worker': worker, 'required_windows': ['w'], 'windows': {'w': {
            'percent_used': pct, 'observed_at': iso(observed), 'expires_at': iso(observed + timedelta(seconds=fresh)),
            'reset_at': iso(observed + timedelta(days=2))}}}}

    def io(self, cache, calls=None):
        calls = [] if calls is None else calls
        return {w: {'observe': (lambda p, _w=w: calls.append(_w)), 'load': (lambda _w=w: cache.get(_w)),
                    'save': lambda o: None} for w in ('codex', 'grok-build', 'cursor')}

    def policy(self, **kw):
        return load_policy(quota={'grok-build': {'enabled': True}, 'cursor': {'enabled': True}}, **kw)

    def test_order_threshold_and_fallback(self):
        cache = {'codex': self.ev('codex', 90), 'grok-build': self.ev('grok-build', 20), 'cursor': self.ev('cursor', 5)}
        result = selection.select(10_000, policy=self.policy(), now=self.NOW, io=self.io(cache))
        self.assertEqual(result['worker'], 'grok-build')
        self.assertEqual(result['reasons'], {'codex': 'over_threshold', 'grok-build': 'quota_available',
                                             'cursor': 'not_consulted', 'developer': 'developer_disabled_or_task_too_large'})
        both = selection.select(10_000, policy=self.policy(), now=self.NOW, io=self.io(cache), consult_all=True)
        self.assertEqual(both['eligible'], ['grok-build', 'cursor'])

    def test_stale_future_malformed_exhausted_never_eligible(self):
        cases = {'stale': self.ev('codex', 5, observed=self.NOW - timedelta(hours=2)),
                 'future': self.ev('codex', 5, observed=self.NOW + timedelta(hours=1)),
                 'malformed': {'evidence': {'source_worker': 'codex', 'windows': 'x'}},
                 'wrong worker': self.ev('cursor', 5),
                 'exhausted': self.ev('codex', 100)}
        pol = load_policy(worker_order=['codex'])
        for name, record in cases.items():
            with self.subTest(case=name):
                calls = []
                result = selection.select(10_000, policy=pol, now=self.NOW, io=self.io({'codex': record}, calls),
                                          cached_only=True)
                self.assertIsNone(result['worker'])

    def test_never_and_developer_draft(self):
        pol = load_policy(worker_order=['codex', 'developer'], never=['cursor'], developer={'enabled': True, 'endpoint': 'http://127.0.0.1:9/v1'})
        result = selection.select(0, policy=pol, now=self.NOW, io=self.io({}), cached_only=True)
        self.assertEqual(result['worker'], 'developer')
        self.assertNotIn('cursor', td.forceable(pol))
        full = selection.select(10_000, policy=pol, now=self.NOW, io=self.io({}), cached_only=True)
        self.assertIsNone(full['worker'])


class DeveloperTests(unittest.TestCase):
    def test_disabled_raises_and_key_from_named_env_only(self):
        with self.assertRaisesRegex(RuntimeError, 'disabled'):
            td.developer_draft(load_policy(), 'hi')
        seen = {}

        class Response:
            def __enter__(self):
                import io
                return io.BytesIO(json.dumps({'choices': [{'message': {'content': 'draft'}}]}).encode())

            def __exit__(self, *a):
                return False

        class Opener:
            def open(self, request, timeout=None):
                seen['auth'] = request.get_header('Authorization')
                seen['url'] = request.full_url
                return Response()

        os.environ['TEST_DEV_KEY'] = 'k-123'
        try:
            pol = load_policy(developer={'enabled': True, 'endpoint': 'http://127.0.0.1:9/v1/chat/completions',
                                         'api_key_env': 'TEST_DEV_KEY'})
            self.assertEqual(td.developer_draft(pol, 'hi', opener=Opener()), 'draft')
        finally:
            del os.environ['TEST_DEV_KEY']
        self.assertEqual(seen['auth'], 'Bearer k-123')


if __name__ == '__main__':
    unittest.main()


class LiveReadClockTests(unittest.TestCase):
    def test_live_read_stamped_after_select_start_is_not_future(self):
        import time as _time
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        def observe(_policy):
            _time.sleep(0.05)
            stamp = _dt.now(_tz.utc)
            window = {'percent_used': 10, 'observed_at': stamp.isoformat(),
                      'expires_at': (stamp + _td(minutes=10)).isoformat()}
            return {'evidence': {'source_worker': 'codex', 'required_windows': ['primary'],
                                 'windows': {'primary': window}}, 'status': 'ok'}
        io = {'codex': {'observe': observe, 'load': lambda: None, 'save': lambda obs: None}}
        result = selection.select(10_000, policy=load_policy(worker_order=['codex']), io=io)
        self.assertEqual(result['reasons']['codex'], 'quota_available')
        self.assertEqual(result['worker'], 'codex')


class StackwichConfigTests(unittest.TestCase):
    def test_defaults_and_custom_opt_in(self):
        policy = load_policy()
        self.assertTrue(all(v == {'enabled': False, 'supported': False}
                            for v in policy['run_task']['stackwich'].values()))
        policy = load_policy(workers={'sample': {'command': ['sample'],
                              'stackwich': {'enabled': True, 'supported': True}}})
        self.assertEqual(policy['run_task']['stackwich']['sample'], {'enabled': True, 'supported': True})
        self.assertEqual(policy['worker_order'], load_policy()['worker_order'])
        self.assertNotIn('claude', policy['worker_order'])

    def test_invalid_options(self):
        for value in (True, [], {'enabled': 1}, {'supported': 'yes'}, {'extra': False}):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                load_policy(workers={'sample': {'command': ['sample'], 'stackwich': value}})
        for value in ([], {'unknown': {}}, {'codex': {'enabled': None}}):
            with self.subTest(value=value), self.assertRaises(config.ConfigError):
                load_policy(run_task={'stackwich': value})

    def test_invalid_internal_map_with_custom_worker(self):
        with self.assertRaises(config.ConfigError):
            load_policy(workers={'sample': {'command': ['sample']}}, run_task={'stackwich': []})
