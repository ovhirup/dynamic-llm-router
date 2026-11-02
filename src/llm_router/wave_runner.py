"""Wave runner (#112): assign task-DAG units to workers and run them in dependency waves.

Thin orchestration over task_dag (parse/assign/waves), unit_worktrees (one worktree + branch per unit),
task_dispatch (concurrency slots, run_task) and merge_queue (gate finished units after every wave).

Decisions (v1):
- Only units assigned a task_dag.PARALLEL_POOL worker get a worktree. Units with no worker, or with the
  last-resort grok-bot, are recorded as `deferred_to_orchestrator` and leave no worktree behind.
- Units inside one wave run sequentially, in wave order, each holding its worker's concurrency slot
  (task_dispatch.acquire_slot) for the duration of its run. A `full` slot defers the unit
  (`deferred_slot_full`) without stopping the wave. Real in-wave parallelism is a follow-up.
- A unit whose dependency did not succeed in this run (deferred or failed) is `blocked_by_dependency`.
- `ok` is false only when a started unit's runner failed; deferrals alone keep it true.
- After every wave merge_queue.queue_batch gates the finished units. Never pushes, merges or opens a PR.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from . import agent_map, alerts, config, orchestrator as orch_module, security, task_dag, task_dispatch
from . import merge_queue
from . import unit_worktrees as uw
from .job_dispatch import Refused, _git

PROMPT_NAME = '.llm-router-unit-prompt.md'


def brief_path(policy, batch_id):
    return config.policy_path(policy or {}, 'run_job', 'waves_dir', 'logs/waves') / f'{batch_id}-brief.json'


def write_continuation_brief(policy, batch_id, brief):
    """Write the wave continuation brief to `<state>/logs/waves/<batch_id>-brief.json`."""
    path = brief_path(policy, batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = security.redact_obj(brief)
    path.write_text(json.dumps(clean, indent=2))
    return path


def read_continuation_brief(policy, batch_id):
    """Read the persisted continuation brief for `batch_id`, or None when absent."""
    path = brief_path(policy, batch_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _has_retained_output(entry):
    """True when the unit worktree already has a committed change (`HEAD != base_sha`) or non-empty diff."""
    worktree = Path(entry.get('worktree') or '/nonexistent')
    if not worktree.is_dir():
        return False
    head = _git(worktree, 'rev-parse', '-q', '--verify', 'HEAD').stdout.strip()
    if head and entry.get('base_sha') and head != entry['base_sha']:
        return True
    status = _git(worktree, 'status', '--porcelain', '--untracked-files=normal').stdout.strip()
    lines = [l for l in status.splitlines() if PROMPT_NAME not in l]
    return bool(lines)


def default_quota_left(units, policy):
    """{worker: units it may take}: every PARALLEL_POOL worker the selector currently finds eligible
    (cached meter evidence) may take every unit; grok-bot is never offered here."""
    from . import selection
    eligible = selection.select(0, policy=policy, cached_only=True).get('eligible') or []
    return {w: len(units) for w in eligible if w in task_dag.PARALLEL_POOL}


def default_unit_runner(entry, prompt, policy):
    """Write the unit prompt into its worktree and run the assigned worker there via task_dispatch.run_task.
    The prompt file lives under the worktree's git dir so it never shows up as an uncommitted change."""
    worktree = Path(entry['worktree'])
    git_dir = Path(task_dispatch._git(worktree, 'rev-parse', '--absolute-git-dir').stdout.strip() or worktree)
    task_file = git_dir / PROMPT_NAME
    task_file.write_text(prompt)
    try:
        result = task_dispatch.run_task(task_file, worktree, worker=entry['worker'], allow_dirty=True,
                                        policy=policy)
    except task_dispatch.Refused as refusal:
        return {'ok': False, 'status': 'refused', 'reason': str(refusal)}
    return {'ok': bool(result.get('ok')), 'status': result.get('status') or ('ok' if result.get('ok') else 'failed'),
            'reason': result.get('reason')}


def _unit_prompt(unit, repo_map=''):
    scope = '\n'.join(f'- {p}' for p in unit['scope'])
    base = (f"{unit['prompt'].rstrip()}\n\nOnly change files inside this scope:\n{scope}\n"
            'Commit your work on the current branch when done. Do not push.\n')
    return f'{base}\n{repo_map}\n' if repo_map else base


def load_run_files(units_file, assign_file=None):
    """(units, assignment or None): units file is a unit list or {"units": [...], "assign": {...}?};
    an --assign-file ({unit id: worker}) overrides any inline `assign`."""
    def read(path):
        try:
            return json.loads(Path(path).expanduser().read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise Refused(f'cannot read {path}: {error}')
    data = read(units_file)
    units, assignment = (data.get('units'), data.get('assign')) if type(data) is dict else (data, None)
    if assign_file:
        assignment = read(assign_file)
    return units, assignment


def run_batch(repo, units, assignment=None, base='HEAD', policy=None, batch_id=None, quota_left=None,
              assign=None, unit_runner=None, acquire_slot=None, test_runner=None, orchestrator=None):
    """Assign, create worktrees, run waves, gate after each wave; returns a JSON-serialisable summary.

    quota_left: {worker: n} or callable(units, policy) -> {worker: n} (default: current meter evidence).
    assign: callable(units, quota_left) -> {unit id: worker or None} (default task_dag.assign).
    unit_runner(entry, prompt, policy) -> {'ok', 'status'?, 'reason'?} (default runs the real worker).
    """
    policy = policy or config.load_policy()
    detected_orch = orch_module.detect_orchestrator(explicit=orchestrator)
    try:
        units = task_dag.parse_units(units)
    except task_dag.DagError as error:
        raise Refused(f'bad units: {error}')
    automatic = assignment is None and assign is None and quota_left is None
    allocations = {}
    if automatic:
        from . import allocation_dispatch
        allocations = {unit['id']: allocation_dispatch.plan_unit(unit, policy) for unit in units}
        assignment = {uid: plan['worker'] for uid, plan in allocations.items()}
    elif assignment is None:
        left = quota_left(units, policy) if callable(quota_left) else \
            quota_left if quota_left is not None else default_quota_left(units, policy)
        assignment = (assign or task_dag.assign)(units, left)
    if type(assignment) is not dict:
        raise Refused('assignment must map unit id -> worker')
    unknown = set(assignment) - {u['id'] for u in units}
    if unknown:
        raise Refused(f'assignment names unknown unit {sorted(unknown)[0]!r}')
    unit_runner = unit_runner or default_unit_runner
    acquire_slot = acquire_slot or task_dispatch.acquire_slot

    deferred, runnable = [], {}
    for unit in units:
        worker = assignment.get(unit['id'])
        if worker in task_dag.PARALLEL_POOL:
            runnable[unit['id']] = worker
            continue
        why = 'no parallel-pool worker with quota' if worker is None else \
            f'worker {worker!r} is not in the parallel pool (orchestrator handles it)'
        deferred.append({'id': unit['id'], 'worker': worker, 'status': 'deferred_to_orchestrator', 'reason': why})

    summary = {'batch_id': None, 'orchestrator': detected_orch['orchestrator'],
               'orchestrator_reason': detected_orch['reason'], 'waves': [], 'deferred': deferred,
               'queue': None, 'continuation_brief': None, 'stall_notice': None, 'ok': True}
    if automatic:
        summary['allocations'] = allocations
    if not runnable:
        stall = {
            'stalled': True,
            'cause': 'all_units_deferred_no_eligible_worker',
            'next_recovery_step': 'check `llm-router status` meters or run with grok-bot orchestrator fallback',
        }
        summary['stall_notice'] = stall
        alerts.raise_alert(policy, f'wave:stall:{len(units)}', 'wave_stall',
                           f"wave stalled: {stall['cause']} ({stall['next_recovery_step']})", level='warn')
        return summary
    # deps on deferred units are dropped for worktree creation only; the run below still honours them
    subset = [dict(u, deps=[d for d in u['deps'] if d in runnable]) for u in units if u['id'] in runnable]
    manifest = uw.create_batch(repo, subset, runnable, base=base, policy=policy, batch_id=batch_id)
    summary['batch_id'] = manifest['batch_id']
    entries = {e['id']: e for e in manifest['units']}
    by_id = {u['id']: u for u in units}
    succeeded = set()
    now_utc = datetime.now(timezone.utc)
    brief = {
        'batch_id': manifest['batch_id'],
        'goal': f"Wave batch {manifest['batch_id']} ({len(units)} units)",
        'mode': 'wave',
        'workers': runnable,
        'shared_concurrency_ceiling': (policy.get('run_task') or {}).get('concurrency') or {},
        'quality_bar': ['tests_passed_zero_skipped', 'diff_in_scope', 'leak_clean'],
        'retry_state': {'succeeded': [], 'deferred': [d['id'] for d in deferred]},
        'next_check_at': (now_utc + timedelta(minutes=5)).isoformat(timespec='seconds'),
    }
    write_continuation_brief(policy, manifest['batch_id'], brief)

    for wave_no, wave in enumerate(task_dag.waves(units)):
        results = []
        for uid in wave:
            if uid not in entries:
                continue
            entry = entries[uid]
            record = {'id': uid, 'worker': entry['worker'], 'wave': wave_no}
            unmet = [d for d in by_id[uid]['deps'] if d not in succeeded]
            if unmet:  # a dependency was deferred or failed: never build on work that did not land
                record.update(ok=False, status='blocked_by_dependency', reason=f'unmet deps: {unmet}')
                summary['deferred'].append(dict(record))
                results.append(record)
                continue
            if _has_retained_output(entry):
                # Adopt, don't duplicate: an existing live/completed worktree commit is adopted directly.
                record.update(ok=True, status='adopted_retained_output', reason='retained output present in worktree')
                succeeded.add(uid)
                results.append(record)
                continue
            if automatic:
                plan = allocation_dispatch.plan_unit(by_id[uid], policy)
                summary['allocations'][uid] = plan
                record['allocation'] = plan
                if plan['worker'] is None:
                    record.update(ok=False, status='deferred_allocation',
                                  reason='no eligible parallel worker in refreshed allocation')
                    summary['deferred'].append(dict(record))
                    results.append(record)
                    continue
                entry = uw.update_worker(manifest['batch_id'], uid, plan['worker'],
                                         policy=policy, allocation=plan)
                entries[uid] = entry
                record['worker'] = entry['worker']
                brief['workers'][uid] = entry['worker']
            state, handle = acquire_slot(policy, entry['worker'])
            if state in ('full', 'local_busy'):
                record.update(ok=False, status='deferred_slot_full',
                              reason=(f"{entry['worker']} concurrency cap reached" if state == 'full'
                                      else 'local_busy: another local model job is running'))
                summary['deferred'].append(dict(record))
                results.append(record)
                continue
            try:
                outcome = unit_runner(entry, _unit_prompt(by_id[uid], agent_map.load_repo_map(entry['worktree'])), policy) or {}
            except Exception as error:  # one unit's crash must not stop the wave
                outcome = {'ok': False, 'status': 'error', 'reason': f'{type(error).__name__}: {error}'}
            finally:
                if handle is not None:
                    handle.close()
            timed_out = bool(outcome.get('timed_out') or outcome.get('status') == 'timeout')
            if not outcome.get('ok') and timed_out and _has_retained_output(entry):
                # Unknown delivery is not failure: client timeout with retained output is adopted, not re-dispatched.
                record.update(ok=True, status='retained_after_timeout',
                              reason='timeout with retained output in worktree; not re-dispatched')
                succeeded.add(uid)
                results.append(record)
                continue
            ok = bool(outcome.get('ok'))
            record.update(ok=ok, status=outcome.get('status') or ('ok' if ok else 'failed'),
                          reason=outcome.get('reason'))
            summary['ok'] = summary['ok'] and ok
            if ok:
                succeeded.add(uid)
            results.append(record)
        summary['waves'].append(results)
        summary['queue'] = merge_queue.queue_batch(manifest['batch_id'], policy=policy, test_runner=test_runner)

    brief['retry_state'] = {
        'succeeded': sorted(succeeded),
        'deferred': [d['id'] for d in summary['deferred']],
    }
    write_continuation_brief(policy, manifest['batch_id'], brief)
    summary['continuation_brief'] = read_continuation_brief(policy, manifest['batch_id'])
    if not succeeded:
        stall = {
            'stalled': True,
            'cause': 'all_workers_stopped_or_blocked',
            'next_recovery_step': 'inspect unit logs/worktrees or re-run after worker cooldowns clear',
        }
        summary['stall_notice'] = stall
        alerts.raise_alert(policy, f"wave:stall:{manifest['batch_id']}", 'wave_stall',
                           f"wave {manifest['batch_id']} stalled: {stall['cause']} ({stall['next_recovery_step']})",
                           level='warn')
    return summary
