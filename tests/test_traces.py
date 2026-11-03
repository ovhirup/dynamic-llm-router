"""Tests for structured job traces, append-only event ledger, replay, and root-cause view (#137)."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from helpers import load_policy
from llm_router import cli, traces


class TraceEngineeringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.policy = load_policy()
        self.policy['run_job']['traces_dir'] = str(self.base / 'traces')

    def test_spans_and_event_ledger_scrub_secrets_and_codex_paths(self):
        secret = 'sk-' + 't' * 30
        span = traces.record_span(
            self.policy,
            'job-100',
            name='attempt-1',
            kind='worker_attempt',
            side_effect='mutating',
            status='ok',
            worker='claude',
            inputs={'prompt': f'read ~/.codex/auth.json and use {secret}'},
            outputs={'diff': '1 file changed'},
            tokens=1200,
        )
        event = traces.append_event(
            self.policy,
            'job-100',
            'claude',
            prompt=f'prompt with {secret}',
            inputs={'file': '/Users/alice/.codex/config.toml'},
            exit_code=0,
            tests_status='passed',
            tokens=1200,
        )
        raw_trace = traces.trace_path(self.policy, 'job-100').read_text()
        raw_events = traces.event_ledger_path(self.policy).read_text()
        for raw in (raw_trace, raw_events):
            self.assertNotIn(secret, raw)
            self.assertNotIn('.codex', raw)
            self.assertNotIn('auth.json', raw)
        self.assertEqual(span['tokens'], 1200)
        self.assertEqual(event['tokens'], 1200)

    def test_replay_skips_earlier_mutating_steps_and_reruns_only_target_step(self):
        traces.record_span(
            self.policy, 'job-200', name='step-1-write', kind='worker_attempt',
            span_id='s1', side_effect='mutating', status='ok', outputs={'wrote': 'a.py'},
        )
        traces.record_span(
            self.policy, 'job-200', name='step-2-test', kind='test_run',
            span_id='s2', parent_span_id='s1', side_effect='read_only', status='failed',
            outputs={'status': 'failed'},
        )
        live_calls = []
        replayed = traces.replay_trace(
            self.policy, 'job-200', 's2',
            live_runner=lambda sp: live_calls.append(sp['span_id']) or {'status': 'passed'},
        )
        self.assertEqual(live_calls, ['s2'])
        self.assertEqual(replayed['steps'][0]['action'], 'cached_skip_mutating')
        self.assertEqual(replayed['steps'][1]['action'], 're_ran_live')
        self.assertEqual(replayed['steps'][1]['outputs'], {'status': 'passed'})

    def test_trace_why_walks_back_to_earliest_root_cause_span_and_cli_works(self):
        # s1 (unit A) fails -> s2 (unit B, depends on s1) fails -> s3 (merge_queue, depends on s2) fails
        traces.record_span(
            self.policy, 'wave-300', name='unit:api', kind='unit',
            span_id='s1', status='failed', error='syntax error in api.py',
        )
        traces.record_span(
            self.policy, 'wave-300', name='unit:ui', kind='unit',
            span_id='s2', status='failed', depends_on=['s1'], error='blocked_by_dependency',
        )
        traces.record_span(
            self.policy, 'wave-300', name='merge_queue', kind='merge_queue',
            span_id='s3', status='failed', depends_on=['s2'], error='unit failed',
        )
        spans = traces.load_trace(self.policy, 'wave-300')
        why = traces.root_cause(spans)
        self.assertEqual(why['status'], 'failed')
        self.assertEqual(why['root_cause_span']['span_id'], 's1')
        self.assertEqual(why['chain'], ['s1', 's2', 's3'])
        self.assertIn('syntax error in api.py', why['explanation'])

        cfg = self.base / 'cfg.json'
        cfg.write_text(json.dumps({
            'worker_order': ['codex'],
            'unmetered_workers': ['codex'],
            'run_job': {'traces_dir': str(self.base / 'traces')},
        }))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['--config', str(cfg), 'trace', 'why', 'wave-300'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())['root_cause_span']['span_id'], 's1')


class TraceRunJobPersistenceTests(unittest.TestCase):
    """run_job must persist its JSON summary even when trace recording fails."""
    setUp = __import__('test_job_dispatch').JobTests.setUp
    tearDown = __import__('test_job_dispatch').JobTests.tearDown
    select = __import__('test_job_dispatch').JobTests.select
    worker = __import__('test_job_dispatch').JobTests.worker

    def job(self, **kw):
        from llm_router import job_dispatch as jd
        kw.setdefault('select', self.select)
        return jd.run_job(self.repo, self.task, policy=self.policy, **kw)

    def test_trace_write_failure_still_persists_job_json(self):
        from unittest.mock import patch
        from llm_router import job_dispatch as jd

        self.worker("open('out.txt','w').write('hi\\n')")
        with patch.object(jd.traces, 'record_span', side_effect=OSError('disk full')):
            result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        job_path = Path(self.policy['run_job']['jobs_dir']) / f"{result['job_id']}.json"
        self.assertTrue(job_path.is_file())
        saved = json.loads(job_path.read_text())
        self.assertEqual(saved['status'], 'committed')
        self.assertNotIn('trace_id', saved)
        self.assertTrue(any('trace not recorded' in w for w in saved.get('warnings', [])))


if __name__ == '__main__':
    unittest.main()
