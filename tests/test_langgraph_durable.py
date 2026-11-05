"""Durable job graph: adapter nodes, same-failure cap, SQLite resume, traces (#238)."""

import ast
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from llm_router.adapters import NativeOrchestrator
from llm_router.adapters.langgraph_job import END, NODES, install_graph, run_durable_job
from llm_router import traces

ROOT = Path(__file__).resolve().parents[1]

CHILD = r"""
import json, os
from llm_router.adapters import NativeOrchestrator
from llm_router.adapters.langgraph_job import run_durable_job

script = json.loads(os.environ['DURABLE_SCRIPT'])
queues = {name: list(script.get(name) or []) for name in script}

def make(name):
    def run():
        with open(os.environ['DURABLE_LOG'], 'a') as handle:
            handle.write(name + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        if not queues[name]:
            raise RuntimeError(name + ' has no scripted result')
        return queues[name].pop(0)
    return run

state = run_durable_job(
    NativeOrchestrator(), {name: make(name) for name in queues},
    thread_id=os.environ['DURABLE_THREAD'], db_path=os.environ['DURABLE_DB'],
    review=True, max_retries=1, backend='sqlite')
print(json.dumps({'status': state['status'], 'ok': state['ok'], 'completed': state['completed']}))
"""


def _boom(message):
    return {'status': 'failed', 'tail': f'AssertionError: {message}'}


def _passed():
    return {'status': 'passed'}


def _scenarios():
    """Twenty phase-outcome fixtures. A live 20-job worktree corpus is not what this runs."""
    approve = {'verdict': 'approve'}
    reject = {'verdict': 'reject'}
    rows = [
        ('pass-approve', True, 1, ('passed',), {'test': [_passed()], 'judge': [approve]},
         'passed', True, ['plan', 'dispatch', 'test', 'judge']),
        ('pass-reject', True, 1, ('passed',), {'test': [_passed()], 'judge': [reject]},
         'review_rejected', False, ['plan', 'dispatch', 'test', 'judge']),
        ('pass-no-review', False, 1, ('passed',), {'test': [_passed()]},
         'passed', True, ['plan', 'dispatch', 'test']),
        ('fix-then-pass-approve', True, 1, ('passed',),
         {'test': [_boom('a'), _passed()], 'fix': [{}], 'judge': [approve]},
         'passed', True, ['plan', 'dispatch', 'test', 'fix', 'test', 'judge']),
        ('fix-then-pass-reject', True, 1, ('passed',),
         {'test': [_boom('a'), _passed()], 'fix': [{}], 'judge': [reject]},
         'review_rejected', False, ['plan', 'dispatch', 'test', 'fix', 'test', 'judge']),
        ('fix-then-pass-no-review', False, 1, ('passed',),
         {'test': [_boom('a'), _passed()], 'fix': [{}]},
         'passed', True, ['plan', 'dispatch', 'test', 'fix', 'test']),
        ('same-failure-high-cap', True, 5, ('passed',),
         {'test': [_boom('same'), _boom('same')], 'fix': [{}]},
         'same_failure_twice', False, ['plan', 'dispatch', 'test', 'fix', 'test']),
        ('two-different-retries-1', True, 1, ('passed',),
         {'test': [_boom('a'), _boom('b')], 'fix': [{}]},
         'failed', False, ['plan', 'dispatch', 'test', 'fix', 'test']),
        ('three-different-retries-2', True, 2, ('passed',),
         {'test': [_boom('a'), _boom('b'), _boom('c')], 'fix': [{}, {}]},
         'failed', False, ['plan', 'dispatch', 'test', 'fix', 'test', 'fix', 'test']),
        ('same-failure-retries-1', True, 1, ('passed',),
         {'test': [_boom('same'), _boom('same')], 'fix': [{}]},
         'same_failure_twice', False, ['plan', 'dispatch', 'test', 'fix', 'test']),
        ('no-retry-on-first-failure', True, 0, ('passed',), {'test': [_boom('a')]},
         'failed', False, ['plan', 'dispatch', 'test']),
        ('dispatch-tokens-do-not-change-outcome', True, 1, ('passed',),
         {'dispatch': [{'ok': True, 'worker': 'codex', 'tokens': 10}], 'test': [_passed()], 'judge': [approve]},
         'passed', True, ['plan', 'dispatch', 'test', 'judge']),
        ('dispatch-failed-no-retry', True, 0, ('passed',),
         {'dispatch': [{'ok': False, 'worker': 'codex'}], 'test': [_boom('worker')]},
         'failed', False, ['plan', 'dispatch', 'test']),
        ('same-failure-retries-4', False, 4, ('passed',),
         {'test': [_boom('loop'), _boom('loop')], 'fix': [{}]},
         'same_failure_twice', False, ['plan', 'dispatch', 'test', 'fix', 'test']),
        ('two-failures-then-pass', False, 3, ('passed',),
         {'test': [_boom('a'), _boom('b'), _passed()], 'fix': [{}, {}]},
         'passed', True, ['plan', 'dispatch', 'test', 'fix', 'test', 'fix', 'test']),
        ('fix-worker-then-approve', True, 1, ('passed',),
         {'test': [_boom('a'), _passed()], 'fix': [{'ok': True, 'worker': 'grok-build'}], 'judge': [approve]},
         'passed', True, ['plan', 'dispatch', 'test', 'fix', 'test', 'judge']),
        ('no-tests-is-not-passing', False, 0, ('passed',), {'test': [{'status': 'no_tests'}]},
         'failed', False, ['plan', 'dispatch', 'test']),
        ('no-tests-opt-in', False, 1, ('passed', 'no_tests'), {'test': [{'status': 'no_tests'}]},
         'passed', True, ['plan', 'dispatch', 'test']),
        ('third-failure-matches-second', True, 5, ('passed',),
         {'test': [_boom('A'), _boom('B'), _boom('B')], 'fix': [{}, {}]},
         'same_failure_twice', False, ['plan', 'dispatch', 'test', 'fix', 'test', 'fix', 'test']),
        ('two-fixes-then-approve', True, 2, ('passed',),
         {'test': [_boom('A'), _boom('B'), _passed()], 'fix': [{}, {}], 'judge': [approve]},
         'passed', True, ['plan', 'dispatch', 'test', 'fix', 'test', 'fix', 'test', 'judge']),
    ]
    scenarios = []
    for name, review, retries, passing, phases, status, ok, completed in rows:
        steps = {'plan': [{}], 'dispatch': [{'ok': True, 'worker': 'codex'}], 'test': [], 'fix': [], 'judge': []}
        steps.update(phases)
        scenarios.append({
            'name': name, 'review': review, 'max_retries': retries, 'passing': passing,
            'steps': steps, 'status': status, 'ok': ok, 'completed': completed,
        })
    return scenarios


def _scripted(spec):
    queues = {name: list(spec[name]) for name in NODES}

    def make(name):
        def run():
            if not queues[name]:
                raise AssertionError(f'{name} was called with no scripted result')
            return queues[name].pop(0)
        return run

    return {name: make(name) for name in NODES}


class _Builder:
    def __init__(self):
        self.nodes = []
        self.edges = []
        self.conditional = []

    def add_node(self, name, fn):
        self.nodes.append(name)

    def add_edge(self, source, target):
        self.edges.append((source, target))

    def add_conditional_edges(self, source, fn, mapping):
        self.conditional.append((source, dict(mapping)))


class DurableJobTests(unittest.TestCase):
    def test_fixture_outcomes_match_the_native_phase_rules(self):
        self.assertEqual(len(_scenarios()), 20)
        for scenario in _scenarios():
            with self.subTest(scenario=scenario['name']):
                with tempfile.TemporaryDirectory() as tmp:
                    state = run_durable_job(
                        NativeOrchestrator(), _scripted(scenario['steps']),
                        thread_id=scenario['name'], db_path=str(Path(tmp) / 'job.sqlite'),
                        review=scenario['review'], max_retries=scenario['max_retries'],
                        passing=scenario['passing'], backend='sqlite')
                self.assertEqual(state['status'], scenario['status'])
                self.assertEqual(state['ok'], scenario['ok'])
                self.assertEqual(state['completed'], scenario['completed'])

    def test_nodes_call_the_adapter_hooks(self):
        adapter = mock.Mock(wraps=NativeOrchestrator())
        with tempfile.TemporaryDirectory() as tmp:
            run_durable_job(
                adapter, _scripted(_scenarios()[0]['steps']),
                thread_id='hooks', db_path=str(Path(tmp) / 'job.sqlite'),
                review=True, max_retries=1, backend='sqlite')
        self.assertEqual([call[0] for call in adapter.mock_calls], ['plan', 'dispatch', 'test', 'judge'])

    def test_graph_edges_cover_the_fix_loop(self):
        builder = _Builder()
        install_graph(builder, {name: lambda state: state for name in NODES}, 'start', END)
        self.assertEqual(builder.nodes, list(NODES))
        self.assertEqual(builder.edges, [
            ('start', 'plan'), ('plan', 'dispatch'), ('dispatch', 'test'), ('fix', 'test'), ('judge', END)])
        self.assertEqual(builder.conditional, [('test', {'judge': 'judge', 'fix': 'fix', 'end': END})])

    def test_same_failure_stops_before_another_fix(self):
        scenario = next(item for item in _scenarios() if item['name'] == 'same-failure-high-cap')
        calls = []
        steps = _scripted(scenario['steps'])
        wrapped = {}
        for name, fn in steps.items():
            def make(phase, operation):
                def run():
                    calls.append(phase)
                    return operation()
                return run
            wrapped[name] = make(name, fn)
        with tempfile.TemporaryDirectory() as tmp:
            state = run_durable_job(
                NativeOrchestrator(), wrapped, thread_id='cap', db_path=str(Path(tmp) / 'job.sqlite'),
                review=True, max_retries=5, backend='sqlite')
        self.assertEqual(state['status'], 'same_failure_twice')
        self.assertEqual(calls.count('fix'), 1)
        self.assertNotIn('judge', calls)

    def test_kill_and_resume_skips_finished_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            db_path = base / 'job.sqlite'
            log = base / 'steps.log'
            flag = base / 'kill.flag'
            script = json.dumps({
                'plan': [{}],
                'dispatch': [{'ok': True, 'worker': 'codex'}],
                'test': [{'status': 'passed'}],
                'fix': [],
                'judge': [{'verdict': 'approve'}],
            })
            env = os.environ.copy()
            env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + env.get('PYTHONPATH', '')
            env.update(DURABLE_SCRIPT=script, DURABLE_LOG=str(log), DURABLE_DB=str(db_path),
                       DURABLE_THREAD='job-238', DLR_DURABLE_KILL_BEFORE='test',
                       DLR_DURABLE_KILL_FLAG=str(flag))
            killed = subprocess.run([sys.executable, '-c', CHILD], capture_output=True, text=True, env=env)
            self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)
            self.assertEqual(log.read_text().splitlines(), ['plan', 'dispatch'])
            env.pop('DLR_DURABLE_KILL_BEFORE')
            env.pop('DLR_DURABLE_KILL_FLAG')
            resumed = subprocess.run([sys.executable, '-c', CHILD], capture_output=True, text=True, env=env)
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            self.assertEqual(log.read_text().splitlines(), ['plan', 'dispatch', 'test', 'judge'])
            payload = json.loads(resumed.stdout)
            self.assertEqual(payload['status'], 'passed')
            self.assertEqual(payload['completed'], ['plan', 'dispatch', 'test', 'judge'])

    def test_finished_checkpoint_does_not_rerun(self):
        calls = []
        steps = _scripted(_scenarios()[2]['steps'])
        wrapped = {}
        for name, fn in steps.items():
            def make(phase, operation):
                def run():
                    calls.append(phase)
                    return operation()
                return run
            wrapped[name] = make(name, fn)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / 'job.sqlite')
            kwargs = dict(thread_id='done', db_path=db_path, review=False, max_retries=1, backend='sqlite')
            first = run_durable_job(NativeOrchestrator(), wrapped, **kwargs)
            second = run_durable_job(NativeOrchestrator(), wrapped, **kwargs)
        self.assertEqual(first['completed'], ['plan', 'dispatch', 'test'])
        self.assertEqual(second['completed'], first['completed'])
        self.assertEqual(calls, ['plan', 'dispatch', 'test'])

    def test_each_node_emits_a_trace_span(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = {'run_job': {'traces_dir': str(Path(tmp) / 'traces')}}
            scenario = next(item for item in _scenarios() if item['name'] == 'dispatch-tokens-do-not-change-outcome')
            run_durable_job(
                NativeOrchestrator(), _scripted(scenario['steps']),
                thread_id='traced', db_path=str(Path(tmp) / 'job.sqlite'),
                review=True, max_retries=1, policy=policy, backend='sqlite')
            spans = traces.load_trace(policy, 'traced')
        self.assertEqual([span['name'] for span in spans], ['plan', 'dispatch', 'test', 'judge'])
        dispatch = spans[1]
        self.assertEqual(dispatch['tokens'], 10)
        self.assertEqual(dispatch['worker'], 'codex')
        for span in spans:
            if span['name'] != 'dispatch':
                self.assertIsNone(span['tokens'])

    def test_langgraph_backend_names_the_extra_when_missing(self):
        with mock.patch.dict(sys.modules, {'langgraph': None, 'langgraph.graph': None,
                                            'langgraph.checkpoint.sqlite': None}):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError) as caught:
                    run_durable_job(
                        NativeOrchestrator(), _scripted(_scenarios()[0]['steps']),
                        thread_id='missing', db_path=str(Path(tmp) / 'job.sqlite'),
                        backend='langgraph')
        self.assertIn('dynamic-llm-router[langgraph]', str(caught.exception))

    def test_import_does_not_load_langgraph(self):
        script = f"""
import sys
sys.path.insert(0, {str(ROOT / 'src')!r})
attempted = []

class BlockFrameworks:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.', 1)[0] in ('langgraph', 'crewai', 'autogen', 'agent_framework'):
            attempted.append(fullname)
            raise ModuleNotFoundError(fullname)

sys.meta_path.insert(0, BlockFrameworks())
import llm_router.cli
import llm_router.job_dispatch
import llm_router.adapters.langgraph_job
assert not attempted, attempted
"""
        result = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_langgraph_import_is_not_at_module_level(self):
        path = ROOT / 'src' / 'llm_router' / 'adapters' / 'langgraph_job.py'
        tree = ast.parse(path.read_text(), filename=str(path))
        module_imports = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                module_imports.extend(alias.name.split('.', 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_imports.append(node.module.split('.', 1)[0])
        self.assertNotIn('langgraph', module_imports)
        for other in (ROOT / 'src' / 'llm_router').rglob('*.py'):
            if other == path:
                continue
            text = other.read_text()
            self.assertNotIn("import_module('langgraph", text)
            self.assertNotIn('import_module("langgraph', text)


if __name__ == '__main__':
    unittest.main()
