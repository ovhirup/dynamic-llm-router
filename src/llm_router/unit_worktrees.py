"""Unit worktrees: one git worktree + branch per task-DAG unit, for parallel dispatch isolation.

A batch takes a validated unit list (task_dag.parse_units) plus a {unit id: worker} assignment
(task_dag.assign or explicit) and creates, from one resolved base sha, one worktree per unit under
<worktree_root>/units-<batch id>/ on branch `dlr/<unit-id-slug>-<worker>` (a `-<batch id>` suffix is
added only when that branch already exists; an existing branch is never overwritten). A JSON manifest
under the state dir records each unit for the merge queue (#40); finished_units() is that seam.

Only task_dag.PARALLEL_POOL workers get a worktree: grok-bot (and anything unknown) is refused.
Never pushes, never touches the main checkout's working tree, index or branch.
"""
from datetime import datetime
import json
from pathlib import Path
import shutil

from . import config, task_dag
from .job_dispatch import Refused, RepoLock, _git, _path, resolve_base, slugify

BRANCH_PREFIX = 'dlr/'
BATCH_PREFIX = 'units-'


def _units_dir(policy):
    return _path(policy, 'units_dir', 'logs/units')


def _manifest_path(policy, batch_id):
    if not task_dag.UNIT_ID.fullmatch(batch_id or ''):
        raise Refused(f'bad batch id: {batch_id!r}')
    return _units_dir(policy) / f'{batch_id}.json'


def _save(path, manifest):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(manifest, indent=2) + '\n')
    tmp.replace(path)


def load_manifest(batch_id, policy=None):
    policy = policy or config.load_policy()
    path = _manifest_path(policy, batch_id)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise Refused(f'no such unit batch: {batch_id}')



def update_worker(batch_id, unit_id, worker, policy=None, allocation=None):
    """Persist an automatic reassignment under the existing repository lock."""
    from . import allocation_dispatch
    policy = policy or config.load_policy()
    if worker not in task_dag.PARALLEL_POOL:
        raise Refused('unit worker must belong to the parallel pool')
    fields = allocation_dispatch.allocation_fields(
        {'allocation': allocation, 'allocation_history': []}) if allocation is not None else {}
    if allocation is not None and not fields:
        raise Refused('invalid unit allocation')
    manifest = load_manifest(batch_id, policy)
    with RepoLock(_path(policy, 'lock_dir', 'logs/locks'), Path(manifest['repo'])):
        manifest = load_manifest(batch_id, policy)
        entry = next((item for item in manifest['units'] if item['id'] == unit_id), None)
        if entry is None:
            raise Refused(f'no such unit: {unit_id}')
        entry['worker'] = worker
        if fields:
            entry['allocation'] = fields['allocation']
        _save(_manifest_path(policy, batch_id), manifest)
    return entry


def _branch_exists(repo, branch):
    return bool(_git(repo, 'rev-parse', '-q', '--verify', f'refs/heads/{branch}').stdout.strip())


def _valid_branch(repo, branch):
    return _git(repo, 'check-ref-format', '--branch', branch).returncode == 0


def branch_name(repo, unit_id, worker, batch_id, taken=()):
    """`dlr/<unit-id-slug>-<worker>`, or with `-<batch id>` on collision; refuses unsafe or unavailable names."""
    if type(unit_id) is not str or not task_dag.UNIT_ID.fullmatch(unit_id):
        raise Refused(f'unsafe unit id: {unit_id!r}')
    if worker not in task_dag.PARALLEL_POOL:
        raise Refused(f'unit {unit_id}: worker {worker!r} is not in the parallel pool (grok-bot never gets a unit worktree)')
    base = f'{BRANCH_PREFIX}{slugify(unit_id, 64)}-{worker}'
    for candidate in (base, f'{base}-{batch_id}'):
        if not _valid_branch(repo, candidate):
            raise Refused(f'unit {unit_id}: unsafe branch name {candidate!r}')
        if candidate not in taken and not _branch_exists(repo, candidate):
            return candidate
    raise Refused(f'unit {unit_id}: branches {base!r} and its batch-suffixed form already exist')


def _new_batch_id(policy, repo):
    root = _path(policy, 'worktree_root', 'worktrees')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    for n in range(100):
        batch_id = f'{slugify(repo.name, 30)}-{stamp}' + (f'-{n}' if n else '')
        if not (root / f'{BATCH_PREFIX}{batch_id}').exists() and not _manifest_path(policy, batch_id).exists():
            return batch_id
    raise Refused('could not pick a free batch id')


def _plan(units, assignment):
    """[(unit, worker, wave)] in wave order, then input order; refuses unassigned or non-pool workers."""
    wave_of = {uid: n for n, wave in enumerate(task_dag.waves(units)) for uid in wave}
    unknown = set(assignment) - {u['id'] for u in units}
    if unknown:
        raise Refused(f'assignment names unknown unit {sorted(unknown)[0]!r}')
    plan = []
    for unit in units:
        worker = assignment.get(unit['id'])
        if worker is None:
            raise Refused(f"unit {unit['id']}: no worker assigned")
        if worker not in task_dag.PARALLEL_POOL:
            raise Refused(f"unit {unit['id']}: worker {worker!r} is not in the parallel pool "
                          '(grok-bot never gets a unit worktree)')
        plan.append((unit, worker, wave_of[unit['id']]))
    return sorted(plan, key=lambda item: item[2])  # stable: keeps input order within a wave


def _remove_unit(repo, entry, base_sha, delete_branch):
    """Remove one unit's worktree (--force) and maybe its branch; returns (worktree_state, branch_state)."""
    worktree = Path(entry['worktree'])
    if worktree.exists() or entry['worktree'] in _git(repo, 'worktree', 'list', '--porcelain').stdout:
        removed = _git(repo, 'worktree', 'remove', '--force', str(worktree))
        wt_state = 'pruned' if removed.returncode == 0 else 'prune_failed'
    else:
        wt_state = 'missing'
    branch = entry['branch']
    head = _git(repo, 'rev-parse', '-q', '--verify', f'refs/heads/{branch}').stdout.strip()
    if not head:
        return wt_state, 'missing'
    if delete_branch == 'all' or (delete_branch and head == base_sha):
        deleted = _git(repo, 'branch', '-D', branch)
        return wt_state, 'deleted' if deleted.returncode == 0 else 'delete_failed'
    return wt_state, 'kept'


def create_batch(repo, units, assignment, base='HEAD', policy=None, batch_id=None):
    """Create one worktree + branch per unit from one base sha; returns the manifest. Rolls back on failure."""
    policy = policy or config.load_policy()
    repo = Path(repo).expanduser().resolve()
    if _git(repo, 'rev-parse', '--is-inside-work-tree').stdout.strip() != 'true':
        raise Refused(f'not a git repository: {repo}')
    try:  # re-validate (parse_units is idempotent on its own output)
        units = task_dag.parse_units(units)
    except task_dag.DagError as error:
        raise Refused(f'bad units: {error}')
    if type(assignment) is not dict:
        raise Refused('assignment must map unit id -> worker')
    plan = _plan(units, assignment)
    base_sha = resolve_base(repo, base)
    if not base_sha:
        raise Refused(f'base ref does not resolve to a commit: {base}')
    with RepoLock(_path(policy, 'lock_dir', 'logs/locks'), repo):
        batch_id = batch_id or _new_batch_id(policy, repo)
        manifest_path = _manifest_path(policy, batch_id)
        batch_root = _path(policy, 'worktree_root', 'worktrees') / f'{BATCH_PREFIX}{batch_id}'
        if manifest_path.exists() or batch_root.exists():
            raise Refused(f'unit batch already exists: {batch_id}')
        entries, taken = [], set()
        for unit, worker, wave in plan:
            branch = branch_name(repo, unit['id'], worker, batch_id, taken)
            taken.add(branch)
            entries.append({'id': unit['id'], 'worker': worker, 'wave': wave, 'branch': branch,
                            'worktree': str(batch_root / slugify(unit['id'], 64)), 'base_sha': base_sha,
                            'scope': unit['scope'], 'deps': unit['deps']})
        if len({e['worktree'] for e in entries}) != len(entries):
            raise Refused('two units would share a worktree path')
        created = []
        try:
            batch_root.mkdir(parents=True)
            for entry in entries:
                _git(repo, 'worktree', 'add', '-q', '-b', entry['branch'], entry['worktree'], base_sha, check=True)
                created.append(entry)
        except BaseException:
            for entry in reversed(created):
                _remove_unit(repo, entry, base_sha, delete_branch='all')
            shutil.rmtree(batch_root, ignore_errors=True)
            raise
        manifest = {'batch_id': batch_id, 'repo': str(repo), 'base': base, 'base_sha': base_sha,
                    'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
                    'worktree_root': str(batch_root), 'state': 'active', 'units': entries}
        try:
            _save(manifest_path, manifest)
        except BaseException:
            for entry in reversed(created):
                _remove_unit(repo, entry, base_sha, delete_branch='all')
            shutil.rmtree(batch_root, ignore_errors=True)
            raise
    return manifest


def list_batches(policy=None, repo=None):
    """Manifests (newest first), each unit annotated with whether its worktree exists and its branch head."""
    policy = policy or config.load_policy()
    wanted = str(Path(repo).expanduser().resolve()) if repo else None
    batches = []
    directory = _units_dir(policy)
    for path in sorted(directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True) \
            if directory.exists() else []:
        try:
            manifest = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if wanted and manifest.get('repo') != wanted:
            continue
        units = [{'id': u['id'], 'worker': u['worker'], 'wave': u['wave'], 'branch': u['branch'],
                  'worktree': u['worktree'], 'worktree_exists': Path(u['worktree']).exists()}
                 for u in manifest.get('units', [])]
        batches.append({'batch_id': manifest.get('batch_id'), 'repo': manifest.get('repo'),
                        'base_sha': manifest.get('base_sha'), 'state': manifest.get('state'),
                        'created_at': manifest.get('created_at'), 'units': units})
    return {'batches': batches}


def finished_units(batch_id, policy=None):
    """Seam for the merge queue (#40): manifest entries, in manifest order, whose branch has commits beyond
    base_sha and whose worktree (if still present) has no uncommitted changes. Each gets a `head` sha."""
    manifest = load_manifest(batch_id, policy)
    repo = Path(manifest['repo'])
    done = []
    for entry in manifest['units']:
        head = _git(repo, 'rev-parse', '-q', '--verify', f"refs/heads/{entry['branch']}").stdout.strip()
        if not head or head == entry['base_sha']:
            continue
        if _git(repo, 'merge-base', '--is-ancestor', entry['base_sha'], head).returncode != 0:
            continue
        worktree = Path(entry['worktree'])
        if worktree.exists() and _git(worktree, 'status', '--porcelain', '--untracked-files=all').stdout.strip():
            continue
        done.append(dict(entry, head=head))
    return done


def remove_batch(repo, batch_id, keep_branches=False, all_branches=False, policy=None):
    """Mirror run-job cleanup: `git worktree remove --force` every unit worktree, delete branches with no commits
    beyond base (all branches when all_branches; none when keep_branches). Safe after success or failure."""
    policy = policy or config.load_policy()
    repo = Path(repo).expanduser().resolve()
    manifest = load_manifest(batch_id, policy)
    if manifest['repo'] != str(repo):
        raise Refused(f"batch {batch_id} belongs to {manifest['repo']}, not {repo}")
    delete = False if keep_branches else 'all' if all_branches else True
    report = {'batch_id': batch_id, 'units': []}
    with RepoLock(_path(policy, 'lock_dir', 'logs/locks'), repo):
        for entry in manifest['units']:
            wt_state, br_state = _remove_unit(repo, entry, manifest['base_sha'], delete)
            entry.update(worktree_state=wt_state, branch_state=br_state)
            report['units'].append({'id': entry['id'], 'branch': entry['branch'],
                                    'worktree_state': wt_state, 'branch_state': br_state})
        _git(repo, 'worktree', 'prune')
        root = Path(manifest['worktree_root'])
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
        failed = any(u['worktree_state'] == 'prune_failed' or u['branch_state'] == 'delete_failed'
                     for u in report['units'])
        manifest['state'] = 'removal_incomplete' if failed else 'removed'
        manifest['removed_at'] = datetime.now().astimezone().isoformat(timespec='seconds')
        _save(_manifest_path(policy, batch_id), manifest)
    report['ok'] = not failed
    return report


def load_units_file(path):
    """Units file: a unit list (worker = each unit's single `workers` entry) or {"units": [...], "assign": {...}}."""
    try:
        data = json.loads(Path(path).expanduser().read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise Refused(f'cannot read units file: {error}')
    if type(data) is dict:
        units, assignment = data.get('units'), data.get('assign')
    else:
        units, assignment = data, None
    try:
        parsed = task_dag.parse_units(units)
    except task_dag.DagError as error:
        raise Refused(f'bad units: {error}')
    if assignment is None:
        assignment = {}
        for unit in parsed:
            if not unit['workers'] or len(unit['workers']) != 1:
                raise Refused(f"unit {unit['id']}: give exactly one worker in `workers` or an `assign` map")
            assignment[unit['id']] = unit['workers'][0]
    return parsed, assignment
