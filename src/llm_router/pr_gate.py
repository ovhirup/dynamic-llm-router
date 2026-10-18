"""Draft-PR gate for run-job branches: review evidence, SHA-bound approvals, push, CI watch, merge.

A human sees the evidence and approves before EVERY push and EVERY merge. Nothing here pushes or
merges without a matching, unexpired approval for the exact head SHA and action; any new commit
invalidates it. Never force-pushes, never pushes the base branch, never uses gh --admin or --auto,
never deletes branches, never closes/reopens PRs (that is only ever suggested).
"""
from datetime import datetime, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

from . import alerts, config, security
from . import task_dispatch as td
ACTIONS = ('push', 'merge')
MERGE_METHODS = ('merge', 'squash', 'rebase')
FORBIDDEN_ARGS = {'-f', '--force', '--force-with-lease', '--force-if-includes', '--mirror', '--delete', '-d',
                  '--prune', '--admin', '--auto', '--delete-branch', '--no-verify', '--all', '--tags'}
GUI_COMMAND = re.compile(r'(?i)(?:^|[\s;&|(`])(?:open|osascript|screencapture|qlmanage)(?:\s|$)|\.app\b|\bSimulator\b')
QUIRK_ACTION = 'missing checks: close and reopen the PR'
NO_CHECKS_YET = 'no checks reported yet: wait, then run ci again'
TERMINAL_CI = ('passed', 'failed', 'error', 'closed')


class Refused(Exception):
    """The gate said no; nothing was pushed or merged."""


# ---- plumbing ------------------------------------------------------------------------

def _cfg(policy):
    return policy.get('pr_gate', {})


def _path(policy, key, default):
    return config.policy_path(policy, 'pr_gate', key, default)


_ZONE = [None]


def now_local(now=None, policy=None):
    zone = config.tz(policy) if policy is not None else (_ZONE[0] or config.tz(None))
    return (now or datetime.now(zone)).astimezone(zone)


def _load_policy(policy):
    policy = policy if policy is not None else config.load_policy()
    _ZONE[0] = config.tz(policy)
    return policy


def check_argv(argv):
    """Hard stop for force pushes, deletes, admin overrides and +refspecs in any git/gh call."""
    for arg in argv[1:]:
        if arg in FORBIDDEN_ARGS or arg.startswith('--force') or arg.startswith('+') or ':+' in arg:
            raise Refused(f'forbidden argument {arg!r} in a {Path(argv[0]).name} call')
    return argv


def _run(argv, cwd, timeout=120):
    check_argv(argv)
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GH_PROMPT_DISABLED='1', GH_NO_UPDATE_NOTIFIER='1',
               NO_COLOR='1', GIT_EDITOR='true')
    return subprocess.run(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          errors='replace', timeout=timeout, env=env)


def _git(cwd, *args, timeout=120):
    return _run(['git', '-C', str(cwd), *args], cwd, timeout)


def _gh(policy, settings, cwd, *args, timeout=120):
    repo_flag = ['-R', settings['github_repo']] if settings.get('github_repo') else []
    return _run([_cfg(policy).get('gh_bin', 'gh'), *args, *repo_flag], cwd, timeout)


def _err(done):
    return security.redact((done.stderr or done.stdout or '').strip())[:300]


def _json(done, default=None):
    try:
        return json.loads(done.stdout)
    except (json.JSONDecodeError, TypeError):
        return default


def _terms(policy):
    return td.private_terms(policy)


def _allow(policy):
    return td.leak_options(policy)['allow']


def load_job(policy, job_id):
    from . import job_dispatch
    if not re.fullmatch(r'[A-Za-z0-9._-]+', job_id or ''):
        raise Refused('invalid job id')
    path = job_dispatch._path(policy, 'jobs_dir', 'logs/jobs') / f'{job_id}.json'
    try:
        job = json.loads(path.read_text())
    except FileNotFoundError:
        raise Refused(f'no run-job record for {job_id}')
    if job.get('status') != 'committed' or not job.get('commit'):
        raise Refused(f'job {job_id} has no committed result (status {job.get("status")})')
    return job


def settings_for(policy, job):
    from . import job_dispatch
    return job_dispatch.repo_settings(policy, Path(job['repo']))


def head_sha(job):
    sha = _git(job['repo'], 'rev-parse', '-q', '--verify', f'refs/heads/{job["branch"]}^{{commit}}').stdout.strip()
    if not sha:
        raise Refused(f'branch {job["branch"]} no longer exists')
    return sha


def guard_branch(policy, job, settings):
    branch, prefix = job['branch'], policy.get('run_job', {}).get('branch_prefix', 'router/')
    base = settings.get('base') or 'main'
    if not branch.startswith(prefix) or branch in {base, 'main', 'master', 'HEAD'}:
        raise Refused(f'only {prefix}* job branches are pushed or merged, never {branch!r}')
    return base


def _gate_path(policy, job_id):
    return _path(policy, 'gate_dir', 'state/gate') / f'{job_id}.json'


def load_gate(policy, job_id):
    try:
        return json.loads(_gate_path(policy, job_id).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {'job_id': job_id}


def save_gate(policy, job_id, gate):
    path = _gate_path(policy, job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(security.redact_obj(gate), indent=2))
    os.chmod(path, 0o600)


def _review_dir(policy, job_id):
    return _path(policy, 'reviews_dir', 'logs/reviews') / job_id


# ---- leak checks over a commit range -------------------------------------------------

def _added_from_diff(text):
    added, current, number = {}, None, 0
    for line in text.splitlines():
        if line.startswith('+++ '):
            current = None if line[4:] == '/dev/null' else line[4:].removeprefix('b/')
        elif line.startswith('@@'):
            match = re.search(r'\+(\d+)', line)
            number = int(match.group(1)) if match else 0
        elif current and line.startswith('+'):
            added.setdefault(current, []).append((number, line[1:]))
            number += 1
    return added


def range_leak_check(policy, repo, base, head, public=False, branch=None, max_bytes=1_000_000):
    """Secrets + private terms in added lines and commit messages; for public repos also the full
    content of every changed file at head and the branch name. Missing terms on a public repo = error."""
    terms, allow = _terms(policy), _allow(policy)
    findings = []
    diff = _git(repo, 'diff', '--no-color', '--no-ext-diff', '-U0', base, head)
    log = _git(repo, 'log', '--format=%B', f'{base}..{head}')
    if diff.returncode != 0 or log.returncode != 0:
        return {'status': 'error', 'error': 'git diff/log failed', 'findings': []}
    for name, lines in _added_from_diff(diff.stdout).items():
        for number, text in lines:
            findings += [{'file': f'{name}:{number}', 'kind': kind, 'match': masked}
                         for _, kind, masked in security.scan_text(text, terms, allow)]
    findings += [{'file': f'commit-message:{n}', 'kind': kind, 'match': masked}
                 for n, kind, masked in security.scan_text(log.stdout, terms, allow)]
    result = {'scope': f'{base[:10]}..{head[:10]}', 'public_grep': bool(public)}
    if public:
        if terms is None:
            return {**result, 'status': 'error', 'error': 'public repo needs the private terms file', 'findings': findings}
        names = _git(repo, 'diff', '--name-only', '-z', '--diff-filter=d', base, head).stdout.split('\0')
        for name in filter(None, names):
            blob = subprocess.run(['git', '-C', str(repo), 'show', f'{head}:{name}'], stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=60).stdout
            if len(blob) > max_bytes or b'\0' in blob[:8192]:
                continue
            findings += [{'file': f'{name}:{n} (full file)', 'kind': kind, 'match': masked}
                         for n, kind, masked in security.scan_text(blob.decode('utf-8', 'replace'), terms, allow)]
        if branch:
            findings += [{'file': 'branch-name', 'kind': kind, 'match': masked}
                         for _, kind, masked in security.scan_text(branch, terms)]
    unique = {json.dumps(f, sort_keys=True): f for f in findings}
    result.update({'status': 'blocked' if unique else 'clean', 'findings': list(unique.values())})
    if terms is None:
        result['note'] = 'private terms file missing; secret scan only'
    return result


# ---- (1) review ----------------------------------------------------------------------

def run_screenshots(policy, job, settings, dest, runner=None):
    """Optional per-repo screenshot_cmd: headless only (GUI launchers refused), SCREENSHOT_DIR=dest."""
    command = (settings.get('screenshot_cmd') or '').strip()
    if not command or command.upper().startswith('TODO'):
        return {'status': 'not_configured'}
    if GUI_COMMAND.search(command):
        return {'status': 'refused', 'why': 'screenshot_cmd looks like it launches a GUI app; headless only'}
    worktree = Path(job.get('worktree') or '/nonexistent')
    if not worktree.is_dir():
        return {'status': 'skipped', 'why': 'job worktree was pruned'}
    runner = runner or td.run_bounded
    dest.mkdir(parents=True, exist_ok=True)
    env, _ = td.worker_environment(policy)
    env.update({'SCREENSHOT_DIR': str(dest), 'ROUTER_HEADLESS': '1', 'CI': '1'})
    result = runner(['/bin/sh', '-c', command], worktree, settings.get('screenshot_timeout_seconds', 900),
                    policy.get('run_task', {}).get('kill_grace_seconds', 10), env=env)
    (dest.parent / 'screenshots.log').write_text(security.redact(result.get('stdout', '') + result.get('stderr', '')))
    files = sorted(p.name for p in dest.iterdir() if p.is_file())
    ok = result.get('returncode') == 0 and not result.get('timed_out')
    return {'status': 'ok' if ok else 'failed', 'returncode': result.get('returncode'), 'files': files}


def _ledger_rows(policy, job_id):
    path = td._router_path(policy, 'ledger', 'logs/ledger.csv')
    try:
        return [security.redact(row) for row in path.read_text().splitlines() if job_id in row]
    except FileNotFoundError:
        return []


def review(job_id, policy=None, now=None, screenshot_runner=None):
    policy = _load_policy(policy)
    job = load_job(policy, job_id)
    settings = settings_for(policy, job)
    repo, base, head = job['repo'], job['base_sha'], head_sha(job)
    out = _review_dir(policy, job_id)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    stat = _git(repo, 'diff', '--stat', base, head).stdout
    (out / 'diffstat.txt').write_text(stat)
    (out / 'diff.patch').write_text(security.redact(_git(repo, 'diff', '--no-color', '--no-ext-diff', base, head).stdout))
    parts = []
    for number, test in enumerate(job.get('tests') or [], 1):
        log = Path(test.get('log') or '/nonexistent')
        body = security.redact(log.read_text(errors='replace')) if log.is_file() else '(no log recorded)'
        parts.append(f'### attempt {number}: {test.get("status")} ({test.get("command")})\n{body}\n')
    (out / 'tests.txt').write_text('\n'.join(parts) or '(no tests recorded)\n')
    leak = range_leak_check(policy, repo, base, head, public=settings.get('visibility') == 'public',
                            branch=job['branch'])
    (out / 'leak_check.json').write_text(json.dumps(leak, indent=2))
    worker = {key: job.get(key) for key in ('job_id', 'task', 'workers', 'attempts', 'tests_status', 'branch',
                                            'base_sha', 'commit', 'started_at', 'finished_at', 'warnings')}
    worker['ledger_rows'] = _ledger_rows(policy, job_id)
    (out / 'worker.json').write_text(json.dumps(security.redact_obj(worker), indent=2))
    shots = run_screenshots(policy, job, settings, out / 'screenshots', screenshot_runner)
    moved = head != job['commit']
    summary = {'job_id': job_id, 'repo': Path(repo).name, 'branch': job['branch'], 'head': head,
               'base_sha': base, 'reviewed_at': now_local(now).isoformat(timespec='seconds'),
               'diff_stat': stat.strip().splitlines()[-1].strip() if stat.strip() else 'no changes',
               'tests': job.get('tests_status'), 'leak_check': leak['status'],
               'leak_findings': len(leak['findings']), 'workers': job.get('workers'), 'screenshots': shots,
               'visibility': settings.get('visibility', 'unknown'), 'bundle': str(out),
               'warnings': (['branch head moved after the job: tests ran on ' + job['commit'][:10]] if moved else [])}
    gate = load_gate(policy, job_id)
    summary['approvals'] = {action: approval_status(policy, job_id, action, head, now)['why'] for action in ACTIONS}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2))
    (out / 'SUMMARY.md').write_text(_summary_md(summary, stat))
    gate['review'] = {'sha': head, 'at': summary['reviewed_at'], 'leak_check': leak['status'], 'dir': str(out)}
    save_gate(policy, job_id, gate)
    return summary


def _summary_md(summary, stat):
    shots = summary['screenshots']
    lines = [f'# Review {summary["job_id"]}', '',
             f'- Branch: `{summary["branch"]}` at `{summary["head"]}`',
             f'- Tests: {summary["tests"]}; leak check: {summary["leak_check"]} ({summary["leak_findings"]} findings)',
             f'- Workers: {", ".join(summary["workers"] or []) or "none"}; visibility: {summary["visibility"]}',
             f'- Screenshots: {shots["status"]}' + (f' ({len(shots.get("files", []))} files)' if shots.get('files') else ''),
             *[f'- WARNING: {w}' for w in summary['warnings']],
             '', '## Diff stat', '```', stat.strip(), '```', '',
             f'Approve with: `llm-router approve {summary["job_id"]} --action push --sha {summary["head"][:12]}`']
    return '\n'.join(lines) + '\n'


# ---- (2) approvals -------------------------------------------------------------------

def _approvals_file(policy):
    return _path(policy, 'approvals_file', 'state/approvals.jsonl')


def approval_records(policy):
    try:
        lines = _approvals_file(policy).read_text().splitlines()
    except FileNotFoundError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _append_approval(policy, record):
    path = _approvals_file(policy)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(record, sort_keys=True) + '\n')
    os.chmod(path, 0o600)
    return record


def approval_status(policy, job_id, action, sha, now=None):
    """Latest record for (job, action, exact sha) decides: approval (unexpired) / denial / used."""
    relevant = [r for r in approval_records(policy) if r.get('job') == job_id and r.get('action') == action]
    latest = None
    for record in relevant:
        if record.get('sha') == sha:
            latest = record
    if latest is None:
        older = any(r.get('type') == 'approval' for r in relevant)
        return {'ok': False, 'why': (f'{action} approval was for an older commit; {sha[:10]} needs a new approval'
                                     if older else f'no {action} approval for {sha[:10]}')}
    if latest['type'] == 'denial':
        return {'ok': False, 'why': f'{action} denied for {sha[:10]}'}
    if latest['type'] == 'used':
        return {'ok': False, 'why': f'{action} approval for {sha[:10]} was already used'}
    expires = latest['expires_at']
    if datetime.fromisoformat(expires) <= now_local(now):
        return {'ok': False, 'why': f'{action} approval for {sha[:10]} expired at {expires}'}
    return {'ok': True, 'why': f'approved until {expires}', 'approval': latest}


def _match_sha(head, sha):
    if sha and (len(sha) < 7 or not head.startswith(sha.lower())):
        raise Refused(f'--sha {sha} is not the current branch head {head[:12]}; review again')


def approve(job_id, action, sha=None, hours=None, note=None, policy=None, now=None):
    policy = _load_policy(policy)
    if action not in ACTIONS:
        raise Refused(f'action must be one of {ACTIONS}')
    job = load_job(policy, job_id)
    guard_branch(policy, job, settings_for(policy, job))
    head = head_sha(job)
    _match_sha(head, sha)
    gate = load_gate(policy, job_id)
    if (gate.get('review') or {}).get('sha') != head:
        raise Refused('no review bundle for the current head; run review first so the evidence matches')
    if (gate.get('review') or {}).get('leak_check') != 'clean':
        raise Refused('the review leak check was not clean; nothing to approve')
    if action == 'merge' and (gate.get('pr') or {}).get('pushed_sha') != head:
        raise Refused('push the branch and open the draft PR before approving a merge')
    stamp = now_local(now)
    ttl = float(hours if hours is not None else _cfg(policy).get('approval_ttl_hours', 24))
    record = {'type': 'approval', 'job': job_id, 'action': action, 'sha': head, 'branch': job['branch'],
              'repo': Path(job['repo']).name, 'at': stamp.isoformat(timespec='seconds'),
              'expires_at': (stamp + timedelta(hours=ttl)).isoformat(timespec='seconds'),
              'note': security.redact(note or ''), 'review_dir': gate['review'].get('dir')}
    record['id'] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:12]
    return _append_approval(policy, record)


def deny(job_id, action, note=None, policy=None, now=None):
    policy = _load_policy(policy)
    if action not in ACTIONS:
        raise Refused(f'action must be one of {ACTIONS}')
    job = load_job(policy, job_id)
    head = head_sha(job)
    record = {'type': 'denial', 'job': job_id, 'action': action, 'sha': head, 'branch': job['branch'],
              'at': now_local(now).isoformat(timespec='seconds'), 'note': security.redact(note or '')}
    return _append_approval(policy, record)


def _mark_used(policy, approval, now=None):
    _append_approval(policy, {'type': 'used', 'job': approval['job'], 'action': approval['action'],
                              'sha': approval['sha'], 'approval_id': approval.get('id'),
                              'at': now_local(now).isoformat(timespec='seconds')})


def _require(policy, job_id, action, sha, now):
    status = approval_status(policy, job_id, action, sha, now)
    if not status['ok']:
        raise Refused(f'{action} refused: {status["why"]}')
    return status['approval']


# ---- (3) push + draft PR -------------------------------------------------------------

def pr_body(job, head, leak, policy):
    review = _review_dir(policy, job['job_id'])
    stat = (review / 'diffstat.txt').read_text().strip() if (review / 'diffstat.txt').exists() else ''
    tests = next((t for t in reversed(job.get('tests') or []) if t.get('status')), {})
    return '\n'.join([
        'Draft PR opened by the Dynamic LLM Router after an explicit push approval for this exact commit.', '',
        f'- Head: `{head}`',
        f'- Tests: {job.get("tests_status")}' + (f' (`{tests.get("command")}`)' if tests.get('command') else ''),
        f'- Leak check: {leak["status"]} (secrets + private terms'
        + (', full-file public-repo grep)' if leak.get('public_grep') else ')'),
        f'- Workers: {", ".join(job.get("workers") or []) or "none"}', '',
        '### Diff stat', '```', stat, '```', '',
        'Merging needs a separate merge approval for the head SHA and green required checks.'])


def _commit_title(job, head):
    subject = _git(job['repo'], 'log', '-1', '--format=%s', head).stdout.strip() or f'router job {job["job_id"]}'
    return security.redact(subject)[:72]


def find_pr(policy, settings, job):
    found = _gh(policy, settings, job['repo'], 'pr', 'list', '--head', job['branch'], '--state', 'open',
                '--json', 'number,url,isDraft,headRefOid')
    items = _json(found, []) if found.returncode == 0 else []
    return items[0] if items else None


def push(job_id, policy=None, now=None):
    policy = _load_policy(policy)
    job = load_job(policy, job_id)
    settings = settings_for(policy, job)
    base = guard_branch(policy, job, settings)
    remote = settings.get('remote')
    if not remote:
        raise Refused('repo has no remote configured in repos.json')
    head = head_sha(job)
    approval = _require(policy, job_id, 'push', head, now)
    public = settings.get('visibility') == 'public'
    leak = range_leak_check(policy, job['repo'], job['base_sha'], head, public=public, branch=job['branch'])
    if leak['status'] != 'clean':
        alerts.raise_alert(policy, f'push:{job_id}:{head}:leak', 'push',
                           f'push {job_id} blocked by leak check ({leak["status"]}, {len(leak["findings"])} findings)',
                           level='stop', now=now)
        raise Refused(f'push blocked by leak check: {leak["status"]} ({len(leak["findings"])} findings)')
    title, body = _commit_title(job, head), pr_body(job, head, leak, policy)
    if security.scan_text(title + '\n' + body, _terms(policy), _allow(policy)):
        raise Refused('PR title/body would contain a secret or private term; nothing pushed')
    branch = job['branch']
    remote_ref = _git(job['repo'], 'ls-remote', remote, f'refs/heads/{branch}')
    if remote_ref.returncode != 0:
        raise Refused('cannot read the remote branch: ' + _err(remote_ref))
    remote_sha = remote_ref.stdout.split()[0] if remote_ref.stdout.strip() else None
    if remote_sha and remote_sha != head and _git(job['repo'], 'merge-base', '--is-ancestor',
                                                  remote_sha, head).returncode != 0:
        raise Refused('remote branch has diverged; that would need a force push, which the router never does')
    result = {'job_id': job_id, 'branch': branch, 'head': head, 'remote': remote, 'leak_check': 'clean',
              'pushed': False, 'pr': None, 'warnings': []}
    if remote_sha != head:
        pushed = _git(job['repo'], 'push', '--porcelain', remote, f'refs/heads/{branch}:refs/heads/{branch}',
                      timeout=300)
        if pushed.returncode != 0:
            raise Refused('git push failed: ' + _err(pushed))
    result['pushed'] = True
    _mark_used(policy, approval, now)
    gate = load_gate(policy, job_id)
    pr = find_pr(policy, settings, job)
    if pr is None:
        body_file = _review_dir(policy, job_id) / 'pr-body.md'
        body_file.parent.mkdir(parents=True, exist_ok=True)
        body_file.write_text(body)
        created = _gh(policy, settings, job['repo'], 'pr', 'create', '--draft', '--base', base, '--head', branch,
                      '--title', title, '--body-file', str(body_file))
        url = (created.stdout.strip().splitlines() or [''])[-1]
        match = re.search(r'/pull/(\d+)', url)
        if created.returncode != 0 or not match:
            result['warnings'].append('pushed, but draft PR creation failed: ' + _err(created))
        else:
            pr = {'number': int(match.group(1)), 'url': url, 'isDraft': True}
    if pr:
        result['pr'] = {'number': pr['number'], 'url': pr.get('url'), 'draft': pr.get('isDraft', True)}
        gate['pr'] = {**result['pr'], 'pushed_sha': head, 'base': base,
                      'at': now_local(now).isoformat(timespec='seconds')}
    gate['pushed_sha'] = head
    save_gate(policy, job_id, gate)
    alerts.raise_alert(policy, f'push:{job_id}:{head}:pushed', 'push',
                       f'pushed {branch} at {head[:10]}' + (f'; draft PR #{pr["number"]}' if pr else '; no PR'),
                       level='info', now=now)
    return result


# ---- (4) CI ------------------------------------------------------------------------------

def _bucket(check):
    bucket = (check.get('bucket') or '').lower()
    if bucket in ('pass', 'fail', 'pending', 'skipping', 'cancel'):
        return {'cancel': 'fail', 'skipping': 'skipped'}.get(bucket, bucket)
    state = (check.get('state') or '').upper()
    if state in ('SUCCESS', 'NEUTRAL'):
        return 'pass'
    if state in ('FAILURE', 'ERROR', 'CANCELLED', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE'):
        return 'fail'
    if state == 'SKIPPED':
        return 'skipped'
    return 'pending'


def evaluate_checks(checks, required):
    """Overall status from gh pr checks JSON: passed / failed / pending / missing (+ suggested action)."""
    results = {}
    for check in checks or []:
        name, bucket = check.get('name') or '?', _bucket(check)
        if results.get(name) != 'fail':
            results[name] = bucket
    required = list(required or [])
    missing = [name for name in required if name not in results]
    watched = required or list(results)
    failing = sorted(name for name, bucket in results.items() if bucket == 'fail')
    pending = [name for name in watched if results.get(name) == 'pending']
    not_green = [name for name in required if results.get(name) not in (None, 'pass', 'pending', 'fail')]
    if failing:
        status = 'failed'
    elif missing or not results:
        status = 'missing'
    elif pending:
        status = 'pending'
    elif not_green:
        status = 'failed'
    else:
        status = 'passed'
    out = {'status': status, 'checks': results, 'missing': missing, 'failing': failing + not_green,
           'pending': pending}
    if status == 'missing':
        only_cloudflare = bool(results) and all(name.lower().startswith('cloudflare') for name in results)
        out['suggested_action'] = QUIRK_ACTION if only_cloudflare else NO_CHECKS_YET if not results else None
    return out


def poll_ci(policy, job, settings, pr_number):
    view = _gh(policy, settings, job['repo'], 'pr', 'view', str(pr_number), '--json',
               'headRefOid,state,isDraft,url,baseRefName')
    info = _json(view, None) if view.returncode == 0 else None
    if not isinstance(info, dict):
        return {'status': 'error', 'error': 'gh pr view failed: ' + _err(view)}
    checks = _gh(policy, settings, job['repo'], 'pr', 'checks', str(pr_number), '--json',
                 'name,state,bucket,workflow,link,completedAt')
    data = _json(checks, None)
    if data is None:
        if 'no checks reported' in (checks.stderr + checks.stdout).lower():
            data = []
        else:
            return {'status': 'error', 'error': 'gh pr checks failed: ' + _err(checks), 'sha': info.get('headRefOid')}
    result = evaluate_checks(data, settings.get('required_checks'))
    result.update({'sha': info.get('headRefOid'), 'pr_state': info.get('state'), 'is_draft': info.get('isDraft'),
                   'pr': pr_number, 'url': info.get('url')})
    if info.get('state') != 'OPEN':
        result['status'] = 'closed' if info.get('state') == 'CLOSED' else result['status']
    return result


def _record_ci(policy, job_id, result, now):
    result['at'] = now_local(now).isoformat(timespec='seconds')
    directory = _review_dir(policy, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'ci.jsonl').open('a') as handle:
        handle.write(json.dumps(security.redact_obj(result), sort_keys=True) + '\n')


def ci(job_id, watch=False, timeout_minutes=None, policy=None, now=None, sleep=time.sleep, clock=time.monotonic,
       pr_number=None, job=None, settings=None):
    """Poll gh pr checks (once, or with --watch until terminal/timeout, exponential backoff); alert."""
    policy = _load_policy(policy)
    job = job or load_job(policy, job_id)
    settings = settings if settings is not None else settings_for(policy, job)
    gate = load_gate(policy, job_id)
    number = pr_number or (gate.get('pr') or {}).get('number')
    if not number:
        raise Refused('no PR recorded for this job; push it first')
    cfg = _cfg(policy).get('ci', {})
    timeout = 60 * float(timeout_minutes if timeout_minutes is not None else cfg.get('timeout_minutes', 60))
    delay, cap = cfg.get('initial_poll_seconds', 30), cfg.get('max_poll_seconds', 300)
    grace = 60 * float(cfg.get('missing_grace_minutes', 10))
    start, missing_since, polls = clock(), None, 0
    while True:
        result = poll_ci(policy, job, settings, number)
        polls += 1
        _record_ci(policy, job_id, result, now)
        terminal = result['status'] in TERMINAL_CI
        if result['status'] == 'missing':
            missing_since = clock() if missing_since is None else missing_since
            terminal = clock() - missing_since >= grace
        if not watch or terminal:
            break
        if clock() - start + delay > timeout:
            result['timed_out'] = True
            break
        sleep(delay)
        delay = min(delay * 2, cap)
    result['polls'] = polls
    gate['ci'] = {key: result.get(key) for key in ('status', 'sha', 'missing', 'failing', 'pending',
                                                   'suggested_action', 'timed_out', 'at', 'checks')}
    save_gate(policy, job_id, gate)
    _ci_alert(policy, job_id, result, watch, now)
    return result


def _ci_alert(policy, job_id, result, watch, now):
    status, sha = result['status'], (result.get('sha') or 'unknown')[:10]
    if result.get('timed_out'):
        level, text = 'warn', f'CI for {job_id} still {status} after the watch timeout'
        status = 'timeout'
    elif status == 'passed':
        level, text = 'info', f'CI passed for {job_id} at {sha}: all required checks green'
    elif status == 'failed':
        level, text = 'stop', f'CI failed for {job_id} at {sha}: {", ".join(result.get("failing") or [])}'
    elif status == 'missing' and result.get('suggested_action') == QUIRK_ACTION:
        level, text = 'warn', f'CI for {job_id} at {sha}: only Cloudflare checks; suggested: {QUIRK_ACTION}'
    elif status == 'error':
        level, text = 'warn', f'CI read failed for {job_id}: {result.get("error")}'
    else:
        return None
    return alerts.raise_alert(policy, f'ci:{job_id}:{sha}:{status}', 'ci', text, level=level, now=now)


# ---- (5) merge -----------------------------------------------------------------------------

def merge(job_id, policy=None, now=None):
    policy = _load_policy(policy)
    job = load_job(policy, job_id)
    settings = settings_for(policy, job)
    guard_branch(policy, job, settings)
    method = settings.get('merge_method')
    if method not in MERGE_METHODS:
        raise Refused(f'merge_method must be one of {MERGE_METHODS} in repos.json')
    if not settings.get('required_checks'):
        raise Refused('no required_checks configured for this repo; refusing to merge blind')
    gate = load_gate(policy, job_id)
    number = (gate.get('pr') or {}).get('number')
    if not number:
        raise Refused('no PR recorded for this job; push it first')
    head = head_sha(job)
    approval = _require(policy, job_id, 'merge', head, now)
    state = poll_ci(policy, job, settings, number)
    _record_ci(policy, job_id, state, now)
    if state.get('sha') != head:
        raise Refused(f'PR head {str(state.get("sha"))[:10]} is not the approved local head {head[:10]}')
    if state.get('pr_state') != 'OPEN':
        raise Refused(f'PR is {state.get("pr_state")}, not open')
    if state['status'] != 'passed':
        raise Refused(f'required checks not green ({state["status"]}): missing={state.get("missing")} '
                      f'failing={state.get("failing")} pending={state.get("pending")}'
                      + (f'; suggested: {state["suggested_action"]}' if state.get('suggested_action') else ''))
    steps = []
    if state.get('is_draft'):
        ready = _gh(policy, settings, job['repo'], 'pr', 'ready', str(number))
        if ready.returncode != 0:
            raise Refused('gh pr ready failed: ' + _err(ready))
        steps.append('marked ready for review')
    merged = _gh(policy, settings, job['repo'], 'pr', 'merge', str(number), f'--{method}',
                 '--match-head-commit', head, timeout=300)
    if merged.returncode != 0:
        raise Refused('gh pr merge failed (branch protection is never bypassed): ' + _err(merged))
    _mark_used(policy, approval, now)
    steps.append(f'merged with --{method}')
    gate['merged'] = {'sha': head, 'method': method, 'at': now_local(now).isoformat(timespec='seconds')}
    save_gate(policy, job_id, gate)
    alerts.raise_alert(policy, f'merge:{job_id}:{head}', 'merge', f'merged PR #{number} for {job_id} at {head[:10]}',
                       level='info', now=now)
    return {'job_id': job_id, 'pr': number, 'head': head, 'method': method, 'steps': steps, 'merged': True}
