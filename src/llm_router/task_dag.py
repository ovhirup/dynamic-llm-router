"""Task DAG: split a job into units, each with a file scope and dependencies, and plan parallel waves.

A unit is {'id', 'prompt', 'scope', 'deps', 'workers'?}. `scope` lists repo-relative files or
directories (a trailing '/' marks a directory). Two units are independent when neither depends on
the other (transitively) and their scopes do not overlap; only independent units share a wave.

Worker assignment: only PARALLEL_POOL workers are dispatched in parallel. grok-bot is never in
that pool; a unit gets grok-bot only when no pool worker is both capable and has quota left.
"""
from pathlib import PurePosixPath
import re

PARALLEL_POOL = ('claude', 'codex', 'grok-build', 'cursor', 'local')
LAST_RESORT = 'grok-bot'
UNIT_ID = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')
MAX_UNITS = 64


class DagError(ValueError):
    pass


def _scope_path(value):
    if type(value) is not str or not value or len(value) > 400 or '\\' in value or '\0' in value:
        raise DagError('bad scope path')
    directory = value.endswith('/')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('..', '.') for part in value.rstrip('/').split('/')) \
            or str(path) in ('', '.'):
        raise DagError(f'scope path must be repo-relative: {value!r}')
    return str(path) + ('/' if directory else '')


def parse_units(data):
    """Validate and normalise a unit list; raise DagError on bad shape, unknown deps or cycles."""
    if type(data) is not list or not data or len(data) > MAX_UNITS:
        raise DagError('units must be a non-empty list')
    units, seen = [], set()
    for raw in data:
        if type(raw) is not dict or not {'id', 'prompt', 'scope'} <= set(raw) or \
                not set(raw) <= {'id', 'prompt', 'scope', 'deps', 'workers'}:
            raise DagError('bad unit shape')
        uid = raw['id']
        if type(uid) is not str or not UNIT_ID.fullmatch(uid) or uid in seen:
            raise DagError(f'bad or duplicate unit id: {uid!r}')
        seen.add(uid)
        if type(raw['prompt']) is not str or not raw['prompt'].strip():
            raise DagError(f'unit {uid}: empty prompt')
        if type(raw['scope']) is not list or not raw['scope']:
            raise DagError(f'unit {uid}: scope must be a non-empty list')
        deps = raw.get('deps', [])
        if type(deps) is not list or not all(type(d) is str for d in deps) or len(set(deps)) != len(deps):
            raise DagError(f'unit {uid}: bad deps')
        workers = raw.get('workers')
        if workers is not None and (type(workers) is not list or not all(type(w) is str for w in workers)):
            raise DagError(f'unit {uid}: bad workers')
        units.append({'id': uid, 'prompt': raw['prompt'], 'scope': sorted({_scope_path(p) for p in raw['scope']}),
                      'deps': list(deps), 'workers': None if workers is None else list(workers)})
    for unit in units:
        missing = [d for d in unit['deps'] if d not in seen or d == unit['id']]
        if missing:
            raise DagError(f"unit {unit['id']}: unknown or self dependency {missing[0]!r}")
    _topological(units)
    return units


def _topological(units):
    by_id = {u['id']: u for u in units}
    order, state = [], {}

    def visit(uid):
        if state.get(uid) == 'done':
            return
        if state.get(uid) == 'open':
            raise DagError(f'dependency cycle at {uid!r}')
        state[uid] = 'open'
        for dep in by_id[uid]['deps']:
            visit(dep)
        state[uid] = 'done'
        order.append(uid)

    for unit in units:
        visit(unit['id'])
    return order


def _covers(directory, path):
    return directory.endswith('/') and (path + '/').startswith(directory)


def scopes_overlap(a, b):
    """True when any path in scope a equals, contains or is contained by a path in scope b."""
    return any(x == y or _covers(x, y) or _covers(y, x) for x in a for y in b)


def waves(units):
    """Ordered lists of unit ids; units in one wave have met deps and pairwise-disjoint scopes."""
    by_id = {u['id']: u for u in units}
    order = _topological(units)
    done, result = set(), []
    while len(done) < len(order):
        wave, claimed = [], []
        for uid in order:
            unit = by_id[uid]
            if uid in done or not set(unit['deps']) <= done:
                continue
            if any(scopes_overlap(unit['scope'], scope) for scope in claimed):
                continue
            wave.append(uid)
            claimed.append(unit['scope'])
        done.update(wave)
        result.append(wave)
    return result


def assign(units, quota_left, capable=None):
    """{unit id: worker or None} for one wave.

    quota_left maps worker -> units it can still take (spent as units are assigned, in order).
    capable(worker, unit) -> bool; by default a unit's `workers` list, else any worker.
    """
    left = dict(quota_left)
    capable = capable or (lambda worker, unit: unit.get('workers') is None or worker in unit['workers'])
    result = {}
    for unit in units:
        choice = next((w for w in PARALLEL_POOL if left.get(w, 0) > 0 and capable(w, unit)), None)
        if choice is None and left.get(LAST_RESORT, 0) > 0 and capable(LAST_RESORT, unit):
            choice = LAST_RESORT
        if choice is not None:
            left[choice] -= 1
        result[unit['id']] = choice
    return result
