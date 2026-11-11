"""Job phase traces (#236): spans, JSONL, optional OTLP, the traces report, and the leak gate."""
import ast
import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from helpers import load_policy
from llm_router import cli, security, traces
from llm_router.adapters.otlp import export_span
from llm_router.adapters.tracing import TracingOrchestrator, record_approval_wait

ROOT = Path(__file__).resolve().parents[1]
PHASES = ('plan', 'dispatch', 'test', 'judge', 'fix', 'approval_wait')
ATTRIBUTES = ('worker', 'model', 'tokens', 'cost_estimate', 'exit_status')


def _policy(base):
    policy = load_policy()
    policy['run_job']['traces_dir'] = str(base / 'traces')
    return policy


class PhaseSpanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.policy = _policy(self.base)
        self.summary = {'job_id': 'job-1', 'task_class': 'feature', 'workers': ['codex'],
                        'tokens': 9999, 'warnings': []}

    def spans(self):
        return traces.load_trace(self.policy, 'job-1')

    def test_each_phase_records_attributes_and_fix_failure_propagates(self):
        adapter = TracingOrchestrator(self.policy, self.summary)
        adapter.plan(lambda: ('instructions', None))
        adapter.dispatch(lambda: {'worker': 'codex', 'model': 'gpt-5', 'tokens': 1000, 'status': 'ok'})
        adapter.test(lambda: {'returncode': 0, 'status': 'passed'})
        adapter.judge(lambda: {'verdict': 'approve', 'worker': 'grok-bot', 'tokens': 50})
        def boom():
            raise RuntimeError('nope')

        with self.assertRaisesRegex(RuntimeError, 'nope'):
            adapter.fix(boom)
        started = datetime.now(timezone.utc) - timedelta(seconds=4)
        record_approval_wait(
            self.policy, {**self.summary, 'finished_at': started.isoformat(timespec='seconds'), 'tokens': 1000},
            exit_status='approved', finished_at=started + timedelta(seconds=4))

        by_name = {}
        for span in self.spans():
            by_name.setdefault(span['name'], span)
        self.assertEqual(set(by_name), set(PHASES))
        for name, span in by_name.items():
            for key in ATTRIBUTES:
                self.assertIn(key, span, name)
            self.assertEqual(span['kind'], name)
        dispatch = by_name['dispatch']
        self.assertEqual(dispatch['worker'], 'codex')
        self.assertEqual(dispatch['model'], 'gpt-5')
        self.assertEqual(dispatch['tokens'], 1000)
        self.assertEqual(dispatch['cost_estimate'], 0.0015)
        self.assertEqual(dispatch['exit_status'], 'ok')
        self.assertEqual(dispatch['task_class'], 'feature')
        self.assertEqual(by_name['test']['exit_status'], 0)
        self.assertEqual(by_name['judge']['exit_status'], 'approve')
        self.assertEqual(by_name['fix']['exit_status'], 'error')
        self.assertEqual(by_name['fix']['status'], 'failed')
        self.assertIsNone(by_name['fix']['tokens'])
        self.assertIsNone(by_name['fix']['cost_estimate'])
        for name in ('plan', 'test', 'judge', 'approval_wait'):
            self.assertIsNone(by_name[name]['tokens'], name)
            self.assertIsNone(by_name[name]['cost_estimate'], name)
        self.assertEqual(by_name['approval_wait']['exit_status'], 'approved')
        self.assertEqual(by_name['approval_wait']['worker'], 'codex')

    def test_fix_tokens_come_from_the_run_task_result(self):
        from llm_router.adapters.tracing import estimate_cost
        adapter = TracingOrchestrator(self.policy, self.summary)
        adapter.fix(lambda: {'worker': 'codex', 'status': 'ok', 'attempts': [{'tokens': 20}]})
        span = self.spans()[-1]
        self.assertEqual(span['name'], 'fix')
        self.assertEqual(span['tokens'], 20)
        self.assertEqual(span['cost_estimate'], estimate_cost('codex', 20))

    def test_span_text_is_redacted_and_the_leak_gate_is_clean(self):
        secret = 'sk-' + 't' * 30
        adapter = TracingOrchestrator(self.policy, self.summary)
        adapter.dispatch(lambda: {'worker': 'codex', 'model': secret, 'tokens': 10, 'status': 'ok'})
        raw = traces.trace_path(self.policy, 'job-1').read_text()
        self.assertNotIn(secret, raw)
        self.assertIn(security.REDACTED, raw)
        gate = traces.scan_trace_files(self.policy)
        self.assertEqual(gate['status'], 'clean')
        self.assertEqual(gate['files_scanned'], 1)
        self.assertEqual(gate['findings'], [])

    def test_otlp_failure_keeps_the_jsonl_span(self):
        self.policy['run_job']['trace_exporter'] = 'otlp'
        adapter = TracingOrchestrator(self.policy, self.summary)
        with mock.patch('llm_router.adapters.otlp.export_span', side_effect=RuntimeError('collector down')):
            self.assertEqual(adapter.test(lambda: {'returncode': 0}), {'returncode': 0})
        self.assertEqual(self.spans()[0]['name'], 'test')
        self.assertTrue(any('otlp export failed' in warning for warning in self.summary['warnings']))

    def test_missing_otlp_extra_is_a_runtime_error(self):
        with mock.patch('llm_router.adapters.otlp._load_exporter', side_effect=ImportError('opentelemetry')):
            with self.assertRaisesRegex(RuntimeError, 'otlp'):
                export_span({'name': 'dispatch', 'worker': 'codex'})


class TraceReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.policy = _policy(self.base)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def span(self, trace_id, name, worker, task_class, seconds, cost, exit_status='ok', started=None):
        started = started or (self.now - timedelta(hours=1))
        traces.record_span(
            self.policy, trace_id, name=name, kind=name, worker=worker, model=worker,
            tokens=100, cost_estimate=cost, exit_status=exit_status, task_class=task_class,
            started_at=started.isoformat(timespec='seconds'),
            finished_at=(started + timedelta(seconds=seconds)).isoformat(timespec='seconds'),
        )

    def test_report_prints_p50_p95_latency_and_cost_per_worker_per_class(self):
        for index, seconds in enumerate((1, 2, 3, 4), start=1):
            self.span(f'job-{index}', 'dispatch', 'codex', 'feature', seconds, 0.1 * index)
        self.span('job-bug', 'test', 'grok-build', 'bugfix', 10, 1.0)
        self.span('job-old', 'dispatch', 'stale', 'feature', 9, 9.0, started=self.now - timedelta(days=30))
        self.span('job-wait', 'approval_wait', 'paused', 'feature', 0, 0.0, exit_status='pending')
        self.span('job-owner', 'approval_wait', 'codex', 'feature', 500, 9.0, exit_status='approved')

        report = traces.report_traces(self.policy, '7d', now=self.now)
        text = report['text']
        self.assertIn('p50/p95 latency and cost per worker per task class', text)
        self.assertIn(
            'worker=codex task_class=feature p50_latency_s=2.500 p95_latency_s=3.850 '
            'p50_cost=0.250000 p95_cost=0.385000',
            text)
        self.assertIn(
            'worker=grok-build task_class=bugfix p50_latency_s=10.000 p95_latency_s=10.000 '
            'p50_cost=1.000000 p95_cost=1.000000',
            text)
        self.assertIn(
            'worker=codex task_class=feature phase=approval_wait p50_latency_s=500.000 '
            'p95_latency_s=500.000 p50_cost=9.000000 p95_cost=9.000000',
            text)
        self.assertNotIn('worker=stale', text)
        self.assertNotIn('worker=paused', text)

        cfg = self.base / 'cfg.json'
        cfg.write_text(json.dumps({
            'worker_order': ['codex'],
            'unmetered_workers': ['codex'],
            'run_job': {'traces_dir': str(self.base / 'traces')},
        }))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['--config', str(cfg), 'report', 'traces', '--since', '7d'])
        self.assertEqual(code, 0)
        printed = out.getvalue()
        self.assertIn('worker=codex task_class=feature', printed)
        self.assertIn('p50_latency_s=2.500', printed)
        self.assertIn('p95_cost=0.385000', printed)
        self.assertNotIn('worker=stale', printed)

    def test_since_rejects_an_unknown_window(self):
        with self.assertRaisesRegex(ValueError, '--since'):
            traces.parse_since('yesterday')


class LeakGateTraceTests(unittest.TestCase):
    def test_scan_blocks_a_secret_planted_in_a_trace_file(self):
        secret = 'sk-' + 'a' * 30
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            tree = base / 'tree'
            tree.mkdir()
            (tree / 'readme.txt').write_text('clean\n')
            traces_dir = base / 'traces'
            traces_dir.mkdir()
            (traces_dir / 'job-9.jsonl').write_text(json.dumps({'model': secret}) + '\n')
            cfg = base / 'cfg.json'
            cfg.write_text(json.dumps({
                'worker_order': ['codex'],
                'unmetered_workers': ['codex'],
                'run_job': {'traces_dir': str(traces_dir)},
            }))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(['--config', str(cfg), 'scan', str(tree)])
        payload = json.loads(out.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload['status'], 'blocked')
        self.assertTrue(any(item['file'].startswith('job-9.jsonl') for item in payload['findings']))
        self.assertNotIn(secret, out.getvalue())


class OptionalExtraTests(unittest.TestCase):
    def test_otlp_is_optional_and_core_does_not_import_it(self):
        text = (ROOT / 'pyproject.toml').read_text()
        head, _, extras = text.partition('[project.optional-dependencies]')
        self.assertIn('dependencies = []', head)
        self.assertNotIn('opentelemetry', head)
        self.assertIn('otlp = [', extras)
        self.assertIn('opentelemetry-sdk', extras)
        self.assertIn('dlr = "llm_router.cli:main"', text)

        for path in sorted((ROOT / 'src' / 'llm_router').rglob('*.py')):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in tree.body:
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                self.assertFalse(any(name.split('.', 1)[0] == 'opentelemetry' for name in modules), path)
            if path.name != 'otlp.py':
                for node in ast.walk(tree):
                    module = getattr(node, 'module', None)
                    # Import aliases have .name; global/nonlocal store plain strings in .names.
                    names = [alias.name for alias in getattr(node, 'names', []) if hasattr(alias, 'name')]
                    imported = [module] if isinstance(node, ast.ImportFrom) and module else names
                    self.assertFalse(
                        any(str(name).split('.', 1)[0] == 'opentelemetry' for name in imported if name),
                        path)

        script = f"""
import sys
sys.path.insert(0, {str(ROOT / 'src')!r})
attempted = []

class Block:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'opentelemetry' or fullname.startswith('opentelemetry.'):
            attempted.append(fullname)
            raise ModuleNotFoundError(fullname)

sys.meta_path.insert(0, Block())
import llm_router.cli
import llm_router.job_dispatch
from llm_router.adapters.tracing import TracingOrchestrator
assert not attempted, attempted
"""
        result = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class JobPhaseTraceTests(unittest.TestCase):
    setUp = __import__('test_job_dispatch').JobTests.setUp
    tearDown = __import__('test_job_dispatch').JobTests.tearDown
    select = __import__('test_job_dispatch').JobTests.select
    worker = __import__('test_job_dispatch').JobTests.worker
    job = __import__('test_job_dispatch').JobTests.job

    def test_run_job_writes_phase_spans_including_approval_wait(self):
        self.policy['run_job']['traces_dir'] = str(self.base / 'traces')
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='test -f out.txt', task_class='feature')
        self.assertTrue(result['ok'], result)
        spans = traces.load_trace(self.policy, result['job_id'])
        names = [span['name'] for span in spans]
        for phase in ('plan', 'dispatch', 'test', 'judge', 'approval_wait'):
            self.assertIn(phase, names)
        phase_spans = [span for span in spans if span['name'] in PHASES]
        for span in phase_spans:
            for key in ATTRIBUTES:
                self.assertIn(key, span)
        dispatch = next(span for span in spans if span['name'] == 'dispatch')
        self.assertEqual(dispatch['worker'], 'codex')
        self.assertEqual(dispatch['task_class'], 'feature')
        waiting = next(span for span in spans if span['name'] == 'approval_wait')
        self.assertEqual(waiting['exit_status'], 'pending')
        raw = traces.trace_path(self.policy, result['job_id']).read_text()
        self.assertEqual(traces.scan_trace_files(self.policy)['status'], 'clean', raw)

    def test_worker_reported_tokens_land_on_the_dispatch_span_only(self):
        from llm_router.adapters.tracing import estimate_cost
        self.policy['run_job']['traces_dir'] = str(self.base / 'traces')
        self.worker("import json; print(json.dumps({'tokens': 4321})); open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='test -f out.txt', task_class='feature')
        self.assertTrue(result['ok'], result)
        spans = traces.load_trace(self.policy, result['job_id'])
        dispatch = next(span for span in spans if span['name'] == 'dispatch')
        self.assertEqual(dispatch['tokens'], 4321)
        self.assertEqual(dispatch['cost_estimate'], estimate_cost('codex', 4321))
        for name in ('plan', 'test', 'judge', 'approval_wait'):
            span = next(item for item in spans if item['name'] == name)
            self.assertIsNone(span['tokens'], name)
            self.assertIsNone(span['cost_estimate'], name)


if __name__ == '__main__':
    unittest.main()
