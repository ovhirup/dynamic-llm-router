"""Plan replay is offline; the Notch feed receives only minimal decisions (#208)."""
import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from helpers import load_policy
from llm_router import cli, selection
import test_live_jobs_feed
from test_allocation_selection import fake_io, observation
from test_usage_snapshot import NOW, snapshot


def run(argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(argv)
    return code, output.getvalue()


class AllocationCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'snapshot.json'
        self.snapshot = snapshot()
        self.path.write_text(json.dumps(self.snapshot))

    def test_parser_and_help(self):
        parser, _ = cli.build_parser()
        args = parser.parse_args(['plan', '--snapshot', str(self.path), '--role', 'judge', '--json'])
        self.assertEqual(args.role, 'judge')
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as done:
            cli.main(['plan', '--help'])
        self.assertEqual(done.exception.code, 0)

    def test_offline_json_matches_pure_policy_and_never_loads_settings_or_observers(self):
        from llm_router.allocation_policy import allocate
        expected = dict(allocate(self.snapshot, 'coder'), snapshot=self.snapshot)
        with mock.patch('llm_router.config.load_policy', side_effect=AssertionError('settings read')):
            with mock.patch('llm_router.quota_service.default_io', side_effect=AssertionError('observer read')):
                code, output = run(['plan', '--snapshot', str(self.path), '--role', 'coder', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), expected)
        self.path.write_text(output)
        code, replay = run(['plan', '--snapshot', str(self.path), '--role', 'coder', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(replay), expected)

    def test_human_order_components_and_reasons_match_json(self):
        code, output = run(['plan', '--snapshot', str(self.path), '--role', 'coder'])
        self.assertEqual(code, 0)
        self.assertIn('codex -> claude', output)
        self.assertIn('role_fit=', output)
        self.assertIn('headroom=', output)
        self.assertIn('score=', output)

    def test_invalid_snapshot_is_clear_and_class_replay_is_rejected(self):
        with mock.patch('llm_router.config.load_policy', side_effect=AssertionError('settings read')):
            code, output = run(['plan', '--snapshot', str(self.path), '--class', 'review', '--json'])
            self.assertEqual(code, 2)
            self.assertIn('class', json.loads(output)['error'])
            for malformed in ('not json', '{}', '{"schema_version": 99}'):
                self.path.write_text(malformed)
                code, output = run(['plan', '--snapshot', str(self.path), '--json'])
                self.assertEqual(code, 2)
                self.assertIn('error', json.loads(output))
                self.assertNotIn('Traceback', output)

    def policy(self):
        policy = load_policy(worker_order=['codex', 'claude'])
        policy['claude_account_check'] = {'enabled': False}
        policy['quota'].setdefault('claude', {})['enabled'] = True
        return policy

    def test_live_class_and_cached_only_are_forwarded_without_observer_call(self):
        policy = self.policy()
        calls = []
        cache = {name: observation(name) for name in ('codex', 'claude')}
        actual_select = selection.select
        def collect(chars, **kwargs):
            self.assertTrue(kwargs['cached_only'])
            self.assertEqual(kwargs['task_class'], 'review')
            return actual_select(chars, now=NOW, io=fake_io(cache, calls), **kwargs)
        with mock.patch('llm_router.config.load_policy', return_value=policy):
            with mock.patch('llm_router.selection.select', side_effect=collect):
                code, output = run(['plan', '--cached-only', '--class', 'review', '--json'])
        self.assertEqual(code, 0)
        self.assertIn('snapshot', json.loads(output))
        self.assertEqual(calls, [])

    def test_cli_select_preserves_urgency_over_legacy_role_preference(self):
        policy = self.policy()
        cache = {'codex': observation('codex', hours=0.01), 'claude': observation('claude', hours=168)}
        actual_select = selection.select
        def collect(chars, **kwargs):
            return actual_select(chars, now=NOW, io=fake_io(cache, []), **kwargs)
        with mock.patch('llm_router.config.load_policy', return_value=policy):
            with mock.patch('llm_router.selection.select', side_effect=collect):
                code, output = run(['select', '--role', 'planner', '--cached-only'])
        result = json.loads(output)
        self.assertEqual(code, 0)
        self.assertEqual(result['worker'], 'codex')
        self.assertEqual(result['role_pick']['worker'], result['allocation']['worker'])
        self.assertEqual(result['role_pick']['candidates'], result['eligible'])


class AllocationFeedTests(unittest.TestCase):
    setUp = test_live_jobs_feed.LiveJobsFeedTests.setUp

    def plan(self, worker):
        return {'as_of': NOW.isoformat(), 'role': 'coder', 'worker': worker, 'order': [worker],
                'scores': {}, 'reasons': {worker: 'fixed allowance'}, 'excluded': {}, 'actions': []}

    def feed(self):
        from llm_router import live_jobs_feed
        return live_jobs_feed.build_live_jobs_feed(policy=self.policy, now=test_live_jobs_feed.NOW)

    def test_finished_job_passes_minimal_allocation_and_history(self):
        plan = self.plan('codex')
        unsafe_plan = dict(plan, snapshot={'private_prompt': 'do not include'})
        data = {'job_id': 'repo-20261101-175500', 'repo': '/tmp/repo', 'workers': ['codex'],
                'started_at': '2026-11-01T17:55:00+00:00', 'finished_at': '2026-11-01T17:59:00+00:00',
                'ok': True, 'allocation': unsafe_plan, 'allocation_history': [unsafe_plan]}
        (self.base / 'jobs' / 'job.json').write_text(json.dumps(data))
        row = self.feed()['recent'][0]
        self.assertEqual(row['allocation'], plan)
        self.assertEqual(row['allocation_history'], [plan])
        self.assertNotIn('private_prompt', json.dumps(row))

    def test_running_numeric_latest_sidecar_is_visible_before_any_log(self):
        from llm_router import allocation_dispatch
        jid, stamp = 'repo-20261101-175500', '20261101-175500'
        (self.base / 'wt' / jid).mkdir()
        for attempt, worker in ((9, 'codex'), (10, 'cursor')):
            allocation_dispatch.write_sidecar(self.base / 'out', f'{stamp}-a{attempt}', {
                'allocation': self.plan(worker), 'allocation_history': [self.plan(worker)]})
        row = self.feed()['running'][0]
        self.assertEqual(row['allocation']['worker'], 'cursor')
        self.assertEqual(row['worker'], 'cursor')
        self.assertEqual(row['allocation_history'], [self.plan('cursor')])

    def test_sidecar_fallback_redaction_and_malformed_data_fail_soft(self):
        from llm_router import allocation_dispatch
        stamp, jid = '20261101-175500', 'repo-20261101-175500'
        data = {'job_id': jid, 'workers': ['codex'], 'ok': True,
                'finished_at': '2026-11-01T17:59:00+00:00'}
        (self.base / 'jobs' / 'job.json').write_text(json.dumps(data))
        plan = self.plan('codex')
        fabricated = 'sk-' + 'cD4e' * 8
        plan['reasons']['codex'] = 'allowance ' + fabricated
        sidecar = self.base / 'out' / (stamp + '-a1-allocation.json')
        sidecar.write_text(json.dumps({'allocation': plan, 'allocation_history': [plan]}))
        row = self.feed()['recent'][0]
        self.assertIn('allocation', row)
        self.assertNotIn(fabricated, json.dumps(row))
        for malformed in ('not json', '[]', '{"allocation": []}',
                          '{"allocation": {"order": "codex"}, "allocation_history": []}'):
            sidecar.write_text(malformed)
            row = self.feed()['recent'][0]
            self.assertNotIn('allocation', row)


class PersistedPlanShapeTests(unittest.TestCase):
    def test_malformed_nested_explanations_are_omitted(self):
        from llm_router.allocation_dispatch import allocation_fields
        from llm_router.allocation_policy import allocate
        base = allocate(snapshot())
        malformed = []
        for field, value in (('score', 'unknown'), ('score', float('nan')),
                             ('reason', {'private_prompt': 'not an explanation'})):
            plan = deepcopy(base)
            plan['scores']['codex'][field] = value
            malformed.append(plan)
        plan = deepcopy(base)
        plan['scored_order'] = 'codex'
        malformed.append(plan)
        plan = deepcopy(base)
        plan['actions'] = [{'reason': {'private_prompt': 'not an action'}}]
        malformed.append(plan)
        for plan in malformed:
            with self.subTest(plan=plan):
                self.assertEqual(allocation_fields({'allocation': plan}), {})
