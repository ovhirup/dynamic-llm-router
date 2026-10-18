"""Background quota refresh, cached-or-live meter reads, alerts evaluation, status table, LaunchAgent.

Observers and loaders are injectable so tests never touch a real CLI. Real observers always run
isolated with a hard timeout (see observers.observe_isolated), so one hung CLI cannot block a refresh.
"""
from datetime import datetime, timedelta, timezone
import fcntl
from importlib import resources
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

from . import alerts, config, observers
from .observers import common


def cache_path(worker):
    return config.state_dir() / 'state' / f'{worker}_quota_observation.json'


def default_io(policy=None):
    """Real observers (isolated, hard timeout) and caches for every configured quota worker."""
    names = config.metered_workers(policy) if policy else ['codex', 'grok-build', 'cursor']
    io = {}
    for name in names:
        io[name] = {'observe': (lambda pol, _n=name: observers.observe_isolated(pol, _n)),
                    'load': (lambda _n=name: common.load_cache(cache_path(_n))),
                    'save': (lambda obs, _n=name: common.save_cache(obs, cache_path(_n)))}
    return io


def workers(policy):
    return config.metered_workers(policy)


def threshold(policy, worker):
    return (config.quota_cfg(policy, worker) or {}).get('usage_threshold_percent', 85)


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def _utcnow(now=None):
    return now or datetime.now(timezone.utc)


def evidence_of(observation):
    return observation.get('evidence') if isinstance(observation, dict) else None


def is_fresh(evidence, now=None):
    if not isinstance(evidence, dict) or not evidence.get('windows'):
        return False
    expiries = [_parse(row.get('expires_at')) for row in evidence['windows'].values()]
    return all(e is not None and e > _utcnow(now) for e in expiries)


def peak(evidence):
    """(percent, window name, reset_at) of the most-used window, or (None, None, None)."""
    if not isinstance(evidence, dict):
        return None, None, None
    best = None
    for name, row in (evidence.get('windows') or {}).items():
        value = row.get('percent_used')
        if type(value) in (int, float) and (best is None or value > best[0]):
            best = (value, name, row.get('reset_at'))
    return best or (None, None, None)


def exhausted_until(evidence, now=None):
    """Earliest reset among windows at 100% that is still in the future, else None."""
    if not isinstance(evidence, dict):
        return None
    resets = [_parse(row.get('reset_at')) for row in (evidence.get('windows') or {}).values()
              if type(row.get('percent_used')) in (int, float) and row['percent_used'] >= 100]
    resets = [r for r in resets if r is not None and r > _utcnow(now)]
    return min(resets) if resets else None


def interval(policy, worker):
    return (policy['quota_refresh'].get('interval_seconds') or {}).get(worker, 900)


def validate(policy):
    """Freshness must cover the refresh interval plus one agent period, or cached reads go stale."""
    options = policy['quota_refresh']
    for worker in workers(policy):
        need = interval(policy, worker) + options['agent_period_seconds']
        have = config.quota_cfg(policy, worker).get('freshness_seconds', 0)
        if have < need:
            raise ValueError(f'{worker} freshness_seconds {have} < refresh interval + agent period ({need})')
    return True


def _state_path(policy, key, default):
    return config.policy_path(policy, 'quota_refresh', key, default)


def load_state(policy):
    try:
        return json.loads(_state_path(policy, 'state_file', 'state/quota_refresh.json').read_text())
    except (OSError, ValueError):
        return {}


def save_state(policy, state):
    path = _state_path(policy, 'state_file', 'state/quota_refresh.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class AlreadyRunning(Exception):
    pass


class RefreshLock:
    def __init__(self, path):
        self.path, self.handle = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, 'a+')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise AlreadyRunning('quota refresh already running')
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def refresh(policy, once=True, worker=None, now=None, io=None, clock=None, sleep=None, max_cycles=None):
    """Refresh due observers serially. Returns a report of what ran, was skipped or backed off."""
    validate(policy)
    io = io or default_io(policy)
    options = policy['quota_refresh']
    if worker and worker not in io:
        raise ValueError(f'{worker} has no enabled quota observer')
    names = [worker] if worker else [w for w in workers(policy) if w in io]
    cycles = 0
    with RefreshLock(_state_path(policy, 'lock_file', 'state/quota_refresh.lock')):
        while True:
            current = clock() if clock else _utcnow(now)
            report = _refresh_cycle(policy, names, current, io, forced=bool(worker))
            cycles += 1
            if once or (max_cycles and cycles >= max_cycles):
                return report
            (sleep or __import__('time').sleep)(options['agent_period_seconds'])


def _refresh_cycle(policy, names, now, io, forced):
    options = policy['quota_refresh']
    state = load_state(policy)
    zone = config.tz(policy)
    report = {'at': now.astimezone(zone).isoformat(timespec='seconds'), 'workers': {}}
    for name in names:
        entry = state.setdefault(name, {})
        cached = io[name]['load']()
        until = exhausted_until(evidence_of(cached), now)
        if until is not None:
            report['workers'][name] = {'action': 'skipped', 'why': 'exhausted until ' + _local(until, zone)}
            continue
        due = _parse(entry.get('next_due_at'))
        if not forced and due is not None and due > now:
            report['workers'][name] = {'action': 'not_due', 'next_due': _local(due, zone)}
            continue
        entry['last_attempt_at'] = now.isoformat()
        try:
            observation = io[name]['observe'](policy)
        except Exception:
            observation = None
        evidence = evidence_of(observation)
        if evidence is not None:
            io[name]['save'](observation)
            entry.update(failures=0, last_success_at=now.isoformat(),
                         next_due_at=(now + timedelta(seconds=interval(policy, name))).isoformat())
            previous = entry.get('cli_version')
            version = observation.get('cli_version')
            if version:
                if previous and version != previous:
                    alerts.raise_alert(policy, f'{name}:version:{version}', 'cli_version',
                                       f'{name} CLI version changed {previous} -> {version}; check the observer',
                                       worker=name, level='warn', now=now)
                entry['cli_version'] = version
            if observation.get('version_warning'):
                alerts.raise_alert(policy, f'{name}:version_warning:{version}', 'cli_version',
                                   observation['version_warning'], worker=name, level='warn', now=now)
            percent, window, reset = peak(evidence)
            report['workers'][name] = {'action': 'refreshed', 'percent_used': percent, 'window': window}
        else:
            failures = entry.get('failures', 0) + 1
            delay = min(options['backoff']['base_seconds'] * 2 ** (failures - 1), options['backoff']['max_seconds'])
            entry.update(failures=failures, next_due_at=(now + timedelta(seconds=delay)).isoformat())
            report['workers'][name] = {'action': 'failed', 'failures': failures, 'retry_in_seconds': delay,
                                       'reason': (observation or {}).get('reason') if isinstance(observation, dict)
                                       else 'observer_error'}
    save_state(policy, state)
    evaluate_alerts(policy, now=now, io=io, state=state)
    alerts.write_summary(policy, status_rows(policy, now=now, io=io), now=now)
    return report


def evaluate_alerts(policy, now=None, io=None, state=None):
    """Threshold, stale/unknown alerts from cached readings (deduped per meter/threshold/reset)."""
    io = io or default_io(policy)
    state = load_state(policy) if state is None else state
    now = _utcnow(now)
    zone = config.tz(policy)
    warn = policy['run_task']['pacing']['warn_percent']
    stop = policy['run_task']['pacing']['refuse_full_percent']
    stale_after = timedelta(minutes=policy['quota_refresh']['stale_alert_minutes'])
    raised = []
    for name in workers(policy):
        if name not in io:
            continue
        evidence = evidence_of(io[name]['load']())
        percent, window, reset = peak(evidence)
        if percent is not None:
            for level, limit, what in (('stop', stop, 'stop new full-size work'), ('warn', warn, 'warn')):
                if percent >= limit:
                    record = alerts.raise_alert(
                        policy, f'{name}:{level}:{reset_period(reset)}', 'threshold',
                        f'{name} {window} at {percent}% (>= {limit}%: {what}); resets {_local(_parse(reset), zone)}',
                        worker=name, level=level, now=now)
                    raised += [record] if record else []
                    break  # the higher level implies the lower one
        if exhausted_until(evidence, now):
            continue  # a known-exhausted pool is not "stale"
        last = _parse((state.get(name) or {}).get('last_success_at'))
        observed = [_parse(r.get('observed_at')) for r in ((evidence or {}).get('windows') or {}).values()]
        observed = max([o for o in observed if o], default=None)
        newest = max([t for t in (last, observed) if t], default=None)
        if newest is None or now - newest > stale_after:
            since = newest.isoformat() if newest else 'never'
            record = alerts.raise_alert(policy, f'{name}:stale:{since}', 'stale',
                                        f'{name} meter unknown or stale (last good reading {_local(newest, zone)})',
                                        worker=name, level='warn', now=now)
            raised += [record] if record else []
    return raised


def reset_period(reset):
    """Dedupe id for a reset: nearest hour, so readings whose reset jitters by seconds share one alert."""
    moment = _parse(reset)
    if moment is None:
        return str(reset)
    return (moment + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:00Z')


def _local(value, zone=None):
    if value is None:
        return 'never'
    moment = value.astimezone(zone) if zone else value.astimezone()
    return moment.strftime('%d %b %H:%M %Z').strip()


def read_meter(policy, worker, now=None, io=None):
    """One worker's meter: fresh cache first, else one live read of that worker only."""
    io = io or default_io(policy)
    if worker not in io:
        unmetered = worker == 'developer' or worker in (policy.get('unmetered_workers') or [])
        return {'meters': {}, 'eligible': [worker] if unmetered else [],
                'reason': 'unmetered' if unmetered else 'no_meter', 'source': None}
    now = _utcnow(now)
    cached = io[worker]['load']()
    evidence, source = evidence_of(cached), 'cache'
    if exhausted_until(evidence, now) is None and not is_fresh(evidence, now):
        try:
            observation = io[worker]['observe'](policy)
        except Exception:
            observation = None
        evidence, source = evidence_of(observation), 'live'
        if evidence is not None:
            io[worker]['save'](observation)
    percent, _, _ = peak(evidence)
    limit = threshold(policy, worker)
    usable = evidence is not None and (is_fresh(evidence, now) or exhausted_until(evidence, now) is not None)
    ok = usable and percent is not None and percent < limit
    return {'meters': {worker: percent if usable else None}, 'eligible': [worker] if ok else [],
            'reason': 'quota_available' if ok else 'quota_exhausted_or_over_threshold' if usable else 'quota_unknown',
            'source': source}


def status_rows(policy, now=None, io=None):
    io = io or default_io(policy)
    now = _utcnow(now)
    zone = config.tz(policy)
    state = load_state(policy)
    rows = []
    for name in [w for w in workers(policy) if w in io]:
        evidence = evidence_of(io[name]['load']())
        percent, window, reset = peak(evidence)
        observed = [_parse(r.get('observed_at')) for r in ((evidence or {}).get('windows') or {}).values()]
        observed = max([o for o in observed if o], default=None)
        age = f'{max(0, int((now - observed).total_seconds() // 60))}m' if observed else '-'
        fresh = is_fresh(evidence, now)
        until = exhausted_until(evidence, now)
        limit = threshold(policy, name)
        if until:
            eligible = 'no (exhausted)'
        elif not fresh:
            eligible = 'no (stale)' if evidence else 'no (unknown)'
        else:
            eligible = 'yes' if percent is not None and percent < limit else f'no (>= {limit}%)'
        windows = ', '.join(f'{w} {r.get("percent_used")}%' for w, r in ((evidence or {}).get('windows') or {}).items())
        rows.append({'worker': name, 'used': f'{percent}%' if percent is not None else '?',
                     'windows': windows or '-',
                     'freshness': f'{"fresh" if fresh else "held (exhausted)" if until else "stale"} ({age})',
                     'resets': _local(_parse(reset), zone) if reset else '-', 'eligible': eligible,
                     'failures': (state.get(name) or {}).get('failures', 0)})
    return rows


def status_table(policy, now=None, io=None):
    rows = status_rows(policy, now=now, io=io)
    headers = ('worker', 'used', 'windows', 'freshness', 'resets', 'eligible')
    widths = {h: max([len(h)] + [len(str(r[h])) for r in rows]) for h in headers}
    lines = ['  '.join(h.ljust(widths[h]) for h in headers)]
    lines += ['  '.join(str(r[h]).ljust(widths[h]) for h in headers) for r in rows]
    pending = alerts.open_alerts(policy)
    lines += ['', f'open alerts: {len(pending)}']
    lines += [f'  {a["id"]}  {a["level"]:<4}  {a["message"]}' for a in pending]
    return '\n'.join(lines)


# ---- LaunchAgent (rendered here; loading it is the user's call) ----------------------

def program_path():
    """The installed `llm-router` console script, else `python -m llm_router`."""
    found = shutil.which('llm-router')
    return [found] if found else [sys.executable, '-m', 'llm_router']


def render_agent(policy, program=None):
    options = policy['launch_agent']
    template = resources.files('llm_router').joinpath('templates/launchd.plist').read_text()
    from xml.sax.saxutils import escape
    argv = program or program_path()
    args = ''.join(f'\n    <string>{escape(a)}</string>' for a in argv + ['quota', 'refresh', '--once'])
    # launchd starts with a minimal PATH; carry the PATH the user installed from so CLIs resolve,
    # plus any explicit llm-router / XDG location overrides.
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin')}
    env.update({k: v for k, v in os.environ.items() if k.startswith('LLM_ROUTER_') or
                k in ('XDG_CONFIG_HOME', 'XDG_STATE_HOME')})
    environment = ''.join(f'\n    <key>{escape(k)}</key>\n    <string>{escape(v)}</string>' for k, v in sorted(env.items()))
    logs = config.state_dir() / 'logs'
    text = (template.replace('__PROGRAM_ARGUMENTS__', args).replace('__ENVIRONMENT__', environment)
            .replace('__STATE_DIR__', str(config.state_dir()))
            .replace('__LOG_DIR__', str(logs))
            .replace('__INTERVAL__', str(int(policy['quota_refresh']['agent_period_seconds'])))
            .replace('__LABEL__', options['label']))
    plistlib.loads(text.encode())  # must be a valid plist
    return text


def agent_commands(policy):
    options = policy['launch_agent']
    target = Path(options['target_dir']).expanduser() / f'{options["label"]}.plist'
    uid = os.getuid()
    return {'plist': str(target),
            'install': ['launchctl', 'bootstrap', f'gui/{uid}', str(target)],
            'uninstall': ['launchctl', 'bootout', f'gui/{uid}/{options["label"]}']}


def install_agent(policy, yes=False, runner=subprocess.run, program=None):
    """Without --yes: print the plist path and exact commands only (nothing written or loaded)."""
    if sys.platform != 'darwin' and not program:
        return {'dry_run': True, 'note': 'LaunchAgents are macOS only; see docs/scheduling.md for systemd/cron'}
    commands = agent_commands(policy)
    text = render_agent(policy, program=program)
    if not yes:
        return {'dry_run': True, 'would_write': commands['plist'], 'would_run': ' '.join(commands['install']),
                'plist': text, 'note': 'Loading a LaunchAgent is an install: rerun with --yes when you mean it.'}
    target = Path(commands['plist'])
    if target.exists():
        return {'dry_run': False, 'refused': f'{target} already exists; uninstall it first'}
    target.parent.mkdir(parents=True, exist_ok=True)
    (config.state_dir() / 'logs').mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    os.chmod(target, 0o644)
    done = runner(commands['install'], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return {'dry_run': False, 'wrote': str(target), 'ran': ' '.join(commands['install']),
            'returncode': done.returncode}


def uninstall_agent(policy, yes=False, runner=subprocess.run):
    commands = agent_commands(policy)
    if not yes:
        return {'dry_run': True, 'would_run': ' '.join(commands['uninstall']), 'would_remove': commands['plist']}
    done = runner(commands['uninstall'], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    Path(commands['plist']).unlink(missing_ok=True)
    return {'dry_run': False, 'ran': ' '.join(commands['uninstall']), 'returncode': done.returncode,
            'removed': commands['plist']}
