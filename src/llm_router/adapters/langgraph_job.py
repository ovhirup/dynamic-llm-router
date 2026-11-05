"""Durable job graph (#238): plan, dispatch, test, judge, and a capped fix loop.

Nodes call the #235 adapter hooks. The fix loop stops on ``retry_feedback.same_failure_twice``,
the same stop ``job_dispatch`` uses. Checkpoints are keyed by job id (``thread_id``).

When the ``langgraph`` extra is installed, the graph is a LangGraph ``StateGraph`` with
``SqliteSaver``. Otherwise the same nodes and routes run on a stdlib SQLite checkpoint so the
core install stays free of framework dependencies. Finished nodes are not run again on resume.
"""

from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import signal
import sqlite3

from .. import retry_feedback, traces

NODES = ('plan', 'dispatch', 'test', 'judge', 'fix')
END = 'end'
_STEP_CAP = 50
_FALLBACK_KIND = {'test': 'test_run', 'judge': 'judge'}


def run_durable_job(adapter, steps, *, thread_id, db_path, review=True, max_retries=1,
                    policy=None, passing=('passed',), backend='auto'):
    """Run or resume one job. ``steps`` maps each phase to a zero-arg callable of that DLR step.

    ``backend`` is ``auto`` (LangGraph when the extra imports, else stdlib SQLite), ``sqlite``,
    or ``langgraph``. ``langgraph`` raises ``RuntimeError`` naming the extra when it is missing.
    """
    missing = [name for name in NODES if not callable(steps.get(name))]
    if missing:
        raise ValueError(f'steps missing callable phase(s): {", ".join(missing)}')
    if backend not in ('auto', 'sqlite', 'langgraph'):
        raise ValueError(f'unknown durable backend {backend!r}')
    state = _initial(review, max_retries, passing)
    nodes = _nodes(adapter, steps, policy, thread_id)
    if backend == 'sqlite' or (backend == 'auto' and not _langgraph_available()):
        return _run_sqlite(db_path, thread_id, state, nodes)
    if not _langgraph_available():
        raise RuntimeError('LangGraph job checkpoints need the optional extra: '
                           'pip install dynamic-llm-router[langgraph]')
    return _run_langgraph(db_path, thread_id, state, nodes)


def install_graph(builder, nodes, start, end):
    """Add the DLR phase nodes and edges to a graph builder. Used by LangGraph and tests."""
    worker_routes = {'test': 'test', 'fix': 'fix', 'end': end}
    for name in NODES:
        builder.add_node(name, nodes[name])
    builder.add_edge(start, 'plan')
    builder.add_edge('plan', 'dispatch')
    builder.add_conditional_edges('dispatch', route_after_worker, worker_routes)
    builder.add_conditional_edges('test', route_after_test, {'judge': 'judge', 'fix': 'fix', 'end': end})
    builder.add_conditional_edges('fix', route_after_worker, worker_routes)
    builder.add_edge('judge', end)
    return builder


def route_after_worker(state):
    """Test only after a worker run that succeeded and changed files. Otherwise retry or stop."""
    if state.get('status') == 'failed':
        return END
    worker = state.get('worker_result') or {}
    if worker.get('ok') and worker.get('changed'):
        return 'test'
    return 'fix'


def route_after_test(state):
    """judge, fix, or end. A failed status already applied the retry cap or same-failure stop."""
    passing = tuple(state.get('passing') or ('passed',))
    if state.get('ok') and state.get('tests_status') in passing:
        return 'judge' if state.get('review') else END
    if state.get('status') == 'failed':
        return END
    return 'fix'


def _initial(review, max_retries, passing):
    return {
        'completed': [],
        'fingerprints': [],
        'attempt': 0,
        'max_retries': int(max_retries),
        'review': bool(review),
        'passing': list(passing),
        'status': 'running',
        'ok': False,
        'reason': None,
        'same_failure_twice': False,
        'tests_status': None,
        'result': {},
        'worker_result': {},
        'next': 'plan',
    }


def _nodes(adapter, steps, policy, thread_id):
    def make(name):
        def node(state):
            _maybe_kill(name)
            try:
                result = getattr(adapter, name)(steps[name])
            except Exception as error:
                _emit_span(policy, thread_id, name, None, error)
                raise
            _emit_span(policy, thread_id, name, result, None)
            return _apply(name, state, result)
        return node
    return {name: make(name) for name in NODES}


def _apply(name, state, result):
    if not isinstance(result, dict):
        result = {'value': result}
    result = json.loads(json.dumps(result, default=str))
    update = {
        'result': result,
        'completed': [*list(state.get('completed') or []), name],
    }
    if name in ('dispatch', 'fix'):
        update['attempt'] = int(state.get('attempt') or 0) + 1
        update['worker_result'] = result
        merged = {**state, **update}
        if result.get('ok') and result.get('changed'):
            update['status'] = 'running'
        else:
            # Native job_dispatch leaves tests at not_run unless the worker succeeded and changed files.
            update.update(_failure_update(merged, {'status': 'not_run'}, result))
    elif name == 'test':
        status = result.get('status') or result.get('tests_status') or 'failed'
        passing = tuple(state.get('passing') or ('passed',))
        update['tests_status'] = status
        if status in passing:
            update['status'] = 'passed'
            update['ok'] = True
            update['reason'] = None
        else:
            worker = state.get('worker_result') or {}
            noted = dict(result, status=status)
            update.update(_failure_update(state, noted, worker))
    elif name == 'judge':
        if result.get('verdict') == 'reject':
            update['status'] = 'review_rejected'
            update['ok'] = False
        else:
            update['status'] = 'passed'
            update['ok'] = True
    return update


def _failure_update(state, tests, worker):
    """Fingerprint this cycle and stop on same-failure-twice or the native retry cap."""
    fingerprint = retry_feedback.failure_fingerprint(
        tests, {'ok': bool(worker.get('ok')), 'runs': worker.get('runs') or []})
    fingerprints = [*list(state.get('fingerprints') or []), fingerprint]
    update = {
        'fingerprints': fingerprints,
        'tests_status': tests.get('status') or 'not_run',
        'ok': False,
        'same_failure_twice': False,
        'reason': None,
    }
    attempt = int(state.get('attempt') or 0)
    limit = state.get('max_retries')
    limit = int(limit) if limit is not None else 0
    if retry_feedback.same_failure_twice(fingerprints):
        update['status'] = 'failed'
        update['reason'] = 'same_failure_twice'
        update['same_failure_twice'] = True
    elif attempt > limit:
        update['status'] = 'failed'
    else:
        update['status'] = 'retry'
    return update


def _next_node(node, state):
    if node == 'plan':
        return 'dispatch'
    if node in ('dispatch', 'fix'):
        return route_after_worker(state)
    if node == 'test':
        return route_after_test(state)
    return END


def _run_loop(state, nodes, save):
    for _ in range(_STEP_CAP):
        node = state.get('next') or 'plan'
        if node == END:
            return state
        state = {**state, **nodes[node](state)}
        state['next'] = _next_node(node, state)
        save(state)
        if state['next'] == END:
            return state
    raise RuntimeError('durable job exceeded the step cap')


def _connect(db_path):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE IF NOT EXISTS job_checkpoints (thread_id TEXT PRIMARY KEY, state TEXT NOT NULL)')
    conn.commit()
    return conn


def _load_state(conn, thread_id):
    row = conn.execute('SELECT state FROM job_checkpoints WHERE thread_id = ?', (str(thread_id),)).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def _run_sqlite(db_path, thread_id, state, nodes):
    conn = _connect(db_path)
    try:
        loaded = _load_state(conn, thread_id)
        if loaded is not None:
            state = loaded

        def save(current):
            conn.execute(
                'INSERT OR REPLACE INTO job_checkpoints (thread_id, state) VALUES (?, ?)',
                (str(thread_id), json.dumps(current)))
            conn.commit()

        if state.get('next') == END:
            return state
        return _run_loop(state, nodes, save)
    finally:
        conn.close()


def _langgraph_available():
    try:
        importlib.import_module('langgraph.graph')
        importlib.import_module('langgraph.checkpoint.sqlite')
    except ModuleNotFoundError:
        return False
    return True


def _run_langgraph(db_path, thread_id, state, nodes):
    graph_mod = importlib.import_module('langgraph.graph')
    saver_mod = importlib.import_module('langgraph.checkpoint.sqlite')
    builder = graph_mod.StateGraph(_schema())
    install_graph(builder, nodes, graph_mod.START, graph_mod.END)
    config = {'configurable': {'thread_id': str(thread_id)}}
    with saver_mod.SqliteSaver.from_conn_string(str(db_path)) as checkpointer:
        graph = builder.compile(checkpointer=checkpointer)
        snapshot = graph.get_state(config)
        values = dict(getattr(snapshot, 'values', None) or {})
        pending = tuple(getattr(snapshot, 'next', ()) or ())
        if values and not pending:
            return values
        incoming = None if values else state
        result = graph.invoke(incoming, config)
    return dict(result)


def _schema():
    try:
        from typing import TypedDict
    except ImportError:  # pragma: no cover
        return dict

    class JobState(TypedDict, total=False):
        completed: list
        fingerprints: list
        attempt: int
        max_retries: int
        review: bool
        passing: list
        status: str
        ok: bool
        tests_status: str
        result: dict
        worker_result: dict
        reason: str
        same_failure_twice: bool

    return JobState


def _maybe_kill(node):
    """Test seam: die before ``node`` runs, after the previous checkpoint commit."""
    target = os.environ.get('DLR_DURABLE_KILL_BEFORE')
    flag = os.environ.get('DLR_DURABLE_KILL_FLAG')
    if not target or not flag or target != node:
        return
    path = Path(flag)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(node)
    os.kill(os.getpid(), signal.SIGKILL)


def _emit_span(policy, thread_id, node, result, error):
    """One span per node when the trace writer accepts it. Trace failures do not fail the job."""
    if not policy:
        return
    kind = node if node in getattr(traces, 'SPAN_KINDS', ()) else _FALLBACK_KIND.get(node, 'worker_attempt')
    worker = result.get('worker') if isinstance(result, dict) else None
    tokens = result.get('tokens') if isinstance(result, dict) else None
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    try:
        traces.record_span(
            policy, thread_id, name=node, kind=kind,
            side_effect='mutating' if node in ('dispatch', 'fix') else 'read_only',
            status='failed' if error else 'ok', worker=worker, tokens=tokens,
            error=f'{type(error).__name__}: {error}' if error else None,
            started_at=now, finished_at=now,
        )
    except ValueError:
        if kind == node:
            return
        try:
            traces.record_span(
                policy, thread_id, name=node, kind=_FALLBACK_KIND.get(node, 'worker_attempt'),
                status='failed' if error else 'ok', worker=worker, tokens=tokens,
                error=f'{type(error).__name__}: {error}' if error else None,
                started_at=now, finished_at=now,
            )
        except Exception:
            return
    except Exception:
        return
