import copy
import json
import os
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
        self.assertEqual(result['excluded_test_artifacts'], ['report.txt'])  # bytecode caches are ignored outright
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

    def test_branch_beats_same_named_tag(self):
        branch = git(self.repo, 'symbolic-ref', '--short', 'HEAD')
        older = git(self.repo, 'rev-parse', 'HEAD')
        (self.repo / 'f.txt').write_text('x')
        git(self.repo, 'add', 'f.txt')
        git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'two')
        newer = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, '-c', 'tag.gpgsign=false', 'tag', branch, older)  # a tag named like the branch, on the older commit
        self.assertEqual(git(self.repo, 'rev-parse', branch), older, 'git itself prefers the tag')
        self.assertEqual(jd.resolve_base(self.repo, branch), newer)
        self.assertEqual(jd.resolve_base(self.repo, 'HEAD'), newer)
        self.assertEqual(jd.resolve_base(self.repo, older[:10]), older)
        self.assertEqual(jd.resolve_base(self.repo, 'no-such-ref'), '')
        self.worker("open('out.txt','w').write('hi\\n')")
        self.assertEqual(self.job(base=branch, test_cmd='true')['base_sha'], newer)

    def test_tag_only_base_still_resolves(self):
        head = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, '-c', 'tag.gpgsign=false', 'tag', 'v1', head)
        self.assertEqual(jd.resolve_base(self.repo, 'v1'), head)

    def test_changed_paths_ignores_only_untracked_interpreter_caches(self):
        (self.repo / 'a.py').write_text('x = 1\n')
        (self.repo / 'pkg' / '__pycache__').mkdir(parents=True)
        (self.repo / 'pkg' / '__pycache__' / 'm.cpython-312.pyc').write_bytes(b'\0')
        (self.repo / 'stray.pyc').write_bytes(b'\0')  # importable as a sourceless module: a real change
        (self.repo / 'x__pycache__').mkdir()
        (self.repo / 'x__pycache__' / 'run.sh').write_text('true\n')  # a look-alike directory is not a cache
        (self.repo / '__pycache__').mkdir()
        (self.repo / '__pycache__' / 'notes.txt').write_text('n\n')  # only .pyc/.pyo files are caches
        self.assertEqual(jd.changed_paths(self.repo),
                         {'a.py', 'local-notes.txt', 'stray.pyc', 'x__pycache__/run.sh', '__pycache__/notes.txt'})

    def test_tracked_cache_changes_still_count(self):
        cache = self.repo / '__pycache__' / 'kept.cpython-312.pyc'
        cache.parent.mkdir()
        cache.write_bytes(b'\0')
        git(self.repo, 'add', '-f', str(cache))
        git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'tracked cache')
        cache.write_bytes(b'\1')
        self.assertIn('__pycache__/kept.cpython-312.pyc', jd.changed_paths(self.repo))

    def test_cache_removal_covers_ignored_symlinked_and_stray_bytecode(self):
        (self.repo / '.gitignore').write_text('__pycache__/\n*.pyc\n')
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'target.pyc').write_bytes(b'keep')
        (self.repo / 'pkg' / '__pycache__').mkdir(parents=True)
        ignored = self.repo / 'pkg' / '__pycache__' / 'm.cpython-312.pyc'
        ignored.write_bytes(b'planted')
        link = self.repo / 'pkg' / '__pycache__' / 'n.cpython-312.pyc'
        link.symlink_to(outside / 'target.pyc')
        (self.repo / 'lib').mkdir()
        (self.repo / 'lib' / '__pycache__').symlink_to(outside, target_is_directory=True)
        stray = self.repo / 'stray.pyc'
        stray.write_bytes(b'planted')
        jd.remove_bytecode_caches(self.repo)
        for path in (ignored, link, self.repo / 'lib' / '__pycache__', stray):
            self.assertFalse(os.path.lexists(path), path)
        self.assertEqual((outside / 'target.pyc').read_bytes(), b'keep', 'a link target is never touched')
        self.assertTrue((self.repo / '.gitignore').exists())

    def test_symlinked_cache_is_never_hidden_from_changed_paths(self):
        (self.base / 'elsewhere.pyc').write_bytes(b'x')
        (self.repo / '__pycache__').mkdir()
        (self.repo / '__pycache__' / 'm.cpython-312.pyc').symlink_to(self.base / 'elsewhere.pyc')
        self.assertIn('__pycache__/m.cpython-312.pyc', jd.changed_paths(self.repo))

    def test_worker_left_caches_are_removed_before_tests(self):
        self.worker("import os; os.makedirs('__pycache__', exist_ok=True); open('__pycache__/out.cpython-312.pyc','wb').write(b'planted'); "
                    "open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='test ! -e __pycache__/out.cpython-312.pyc')
        self.assertTrue(result['ok'], result)
        files = git(self.repo, 'show', '--name-only', '--format=', result['commit']).split()
        self.assertEqual(files, ['out.txt'])

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


class StackwichJobTests(unittest.TestCase):
    setUp = JobTests.setUp
    tearDown = JobTests.tearDown
    select = JobTests.select
    worker = JobTests.worker
    job = JobTests.job

    def supported(self, prefix='', suffix='', name='codex'):
        from llm_router import stackwich
        self.policy['run_task'].setdefault('stackwich', {})[name] = {'enabled': True, 'supported': True}
        code = ("import json,re,sys,pathlib; p=sys.stdin.read(); "
                "cycle=int(re.search(r'cycles equal to (\\d+)',p).group(1)); "
                + prefix + "open('out.txt','w').write('hi\\n'); "
                "pathlib.Path('.router-stackwich').mkdir(exist_ok=True); "
                f"r={stackwich.SCHEMA!r}; r['cycles']=cycle; " + suffix +
                "pathlib.Path('.router-stackwich/report.json').write_text(json.dumps(r))")
        self.worker(code, name)

    def test_verified_commit_excludes_report_and_has_trailer(self):
        self.supported()
        result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['stackwich']['status'], 'verified')
        self.assertEqual(git(self.repo, 'show', '--name-only', '--format=', result['commit']), 'out.txt')
        self.assertIn('Stackwich: verified', git(self.repo, 'log', '-1', '--format=%B', result['commit']))
        self.assertTrue((Path(result['worktree']) / '.router-stackwich/report.json').is_file())

    def test_corrected_second_cycle_is_pinned(self):
        self.supported(suffix="r['verify']['gate']='FAIL' if cycle==1 else 'PASS'; ")
        result = self.job(test_cmd='true')
        self.assertTrue(result['ok'], result)
        self.assertEqual([a['stackwich']['status'] for a in result['attempts']], ['blocked', 'verified'])
        self.assertEqual([a['worker'] for a in result['attempts']], ['codex', 'codex'])
        self.assertEqual(result['stackwich']['cycle'], 2)

    def test_two_failures_block_even_with_large_retry_setting(self):
        self.policy['run_job']['max_retries'] = 8
        self.supported(suffix="r['review']['verdict']='REVISE'; ")
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_stackwich', result)
        self.assertEqual(len(result['attempts']), 2)
        self.assertIsNone(result['commit'])
        self.assertEqual(result['worktree_state'], 'kept')

    def test_missing_or_failing_tests_block(self):
        for command in (None, 'false'):
            with self.subTest(command=command):
                self.supported()
                result = self.job(test_cmd=command)
                self.assertEqual(result['status'], 'blocked_by_stackwich', result)
                self.assertIsNone(result['commit'])

    def test_unsupported_fallback_has_no_trailer(self):
        self.policy['run_task'].setdefault('stackwich', {})['codex'] = {'enabled': True, 'supported': False}
        self.worker("open('out.txt','w').write('hi')")
        result = self.job(test_cmd='true')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['stackwich']['status'], 'unsupported')
        self.assertNotIn('Stackwich: verified', git(self.repo, 'log', '-1', '--format=%B', result['commit']))

    def test_custom_worker_is_neutral(self):
        self.policy['run_task']['commands']['sample'] = ['fake']
        self.policy['run_task']['timeouts_seconds']['sample'] = 60
        self.policy['unmetered_workers'].append('sample')
        self.supported(name='sample')
        result = self.job(test_cmd='true', worker='sample')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['workers'], ['sample'])

    def test_extra_report_directory_file_blocks_scope(self):
        self.supported(suffix="open('.router-stackwich/extra.txt','w').write('extra'); ")
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_stackwich', result)

    def test_report_or_product_mutated_by_tests_blocks(self):
        for command in ("echo changed > out.txt", "echo '{}' > .router-stackwich/report.json"):
            with self.subTest(command=command):
                self.supported()
                result = self.job(test_cmd=command)
                self.assertEqual(result['status'], 'blocked_by_stackwich', result)
                self.assertIsNone(result['commit'])

    def test_report_only_blocks(self):
        self.supported(suffix="pathlib.Path('out.txt').unlink(); ")
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_stackwich', result)
        self.assertIsNone(result['commit'])

    def test_worker_commit_blocks(self):
        self.supported(suffix="import subprocess; subprocess.run(['git','add','out.txt']); "
                       "subprocess.run(['git','-c','user.name=Worker','-c','user.email=worker@localhost',"
                       "'commit','-qm','worker commit']); ")
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_stackwich', result)
        self.assertIsNone(result['commit'])

    def test_required_worker_failure_does_not_use_unsupported_failover(self):
        self.supported(prefix='sys.exit(3); ')
        self.worker("open('bypass.txt','w').write('bypass')", 'grok-build')
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_stackwich', result)
        self.assertEqual(len(result['attempts']), 2)
        self.assertFalse((Path(result['worktree']) / 'bypass.txt').exists())

    def test_leak_blocks_immediately(self):
        self.supported(suffix="open('out.txt','w').write('Zephyrquill'); ")
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_leak_check', result)
        self.assertEqual(len(result['attempts']), 1)

    def test_report_leak_created_by_tests_stops_before_reading(self):
        self.supported()
        result = self.job(test_cmd="echo Zephyrquill > .router-stackwich/report.json")
        self.assertEqual(result['status'], 'blocked_by_leak_check', result)
        self.assertEqual(len(result['attempts']), 1)

    def test_rename_scope_includes_source_and_destination(self):
        (self.repo / 'old.txt').write_text('old')
        git(self.repo, 'add', 'old.txt')
        git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
            'commit', '-qm', 'fixture')
        self.supported(suffix="pathlib.Path('old.txt').rename('new.txt') if cycle==1 else None; ")
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_stackwich', result)
        self.assertIn('old.txt', jd.changed_paths(Path(result['worktree'])))
        self.assertIn('new.txt', jd.changed_paths(Path(result['worktree'])))

    def test_missing_metadata_cannot_downgrade_latched_gate(self):
        self.supported(suffix="r['verify']['gate']='FAIL'; ")
        calls = []
        def run_task(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return td.run_task(*args, **kwargs)
            return {'ok': True, 'worker': 'grok-build', 'leak_check': {'status': 'clean'}}
        result = self.job(test_cmd='true', run_task=run_task)
        self.assertEqual(result['status'], 'blocked_by_stackwich', result)
        self.assertEqual(calls[1]['worker'], 'codex')
        self.assertIsNone(result['commit'])

    def test_ignored_report_leak_is_still_blocked(self):
        self.supported(suffix="open('.gitignore','w').write('.router-stackwich/\\n'); r['plan']['steps']=['Zephyrquill']; ")
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_leak_check', result)
        self.assertEqual(len(result['attempts']), 1)
