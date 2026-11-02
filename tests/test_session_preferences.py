"""Deterministic setup preferences and private runtime override audit (#210)."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from helpers import load_policy
from llm_router import config
from llm_router.setup_wizard import build_profile
from test_usage_snapshot import NOW, snapshot


def policy(mode='always_ask', preferred='grok-bot', workers=('codex', 'grok-bot'),
           orchestrators=('claude', 'codex', 'cursor', 'grok-bot', 'cli'), **overrides):
    tools = {}
    for name in set(workers) | set(orchestrators):
        auth = 'not_required' if name == 'cli' else 'user_verified' if name == 'grok-bot' else 'verified'
        tools[name] = {'installed': name != 'grok-bot', 'auth': auth,
                       'requested_capabilities': {'worker': name in workers, 'orchestrator': name in orchestrators},
                       'smoke': {'passed': True, 'status': 'passed', 'auth': auth}}
    fragment = build_profile(tools, preferred_orchestrator=preferred,
                              switch_policy={'mode': mode, 'target': preferred if mode == 'always_use' else None},
                              **overrides)
    return config.deep_merge(config.defaults(), fragment)


def budgets(exhausted_bot=False):
    snap = snapshot(order=('codex', 'claude', 'grok-bot'))
    snap['providers']['codex']['windows']['weekly']['percent_used'] = 20
    snap['providers']['grok-bot']['windows']['weekly']['percent_used'] = 100 if exhausted_bot else 10
    snap['providers']['grok-bot']['banked_resets'] = [
        {'id': 'bank-1', 'count': 1, 'expires_at': (NOW + timedelta(hours=2)).isoformat()}]
    return {'usage_snapshot': snap, 'allocation': {
        'order': ['codex', 'claude'], 'worker': 'codex', 'actions': [
            {'provider': 'grok-bot', 'reset_id': 'bank-1', 'action': 'action_required', 'reason': 'recommend only'}]}}


class SessionPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'
        self.patch = mock.patch.object(config, 'state_dir', return_value=self.state)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def read_audit(self):
        path = self.state / 'logs' / 'setup-decisions.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_preview_is_pure_and_always_ask_retains_current_with_pending_proposal(self):
        from llm_router import session_preferences as prefs
        configured = policy()
        before = deepcopy(configured)
        with mock.patch.object(config, 'state_dir', side_effect=AssertionError('state read')):
            with mock.patch('llm_router.selection.select', side_effect=AssertionError('quota read')):
                result = prefs.preview(configured, env={'CODEX_SANDBOX': 'opaque'},
                                       current='grok-bot', selection_result=budgets())
        self.assertEqual(result['orchestrator'], 'grok-bot')
        self.assertEqual(result['proposed'], 'codex')
        self.assertTrue(result['confirmation_required'])
        self.assertFalse(result['switched'])
        self.assertEqual(configured, before)
        self.assertFalse(self.state.exists())

    def test_no_callback_never_prompts_or_silently_switches(self):
        from llm_router import session_preferences as prefs
        with mock.patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
            result = prefs.resolve_orchestrator(policy(), env={'CODEX_SANDBOX': 'opaque'},
                                                selection_result=budgets(), now=NOW)
        self.assertEqual(result['orchestrator'], 'grok-bot')
        self.assertTrue(result['confirmation_required'])
        self.assertFalse(self.read_audit()[0]['switched'])
        self.assertTrue(self.read_audit()[0]['reason'])

    def test_yes_keep_and_recommended_choices_get_complete_explanation(self):
        from llm_router import session_preferences as prefs
        for answer, expected in [('yes', 'cursor'), ('keep current', 'grok-bot'), ('use recommended', 'codex')]:
            with self.subTest(answer=answer):
                explanations = []
                def confirm(message):
                    explanations.append(message)
                    return answer
                result = prefs.resolve_orchestrator(policy(), env={'CURSOR_AGENT': 'opaque'},
                                                    selection_result=budgets(True), confirm=confirm,
                                                    now=NOW)
                self.assertEqual(result['orchestrator'], expected)
                self.assertIn('Recommendation: codex', explanations[0])
                self.assertIn('80', explanations[0])
                self.assertIn('reset', explanations[0].lower())
                self.assertIn('bank-1', explanations[0])
                # Reset the per-subtest current without leaking to another test.
                (self.state / 'logs' / 'setup-decisions.jsonl').unlink()

    def test_fixed_caller_role_and_explicit_precedence(self):
        from llm_router import session_preferences as prefs
        fixed = prefs.preview(policy('always_use', 'codex'), env={'CURSOR_AGENT': 'opaque'}, selection_result=budgets())
        self.assertEqual(fixed['orchestrator'], 'codex')
        caller = prefs.preview(policy('always_accept_caller'), env={'CURSOR_AGENT': 'opaque'}, selection_result=budgets())
        self.assertEqual(caller['orchestrator'], 'cursor')
        configured = policy('always_use', 'codex', orchestrator_overrides={
            'roles': {'judge': {'orchestrator': 'claude', 'reason': 'strong review'}}})
        role = prefs.preview(configured, role='judge', env={'CURSOR_AGENT': 'opaque'}, selection_result=budgets())
        self.assertEqual(role['orchestrator'], 'claude')
        self.assertIn('strong review', role['reason'])
        explicit = prefs.preview(configured, explicit='cursor', role='judge', env={}, selection_result=budgets())
        self.assertEqual(explicit['orchestrator'], 'cursor')

    def test_disabled_targets_rejected_and_disabled_caller_not_promoted(self):
        from llm_router import session_preferences as prefs
        configured = policy(orchestrators=('codex', 'grok-bot'))
        with self.assertRaises(config.ConfigError):
            prefs.preview(configured, explicit='cursor', env={}, selection_result=budgets())
        with self.assertRaises(config.ConfigError):
            prefs.resolve_worker(configured, explicit='cursor', audit=False)
        configured['setup']['orchestrator_overrides']['roles']['judge'] = {'orchestrator': 'cursor', 'reason': 'disabled'}
        with self.assertRaises(config.ConfigError):
            prefs.preview(configured, role='judge', env={}, selection_result=budgets())
        configured = policy(orchestrators=('codex', 'grok-bot'))
        result = prefs.preview(configured, env={'CURSOR_AGENT': 'opaque'}, selection_result=budgets())
        self.assertEqual(result['orchestrator'], 'grok-bot')
        self.assertIsNone(result['proposed'])

    def test_cli_detection_and_force_confirmation(self):
        from llm_router import session_preferences as prefs
        result = prefs.preview(policy('always_accept_caller'), env={}, selection_result=budgets())
        self.assertEqual(result['caller'], 'cli')
        self.assertEqual(result['orchestrator'], 'cli')
        result = prefs.preview(policy(), env={'LLM_ROUTER_USER_AGENT': 'CLI'}, selection_result=budgets())
        self.assertEqual(result['caller'], 'cli')
        pending = prefs.resolve_orchestrator(policy(), explicit='codex', force_confirmation=True,
                                             env={}, selection_result=budgets(), now=NOW)
        self.assertTrue(pending['confirmation_required'])
        self.assertEqual(pending['orchestrator'], 'grok-bot')

    def test_exhausted_preferred_recommendation_has_windows_resets_and_no_banked_capacity(self):
        from llm_router import session_preferences as prefs
        result = prefs.preview(policy('always_use', 'grok-bot'), env={}, selection_result=budgets(True))
        recommendation = result['recommendation']
        self.assertEqual(recommendation['orchestrator'], 'codex')
        self.assertEqual(result['orchestrator'], 'grok-bot')
        self.assertTrue(result['warnings'])
        self.assertTrue(recommendation['budgets']['grok-bot']['exhausted'])
        self.assertEqual(recommendation['budgets']['codex']['windows'][0]['percent_remaining'], 80)
        self.assertTrue(recommendation['budgets']['codex']['windows'][0]['reset_at'])
        self.assertEqual(recommendation['budgets']['grok-bot']['banked_resets'][0]['id'], 'bank-1')
        self.assertEqual(recommendation['actions'][0]['action'], 'action_required')

    def test_unknown_budget_is_explicit_and_no_remaining_allowance_is_invented(self):
        from llm_router import session_preferences as prefs
        result = prefs.preview(policy(), env={}, selection_result={})
        rec = result['recommendation']
        self.assertEqual(rec['orchestrator'], 'grok-bot')
        self.assertIn('unknown', rec['reason'])
        self.assertEqual(rec['budgets']['codex']['status'], 'unknown')
        self.assertIsNone(rec['budgets']['codex']['percent_remaining'])

    def test_current_persists_and_all_switches_are_audited_even_across_worker_events(self):
        from llm_router import session_preferences as prefs
        first = prefs.resolve_orchestrator(policy(), explicit='codex', reason='explicit coordination',
                                           env={}, selection_result=budgets(), now=NOW)
        self.assertEqual(first['orchestrator'], 'codex')
        prefs.resolve_worker(policy(), explicit='codex', reason='focused edit', now=NOW)
        pending = prefs.resolve_orchestrator(policy(), env={'CURSOR_AGENT': 'opaque'},
                                             selection_result=budgets(), now=NOW)
        self.assertEqual(pending['current'], 'codex')
        self.assertEqual(pending['orchestrator'], 'codex')
        accepted = prefs.resolve_orchestrator(policy(), env={'CURSOR_AGENT': 'opaque'},
                                              selection_result=budgets(), confirm=lambda message: 'yes', now=NOW)
        self.assertEqual(accepted['orchestrator'], 'cursor')
        events = [event for event in self.read_audit() if event['event'] == 'orchestrator_decision']
        self.assertEqual([event['switched'] for event in events], [True, False, True])
        self.assertTrue(all(event['reason'] for event in events))
        self.assertEqual(stat.S_IMODE((self.state / 'logs' / 'setup-decisions.jsonl').stat().st_mode), 0o600)

    def test_worker_precedence_and_bot_is_only_an_intent(self):
        from llm_router import session_preferences as prefs
        configured = policy(worker_overrides={
            'default': {'worker': 'codex', 'reason': 'bulk work'},
            'roles': {'coder': {'worker': 'grok-bot', 'reason': 'serial chat handback'}}})
        self.assertEqual(prefs.resolve_worker(configured, audit=False)['worker'], 'codex')
        self.assertEqual(prefs.resolve_worker(configured, role='coder', audit=False)['worker'], 'grok-bot')
        self.assertEqual(prefs.resolve_worker(configured, explicit='codex', role='coder', audit=False)['worker'], 'codex')
        self.assertFalse(self.state.exists())

    def test_default_collector_includes_orchestrator_only_tools_without_mutating_policy(self):
        from llm_router import session_preferences as prefs
        configured = policy(workers=(), orchestrators=('codex', 'grok-bot'))
        before = deepcopy(configured)
        with mock.patch('llm_router.selection.select', return_value=budgets()) as collect:
            prefs.resolve_orchestrator(configured, env={}, persist=False, now=NOW)
        copied = collect.call_args.kwargs['policy']
        self.assertNotIn('setup', copied)
        self.assertEqual(copied['worker_order'], ['codex', 'grok-bot'])
        self.assertTrue(collect.call_args.kwargs['cached_only'])
        self.assertEqual(configured, before)
        self.assertFalse(self.state.exists())

    def test_legacy_automatic_calls_preserve_reason_without_state_or_quota_io(self):
        from llm_router import session_preferences as prefs
        legacy = config.defaults()
        with mock.patch.object(config, 'state_dir', side_effect=AssertionError('state IO')):
            with mock.patch('llm_router.selection.select', side_effect=AssertionError('quota IO')):
                result = prefs.resolve_orchestrator(legacy, env={'CODEX_SANDBOX': 'opaque'})
                worker = prefs.resolve_worker(legacy)
        self.assertEqual(result['orchestrator'], 'codex')
        self.assertEqual(result['reason'], 'env:CODEX_SANDBOX')
        self.assertIsNone(worker['worker'])
        self.assertEqual(prefs.resolve_worker(legacy, explicit='registered-custom', audit=False)['worker'],
                         'registered-custom')
        explicit = prefs.resolve_orchestrator(legacy, explicit='claude', env={}, now=NOW)
        self.assertEqual(explicit['reason'], 'explicit_flag')
        self.assertIn('explicit --orchestrator', self.read_audit()[-1]['reason'])

    def test_redacted_whitelisted_audit_corrupt_log_and_nonpersisting_resolution(self):
        from llm_router import session_preferences as prefs
        fabricated = 'sk-' + 'aB3d' * 8
        path = prefs.audit_event(policy(), {'event': 'worker_override', 'worker': 'codex',
                                          'reason': 'override ' + fabricated, 'env': {'opaque': fabricated},
                                          'prompt': fabricated, 'usage_snapshot': {'secret': fabricated}}, now=NOW)
        raw = Path(path).read_text()
        self.assertNotIn(fabricated, raw)
        self.assertNotIn('prompt', raw)
        with Path(path).open('a') as handle:
            handle.write('corrupt record\n')
        pending = prefs.resolve_orchestrator(policy(), explicit='codex', persist=False,
                                             env={}, selection_result=budgets(), now=NOW)
        self.assertEqual(pending['orchestrator'], 'grok-bot')
        self.assertFalse(pending['switched'])
        self.assertEqual(Path(path).read_text(), raw + 'corrupt record\n')

    def test_concurrent_keep_retains_latest_locked_current(self):
        from llm_router import session_preferences as prefs
        configured = policy()
        def confirm(message):
            prefs.resolve_orchestrator(configured, explicit='claude', env={},
                                        selection_result=budgets(), now=NOW)
            return 'keep current'
        result = prefs.resolve_orchestrator(configured, env={'CURSOR_AGENT': 'opaque'},
                                            selection_result=budgets(), confirm=confirm, now=NOW)
        self.assertEqual(result['orchestrator'], 'claude')
        self.assertEqual(self.read_audit()[-1]['previous'], 'claude')
        self.assertFalse(self.read_audit()[-1]['switched'])

    def test_concurrent_pending_retains_latest_locked_current(self):
        from llm_router import session_preferences as prefs
        configured = policy()
        original = prefs.preview
        interleave_once = [True]
        def interleave(*args, **kwargs):
            decision = original(*args, **kwargs)
            if interleave_once[0]:
                interleave_once[0] = False
                prefs.resolve_orchestrator(configured, explicit='claude', env={},
                                            selection_result=budgets(), now=NOW)
            return decision
        with mock.patch.object(prefs, 'preview', side_effect=interleave):
            result = prefs.resolve_orchestrator(configured, env={'CURSOR_AGENT': 'opaque'},
                                                selection_result=budgets(), now=NOW)
        self.assertEqual(result['orchestrator'], 'claude')
        self.assertFalse(self.read_audit()[-1]['switched'])

    def test_incomplete_json_records_do_not_replace_last_valid_current(self):
        from llm_router import session_preferences as prefs
        configured = policy()
        prefs.resolve_orchestrator(configured, explicit='codex', env={}, selection_result=budgets(), now=NOW)
        path = self.state / 'logs' / 'setup-decisions.jsonl'
        with path.open('a') as handle:
            handle.write(json.dumps({'event': 'orchestrator_decision'}) + '\n')
        result = prefs.resolve_orchestrator(configured, env={'CURSOR_AGENT': 'opaque'},
                                            selection_result=budgets(), persist=False, now=NOW)
        self.assertEqual(result['current'], 'codex')

    def test_nested_audit_scalar_fields_are_refused_before_file_io(self):
        from llm_router import session_preferences as prefs
        with self.assertRaises(config.ConfigError):
            prefs.audit_event(policy(), {'event': 'worker_override', 'worker': 'codex',
                                          'reason': 'explicit override',
                                          'source': {'prompt': 'private content'}}, now=NOW)
        self.assertFalse(self.state.exists())

    def test_hardlinked_and_nonregular_current_logs_are_refused_before_reading(self):
        from llm_router import session_preferences as prefs
        import os
        target = Path(self.tmp.name) / 'unrelated.txt'
        target.write_text('unrelated content')
        path = self.state / 'logs' / 'setup-decisions.jsonl'
        path.parent.mkdir(parents=True)
        os.link(target, path)
        with mock.patch.object(prefs, '_latest', side_effect=AssertionError('content read')):
            with self.assertRaises(config.ConfigError):
                prefs.resolve_orchestrator(policy(), explicit='codex', env={},
                                            selection_result=budgets(), persist=False, now=NOW)
        path.unlink()
        path.mkdir()
        with mock.patch.object(prefs, '_latest', side_effect=AssertionError('content read')):
            with self.assertRaises(config.ConfigError):
                prefs.resolve_orchestrator(policy(), explicit='codex', env={},
                                            selection_result=budgets(), persist=False, now=NOW)

    def test_rich_and_scalar_recommendations_persist_the_same_readable_identifier(self):
        from llm_router import session_preferences as prefs
        records = []
        for recommendation in ({'orchestrator': 'codex', 'budgets': {'private': 'omit'}}, 'codex'):
            path = self.state / 'logs' / 'setup-decisions.jsonl'
            if path.exists():
                path.unlink()
            prefs.audit_event(policy(), {'event': 'orchestrator_decision', 'orchestrator': 'codex',
                                          'current': 'grok-bot', 'confirmed': True,
                                          'reason': 'explicit coordination',
                                          'recommendation': recommendation}, now=NOW)
            records.append(self.read_audit()[-1])
            result = prefs.resolve_orchestrator(policy(), env={}, selection_result=budgets(),
                                                persist=False, now=NOW)
            self.assertEqual(result['current'], 'codex')
            self.assertEqual(records[-1]['recommendation'], 'codex')
            self.assertNotIn('budgets', path.read_text())
        self.assertEqual(records[0], records[1])

    def test_invalid_recommendation_representations_are_refused_before_creation(self):
        from llm_router import session_preferences as prefs
        invalid = [True, False, [], 1, 'unsupported', {}, {'orchestrator': {'id': 'codex'}}]
        for index, recommendation in enumerate(invalid):
            destination = self.state / ('invalid-' + str(index))
            with self.subTest(index=index), mock.patch.object(config, 'state_dir', return_value=destination):
                with self.assertRaises(config.ConfigError):
                    prefs.audit_event(policy(), {'event': 'worker_override', 'worker': 'codex',
                                                  'reason': 'explicit override',
                                                  'recommendation': recommendation}, now=NOW)
                self.assertFalse(destination.exists())

    def test_null_current_and_malformed_previous_records_do_not_replace_valid_current(self):
        from llm_router import session_preferences as prefs
        configured = policy()
        prefs.resolve_orchestrator(configured, explicit='codex', env={}, selection_result=budgets(), now=NOW)
        valid = self.read_audit()[0]
        invalid = [{'event': 'orchestrator_decision'},
                   dict(valid, orchestrator=None),
                   dict(valid, orchestrator='claude', previous={'nested': 'codex'}),
                   dict(valid, orchestrator='claude', previous='unsupported')]
        path = self.state / 'logs' / 'setup-decisions.jsonl'
        for event in invalid:
            with path.open('a') as handle:
                handle.write(json.dumps(event) + '\n')
            result = prefs.resolve_orchestrator(configured, env={}, selection_result=budgets(),
                                                persist=False, now=NOW)
            self.assertEqual(result['current'], 'codex')
