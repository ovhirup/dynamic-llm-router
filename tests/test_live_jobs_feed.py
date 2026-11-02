"""Tests for live jobs feed schema and notch row logic (#99)."""
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from helpers import TERM, load_policy
from llm_router import cli, live_jobs_feed

NOW = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)


class LiveJobsFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.policy = load_policy(timezone='UTC')
        self.policy['run_job'].update(
            jobs_dir=str(self.base / 'jobs'),
            worktree_root=str(self.base / 'wt'),
        )
        self.policy['run_task']['output_dir'] = str(self.base / 'out')
        terms = self.base / 'terms.txt'
        terms.write_text(TERM + '\n')
        self.policy['run_task']['private_terms_file'] = str(terms)
        for d in ('jobs', 'wt', 'out'):
            (self.base / d).mkdir(parents=True, exist_ok=True)

    def test_in_flight_failover_preview_redaction_and_30m_recent_window(self):
        secret = 'sk-' + 'r' * 30
        # 1. In-flight job with failover claude -> codex and live output log
        in_flight_id = 'myrepo-20261101-175500'
        (self.base / 'wt' / in_flight_id).mkdir()
        (self.base / 'out' / '20261101-175500-a1-claude.log').write_text('attempt 1 quota hit\n')
        (self.base / 'out' / '20261101-175500-a2-codex.log').write_text(
            f'line 1\nline 2\nline 3 with {secret}\nline 4 with {TERM}\n'
        )

        # 2. Recently finished job (10 min ago -> in `recent`)
        recent_id = 'myrepo-20261101-174000'
        (self.base / 'jobs' / f'{recent_id}.json').write_text(json.dumps({
            'job_id': recent_id,
            'repo': '/tmp/myrepo',
            'task': 'fix-bug.md',
            'workers': ['claude'],
            'started_at': '2026-11-01T17:40:00+00:00',
            'finished_at': '2026-11-01T17:50:00+00:00',
            'ok': True,
            'status': 'committed',
            'branch': 'router/fix-bug',
            'commit': '1234567890abcdef',
        }))

        # 3. Older finished job (2 hours ago -> in `history`)
        old_id = 'myrepo-20261101-150000'
        (self.base / 'jobs' / f'{old_id}.json').write_text(json.dumps({
            'job_id': old_id,
            'repo': '/tmp/myrepo',
            'task': 'old.md',
            'workers': ['codex'],
            'started_at': '2026-11-01T15:00:00+00:00',
            'finished_at': '2026-11-01T15:10:00+00:00',
            'ok': False,
            'status': 'failed',
        }))

        feed = live_jobs_feed.build_live_jobs_feed(policy=self.policy, now=NOW)
        self.assertEqual([j['job_id'] for j in feed['running']], [in_flight_id])
        run_job = feed['running'][0]
        self.assertEqual(run_job['worker'], 'codex')
        self.assertEqual(run_job['workers'], ['claude', 'codex'])
        self.assertEqual(run_job['attempt'], 2)
        self.assertEqual(run_job['hops'], [{'from': 'claude', 'to': 'codex', 'attempt': 2}])
        self.assertEqual(len(run_job['preview']), 3)
        raw_preview = '\n'.join(run_job['preview'])
        self.assertNotIn(secret, raw_preview)
        self.assertNotIn(TERM, raw_preview)

        self.assertEqual([j['job_id'] for j in feed['recent']], [recent_id])
        self.assertEqual(feed['recent'][0]['state'], 'done')
        self.assertEqual(feed['recent'][0]['commit'], '1234567')

        self.assertEqual([j['job_id'] for j in feed['history']], [old_id])
        self.assertEqual(feed['history'][0]['state'], 'failed')

    def test_cli_jobs_live_feed(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['jobs', '--live-feed'])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        for key in ('running', 'recent', 'history'):
            self.assertIn(key, payload)


if __name__ == '__main__':
    unittest.main()
