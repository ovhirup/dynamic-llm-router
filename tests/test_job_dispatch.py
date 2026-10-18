import copy
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import load_policy, TERM
from llm_router import job_dispatch as jd
from llm_router import task_dispatch as td
from test_task_dispatch import repo


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True).stdout.strip()


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.base = base
        self.repo = repo(base / 'main-repo')
        (self.repo / 'local-notes.txt').write_text('user work in progress\n')  # dirty main checkout stays put
        self.task = base / 'add-feature.md'
        self.task.write_text('# Add out.txt\nCreate out.txt.\n')
        policy = copy.deepcopy(load_policy())
        rt, rj = policy['run_task'], policy['run_job']
        rt['ledger'] = str(base / 'ledger.csv')
        rt['output_dir'] = str(base / 'out')
        rt['private_terms_file'] = str(base / 'terms.txt')
        rt['block_log'] = str(base / 'block.log')
        rt['shim_dir'] = str(base / 'shims')
        (base / 'terms.txt').write_text('Zephyrquill\n')
        rj.update(worktree_root=str(base / 'wt'), lock_dir=str(base / 'locks'), jobs_dir=str(base / 'jobs'),
                  repo_config=str(base / 'repos.json'))
        policy['alerts'] = {'file': str(base / 'alerts.jsonl'), 'summary': str(base / 'ALERTS.md'),
                            'macos_notification': False}
        rj['caffeinate'] = False
        self.policy = policy
        # Never reach a real CLI from unit tests: every worker starts as a failing fake.
        for name in ('codex', 'grok-build', 'cursor'):
            self.worker("import sys; sys.exit(3)", name)

    def tearDown(self):
        self.tmp.cleanup()

    def select(self, chars, **kw):
        return {'eligible': ['codex', 'grok-build'], 'meters': {'codex': 10, 'grok-build': 10}, 'reason': 'test'}

    def worker(self, code, name='codex'):
        tail = [a for a in self.policy['run_task']['commands'][name] if a in ('-', '{prompt_file}', '{prompt_pointer}')]
        self.policy['run_task']['commands'][name] = [sys.executable, '-c', code] + tail

    def job(self, **kw):
        kw.setdefault('select', self.select)
        return jd.run_job(self.repo, self.task, policy=self.policy, **kw)

    def assert_main_untouched(self, before):
        self.assertEqual(jd.main_state(self.repo), before)
        self.assertEqual((self.repo / 'local-notes.txt').read_text(), 'user work in progress\n')
        self.assertFalse((self.repo / 'out.txt').exists())

    def test_success_commits_on_router_branch_only(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        before = jd.main_state(self.repo)
        result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'committed')
        self.assertTrue(result['branch'].startswith('router/add-feature-'))
        self.assertEqual(git(self.repo, 'rev-parse', result['branch']), result['commit'])
        self.assertEqual(git(self.repo, 'rev-parse', 'HEAD'), result['base_sha'])
        msg = git(self.repo, 'log', '-1', '--format=%B', result['commit'])
        self.assertTrue(msg.startswith('router: Add out.txt'))
        self.assertIn('Router-Job: ' + result['job_id'], msg)
        self.assertIn('out.txt', result['diff_stat'])
        self.assertEqual((result['tests_status'], result['worktree_state'], result['branch_state']),
                         ('passed', 'kept', 'kept'))
        self.assertTrue(Path(result['worktree']).is_dir())
        self.assertTrue(result['main_checkout_untouched'])
        self.assertEqual(result['leak_check']['status'], 'clean')
        self.assert_main_untouched(before)
        self.assertTrue((self.base / 'jobs' / f'{result["job_id"]}.json').exists())
        self.assertIn(',run-job,', (self.base / 'ledger.csv').read_text())

    def test_retry_with_test_output_then_pass(self):
        capture = self.base / 'prompts.txt'
        self.worker("import sys; p=sys.stdin.read(); open(%r,'a').write(p+'\\n=====\\n'); "
                    "open('n.txt','a').write('x\\n')" % str(capture))
        result = self.job(test_cmd='echo FAILMARK-lines=$(wc -l < n.txt); test $(wc -l < n.txt) -ge 2')
        self.assertTrue(result['ok'], result)
        self.assertEqual([a['tests'] for a in result['attempts']], ['failed', 'passed'])
        second = capture.read_text().split('=====')[1]
        self.assertIn('Previous attempt did not pass', second)
        self.assertRegex(second, r'FAILMARK-lines= *1')

    def test_lock_conflict_refused_without_side_effects(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        repo_path = Path(git(self.repo, 'rev-parse', '--show-toplevel')).resolve()
        before_branches = git(self.repo, 'branch', '--list')
        with jd.RepoLock(self.base / 'locks', repo_path):
            with self.assertRaisesRegex(jd.Refused, 'locked by another run-job'):
                self.job(test_cmd='true')
        self.assertEqual(git(self.repo, 'branch', '--list'), before_branches)
        self.assertFalse((self.base / 'wt').exists() and any((self.base / 'wt').iterdir()))

    def test_total_failure_prunes_worktree_and_deletes_empty_branch(self):
        self.worker("import sys; sys.exit(1)")
        before = jd.main_state(self.repo)
        result = self.job(test_cmd='true', worker='codex')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual((result['worktree_state'], result['branch_state']), ('pruned', 'deleted'))
        self.assertFalse(Path(result['worktree']).exists())
        self.assertEqual(git(self.repo, 'branch', '--list', result['branch']), '')
        self.assert_main_untouched(before)

    def test_keep_on_failure(self):
        self.worker("import sys; sys.exit(1)")
        result = self.job(test_cmd='true', keep=True)
        self.assertEqual(result['worktree_state'], 'kept')
        self.assertTrue(Path(result['worktree']).exists())

    def test_prune_on_success_keeps_committed_branch(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='true', keep=False)
        self.assertTrue(result['ok'])
        self.assertEqual((result['worktree_state'], result['branch_state']), ('pruned', 'kept'))
        self.assertEqual(git(self.repo, 'rev-parse', result['branch']), result['commit'])

    def test_leak_hit_blocks_commit_and_keeps_worktree(self):
        self.worker("open('out.txt','w').write('for Zephyrquill\\n')")
        result = self.job(test_cmd='true')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'blocked_by_leak_check')
        self.assertIsNone(result['commit'])
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['worktree_state'], 'kept')
        self.assertTrue((Path(result['worktree']) / 'out.txt').exists())
        self.assertEqual(git(self.repo, 'rev-parse', result['branch']), result['base_sha'])

    def test_test_artifacts_never_committed(self):
        self.worker("open('out.txt','w').write('ok\\n')")
        result = self.job(test_cmd='echo Zephyrquill > report.txt; mkdir -p __pycache__; echo b > __pycache__/x.pyc')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['excluded_test_artifacts'], ['__pycache__/x.pyc', 'report.txt'])
        files = git(self.repo, 'show', '--name-only', '--format=', result['commit']).split()
        self.assertEqual(files, ['out.txt'])
        self.assertEqual(result['leak_check']['status'], 'clean')

    def test_worker_leak_in_staged_change_blocks_commit(self):
        self.worker("open('a.txt','w').write('fine\\n')")
        policy_terms = self.base / 'terms.txt'
        # Term appears only once the worker's change is staged: run-task scan is clean for a.txt,
        # then the term list grows before commit, so the staged re-scan must catch it.
        result = self.job(test_cmd=f"printf 'fine\\n' > /dev/null; echo fine >> {policy_terms}")
        self.assertEqual(result['status'], 'blocked_by_leak_check')
        self.assertIsNone(result['commit'])
        self.assertEqual(git(Path(result['worktree']), 'diff', '--cached', '--name-only'), '')

    def test_no_change_is_failure(self):
        self.worker("pass")
        result = self.job(test_cmd='true')
        self.assertFalse(result['ok'])
        self.assertEqual([a['changed'] for a in result['attempts']], [False, False])

    def test_test_cmd_from_repo_config_and_refusals(self):
        (self.base / 'repos.json').write_text(json.dumps({'main-repo': {'test_cmd': 'test -f out.txt'}}))
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job()
        self.assertEqual(result['tests'][0]['command'], 'test -f out.txt')
        with self.assertRaisesRegex(jd.Refused, 'already exists'):
            self.job(branch=result['branch'])
        with self.assertRaisesRegex(jd.Refused, 'not a commit'):
            self.job(base='nope')
        with self.assertRaisesRegex(jd.Refused, 'invalid branch'):
            self.job(branch='bad..name')
        with self.assertRaisesRegex(jd.Refused, 'not a git'):
            jd.run_job(self.base, self.task, policy=self.policy)

    def test_same_second_jobs_get_distinct_worktrees(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        fixed = td.now_local()
        first = self.job(test_cmd='true', now=fixed)
        second = self.job(test_cmd='true', now=fixed, branch='router/second')
        self.assertNotEqual(first['worktree'], second['worktree'])
        self.assertTrue(second['job_id'].endswith('-2'))

    def test_failed_and_leak_jobs_raise_one_alert_each(self):
        from llm_router import alerts
        self.worker("import sys; sys.exit(1)")
        failed = self.job(test_cmd='true')
        self.worker("open('out.txt','w').write('Zephyrquill\\n')")
        leaked = self.job(test_cmd='true')
        self.worker("open('ok.txt','w').write('fine\\n')")
        self.job(test_cmd='true')
        pending = alerts.open_alerts(self.policy)
        self.assertEqual(sorted(a['key'] for a in pending),
                         sorted([f'job:{failed["job_id"]}:failed', f'job:{leaked["job_id"]}:blocked_by_leak_check']))
        self.assertNotIn('Zephyrquill', (self.base / 'alerts.jsonl').read_text())

    @unittest.skipUnless(Path('/usr/bin/caffeinate').exists(), 'macOS only')
    def test_caffeinate_wraps_job_only(self):
        self.policy['run_job']['caffeinate'] = True
        calls = []

        class Fake:
            def __init__(self, argv, **kw):
                calls.append(argv)
                self.terminated = False

            def terminate(self):
                calls.append('terminate')

            def wait(self, timeout=None):
                return 0
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='true', popen=Fake)
        self.assertTrue(result['ok'])
        self.assertEqual(calls[0][:3], ['/usr/bin/caffeinate', '-i', '-w'])
        self.assertEqual(calls[-1], 'terminate')

    def test_jobs_and_cleanup(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        done = self.job(test_cmd='true')
        listing = jd.list_jobs(policy=self.policy)
        self.assertEqual(listing['jobs'][0]['job_id'], done['job_id'])
        self.assertTrue(listing['jobs'][0]['worktree_exists'])
        dry = jd.cleanup('0m', dry_run=True, policy=self.policy, now=None)
        self.assertEqual(len(dry['removed']), 1)
        self.assertTrue(Path(done['worktree']).exists())
        report = jd.cleanup('0m', policy=self.policy)
        self.assertFalse(Path(done['worktree']).exists())
        self.assertEqual(report['removed'][0]['branch_action'], 'kept')
        self.assertEqual(git(self.repo, 'rev-parse', done['branch']), done['commit'])
        with self.assertRaises(jd.Refused):
            jd.parse_age('soon')

    def test_cleanup_removes_worktree_with_only_test_artifacts(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        done = self.job(test_cmd='echo x > artifact.log')
        self.assertEqual(done['excluded_test_artifacts'], ['artifact.log'])
        report = jd.cleanup('0m', policy=self.policy)
        self.assertEqual(len(report['removed']), 1)
        self.assertFalse(Path(done['worktree']).exists())

    def test_cleanup_skips_dirty_worktree(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        done = self.job(test_cmd='true')
        (Path(done['worktree']) / 'scratch.txt').write_text('wip')
        report = jd.cleanup('0m', policy=self.policy)
        self.assertEqual(report['skipped'][0]['why'], 'uncommitted changes')
        self.assertTrue(Path(done['worktree']).exists())


class WatchdogAndPacingTests(unittest.TestCase):
    def test_stall_watchdog_kills_silent_worker(self):
        code = "import sys,time; print('started', flush=True); time.sleep(60)  # stall-marker-x1"
        started = time.monotonic()
        result = td.run_bounded([sys.executable, '-c', code], '.', timeout=60, grace=1, stall_seconds=1)
        self.assertTrue(result['stalled'])
        self.assertTrue(result['timed_out'])
        self.assertLess(time.monotonic() - started, 6)
        self.assertIn('started', result['stdout'])
        time.sleep(0.3)
        self.assertEqual(subprocess.run(['pgrep', '-f', 'stall-marker-x1'], capture_output=True, text=True).stdout, '')

    def test_chatty_worker_not_stalled(self):
        code = "import time\nfor i in range(6):\n    print(i, flush=True); time.sleep(0.3)"
        result = td.run_bounded([sys.executable, '-c', code], '.', timeout=30, grace=1, stall_seconds=1)
        self.assertFalse(result['stalled'])
        self.assertEqual(result['returncode'], 0)

    def test_stdin_delivery_still_works(self):
        code = "import sys; print(sys.stdin.read().upper())"
        result = td.run_bounded([sys.executable, '-c', code], '.', timeout=10, input_text='abc')
        self.assertIn('ABC', result['stdout'])

    def test_forced_cursor_reads_its_meter_and_is_paced(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = repo(Path(tmp) / 't')
            task = Path(tmp) / 'task.md'
            task.write_text('x')
            policy = copy.deepcopy(load_policy())
            seen = {}

            def select(chars, **kw):
                seen.update(kw)
                return {'eligible': [], 'meters': {'cursor': None}, 'reason': 'test'}
            with self.assertRaisesRegex(td.Refused, 'meter unknown'):
                td.run_task(task, target, worker='cursor', select=select, policy=policy)
            self.assertEqual(seen, {'consult_cursor': True})
            with self.assertRaisesRegex(td.Refused, 'refusing new full-size'):
                td.run_task(task, target, worker='cursor', policy=policy,
                            select=lambda c, **k: {'eligible': [], 'meters': {'cursor': 100}, 'reason': 't'})


class LedgerLockTests(unittest.TestCase):
    def test_concurrent_appends_stay_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'ledger.csv'
            threads = [threading.Thread(target=lambda n=n: [td.append_ledger(path, [n, i, 'x' * 200, '', '', ''])
                                                            for i in range(40)]) for n in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            lines = path.read_text().splitlines()
            self.assertEqual(lines[0], 'date,runner,task,meter,used_percent,note')
            self.assertEqual(len(lines), 201)
            self.assertTrue(all(len(line.split(',')) == 6 for line in lines))


if __name__ == '__main__':
    unittest.main()
