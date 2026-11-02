"""llm-router command line."""
import argparse
import json
import os
from datetime import datetime, timezone
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
    init.add_argument('--force', action='store_true', help='back up and overwrite config.json')
    init.add_argument('--workers')
    init.add_argument('--never', default='')
    init.add_argument('--meters', default='')
    init.add_argument('--non-interactive', action='store_true')
    init.add_argument('--dry-run', action='store_true')
    init_map_cmd = commands.add_parser('init-map',
                                       help='Write AGENTS.md, CLAUDE.md, docs/decisions.md and docs/learnings.md')
    init_map_cmd.add_argument('path', nargs='?', default='.', help='target repository root (default: .)')
    init_map_cmd.add_argument('--force', action='store_true', help='overwrite existing map files')
    commands.add_parser('doctor', help='Validate config, binaries and observers (no worker is run)')

    selection = commands.add_parser('select', help='Show which worker would be picked (reads meters)')
    selection.add_argument('--size', choices=('draft', 'full'), default='full')
    selection.add_argument('--cached-only', action='store_true')
    selection.add_argument('--class', dest='task_class', choices=config.TASK_CLASSES)
    selection.add_argument('--role', choices=('planner', 'coder', 'searcher', 'judge', 'advisor'),
                           help='pick worker by role (planner, coder, searcher, judge, advisor)')

    task = commands.add_parser('run-task', help='Run a task file headless in a clean git checkout')
    task.add_argument('--task-file', required=True)
    task.add_argument('--cwd', required=True)
    task.add_argument('--size', choices=('draft', 'full'), default='full')
    task.add_argument('--worker')
    task.add_argument('--class', dest='task_class', choices=config.TASK_CLASSES)
    task.add_argument('--allow-dirty', action='store_true')

    job = commands.add_parser('run-job', help='Run a task in a new worktree branch, test, retry once, commit locally')
    job.add_argument('--repo', required=True)
    job.add_argument('--task-file', required=True)
    job.add_argument('--base', help='base ref (default: current HEAD of --repo)')
    job.add_argument('--branch', help='new branch name (default router/<slug>-<stamp>)')
    job.add_argument('--test-cmd', help='shell test command (default: test_cmd from repos.json)')
    job.add_argument('--size', choices=('draft', 'full'), default='full')
    job.add_argument('--worker')
    job.add_argument('--class', dest='task_class', choices=config.TASK_CLASSES)
    job.add_argument('--orchestrator', choices=('cursor', 'claude', 'codex', 'grok-build', 'grok-bot'),
                     help='calling orchestrator tool (default: auto-detected from environment)')
    job.add_argument('--role', choices=('planner', 'coder', 'searcher', 'judge', 'advisor'),
                     help='role for this job (planner, coder, searcher, judge, advisor)')
    keep = job.add_mutually_exclusive_group()
    keep.add_argument('--keep', action='store_true', help='keep the worktree even on failure')
    keep.add_argument('--prune', action='store_true', help='remove the worktree even on success')

    dispatch = commands.add_parser('bot-dispatch',
                                   help='Map --class and --size to class_order, call run-job and print one JSON verdict')
    dispatch.add_argument('--repo', required=True)
    dispatch.add_argument('--task-file', required=True)
    dispatch.add_argument('--class', dest='task_class', default='feature',
                          help='task class (' + ', '.join(config.TASK_CLASSES) + '; research is refused)')
    dispatch.add_argument('--size', choices=('draft', 'full'), default=None,
                          help='task size (default: draft for --class draft, else full)')
    dispatch.add_argument('--base', help='base ref (default: current HEAD of --repo)')
    dispatch.add_argument('--branch', help='new branch name (default router/<slug>-<stamp>)')
    dispatch.add_argument('--test-cmd', help='shell test command (default: test_cmd from repos.json)')
    dispatch.add_argument('--worker')
    dispatch_keep = dispatch.add_mutually_exclusive_group()
    dispatch_keep.add_argument('--keep', action='store_true', help='keep the worktree even on failure')
    dispatch_keep.add_argument('--prune', action='store_true', help='remove the worktree even on success')

    jobs = commands.add_parser('jobs', help='List recent run-job jobs and worktrees')
    jobs.add_argument('--limit', type=int, default=20)
    jobs.add_argument('--live-feed', action='store_true',
                      help='emit the live jobs feed (running, 30m recent, history, 3-line redacted log preview)')
    clean = commands.add_parser('cleanup', help='Remove old router worktrees')
    clean.add_argument('--older-than', required=True, help='age like 30m, 12h or 7d')
    clean.add_argument('--dry-run', action='store_true')

    unit_cmd = commands.add_parser('units', help='One worktree + branch per task-DAG unit (parallel isolation)')
    unit_cmds = unit_cmd.add_subparsers(dest='units_command', required=True)
    unit_create = unit_cmds.add_parser('create', help='Create a unit batch from a units JSON file')
    unit_create.add_argument('--repo', required=True)
    unit_create.add_argument('--units-file', required=True,
                             help='unit list (one worker per unit in `workers`) or {"units": [...], "assign": {...}}')
    unit_create.add_argument('--base', default='HEAD')
    unit_list = unit_cmds.add_parser('list', help='List unit batches')
    unit_list.add_argument('--repo')
    unit_remove = unit_cmds.add_parser('remove', help='Remove a batch: worktrees, and branches with no commits')
    unit_remove.add_argument('--repo', required=True)
    unit_remove.add_argument('--batch', required=True)
    branches = unit_remove.add_mutually_exclusive_group()
    branches.add_argument('--keep-branches', action='store_true', help='delete no branches')
    branches.add_argument('--all-branches', action='store_true', help='delete branches even with commits')
    unit_queue = unit_cmds.add_parser('queue', help='Merge queue: gate finished units (tests, diff scope, review); '
                                      'never pushes or merges')
    unit_queue.add_argument('--batch', required=True)
    unit_review = unit_cmds.add_parser('review', help='Live stronger-model review (claude, else grok-bot handoff) '
                                       'of needs_review queue entries; records the verdict; never pushes or merges')
    unit_review.add_argument('--batch', required=True)
    unit_review.add_argument('--unit', help='review only this unit (default: every needs_review entry)')
    unit_review.add_argument('--reviewer', choices=('claude', 'grok-bot'),
                             help='use only this reviewer (default: claude, then grok-bot handoff)')
    unit_review.add_argument('--verdict', choices=('approve', 'reject'),
                             help='record a verdict from an out-of-band review (e.g. grok-bot) instead of '
                                  'dispatching one; needs --unit')
    unit_review.add_argument('--notes', default='')
    unit_qstatus = unit_cmds.add_parser('queue-status', help='List merge-queue entries and their gate results')
    unit_qstatus.add_argument('--batch')
    unit_run = unit_cmds.add_parser('run', help='Assign units, create worktrees, run them in DAG waves and gate '
                                    'finished units in the merge queue; never pushes or merges')
    unit_run.add_argument('--repo', required=True)
    unit_run.add_argument('--units-file', required=True,
                          help='unit list or {"units": [...], "assign": {...}}; without an assignment, '
                               'workers are picked from current meter evidence')
    unit_run.add_argument('--base', default='HEAD')
    unit_run.add_argument('--assign-file', help='JSON {unit id: worker}; overrides any inline assign')
    unit_run.add_argument('--orchestrator', choices=('cursor', 'claude', 'codex', 'grok-build', 'grok-bot'),
                          help='calling orchestrator tool (default: auto-detected from environment)')

    orch = commands.add_parser('orchestrator', help='Dynamic orchestrator detection and budget-aware handoff')
    orch_cmds = orch.add_subparsers(dest='orchestrator_command', required=True)
    orch_status = orch_cmds.add_parser('status', help='Show current orchestrator, why it was chosen, and fallback chain')
    orch_status.add_argument('--orchestrator', choices=('cursor', 'claude', 'codex', 'grok-build', 'grok-bot'))

    evaluate = commands.add_parser('eval', help='Evaluation harness: replay past jobs and A/B two routing policies')
    eval_cmds = evaluate.add_subparsers(dest='eval_command', required=True)
    capture = eval_cmds.add_parser('capture', help='Build the replay corpus (JSONL) from run-job logs')
    capture.add_argument('--jobs-dir', help='job logs to read (default: the jobs_dir)')
    capture.add_argument('--out', help='corpus file (default: <state>/logs/eval/corpus.jsonl)')
    capture.add_argument('--limit', type=int, help='keep only the newest N jobs')
    replay = eval_cmds.add_parser('run', help='Replay the corpus under two policies and write the A/B report')
    replay.add_argument('--policies', required=True, help='JSON file: [baseline spec, candidate spec]')
    replay.add_argument('--corpus', help='corpus file (default: <state>/logs/eval/corpus.jsonl)')
    replay.add_argument('--eval-set', action='store_true',
                        help='use the packaged DLR benchmark eval set (fixed cases per task class) when no --corpus')
    replay.add_argument('--class', dest='task_class', default=None,
                        choices=('feature', 'bugfix', 'review', 'mechanical', 'draft'),
                        help='only replay cases of this task class')
    replay.add_argument('--mode', choices=('offline', 'live'), default='offline')
    replay.add_argument('--fake-workers', help='offline: JSON file of per-worker fake profiles')
    replay.add_argument('--no-recorded', action='store_true',
                        help='offline: never replay recorded outcomes, use fake profiles for every worker')
    replay.add_argument('--size', choices=('draft', 'full'), default=None,
                        help='override task size (e.g. --size draft for stale-meter runs); recorded in report')
    replay.add_argument('--yes', action='store_true', help='live: confirm running real workers')

    quota = commands.add_parser('quota', help='Quota meter refresh')
    quota_cmds = quota.add_subparsers(dest='quota_command', required=True)
    refresh = quota_cmds.add_parser('refresh')
    refresh.add_argument('--once', action='store_true', help='one serial pass (what the scheduler runs)')
    refresh.add_argument('--worker')
    commands.add_parser('status', help='Meters, freshness, resets, eligibility, open alerts')
    usage = commands.add_parser('usage', aliases=['burn-down'],
                                help='Token burn-down dashboard: used %, remaining, reset, burn rate and next worker')
    usage.add_argument('--json', action='store_true', dest='as_json', help='print machine-readable JSON')
    alert = commands.add_parser('alerts', help='List or acknowledge open alerts')
    alert_cmds = alert.add_subparsers(dest='alerts_command')
    acknowledge = alert_cmds.add_parser('ack')
    acknowledge.add_argument('--id', action='append', dest='ids')
    for name in ('install-agent', 'uninstall-agent'):
        agent = commands.add_parser(name, help='macOS LaunchAgent for quota refresh (dry run unless --yes)')
        agent.add_argument('--yes', action='store_true', help='actually write/load the agent')

    trig = commands.add_parser('triggers', help='Proactive event triggers from GitHub issues, CI failures, and PR comments')
    trig_cmds = trig.add_subparsers(dest='triggers_command', required=True)
    trig_cmds.add_parser('status', help='Show trigger configuration, seen event count, and recent dispatches')

    wd = commands.add_parser('watchdog', help='Monitor running jobs for stalls, loops, drift, or infra failures')
    wd.add_argument('--stall-seconds', type=float, default=300, help='seconds without heartbeat/log before stalled')
    wd.add_argument('--loop-threshold', type=int, default=3, help='repeated identical lines before looping')
    wd.add_argument('--recover', action='store_true', help='terminate unhealthy workers and reassign to next eligible')

    scan = commands.add_parser('scan', help='Leak-scan a git tree (tracked + untracked, not ignored) before publishing')
    scan.add_argument('path', nargs='?', default='.')
    scan.add_argument('--terms', help='private terms file (default: the configured private_terms_file)')
    scan.add_argument('--history', action='store_true', help='also scan every commit, author and message')

    gate_help = {'review': 'Write the evidence bundle for a committed job',
                 'approve': 'Record an approval bound to the current head SHA',
                 'deny': 'Record a denial for the current head SHA',
                 'undo': 'Undo an unused approval within its undo window (or before it is used)',
                 'push': 'Push the approved router branch (never force) and open a DRAFT PR',
                 'ci': 'Read gh pr checks for the job PR; --watch polls with backoff',
                 'merge': 'Merge with the repo merge_method: needs a merge approval + green required checks'}
    for name, text in gate_help.items():
        gate = commands.add_parser(name, help=text)
        gate.add_argument('job_id')
        if name in ('approve', 'deny', 'undo'):
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


def init_config(force=False, workers=None, never='', meters='', non_interactive=False,
                dry_run=False, input_fn=input, output_fn=print, which=shutil.which, is_tty=None):
    from importlib import resources
    known = {'codex': 'codex', 'grok-build': 'grok', 'cursor': 'cursor-agent',
             'claude': 'claude', 'aider': 'aider', 'opencode': 'opencode', 'aider-ollama': 'ollama',
             'nativ': 'nativ'}
    detected = [name for name, binary in known.items() if which(binary)]
    interactive = (sys.stdin.isatty() if is_tty is None else is_tty) and not non_interactive
    split = lambda value: [v.strip() for v in value.split(',') if v.strip()] if isinstance(value, str) else list(value)
    if workers is None and not interactive:
        raise config.ConfigError('--workers is required for non-interactive init; detected CLIs: ' + ','.join(detected))
    if workers is None:
        output_fn('Detected CLIs: ' + ', '.join(detected))
        default = ','.join(w for w in detected if w != 'claude')
        answer = input_fn(f'Enter workers in preference order, comma-separated [{default}]: ')
        chosen = split(answer or default)
        if 'claude' in detected:
            opted_in = input_fn('Claude Code detected. Add it? [y/N] ').strip().lower() in ('y', 'yes')
            chosen = [w for w in chosen if w != 'claude']
            if opted_in:
                chosen.append('claude')
    else:
        chosen = split(workers)
    if len(set(chosen)) != len(chosen) or any(w not in known for w in chosen):
        raise config.ConfigError('unknown or duplicate workers; choose from: ' + ','.join(known))
    banned, enabled = split(never), split(meters)
    if any(w not in ('codex', 'grok-build', 'cursor', 'claude') for w in enabled):
        raise config.ConfigError('--meters names must be codex,grok-build,cursor,claude')
    if any(w not in chosen for w in enabled):
        raise config.ConfigError('--meters must name chosen workers')
    if interactive:
        for name in chosen:
            if name in ('grok-build', 'cursor', 'claude') and name not in enabled:
                if input_fn(f'{name}: Enable experimental usage reader? [y/N] ').strip().lower() in ('y', 'yes'):
                    enabled.append(name)
    if 'codex' in chosen and 'codex' not in enabled:
        enabled.append('codex')
    fragment = {'worker_order': chosen, 'never': banned, 'workers': {}, 'quota': {}, 'unmetered_workers': []}
    examples = resources.files('llm_router').joinpath('examples')
    warnings = []
    for name in chosen:
        if name in ('aider', 'aider-ollama', 'opencode', 'nativ'):
            preset = json.loads(examples.joinpath('workers', name + '.json').read_text())
            fragment['workers'].update(preset['workers'])
        if name in ('codex', 'grok-build', 'cursor', 'claude'):
            fragment['quota'][name] = {'enabled': name in enabled}
        if name not in enabled:
            fragment['unmetered_workers'].append(name)
            warnings.append(f'{name} is unmetered: not paced')
        if name == 'opencode':
            warnings.append('prompt passed in argv: visible to other local users in the process list')
    config.validate(config.deep_merge(config.defaults(), fragment))
    target = config.config_dir()
    path = target / 'config.json'
    if path.exists() and not force and not dry_run:
        raise config.ConfigError('config.json already exists; use --force to back it up and overwrite')
    summary = {'config_dir': str(target), 'config': fragment, 'detected': detected, 'warnings': warnings,
               'dry_run': dry_run, 'written': [], 'skipped_existing': []}
    if dry_run:
        return summary
    target.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name('config.json.bak-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        shutil.copyfile(path, backup)
        backup.chmod(0o600)
        summary['backup'] = str(backup)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(fragment, handle, indent=2)
        handle.write('\n')
    path.chmod(0o600)
    summary['written'].append(str(path))
    for name, dest in [('repos.example.json', 'repos.json'), ('private_terms.example.txt', 'private_terms.txt')]:
        path = target / dest
        if path.exists():
            summary['skipped_existing'].append(str(path))
        else:
            path.write_text(examples.joinpath(name).read_text())
            path.chmod(0o600)
            summary['written'].append(str(path))
    return summary


def doctor(policy):
    from . import observers, quota_service
    report = paths_report(policy)
    problems = []
    warnings = []
    for worker in dict.fromkeys(policy['worker_order'] + list(policy.get('workers', {}))):
        if worker == 'developer':
            continue
        argv0 = policy['run_task']['commands'][worker][0]
        placeholder = config.WORKER_BINARY.get(worker)
        if worker in policy.get('workers', {}) or not placeholder:
            executable = argv0
            for key in config.BINARY_DEFAULTS:
                executable = executable.replace('{' + key + '}', config.binary(policy, key))
            if not shutil.which(executable):
                problems.append(f'{worker}: binary {executable!r} not found on PATH')
        if policy['run_task'].get('prompt_delivery', {}).get(worker) == 'arg':
            warnings.append(f'{worker}: prompt passed in argv: visible to other local users in the process list')
        quota = config.quota_cfg(policy, worker)
        if worker in policy['worker_order'] and quota and not quota.get('enabled', True) and worker not in policy.get('unmetered_workers', []):
            warnings.append(f'{worker}: observer disabled and worker is not unmetered')
        if placeholder and worker not in policy.get('workers', {}) and not report['binaries'].get(placeholder):
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
    report['warnings'] = warnings
    report['problems'] = problems
    report['ok'] = not problems
    return report


def main(argv=None):
    parser, gate_help = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'init':
        try:
            _print(init_config(force=args.force, workers=args.workers, never=args.never, meters=args.meters,
                               non_interactive=args.non_interactive, dry_run=args.dry_run))
            return 0
        except config.ConfigError as error:
            _print({'ok': False, 'config_error': str(error)})
            return 2
    try:
        policy = config.load_policy(args.config)
    except config.ConfigError as error:
        print(json.dumps({'ok': False, 'config_error': str(error)}))
        return 2
    if args.command == 'init-map':
        from . import agent_map
        _print(agent_map.init_map(args.path, force=args.force))
        return 0
    if args.command == 'paths':
        _print(paths_report(policy))
        return 0
    if args.command == 'doctor':
        report = doctor(policy)
        _print(report)
        return 0 if report['ok'] else 1
    if args.command == 'triggers':
        from . import triggers
        _print(triggers.status_report(policy))
        return 0
    if args.command == 'watchdog':
        from . import watchdog
        report = watchdog.check_all(policy=policy, stall_seconds=args.stall_seconds,
                                    loop_threshold=args.loop_threshold, recover=args.recover)
        _print(report)
        return 0 if report['ok'] or args.recover else 1
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
            elif args.command == 'undo':
                result = pr_gate.undo(args.job_id, args.action, note=args.note, policy=policy)
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
    if args.command == 'eval':
        from . import eval_corpus, eval_harness
        if args.eval_command == 'capture':
            _print(eval_corpus.capture(policy, source=args.jobs_dir, out=args.out, limit=args.limit))
            return 0
        try:
            specs = json.loads(Path(args.policies).read_text())
            profiles = json.loads(Path(args.fake_workers).read_text()) if args.fake_workers else {}
            if args.eval_set and not args.corpus:
                cases = eval_corpus.default_eval_set()
            else:
                cases = eval_corpus.load(args.corpus or eval_corpus.corpus_path(policy))
            report = eval_harness.run(cases, specs, mode=args.mode, profiles=profiles, policy=policy,
                                      confirm=args.yes, use_recorded=not args.no_recorded,
                                      task_class=args.task_class, size=args.size)
        except (eval_harness.Refused, OSError, json.JSONDecodeError) as refusal:
            print(json.dumps({'ok': False, 'refused': str(refusal)}))
            return 3
        stamp = config.now_local(policy).strftime('%Y%m%d-%H%M%S')
        _print({'gate': report['gate'], 'task_class': args.task_class, 'cases': report['cases'],
                'size_override': report.get('size_override'),
                'metrics': {name: entry['metrics'] for name, entry in report['policies'].items()},
                'by_class': {name: entry['by_class'] for name, entry in report['policies'].items()},
                'skipped': len(report['skipped']), 'report': eval_harness.write_report(report, policy, stamp)})
        return 0 if report['gate']['adopt'] else 1
    if args.command in ('usage', 'burn-down'):
        from . import burn_down
        report = burn_down.usage_report(policy)
        if args.as_json:
            _print(report)
        else:
            print(burn_down.render_usage_table(report))
        return 0
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
    if args.command == 'orchestrator':
        from . import orchestrator as orch_module
        _print(orch_module.status_report(policy=policy, explicit=args.orchestrator))
        return 0
    if args.command == 'bot-dispatch':
        from . import job_dispatch
        result = job_dispatch.bot_dispatch(
            args.repo, args.task_file, task_class=args.task_class, size=args.size,
            base=args.base, branch=args.branch, test_cmd=args.test_cmd, worker=args.worker,
            keep=True if args.keep else False if args.prune else None, policy=policy)
        _print(result)
        if result['action'] == 'refused':
            return 3
        return 0 if result['action'] in ('dispatched', 'handoff_to_orchestrator') else 1
    if args.command in ('run-job', 'jobs', 'cleanup'):
        from . import job_dispatch
        try:
            if args.command == 'run-job':
                result = job_dispatch.run_job(args.repo, args.task_file, base=args.base, branch=args.branch,
                                              test_cmd=args.test_cmd, size=args.size, worker=args.worker,
                                              keep=True if args.keep else False if args.prune else None,
                                              policy=policy, task_class=args.task_class,
                                              orchestrator=args.orchestrator, role=args.role)
            elif args.command == 'jobs':
                if getattr(args, 'live_feed', False):
                    from . import live_jobs_feed
                    result = live_jobs_feed.build_live_jobs_feed(policy=policy)
                else:
                    result = job_dispatch.list_jobs(policy=policy, limit=args.limit)
            else:
                result = job_dispatch.cleanup(args.older_than, dry_run=args.dry_run, policy=policy)
        except job_dispatch.Refused as refusal:
            print(json.dumps({'ok': False, 'refused': str(refusal)}))
            return 3
        _print(result)
        if result.get('status') == 'handoff_to_orchestrator':
            return 0
        return 3 if result.get('status') == 'refused' else 0 if result.get('ok', True) else 1
    if args.command == 'units':
        from . import merge_queue
        from . import unit_worktrees as uw
        try:
            if args.units_command == 'queue':
                result = merge_queue.queue_batch(args.batch, policy=policy)
            elif args.units_command == 'review':
                from . import unit_review
                if args.verdict:
                    if not args.unit:
                        raise uw.Refused('--verdict needs --unit')
                    entry = merge_queue.record_review(args.batch, args.unit, args.verdict, notes=args.notes,
                                                      policy=policy, reviewer=args.reviewer or 'manual')
                    result = {'ok': entry['status'] == 'ready_for_human', 'entry': entry}
                else:
                    reviewers = (args.reviewer,) if args.reviewer else unit_review.REVIEWERS
                    result = unit_review.review_batch(args.batch, args.unit, policy=policy, reviewers=reviewers)
            elif args.units_command == 'queue-status':
                result = merge_queue.queue_status(args.batch, policy=policy)
            elif args.units_command == 'create':
                units, assignment = uw.load_units_file(args.units_file)
                result = uw.create_batch(args.repo, units, assignment, base=args.base, policy=policy)
            elif args.units_command == 'run':
                from . import wave_runner
                units, assignment = wave_runner.load_run_files(args.units_file, args.assign_file)
                result = wave_runner.run_batch(args.repo, units, assignment, base=args.base, policy=policy,
                                               orchestrator=args.orchestrator)
            elif args.units_command == 'list':
                result = uw.list_batches(policy=policy, repo=args.repo)
            else:
                result = uw.remove_batch(args.repo, args.batch, keep_branches=args.keep_branches,
                                         all_branches=args.all_branches, policy=policy)
        except uw.Refused as refusal:
            print(json.dumps({'ok': False, 'refused': str(refusal)}))
            return 3
        _print(result)
        return 0 if result.get('ok', True) else 1
    if args.command == 'run-task':
        from . import task_dispatch
        try:
            result = task_dispatch.run_task(args.task_file, args.cwd, size=args.size, worker=args.worker,
                                            allow_dirty=args.allow_dirty, policy=policy,
                                            task_class=args.task_class)
        except task_dispatch.Refused as refusal:
            print(json.dumps({'ok': False, 'refused': str(refusal)}))
            return 3
        _print(result)
        return 0 if result['ok'] or result.get('status') == 'handoff_to_orchestrator' else 1
    if args.command == 'select':
        from . import roles, selection
        chars = 0 if args.size == 'draft' else (policy.get('developer') or {}).get('draft_max_chars', 4000) + 1
        base_sel = selection.select(chars, policy=policy, cached_only=args.cached_only, task_class=args.task_class)
        if args.role:
            role_pick = roles.select_for_role(
                args.role, policy=policy, eligible=base_sel.get('eligible'), meters=base_sel.get('meters'))
            base_sel.update(worker=role_pick['worker'], role=args.role, role_pick=role_pick)
        _print(base_sel)
        return 0
    parser.error('unknown command')
    return 2


if __name__ == '__main__':
    sys.exit(main())
