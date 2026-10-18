"""Real-task dispatch: pick a worker by policy and run it headless in a git checkout.

Never commits or pushes in the target repo. Workers come from run_task.commands (codex, grok-build,
cursor, optional claude) plus the optional `developer` HTTP draft worker. Workers listed in `never`
are refused and shadowed on PATH by a refusing shim.
"""
import csv
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import urllib.request

from . import config, security


def coding_workers(policy):
    return tuple(k for k in policy['run_task']['commands'] if not k.startswith('_'))


def forceable(policy):
    never = set(policy.get('never') or [])
    return tuple(w for w in coding_workers(policy) + ('developer',) if w not in never)


def meter_name(policy, worker):
    quota = config.quota_cfg(policy, worker) or {}
    return quota.get('meter') or ('local-draft' if worker == 'developer' else f'{worker}-unmetered')


class Refused(Exception):
    """A pre-flight refusal: nothing was run."""


def now_local(policy=None):
    return config.now_local(policy)


def _git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=30)


def check_repo(cwd, allow_dirty=False):
    """Return HEAD (or None for an unborn branch); refuse non-repos and dirty trees."""
    if not Path(cwd).is_dir():
        raise Refused('cwd is not a directory')
    inside = _git(cwd, 'rev-parse', '--is-inside-work-tree')
    if inside.returncode != 0 or inside.stdout.strip() != 'true':
        raise Refused('cwd is not a git repository')
    status = _git(cwd, 'status', '--porcelain')
    if status.returncode != 0:
        raise Refused('git status failed')
    if status.stdout.strip() and not allow_dirty:
        raise Refused('working tree has uncommitted changes (use --allow-dirty to override)')
    head = _git(cwd, 'rev-parse', '--verify', '-q', 'HEAD')
    return head.stdout.strip() or None


def diff_summary(cwd):
    stat = _git(cwd, 'diff', '--stat').stdout.strip()
    untracked = [line[3:] for line in _git(cwd, 'status', '--porcelain').stdout.splitlines()
                 if line.startswith('?? ')]
    return {'diff_stat': stat, 'untracked': untracked}


def run_bounded(argv, cwd, timeout, grace=5, popen=subprocess.Popen, input_text=None, env=None,
                stall_seconds=None):
    """Run in a new session; kill the whole group on overall timeout or on an output stall.

    input_text (the prompt, for stdin delivery) never touches argv. stall_seconds: kill when
    neither stdout nor stderr produced anything for that long (separate from timeout)."""
    started = time.monotonic()
    process = popen(argv, cwd=str(cwd), stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                    env=env)
    chunks = {'stdout': [], 'stderr': []}
    last = [time.monotonic()]

    def pump(name, stream):
        try:
            for line in iter(stream.readline, ''):
                chunks[name].append(line)
                last[0] = time.monotonic()
        except (ValueError, OSError):
            pass

    readers = [threading.Thread(target=pump, args=(name, getattr(process, name)), daemon=True)
               for name in ('stdout', 'stderr')]
    for reader in readers:
        reader.start()
    if input_text is not None:
        def feed():
            try:
                process.stdin.write(input_text)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True).start()
    timed_out = stalled = False
    try:
        while process.poll() is None:
            now = time.monotonic()
            if now - started >= timeout:
                timed_out = True
            elif stall_seconds and now - last[0] >= stall_seconds:
                stalled = True
            if timed_out or stalled:
                _kill_group(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    _kill_group(process.pid, signal.SIGKILL)
                    process.wait()
                break
            time.sleep(0.05)
    finally:
        # Workers may leave helpers in their session; the group is ours alone.
        _kill_group(process.pid, signal.SIGKILL)
        for reader in readers:
            reader.join(timeout=2)
        for stream in (process.stdout, process.stderr, process.stdin):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
    return {'returncode': process.returncode, 'timed_out': timed_out or stalled, 'stalled': stalled,
            'stdout': ''.join(chunks['stdout']), 'stderr': ''.join(chunks['stderr']),
            'elapsed_seconds': round(time.monotonic() - started, 1)}


def _kill_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def handoff_snapshot(cwd, name='HANDOFF.md'):
    """Record a protected cwd-root file (e.g. HANDOFF.md written by a CLI's session hook) before a run."""
    path = Path(cwd) / name
    if not path.exists() and not path.is_symlink():
        return {'state': 'absent', 'bytes': None, 'name': name}
    if path.is_symlink() or not path.is_file():
        return {'state': 'not_a_regular_file', 'bytes': None, 'name': name}
    tracked = _git(cwd, 'ls-files', '--error-unmatch', '--', name).returncode == 0
    return {'state': 'tracked' if tracked else 'untracked', 'bytes': path.read_bytes(), 'name': name}


def handoff_restore(cwd, snapshot):
    """Undo any change to the protected file at the cwd root; returns the action taken."""
    path = Path(cwd) / snapshot.get('name', 'HANDOFF.md')
    state = snapshot['state']
    if state == 'not_a_regular_file':
        return {'before': state, 'action': 'left_alone'}
    if state == 'absent':
        if path.is_symlink() or path.is_file():
            path.unlink()
            return {'before': state, 'action': 'deleted_new'}
        if path.exists():
            return {'before': state, 'action': 'left_alone_not_a_file'}
        return {'before': state, 'action': 'none'}
    if path.is_file() and not path.is_symlink() and path.read_bytes() == snapshot['bytes']:
        return {'before': state, 'action': 'unchanged'}
    if path.is_symlink():
        path.unlink()
    path.write_bytes(snapshot['bytes'])
    return {'before': state, 'action': 'restored'}


def build_argv(policy, worker, cwd, prompt_file=None):
    """Expand a linted template. The prompt itself is never an argv value."""
    template = policy['run_task']['commands'][worker]
    values = {'{cwd}': str(cwd)}
    if prompt_file is not None:
        values['{prompt_file}'] = str(prompt_file)
        values['{prompt_pointer}'] = (f'Your full task is in the file {prompt_file}. Read that file and follow '
                                      'it exactly; do not copy it into the repository.')
    values.update({'{' + name + '}': config.binary(policy, name) for name in config.BINARY_DEFAULTS
                   if '{' + name + '}' in template})
    argv = [values.get(arg, arg) for arg in template]
    if any(arg in ('{prompt_file}', '{prompt_pointer}') for arg in argv):
        raise Refused(f'{worker}: template needs a prompt file')
    return argv


def delivery(policy, worker):
    mode = policy['run_task'].get('prompt_delivery', {}).get(worker, 'stdin')
    if mode not in ('stdin', 'file', 'pointer'):
        raise Refused(f'{worker}: unknown prompt delivery {mode}')
    return mode


def write_prompt_file(cwd, stamp, worker, prompt):
    """0600 prompt file inside the checkout's git dir: never in the diff or untracked list."""
    git_dir = _git(cwd, 'rev-parse', '--absolute-git-dir').stdout.strip()
    directory = Path(git_dir) / 'router-prompts'
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f'{stamp}-{worker}.md'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(prompt)
    os.chmod(path, 0o600)
    return path


def developer_draft(policy, prompt, opener=None):
    """POST to an OpenAI-compatible chat endpoint; the key comes only from the env var named in config."""
    options = policy['developer']
    if not options.get('enabled') or not options.get('endpoint'):
        raise RuntimeError('developer worker is disabled (developer.enabled / developer.endpoint)')
    headers = {'Content-Type': 'application/json'}
    if options.get('api_key_env'):
        key = os.environ.get(options['api_key_env'])
        if key:
            headers['Authorization'] = 'Bearer ' + key  # stays in this process; never logged
    request = urllib.request.Request(options['endpoint'], data=json.dumps({
        'model': options['model'], 'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': options['max_tokens']}).encode(), headers=headers, method='POST')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=policy['run_task']['timeouts_seconds'].get('developer', 180)) as response:
        return json.load(response)['choices'][0]['message']['content']


def append_ledger(path, row):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', newline='') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)  # serialise concurrent jobs; released on close
        new = os.fstat(handle.fileno()).st_size == 0
        writer = csv.writer(handle)
        if new:
            writer.writerow(['date', 'runner', 'task', 'meter', 'used_percent', 'note'])
        writer.writerow(row)


def pacing(policy, meters, worker, size):
    """Return (refuse_reason or None, warning or None) for the chosen worker's meter."""
    used = meters.get(worker) if worker != 'developer' else None
    limits = policy['run_task']['pacing']
    if used is None:
        return None, None
    if size == 'full' and used >= limits['refuse_full_percent']:
        return f'{worker} meter at {used}% >= {limits["refuse_full_percent"]}%: refusing new full-size work', None
    if used >= limits['warn_percent']:
        return None, f'{worker} meter at {used}% (warn at {limits["warn_percent"]}%)'
    return None, None


def run_task(task_file, cwd, size='full', worker=None, allow_dirty=False, select=None,
             runner=run_bounded, draft=developer_draft, policy=None, output_root=None,
             extra_prompt=None, stamp=None, meter=None):
    from . import selection as selection_module
    policy = policy or config.load_policy()
    injected_select = select is not None
    select = select or (lambda chars, **kw: selection_module.select(chars, policy=policy, **kw))
    coding = coding_workers(policy)
    metered = set(config.metered_workers(policy))
    cwd = Path(cwd).expanduser().resolve()
    task_path = Path(task_file).expanduser().resolve()
    task = task_path.read_text()
    started = now_local(policy)
    summary = {'task': task_path.name, 'cwd': str(cwd), 'size': size, 'worker': None,
               'reason': None, 'started_at': started.isoformat(timespec='seconds'),
               'finished_at': None, 'ok': False, 'attempts': [], 'warnings': [],
               'diff_stat': '', 'untracked': [], 'output_path': None,
               'handoff_cleanup': 'not_applicable', 'leak_check': None,
               'blocked_by_leak_check': False, 'blocked_worker_attempts': 0}
    if worker is not None and worker not in forceable(policy):
        raise Refused('worker must be one of ' + ', '.join(forceable(policy)) + ' (workers in `never` are refused)')
    if size not in ('draft', 'full'):
        raise Refused('size must be draft or full')
    if worker == 'developer' and size != 'draft':
        raise Refused('developer only produces draft text (use --size draft)')
    head_before = check_repo(cwd, allow_dirty)
    chars = 0 if size == 'draft' else policy['developer']['draft_max_chars'] + 1
    # A forced cursor still gets its own meter read (it is otherwise consulted only as a fallback).
    if worker is not None and (meter is not None or not injected_select):
        # A forced worker reads only its own meter (fresh refresher cache, else one live read).
        selection = (meter or (lambda name: selection_module.read_meter(policy, name)))(worker)
    else:
        selection = select(chars, consult_cursor=True) if worker == 'cursor' else select(chars)
    meters = selection.get('meters', {})
    if worker is not None:
        order, summary['reason'] = [worker], 'forced'
    else:
        order, summary['reason'] = list(selection.get('eligible', [])), selection.get('reason')
    if not order:
        raise Refused('no eligible worker: ' + str(selection.get('reason')))
    unmetered = set(policy.get('unmetered_workers') or [])
    if worker in coding and worker in unmetered:
        summary['warnings'].append(f'{worker} is unmetered: run not paced')
    elif worker in coding and meters.get(worker) is None:
        if size == 'full':
            raise Refused(f'{worker} meter unknown or stale: forced full-size work must be paced (use --size draft)')
        summary['warnings'].append(f'{worker} meter unknown; draft-size run not paced')
    refuse, warning = pacing(policy, meters, order[0], size)
    if refuse:
        raise Refused(refuse)
    if warning:
        summary['warnings'].append(warning)
    out_dir = Path(output_root or config.policy_path(policy, 'run_task', 'output_dir', 'logs/task-output'))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or started.strftime('%Y%m%d-%H%M%S')
    prompt = policy['run_task']['prompt_prefix'] + '\n\n' + task
    if extra_prompt:
        prompt += '\n\n' + extra_prompt
    quota_error = re.compile(policy['run_task']['quota_error_pattern'])
    env, block_log = worker_environment(policy)
    blocks_before = security.count_lines(block_log)
    for attempt, name in enumerate(order[:2]):  # the chosen worker plus one failover
        if attempt and name in coding:
            refuse, warning = pacing(policy, meters, name, size)
            if refuse:
                summary['warnings'].append('failover skipped: ' + refuse)
                break
            if warning:
                summary['warnings'].append(warning)
        record = {'worker': name}
        if name == 'developer':
            path = out_dir / f'{stamp}-developer.md'
            try:
                path.write_text(security.redact(draft(policy, prompt)))
                record.update(ok=True)
            except Exception:
                record.update(ok=False, error='developer draft failed')
        else:
            path = out_dir / f'{stamp}-{name}.log'
            protected = (policy['run_task'].get('protect_paths') or {}).get(name) or []
            snapshots = [handoff_snapshot(cwd, item) for item in protected]
            mode = delivery(policy, name)
            prompt_file = None
            try:
                if mode != 'stdin':
                    prompt_file = write_prompt_file(cwd, stamp, name, prompt)
                result = runner(build_argv(policy, name, cwd, prompt_file), cwd,
                                policy['run_task']['timeouts_seconds'][name],
                                policy['run_task']['kill_grace_seconds'],
                                input_text=prompt if mode == 'stdin' else None, env=env,
                                stall_seconds=policy['run_task'].get('stall_seconds', {}).get(name))
            finally:
                if prompt_file is not None:
                    prompt_file.unlink(missing_ok=True)
                if snapshots:
                    actions = [handoff_restore(cwd, snap) for snap in snapshots]
                    summary['handoff_cleanup'] = (actions[0] if len(actions) == 1 else
                                                  {snap['name']: act for snap, act in zip(snapshots, actions)})
            record['prompt_delivery'] = mode
            path.write_text(security.redact('$ exit=%s timed_out=%s elapsed=%ss\n--- stdout\n%s\n--- stderr\n%s\n' % (
                result['returncode'], result['timed_out'], result['elapsed_seconds'],
                result['stdout'], result['stderr'])))
            quota = bool(quota_error.search(result['stderr'] + result['stdout'][-4000:])) and result['returncode'] != 0
            record.update(ok=result['returncode'] == 0 and not result['timed_out'],
                          timed_out=result['timed_out'], returncode=result['returncode'],
                          quota_error=quota, elapsed_seconds=result['elapsed_seconds'],
                          stalled=result.get('stalled', False))
        record['output_path'] = str(path)
        summary['attempts'].append(record)
        summary.update(worker=name, ok=record['ok'], output_path=str(path))
        if record['ok']:
            break
    head_after = _git(cwd, 'rev-parse', '--verify', '-q', 'HEAD').stdout.strip() or None
    if head_after != head_before:
        summary['warnings'].append('target HEAD changed during the run; review it (router never commits)')
    summary.update(diff_summary(cwd))
    summary['leak_check'] = security.leak_check(cwd, head_before, None, private_terms(policy),
                                                **leak_options(policy))
    if summary['leak_check']['status'] != 'clean':
        summary['blocked_by_leak_check'] = True
        summary['ok'] = False
        summary['warnings'].append('leak check %s: %d finding(s); nothing was deleted, review before sharing'
                                   % (summary['leak_check']['status'], len(summary['leak_check']['findings'])))
    summary['blocked_worker_attempts'] = security.count_lines(block_log) - blocks_before
    if summary['blocked_worker_attempts']:
        summary['warnings'].append('a worker tried to start a denylisted CLI; the shim refused it')
    summary['finished_at'] = now_local(policy).isoformat(timespec='seconds')
    for record in summary['attempts']:
        used = meters.get(record['worker'], '') if record['worker'] in metered else ''
        note = 'leak-blocked' if summary['blocked_by_leak_check'] and record['ok'] else 'ok' if record['ok'] else ('stalled' if record.get('stalled') else 'timeout' if record.get('timed_out') else
                                          'quota error' if record.get('quota_error') else 'failed')
        row = [started.strftime('%Y-%m-%d %H:%M'), record['worker'], f'run-task {task_path.name} ({size})',
               meter_name(policy, record['worker']), '' if used is None else used,
               f'{note}; cwd={cwd.name}; diff={summary["diff_stat"].splitlines()[-1].strip() if summary["diff_stat"] else "none"}; untracked={len(summary["untracked"])}']
        row = [security.redact(str(cell)) for cell in row]
        append_ledger(config.policy_path(policy, 'run_task', 'ledger', 'logs/ledger.csv'), row)
        for extra in policy['run_task'].get('extra_ledgers') or []:
            extra = config.resolve(extra)
            if extra.parent.exists():  # mirror only into a ledger dir the user created
                append_ledger(extra, row)
    return security.redact_obj(summary)


def _router_path(policy, key, default):
    return config.policy_path(policy, 'run_task', key, default)


def private_terms(policy):
    return security.load_terms(_router_path(policy, 'private_terms_file', 'private_terms.txt'))


def leak_options(policy):
    options = policy.get('leak_check') or {}
    return {'allow': security.compile_allow(options.get('allow_regexes')),
            'skip_dirs': tuple(options.get('skip_untracked_dirs') or security.DEFAULT_SKIP_DIRS),
            'max_bytes': options.get('max_file_bytes', 1_000_000)}


def worker_environment(policy):
    """(env, block_log): PATH shims for every `never` CLI, configured env prefixes stripped."""
    block_log = _router_path(policy, 'block_log', 'logs/blocked-workers.log')
    shim_dir = security.ensure_block_shims(_router_path(policy, 'shim_dir', 'state/shims'), block_log,
                                           policy.get('never') or [])
    env = security.worker_env(shim_dir, strip_prefixes=policy['run_task'].get('strip_env_prefixes',
                                                                              security.STRIP_PREFIXES))
    return env, block_log
