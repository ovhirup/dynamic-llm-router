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
from llm_router import cooldown, task_dispatch as td
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

    def test_diff_scope_gate_blocks_extra_file_and_allows_in_scope_edits(self):
        self.task.write_text('# Scoped task\nCreate out.txt only.\n\n## Allowed paths\n- out.txt\n- docs/\n')
        self.worker("open('out.txt','w').write('ok\\n'); open('rogue.txt','w').write('extra\\n')")
        before = jd.main_state(self.repo)
        result = self.job(test_cmd='test -f out.txt')
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'diff_out_of_scope')
        self.assertEqual(result['reason'], 'diff_out_of_scope')
        self.assertIsNone(result['commit'])
        self.assertEqual(result['diff_scope']['out_of_scope'], ['rogue.txt'])
        self.assertEqual(result['allowed_paths'], ['docs/', 'out.txt'])
        self.assert_main_untouched(before)

        # In-scope edit (including inside a declared directory) passes and commits.
        self.worker("import pathlib; pathlib.Path('docs').mkdir(exist_ok=True); "
                    "open('out.txt','w').write('ok\\n'); open('docs/guide.md','w').write('hi\\n')")
        ok_res = self.job(test_cmd='test -f out.txt')
        self.assertTrue(ok_res['ok'], ok_res)
        self.assertEqual(ok_res['status'], 'committed')
        self.assertEqual(ok_res['diff_scope']['status'], 'ok')
        self.assertEqual(ok_res['diff_scope']['out_of_scope'], [])

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

    def select_all(self, chars, **kw):
        return {'eligible': ['codex', 'grok-bot', 'grok-build', 'cursor'],
                'meters': {'codex': 10, 'grok-build': 10, 'cursor': 10}, 'reason': 'test'}

    def test_quota_error_fails_over_to_next_worker(self):
        self.worker("import sys; print('Usage limit reached', file=sys.stderr); sys.exit(1)")
        self.worker("open('out.txt','w').write('hi\\n')", 'grok-build')
        result = self.job(test_cmd='test -f out.txt', select=self.select_all)
        self.assertTrue(result['ok'], result)
        runs = result['attempts'][0]['runs']
        self.assertEqual([(r['worker'], r['reason']) for r in runs],
                         [('codex', 'quota_error'), ('grok-build', 'ok')])
        self.assertIn('codex', cooldown.active())

    def test_three_failed_workers_end_the_job(self):
        result = self.job(test_cmd='true', select=self.select_all)
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'failed')
        runs = [r for a in result['attempts'] for r in a.get('runs') or []]
        self.assertEqual([r['worker'] for r in runs], ['codex', 'grok-build', 'cursor'])
        self.assertTrue(all(r['reason'] not in (None, 'ok') for r in runs), runs)

    def test_failed_worker_exit_1_saves_root_cause_in_job_json(self):
        # Issue #14: an exit-1 worker with stderr must not leave the job log without a reason.
        for name in ('codex', 'grok-build'):
            self.worker("import sys; print('loading model', file=sys.stderr); "
                        "print('Error: model qwen2.5-coder:14b not found', file=sys.stderr); sys.exit(1)", name)
        result = self.job(test_cmd='true')
        self.assertFalse(result['ok'])
        saved = json.loads((self.base / 'jobs' / f'{result["job_id"]}.json').read_text())
        for doc in (result, saved):
            self.assertEqual(doc['failure_reason'], 'Error: model qwen2.5-coder:14b not found')
            self.assertIn('loading model', doc['stderr_tail'])
            self.assertIn('not found', doc['error'])
            for attempt in [a for a in doc['attempts'] if a.get('runs')]:
                self.assertEqual(attempt['failure_reason'], 'Error: model qwen2.5-coder:14b not found')
                self.assertIn('not found', attempt['stderr_tail'])
                self.assertTrue(all(r['returncode'] == 1 and 'not found' in r['stderr_tail']
                                    for r in attempt['runs']))

    def test_failed_worker_is_not_retried_in_the_next_cycle(self):
        calls = []
        def run_task(*args, **kwargs):
            calls.append(kwargs)
            return td.run_task(*args, **kwargs)
        result = self.job(test_cmd='true', run_task=run_task)  # eligible: codex, grok-build
        self.assertFalse(result['ok'])
        self.assertEqual([r['worker'] for r in result['attempts'][0]['runs']], ['codex', 'grok-build'])
        self.assertEqual(calls[1]['exclude'], ['codex', 'grok-build'])
        self.assertIn('no eligible worker', result['attempts'][1]['refused'])  # nobody re-runs

    def test_auth_error_marks_worker_unavailable_and_fails_over(self):
        self.worker("import sys; print('Failed to authenticate: OAuth session expired and could not be "
                    "refreshed', file=sys.stderr); sys.exit(1)")
        self.worker("open('out.txt','w').write('hi\\n')", 'grok-build')
        result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        runs = result['attempts'][0]['runs']
        self.assertEqual([(r['worker'], r['reason']) for r in runs], [('codex', 'auth_error'), ('grok-build', 'ok')])
        self.assertTrue(runs[0]['unavailable'])
        self.assertFalse(runs[0]['quota_error'])
        self.assertEqual(cooldown.active()['codex']['reason'] if isinstance(cooldown.active().get('codex'), dict)
                         else 'auth_error', 'auth_error')
        self.assertEqual([(h['worker'], h['reason'], h['unavailable']) for h in result['hops']],
                         [('codex', 'auth_error', True), ('grok-build', 'ok', False)])

    def test_live_case_auth_then_timeout_fails_over_to_third_worker(self):
        # 31 Oct: claude auth failed in 3s, aider timed out, and the router refused the retry.
        self.worker("import sys; print('OAuth session expired', file=sys.stderr); sys.exit(1)")
        self.worker("import time, sys; print('quota exhausted? no, just slow'); sys.stdout.flush(); time.sleep(30)",
                    'grok-build')
        self.policy['run_task']['timeouts_seconds']['grok-build'] = 1
        self.worker("open('out.txt','w').write('hi\\n')", 'cursor')
        result = self.job(test_cmd='test -f out.txt', select=self.select_all)
        self.assertTrue(result['ok'], result)
        self.assertEqual([(h['worker'], h['reason']) for h in result['hops']],
                         [('codex', 'auth_error'), ('grok-build', 'timeout'), ('cursor', 'ok')])
        self.assertNotIn('grok-bot', [h['worker'] for h in result['hops']])
        self.assertNotIn('grok-build', cooldown.active())  # a timeout is not a quota cooldown

    def test_exhausted_failover_is_reported_not_refused(self):
        self.worker("import sys; print('OAuth session expired', file=sys.stderr); sys.exit(1)")
        result = self.job(test_cmd='true')  # eligible: codex, grok-build (both fail)
        self.assertFalse(result['ok'])
        self.assertEqual((result['status'], result['failover']), ('failed', 'exhausted'))
        self.assertEqual([h['worker'] for h in result['hops']], ['codex', 'grok-build'])
        self.assertTrue(any('failover exhausted' in w for w in result['warnings']), result['warnings'])

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

    def test_config_test_cmd_is_recorded_with_its_source(self):
        (self.base / 'repos.json').write_text(json.dumps({'main-repo': {'test_cmd': 'test -f out.txt'}}))
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job()
        self.assertTrue(result['ok'], result)
        self.assertEqual((result['test_command'], result['test_command_source']), ('test -f out.txt', 'config'))
        self.assertEqual(result['tests'][0]['source'], 'config')
        saved = json.loads((self.base / 'jobs' / f'{result["job_id"]}.json').read_text())
        self.assertEqual((saved['test_command'], saved['test_command_source']), ('test -f out.txt', 'config'))
        cli = self.job(test_cmd='true', branch='router/cli-wins')
        self.assertEqual((cli['test_command'], cli['test_command_source']), ('true', 'cli'))

    def commit_files(self, files):
        for name, text in files.items():
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text(text)
        subprocess.run(['git', '-C', str(self.repo), 'add', *files], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-qm', 'project files'], check=True)

    def test_auto_detects_python_unittest(self):
        self.commit_files({'src/pkg/__init__.py': '', 'tests/test_out.py':
                           'import os, unittest\nclass T(unittest.TestCase):\n'
                           '    def test_out(self):\n        self.assertTrue(os.path.exists("out.txt"))\n'})
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['test_command_source'], 'auto')
        self.assertTrue(result['test_command'].startswith('PYTHONPATH=src '), result['test_command'])
        self.assertTrue(result['test_command'].endswith('-m unittest discover -s tests'))
        self.assertEqual(result['tests_status'], 'passed')

    def test_detect_prefers_pytest_and_repo_venv(self):
        root = self.base / 'py'
        (root / 'tests').mkdir(parents=True)
        self.assertEqual(jd.detect_test_cmd(root), f'{sys.executable} -m unittest discover -s tests')
        (root / 'pyproject.toml').write_text('[tool.pytest.ini_options]\n')
        source = self.base / 'source'
        (source / '.venv' / 'bin').mkdir(parents=True)
        (source / '.venv' / 'bin' / 'python').write_text('')
        self.assertEqual(jd.detect_test_cmd(root, source), f'{source}/.venv/bin/python -m pytest')

    def test_detect_node_and_swift(self):
        root = self.base / 'proj'
        root.mkdir()
        self.assertIsNone(jd.detect_test_cmd(root))
        (root / 'package.json').write_text(json.dumps({'scripts': {'test': 'echo "Error: no test specified" && exit 1'}}))
        self.assertIsNone(jd.detect_test_cmd(root))
        (root / 'Package.swift').write_text('')
        self.assertEqual(jd.detect_test_cmd(root), 'swift test')
        (root / 'package.json').write_text(json.dumps({'scripts': {'test': 'node --test'}}))
        self.assertEqual(jd.detect_test_cmd(root), 'npm test --silent')

    def test_auto_detects_node_in_job(self):
        self.commit_files({'package.json': json.dumps({'scripts': {'test': 'test -f out.txt'}})})
        self.worker("open('out.txt','w').write('hi\\n')")
        calls = []

        def runner(argv, cwd, *a, **kw):
            calls.append(argv)
            return {'returncode': 0, 'timed_out': False, 'stdout': '', 'stderr': '', 'elapsed_seconds': 0}
        result = self.job(test_runner=runner)
        self.assertTrue(result['ok'], result)
        self.assertEqual((result['test_command'], result['test_command_source']), ('npm test --silent', 'auto'))
        self.assertEqual(calls, [['/bin/sh', '-c', 'npm test --silent']])

    def test_no_tests_without_opt_in_fails_and_keeps_branch(self):
        marker = self.base / 'worker-ran'
        self.worker(f"open({str(marker)!r},'w').write('x'); open('out.txt','w').write('hi\\n')")
        result = self.job()
        self.assertFalse(result['ok'])
        self.assertEqual((result['status'], result['tests_status']), ('no_tests', 'no_tests'))
        self.assertEqual(result['reason'], 'no test command configured or detected')
        self.assertIsNone(result['commit'])
        self.assertIsNone(result['test_command_source'])
        self.assertEqual((result['worktree_state'], result['branch_state']), ('kept', 'kept'))
        self.assertNotEqual(git(self.repo, 'branch', '--list', result['branch']), '')
        self.assertFalse(marker.exists())  # no worker run is spent on a change nothing would verify

    def test_no_tests_allowed_only_on_opt_in(self):
        (self.base / 'repos.json').write_text(json.dumps({'main-repo': {'allow_no_tests': True}}))
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job()
        self.assertTrue(result['ok'], result)
        self.assertEqual((result['status'], result['tests_status']), ('committed', 'no_tests'))
        self.assertIn('Tests: no_tests', git(self.repo, 'log', '-1', '--format=%B', result['commit']))

    def test_skipped_never_counts_as_success(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        skipped = lambda *a, **kw: {'status': 'skipped', 'command': None}
        original = jd.run_tests
        jd.run_tests = skipped
        try:
            result = self.job(test_cmd='true')
        finally:
            jd.run_tests = original
        self.assertFalse(result['ok'])
        self.assertIsNone(result['commit'])
        self.assertEqual(result['tests_status'], 'skipped')

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

    def test_tests_use_a_fresh_pycache_prefix_outside_the_worktree(self):
        seen = {}

        def runner(argv, cwd, timeout, grace, env=None, stall_seconds=None):
            seen['prefix'] = env['PYTHONPYCACHEPREFIX']
            seen['existed'] = Path(seen['prefix']).is_dir()
            return {'stdout': '', 'stderr': '', 'returncode': 0, 'timed_out': False, 'elapsed_seconds': 0}

        result = jd.run_tests(self.policy, self.repo, 'true', 5, self.base / 'tests.log', runner=runner)
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(seen['existed'])
        self.assertNotIn(str(self.repo.resolve()), str(Path(seen['prefix']).resolve()))
        self.assertFalse(Path(seen['prefix']).exists(), 'the prefix is removed after the run')

    def test_tracked_cache_symlinks_are_baseline_and_kept(self):
        (self.base / 'cache-target').mkdir()
        (self.repo / 'lib').mkdir()
        (self.repo / 'lib' / '__pycache__').symlink_to(self.base / 'cache-target', target_is_directory=True)
        (self.repo / '__pycache__').symlink_to(self.base / 'missing-target')  # dangling, listed with the files
        git(self.repo, 'add', 'lib/__pycache__', '__pycache__')
        git(self.repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'tracked links')
        jd.remove_bytecode_caches(self.repo)
        self.assertTrue((self.repo / 'lib' / '__pycache__').is_symlink())
        self.assertTrue((self.repo / '__pycache__').is_symlink())
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='true')
        self.assertTrue(result['ok'], result)
        files = git(self.repo, 'show', '--name-only', '--format=', result['commit']).split()
        self.assertEqual(files, ['out.txt'], 'baseline cache links are not committed as worker changes')

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

    def test_cleanup_removes_worktree_with_only_untracked_bytecode(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        done = self.job(test_cmd='mkdir -p __pycache__; echo b > __pycache__/x.pyc')
        self.assertEqual(done['excluded_test_artifacts'], [])
        report = jd.cleanup('0m', policy=self.policy)
        self.assertEqual(len(report['removed']), 1, report)
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
        for command, status in ((None, 'no_tests'), ('false', 'blocked_by_stackwich')):
            with self.subTest(command=command):
                self.supported()
                result = self.job(test_cmd=command)
                self.assertEqual(result['status'], status, result)
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
