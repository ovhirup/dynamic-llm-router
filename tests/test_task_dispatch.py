import copy
import csv
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import load_policy, TERM
from llm_router import task_dispatch as td

MARK = 'router-dispatch-timeout-marker'


def repo(path):
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    (path / 'README.md').write_text('hello\n')
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(path), '-c', 'user.name=t', '-c', 'user.email=t@t',
                    'commit', '-qm', 'init'], check=True)
    return path


class TaskDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = repo(base / 'target')
        self.task = base / 'task.md'
        self.task.write_text('Append a line to README.md.\n')
        self.out = base / 'out'
        self.policy = copy.deepcopy(load_policy())
        self.policy['run_task']['ledger'] = str(base / 'ledger.csv')
        self.ledger = base / 'ledger.csv'

    def tearDown(self):
        self.tmp.cleanup()

    def selection(self, eligible, meters=None, reason='preferred_eligible_worker'):
        meters = {'codex': 10, 'grok-build': 10, 'cursor': 10} if meters is None else meters
        return lambda chars, **kw: {'eligible': list(eligible), 'meters': meters, 'reason': reason}

    def fake_runner(self, results, calls):
        def runner(argv, cwd, timeout, grace, **kw):
            calls.append(argv)
            outcome = results.pop(0)
            if outcome == 'edit':
                (Path(cwd) / 'README.md').write_text('hello\nworld\n')
                return {'returncode': 0, 'timed_out': False, 'stdout': 'done', 'stderr': '', 'elapsed_seconds': 0.1}
            if outcome == 'quota':
                return {'returncode': 1, 'timed_out': False, 'stdout': '', 'stderr': 'Usage limit reached', 'elapsed_seconds': 0.1}
            return {'returncode': 1, 'timed_out': True, 'stdout': '', 'stderr': '', 'elapsed_seconds': 1}
        return runner

    def run_task(self, **kw):
        kw.setdefault('policy', self.policy)
        kw.setdefault('output_root', self.out)
        return td.run_task(self.task, self.repo, **kw)

    def test_refuses_non_repo_and_dirty_tree(self):
        with self.assertRaisesRegex(td.Refused, 'not a git repository'):
            td.check_repo(self.out.parent)
        (self.repo / 'README.md').write_text('dirty\n')
        with self.assertRaisesRegex(td.Refused, 'uncommitted'):
            self.run_task(select=self.selection(['codex']), runner=self.fake_runner([], []))
        self.assertTrue(td.check_repo(self.repo, allow_dirty=True))

    def test_auto_pick_edits_and_reports_diff_without_commit(self):
        calls = []
        result = self.run_task(select=self.selection(['codex', 'grok-build'], {'codex': 16}),
                               runner=self.fake_runner(['edit'], calls))
        self.assertTrue(result['ok'])
        self.assertEqual(result['worker'], 'codex')
        self.assertIn('README.md', result['diff_stat'])
        self.assertEqual(calls[0][1:4], ['exec', '-C', str(self.repo.resolve())])
        self.assertIn('features.multi_agent=false', calls[0])
        log = subprocess.run(['git', '-C', str(self.repo), 'log', '--oneline'], capture_output=True, text=True).stdout
        self.assertEqual(len(log.splitlines()), 1)
        with self.ledger.open() as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], ['date', 'runner', 'task', 'meter', 'used_percent', 'note'])
        self.assertEqual(rows[1][1], 'codex')
        self.assertEqual(rows[1][4], '16')

    def test_failover_once_on_quota_error(self):
        calls = []
        result = self.run_task(select=self.selection(['codex', 'grok-build', 'cursor']),
                               runner=self.fake_runner(['quota', 'edit'], calls))
        self.assertTrue(result['ok'])
        self.assertEqual([a['worker'] for a in result['attempts']], ['codex', 'grok-build'])
        self.assertTrue(result['attempts'][0]['quota_error'])
        self.assertIn('--cwd', calls[1])

    def test_only_one_failover(self):
        result = self.run_task(select=self.selection(['codex', 'grok-build', 'cursor']),
                               runner=self.fake_runner(['timeout', 'timeout', 'edit'], []))
        self.assertFalse(result['ok'])
        self.assertEqual(len(result['attempts']), 2)

    def test_forced_workers_and_never(self):
        for name in ('claude', 'grok-bot'):
            with self.assertRaises(td.Refused):
                self.run_task(worker=name, select=self.selection(['codex']))
        calls = []
        result = self.run_task(worker='grok-build', select=self.selection(['codex']),
                               runner=self.fake_runner(['edit'], calls))
        self.assertEqual(result['worker'], 'grok-build')
        self.assertEqual(result['reason'], 'forced')
        with self.assertRaisesRegex(td.Refused, 'draft'):
            self.run_task(worker='developer', size='full', select=self.selection([]))

    def test_cursor_argv(self):
        argv = td.build_argv(self.policy, 'cursor', self.repo, Path('/x/.git/router-prompts/t.md'))
        self.assertEqual(argv[1:5], ['-p', '--force', '--output-format', 'stream-json'])
        self.assertTrue(argv[-1].startswith('Your full task is in the file /x/.git/router-prompts/t.md'))
        self.assertEqual(td.build_argv(self.policy, 'codex', self.repo)[-1], '-')
        grok = td.build_argv(self.policy, 'grok-build', self.repo, Path('/p.md'))
        self.assertEqual(grok[grok.index('--prompt-file') + 1], '/p.md')
        with self.assertRaises(td.Refused):
            td.build_argv(self.policy, 'grok-build', self.repo)

    def test_pacing_refuse_and_warn(self):
        with self.assertRaisesRegex(td.Refused, 'refusing new full-size'):
            self.run_task(worker='codex', select=self.selection([], {'codex': 85}))
        result = self.run_task(select=self.selection(['codex'], {'codex': 80}),
                               runner=self.fake_runner(['edit'], []))
        self.assertTrue(any('warn at 70' in w for w in result['warnings']))
        subprocess.run(['git', '-C', str(self.repo), 'checkout', '-q', '.'], check=True)
        result = self.run_task(size='draft', worker='codex', select=self.selection([], {'codex': 90}),
                               runner=self.fake_runner(['edit'], []))
        self.assertTrue(result['ok'])

    def test_developer_draft_writes_file_only(self):
        result = self.run_task(size='draft', select=self.selection(['developer']),
                               draft=lambda policy, prompt: 'draft text')
        self.assertTrue(result['ok'])
        self.assertEqual(Path(result['output_path']).read_text(), 'draft text')
        self.assertEqual(result['diff_stat'], '')
        self.assertEqual(result['untracked'], [])

    def test_local_worker_capped_at_one(self):
        self.policy['run_task']['slot_dir'] = str(self.out.parent / 'slots')
        self.assertEqual(td.concurrency_cap(self.policy, 'developer'), 1)
        self.assertIsNone(td.concurrency_cap(self.policy, 'codex'))
        self.assertEqual(td.acquire_slot(self.policy, 'codex'), ('uncapped', None))
        state, held = td.acquire_slot(self.policy, 'developer')
        self.assertEqual(state, 'held')
        try:
            self.assertEqual(td.acquire_slot(self.policy, 'developer'), ('full', None))
            with self.assertRaisesRegex(td.Refused, 'concurrency cap'):
                self.run_task(size='draft', select=self.selection(['developer']),
                              draft=lambda policy, prompt: 'draft text')
        finally:
            held.close()
        result = self.run_task(size='draft', select=self.selection(['developer']),
                               draft=lambda policy, prompt: 'draft text')
        self.assertTrue(result['ok'])
        state, held = td.acquire_slot(self.policy, 'developer')  # the run released its slot
        self.assertEqual(state, 'held')
        held.close()

    def test_full_worker_is_skipped_for_next_eligible(self):
        self.policy['run_task']['slot_dir'] = str(self.out.parent / 'slots')
        self.policy['run_task']['concurrency']['codex'] = 2
        held = [td.acquire_slot(self.policy, 'codex')[1] for _ in range(2)]
        try:
            calls = []
            result = self.run_task(select=self.selection(['codex', 'grok-build']),
                                   runner=self.fake_runner(['edit'], calls))
            self.assertTrue(result['ok'])
            self.assertEqual([a['worker'] for a in result['attempts']], ['grok-build'])
            self.assertTrue(any('codex at its concurrency cap (2)' in w for w in result['warnings']))
        finally:
            for handle in held:
                handle.close()

    def test_slot_released_when_run_raises(self):
        self.policy['run_task']['slot_dir'] = str(self.out.parent / 'slots')
        self.policy['run_task']['concurrency']['codex'] = 1

        def boom(*args, **kw):
            raise RuntimeError('worker crashed')
        with self.assertRaises(RuntimeError):
            self.run_task(select=self.selection(['codex']), runner=boom)
        state, held = td.acquire_slot(self.policy, 'codex')
        self.assertEqual(state, 'held')
        held.close()

    def test_no_eligible_worker_refuses(self):
        with self.assertRaisesRegex(td.Refused, 'no eligible'):
            self.run_task(select=self.selection([], reason='no_eligible_worker'))

    def test_timeout_kills_process_group(self):
        child = f"import time; time.sleep(300)  # {MARK}-child"
        script = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(300)  # {MARK}"
        argv = [sys.executable, '-c', script]
        started = time.monotonic()
        result = td.run_bounded(argv, self.repo, timeout=1, grace=1)
        self.assertTrue(result['timed_out'])
        self.assertLess(time.monotonic() - started, 10)
        time.sleep(0.5)
        left = subprocess.run(['pgrep', '-f', MARK], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, '')

    def test_timeout_through_run_task_fails_over(self):
        policy = copy.deepcopy(self.policy)
        policy['run_task']['commands']['codex'] = [sys.executable, '-c', f'import time; time.sleep(60) # {MARK}']
        policy['run_task']['commands']['grok-build'] = [sys.executable, '-c',
                                                       "open('README.md','a').write('x\\n')"]
        policy['run_task']['timeouts_seconds']['codex'] = 1
        policy['run_task']['kill_grace_seconds'] = 1
        result = self.run_task(policy=policy, select=self.selection(['codex', 'grok-build']))
        self.assertTrue(result['attempts'][0]['timed_out'])
        self.assertTrue(result['ok'])
        self.assertEqual(result['worker'], 'grok-build')
        self.assertEqual(subprocess.run(['pgrep', '-f', MARK], capture_output=True, text=True).stdout.strip(), '')

    def grok_policy(self, code, timeout=None):
        policy = copy.deepcopy(self.policy)
        policy['run_task']['commands']['grok-build'] = [sys.executable, '-c', code]
        if timeout:
            policy['run_task']['timeouts_seconds']['grok-build'] = timeout
            policy['run_task']['kill_grace_seconds'] = 1
        return policy

    HOOK = "open('HANDOFF.md','w').write('## Machine snapshot\\n'); open('grok.txt','w').write('ok\\n')"

    def test_handoff_absent_is_deleted(self):
        result = self.run_task(worker='grok-build', policy=self.grok_policy(self.HOOK),
                               select=self.selection([]))
        self.assertTrue(result['ok'])
        self.assertEqual(result['handoff_cleanup'], {'before': 'absent', 'action': 'deleted_new'})
        self.assertFalse((self.repo / 'HANDOFF.md').exists())
        self.assertEqual(result['untracked'], ['grok.txt'])

    def test_handoff_tracked_and_untracked_restored_exactly(self):
        original = b'# mine\r\n\xe2\x9c\x93 no trailing newline'
        (self.repo / 'HANDOFF.md').write_bytes(original)
        subprocess.run(['git', '-C', str(self.repo), 'add', 'HANDOFF.md'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-qm', 'handoff'], check=True)
        result = self.run_task(worker='grok-build', policy=self.grok_policy(self.HOOK),
                               select=self.selection([]))
        self.assertEqual(result['handoff_cleanup'], {'before': 'tracked', 'action': 'restored'})
        self.assertEqual((self.repo / 'HANDOFF.md').read_bytes(), original)
        self.assertEqual(result['diff_stat'], '')
        (self.repo / 'grok.txt').unlink()
        subprocess.run(['git', '-C', str(self.repo), 'rm', '-q', '--cached', 'HANDOFF.md'], check=True)
        result = self.run_task(worker='grok-build', allow_dirty=True,
                               policy=self.grok_policy("import os; os.remove('HANDOFF.md')"),
                               select=self.selection([]))
        self.assertEqual(result['handoff_cleanup'], {'before': 'untracked', 'action': 'restored'})
        self.assertEqual((self.repo / 'HANDOFF.md').read_bytes(), original)
        result = self.run_task(worker='grok-build', allow_dirty=True, policy=self.grok_policy('pass'),
                               select=self.selection([]))
        self.assertEqual(result['handoff_cleanup']['action'], 'unchanged')

    def test_handoff_cleaned_on_timeout_and_failover(self):
        code = self.HOOK + f"; import time; time.sleep(60)  # {MARK}"
        policy = self.grok_policy(code, timeout=1)
        policy['run_task']['commands']['cursor'] = [sys.executable, '-c', "open('c.txt','w').write('c')"]
        result = self.run_task(policy=policy, select=self.selection(['grok-build', 'cursor']))
        self.assertTrue(result['attempts'][0]['timed_out'])
        self.assertEqual(result['worker'], 'cursor')
        self.assertEqual(result['handoff_cleanup'], {'before': 'absent', 'action': 'deleted_new'})
        self.assertFalse((self.repo / 'HANDOFF.md').exists())

    def test_handoff_cleaned_when_runner_raises(self):
        def boom(*args, **kw):
            (self.repo / 'HANDOFF.md').write_text('hook')
            raise OSError('spawn failed')
        with self.assertRaises(OSError):
            self.run_task(worker='grok-build', runner=boom, select=self.selection([]))
        self.assertFalse((self.repo / 'HANDOFF.md').exists())

    def test_handoff_untouched_for_other_workers(self):
        (self.repo / 'HANDOFF.md').write_text('codex wrote this')
        result = self.run_task(worker='codex', allow_dirty=True, select=self.selection([]),
                               runner=self.fake_runner(['edit'], []))
        self.assertEqual(result['handoff_cleanup'], 'not_applicable')
        self.assertEqual((self.repo / 'HANDOFF.md').read_text(), 'codex wrote this')


if __name__ == '__main__':
    unittest.main()
