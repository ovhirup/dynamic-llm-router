"""run-job: one task in a fresh git worktree + branch, tested, retried once, committed locally.

Never touches the user's main checkout (its HEAD, branch, index or files), never commits on
the base branch, never pushes. A leak-check hit blocks the commit.
"""
from datetime import datetime, timedelta
import fcntl
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

from . import alerts, brain, config, handoff, security, stackwich, task_dag
from . import task_dispatch as td

Refused = td.Refused

# Output from these (weaker or local) workers needs a stronger-model review before the owner sees it.
REVIEWED_WORKERS = ('codex', 'cursor', 'local', 'developer', 'nativ')
REVIEW_PROMPT_FILE = '.llm-router-review-prompt.md'
DEFAULT_REVIEWER = 'grok-bot'


def review_required(worker):
    return worker in REVIEWED_WORKERS or str(worker or '').startswith('aider')


def build_review_prompt(summary, diff, paths, tests_status):
    scope = '\n'.join(f'- {path}' for path in sorted(paths)) or '- (none)'
    return (f'# Stronger-model review: job {summary["job_id"]}\n\n'
            f'Workers: {", ".join(summary.get("workers") or []) or "unknown"}\n'
            f'Branch: {summary.get("branch")}\nBase: {summary.get("base_sha")}\n'
            f'Commit: {summary.get("commit")}\n\n'
            f'## Declared scope / changed paths\n{scope}\n\n'
            f'## Test status\n{tests_status}\n\n'
            '## Instructions\nReview the diff for correctness, scope creep, leaked secrets and missing tests. '
            'Answer with a verdict of `approve` or `reject` and short notes.\n\n'
            f'## Diff\n```diff\n{diff}\n```\n')


def _stronger_review(summary, worktree, paths, reviewer):
    """Write the review prompt into the worktree's git dir and record the review (or mark it pending)."""
    git_dir = Path(_git(worktree, 'rev-parse', '--absolute-git-dir', check=True).stdout.strip())
    diff = _git(worktree, 'diff', f'{summary["base_sha"]}..{summary["commit"]}').stdout
    prompt_path = git_dir / REVIEW_PROMPT_FILE
    prompt_path.unlink(missing_ok=True)
    fd = os.open(prompt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(build_review_prompt(summary, diff, paths, summary.get('tests_status')))
    os.chmod(prompt_path, 0o600)
    if reviewer is None:
        return {'status': 'pending', 'reviewer': DEFAULT_REVIEWER, 'prompt_file': str(prompt_path)}
    try:
        review = dict(reviewer(summary, diff, prompt_path) or {})
    except Exception as error:  # an unreviewed change is never treated as approved
        return {'status': 'error', 'reviewer': getattr(reviewer, '__name__', 'reviewer'),
                'prompt_file': str(prompt_path),
                'notes': f'{type(error).__name__}: {security.redact(str(error))[:300]}'}
    review.setdefault('prompt_file', str(prompt_path))
    return review


_SCOPE_HEADING = re.compile(r'^\s*#{1,6}\s*(?:allowed\s+(?:paths|files)|scope)\s*:?\s*$', re.I)
_SCOPE_INLINE = re.compile(r'^\s*(?:allowed\s+(?:paths|files)|scope)\s*:\s*(.+)$', re.I)
_HEADING = re.compile(r'^\s*#{1,6}\s+\S')
_BULLET = re.compile(r'^\s*[-*]\s+(.+?)\s*$')


def parse_allowed_paths(task_text):
    """Repo-relative files or trailing-'/' directories declared in a task file, or None when undeclared."""
    raw = []
    in_section = False
    for line in (task_text or '').splitlines():
        if _SCOPE_HEADING.match(line):
            in_section = True
            continue
        if in_section and _HEADING.match(line):
            in_section = False
        inline = _SCOPE_INLINE.match(line)
        if inline:
            for item in re.split(r'[,\s]+', inline.group(1).strip()):
                cleaned = item.strip().strip('`\'"')
                if cleaned:
                    raw.append(cleaned)
            continue
        if in_section:
            bullet = _BULLET.match(line)
            if bullet:
                cleaned = bullet.group(1).strip().strip('`\'"')
                if cleaned:
                    raw.append(cleaned)
    if not raw:
        return None
    try:
        return sorted({task_dag._scope_path(p) for p in raw})
    except task_dag.DagError as error:
        raise Refused(f'invalid allowed path in task file: {error}') from None


def out_of_scope(paths, scope):
    """Changed paths not covered by any scope entry (exact match or under a trailing-'/' directory)."""
    return sorted(p for p in paths if not any(s == p or task_dag._covers(s, p) for s in scope))


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


NO_TESTS_REASON = 'no test command configured or detected'
_NPM_PLACEHOLDER = 'no test specified'


def _pytest_configured(root):
    if (root / 'pytest.ini').is_file():
        return True
    for name, marker in (('pyproject.toml', '[tool.pytest'), ('setup.cfg', '[tool:pytest]')):
        try:
            if marker in (root / name).read_text(errors='replace'):
                return True
        except OSError:
            pass
    return False


def detect_test_cmd(worktree, source_repo=None):
    """A test command inferred from the checkout at base (Python, Node, Swift), or None. Python runs with the source
    repo's .venv interpreter when it has one, else this interpreter."""
    root = Path(worktree)
    venv = Path(source_repo or root) / '.venv' / 'bin' / 'python'
    python = shlex.quote(str(venv) if venv.is_file() else sys.executable)
    if _pytest_configured(root):
        return f'{python} -m pytest'
    if (root / 'tests').is_dir():
        prefix = 'PYTHONPATH=src ' if (root / 'src').is_dir() else ''
        return f'{prefix}{python} -m unittest discover -s tests'
    try:
        script = (json.loads((root / 'package.json').read_text()).get('scripts') or {}).get('test')
    except (OSError, ValueError, AttributeError):
        script = None
    if isinstance(script, str) and script.strip() and _NPM_PLACEHOLDER not in script:
        return 'npm test --silent'
    if (root / 'Package.swift').is_file():
        return 'swift test'
    return None


def run_tests(policy, cwd, command, timeout, log_path, runner=td.run_bounded):
    if not command:
        return {'status': 'no_tests', 'command': None}
    env, _ = td.worker_environment(policy)
    # Python reads and writes bytecode only under a fresh router-owned prefix, never an in-tree __pycache__
    # (linked or not), so bytecode planted in the worktree or behind a tracked cache link is never executed.
    with tempfile.TemporaryDirectory(prefix='llm-router-pycache-') as pycache:
        env = dict(env, PYTHONPYCACHEPREFIX=pycache)
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
        # Anything tracked at HEAD is baseline content and is never removed.
        for name in list(dirs):
            rel = name if rel_dir == '.' else f'{rel_dir}/{name}'.replace(os.sep, '/')
            if name == '__pycache__' and stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode):
                dirs.remove(name)
                if rel not in tracked:
                    os.unlink(name, dir_fd=dir_fd)
        for name in files:
            rel = name if rel_dir == '.' else f'{rel_dir}/{name}'.replace(os.sep, '/')
            if rel in tracked:
                continue
            linked = stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode)
            if (name == '__pycache__' and linked) or name.endswith(('.pyc', '.pyo')):
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


def hops(summary):
    """Every worker run in the job, in order: worker, reason (ok, quota_error, auth_error, timeout, ...)."""
    out = []
    for attempt in summary.get('attempts') or []:
        for run in attempt.get('runs') or []:
            if run.get('worker'):
                out.append({'cycle': attempt.get('attempt'), 'worker': run['worker'],
                            'reason': run.get('reason') or td.hop_reason(run), 'ok': bool(run.get('ok')),
                            'elapsed_seconds': run.get('elapsed_seconds'),
                            'unavailable': bool(run.get('quota_error') or run.get('auth_error'))})
    return out


def run_job(repo, task_file, base=None, branch=None, test_cmd=None, size='full', worker=None,
            keep=None, policy=None, run_task=td.run_task, select=None, now=None, test_runner=td.run_bounded,
            popen=subprocess.Popen, notifier=None, meter=None, task_class=None,
            reviewer=None, allowed_paths=None):
    """keep: True (--keep), False (--prune) or None (keep on success, prune on failure).
    meter(worker) -> percent used or None (default: the cached meter, never a live read).
    task_class ranks workers by class_order.<task_class> (see selection.select)."""
    policy = policy or config.load_policy()
    if task_class is not None:
        config.worker_order(policy, task_class)  # unknown class -> ConfigError before any worktree
        run_task = functools.partial(run_task, task_class=task_class)
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
    if allowed_paths is not None:
        try:
            scope = sorted({task_dag._scope_path(p) for p in allowed_paths})
        except task_dag.DagError as error:
            raise Refused(f'invalid allowed path: {error}') from None
    else:
        scope = parse_allowed_paths(task_text)
    settings = repo_settings(policy, repo)
    if test_cmd:
        test_source = 'cli'
    elif settings.get('test_cmd'):
        test_cmd, test_source = settings['test_cmd'], 'config'
    else:
        test_cmd, test_source = None, None
    allow_no_tests = settings.get('allow_no_tests') is True
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
               'test_command': None, 'test_command_source': None, 'allow_no_tests': allow_no_tests,
               'tests_status': None, 'leak_check': None, 'blocked_by_leak_check': False,
               'allowed_paths': scope, 'diff_scope': None,
               'attempts': [], 'workers': [], 'worktree_state': None, 'branch_state': None,
               'main_checkout_untouched': None, 'handoffs': [], 'warnings': [],
               'review_required': False, 'review': None}
    with RepoLock(_path(policy, 'lock_dir', 'logs/locks'), repo), _NoSleep(policy, popen):
        before = main_state(repo)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, 'worktree', 'add', '-q', '-b', branch, str(worktree), base_sha, check=True)
        try:
            if not test_cmd:  # detected at base, so the worker cannot choose the command that judges it
                test_cmd = detect_test_cmd(worktree, repo)
                test_source = 'auto' if test_cmd else None
            summary['test_command'] = security.redact(test_cmd) if test_cmd else None
            summary['test_command_source'] = test_source
            if test_cmd or allow_no_tests:
                _job_body(policy, summary, task_path, task_text, worktree, size, worker, test_cmd,
                          test_timeout, stamp, out_dir, run_task, select, test_runner, allow_no_tests,
                          meter or handoff.meter_percent, reviewer=reviewer, scope=scope)
            else:  # fail before spending a worker run on a change nothing would verify
                summary.update(status='no_tests', tests_status='no_tests', reason=NO_TESTS_REASON)
                summary['warnings'].append(NO_TESTS_REASON + ': set test_cmd, pass --test-cmd, or set '
                                           '"allow_no_tests": true in repos.json')
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
    if not summary['ok']:
        cause = td.failure_fields(summary['attempts'])
        for key in ('failure_reason', 'stderr_tail', 'error'):
            if cause.get(key) and not summary.get(key):
                summary[key] = cause[key]
    summary['hops'] = hops(summary)
    summary['finished_at'] = td.now_local(policy).isoformat(timespec='seconds')
    summary = security.redact_obj(summary)
    jobs_dir = _path(policy, 'jobs_dir', 'logs/jobs')
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f'{job_id}.json').write_text(json.dumps(summary, indent=2))
    try:  # the vault note is a by-product: never fail the job over it
        note_path = brain.write_job_outcome(policy, summary)
    except OSError as error:
        note_path = None
        summary['warnings'].append(f'vault note not written: {security.redact(str(error))[:200]}')
    summary['vault_note'] = str(note_path) if note_path else None
    (jobs_dir / f'{job_id}.json').write_text(json.dumps(summary, indent=2))
    td.append_ledger(td._router_path(policy, 'ledger', 'logs/ledger.csv'),
                     [started.strftime('%Y-%m-%d %H:%M'), 'run-job', f'run-job {job_id} {task_path.name}',
                      '+'.join(summary['workers']) or 'none', '',
                      f'{summary["status"]}; branch={branch}; commit={(summary["commit"] or "none")[:10]}; '
                      f'tests={summary["tests_status"]}; leak={(summary["leak_check"] or {}).get("status")}; '
                      f'worktree={summary["worktree_state"]}'])
    alerts.job_alert(policy, summary, notifier=notifier)
    return summary


RESEARCH_REFUSAL = 'research class is never routed to a worker; the orchestrator handles research directly'


def bot_dispatch(repo, task_file, task_class='feature', size=None, base=None, branch=None,
                 test_cmd=None, worker=None, keep=None, policy=None, run_job_fn=None, **kwargs):
    """Map task_class + size to class_order, call run-job, and return one compact JSON summary
    with action, worker, job_id and reason. Refuses research tasks before touching the repo."""
    policy = policy or config.load_policy()
    normalized_class = (task_class or 'feature').strip().lower()
    resolved_size = size or ('draft' if normalized_class == 'draft' else 'full')
    if normalized_class == 'research':
        return {'ok': False, 'action': 'refused', 'worker': None, 'job_id': None,
                'reason': RESEARCH_REFUSAL, 'refused': RESEARCH_REFUSAL,
                'task_class': normalized_class, 'size': resolved_size}
    if normalized_class not in config.TASK_CLASSES:
        reason = f'unknown task class {task_class!r}: use one of {", ".join(config.TASK_CLASSES)}'
        return {'ok': False, 'action': 'refused', 'worker': None, 'job_id': None,
                'reason': reason, 'refused': reason, 'task_class': task_class, 'size': resolved_size}
    if resolved_size not in ('draft', 'full'):
        reason = f'size must be draft or full (got {resolved_size!r})'
        return {'ok': False, 'action': 'refused', 'worker': None, 'job_id': None,
                'reason': reason, 'refused': reason, 'task_class': normalized_class, 'size': resolved_size}
    order = config.worker_order(policy, normalized_class)
    runner = run_job_fn or run_job
    try:
        job = runner(repo, task_file, base=base, branch=branch, test_cmd=test_cmd,
                     size=resolved_size, worker=worker, keep=keep, policy=policy,
                     task_class=normalized_class, **kwargs)
    except (Refused, config.ConfigError) as refusal:
        reason = str(refusal)
        return {'ok': False, 'action': 'refused', 'worker': None, 'job_id': None,
                'reason': reason, 'refused': reason, 'task_class': normalized_class,
                'size': resolved_size, 'class_order': order}
    status = job.get('status')
    chosen = (job.get('worker')
              or (job['workers'][-1] if job.get('workers') else None)
              or (config.ORCHESTRATOR if status == 'handoff_to_orchestrator' else None))
    if status == 'handoff_to_orchestrator':
        action = 'handoff_to_orchestrator'
    elif status == 'refused':
        action = 'refused'
    elif job.get('ok'):
        action = 'dispatched'
    else:
        action = 'failed'
    reason = (job.get('reason')
              or (job['warnings'][-1] if job.get('warnings') else None)
              or status
              or ('committed' if job.get('ok') else 'failed'))
    return {'ok': bool(job.get('ok')), 'action': action, 'worker': chosen,
            'job_id': job.get('job_id'), 'reason': reason, 'status': status,
            'task_class': normalized_class, 'size': resolved_size, 'class_order': order,
            'branch': job.get('branch'), 'commit': job.get('commit')}


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
              stamp, out_dir, run_task, select, test_runner, allow_no_tests=False,
              meter=handoff.meter_percent, reviewer=None, scope=None):
    # 'no_tests' passes only on the repo's explicit opt-in; anything else ('skipped', 'not_run') never does.
    passing = ('passed', 'no_tests') if allow_no_tests else ('passed',)
    extra = handoff.WORKER_INSTRUCTIONS  # every worker writes a handoff note at checkpoints and on stop
    test_artifacts, worker_paths = set(), set()

    def save_note(worker, reason, worker_note=None, tests=None, test_log=None):
        """Write a handoff note (the worker's if valid, else a router one) into the job's handoffs dir."""
        note, path = handoff.checkpoint(
            policy, summary['job_id'], worktree, summary['base_sha'], worker, reason,
            changed_paths(worktree) - test_artifacts - {handoff.NOTE_FILE}, worker_note=worker_note,
            goal=task_text[:1500], test_status=(tests or {}).get('status'), test_log=test_log,
            meter=meter(worker) if worker else None, branch=summary['branch'])
        summary['handoffs'].append({'path': str(path), 'worker': worker, 'source': note.get('source'),
                                    'switch_reason': reason})
        return note

    def hop(record, next_worker):
        """Called by run_task before it fails over to `next_worker` in this same worktree and branch."""
        note = save_note(record.get('worker'), record.get('reason') or td.hop_reason(record),
                         handoff.collect_worker_note(worktree))
        return handoff.render_for_prompt(note)

    required, pinned, evidence = False, None, None
    retries = policy['run_job'].get('max_retries', 1)
    attempt_number = 0
    used, failed = set(), set()
    while attempt_number <= (1 if required else retries):
        cycle = attempt_number + 1
        kwargs = {'size': size, 'worker': pinned or worker, 'allow_dirty': attempt_number > 0,
                  'policy': policy, 'extra_prompt': extra, 'stamp': f'{stamp}-a{cycle}',
                  'stackwich_cycle': cycle, 'handoff': hop}
        if pinned == 'claude' and worker is None:
            kwargs['_claude_pinned'] = True
        if not (pinned or worker) and used:
            # A failed worker is never retried: the job moves to the next eligible one (max MAX_WORKERS).
            kwargs.update(exclude=sorted(failed), tried=sorted(used))
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
            # Earlier hops used up every eligible worker: report it as exhausted, not as a refusal.
            summary['attempts'][-1]['refused'] = 'workers_exhausted: ' + str(refusal)
            summary.update(status='failed', failover='exhausted')
            summary['warnings'].append('failover exhausted: no untried eligible worker left ('
                                       + ', '.join(sorted(used)) + ' tried)')
            return
        if result.get('status') == 'handoff_to_orchestrator':
            # Every worker is out: the job goes back to the orchestrator, not refused.
            summary.update(status='handoff_to_orchestrator', task_file=result.get('task_file'),
                           reason=result.get('reason'))
            summary['attempts'].append({'attempt': cycle, 'handoff_to_orchestrator': result.get('task_file')})
            return
        runs = result.get('attempts', [])
        used |= {r['worker'] for r in runs if r.get('worker')}
        failed |= {r['worker'] for r in runs if r.get('worker') and not r.get('ok')}
        claude_quota = any(r.get('worker') == 'claude' and r.get('quota_error') for r in runs)
        claude_refusal = any(r.get('worker') == 'claude' and r.get('success_check') == 'refusal'
                             for r in runs)
        invoked = [r.get('worker') for r in runs
                   if (r.get('stackwich') or {}).get('status') == 'required' and
                   not (r.get('worker') == 'claude' and r.get('quota_error'))]
        selected = result.get('worker')
        # Collect the worker's note now so it never reaches the diff, tests or commit.
        worker_note = handoff.collect_worker_note(worktree)
        percent = meter(selected) if selected else None
        if handoff.threshold_crossed(policy, percent):
            # Headroom is low: secure a note before the worker's tokens run out.
            summary['warnings'].append(f'{selected} meter at {percent:g}% '
                                       f'(>= {handoff.threshold(policy)}%): handoff note written')
            save_note(selected, f'meter_threshold {percent:g}%', worker_note)
        elif worker_note is not None:
            save_note(selected, 'checkpoint', worker_note)
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
        if scope:
            outside = out_of_scope(worker_paths, scope)
            diff_scope = {'status': 'fail' if outside else 'ok', 'allowed': list(scope),
                          'changed': sorted(worker_paths), 'out_of_scope': outside}
            summary['diff_scope'] = diff_scope
            if outside:
                if selected and selected not in summary['workers']:
                    summary['workers'].append(selected)
                summary.update(status='diff_out_of_scope', reason='diff_out_of_scope')
                summary['warnings'].append('diff_out_of_scope: ' + ', '.join(outside))
                summary['attempts'].append({'attempt': cycle, 'worker': selected, 'ok': False,
                                            'changed': changed, 'runs': result.get('attempts'),
                                            'reason': 'diff_out_of_scope', 'diff_scope': diff_scope,
                                            'leak_check': (result.get('leak_check') or {}).get('status'),
                                            'warnings': result.get('warnings')})
                return
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
            tests['source'] = summary.get('test_command_source')
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
        if not result.get('ok'):
            cause = td.failure_fields(runs)
            for key in ('failure_reason', 'stderr_tail'):
                if cause.get(key):
                    summary['attempts'][-1][key] = cause[key]
        if selected and selected not in summary['workers']:
            summary['workers'].append(selected)
        if result['ok'] and changed and tests['status'] in passing and (
                not required or evidence['status'] == 'verified'):
            break
        if claude_refusal or (claude_quota and not result.get('ok')):
            summary['status'] = 'blocked_by_stackwich' if required else 'failed'
            return
        if not (pinned or worker or result.get('ok')) and len(used) >= td.MAX_WORKERS:
            summary['warnings'].append(f'failover stopped: {td.MAX_WORKERS} workers already tried')
            summary['status'] = 'failed'
            return
        last_run = runs[-1] if runs else {}
        reason = (last_run.get('reason') or td.hop_reason(last_run) if last_run and not last_run.get('ok')
                  else f'tests {tests["status"]}' if tests['status'] not in passing else 'retry')
        log = out_dir / f'{stamp}-a{cycle}-tests.log'
        note = save_note(selected, reason, worker_note, tests, log if log.exists() else None)
        extra = '\n\n'.join(filter(None, [handoff.WORKER_INSTRUCTIONS, handoff.render_for_prompt(note),
                                          _retry_note(result, tests)]))
        if required:
            extra += '\nStackwich errors: ' + '; '.join(evidence['errors'])
        attempt_number += 1
    else:
        summary['status'] = 'blocked_by_stackwich' if required else 'failed'
        return
    last = summary['attempts'][-1]
    if not (last.get('ok') and last.get('changed') and last.get('tests') in passing):
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
    producer = last.get('worker')
    summary['review_required'] = any(review_required(w) for w in [producer, *summary['workers']] if w)
    if summary['review_required']:
        summary['review'] = _stronger_review(summary, worktree, worker_paths, reviewer)
        if summary['review'].get('verdict') == 'reject':
            summary['ok'] = False
            summary['status'] = 'review_rejected'


def _finish(policy, summary, repo, worktree, branch, base_sha, keep):
    if summary['status'] is None:
        summary['status'] = 'failed'
    success = summary['ok']
    leak = summary['blocked_by_leak_check']
    blocked = summary['status'] in ('blocked_by_stackwich', 'no_tests', 'review_rejected')
    prune = keep is False or (keep is None and not success and not leak and not blocked)
    if summary['status'] == 'blocked_by_stackwich' and keep is None:
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
            # --force only discards the job's own recorded test artifacts and untracked interpreter caches
            # (changed_paths hides those, so git still sees them as dirty); both were checked above.
            force = bool(pending) or _has_changes(worktree)
            _git(repo, 'worktree', 'remove', *(['--force'] if force else []), str(worktree), check=True)
            base_sha = job.get('base_sha')
            head = _git(repo, 'rev-parse', '-q', '--verify', f'refs/heads/{branch}').stdout.strip() if branch else ''
            if branch.startswith(policy['run_job'].get('branch_prefix', 'router/')) and base_sha and head == base_sha:
                _git(repo, 'branch', '-D', branch)
                entry['branch_action'] = 'deleted (no commits)'
        report['removed'].append(entry)
    return report
