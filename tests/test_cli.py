import contextlib
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from helpers import load_policy, TERM
from llm_router import cli, quota_service


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(argv)
    return code, out.getvalue()


class CliTests(unittest.TestCase):
    def test_paths_and_help(self):
        code, out = run(['paths'])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn('llm-router-tests-', data['state_dir'])
        for name in ('select', 'run-task', 'run-job', 'quota', 'status', 'scan', 'doctor', 'init'):
            with self.assertRaises(SystemExit) as done, contextlib.redirect_stdout(io.StringIO()):
                cli.main([name, '--help'])
            self.assertEqual(done.exception.code, 0)

    def test_init_never_overwrites(self):
        import os
        saved = os.environ['LLM_ROUTER_CONFIG_DIR']
        with tempfile.TemporaryDirectory() as tmp:
            os.environ['LLM_ROUTER_CONFIG_DIR'] = tmp
            try:
                code, out = run(['init', '--workers', 'codex', '--non-interactive'])
                first = json.loads(out)
                code, out = run(['init', '--workers', 'codex', '--non-interactive'])
                second = json.loads(out)
                self.assertEqual(json.loads((Path(tmp) / 'config.json').read_text())['worker_order'][0], 'codex')
            finally:
                os.environ['LLM_ROUTER_CONFIG_DIR'] = saved
        self.assertEqual(len(first['written']), 3)
        self.assertEqual(code, 2)
        self.assertIn('already exists', second['config_error'])

    def test_scan_and_bad_config(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            terms = Path(other) / 'terms.txt'
            terms.write_text(TERM + '\n')
            (Path(tmp) / 'f.txt').write_text('clean\n')
            code, out = run(['scan', tmp, '--terms', str(terms)])
            self.assertEqual((code, json.loads(out)['status']), (0, 'clean'))
            (Path(tmp) / 'g.txt').write_text(TERM + '\n')
            code, out = run(['scan', tmp, '--terms', str(terms)])
            self.assertEqual((code, json.loads(out)['status']), (1, 'blocked'))
            bad = Path(other) / 'bad.json'
            bad.write_text('{"worker_order": ["nope"]}')
            code, out = run(['--config', str(bad), 'paths'])
            self.assertEqual(code, 2)
            self.assertIn('unknown workers', out)

    def test_select_cached_only_without_meters_picks_nobody(self):
        code, out = run(['select', '--cached-only'])
        self.assertIsNone(json.loads(out)['worker'])


class RefreshHangTests(unittest.TestCase):
    def test_hung_observer_does_not_block_refresh(self):
        """Fix for the Cursor reader hang: the real (isolated) io path degrades to unknown + backoff."""
        with tempfile.TemporaryDirectory() as tmp:
            policy = load_policy(worker_order=['codex', 'cursor'], quota={
                'codex': {'observer': 'fake_observers:ok', 'hard_timeout_seconds': 20},
                'cursor': {'enabled': True, 'observer': 'fake_observers:hang', 'hard_timeout_seconds': 2}})
            policy['quota_refresh'].update(state_file=f'{tmp}/s.json', lock_file=f'{tmp}/l.lock')
            policy['alerts'] = {'file': f'{tmp}/a.jsonl', 'summary': f'{tmp}/A.md'}
            started = time.monotonic()
            report = quota_service.refresh(policy, once=True, now=datetime(2026, 10, 17, 6, 36, tzinfo=timezone.utc))
            self.assertLess(time.monotonic() - started, 15)
            self.assertEqual(report['workers']['codex']['action'], 'refreshed')
            self.assertEqual(report['workers']['cursor']['action'], 'failed')
            self.assertEqual(report['workers']['cursor']['reason'], 'hard_timeout')


class EntryPointTests(unittest.TestCase):
    def test_python_m_runs(self):
        done = subprocess.run([sys.executable, '-m', 'llm_router', '--version'], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0)
        self.assertTrue(done.stdout.startswith('llm-router '))


if __name__ == '__main__':
    unittest.main()
