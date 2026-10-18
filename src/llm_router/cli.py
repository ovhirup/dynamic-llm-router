"""llm-router command line."""
import argparse
import json
from pathlib import Path
import shutil
import sys

from . import __version__, config


def _print(value):
    print(json.dumps(value, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(prog='llm-router', allow_abbrev=False,
                                     description='Quota-aware dispatch of coding tasks to headless agent CLIs.')
    parser.add_argument('--version', action='version', version=f'llm-router {__version__}')
    parser.add_argument('--config', help='config JSON file (default: $LLM_ROUTER_CONFIG or <config dir>/config.json)')
    commands = parser.add_subparsers(dest='command', required=True)

    commands.add_parser('paths', help='Show config/state locations and which binaries resolve')
    init = commands.add_parser('init', help='Write example config files into the config dir (never overwrites)')
    init.add_argument('--force', action='store_true', help='overwrite existing example files')
    commands.add_parser('doctor', help='Validate config, binaries and observers (no worker is run)')

    selection = commands.add_parser('select', help='Show which worker would be picked (reads meters)')
    selection.add_argument('--size', choices=('draft', 'full'), default='full')
    selection.add_argument('--cached-only', action='store_true')

    task = commands.add_parser('run-task', help='Run a task file headless in a clean git checkout')
    task.add_argument('--task-file', required=True)
    task.add_argument('--cwd', required=True)
    task.add_argument('--size', choices=('draft', 'full'), default='full')
    task.add_argument('--worker')
    task.add_argument('--allow-dirty', action='store_true')

    job = commands.add_parser('run-job', help='Run a task in a new worktree branch, test, retry once, commit locally')
    job.add_argument('--repo', required=True)
    job.add_argument('--task-file', required=True)
    job.add_argument('--base', help='base ref (default: current HEAD of --repo)')
    job.add_argument('--branch', help='new branch name (default router/<slug>-<stamp>)')
    job.add_argument('--test-cmd', help='shell test command (default: test_cmd from repos.json)')
    job.add_argument('--size', choices=('draft', 'full'), default='full')
    job.add_argument('--worker')
    keep = job.add_mutually_exclusive_group()
    keep.add_argument('--keep', action='store_true', help='keep the worktree even on failure')
    keep.add_argument('--prune', action='store_true', help='remove the worktree even on success')

    jobs = commands.add_parser('jobs', help='List recent run-job jobs and worktrees')
    jobs.add_argument('--limit', type=int, default=20)
    clean = commands.add_parser('cleanup', help='Remove old router worktrees')
    clean.add_argument('--older-than', required=True, help='age like 30m, 12h or 7d')
    clean.add_argument('--dry-run', action='store_true')

    quota = commands.add_parser('quota', help='Quota meter refresh')
    quota_cmds = quota.add_subparsers(dest='quota_command', required=True)
    refresh = quota_cmds.add_parser('refresh')
    refresh.add_argument('--once', action='store_true', help='one serial pass (what the scheduler runs)')
    refresh.add_argument('--worker')
    commands.add_parser('status', help='Meters, freshness, resets, eligibility, open alerts')
    alert = commands.add_parser('alerts', help='List or acknowledge open alerts')
    alert_cmds = alert.add_subparsers(dest='alerts_command')
    acknowledge = alert_cmds.add_parser('ack')
    acknowledge.add_argument('--id', action='append', dest='ids')
    for name in ('install-agent', 'uninstall-agent'):
        agent = commands.add_parser(name, help='macOS LaunchAgent for quota refresh (dry run unless --yes)')
        agent.add_argument('--yes', action='store_true', help='actually write/load the agent')

    scan = commands.add_parser('scan', help='Leak-scan a git tree (tracked + untracked, not ignored) before publishing')
    scan.add_argument('path', nargs='?', default='.')
    scan.add_argument('--terms', help='private terms file (default: the configured private_terms_file)')
    scan.add_argument('--history', action='store_true', help='also scan every commit, author and message')

    gate_help = {'review': 'Write the evidence bundle for a committed job',
                 'approve': 'Record an approval bound to the current head SHA',
                 'deny': 'Record a denial for the current head SHA',
                 'push': 'Push the approved router branch (never force) and open a DRAFT PR',
                 'ci': 'Read gh pr checks for the job PR; --watch polls with backoff',
                 'merge': 'Merge with the repo merge_method: needs a merge approval + green required checks'}
    for name, text in gate_help.items():
        gate = commands.add_parser(name, help=text)
        gate.add_argument('job_id')
        if name in ('approve', 'deny'):
            gate.add_argument('--action', choices=('push', 'merge'), required=True)
            gate.add_argument('--note')
        if name == 'approve':
            gate.add_argument('--sha', help='head SHA you reviewed (prefix ok); refused if the branch moved')
            gate.add_argument('--hours', type=float, help='expiry (default pr_gate.approval_ttl_hours)')
        if name == 'ci':
            gate.add_argument('--watch', action='store_true')
            gate.add_argument('--timeout-minutes', type=float)
    return parser, gate_help


def paths_report(policy):
    binaries = {name: shutil.which(config.binary(policy, name)) or None for name in config.BINARY_DEFAULTS}
    return {'version': __version__, 'config_file': str(config.config_file()),
            'config_file_exists': config.config_file().exists(), 'config_dir': str(config.config_dir()),
            'state_dir': str(config.state_dir()), 'timezone': str(config.tz(policy)),
            'worker_order': policy['worker_order'], 'never': policy.get('never') or [],
            'metered_workers': config.metered_workers(policy), 'binaries': binaries}


def init_config(force=False):
    from importlib import resources
    target = config.config_dir()
    target.mkdir(parents=True, exist_ok=True)
    written, skipped = [], []
    examples = resources.files('llm_router').joinpath('examples')
    for name, dest in (('config.example.json', 'config.json'), ('repos.example.json', 'repos.json'),
                       ('private_terms.example.txt', 'private_terms.txt')):
        path = target / dest
        if path.exists() and not force:
            skipped.append(str(path))
            continue
        path.write_text(examples.joinpath(name).read_text())
        path.chmod(0o600)
        written.append(str(path))
    return {'config_dir': str(target), 'written': written, 'skipped_existing': skipped}


def doctor(policy):
    from . import observers, quota_service
    report = paths_report(policy)
    problems = []
    for worker in policy['worker_order']:
        if worker == 'developer':
            continue
        placeholder = config.WORKER_BINARY.get(worker)
        if placeholder and not report['binaries'].get(placeholder):
            problems.append(f'{worker}: binary {config.binary(policy, placeholder)!r} not found on PATH')
    for worker in config.metered_workers(policy):
        try:
            observers.load_observer(config.quota_cfg(policy, worker)['observer'])
        except Exception as error:
            problems.append(f'{worker}: observer not importable ({type(error).__name__})')
        if config.quota_cfg(policy, worker).get('experimental'):
            report.setdefault('experimental_observers', []).append(worker)
    terms = config.policy_path(policy, 'run_task', 'private_terms_file', 'private_terms.txt')
    if not terms.exists():
        problems.append(f'private terms file missing ({terms}); leak checks are secret-shape only')
    report['cached_meters'] = {r['worker']: r['used'] + ' ' + r['freshness'] for r in quota_service.status_rows(policy)}
    report['problems'] = problems
    report['ok'] = not problems
    return report


def main(argv=None):
    parser, gate_help = build_parser()
    args = parser.parse_args(argv)
    try:
        policy = config.load_policy(args.config)
    except config.ConfigError as error:
        print(json.dumps({'ok': False, 'config_error': str(error)}))
        return 2
    if args.command == 'paths':
        _print(paths_report(policy))
        return 0
    if args.command == 'init':
        _print(init_config(force=args.force))
        return 0
    if args.command == 'doctor':
        report = doctor(policy)
        _print(report)
        return 0 if report['ok'] else 1
    if args.command == 'scan':
        from . import security, task_dispatch
        terms = security.load_terms(args.terms) if args.terms else task_dispatch.private_terms(policy)
        result = security.scan_tree(Path(args.path), terms, task_dispatch.leak_options(policy)['allow'],
                                    history=args.history)
        _print(result)
        return 0 if result['status'] == 'clean' else 1
    if args.command in gate_help:
        from . import pr_gate
        try:
            if args.command == 'review':
                result = pr_gate.review(args.job_id, policy=policy)
            elif args.command == 'approve':
                result = pr_gate.approve(args.job_id, args.action, sha=args.sha, hours=args.hours,
                                         note=args.note, policy=policy)
            elif args.command == 'deny':
                result = pr_gate.deny(args.job_id, args.action, note=args.note, policy=policy)
            elif args.command == 'push':
                result = pr_gate.push(args.job_id, policy=policy)
            elif args.command == 'ci':
                result = pr_gate.ci(args.job_id, watch=args.watch, timeout_minutes=args.timeout_minutes,
                                    policy=policy)
            else:
                result = pr_gate.merge(args.job_id, policy=policy)
        except pr_gate.Refused as refusal:
            print(json.dumps({'ok': False, 'refused': str(refusal)}))
            return 3
        _print(result)
        return 1 if args.command == 'ci' and result.get('status') in ('failed', 'error') else 0
    if args.command in ('quota', 'status', 'alerts', 'install-agent', 'uninstall-agent'):
        from . import alerts as alerts_module, quota_service
        if args.command == 'quota':
            try:
                result = quota_service.refresh(policy, once=args.once, worker=args.worker)
            except quota_service.AlreadyRunning as busy:
                result = {'skipped': str(busy)}
            except ValueError as error:
                result = {'ok': False, 'error': str(error)}
        elif args.command == 'status':
            print(quota_service.status_table(policy))
            return 0
        elif args.command == 'alerts':
            if args.alerts_command == 'ack':
                result = {'acknowledged': alerts_module.ack(policy, args.ids)}
            else:
                result = {'open': alerts_module.open_alerts(policy)}
        elif args.command == 'install-agent':
            result = quota_service.install_agent(policy, yes=args.yes)
        else:
            result = quota_service.uninstall_agent(policy, yes=args.yes)
        _print(result)
        return 0
    if args.command in ('run-job', 'jobs', 'cleanup'):
        from . import job_dispatch
        try:
            if args.command == 'run-job':
                result = job_dispatch.run_job(args.repo, args.task_file, base=args.base, branch=args.branch,
                                              test_cmd=args.test_cmd, size=args.size, worker=args.worker,
                                              keep=True if args.keep else False if args.prune else None,
                                              policy=policy)
            elif args.command == 'jobs':
                result = job_dispatch.list_jobs(policy=policy, limit=args.limit)
            else:
                result = job_dispatch.cleanup(args.older_than, dry_run=args.dry_run, policy=policy)
        except job_dispatch.Refused as refusal:
            print(json.dumps({'ok': False, 'refused': str(refusal)}))
            return 3
        _print(result)
        return 0 if result.get('ok', True) else 1
    if args.command == 'run-task':
        from . import task_dispatch
        try:
            result = task_dispatch.run_task(args.task_file, args.cwd, size=args.size, worker=args.worker,
                                            allow_dirty=args.allow_dirty, policy=policy)
        except task_dispatch.Refused as refusal:
            print(json.dumps({'ok': False, 'refused': str(refusal)}))
            return 3
        _print(result)
        return 0 if result['ok'] else 1
    if args.command == 'select':
        from . import selection
        chars = 0 if args.size == 'draft' else (policy.get('developer') or {}).get('draft_max_chars', 4000) + 1
        _print(selection.select(chars, policy=policy, cached_only=args.cached_only))
        return 0
    parser.error('unknown command')
    return 2


if __name__ == '__main__':
    sys.exit(main())
