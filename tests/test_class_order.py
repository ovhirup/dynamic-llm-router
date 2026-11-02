"""Per-class worker ranking (class_order) and the grok-bot orchestrator fallback (frozen clock)."""
import copy
from datetime import datetime, timedelta, timezone
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import load_policy
from llm_router import config, selection, task_dispatch as td

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
CLASS_ORDER = {
    'feature': ['grok-build', 'cursor', 'codex'],
    'bugfix': ['grok-build', 'codex', 'cursor'],
    'review': ['codex', 'grok-build'],
    'mechanical': ['cursor', 'codex', 'grok-build', 'grok-bot'],
    'draft': ['developer', 'cursor'],
}


def class_policy(**extra):
    # class ranking is under test, so grok-build's grok-bot pool is billed separately and on its week curve.
    extra.setdefault('grok_bot', {'billing': {'mode': 'separate'}, 'evidence': {
        'percent_used': 0, 'reset_at': (NOW + timedelta(days=6)).isoformat()}})
    policy = load_policy(class_order=CLASS_ORDER, **extra)
    policy['unmetered_workers'] = ['codex', 'grok-build', 'cursor']
    return policy


def cooled(*workers):
    return {w: {'until': (NOW + timedelta(hours=1)).isoformat()} for w in workers}


class ClassOrderTests(unittest.TestCase):
    def select(self, policy, task_class=None, cooldowns=None, chars=10000):
        return selection.select(chars, policy=policy, io={}, now=NOW, cooldowns=cooldowns or {},
                                task_class=task_class)

    def test_each_class_picks_its_own_first_worker(self):
        policy = class_policy()
        expected = {'feature': 'grok-build', 'bugfix': 'grok-build', 'review': 'codex',
                    'mechanical': 'cursor', 'draft': 'cursor'}  # developer is disabled by default
        for task_class, worker in expected.items():
            with self.subTest(task_class=task_class):
                result = self.select(policy, task_class)
                self.assertEqual(result['worker'], worker)
                self.assertEqual(result['eligible'][-1], 'grok-bot')
                self.assertEqual(result['task_class'], task_class)

    def test_each_class_falls_through_its_own_order(self):
        policy = class_policy()
        for task_class, order in CLASS_ORDER.items():
            order = [w for w in order if w not in ('developer', 'grok-bot')]
            with self.subTest(task_class=task_class):
                result = self.select(policy, task_class, cooldowns=cooled(order[0]))
                self.assertEqual(result['worker'], order[1] if len(order) > 1 else 'grok-bot')
                self.assertTrue(result['reasons'][order[0]].startswith('cooldown_until_'))

    def test_flat_worker_order_unchanged_without_class(self):
        policy = class_policy()
        result = self.select(policy)
        self.assertEqual(result['worker'], 'codex')
        self.assertNotIn('grok-bot', result['eligible'])
        self.assertIsNone(result['task_class'])

    def test_class_without_entry_uses_worker_order_then_grok_bot(self):
        policy = load_policy()
        policy['unmetered_workers'] = ['codex', 'grok-build', 'cursor']
        self.assertEqual(config.worker_order(policy, 'feature'),
                         ['codex', 'grok-build', 'cursor', 'developer', 'grok-bot'])

    def test_all_workers_out_hands_off_to_grok_bot(self):
        policy = class_policy()
        for task_class in config.TASK_CLASSES:
            with self.subTest(task_class=task_class):
                result = self.select(policy, task_class, cooldowns=cooled('codex', 'grok-build', 'cursor'))
                self.assertEqual(result['worker'], 'grok-bot')
                self.assertEqual(result['reason'], 'handoff_to_orchestrator')
                self.assertEqual(result['reasons']['grok-bot'], 'orchestrator_fallback')

    def test_grok_bot_is_never_quota_banned(self):
        policy = class_policy(ineligible={'grok-bot': 'test'})
        policy['ineligible'] = {'grok-bot': 'test'}
        result = self.select(policy, 'feature', cooldowns=cooled('grok-bot', 'codex', 'grok-build', 'cursor'))
        self.assertEqual(result['worker'], 'grok-bot')

    def test_config_validation(self):
        with self.assertRaisesRegex(config.ConfigError, 'must be last'):
            load_policy(class_order={'feature': ['grok-bot', 'codex']})
        with self.assertRaisesRegex(config.ConfigError, 'unknown class'):
            load_policy(class_order={'research': ['codex']})
        with self.assertRaisesRegex(config.ConfigError, 'unknown workers'):
            load_policy(class_order={'feature': ['nope']})
        with self.assertRaisesRegex(config.ConfigError, 'unique'):
            load_policy(class_order={'feature': ['codex', 'codex']})
        with self.assertRaisesRegex(config.ConfigError, 'unknown task class'):
            config.worker_order(load_policy(), 'research')


class OrchestratorHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.repo = base / 'target'
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        (self.repo / 'README.md').write_text('hello\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-qm', 'init'], check=True)
        self.task = base / 'task.md'
        self.task.write_text('Append a line to README.md.\n')
        self.policy = copy.deepcopy(class_policy())
        self.policy['run_task']['ledger'] = str(base / 'ledger.csv')
        self.out = base / 'out'

    def select(self, chars, **kw):
        return selection.select(chars, policy=self.policy, io={}, now=NOW,
                                cooldowns=cooled('codex', 'grok-build', 'cursor'), **kw)

    def test_all_workers_out_returns_handoff_not_refusal(self):
        def runner(*a, **kw):
            raise AssertionError('no worker may run on a handoff')
        result = td.run_task(self.task, self.repo, task_class='feature', select=self.select,
                             policy=self.policy, output_root=self.out, runner=runner)
        self.assertEqual(result['status'], 'handoff_to_orchestrator')
        self.assertEqual(result['worker'], 'grok-bot')
        self.assertEqual(result['task_file'], str(self.task.resolve()))
        self.assertEqual(result['attempts'], [])

    def test_without_class_all_out_is_still_refused(self):
        with self.assertRaisesRegex(td.Refused, 'no eligible'):
            td.run_task(self.task, self.repo, select=self.select, policy=self.policy, output_root=self.out)

    def test_bot_dispatch_end_to_end_handoff_and_draft_size(self):
        from llm_router import job_dispatch as jd
        calls = []

        def fake_run_job(repo, task_file, **kw):
            calls.append(kw)
            return {'ok': False, 'status': 'handoff_to_orchestrator', 'job_id': 'target-20261031-120000',
                    'workers': [], 'reason': 'no_eligible_worker', 'branch': 'router/task-20261031-120000',
                    'commit': None}

        res = jd.bot_dispatch(self.repo, self.task, task_class='draft', policy=self.policy,
                              run_job_fn=fake_run_job)
        self.assertEqual(calls[-1]['task_class'], 'draft')
        self.assertEqual(calls[-1]['size'], 'draft')
        self.assertEqual(res['action'], 'handoff_to_orchestrator')
        self.assertEqual(res['worker'], 'grok-bot')
        self.assertEqual(res['job_id'], 'target-20261031-120000')
        self.assertEqual(res['reason'], 'no_eligible_worker')


if __name__ == '__main__':
    unittest.main()
