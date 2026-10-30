"""run-job: one task in a fresh git worktree + branch, tested, retried once, committed locally.

Never touches the user's main checkout (its HEAD, branch, index or files), never commits on
the base branch, never pushes. A leak-check hit blocks the commit.
"""
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

from . import alerts, config, security, stackwich
from . import task_dispatch as td

Refused = td.Refused


def _git(cwd, *args, check=False):
    done = subprocess.run(['git', '-C', str(cwd), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=120)
    if check and done.returncode != 0:
        raise Refused(f'git {args[0]} failed: ' + security.redact(done.stderr.strip())[:300])
    return done


def resolve_base(repo, ref):
    """Commit sha for a base ref, or ''. A branch name wins over a same-named tag (git's own order prefers the tag,
    so `main` could resolve to a stale `main` tag): try refs/heads/, then refs/remotes/, then the ref as given."""
    candidates = [ref] if ref == 'HEAD' or ref.startswith('refs/') else [f'refs/heads/{ref}', f'refs/remotes/{ref}', ref]
    for candidate in candidates:
        sha = _git(repo, 'rev-parse', '-q', '--verify', candidate + '^{commit}').stdout.strip()
        if sha:
            return sha
    return ''


def _path(policy, key, default):
    return config.policy_path(policy, 'run_job', key, default)


def slugify(text, limit=40):
    slug = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return (slug[:limit].rstrip('-') or 'task')


def main_state(repo):
    """Fingerprint of the user's checkout: HEAD sha, branch, index/worktree status."""
    return {'head': _git(repo, 'rev-parse', '-q', '--verify', 'HEAD').stdout.strip(),
            'branch': _git(repo, 'symbolic-ref', '-q', 'HEAD').stdout.strip(),
            'status': _git(repo, 'status', '--porcelain', '--untracked-files=all').stdout}


class RepoLock:
    """Non-blocking flock per repo under logs/locks; refuses if another job holds it."""

    def __init__(self, lock_dir, repo):
        lock_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(str(repo).encode()).hexdigest()[:12]
        self.path = lock_dir / f'{slugify(repo.name)}-{digest}.lock'
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, 'a+')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise Refused(f'repo is locked by another run-job ({self.path.name})')
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(f'{os.getpid()}\n')
        self.handle.flush()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def repo_settings(policy, repo):
    path = _path(policy, 'repo_config', 'repos.json')
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data.get(str(repo)) or data.get(repo.name) or {}


def run_tests(policy, cwd, command, timeout, log_path, runner=td.run_bounded):
    if not command:
        return {'status': 'skipped', 'command': None}
    env, _ = td.worker_environment(policy)
    result = runner(['/bin/sh', '-c', command], cwd, timeout, policy['run_task']['kill_grace_seconds'],
                    env=env,
                    stall_seconds=policy['run_job'].get('test_stall_seconds'))
    output = security.redact(result['stdout'] + result['stderr'])
    log_path.write_text(f'$ {security.redact(command)}\nexit={result["returncode"]} timed_out={result["timed_out"]} '
                        f'stalled={result.get("stalled")}\n{output}')
    status = ('timeout' if result['timed_out'] else 'passed' if result['returncode'] == 0 else 'failed')
    return {'status': status, 'command': security.redact(command), 'returncode': result['returncode'],
            'elapsed_seconds': result['elapsed_seconds'], 'log': str(log_path),
            'tail': output[-policy['run_job'].get('test_output_tail_chars', 4000):]}


def _is_bytecode_cache(path):
    """A CPython 3 cache file: .pyc/.pyo directly inside a __pycache__ directory. A .pyc anywhere else can be
    imported as a sourceless module, so it is a real change."""
    parts = path.split('/')
    return len(parts) >= 2 and parts[-2] == '__pycache__' and parts[-1].endswith(('.pyc', '.pyo'))


def remove_bytecode_caches(cwd):
    """Delete every untracked .pyc/.pyo and any symlinked __pycache__ the worker left, including gitignored ones
    (git status does not list those), so tests rebuild caches from the reviewed source and never run planted
    bytecode. The walk holds directory descriptors and never follows links, and each deletion is relative to the
    descriptor, so a path swapped mid-walk (e.g. by a leftover worker process) cannot redirect it outside the tree.
    A link is removed itself, never its target."""
    tracked = set(filter(None, _git(cwd, 'ls-tree', '-r', '--name-only', '-z', 'HEAD').stdout.split('\0')))
    for folder, dirs, files, dir_fd in os.fwalk(str(cwd), follow_symlinks=False):
        rel_dir = os.path.relpath(folder, str(cwd))
        if rel_dir == '.' and '.git' in dirs:
            dirs.remove('.git')
        # A symlinked __pycache__ is never descended into; it may be listed with the dirs or the files.
        for name in list(dirs):
            if name == '__pycache__' and stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode):
                os.unlink(name, dir_fd=dir_fd)
                dirs.remove(name)
        for name in files:
            rel = name if rel_dir == '.' else f'{rel_dir}/{name}'.replace(os.sep, '/')
            linked = stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
            if (name == '__pycache__' and linked) or (name.endswith(('.pyc', '.pyo')) and rel not in tracked):
                os.unlink(name, dir_fd=dir_fd)


def changed_paths(cwd):
    """Paths with any change vs HEAD (tracked or untracked), from porcelain -z. Untracked interpreter caches are
    left out (counting them caused spurious out-of-scope failures); tracked ones still count."""
    out = _git(cwd, 'status', '--porcelain', '-z', '--untracked-files=all').stdout
    entries, paths, skip = out.split('\0'), set(), False
    for entry in entries:
        if skip:
            skip = False
            paths.add(entry)
            continue
        if len(entry) < 4:
            continue
        if not (entry.startswith('?? ') and _is_bytecode_cache(entry[3:])
                and not os.path.islink(os.path.join(cwd, entry[3:]))):
            paths.add(entry[3:])
        if entry[0] in 'RC':
            skip = True  # next entry is the rename source
    return paths


def _has_changes(cwd):
    return bool(_git(cwd, 'status', '--porcelain', '--untracked-files=all').stdout.strip())


def _report_leak_check(policy, worktree, base_sha):
    # Include even ignored reports, but never follow an unsafe report path.
    try:
        stackwich._safe_path(worktree)
    except ValueError:
        return {'status': 'clean', 'findings': []}  # evidence validation rejects unsafe paths
    return security.leak_check(worktree, base_sha, [stackwich.REPORT_PATH], td.private_terms(policy),
                               **td.leak_options(policy))


def _retry_note(attempt, tests):
    if attempt.get('refused'):
        return None
    lines = ['## Previous attempt did not pass', 'Your earlier changes are still in this checkout. Fix them.']
    if not attempt.get('ok'):
        lines.append('The worker run failed or made no usable change.')
    if tests.get('status') in ('failed', 'timeout'):
        lines += [f'The test command `{tests["command"]}` {tests["status"]}. Output tail:', '```',
                  tests.get('tail', ''), '```']
    return '\n'.join(lines)


def _commit_message(task_text, job_id, workers, tests, verified=False):
    title = next((line.strip('# ').strip() for line in task_text.splitlines() if line.strip()), 'task')
    title = security.redact(title)[:60]
    return (f'router: {title}\n\nRouter-Job: {job_id}\nWorkers: {", ".join(workers) or "none"}\n'
            f'Tests: {tests.get("status")}\nCommitted by run-job in a worktree branch; not pushed.' +
            ('\n\nStackwich: verified' if verified else ''))


def run_job(repo, task_file, base=None, branch=None, test_cmd=None, size='full', worker=None,
            keep=None, policy=None, run_task=td.run_task, select=None, now=None, test_runner=td.run_bounded,
            popen=subprocess.Popen, notifier=None):
    """keep: True (--keep), False (--prune) or None (keep on success, prune on failure)."""
    policy = policy or config.load_policy()
    if worker is not None and config.blocked_worker(policy, worker):
        raise Refused(f'{worker} is in never; remove it from never to use it')
    started = now or td.now_local(policy)
    stamp = started.strftime('%Y%m%d-%H%M%S')
    top = _git(Path(repo).expanduser(), 'rev-parse', '--show-toplevel')
    if top.returncode != 0:
        raise Refused('repo is not a git repository')
    repo = Path(top.stdout.strip()).resolve()
    if _git(repo, 'rev-parse', '--is-bare-repository').stdout.strip() == 'true':
        raise Refused('bare repositories are not supported')
    task_path = Path(task_file).expanduser().resolve()
    task_text = task_path.read_text()
    settings = repo_settings(policy, repo)
    test_cmd = test_cmd if test_cmd is not None else settings.get('test_cmd')
    test_timeout = settings.get('test_timeout_seconds', policy['run_job']['test_timeout_seconds'])
    base_ref = base or 'HEAD'
    base_sha = resolve_base(repo, base_ref)
    if not base_sha:
        raise Refused(f'base {base_ref!r} is not a commit')
    slug = slugify(task_path.stem)
    exists = lambda name: _git(repo, 'rev-parse', '-q', '--verify', f'refs/heads/{name}').returncode == 0
    if branch is not None:
        if _git(repo, 'check-ref-format', '--branch', branch).returncode != 0:
            raise Refused(f'invalid branch name {branch!r}')
        if exists(branch):
            raise Refused(f'branch {branch} already exists')
    root = _path(policy, 'worktree_root', 'worktrees')
    prefix = policy['run_job'].get('branch_prefix', 'router/')
    n = 1
    while True:  # two jobs started in the same second get a -N suffix
        suffix = '' if n == 1 else f'-{n}'
        worktree = root / f'{slugify(repo.name)}-{stamp}{suffix}'
        candidate = branch or f'{prefix}{slug}-{stamp}{suffix}'
        if not (worktree.exists() or (_path(policy, 'jobs_dir', 'logs/jobs') / f'{worktree.name}.json').exists()
                or (branch is None and exists(candidate))):
            break
        n += 1
    branch = candidate
    if _git(repo, 'check-ref-format', '--branch', branch).returncode != 0:
        raise Refused(f'invalid branch name {branch!r}')
    job_id = worktree.name
    out_dir = td._router_path(policy, 'output_dir', 'logs/task-output')
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {'job_id': job_id, 'repo': str(repo), 'task': task_path.name, 'base': base_ref,
               'base_sha': base_sha, 'branch': branch, 'worktree': str(worktree), 'size': size,
               'started_at': started.isoformat(timespec='seconds'), 'finished_at': None,
               'ok': False, 'status': None, 'commit': None, 'diff_stat': '', 'tests': [],
               'tests_status': None, 'leak_check': None, 'blocked_by_leak_check': False,
               'attempts': [], 'workers': [], 'worktree_state': None, 'branch_state': None,
               'main_checkout_untouched': None, 'warnings': []}
    with RepoLock(_path(policy, 'lock_dir', 'logs/locks'), repo), _NoSleep(policy, popen):
        before = main_state(repo)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, 'worktree', 'add', '-q', '-b', branch, str(worktree), base_sha, check=True)
        try:
            _job_body(policy, summary, task_path, task_text, worktree, size, worker, test_cmd,
                      test_timeout, stamp, out_dir, run_task, select, test_runner)
        except Refused as refusal:
            summary['status'] = 'refused'
            summary['warnings'].append(str(refusal))
        except Exception as error:  # report and still clean up
            summary['status'] = 'error'
            summary['warnings'].append(f'{type(error).__name__}: {security.redact(str(error))[:300]}')
        finally:
            _finish(policy, summary, repo, worktree, branch, base_sha, keep)
            after = main_state(repo)
            summary['main_checkout_untouched'] = after == before
            if after != before:
                summary['ok'] = False
                summary['warnings'].append('main checkout state changed during the job: review it')
    summary['finished_at'] = td.now_local(policy).isoformat(timespec='seconds')
    summary = security.redact_obj(summary)
    jobs_dir = _path(policy, 'jobs_dir', 'logs/jobs')
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f'{job_id}.json').write_text(json.dumps(summary, indent=2))
    td.append_ledger(td._router_path(policy, 'ledger', 'logs/ledger.csv'),
                     [started.strftime('%Y-%m-%d %H:%M'), 'run-job', f'run-job {job_id} {task_path.name}',
                      '+'.join(summary['workers']) or 'none', '',
                      f'{summary["status"]}; branch={branch}; commit={(summary["commit"] or "none")[:10]}; '
                      f'tests={summary["tests_status"]}; leak={(summary["leak_check"] or {}).get("status")}; '
                      f'worktree={summary["worktree_state"]}'])
    alerts.job_alert(policy, summary, notifier=notifier)
    return summary


class _NoSleep:
    """`caffeinate -i -w <pid>` for the job only (run_job.caffeinate); exits with us. No window."""

    def __init__(self, policy, popen):
        self.enabled = policy.get('run_job', {}).get('caffeinate', False) and Path('/usr/bin/caffeinate').exists()
        self.popen, self.process = popen, None

    def __enter__(self):
        if self.enabled:
            try:
                self.process = self.popen(['/usr/bin/caffeinate', '-i', '-w', str(os.getpid())],
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
            except OSError:
                self.process = None
        return self

    def __exit__(self, *exc):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def _job_body(policy, summary, task_path, task_text, worktree, size, worker, test_cmd, test_timeout,
              stamp, out_dir, run_task, select, test_runner):
    extra = None
    test_artifacts, worker_paths = set(), set()
    required, pinned, evidence = False, None, None
    retries = policy['run_job'].get('max_retries', 1)
    attempt_number = 0
    while attempt_number <= (1 if required else retries):
        cycle = attempt_number + 1
        kwargs = {'size': size, 'worker': pinned or worker, 'allow_dirty': attempt_number > 0,
                  'policy': policy, 'extra_prompt': extra, 'stamp': f'{stamp}-a{cycle}',
                  'stackwich_cycle': cycle}
        if pinned == 'claude' and worker is None:
            kwargs['_claude_pinned'] = True
        if select is not None:
            kwargs['select'] = select
        try:
            result = run_task(task_path, worktree, **kwargs)
        except Refused as refusal:
            summary['attempts'].append({'attempt': cycle, 'refused': str(refusal)})
            if required:
                summary['status'] = 'blocked_by_stackwich'
                summary['stackwich'] = {'status': 'blocked', 'cycle': cycle,
                                         'errors': ['correction invocation refused']}
                return
            if attempt_number == 0:
                raise
            break
        runs = result.get('attempts', [])
        claude_quota = any(r.get('worker') == 'claude' and r.get('quota_error') for r in runs)
        claude_refusal = any(r.get('worker') == 'claude' and r.get('success_check') == 'refusal'
                             for r in runs)
        invoked = [r.get('worker') for r in runs
                   if (r.get('stackwich') or {}).get('status') == 'required' and
                   not (r.get('worker') == 'claude' and r.get('quota_error'))]
        selected = result.get('worker')
        if claude_quota and selected != 'claude':
            # The Stackwich-capable replacement owns subsequent corrections; a required cycle stays required.
            pinned, evidence = selected, None
        active = (bool(invoked) or (result.get('stackwich') or {}).get('status') == 'required' or
                  (selected is not None and stackwich.status(policy, selected) == 'required'))
        if active and not required:
            required, pinned = True, (invoked[0] if invoked else selected)
        summary['stackwich'] = result.get('stackwich', {'status': 'disabled'})
        summary['leak_check'] = result.get('leak_check')
        summary['blocked_by_leak_check'] = result.get('blocked_by_leak_check', False)
        if summary['blocked_by_leak_check'] or (required and (result.get('leak_check') or {}).get('status') != 'clean'):
            summary['blocked_by_leak_check'] = True
            summary['status'] = 'blocked_by_leak_check'
            summary['attempts'].append({'attempt': cycle, 'worker': selected, 'ok': False,
                                        'leak_check': (result.get('leak_check') or {}).get('status')})
            return
        remove_bytecode_caches(worktree)
        worker_paths = changed_paths(worktree) - test_artifacts
        if required:
            worker_paths.discard(stackwich.REPORT_PATH)
        changed = bool(worker_paths)
        if required:
            scan = _report_leak_check(policy, worktree, summary['base_sha'])
            if scan['status'] != 'clean':
                summary.update(status='blocked_by_leak_check', blocked_by_leak_check=True,
                               leak_check=scan)
                summary['attempts'].append({'attempt': cycle, 'worker': selected,
                                            'ok': False, 'leak_check': scan['status']})
                return
        evidence = stackwich.evidence(worktree, worker_paths, cycle) if required else None
        before = stackwich.fingerprints(worktree, worker_paths) if required else None
        tests = {'status': 'not_run'}
        if result['ok'] and changed:
            tests = run_tests(policy, worktree, test_cmd, test_timeout,
                              out_dir / f'{stamp}-a{cycle}-tests.log', test_runner)
            test_artifacts |= changed_paths(worktree) - worker_paths - {stackwich.REPORT_PATH}
        if required:
            scan = _report_leak_check(policy, worktree, summary['base_sha'])
            if scan['status'] != 'clean':
                summary.update(status='blocked_by_leak_check', blocked_by_leak_check=True,
                               leak_check=scan)
                summary['attempts'].append({'attempt': cycle, 'worker': selected, 'ok': False,
                                            'tests': tests['status'], 'leak_check': scan['status']})
                return
            final_evidence = stackwich.evidence(worktree, changed_paths(worktree) - test_artifacts -
                                                {stackwich.REPORT_PATH}, cycle)
            errors = list(evidence['errors']) + final_evidence['errors']
            if evidence.get('digest') != final_evidence.get('digest'):
                errors.append('report changed during router tests')
            if before != stackwich.fingerprints(worktree, worker_paths):
                errors.append('product contents changed during router tests')
            if tests['status'] != 'passed':
                errors.append('router tests must pass (a real test command is required)')
            if not result.get('ok'):
                errors.append('worker invocation did not succeed')
            if not pinned or selected != pinned or (result.get('stackwich') or {}).get('status') != 'required':
                errors.append('required invocation metadata missing or worker changed')
            if _git(worktree, 'rev-parse', 'HEAD').stdout.strip() != summary['base_sha']:
                errors.append('worker changed HEAD')
            evidence['errors'] = sorted(set(errors))
            evidence['status'] = 'blocked' if errors else 'verified'
            summary['stackwich'] = evidence
        summary['tests'].append(tests)
        summary['tests_status'] = tests['status']
        summary['attempts'].append({'attempt': cycle, 'worker': selected, 'ok': result.get('ok'),
                                    'changed': changed, 'runs': result.get('attempts'),
                                    'tests': tests['status'], 'stackwich': evidence,
                                    'leak_check': (result.get('leak_check') or {}).get('status'),
                                    'warnings': result.get('warnings')})
        if selected and selected not in summary['workers']:
            summary['workers'].append(selected)
        if result['ok'] and changed and tests['status'] in ('passed', 'skipped') and (
                not required or evidence['status'] == 'verified'):
            break
        if claude_refusal or (claude_quota and not result.get('ok')):
            summary['status'] = 'blocked_by_stackwich' if required else 'failed'
            return
        extra = _retry_note(result, tests) or ''
        if required:
            extra += '\nStackwich errors: ' + '; '.join(evidence['errors'])
        attempt_number += 1
    else:
        summary['status'] = 'blocked_by_stackwich' if required else 'failed'
        return
    last = summary['attempts'][-1]
    if not (last.get('ok') and last.get('changed') and last.get('tests') in ('passed', 'skipped')):
        summary['status'] = 'blocked_by_stackwich' if required else 'failed'
        return
    if required:
        final_evidence = stackwich.evidence(worktree, changed_paths(worktree) - test_artifacts -
                                            {stackwich.REPORT_PATH}, cycle)
        if final_evidence['errors'] or final_evidence.get('digest') != evidence.get('digest') or (
                before != stackwich.fingerprints(worktree, worker_paths)):
            summary['status'] = 'blocked_by_stackwich'
            summary['stackwich'] = dict(final_evidence, status='blocked',
                                         errors=final_evidence['errors'] + ['final commit evidence changed'])
            return
    if _git(worktree, 'rev-parse', 'HEAD').stdout.strip() != summary['base_sha']:
        summary['status'] = 'blocked_by_stackwich' if required else 'failed'
        return
    _git(worktree, 'add', '-A', '--', *sorted(worker_paths), check=True)
    if required:
        staged = set(filter(None, _git(worktree, 'diff', '--cached', '--name-only', '--no-renames', '-z').stdout.split('\0')))
        if staged != worker_paths:
            _git(worktree, 'reset', '-q')
            summary['status'] = 'blocked_by_stackwich'
            summary['stackwich']['errors'].append('staged paths differ from validated scope')
            summary['stackwich']['status'] = 'blocked'
            return
    summary['excluded_test_artifacts'] = sorted(test_artifacts)
    final = security.leak_check(worktree, summary['base_sha'], [], td.private_terms(policy), cached=True,
                                **td.leak_options(policy))
    summary['leak_check'] = final
    if final['status'] != 'clean':
        _git(worktree, 'reset', '-q')
        summary['blocked_by_leak_check'] = True
        summary['status'] = 'blocked_by_leak_check'
        return
    identity = policy['run_job']['commit_identity']
    commit = _git(worktree, '-c', f'user.name={identity["name"]}', '-c', f'user.email={identity["email"]}',
                  'commit', '-q', '-m', _commit_message(task_text, summary['job_id'], summary['workers'],
                                                       {'status': summary['tests_status']}, verified=required))
    if commit.returncode != 0:
        summary['status'] = 'commit_failed'
        summary['warnings'].append('git commit failed: ' + security.redact(commit.stderr.strip() or commit.stdout.strip())[:300])
        return
    sha = _git(worktree, 'rev-parse', 'HEAD').stdout.strip()
    if _git(worktree, 'symbolic-ref', '-q', 'HEAD').stdout.strip() != f'refs/heads/{summary["branch"]}':
        raise RuntimeError('worktree is not on the router branch')
    summary['commit'] = sha
    summary['diff_stat'] = _git(worktree, 'diff', '--stat', summary['base_sha'], sha).stdout.strip()
    summary['ok'] = True
    summary['status'] = 'committed'


def _finish(policy, summary, repo, worktree, branch, base_sha, keep):
    if summary['status'] is None:
        summary['status'] = 'failed'
    success = summary['ok']
    leak = summary['blocked_by_leak_check']
    blocked = summary['status'] == 'blocked_by_stackwich'
    prune = keep is False or (keep is None and not success and not leak and not blocked)
    if blocked and keep is None:
        summary['warnings'].append('Stackwich blocked: worktree kept for review')
    if leak and keep is None:
        summary['warnings'].append('leak check hit: worktree kept for review (nothing deleted)')
    if prune:
        removed = _git(repo, 'worktree', 'remove', '--force', str(worktree))
        summary['worktree_state'] = 'pruned' if removed.returncode == 0 else 'prune_failed'
        branch_sha = _git(repo, 'rev-parse', '-q', '--verify', f'refs/heads/{branch}').stdout.strip()
        if summary['commit'] is None and branch_sha == base_sha:
            deleted = _git(repo, 'branch', '-D', branch)
            summary['branch_state'] = 'deleted' if deleted.returncode == 0 else 'delete_failed'
        else:
            summary['branch_state'] = 'kept'
    else:
        summary['worktree_state'] = 'kept'
        summary['branch_state'] = 'kept'


# ---- jobs / cleanup ----------------------------------------------------------------

def list_jobs(policy=None, limit=20):
    policy = policy or config.load_policy()
    jobs_dir = _path(policy, 'jobs_dir', 'logs/jobs')
    root = _path(policy, 'worktree_root', 'worktrees')
    records = []
    for path in sorted(jobs_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            job = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        records.append({key: job.get(key) for key in ('job_id', 'status', 'ok', 'branch', 'commit', 'workers',
                                                       'tests_status', 'worktree', 'worktree_state',
                                                       'started_at')})
        records[-1]['worktree_exists'] = Path(job.get('worktree') or '/nonexistent').exists()
    known = {r['worktree'] for r in records}
    orphans = [str(p) for p in sorted(root.glob('*')) if p.is_dir() and str(p) not in known] if root.exists() else []
    ledger = td._router_path(policy, 'ledger', 'logs/ledger.csv')
    rows = [line for line in ledger.read_text().splitlines() if ',run-job,' in line][-limit:] if ledger.exists() else []
    return {'jobs': records, 'other_worktrees': orphans, 'ledger_rows': rows}


def parse_age(text):
    match = re.fullmatch(r'(\d+)([mhd])', text.strip())
    if not match:
        raise Refused('use --older-than like 30m, 12h or 7d')
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(minutes=amount) if unit == 'm' else timedelta(hours=amount) if unit == 'h' else timedelta(days=amount)


def cleanup(older_than, dry_run=False, policy=None, now=None):
    """Remove router worktrees older than the age. Keeps branches with commits; skips dirty trees."""
    policy = policy or config.load_policy()
    age = parse_age(older_than)
    root = _path(policy, 'worktree_root', 'worktrees')
    cutoff = (now or datetime.now()).timestamp() - age.total_seconds()
    jobs = {}
    for path in _path(policy, 'jobs_dir', 'logs/jobs').glob('*.json'):
        try:
            job = json.loads(path.read_text())
            jobs[job.get('worktree')] = job
        except json.JSONDecodeError:
            pass
    report = {'removed': [], 'skipped': [], 'dry_run': dry_run}
    for worktree in sorted(root.glob('*')) if root.exists() else []:
        if not worktree.is_dir() or worktree.stat().st_mtime > cutoff:
            continue
        common = _git(worktree, 'rev-parse', '--path-format=absolute', '--git-common-dir')
        if common.returncode != 0 or not (worktree / '.git').is_file():
            report['skipped'].append({'worktree': str(worktree), 'why': 'not a git worktree'})
            continue
        repo = Path(common.stdout.strip()).parent
        job = jobs.get(str(worktree), {})
        artifacts = set(job.get('excluded_test_artifacts') or [])
        pending = changed_paths(worktree)
        if pending - artifacts:
            report['skipped'].append({'worktree': str(worktree), 'why': 'uncommitted changes'})
            continue
        branch = _git(worktree, 'symbolic-ref', '-q', '--short', 'HEAD').stdout.strip()
        entry = {'worktree': str(worktree), 'branch': branch, 'branch_action': 'kept'}
        if not dry_run:
            # --force only discards the job's own recorded test artifacts (checked above).
            _git(repo, 'worktree', 'remove', *(['--force'] if pending else []), str(worktree), check=True)
            base_sha = job.get('base_sha')
            head = _git(repo, 'rev-parse', '-q', '--verify', f'refs/heads/{branch}').stdout.strip() if branch else ''
            if branch.startswith(policy['run_job'].get('branch_prefix', 'router/')) and base_sha and head == base_sha:
                _git(repo, 'branch', '-D', branch)
                entry['branch_action'] = 'deleted (no commits)'
        report['removed'].append(entry)
    return report
