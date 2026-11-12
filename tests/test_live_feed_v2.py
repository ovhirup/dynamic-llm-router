"""#288 slice 2: parallel-safe output stamps, feed v2 (timeline, now, gates) and `llm-router watch`."""
from datetime import datetime, timedelta, timezone
import io
import json
import re
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import test_job_dispatch as tjd
from helpers import TERM, load_policy
from llm_router import job_dispatch as jd, live_jobs_feed, pr_gate, task_dispatch as td, watch
from test_worker_events import replay
from test_task_dispatch import repo as make_repo


class ParallelStampTests(unittest.TestCase):
    """Done-check 1: jobs started in the same second never share output files or a lane."""

    def setUp(self):
        tjd.JobTests.setUp(self)

    def tearDown(self):
        self.tmp.cleanup()

    select = tjd.JobTests.select
    worker = tjd.JobTests.worker

    def job(self, repo, marker, **kw):
        self.worker(f"print('marker-{marker}'); open('out.txt','w').write('{marker}\\n')")
        return jd.run_job(repo, self.task, policy=self.policy, select=self.select, test_cmd='true', **kw)

    def outputs(self, stamp):
        return sorted(p.name for p in (self.base / 'out').glob(f'{stamp}-a*') if p.name.startswith(stamp + '-a'))

    def assert_own_lane(self, feed_job, marker, other):
        texts = ' '.join(event['text'] for event in feed_job['events'])
        self.assertIn(f'marker-{marker}', texts)
        self.assertNotIn(f'marker-{other}', texts)

    def test_same_second_jobs_in_one_repo_write_separate_outputs_and_lanes(self):
        fixed = td.now_local()
        first = self.job(self.repo, 'one', now=fixed)
        second = self.job(self.repo, 'two', now=fixed, branch='router/second')
        self.assertTrue(first['ok'] and second['ok'], (first, second))
        self.assertNotEqual(first['output_stamp'], second['output_stamp'])
        self.assertEqual(second['output_stamp'], first['output_stamp'] + '-2')
        mine, theirs = self.outputs(first['output_stamp']), self.outputs(second['output_stamp'])
        self.assertIn(f'{first["output_stamp"]}-a1-codex.log', mine)
        self.assertIn(f'{first["output_stamp"]}-a1-codex.events.jsonl', mine)
        self.assertIn(f'{second["output_stamp"]}-a1-codex.events.jsonl', theirs)
        self.assertFalse(set(mine) & set(theirs))
        feed = live_jobs_feed.build_live_jobs_feed(policy=self.policy)
        jobs = {job['job_id']: job for job in feed['recent']}
        self.assert_own_lane(jobs[first['job_id']], 'one', 'two')
        self.assert_own_lane(jobs[second['job_id']], 'two', 'one')

    def test_same_second_jobs_in_two_repos_get_distinct_output_stamps(self):
        # Worktree names differ by repo, so the worktree suffix alone never separated these.
        other = make_repo(self.base / 'other-repo')
        fixed = td.now_local()
        first = self.job(self.repo, 'one', now=fixed)
        second = self.job(other, 'two', now=fixed)
        self.assertTrue(first['ok'] and second['ok'], (first, second))
        self.assertFalse(second['job_id'].endswith('-2'))
        self.assertNotEqual(first['output_stamp'], second['output_stamp'])
        feed = live_jobs_feed.build_live_jobs_feed(policy=self.policy)
        jobs = {job['job_id']: job for job in feed['recent']}
        self.assert_own_lane(jobs[first['job_id']], 'one', 'two')
        self.assert_own_lane(jobs[second['job_id']], 'two', 'one')

    def test_output_stamp_reservation_is_exclusive(self):
        out = self.base / 'out'
        out.mkdir(parents=True, exist_ok=True)
        names = [jd.reserve_output_stamp(out, '20261112-120000', f'job-{n}') for n in range(3)]
        self.assertEqual(names, ['20261112-120000', '20261112-120000-2', '20261112-120000-3'])
        self.assertEqual(json.loads((out / '20261112-120000-2-job.json').read_text()), {'job_id': 'job-1'})
        # A stamp already used by older output files (from before reservations existed) is skipped too.
        (out / '20261112-130000-a1-codex.log').write_text('old\n')
        self.assertEqual(jd.reserve_output_stamp(out, '20261112-130000', 'job-new'), '20261112-130000-2')

    def test_suffixed_names_parse_and_unsuffixed_still_do(self):
        parse = live_jobs_feed.parse_output_log_name
        self.assertEqual(parse('20261112-120000-2-a3-codex.log')['stamp'], '20261112-120000-2')
        self.assertEqual(parse('20261112-120000-2-a3-codex.log')['attempt'], 3)
        self.assertEqual(parse('20261112-120000-a1-tests.log')['stamp'], '20261112-120000')
        self.assertTrue(parse('20261112-120000-a1-tests.log')['is_tests'])


NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)
SHA = 'a' * 40
OLD_SHA = 'b' * 40
SECRET = 'sk-' + 'w' * 32


def event_line(kind, text, ts):
    return json.dumps({'ts': ts, 'worker': 'x', 'stream': 'stdout', 'kind': kind, 'text': text}) + '\n'


class FeedFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.policy = load_policy(timezone='UTC')
        self.policy['run_job'].update(jobs_dir=str(self.base / 'jobs'), worktree_root=str(self.base / 'wt'))
        self.policy['run_task']['output_dir'] = str(self.base / 'out')
        terms = self.base / 'terms.txt'
        terms.write_text(TERM + '\n')
        self.policy['run_task']['private_terms_file'] = str(terms)
        self.policy['pr_gate'].update(gate_dir=str(self.base / 'gate'),
                                      approvals_file=str(self.base / 'approvals.jsonl'))
        for name in ('jobs', 'wt', 'out', 'gate'):
            (self.base / name).mkdir()
        self.out = self.base / 'out'

    def running(self, job_id, stamp=None):
        (self.base / 'wt' / job_id).mkdir()
        if stamp:
            (self.out / f'{stamp}-job.json').write_text(json.dumps({'job_id': job_id}))

    def events(self, stamp, attempt, worker, items, start=0):
        path = self.out / f'{stamp}-a{attempt}-{worker}.events.jsonl'
        with path.open('a') as handle:
            for offset, (kind, text) in enumerate(items):
                handle.write(event_line(kind, text, f'2026-11-12T17:{50 + attempt}:{start + offset:02d}.000+00:00'))

    def finished(self, job_id, minutes_ago=5, **fields):
        data = {'job_id': job_id, 'repo': '/tmp/demo', 'task': 'task.md', 'workers': ['codex'],
                'started_at': (NOW - timedelta(minutes=minutes_ago + 5)).isoformat(),
                'finished_at': (NOW - timedelta(minutes=minutes_ago)).isoformat(),
                'ok': True, 'status': 'committed', 'branch': 'router/task', 'commit': SHA,
                'tests_status': 'passed', 'leak_check': {'status': 'clean'}, 'review_required': False}
        data.update(fields)
        (self.base / 'jobs' / f'{job_id}.json').write_text(json.dumps(data))

    def approve(self, job_id, sha, action='push', hours=1, kind='approval'):
        record = {'type': kind, 'job': job_id, 'action': action, 'sha': sha,
                  'expires_at': (NOW + timedelta(hours=hours)).isoformat()}
        with (self.base / 'approvals.jsonl').open('a') as handle:
            handle.write(json.dumps(record) + '\n')

    def feed(self):
        return live_jobs_feed.build_live_jobs_feed(policy=self.policy, now=NOW)

    def find(self, feed, job_id):
        for section in ('running', 'recent', 'history'):
            for job in feed[section]:
                if job['job_id'] == job_id:
                    return job
        raise AssertionError(f'{job_id} not in feed')


class TimelineAndNowTests(FeedFixture):
    """Done-checks 2 and 3."""

    def test_timeline_spans_failover_attempts_oldest_first_and_is_capped(self):
        self.running('demo-20261112-175000', '20261112-175000')
        self.events('20261112-175000', 1, 'claude', [('message', f'claude step {n}') for n in range(15)])
        self.events('20261112-175000', 2, 'codex', [('command', f'codex step {n}') for n in range(14)] +
                    [('command', 'pytest -q')])
        job = self.find(self.feed(), 'demo-20261112-175000')
        self.assertEqual(len(job['events']), 20)
        self.assertEqual(job['events'][0], {'ts': '2026-11-12T17:51:10.000+00:00', 'worker': 'claude',
                                            'attempt': 1, 'kind': 'message', 'text': 'claude step 10'})
        self.assertEqual([e['attempt'] for e in job['events']], [1] * 5 + [2] * 15)
        self.assertEqual(job['now'], 'running pytest -q')

    def test_now_line_verbs_tests_phase_and_clip(self):
        line = live_jobs_feed.now_line
        self.assertIsNone(line([], 'running'))
        self.assertEqual(line([{'kind': 'edit', 'text': 'src/app.py'}], 'running'), 'editing src/app.py')
        self.assertEqual(line([{'kind': 'message', 'text': 'hi'}], 'running'), 'says hi')
        self.assertEqual(line([{'kind': 'error', 'text': 'boom'}], 'running'), 'error: boom')
        self.assertEqual(line([{'kind': 'done', 'text': 'finished in 3s'}], 'running'), 'finished')
        self.assertEqual(line([{'kind': 'raw', 'text': 'x'}], 'running'), 'output: x')
        self.assertEqual(line([{'kind': 'edit', 'text': 'a.py'}], 'testing'), 'running tests')
        self.assertEqual(len(line([{'kind': 'message', 'text': 'y' * 500}], 'running')), 120)

    def test_tests_phase_shows_running_tests(self):
        self.running('demo-20261112-175500', '20261112-175500')
        self.events('20261112-175500', 1, 'codex', [('edit', 'src/app.py')])
        tests_log = self.out / '20261112-175500-a1-tests.log'
        tests_log.write_text('running\n')
        os.utime(tests_log, (NOW.timestamp(), NOW.timestamp()))
        job = self.find(self.feed(), 'demo-20261112-175500')
        self.assertEqual(job['state'], 'testing')
        self.assertEqual(job['now'], 'running tests')
        self.assertEqual(job['gates']['tests']['state'], 'running')

    def test_a_running_job_names_its_worker_from_live_events_before_any_log(self):
        # Seen in the live demo: a forced worker records no allocation, and its .log lands only at exit.
        self.running('demo-20261112-175100', '20261112-175100')
        self.events('20261112-175100', 1, 'cursor', [('command', 'ls')])
        job = self.find(self.feed(), 'demo-20261112-175100')
        self.assertEqual((job['worker'], job['workers']), ('cursor', ['cursor']))

    def test_jobs_without_events_and_history_jobs(self):
        self.running('demo-20261112-175900')
        self.finished('demo-20261112-120000', minutes_ago=300)
        feed = self.feed()
        running = self.find(feed, 'demo-20261112-175900')
        self.assertEqual((running['events'], running['now']), ([], None))
        history = self.find(feed, 'demo-20261112-120000')
        self.assertIn(history, feed['history'])
        self.assertNotIn('events', history)


class GateTests(FeedFixture):
    """Done-check 4: gate status per job, read-only."""

    def gates(self, job_id):
        return self.find(self.feed(), job_id)['gates']

    def test_running_job_gates_wait_for_the_result(self):
        self.running('demo-20261112-175000')
        gates = self.gates('demo-20261112-175000')
        self.assertEqual({k: v['state'] for k, v in gates.items()},
                         {'tests': 'pending', 'leak_check': 'pending', 'review': 'pending', 'approval': 'pending'})

    def test_leak_check_blocked(self):
        self.finished('demo-20261112-174000', ok=False, status='blocked_by_leak_check', commit=None,
                      blocked_by_leak_check=True, leak_check={'status': 'findings'})
        gates = self.gates('demo-20261112-174000')
        self.assertEqual(gates['leak_check'], {'state': 'blocked', 'detail': 'leak check: findings'})
        self.assertEqual(gates['approval']['state'], 'n/a')

    def test_review_pending_blocks_approval(self):
        self.finished('demo-20261112-174100', review_required=True,
                      review={'status': 'pending', 'reviewer': 'grok-bot',
                              'next': 'record the verdict: jobs --job demo-20261112-174100 --verdict approve|reject'})
        gates = self.gates('demo-20261112-174100')
        self.assertEqual(gates['review']['state'], 'waiting')
        self.assertIn('--verdict approve|reject', gates['review']['detail'])
        self.assertEqual(gates['approval'], {'state': 'blocked', 'detail': 'stronger-model review must approve first'})

    def test_approval_waiting_shows_the_full_sha_and_command(self):
        self.finished('demo-20261112-174200')
        approval = self.gates('demo-20261112-174200')['approval']
        self.assertEqual(approval['state'], 'waiting')
        self.assertEqual(approval['detail'], f'push approval needed for {SHA}')
        self.assertEqual(approval['command'], f'llm-router approve demo-20261112-174200 --action push --sha {SHA}')
        gates = self.gates('demo-20261112-174200')
        self.assertEqual((gates['tests']['state'], gates['leak_check']['state'], gates['review']['state']),
                         ('passed', 'passed', 'n/a'))

    def test_approved_merge_step_and_merged(self):
        self.finished('demo-20261112-174300')
        self.approve('demo-20261112-174300', SHA)
        self.assertEqual(self.gates('demo-20261112-174300')['approval']['state'], 'passed')
        (self.base / 'gate' / 'demo-20261112-174300.json').write_text(json.dumps(
            {'review': {'sha': SHA}, 'pr': {'pushed_sha': SHA}}))
        approval = self.gates('demo-20261112-174300')['approval']
        self.assertEqual((approval['state'], approval['action']), ('waiting', 'merge'))
        (self.base / 'gate' / 'demo-20261112-174300.json').write_text(json.dumps(
            {'review': {'sha': SHA}, 'pr': {'pushed_sha': SHA}, 'merged': {'sha': SHA}}))
        self.assertEqual(self.gates('demo-20261112-174300')['approval']['state'], 'passed')

    def test_approval_for_an_older_sha_is_stale(self):
        self.finished('demo-20261112-174400')
        self.approve('demo-20261112-174400', OLD_SHA)
        approval = self.gates('demo-20261112-174400')['approval']
        self.assertEqual(approval['state'], 'waiting')
        self.assertIn('older commit', approval['reason'])

    def test_the_reviewed_sha_wins_over_the_job_commit(self):
        self.finished('demo-20261112-174500')
        (self.base / 'gate' / 'demo-20261112-174500.json').write_text(json.dumps({'review': {'sha': OLD_SHA}}))
        self.assertEqual(self.gates('demo-20261112-174500')['approval']['sha'], OLD_SHA)

    def test_building_the_feed_writes_nothing(self):
        self.running('demo-20261112-175000', '20261112-175000')
        self.events('20261112-175000', 1, 'codex', [('command', 'ls')])
        self.finished('demo-20261112-174200')
        self.approve('demo-20261112-174200', OLD_SHA)
        (self.base / 'gate' / 'demo-20261112-174200.json').write_text(json.dumps({'review': {'sha': SHA}}))

        def snapshot():
            return {str(p): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                    for p in sorted(self.base.rglob('*'))}

        before = snapshot()
        self.feed()
        self.assertEqual(snapshot(), before)


class RedactionAndScaleTests(FeedFixture):
    """Done-check 5."""

    def test_planted_term_and_secret_never_reach_the_feed(self):
        self.running(f'demo-20261112-175000', '20261112-175000')
        self.events('20261112-175000', 1, 'codex', [('message', f'{TERM} {SECRET}'), ('command', f'echo {SECRET}')])
        self.finished('demo-20261112-174000', task=f'{TERM}-task.md', review_required=True,
                      review={'status': 'error', 'reviewer': 'grok-bot', 'reason': f'{TERM} {SECRET}'})
        body = json.dumps(self.feed())
        self.assertNotIn(SECRET, body)
        self.assertNotIn(TERM.lower(), body.lower())

    def test_existing_keys_are_unchanged_and_new_keys_are_additive(self):
        self.running('demo-20261112-175000')
        self.finished('demo-20261112-174000')
        feed = self.feed()
        old = {'job_id', 'repo', 'task', 'worker', 'workers', 'hops', 'attempt', 'started_at', 'finished_at',
               'elapsed_seconds', 'state', 'status', 'ok', 'branch', 'commit', 'log_path', 'preview'}
        for job in feed['running'] + feed['recent']:
            self.assertTrue(old <= set(job))
            self.assertEqual(set(job) - old - {'allocation', 'allocation_history'}, {'events', 'now', 'gates'})
        self.assertEqual(self.find(feed, 'demo-20261112-174000')['commit'], SHA[:7])

    def test_200_history_and_10_running_jobs_build_fast_and_read_approvals_once(self):
        for n in range(200):
            self.finished(f'demo-20261101-{n // 60:02d}{n % 60:02d}00', minutes_ago=600 + n)
            self.approve(f'demo-20261101-{n // 60:02d}{n % 60:02d}00', OLD_SHA)
        for n in range(10):
            stamp = f'20261112-1750{n:02d}'
            self.running(f'demo-{stamp}', stamp)
            self.events(stamp, 1, 'codex', [('command', f'step {k}') for k in range(30)])
        calls = []
        original = pr_gate.approval_records
        with mock.patch.object(pr_gate, 'approval_records', side_effect=lambda p: calls.append(1) or original(p)):
            started = time.monotonic()
            feed = self.feed()
            elapsed = time.monotonic() - started
        self.assertEqual((len(feed['history']), len(feed['running'])), (200, 10))
        self.assertEqual(len(calls), 1)
        self.assertLess(elapsed, 1.0)


GOLDEN = Path(__file__).resolve().parent / 'fixtures' / 'watch'
PUSH_SHA = '5659e86f2fdbe4dcb82ee716be3c1ad909f0a552'


def recorded_events(worker, name):
    """The slice 1 recording of one worker, as feed timeline events (its final `done` dropped: still running)."""
    with tempfile.TemporaryDirectory() as tmp:
        records = replay(worker, name, Path(tmp) / 'events.jsonl')
    return [{'ts': r['ts'], 'worker': worker, 'attempt': 1, 'kind': r['kind'], 'text': r['text']}
            for r in records][:-1]


def demo_feed():
    """Three agents running in parallel plus one finished job waiting for a push approval."""
    running = []
    for n, worker in enumerate(('codex', 'cursor', 'claude')):
        events = recorded_events(worker, worker)[-live_jobs_feed.TIMELINE_EVENTS:]
        running.append({'job_id': f'demo-20261112-1755{n:02d}', 'repo': 'demo', 'task': 'add-hello.md',
                        'worker': worker, 'workers': [worker], 'elapsed_seconds': 65 + 40 * n, 'state': 'running',
                        'events': events, 'now': live_jobs_feed.now_line(events, 'running'),
                        'gates': live_jobs_feed.running_gates('running')})
    waiting = {'job_id': 'demo-20261112-174000', 'repo': 'demo', 'task': 'add-hello.md', 'worker': 'codex',
               'workers': ['codex'], 'state': 'done', 'status': 'committed',
               'gates': {'tests': {'state': 'passed', 'detail': 'tests passed'},
                         'leak_check': {'state': 'passed', 'detail': 'leak check clean'},
                         'review': {'state': 'n/a', 'detail': 'not required'},
                         'approval': {'state': 'waiting', 'detail': f'push approval needed for {PUSH_SHA}',
                                      'sha': PUSH_SHA, 'action': 'push',
                                      'command': f'llm-router approve demo-20261112-174000 --action push '
                                                 f'--sha {PUSH_SHA}'}}}
    return {'updated_at': '2026-11-12T18:00:00+00:00', 'running': running, 'recent': [waiting], 'history': []}


class WatchTests(FeedFixture):
    """Done-check 6: `llm-router watch`."""

    def test_golden_frames_at_80_and_160_columns(self):
        for width in (80, 160):
            with self.subTest(width=width):
                frame = watch.render(demo_feed(), width)
                self.assertEqual(frame + '\n', (GOLDEN / f'frame-{width}.txt').read_text())

    def test_lanes_sit_side_by_side_from_100_columns_and_stack_below(self):
        self.assertIn('│', watch.render(demo_feed(), 100))
        self.assertNotIn('│', watch.render(demo_feed(), 99))

    def test_only_the_approval_lines_may_exceed_the_width(self):
        for width in (80, 100, 160):
            frame = watch.render(demo_feed(), width)
            long = [line for line in frame.splitlines() if len(line) > width]
            self.assertTrue(all(PUSH_SHA in line for line in long), long)
            self.assertIn(f'push approval needed for {PUSH_SHA}', frame)
            self.assertIn(f'--sha {PUSH_SHA}', frame)

    def test_color_only_on_a_terminal(self):
        self.assertNotIn('\x1b', watch.render(demo_feed(), 160))
        self.assertIn('\x1b[32m✓\x1b[0m', watch.render(demo_feed(), 160, color=True))
        stream = io.StringIO()
        self.assertEqual(watch.run(policy=self.policy, once=True, stream=stream, width=120), 0)
        self.assertNotIn('\x1b', stream.getvalue())

    def test_once_output_holds_no_planted_secret_or_term(self):
        self.running('demo-20261112-175000', '20261112-175000')
        self.events('20261112-175000', 1, 'codex', [('message', f'{TERM} {SECRET}'), ('command', f'echo {SECRET}')])
        self.finished('demo-20261112-174000', task=f'{TERM}-task.md')
        for as_json in (False, True):
            stream = io.StringIO()
            watch.run(policy=self.policy, once=True, as_json=as_json, stream=stream, width=120,
                      build=lambda policy: live_jobs_feed.build_live_jobs_feed(policy=policy, now=NOW))
            self.assertIn('demo', stream.getvalue())
            self.assertNotIn(SECRET, stream.getvalue())
            self.assertNotIn(TERM.lower(), stream.getvalue().lower())

    def test_terminal_escapes_from_worker_output_never_reach_the_terminal(self):
        # From the security review: worker output is untrusted. OSC 52 would write the clipboard, OSC 0 the
        # window title; CSI sequences could hide or rewrite lines.
        nasty = ('\x1b]52;c;ZWNobyBwd25lZA==\x07 clip \x1b]0;owned\x1b\\ title \x1b[2J\x1b[31mred\x1b[0m '
                 '\x9b31m c1 \x08\x07 bell')
        feed = demo_feed()
        job = feed['running'][0]
        job['events'] = job['events'] + [{'ts': '', 'worker': 'codex', 'attempt': 1, 'kind': 'message', 'text': nasty}]
        job.update(now=nasty, repo=nasty)
        feed['recent'][0]['task'] = nasty
        feed['recent'][0]['gates']['approval']['detail'] = nasty
        for width in (80, 160):
            frame = watch.render(feed, width)
            self.assertNotIn('\x1b', frame)
            self.assertFalse([ch for ch in frame if ord(ch) < 32 and ch != '\n' or 0x7f <= ord(ch) <= 0x9f], frame)
            self.assertIn('clip', frame)
        colored = watch.render(feed, 160, color=True)
        self.assertEqual(set(re.findall(r'\x1b\[[0-9;]*m', colored)), {'\x1b[32m', '\x1b[33m', '\x1b[0m'})

    def test_json_and_job_filter(self):
        self.running('demo-20261112-175000')
        self.finished('demo-20261112-120000', minutes_ago=300)
        stream = io.StringIO()
        watch.run(policy=self.policy, as_json=True, job_id='demo-20261112-120000', stream=stream)
        feed = json.loads(stream.getvalue())
        self.assertEqual(([j['job_id'] for j in feed['recent']], feed['running'], feed['history']),
                         (['demo-20261112-120000'], [], []))

    def test_ctrl_c_exits_zero_without_a_traceback(self):
        def interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt
        stream = io.StringIO()
        self.assertEqual(watch.run(policy=self.policy, stream=stream, sleep=interrupt, width=120), 0)
        self.assertIn('DLR watch', stream.getvalue())
        self.assertEqual(watch.run(policy=self.policy, stream=io.StringIO(), build=interrupt), 0)

    def test_watch_never_writes_state(self):
        self.running('demo-20261112-175000', '20261112-175000')
        self.finished('demo-20261112-174200')
        before = {str(p): p.stat().st_mtime_ns for p in self.base.rglob('*')}
        frames = []
        watch.run(policy=self.policy, stream=io.StringIO(), width=120,
                  sleep=lambda _s: frames.append(1) if len(frames) < 2 else (_ for _ in ()).throw(KeyboardInterrupt))
        self.assertEqual({str(p): p.stat().st_mtime_ns for p in self.base.rglob('*')}, before)

    def test_cli_watch_once_subprocess(self):
        state = self.base / 'state'
        (state / 'worktrees' / 'demo-20261112-175000').mkdir(parents=True)
        policy = self.base / 'policy.json'
        policy.write_text('{}')
        env = dict(os.environ, PYTHONPATH=str(Path(live_jobs_feed.__file__).resolve().parents[1]),
                   LLM_ROUTER_CONFIG=str(policy), LLM_ROUTER_CONFIG_DIR=str(self.base / 'config'),
                   LLM_ROUTER_STATE_DIR=str(state))
        done = subprocess.run([sys.executable, '-m', 'llm_router', 'watch', '--once'], capture_output=True,
                              text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('1 running', done.stdout)
        self.assertIn('demo-20261112-175000'.split('-')[0], done.stdout)
        self.assertEqual(done.stderr, '')


if __name__ == '__main__':
    unittest.main()
