"""pr_gate: fake gh (a script that logs argv and replays canned JSON) + a local bare git remote.
No network, no real gh, no real pushes anywhere outside the temp dir."""
import contextlib
import copy
from datetime import datetime, timedelta
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from llm_router import alerts
from helpers import load_policy, TERM
from llm_router import cli, pr_gate as pg

IST = ZoneInfo('Asia/Kolkata')
NOW = datetime(2026, 10, 17, 18, 0, tzinfo=IST)
JOB = 'proj-20261017-180000'
BRANCH = 'router/feature-20261017-180000'
REQUIRED = ['ios unit tests', 'macos unit tests', 'ios synthetic smoke', 'macos synthetic smoke',
            'Cloudflare Pages: demo-app', 'Cloudflare Pages: demo-beta']
FAKE_GH = r'''#!{python}
import json, os, sys
d = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(d, 'calls.jsonl'), 'a') as f:
    f.write(json.dumps(args) + '\n')
cfg = json.load(open(os.path.join(d, 'responses.json')))
key = ' '.join(args[:2])
r = cfg.get(key, {{}})
if isinstance(r, list):
    cpath = os.path.join(d, 'count-' + key.replace(' ', '-'))
    n = int(open(cpath).read()) if os.path.exists(cpath) else 0
    open(cpath, 'w').write(str(n + 1))
    r = r[min(n, len(r) - 1)]
sys.stdout.write(r.get('stdout', ''))
sys.stderr.write(r.get('stderr', ''))
sys.exit(r.get('rc', 0))
'''


def git(cwd, *args, check=True):
    done = subprocess.run(['git', '-C', str(cwd), '-c', 'user.name=T', '-c', 'user.email=t@example.invalid',
                           '-c', 'commit.gpgsign=false', *args], capture_output=True, text=True)
    if check and done.returncode != 0:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


def checks(buckets):
    return {'stdout': json.dumps([{'name': n, 'bucket': b, 'state': b.upper(), 'workflow': ''}
                                  for n, b in buckets.items()])}


def all_pass():
    return checks({name: 'pass' for name in REQUIRED})


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.base = Path(self.tmp.name)
        self.remote = base / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', '--initial-branch=main', str(self.remote)], check=True)
        self.repo = base / 'proj'
        subprocess.run(['git', 'init', '-q', '--initial-branch=main', str(self.repo)], check=True)
        (self.repo / 'app.txt').write_text('line one\nline two\n')
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', 'base')
        git(self.repo, 'remote', 'add', 'origin', str(self.remote))
        git(self.repo, 'push', '-q', 'origin', 'main')  # test fixture: local bare remote only
        self.base_sha = git(self.repo, 'rev-parse', 'HEAD')
        self.head = self.commit({'feature.txt': 'a new feature\n'}, 'router: add feature')
        tests_log = base / 'tests.log'
        tests_log.write_text('$ true\nexit=0\nall good OPENAI_API_KEY=' + 'sk-' + 'abcdefghijklmnopqrstuvwxyz123456\n')
        (base / 'jobs').mkdir()
        self.job = {'job_id': JOB, 'repo': str(self.repo), 'branch': BRANCH, 'base': 'HEAD',
                    'base_sha': self.base_sha, 'commit': self.head, 'status': 'committed', 'ok': True,
                    'worktree': str(base / 'pruned-worktree'), 'workers': ['codex'], 'tests_status': 'passed',
                    'tests': [{'status': 'passed', 'command': 'true', 'log': str(tests_log)}]}
        self.write_job()
        self.settings = {'remote': 'origin', 'base': 'main', 'visibility': 'public', 'test_cmd': 'true',
                         'screenshot_cmd': 'TODO', 'merge_method': 'squash', 'required_checks': REQUIRED}
        self.write_settings()
        (base / 'terms.txt').write_text('Zephyrquill\n')
        policy = copy.deepcopy(load_policy(timezone='Asia/Kolkata'))
        rt, rj = policy['run_task'], policy['run_job']
        rt.update(ledger=str(base / 'ledger.csv'), private_terms_file=str(base / 'terms.txt'),
                  claude_block_log=str(base / 'block.log'), shim_dir=str(base / 'shims'))
        rj.update(jobs_dir=str(base / 'jobs'), repo_config=str(base / 'repos.json'), caffeinate=False)
        policy['alerts'] = {'file': str(base / 'alerts.jsonl'), 'summary': str(base / 'ALERTS.md'),
                            'macos_notification': False}
        self.gh_dir = base / 'fakegh'
        self.gh_dir.mkdir()
        gh = self.gh_dir / 'gh'
        gh.write_text(FAKE_GH.format(python=sys.executable))
        gh.chmod(0o755)
        policy['pr_gate'] = {'approvals_file': str(base / 'approvals.jsonl'), 'gate_dir': str(base / 'gate'),
                             'reviews_dir': str(base / 'reviews'), 'gh_bin': str(gh), 'approval_ttl_hours': 24,
                             'ci': {'initial_poll_seconds': 30, 'max_poll_seconds': 120, 'timeout_minutes': 10,
                                    'missing_grace_minutes': 5}}
        self.policy = policy
        self.responses = {'pr list': {'stdout': '[]'},
                          'pr create': {'stdout': 'https://github.com/example/proj/pull/7\n'},
                          'pr view': self.view(), 'pr checks': all_pass(),
                          'pr ready': {}, 'pr merge': {}}
        self.save_responses()

    def tearDown(self):
        self.tmp.cleanup()

    # helpers
    def commit(self, files, message):
        current = git(self.repo, 'symbolic-ref', '--short', 'HEAD')
        exists = git(self.repo, 'rev-parse', '-q', '--verify', f'refs/heads/{BRANCH}', check=False)
        git(self.repo, 'checkout', '-q', *([] if exists else ['-b']), BRANCH)
        for name, text in files.items():
            (self.repo / name).write_text(text)
        git(self.repo, 'add', '-A')
        git(self.repo, 'commit', '-q', '-m', message)
        sha = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', '-q', current)
        return sha

    def write_job(self):
        (self.base / 'jobs' / f'{JOB}.json').write_text(json.dumps(self.job))

    def write_settings(self):
        (self.base / 'repos.json').write_text(json.dumps({'proj': self.settings}))

    def view(self, sha=None, draft=True, state='OPEN'):
        return {'stdout': json.dumps({'headRefOid': sha or self.head, 'state': state, 'isDraft': draft,
                                      'url': 'https://github.com/example/proj/pull/7', 'baseRefName': 'main'})}

    def save_responses(self):
        (self.gh_dir / 'responses.json').write_text(json.dumps(self.responses))

    def respond(self, key, value):
        self.responses[key] = value
        self.save_responses()

    def calls(self):
        path = self.gh_dir / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def remote_sha(self, branch=BRANCH):
        out = git(self.repo, 'ls-remote', 'origin', f'refs/heads/{branch}')
        return out.split()[0] if out else None

    def review(self, **kw):
        return pg.review(JOB, policy=self.policy, now=NOW, **kw)

    def approve(self, action='push', **kw):
        return pg.approve(JOB, action, policy=self.policy, now=kw.pop('now', NOW), **kw)

    def push(self, now=NOW):
        return pg.push(JOB, policy=self.policy, now=now)

    def pushed(self):
        self.review()
        self.approve('push')
        return self.push()

    def alerts(self):
        return alerts.open_alerts(self.policy)

    # (1) review
    def test_review_writes_redacted_evidence_bundle(self):
        summary = self.review()
        out = Path(summary['bundle'])
        for name in ('diffstat.txt', 'diff.patch', 'tests.txt', 'leak_check.json', 'worker.json',
                     'summary.json', 'SUMMARY.md'):
            self.assertTrue((out / name).is_file(), name)
        self.assertEqual(summary['head'], self.head)
        self.assertEqual(summary['leak_check'], 'clean')
        self.assertIn('feature.txt', (out / 'diffstat.txt').read_text())
        self.assertIn('+a new feature', (out / 'diff.patch').read_text())
        tests = (out / 'tests.txt').read_text()
        self.assertIn('[REDACTED]', tests)
        self.assertNotIn('sk-abcdefghijklmnop', tests)
        self.assertEqual(summary['screenshots']['status'], 'not_configured')
        self.assertIn('no push approval', summary['approvals']['push'])
        self.assertEqual(self.calls(), [])  # review never talks to GitHub

    def test_review_redacts_secrets_in_diff(self):
        self.head = self.commit({'cfg.txt': 'token = ghp_' + 'a' * 36 + '\n'}, 'add cfg')
        summary = self.review()
        diff = (Path(summary['bundle']) / 'diff.patch').read_text()
        self.assertNotIn('ghp_aaaa', diff)
        self.assertEqual(summary['leak_check'], 'blocked')
        self.assertIn('branch head moved', summary['warnings'][0])

    def test_screenshot_cmd_headless_only(self):
        worktree = self.base / 'wt'
        worktree.mkdir()
        self.job['worktree'] = str(worktree)
        self.write_job()
        seen = []

        def runner(argv, cwd, timeout, grace, env=None):
            seen.append(argv)
            Path(env['SCREENSHOT_DIR'], 'home.png').write_bytes(b'png')
            self.assertEqual(env['ROUTER_HEADLESS'], '1')
            return {'returncode': 0, 'stdout': 'ok', 'stderr': '', 'timed_out': False}

        for gui in ('open -a Simulator', 'osascript -e x', 'xcrun simctl boot X && open /Applications/Foo.app'):
            self.settings['screenshot_cmd'] = gui
            self.write_settings()
            self.assertEqual(self.review(screenshot_runner=runner)['screenshots']['status'], 'refused')
        self.assertEqual(seen, [])
        self.settings['screenshot_cmd'] = 'xcrun simctl io booted screenshot "$SCREENSHOT_DIR/home.png"'
        self.write_settings()
        shots = self.review(screenshot_runner=runner)['screenshots']
        self.assertEqual((shots['status'], shots['files']), ('ok', ['home.png']))

    # (2) approvals
    def test_approve_requires_review_of_current_head(self):
        with self.assertRaisesRegex(pg.Refused, 'run review first'):
            self.approve()
        self.review()
        with self.assertRaisesRegex(pg.Refused, 'not the current branch head'):
            self.approve(sha='deadbeef')
        record = self.approve(sha=self.head[:12])
        self.assertEqual((record['sha'], record['action']), (self.head, 'push'))
        self.assertEqual(record['expires_at'], (NOW + timedelta(hours=24)).isoformat(timespec='seconds'))
        self.assertTrue(pg.approval_status(self.policy, JOB, 'push', self.head, NOW)['ok'])
        self.assertFalse(pg.approval_status(self.policy, JOB, 'merge', self.head, NOW)['ok'])

    def test_approval_expires(self):
        self.review()
        self.approve(hours=2)
        later = NOW + timedelta(hours=2, minutes=1)
        self.assertIn('expired', pg.approval_status(self.policy, JOB, 'push', self.head, later)['why'])
        with self.assertRaisesRegex(pg.Refused, 'expired'):
            self.push(now=later)
        self.assertIsNone(self.remote_sha())

    def test_new_commit_invalidates_approval(self):
        self.review()
        self.approve()
        self.commit({'extra.txt': 'sneaky\n'}, 'later change')
        with self.assertRaisesRegex(pg.Refused, 'older commit'):
            self.push()
        self.assertIsNone(self.remote_sha())
        with self.assertRaisesRegex(pg.Refused, 'run review first'):
            self.approve()  # the new head needs fresh evidence too

    def test_denial_overrides_approval(self):
        self.review()
        self.approve()
        pg.deny(JOB, 'push', note='not yet', policy=self.policy, now=NOW)
        with self.assertRaisesRegex(pg.Refused, 'denied'):
            self.push()
        self.assertIsNone(self.remote_sha())

    def test_approve_refuses_unclean_review_and_non_router_branch(self):
        self.commit({'notes.txt': 'mentions Zephyrquill\n'}, 'oops')
        self.review()
        with self.assertRaisesRegex(pg.Refused, 'not clean'):
            self.approve()
        self.job['branch'] = 'main'
        self.write_job()
        with self.assertRaisesRegex(pg.Refused, 'never'):
            self.approve()

    # (3) push
    def test_push_pushes_router_branch_and_opens_draft_pr(self):
        result = self.pushed()
        self.assertTrue(result['pushed'])
        self.assertEqual(self.remote_sha(), self.head)
        self.assertEqual(git(self.repo, 'ls-remote', 'origin', 'refs/heads/main').split()[0], self.base_sha)
        self.assertEqual(result['pr'], {'number': 7, 'url': 'https://github.com/example/proj/pull/7', 'draft': True})
        create = [c for c in self.calls() if c[:2] == ['pr', 'create']]
        self.assertEqual(len(create), 1)
        self.assertIn('--draft', create[0])
        self.assertEqual(create[0][create[0].index('--base') + 1], 'main')
        self.assertEqual(create[0][create[0].index('--head') + 1], BRANCH)
        body = Path(create[0][create[0].index('--body-file') + 1]).read_text()
        self.assertIn(self.head, body)
        self.assertIn('feature.txt', body)
        for call in self.calls():
            self.assertFalse({'--force', '--admin', '-f'} & set(call))
        with self.assertRaisesRegex(pg.Refused, 'already used'):
            self.push()  # an approval is single use
        self.assertTrue(any(a['kind'] == 'push' for a in self.alerts()))

    def test_push_reuses_existing_pr(self):
        self.respond('pr list', {'stdout': json.dumps([{'number': 9, 'url': 'u/pull/9', 'isDraft': True,
                                                        'headRefOid': self.head}])})
        result = self.pushed()
        self.assertEqual(result['pr']['number'], 9)
        self.assertFalse([c for c in self.calls() if c[:2] == ['pr', 'create']])

    def test_push_blocked_by_private_term_in_added_line(self):
        self.head = self.commit({'leak.txt': 'Zephyrquill\n'}, 'add file')
        gate = pg.load_gate(self.policy, JOB)
        gate['review'] = {'sha': self.head, 'leak_check': 'clean', 'dir': 'x'}  # forge a stale clean review
        pg.save_gate(self.policy, JOB, gate)
        self.approve()
        with self.assertRaisesRegex(pg.Refused, 'leak check'):
            self.push()
        self.assertIsNone(self.remote_sha())
        self.assertEqual(self.calls(), [])
        self.assertTrue(any(a['level'] == 'stop' and a['kind'] == 'push' for a in self.alerts()))

    def test_public_repo_grep_catches_term_in_unchanged_context(self):
        git(self.repo, 'checkout', '-q', 'main')
        (self.repo / 'app.txt').write_text('line one\nZephyrquill internal\nline two\n')
        git(self.repo, 'commit', '-q', '-am', 'base with private word')  # pre-existing on base
        new_base = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', '-q', '-b', 'router/x', new_base)
        (self.repo / 'app.txt').write_text('line one\nZephyrquill internal\nline two changed\n')
        git(self.repo, 'commit', '-q', '-am', 'touch app')
        head = git(self.repo, 'rev-parse', 'HEAD')
        git(self.repo, 'checkout', '-q', 'main')
        private = pg.range_leak_check(self.policy, self.repo, new_base, head, public=False)
        public = pg.range_leak_check(self.policy, self.repo, new_base, head, public=True)
        self.assertEqual(private['status'], 'clean')
        self.assertEqual(public['status'], 'blocked')
        self.assertIn('(full file)', public['findings'][0]['file'])
        (self.base / 'terms.txt').unlink()
        self.assertEqual(pg.range_leak_check(self.policy, self.repo, new_base, head, public=True)['status'], 'error')

    def test_never_force_pushes_over_diverged_remote(self):
        other = self.base / 'other'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(other)], check=True)
        git(other, 'checkout', '-q', '-b', BRANCH)
        (other / 'theirs.txt').write_text('theirs\n')
        git(other, 'add', '-A')
        git(other, 'commit', '-q', '-m', 'someone else')
        git(other, 'push', '-q', 'origin', BRANCH)
        theirs = self.remote_sha()
        self.review()
        self.approve()
        with self.assertRaisesRegex(pg.Refused, 'force push'):
            self.push()
        self.assertEqual(self.remote_sha(), theirs)

    def test_push_refuses_base_branch_and_missing_remote(self):
        self.settings['remote'] = ''
        self.write_settings()
        self.review()
        self.approve()
        with self.assertRaisesRegex(pg.Refused, 'no remote'):
            self.push()
        self.job['branch'] = 'main'
        self.write_job()
        with self.assertRaisesRegex(pg.Refused, 'never'):
            self.push()

    def test_argv_guard(self):
        for argv in (['git', 'push', '--force', 'o', 'b'], ['git', 'push', 'o', '+refs/heads/b:refs/heads/b'],
                     ['git', 'push', '--force-with-lease'], ['gh', 'pr', 'merge', '1', '--admin'],
                     ['gh', 'pr', 'merge', '1', '--auto'], ['git', 'push', '-d', 'o', 'b'],
                     ['gh', 'pr', 'merge', '1', '--delete-branch']):
            with self.assertRaises(pg.Refused):
                pg.check_argv(argv)
        pg.check_argv(['git', 'push', '--porcelain', 'origin', 'refs/heads/a:refs/heads/a'])

    # (4) CI
    def test_evaluate_checks_states(self):
        e = pg.evaluate_checks
        self.assertEqual(e(json.loads(all_pass()['stdout']), REQUIRED)['status'], 'passed')
        mixed = {name: 'pass' for name in REQUIRED}
        mixed['ios unit tests'] = 'fail'
        self.assertEqual(e(json.loads(checks(mixed)['stdout']), REQUIRED)['failing'], ['ios unit tests'])
        mixed['ios unit tests'] = 'pending'
        self.assertEqual(e(json.loads(checks(mixed)['stdout']), REQUIRED)['status'], 'pending')
        mixed['ios unit tests'] = 'skipping'
        self.assertEqual(e(json.loads(checks(mixed)['stdout']), REQUIRED)['status'], 'failed')
        cf = {'Cloudflare Pages: demo-app': 'pass', 'Cloudflare Pages: demo-beta': 'pass'}
        quirk = e(json.loads(checks(cf)['stdout']), REQUIRED)
        self.assertEqual((quirk['status'], quirk['suggested_action']), ('missing', pg.QUIRK_ACTION))
        self.assertEqual(len(quirk['missing']), 4)
        self.assertEqual(e([], REQUIRED)['suggested_action'], pg.NO_CHECKS_YET)
        self.assertEqual(e([{'name': 'x', 'state': 'SUCCESS'}], [])['status'], 'passed')

    def test_ci_watch_backs_off_until_pass_and_alerts(self):
        self.pushed()
        pending = {name: 'pending' for name in REQUIRED}
        self.respond('pr checks', [checks(pending), checks(pending), all_pass()])
        clock, sleeps = [0.0], []
        result = pg.ci(JOB, watch=True, policy=self.policy, now=NOW,
                       sleep=lambda s: (sleeps.append(s), clock.__setitem__(0, clock[0] + s)),
                       clock=lambda: clock[0])
        self.assertEqual((result['status'], result['polls'], sleeps), ('passed', 3, [30, 60]))
        self.assertTrue(any(a['kind'] == 'ci' and 'passed' in a['message'] for a in self.alerts()))
        recorded = (self.base / 'reviews' / JOB / 'ci.jsonl').read_text().splitlines()
        self.assertEqual(len(recorded), 3)
        self.assertEqual(pg.load_gate(self.policy, JOB)['ci']['status'], 'passed')

    def test_ci_failure_alert_once_without_watch(self):
        self.pushed()
        failing = {name: 'pass' for name in REQUIRED}
        failing['macos synthetic smoke'] = 'fail'
        self.respond('pr checks', {**checks(failing), 'rc': 1})
        result = pg.ci(JOB, policy=self.policy, now=NOW, sleep=self.fail)
        self.assertEqual((result['status'], result['polls']), ('failed', 1))
        pg.ci(JOB, policy=self.policy, now=NOW, sleep=self.fail)
        stops = [a for a in self.alerts() if a['kind'] == 'ci' and a['level'] == 'stop']
        self.assertEqual(len(stops), 1)  # deduped per sha + status

    def test_ci_cloudflare_only_quirk_suggests_but_never_acts(self):
        self.pushed()
        cf = {'Cloudflare Pages: demo-app': 'pass', 'Cloudflare Pages: demo-beta': 'pass'}
        self.respond('pr checks', checks(cf))
        clock = [0.0]
        result = pg.ci(JOB, watch=True, policy=self.policy, now=NOW,
                       sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0])
        self.assertEqual((result['status'], result['suggested_action']), ('missing', pg.QUIRK_ACTION))
        self.assertFalse(result.get('timed_out'))
        verbs = {tuple(c[:2]) for c in self.calls()}
        self.assertFalse({('pr', 'close'), ('pr', 'reopen'), ('pr', 'edit')} & verbs)
        self.assertTrue(any(pg.QUIRK_ACTION in a['message'] for a in self.alerts()))

    def test_ci_watch_times_out(self):
        self.pushed()
        self.respond('pr checks', checks({name: 'pending' for name in REQUIRED}))
        clock = [0.0]
        result = pg.ci(JOB, watch=True, timeout_minutes=3, policy=self.policy, now=NOW,
                       sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0])
        self.assertTrue(result['timed_out'])
        self.assertLessEqual(clock[0], 180)
        self.assertTrue(any('watch timeout' in a['message'] for a in self.alerts()))

    def test_ci_handles_no_checks_and_gh_errors(self):
        self.pushed()
        self.respond('pr checks', {'stderr': 'no checks reported on the branch', 'rc': 1})
        self.assertEqual(pg.ci(JOB, policy=self.policy, now=NOW)['suggested_action'], pg.NO_CHECKS_YET)
        self.respond('pr view', {'stderr': 'HTTP 502', 'rc': 1})
        self.assertEqual(pg.ci(JOB, policy=self.policy, now=NOW)['status'], 'error')

    def fail(self, *_):
        raise AssertionError('should not sleep')

    # (5) merge
    def test_merge_needs_separate_merge_approval(self):
        self.pushed()
        with self.assertRaisesRegex(pg.Refused, 'no merge approval'):
            pg.merge(JOB, policy=self.policy, now=NOW)
        self.assertFalse([c for c in self.calls() if c[:2] == ['pr', 'merge']])

    def test_merge_approval_requires_pushed_pr(self):
        self.review()
        with self.assertRaisesRegex(pg.Refused, 'push the branch'):
            self.approve('merge')

    def test_merge_refuses_unless_required_checks_green(self):
        self.pushed()
        self.approve('merge')
        cf = {'Cloudflare Pages: demo-app': 'pass', 'Cloudflare Pages: demo-beta': 'pass'}
        self.respond('pr checks', checks(cf))
        with self.assertRaisesRegex(pg.Refused, 'close and reopen'):
            pg.merge(JOB, policy=self.policy, now=NOW)
        self.respond('pr checks', checks({**{n: 'pass' for n in REQUIRED}, 'ios unit tests': 'pending'}))
        with self.assertRaisesRegex(pg.Refused, 'not green'):
            pg.merge(JOB, policy=self.policy, now=NOW)
        self.assertFalse([c for c in self.calls() if c[:2] in (['pr', 'merge'], ['pr', 'ready'])])

    def test_merge_refuses_when_pr_head_differs(self):
        self.pushed()
        self.approve('merge')
        self.respond('pr view', self.view(sha='f' * 40))
        with self.assertRaisesRegex(pg.Refused, 'not the approved local head'):
            pg.merge(JOB, policy=self.policy, now=NOW)

    def test_merge_happy_path_uses_method_and_match_head(self):
        self.pushed()
        self.approve('merge')
        result = pg.merge(JOB, policy=self.policy, now=NOW)
        self.assertEqual(result['steps'], ['marked ready for review', 'merged with --squash'])
        merge_call = [c for c in self.calls() if c[:2] == ['pr', 'merge']][0]
        self.assertEqual(merge_call[:6], ['pr', 'merge', '7', '--squash', '--match-head-commit', self.head])
        self.assertNotIn('--admin', merge_call)
        self.assertNotIn('--delete-branch', merge_call)
        with self.assertRaisesRegex(pg.Refused, 'already used'):
            pg.merge(JOB, policy=self.policy, now=NOW)

    def test_merge_config_guards(self):
        self.pushed()
        self.approve('merge')
        self.settings['merge_method'] = 'force'
        self.write_settings()
        with self.assertRaisesRegex(pg.Refused, 'merge_method'):
            pg.merge(JOB, policy=self.policy, now=NOW)
        self.settings.update(merge_method='merge', required_checks=[])
        self.write_settings()
        with self.assertRaisesRegex(pg.Refused, 'required_checks'):
            pg.merge(JOB, policy=self.policy, now=NOW)

    def test_new_commit_after_push_needs_new_merge_approval(self):
        self.pushed()
        self.approve('merge')
        self.head = self.commit({'later.txt': 'x\n'}, 'later')
        self.respond('pr view', self.view())
        with self.assertRaisesRegex(pg.Refused, 'older commit'):
            pg.merge(JOB, policy=self.policy, now=NOW)


class CliTests(unittest.TestCase):
    def test_cli_lists_gate_commands(self):
        for name in ('review', 'approve', 'deny', 'push', 'ci', 'merge'):
            with self.assertRaises(SystemExit) as done, contextlib.redirect_stdout(io.StringIO()):
                cli.main([name, '--help'])
            self.assertEqual(done.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
