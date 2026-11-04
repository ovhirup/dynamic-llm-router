"""Overnight batch runner (#119): 3-5 jobs, each through run-job, merged into a nightly staging branch.

`llm-router batch` takes 3-5 issue numbers (or task files) and, with at most `max_parallel` jobs running at once:
1. runs every job through job_dispatch.run_job (allocation, worktree, tests, commit) from the staging head;
2. sends each committed job to a verifier unit that only reads and runs code and records
   a private JSON verdict. Findings go back to the producing worker as a fix round (new failing tests
   first, then the fix); after MAX_FIX_ROUNDS failed rounds the job is blocked;
3. merges each verified job (`wrong=0 crash=0`, tests passed, zero skipped) into `staging/YYYYMMDD` one at a
   time, keeping the merge only when the full test suite passes on the merged result. A failed job stays on its
   own branch and is recorded, never merged;
4. writes `summary.json` + `summary.md` (job, worker, branch, commit, tests, verdict) and keeps `status.json`
   plus `events.jsonl` current so the Notch can show progress.

Decisions (v1):
- run_job holds a per-repo lock for its whole run. The batch takes that repo lock once for the whole night (so no
  outside run-job interleaves) and gives each of its own jobs a private lock dir, so up to `max_parallel` jobs
  overlap while every job still runs against the real repo (repos.json, lessons and the job record stay keyed to
  it). Job starts are spaced into distinct seconds because run_job names worktrees and logs by the second.
- Staging merges the exact commit the verifier passed, never a branch tip that moved after verification. Fix
  rounds keep the task's allowed-paths scope, and a fixer that commits by itself blocks the job.
- Verifiers may write their private verdict file. The runner checks the worktree afterwards. A verifier that moved
  HEAD or left any change blocks the job (`verifier_wrote`); its edits are left in place for a human, never kept.
- The verifier is the `judge` role with the producing worker excluded, so the finder is never the fixer.
- Never pushes, never touches main or the user's checkout: staging is updated in its own temporary worktree, and
  `main`/`master`/the base branch are refused as staging names.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time

from . import claude_handoff, config, security
from . import job_dispatch as jd
from . import task_dispatch as td
from .job_dispatch import Refused

MIN_JOBS, MAX_JOBS = 3, 5
DEFAULT_MAX_PARALLEL = 2
MAX_FIX_ROUNDS = 3
PROTECTED_BRANCHES = ('main', 'master')
PROMPT_NAME = '.llm-router-batch-prompt.md'
MAX_DIFF_CHARS = 60_000
# Code under test shares the repo's .git: every router git call ignores replace refs (which could swap the content a
# verified sha points at) and hooks it may have planted. Ref -> sha lookups (jd.resolve_base) read refs, not
# objects, so replace refs cannot change them.
SAFE_GIT = ('--no-replace-objects', '-c', 'core.hooksPath=/dev/null')
STREAM_JSON = ('stream-json', 'streaming-json')
SKIPPED = re.compile(r'(?<!\d)[1-9]\d*\s+skipped\b|\bskip(?:ped)?=[1-9]|^(?:--- SKIP\b|SKIPPED \[)', re.I | re.M)
ANSI = re.compile(r'\x1b\[[0-9;]*m')

VERIFIER_RULES = """\
Verifier rules (binding):
1. You only read and run code. The designated verdict file is your only authorized write.
   Any worktree change voids verification. Never commit, push, merge or open a PR.
2. Judge the produced diff against the task by running the tests and the code. Never trust the worker's claims.
3. Count each distinct wrong behaviour (`wrong`) and each crash, traceback or failing/erroring run (`crash`).
   For each, give the command you ran, what you expected and what you saw, so it can become a failing test.
4. Write a JSON object with exactly two nonnegative integer fields, wrong and crash, to the designated
   verdict file after testing. Write it yourself and keep the path out of test commands and their output.
"""


def _now(policy):
    return td.now_local(policy).isoformat(timespec='seconds')


def _git(cwd, *args, check=False):
    done = jd._git(cwd, *SAFE_GIT, *args)
    if check and done.returncode != 0:
        raise Refused(f'git {args[0]} failed: ' + security.redact(done.stderr.strip())[:300])
    return done


def batch_dir(policy, batch_id):
    return config.policy_path(policy, 'batch', 'dir', 'logs/batches') / batch_id


def is_stream_json(command):
    """Read the last output-format option before the option terminator."""
    output_format = None
    args = iter(command[1:])
    for arg in args:
        if arg == '--':
            break
        if arg == '--output-format':
            output_format = next(args, None)
            if output_format == '--':
                break
        elif arg.startswith('--output-format='):
            output_format = arg.split('=', 1)[1]
    return output_format in STREAM_JSON


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('duplicate verdict field')
    return result


def _valid_verdict(verdict):
    return (isinstance(verdict, dict) and set(verdict) == {'wrong', 'crash'}
            and all(type(count) is int and count >= 0 for count in verdict.values()))


def _read_verdict(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            raw = handle.read(1025)
        if len(raw) > 1024:
            return None
        verdict = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, ValueError, RecursionError):
        return None
    return verdict if _valid_verdict(verdict) else None


def gate_tests(tests):
    """A test result with any skipped test never counts as passed."""
    if tests.get('status') == 'passed' and SKIPPED.search(ANSI.sub('', tests.get('tail') or '')):
        return dict(tests, status='skipped')
    return tests


def check_staging_name(repo, name, base):
    if _git(repo, 'check-ref-format', '--branch', name).returncode != 0:
        raise Refused(f'invalid staging branch name {name!r}')
    short = name.removeprefix('refs/heads/')
    if short in PROTECTED_BRANCHES or short == base.removeprefix('refs/heads/').removeprefix('origin/'):
        raise Refused(f'staging branch {name!r} must not be main, master or the base branch')
    return short


def gh_issue(number, repo, policy, gh_repo=None):
    """{'title', 'body'} for one issue via `gh issue view` (read only)."""
    gh = (policy.get('pr_gate') or {}).get('gh_bin', 'gh')
    argv = [gh, 'issue', 'view', str(number), '--json', 'title,body'] + (['-R', gh_repo] if gh_repo else [])
    try:
        done = subprocess.run(argv, cwd=repo, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f'cannot read issue #{number}: {error}')
    if done.returncode != 0:
        raise Refused(f'cannot read issue #{number}: ' + security.redact(done.stderr.strip())[:300])
    return json.loads(done.stdout)


def _write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    return path


def _prompt_file(worktree, prompt):
    """Prompt under the worktree's git dir, so it never shows up as a change."""
    git_dir = Path(_git(worktree, 'rev-parse', '--absolute-git-dir', check=True).stdout.strip())
    return _write_private(git_dir / PROMPT_NAME, prompt)


def _snapshot(worktree):
    return (_git(worktree, 'rev-parse', 'HEAD').stdout.strip(),
            _git(worktree, 'status', '--porcelain', '--untracked-files=normal').stdout)


def verdict_text(argv, done):
    """Extract the report for review. Report output cannot authorize staging."""
    stdout = done.get('stdout') or ''
    if not is_stream_json(argv):
        return stdout
    return claude_handoff.inspect(stdout, done.get('stderr') or '', done.get('returncode') or 0)['text']


def default_verifier(job, prompt, policy):
    """Run a judge in the job worktree and accept its verdict only from a private file."""
    attempts = []
    with tempfile.TemporaryDirectory(prefix='dlr-judge-') as directory:
        verdict_path = Path(directory) / 'verdict.json'
        task_file = _write_private(Path(directory) / 'prompt.md', f'{prompt}\nVerdict file `{verdict_path}`\n')

        def capture(argv, *args, **kwargs):
            # A failed worker or continuation cannot leave a verdict for a later attempt.
            verdict_path.unlink(missing_ok=True)
            done = td.run_bounded(argv, *args, **kwargs)
            attempts.append((argv, done))
            return done

        try:
            result = td.run_task(task_file, job['worktree'], allow_dirty=True, policy=policy, role='judge',
                                 exclude=tuple(w for w in [job.get('worker')] if w), runner=capture,
                                 stamp=f"{job['job_id']}-verify{len(job['verifications']) + 1}")
        except td.Refused as refusal:
            return {'status': 'unavailable', 'reason': str(refusal)}
        worker = result.get('worker')
        report = verdict_text(*attempts[-1]) if attempts else ''
        if not result.get('ok'):
            return {'status': 'unavailable', 'reason': result.get('reason') or 'verifier run failed',
                    'text': report, 'worker': worker}
        verdict = _read_verdict(verdict_path) if attempts else None
        return {'status': 'done', 'text': report, 'verdict': verdict, 'worker': worker}


def default_fixer(job, prompt, policy):
    """Run the producing worker in the job worktree on the verifier's findings (the router commits)."""
    task_file = _prompt_file(Path(job['worktree']), prompt)
    try:
        result = td.run_task(task_file, job['worktree'], worker=job.get('worker'), allow_dirty=True, policy=policy,
                             stamp=f"{job['job_id']}-fix{len(job['fix_rounds']) + 1}")
    except td.Refused as refusal:
        return {'ok': False, 'reason': str(refusal)}
    return {'ok': bool(result.get('ok')), 'reason': result.get('reason'), 'worker': result.get('worker'),
            'blocked_by_leak_check': bool(result.get('blocked_by_leak_check'))}


class Batch:
    def __init__(self, repo, jobs, policy, batch_id, staging, test_cmd, test_runner, verifier, fixer):
        self.repo, self.jobs, self.policy, self.batch_id = repo, jobs, policy, batch_id
        self.staging, self.test_cmd, self.test_runner = staging, test_cmd, test_runner
        self.verifier, self.fixer = verifier, fixer
        self.dir = batch_dir(policy, batch_id)
        self.timeout = jd.repo_settings(policy, repo).get('test_timeout_seconds',
                                                          policy['run_job']['test_timeout_seconds'])
        self.lock = threading.Lock()
        self.start_lock, self.last_stamp = threading.Lock(), None
        self.staging_restored_from = None
        self.rollback_failure = None

    # ---- progress log (read by the Notch) ----
    def update(self, job, state, **fields):
        with self.lock:
            job.update(fields, state=state, updated_at=_now(self.policy))
            event = {'at': job['updated_at'], 'batch_id': self.batch_id, 'job': job['key'], 'state': state}
            event.update({k: v for k, v in fields.items() if k in ('status', 'reason', 'commit', 'round')})
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / 'events.jsonl', 'a') as handle:
                handle.write(json.dumps(security.redact_obj(event)) + '\n')
            self.write_status()

    def write_status(self, final=False):
        status = {'batch_id': self.batch_id, 'repo': str(self.repo), 'staging_branch': self.staging,
                  'updated_at': _now(self.policy), 'finished': final,
                  'jobs': [{k: job.get(k) for k in ('key', 'source', 'state', 'status', 'worker', 'branch',
                                                     'commit', 'reason', 'updated_at')} for job in self.jobs]}
        if self.rollback_failure is not None:
            status['rollback_failure'] = self.rollback_failure
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / 'status.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(security.redact_obj(status), indent=2))
        tmp.replace(path)

    def run_tests(self, cwd, name):
        kwargs = {'runner': self.test_runner} if self.test_runner else {}
        log = self.dir / 'tests' / f'{name}.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        return gate_tests(jd.run_tests(self.policy, cwd, self.test_cmd, self.timeout, log, **kwargs))

    # ---- one job: run-job, then verify / fix rounds ----
    def run_one(self, job, run_job, options):
        try:
            self.run_job(job, run_job, options)
        except Exception as error:  # one job's crash never stops the batch
            self.update(job, 'failed', status='error', reason=security.redact(f'{type(error).__name__}: {error}')[:300])

    def start_time(self):
        """A start time in a second no other job of this batch started in (run_job names by the second)."""
        with self.start_lock:
            while True:
                started = td.now_local(self.policy)
                stamp = started.strftime('%Y%m%d-%H%M%S')
                if stamp != self.last_stamp:
                    self.last_stamp = stamp
                    return started
                time.sleep(0.05)

    def run_job(self, job, run_job, options):
        # the batch holds the real repo lock; each job locks only within its own dir
        policy = dict(self.policy, run_job=dict(self.policy['run_job'], lock_dir=str(self.dir / 'locks' / job['key'])))
        started = self.start_time()
        self.update(job, 'running')
        try:
            summary = run_job(self.repo, job['task_file'], base=job['base_sha'], branch=job['branch'],
                              test_cmd=self.test_cmd, keep=True, policy=policy, now=started, **options)
        except Exception as error:
            return self.update(job, 'failed', status='error', reason=security.redact(f'{type(error).__name__}: {error}')[:300])
        tests = gate_tests((summary.get('tests') or [{}])[-1] or {})
        job.update(job_id=summary.get('job_id'), worktree=summary.get('worktree'),
                   worker=(summary.get('workers') or [None])[-1], commit=summary.get('commit'),
                   tests=tests.get('status') or summary.get('tests_status'))
        if not summary.get('ok') or summary.get('status') != 'committed':
            return self.update(job, 'failed', status=summary.get('status'),
                               reason=summary.get('failure_reason') or '; '.join(summary.get('warnings') or [])[:300])
        if job['tests'] != 'passed':
            return self.update(job, 'failed', status=f"tests_{job['tests']}", reason='job tests did not pass')
        self.verify_loop(job)

    def verify_loop(self, job):
        findings = None
        for round_no in range(MAX_FIX_ROUNDS + 1):
            if round_no:
                fix = self.fix(job, findings, round_no)
                job['fix_rounds'].append(fix)
                if fix['status'] not in ('committed', 'no_change'):
                    return self.update(job, 'blocked', status=f"fix_{fix['status']}", reason=fix.get('reason'))
                if fix['tests'] != 'passed':
                    findings = f"The test suite is {fix['tests']} after fix round {round_no}:\n{fix.get('tail', '')}"
                    continue
            verdict = self.verify(job)
            job['verifications'].append(verdict)
            if verdict['status'] != 'done':
                return self.update(job, 'blocked', status=verdict['status'], reason=verdict.get('reason'))
            job['verdict'] = f"RESULT wrong={verdict['wrong']} crash={verdict['crash']}"
            if verdict['wrong'] == 0 and verdict['crash'] == 0:
                return self.update(job, 'verified', status='verified', verified_sha=verdict['sha'], commit=verdict['sha'])
            findings = verdict['report']
        self.update(job, 'blocked', status='blocked_after_fix_rounds',
                    reason=f'{MAX_FIX_ROUNDS} fix rounds did not reach RESULT wrong=0 crash=0')

    def verify(self, job):
        self.update(job, 'verifying', round=len(job['fix_rounds']))
        worktree = Path(job['worktree'])
        diff = _git(worktree, 'diff', '--no-renames', '--no-color', f"{job['base_sha']}..HEAD").stdout
        prompt = (f"Verify batch job `{job['key']}` (branch {job['branch']}) against base {job['base_sha']}.\n\n"
                  f"{VERIFIER_RULES}\n## Task\n{job['task_text']}\n\n## Test command\n{self.test_cmd}\n\n"
                  f"## Diff{' (truncated)' if len(diff) > MAX_DIFF_CHARS else ''}\n"
                  f"```diff\n{security.redact(diff[:MAX_DIFF_CHARS])}\n```\n")
        before = _snapshot(worktree)
        try:
            result = self.verifier(job, prompt, self.policy) or {}
        except Exception as error:
            result = {'status': 'unavailable', 'reason': f'{type(error).__name__}: {error}'[:300]}
        record = {'worker': result.get('worker'), 'sha': before[0], 'at': _now(self.policy)}
        if _snapshot(worktree) != before:  # read and run only: a verifier never makes a problem disappear
            return dict(record, status='verifier_wrote', reason='verifier changed the worktree; job blocked')
        if result.get('status') != 'done':
            return dict(record, status='verifier_unavailable', reason=result.get('reason'))
        if result.get('worker') and result.get('worker') == job.get('worker'):
            return dict(record, status='verifier_is_producer', reason='the producing worker cannot verify its own job')
        verdict = result.get('verdict')
        if not _valid_verdict(verdict):
            return dict(record, status='invalid_verdict', reason='judge verdict file was missing or invalid')
        return dict(record, status='done', wrong=verdict['wrong'], crash=verdict['crash'],
                    report=security.redact((result.get('text') or '').strip()[-4000:]))

    def fix(self, job, findings, round_no):
        self.update(job, 'fixing', round=round_no)
        worktree = Path(job['worktree'])
        head = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
        prompt = (f"{job['task_text'].rstrip()}\n\n## Verifier findings (fix round {round_no} of {MAX_FIX_ROUNDS})\n"
                  f"{findings}\n\nFor each finding, first add a test that fails because of it, then fix the code "
                  'so the whole test suite passes. Do not commit, push or merge.\n')
        try:
            result = self.fixer(job, prompt, self.policy) or {}
        except Exception as error:
            result = {'ok': False, 'reason': f'{type(error).__name__}: {error}'[:300]}
        record = {'round': round_no, 'worker': result.get('worker') or job.get('worker')}
        if result.get('blocked_by_leak_check'):
            return dict(record, status='leak_blocked', reason='leak check hit; nothing committed')
        if _git(worktree, 'rev-parse', 'HEAD').stdout.strip() != head:
            return dict(record, status='moved_head', reason='the fixer committed or moved HEAD itself')
        jd.remove_bytecode_caches(worktree)
        scope = jd.parse_allowed_paths(job['task_text'])
        outside = jd.out_of_scope(jd.changed_paths(worktree), scope) if scope else []
        if outside:
            return dict(record, status='out_of_scope', reason=f'fix changed paths outside the task scope: {outside[:5]}')
        if not jd._has_changes(worktree):
            status = 'no_change' if result.get('ok') else 'worker_failed'
            record.update(status=status, reason=result.get('reason'))
        else:
            identity = self.policy['run_job']['commit_identity']
            _git(worktree, 'add', '-A', check=True)
            done = _git(worktree, '-c', f'user.name={identity["name"]}', '-c', f'user.email={identity["email"]}',
                        'commit', '-q', '-m', f"router: batch {self.batch_id} fix round {round_no} ({job['key']})")
            if done.returncode != 0:
                return dict(record, status='commit_failed', reason=security.redact(done.stderr.strip())[:300])
            job['commit'] = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
            record.update(status='committed', commit=job['commit'])
        if record['status'] == 'worker_failed':
            return record
        tests = self.run_tests(worktree, f"{job['key']}-fix{round_no}")
        record.update(tests=tests['status'], tail=tests.get('tail', '')[-2000:])
        job['tests'] = tests['status']
        return record

    # ---- staging: merge one verified job at a time, keep it only when the full suite passes ----
    def stop_after_rollback_failure(self, ref, expected, error, job=None):
        try:
            observed = jd.resolve_base(self.repo, ref) or None
        except (OSError, subprocess.SubprocessError):
            observed = None
        reason = security.redact(f'{type(error).__name__}: {error}')[:300]
        self.rollback_failure = {'ref': ref, 'expected_sha': expected, 'observed_sha': observed, 'reason': reason}
        if job is not None:
            self.update(job, 'rollback_failed', status='rollback_failed', reason=reason)
        for pending in self.jobs:
            if pending['state'] == 'verified':
                self.update(pending, 'not_merged', status='staging_aborted',
                            reason='staging stopped after a rollback failure')

    def merge_all(self, root, start_sha):
        """start_sha: the staging tip recorded under the repo lock before any job ran. Job code ran against the
        shared .git meanwhile, so the branch is put back on it rather than trusted. Every git call here runs with
        hooks off, and each merge must have exactly (previous tip, verified job commit) as parents."""
        staging_ref = f'refs/heads/{self.staging}'
        current = jd.resolve_base(self.repo, staging_ref)
        if current != start_sha:  # put staging back even when nothing is verified or no worktree opens
            self.staging_restored_from = current or None
            try:
                _git(self.repo, 'update-ref', staging_ref, start_sha, check=True)
                if jd.resolve_base(self.repo, staging_ref) != start_sha:
                    raise Refused('staging ref did not return to its recorded start')
            except (Refused, OSError, subprocess.SubprocessError) as error:
                self.stop_after_rollback_failure(staging_ref, start_sha, error)
                return
        verified = [job for job in self.jobs if job['state'] == 'verified']
        if not verified:
            return
        worktree = root / f"{jd.slugify(self.repo.name)}-staging-{self.batch_id}"
        added = _git(self.repo, 'worktree', 'add', '-q', str(worktree), self.staging)
        if added.returncode != 0:  # e.g. staging is checked out elsewhere: nothing is merged
            reason = 'cannot open staging worktree: ' + security.redact(added.stderr.strip())[:300]
            for job in verified:
                self.update(job, 'not_merged', status='staging_unavailable', reason=reason)
            return
        identity = self.policy['run_job']['commit_identity']

        def rev(ref):
            return _git(worktree, 'rev-parse', '-q', '--verify', ref).stdout.strip()

        def at(sha):  # both the staging worktree and the staging branch ref are exactly at sha
            return rev('HEAD') == sha and jd.resolve_base(self.repo, staging_ref) == sha

        class RollbackFailed(Exception):
            pass

        def restore(sha):  # a failed Git operation must never be reported as a successful rollback
            try:
                _git(self.repo, 'update-ref', staging_ref, sha, check=True)
                _git(worktree, 'checkout', '-q', '-f', self.staging, check=True)
                _git(worktree, 'reset', '-q', '--hard', sha, check=True)
                _git(worktree, 'clean', '-ffdxq', check=True)
                if not at(sha):
                    raise Refused('staging ref or worktree HEAD did not return to the expected commit')
            except (Refused, OSError, subprocess.SubprocessError) as error:
                raise RollbackFailed(str(error)) from error

        def merge_one(job, expected):
            """Merge one verified job onto `expected`; returns the new staging tip (unchanged when not merged)."""
            restore(expected)  # also clears files the previous job's staging tests left behind
            if jd.resolve_base(self.repo, f"refs/heads/{job['branch']}") != job['verified_sha']:
                self.update(job, 'not_merged', status='branch_moved',
                            reason='job branch moved after verification; nothing merged')
                return expected
            merged = _git(worktree, '-c', f'user.name={identity["name"]}',
                          '-c', f'user.email={identity["email"]}', 'merge', '--no-ff', '--no-edit', '-m',
                          f"router: batch {self.batch_id} merge {job['branch']} ({job['key']})", job['verified_sha'])
            if merged.returncode != 0:
                restore(expected)
                self.update(job, 'not_merged', status='merge_conflict',
                            reason=security.redact(merged.stdout.strip() or merged.stderr.strip())[-300:])
                return expected
            merge_sha = rev('HEAD')
            if rev(f'{merge_sha}^1') != expected or rev(f'{merge_sha}^2') != job['verified_sha'] or \
                    rev(f'{merge_sha}^3'):
                restore(expected)
                self.update(job, 'not_merged', status='staging_tampered',
                            reason='merge commit parents are not (staging tip, verified commit); merge undone')
                return expected
            try:
                tests = self.run_tests(worktree, f"{job['key']}-staging")
            except Exception as error:  # a test run that cannot even start is a failed run, never a pass
                job['staging_tests'] = 'error'
                restore(expected)
                self.update(job, 'not_merged', status='staging_tests_error',
                            reason=security.redact(f'staging test run raised {type(error).__name__}: {error}')[:300])
                return expected
            job['staging_tests'] = tests['status']
            if not at(merge_sha):  # the merged code moved HEAD or the branch while its tests ran
                restore(expected)
                self.update(job, 'not_merged', status='staging_tampered',
                            reason='HEAD or the staging branch moved during the staging test run; merge undone')
                return expected
            if tests['status'] != 'passed':
                restore(expected)  # undo only the merge this runner just made
                self.update(job, 'not_merged', status=f"staging_tests_{tests['status']}",
                            reason='full test suite did not pass after the merge; staging left unchanged')
                return expected
            self.update(job, 'merged', status='merged', staging_commit=merge_sha)
            return merge_sha
        expected = start_sha
        job = None
        try:
            for job in verified:
                self.update(job, 'merging')
                try:
                    expected = merge_one(job, expected)
                except RollbackFailed:
                    raise
                except Exception as error:  # recover other failures before proceeding to another merge
                    restore(expected)
                    self.update(job, 'not_merged', status='staging_error',
                                reason=security.redact(f'{type(error).__name__}: {error}')[:300])
            job = None
            restore(expected)
        except RollbackFailed as error:
            self.stop_after_rollback_failure(staging_ref, expected, error, job)
        finally:
            try:
                _git(self.repo, 'worktree', 'remove', '--force', str(worktree))
            except (OSError, subprocess.SubprocessError):
                if self.rollback_failure is None:
                    raise


def run_batch(repo, issues=(), task_files=(), max_parallel=None, base='main', staging_branch=None, test_cmd=None,
              task_class=None, size='full', worker=None, gh_repo=None, policy=None, now=None, fetch_issue=None,
              run_job=None, verifier=None, fixer=None, test_runner=None):
    """Run 3-5 jobs into a nightly staging branch; returns the morning summary (also written to disk).

    fetch_issue(number) -> {'title', 'body'}; run_job(repo, task_file, **kw) -> run-job summary;
    verifier(job, prompt, policy) -> {'status', 'text', 'worker', 'verdict'};
    fixer(job, prompt, policy) -> {'ok', 'reason'?, 'worker'?, 'blocked_by_leak_check'?}. Defaults run the
    real gh/run-job/workers; test_runner is passed to every test run.
    """
    policy = policy or config.load_policy()
    settings = policy.get('batch') or {}
    if bool(issues) == bool(task_files):
        raise Refused('give either issue numbers or task files')
    sources = [int(n) for n in issues] or [Path(p).expanduser().resolve() for p in task_files]
    if len(set(sources)) != len(sources):
        raise Refused('the same job is listed twice')
    low, high = settings.get('min_jobs', MIN_JOBS), settings.get('max_jobs', MAX_JOBS)
    if not low <= len(sources) <= high:
        raise Refused(f'a batch takes {low} to {high} jobs, got {len(sources)}')
    cap = settings.get('max_parallel', DEFAULT_MAX_PARALLEL) if max_parallel is None else max_parallel
    if type(cap) is not int or cap < 1:
        raise Refused('max_parallel must be a positive integer')
    top = _git(Path(repo).expanduser(), 'rev-parse', '--show-toplevel')
    if top.returncode != 0:
        raise Refused('repo is not a git repository')
    repo = Path(top.stdout.strip()).resolve()
    started = now or td.now_local(policy)
    batch_id = started.strftime('%Y%m%d-%H%M%S')
    staging = check_staging_name(repo, staging_branch or f"staging/{started.strftime('%Y%m%d')}", base)
    base_sha = jd.resolve_base(repo, base)
    if not base_sha:
        raise Refused(f'base {base!r} is not a commit')
    test_cmd = test_cmd or jd.repo_settings(policy, repo).get('test_cmd') or jd.detect_test_cmd(repo, repo)
    if not test_cmd:
        raise Refused('no test command: a batch never merges untested work (pass --test-cmd)')

    out = batch_dir(policy, batch_id)
    jobs = []
    for source in sources:
        if isinstance(source, int):
            issue = (fetch_issue or (lambda n: gh_issue(n, repo, policy, gh_repo)))(source)
            key = f'issue-{source}'
            text = (f"# Issue #{source}: {security.redact(str(issue.get('title') or ''))}\n\n"
                    f"{security.redact(str(issue.get('body') or ''))}\n")
            task_file = _write_private(out / 'tasks' / f'{key}.md', text)
        else:
            if not source.is_file():
                raise Refused(f'task file not found: {source}')
            key, text, task_file = jd.slugify(source.stem), source.read_text(), source
        jobs.append({'key': key, 'source': f'#{source}' if isinstance(source, int) else str(source),
                     'task_file': str(task_file), 'task_text': text, 'state': 'queued', 'worker': None,
                     'branch': None, 'commit': None, 'tests': None, 'verdict': None, 'verifications': [],
                     'fix_rounds': [], 'staging_tests': None, 'status': None, 'reason': None})
    keys = [job['key'] for job in jobs]
    if len(set(keys)) != len(keys):
        raise Refused('two task files share a name')

    with jd.RepoLock(jd._path(policy, 'lock_dir', 'logs/locks'), repo):
        before = jd.main_state(repo)
        staging_sha = jd.resolve_base(repo, f'refs/heads/{staging}')
        if not staging_sha:
            _git(repo, 'branch', staging, base_sha, check=True)  # creates a ref only; no checkout changes
            staging_sha = base_sha
        root = jd._path(policy, 'worktree_root', 'worktrees')
        root.mkdir(parents=True, exist_ok=True)
        prefix = policy['run_job'].get('branch_prefix', 'router/')
        batch = Batch(repo, jobs, policy, batch_id, staging, test_cmd, test_runner,
                      verifier or default_verifier, fixer or default_fixer)
        for job in jobs:
            job.update(base_sha=staging_sha, branch=f"{prefix}batch-{batch_id}-{job['key']}", verified_sha=None)
        batch.write_status()
        options = {'size': size, 'worker': worker, 'task_class': task_class}
        if test_runner:
            options['test_runner'] = test_runner
        with ThreadPoolExecutor(max_workers=cap) as pool:
            list(pool.map(lambda job: batch.run_one(job, run_job or jd.run_job, options), jobs))
        batch.merge_all(root, staging_sha)
    for job in jobs:
        if job['state'] not in ('merged', 'not_merged', 'rollback_failed'):
            batch.update(job, 'not_merged', status=job.get('status'), reason=job.get('reason'))
    batch.write_status(final=True)

    summary = {'batch_id': batch_id, 'repo': str(repo), 'base': base, 'base_sha': base_sha,
               'staging_branch': staging, 'staging_start': staging_sha,
               'staging_restored_from': batch.staging_restored_from,
               'staging_head': jd.resolve_base(repo, f'refs/heads/{staging}'), 'test_command': test_cmd,
               'max_parallel': cap, 'started_at': started.isoformat(timespec='seconds'),
               'finished_at': _now(policy), 'pushed': False,
               'main_checkout_untouched': jd.main_state(repo) == before,
               'jobs': [{'job': job['key'], 'source': job['source'], 'worker': job['worker'],
                         'branch': job['branch'], 'commit': job['commit'], 'tests': job['tests'],
                         'verdict': job['verdict'], 'fix_rounds': len(job['fix_rounds']),
                         'staging_tests': job['staging_tests'], 'merged': job['state'] == 'merged',
                         'status': job['status'], 'reason': job['reason'], 'job_id': job.get('job_id'),
                         'worktree': job.get('worktree')} for job in jobs]}
    summary['merged'] = sum(job['merged'] for job in summary['jobs'])
    if batch.rollback_failure is not None:
        summary['rollback_failure'] = batch.rollback_failure
    summary['ok'] = (summary['merged'] == len(jobs) and summary['main_checkout_untouched']
                     and batch.rollback_failure is None)
    summary = security.redact_obj(summary)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'summary.json').write_text(json.dumps(summary, indent=2))
    (out / 'summary.md').write_text(render_markdown(summary))
    return dict(summary, files={'summary_json': str(out / 'summary.json'), 'summary_md': str(out / 'summary.md'),
                        'status': str(out / 'status.json'), 'events': str(out / 'events.jsonl')})


def render_markdown(summary):
    def cell(value):
        return str(value if value not in (None, '') else '-').replace('|', '/').replace('\n', ' ')
    rows = '\n'.join(
        f"| {cell(j['job'])} | {cell(j['worker'])} | `{cell(j['branch'])}` | `{cell((j['commit'] or '')[:10])}` | "
        f"{cell(j['tests'])} | {cell(j['verdict'])} | {'merged' if j['merged'] else cell(j['status'])} |"
        for j in summary['jobs'])
    notes = '\n'.join(f"- **{j['job']}**: {j['reason']}" for j in summary['jobs'] if not j['merged'] and j['reason'])
    failure = summary.get('rollback_failure')
    if failure:
        notes += (f"\n- **Rollback failed** for `{failure['ref']}`. Expected `{failure['expected_sha']}`, "
                  f"observed `{failure['observed_sha'] or 'unknown'}`. {failure['reason']}")
    return (f"# Batch {summary['batch_id']}: {summary['merged']}/{len(summary['jobs'])} merged into "
            f"`{summary['staging_branch']}`\n\n"
            f"Staging `{(summary['staging_start'] or '')[:10]}` -> `{(summary['staging_head'] or '')[:10]}` "
            f"(base `{summary['base']}`). Tests: `{summary['test_command']}`. Nothing was pushed; main was not touched.\n\n"
            '| Job | Worker | Branch | Commit | Tests | Verdict | Staging |\n|---|---|---|---|---|---|---|\n'
            f"{rows}\n" + (f"\n## Not merged\n{notes}\n" if notes else '') +
            '\nMerging staging into main still needs a human yes.\n')


# ---- CLI ----------------------------------------------------------------------------------
def register(commands):
    parser = commands.add_parser('batch', help='Overnight batch: run 3-5 jobs, verify, merge passing ones into '
                                               'staging/YYYYMMDD (never pushes, never touches main)')
    parser.add_argument('--repo', required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--issues', nargs='+', type=int, metavar='N', help='GitHub issue numbers (read via gh)')
    source.add_argument('--task-files', nargs='+', metavar='FILE', help='task files instead of issues')
    parser.add_argument('--max-parallel', type=int, help=f'jobs running at once (default {DEFAULT_MAX_PARALLEL})')
    parser.add_argument('--base', default='main', help='base for a new staging branch (default main)')
    parser.add_argument('--staging-branch', help='default staging/YYYYMMDD; reused when it already exists')
    parser.add_argument('--test-cmd', help='full test suite command (default: repos.json test_cmd or detected)')
    parser.add_argument('--gh-repo', help='owner/name for gh when --repo has no matching remote')
    parser.add_argument('--size', choices=('draft', 'full'), default='full')
    parser.add_argument('--worker')
    parser.add_argument('--class', dest='task_class', choices=config.TASK_CLASSES)


def run(args, policy):
    try:
        result = run_batch(args.repo, issues=args.issues or (), task_files=args.task_files or (),
                           max_parallel=args.max_parallel, base=args.base, staging_branch=args.staging_branch,
                           test_cmd=args.test_cmd, task_class=args.task_class, size=args.size, worker=args.worker,
                           gh_repo=args.gh_repo, policy=policy)
    except Refused as refusal:
        print(json.dumps({'ok': False, 'refused': str(refusal)}))
        return 3
    print(json.dumps(result, indent=2))
    return 0 if result['ok'] else 1
