"""Live worker events (#288 slice 1): capture, normalizer, redaction, Codex --json, feed hook, fixtures."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from helpers import TERM, load_policy
from llm_router import eval_corpus, live_jobs_feed, task_dispatch as td, worker_events as we

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'worker_streams'
RECORDED = {'codex': 'codex', 'cursor': 'cursor', 'claude': 'claude', 'grok-build': 'grok'}
SECRET = 'sk-' + 'q' * 32


def fixture_lines(name):
    return (FIXTURES / f'{name}.jsonl').read_text().splitlines()


def replay(worker, name, path, **kwargs):
    log = we.EventLog(path, worker, cwd='/tmp/fixture-repo', **kwargs)
    for line in fixture_lines(name):
        log.feed('stdout', line + '\n')
    log.close()
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def git_repo(path):
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    (path / 'README.md').write_text('hello\n')
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(path), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init'],
                   check=True)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)


class LiveCaptureTests(Base):
    """Done-check 1: lines reach the events file while the worker is still running."""

    def test_first_event_is_on_disk_before_the_worker_exits(self):
        path = self.base / 'out' / '20261112-120000-a1-codex.events.jsonl'
        log = we.EventLog(path, 'codex')
        script = ('import sys, time\n'
                  'print(\'{"type":"item.completed","item":{"type":"agent_message","text":"working"}}\', flush=True)\n'
                  'time.sleep(3)\n'
                  'print(\'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\', flush=True)\n')
        result = {}

        def run():
            token = td.LINE_SINK.set(log.feed)
            try:
                result.update(td.run_bounded([sys.executable, '-c', script], self.base, 30))
            finally:
                td.LINE_SINK.reset(token)

        worker = threading.Thread(target=run)
        worker.start()
        seen_while_running = None
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            if path.exists() and path.read_text().strip():
                seen_while_running = worker.is_alive()
                break
            time.sleep(0.05)
        worker.join(30)
        log.close()
        self.assertTrue(seen_while_running, 'the first event must be written before the worker exits')
        kinds = [json.loads(line)['kind'] for line in path.read_text().splitlines()]
        self.assertEqual(kinds, ['message', 'done'])
        self.assertIn('turn.completed', result['stdout'])  # captured output is unchanged

    def test_a_failing_sink_never_breaks_the_run(self):
        def broken(stream, line):
            raise RuntimeError('sink exploded')
        token = td.LINE_SINK.set(broken)
        try:
            result = td.run_bounded([sys.executable, '-c', 'print("a"); print("b")'], self.base, 30)
        finally:
            td.LINE_SINK.reset(token)
        self.assertEqual(result['returncode'], 0)
        self.assertEqual(result['stdout'], 'a\nb\n')

    def test_no_sink_outside_a_worker_invocation(self):
        self.assertIsNone(td.LINE_SINK.get())


class NormalizerTests(unittest.TestCase):
    """Done-check 2: the normalizer handles every recorded worker and never raises."""

    def test_unknown_and_malformed_lines_become_raw(self):
        for worker in ('codex', 'cursor', 'claude', 'grok-build', 'someone-else'):
            with self.subTest(worker=worker):
                self.assertEqual([e['kind'] for e in we.parse(worker, 'plain progress text')], ['raw'])
                self.assertEqual([e['kind'] for e in we.parse(worker, '{"type": "brand_new_event"}')], ['raw'])
                self.assertEqual(we.parse(worker, '{"type": "brand_new_event"}')[0]['text'], 'brand_new_event')
                self.assertEqual([e['kind'] for e in we.parse(worker, '{"type": "item.completed", "item": ')],
                                 ['raw'])
                self.assertEqual([e['kind'] for e in we.parse(worker, '[1, 2, 3]')], ['raw'])
                self.assertEqual(we.parse(worker, '   \n'), [])

    def test_fuzz_never_raises_and_only_emits_known_kinds(self):
        rng = random.Random(288)
        samples = [line for name in RECORDED.values() for line in fixture_lines(name)]
        for _ in range(3000):
            choice = rng.random()
            if choice < 0.3:
                line = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 200)))
            elif choice < 0.6:
                source = rng.choice(samples)
                line = source[:rng.randrange(len(source))]
            elif choice < 0.8:
                event = json.loads(rng.choice(samples))
                for key in list(event)[:rng.randrange(1, 4)]:
                    event[key] = rng.choice([None, 7, [], {}, 'x' * 5000, {'nested': [None]}, True])
                line = json.dumps(event)
            else:
                line = '{"type":"assistant","message":{"content":"' + 'z' * rng.randrange(10, 200_000) + '"}}'
            worker = rng.choice(['codex', 'cursor', 'claude', 'grok-build', 'nobody'])
            events = we.parse(worker, line, rng.choice(['stdout', 'stderr']))
            self.assertIsInstance(events, list)
            for event in events:
                self.assertIn(event['kind'], we.KINDS)
                self.assertEqual(set(event) - {'partial'}, {'worker', 'stream', 'kind', 'text'})
                written = we.redact_text(event['text'])
                self.assertLessEqual(len(written), we.TEXT_LIMIT)

    def test_shell_wrappers_are_unwrapped_and_test_runs_are_tagged(self):
        line = json.dumps({'type': 'item.started', 'item': {
            'type': 'command_execution', 'command': "/bin/zsh -lc 'cd tests && python -m unittest discover'"}})
        self.assertEqual(we.parse('codex', line),
                         [{'worker': 'codex', 'stream': 'stdout', 'kind': 'test',
                           'text': 'cd tests && python -m unittest discover'}])

    def test_thinking_is_never_shown(self):
        self.assertEqual(we.parse('cursor', '{"type":"thinking","subtype":"delta","text":"private musing"}'), [])
        self.assertEqual(we.parse('grok-build', '{"type":"thought","data":"private musing"}'), [])
        self.assertEqual(we.parse('codex', '{"type":"item.completed","item":{"type":"reasoning","text":"x"}}'), [])

    def test_codex_fatal_errors_and_warnings(self):
        failed = json.dumps({'type': 'turn.failed', 'error': {'message': 'stream disconnected'}})
        self.assertEqual(we.parse('codex', failed)[0]['kind'], 'error')
        failing_command = json.dumps({'type': 'item.completed', 'item': {
            'type': 'command_execution', 'command': 'false', 'exit_code': 1}})
        self.assertEqual(we.parse('codex', failing_command)[0]['text'], 'exit 1: false')


class RedactionTests(Base):
    """Done-check 3: nothing secret or private reaches the events file; the file is 0600."""

    def test_secrets_and_private_terms_never_reach_the_file(self):
        path = self.base / 'out' / 'events.jsonl'
        log = we.EventLog(path, 'grok-build', private_terms=[TERM])
        half = len(SECRET) // 2
        for line in (
            json.dumps({'type': 'text', 'data': f'about {TERM[:4]}'}),  # the term is split across chunks
            json.dumps({'type': 'text', 'data': f'{TERM[4:]} and {SECRET[:half]}'}),
            json.dumps({'type': 'text', 'data': SECRET[half:] + ' done'}),
            json.dumps({'type': 'tool_call', 'kind': 'execute',
                        'rawInput': {'command': f'echo {SECRET} {TERM.upper()}'}}),
            f'stderr noise mentioning {TERM.lower()} and {SECRET}',
            json.dumps({'type': 'end', 'stopReason': 'end_turn'}),
        ):
            log.feed('stdout', line + '\n')
        log.close()
        body = path.read_text()
        self.assertNotIn(TERM.lower(), body.lower())
        self.assertNotIn(SECRET, body)
        self.assertNotIn(SECRET[:half], body)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        events = [json.loads(line) for line in body.splitlines()]
        self.assertEqual([e['kind'] for e in events], ['message', 'command', 'raw', 'done'])
        self.assertTrue(all(len(e['text']) <= we.TEXT_LIMIT for e in events))

    def test_no_cut_before_redaction_in_raw_lines(self):
        # Redaction shrinks the many full secrets, so whatever sat past an early cut point would become visible.
        path = self.base / 'events.jsonl'
        log = we.EventLog(path, 'codex')
        # A 400-char pre-cut would land 13 chars into the 11th secret: 'sk-' + 10 chars, too short to match the
        # token pattern, and it would sit inside the 200 shown chars once the first 10 secrets are redacted.
        log.feed('stderr', 'p' * 25 + ' ' + (SECRET + ' ') * 12 + '\n')
        log.close()
        self.assertNotIn('sk-qqqq', path.read_text())

    def test_secret_split_across_a_long_streamed_run_is_redacted_whole(self):
        path = self.base / 'events.jsonl'
        log = we.EventLog(path, 'grok-build')
        half = len(SECRET) // 2
        log.feed('stdout', json.dumps({'type': 'text', 'data': 'x ' * 1995 + SECRET[:half]}) + '\n')
        log.feed('stdout', json.dumps({'type': 'text', 'data': SECRET[half:] + ' tail'}) + '\n')
        log.feed('stdout', json.dumps({'type': 'end', 'stopReason': 'end_turn'}) + '\n')
        log.close()
        body = path.read_text()
        self.assertNotIn(SECRET[half:], body)
        self.assertEqual([json.loads(line)['kind'] for line in body.splitlines()], ['message', 'done'])

    def test_long_quoted_credential_in_a_raw_line_is_redacted(self):
        # From review: clipping first would drop the closing quote that QUOTED_SECRET needs.
        value = 'v' * 500
        path = self.base / 'events.jsonl'
        log = we.EventLog(path, 'codex')
        log.feed('stderr', f'password: "{value}"\n')
        log.close()
        self.assertNotIn('vvvv', path.read_text())

    def test_stderr_between_stdout_chunks_does_not_split_the_redaction(self):
        # From review: a stderr line must not flush a half-finished stdout run.
        half = len(SECRET) // 2
        path = self.base / 'events.jsonl'
        log = we.EventLog(path, 'grok-build', private_terms=[TERM])
        log.feed('stdout', json.dumps({'type': 'text', 'data': f'key {SECRET[:half]}'}) + '\n')
        log.feed('stderr', 'warning: retrying\n')
        log.feed('stdout', json.dumps({'type': 'text', 'data': f'{SECRET[half:]} for {TERM[:3]}'}) + '\n')
        log.feed('stderr', 'warning: retrying again\n')
        log.feed('stdout', json.dumps({'type': 'text', 'data': f'{TERM[3:]} ok'}) + '\n')
        log.feed('stdout', json.dumps({'type': 'end', 'stopReason': 'end_turn'}) + '\n')
        log.close()
        body = path.read_text()
        self.assertNotIn(SECRET[:half], body)
        self.assertNotIn(SECRET[half:], body)
        self.assertNotIn(TERM[:3] + '"', body)
        self.assertNotIn(TERM[3:], body)
        events = [json.loads(line) for line in body.splitlines()]
        self.assertEqual([(e['stream'], e['kind']) for e in events],
                         [('stderr', 'raw'), ('stderr', 'raw'), ('stdout', 'message'), ('stdout', 'done')])
        self.assertEqual(events[2]['text'], 'key [REDACTED] for [REDACTED] ok')

    def test_an_overflowing_streamed_run_never_shows_a_cut_secret(self):
        # From review (REVISE 1.1): cutting the run at the cap left 'sk-' + 7 chars, too short to match the
        # token pattern; whitespace collapse and the shrinking password then pulled it into the shown text.
        prefix = 'password: "' + 'v' * 64 + '" '
        path = self.base / 'events.jsonl'
        log = we.EventLog(path, 'grok-build')
        log.feed('stdout', json.dumps({'type': 'text', 'data': ' ' * (we._PARTIAL_CAP - len(prefix) - 10)}) + '\n')
        log.feed('stdout', json.dumps({'type': 'text', 'data': prefix + SECRET + ' more'}) + '\n')
        log.feed('stdout', json.dumps({'type': 'text', 'data': ' and ' + SECRET}) + '\n')
        log.feed('stdout', json.dumps({'type': 'end', 'stopReason': 'end_turn'}) + '\n')
        log.close()
        body = path.read_text()
        self.assertNotIn('sk-q', body)
        self.assertNotIn('vvvv', body)
        self.assertEqual([json.loads(line)['kind'] for line in body.splitlines()], ['message', 'done'])

    def test_a_write_error_during_close_never_raises(self):
        log = we.EventLog(self.base / 'events.jsonl', 'grok-build')
        log.feed('stdout', json.dumps({'type': 'text', 'data': 'pending text'}) + '\n')
        log._handle.close()  # the final flush in close() now hits a closed file
        log.close()
        log.feed('stdout', 'after close\n')  # and the log stays disabled, silently

    def test_existing_file_is_tightened_to_0600(self):
        path = self.base / 'events.jsonl'
        path.write_text('')
        os.chmod(path, 0o644)
        we.EventLog(path, 'codex').close()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class RunTaskTests(Base):
    """Done-checks 1, 3, 4 and 5 through run_task."""

    STAMP = '20261112-120000-a1'

    def setUp(self):
        super().setUp()
        self.repo = git_repo(self.base / 'target')
        self.task = self.base / 'task.md'
        self.task.write_text('Append a line to README.md.\n')
        self.out = self.base / 'out'
        self.policy = copy.deepcopy(load_policy())
        self.policy['run_task']['ledger'] = str(self.base / 'ledger.csv')
        terms = self.base / 'terms.txt'
        terms.write_text(TERM + '\n')
        self.policy['run_task']['private_terms_file'] = str(terms)

    def run_codex(self, stdout_lines, returncode=0, stderr='', edit=True):
        def runner(argv, cwd, timeout, grace, **kw):
            sink = td.LINE_SINK.get()
            for line in stdout_lines:
                if sink:
                    sink('stdout', line + '\n')
            if edit:
                (Path(cwd) / 'README.md').write_text('hello\nworld\n')
            return {'returncode': returncode, 'timed_out': False, 'stdout': '\n'.join(stdout_lines) + '\n',
                    'stderr': stderr, 'elapsed_seconds': 0.1}
        select = lambda chars, **kw: {'eligible': ['codex'], 'meters': {'codex': 10},  # noqa: E731
                                      'reason': 'preferred_eligible_worker'}
        return td.run_task(self.task, self.repo, policy=self.policy, output_root=self.out, select=select,
                           runner=runner, stamp=self.STAMP)

    def test_events_file_is_written_next_to_the_log_not_in_the_checkout(self):
        result = self.run_codex(fixture_lines('codex'))
        self.assertTrue(result['ok'])
        path = self.out / f'{self.STAMP}-codex.events.jsonl'
        self.assertTrue(path.is_file())
        self.assertFalse(path.resolve().is_relative_to(self.repo.resolve()))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        kinds = [json.loads(line)['kind'] for line in path.read_text().splitlines()]
        self.assertIn('message', kinds)
        self.assertEqual(kinds[-1], 'done')
        self.assertIsNone(td.LINE_SINK.get())

    def test_no_events_file_when_the_output_dir_is_inside_the_checkout(self):
        # From review (REVISE 1.2): a configured output dir inside the checkout must not receive events,
        # including when it is reached through a symlink.
        for via_link in (False, True):
            self.repo = git_repo(self.base / f'target-{via_link}')
            root = self.repo / 'logs'
            if via_link:
                link = self.base / 'outlink'
                link.symlink_to(self.repo)
                root = link / 'logs'
            with self.subTest(root=str(root)):
                self.out = root
                result = self.run_codex(fixture_lines('codex'))
                self.assertTrue(result['ok'])
                self.assertEqual(list(root.glob('*.events.jsonl')), [])
                self.assertTrue(any('live events off' in w for w in result['warnings']), result['warnings'])

    def test_quoted_secret_inside_codex_json_is_redacted_in_log_and_tail(self):
        # From review (REVISE 1.3): JSON escapes the quotes (\"...\"), so the plain quoted-secret rule missed it.
        value = 'v' * 64
        lines = [json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message',
                                                                'text': f'password: "{value}"'}}),
                 json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution',
                                                                'command': 'env', 'exit_code': 0,
                                                                'aggregated_output': f'DB_PASSWORD="{value}"\n'}}),
                 json.dumps({'type': 'turn.failed', 'error': {'message': 'stopped'}})]
        result = self.run_codex(lines, returncode=1, edit=False)
        attempt = result['attempts'][0]
        self.assertFalse(attempt['ok'])
        self.assertNotIn('vvvv', (self.out / f'{self.STAMP}-codex.log').read_text())
        self.assertIn('[REDACTED]', attempt['stderr_tail'])
        self.assertNotIn('vvvv', json.dumps(result))

    def test_json_lines_without_secrets_stay_byte_identical_in_the_log(self):
        lines = fixture_lines('codex')
        self.run_codex(lines)
        log = (self.out / f'{self.STAMP}-codex.log').read_text()
        self.assertIn('\n'.join(lines), log)

    def test_final_log_format_is_unchanged(self):
        self.run_codex(['plain line'])
        log = (self.out / f'{self.STAMP}-codex.log').read_text()
        self.assertEqual(log, '$ exit=0 timed_out=False elapsed=0.1s\n--- stdout\nplain line\n\n--- stderr\n\n')

    def test_codex_failure_reason_comes_from_the_fatal_json_event(self):
        lines = [json.dumps({'type': 'item.completed', 'item': {'type': 'error', 'message': 'plugin config warning'}}),
                 json.dumps({'type': 'turn.failed', 'error': {'message': f'model overloaded for {TERM}'}})]
        result = self.run_codex(lines, returncode=1, stderr='Reading additional input from stdin...', edit=False)
        attempt = result['attempts'][0]
        self.assertFalse(attempt['ok'])
        self.assertEqual(attempt['failure_reason'], 'model overloaded for [REDACTED]')

    def test_codex_item_warnings_alone_fall_back_to_the_old_reason(self):
        lines = [json.dumps({'type': 'item.completed', 'item': {'type': 'error', 'message': 'plugin config warning'}})]
        result = self.run_codex(lines, returncode=1, stderr='Error: sandbox setup failed', edit=False)
        self.assertEqual(result['attempts'][0]['failure_reason'], 'Error: sandbox setup failed')

    def test_codex_quota_error_is_still_detected_in_json_output(self):
        lines = [json.dumps({'type': 'turn.failed', 'error': {'message': "You've hit your usage limit."}})]
        result = self.run_codex(lines, returncode=1, edit=False)
        self.assertTrue(result['attempts'][0]['quota_error'])

    def test_codex_json_tokens_are_counted(self):
        usage = json.loads(fixture_lines('codex')[-1])['usage']
        self.assertEqual(eval_corpus.extract_tokens_from_text('\n'.join(fixture_lines('codex'))),
                         usage['input_tokens'] + usage['output_tokens'])


class FeedHookTests(Base):
    """Done-check 6: running jobs preview their newest events; everything else is unchanged."""

    def setUp(self):
        super().setUp()
        self.policy = load_policy(timezone='UTC')
        self.policy['run_job'].update(jobs_dir=str(self.base / 'jobs'), worktree_root=str(self.base / 'wt'))
        self.policy['run_task']['output_dir'] = str(self.base / 'out')
        terms = self.base / 'terms.txt'
        terms.write_text(TERM + '\n')
        self.policy['run_task']['private_terms_file'] = str(terms)
        for name in ('jobs', 'wt', 'out'):
            (self.base / name).mkdir()
        self.now = datetime(2026, 11, 12, 12, 5, tzinfo=timezone.utc)

    def feed(self):
        return live_jobs_feed.build_live_jobs_feed(policy=self.policy, now=self.now)

    def test_running_job_preview_comes_from_its_newest_events_file(self):
        (self.base / 'wt' / 'demo-20261112-120000').mkdir()
        (self.base / 'out' / '20261112-120000-a1-claude.log').write_text('old attempt output\n')
        old = self.base / 'out' / '20261112-120000-a1-claude.events.jsonl'
        old.write_text(json.dumps({'ts': 't', 'kind': 'error', 'text': 'quota'}) + '\n')
        replay('codex', 'codex', self.base / 'out' / '20261112-120000-a2-codex.events.jsonl')
        with open(self.base / 'out' / '20261112-120000-a2-codex.events.jsonl', 'a') as handle:
            handle.write(json.dumps({'ts': 't', 'kind': 'message', 'text': f'tampered {TERM}'}) + '\n')
        running = self.feed()['running'][0]
        self.assertEqual(running['preview'][:2], ['message: Created `hello.txt` containing `hi`. `ls` confirmed: `hello.txt`.',
                                                  'done: finished'])
        self.assertEqual(running['preview'][2], 'message: tampered [REDACTED]')

    def test_running_job_without_events_keeps_the_log_preview(self):
        (self.base / 'wt' / 'demo-20261112-120000').mkdir()
        (self.base / 'out' / '20261112-120000-a1-codex.log').write_text('line one\nline two\n')
        self.assertEqual(self.feed()['running'][0]['preview'], ['line one', 'line two'])

    def test_finished_job_preview_is_unchanged(self):
        (self.base / 'jobs' / 'demo-20261112-120000.json').write_text(json.dumps({
            'job_id': 'demo-20261112-120000', 'repo': '/tmp/demo', 'task': 't.md', 'workers': ['codex'],
            'started_at': '2026-11-12T12:00:00+00:00', 'finished_at': '2026-11-12T12:03:00+00:00', 'ok': True}))
        (self.base / 'out' / '20261112-120000-a1-codex.log').write_text('final line\n')
        replay('codex', 'codex', self.base / 'out' / '20261112-120000-a1-codex.events.jsonl')
        self.assertEqual(self.feed()['recent'][0]['preview'], ['final line'])


class RecordedFixtureTests(Base):
    """Done-check 7: real recordings, with origins, parse into message, command and done events."""

    def test_every_recorded_worker_shows_message_command_and_done(self):
        for worker, name in RECORDED.items():
            with self.subTest(worker=worker):
                header = json.loads((FIXTURES / f'{name}.header.json').read_text())
                for key in ('cli_version', 'captured', 'origin', 'scrubbed', 'prompt'):
                    self.assertTrue(header.get(key), key)
                self.assertIn('real CLI run', header['origin'])
                kinds = [e['kind'] for e in replay(worker, name, self.base / f'{name}.events.jsonl')]
                self.assertIn('message', kinds)
                self.assertTrue({'command', 'test'} & set(kinds))
                if worker != 'codex':  # the Codex recording wrote its file through a shell command
                    self.assertIn('edit', kinds)
                self.assertEqual(kinds[-1], 'done')

    def test_fixtures_hold_no_local_paths_even_across_streamed_chunks(self):
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, list):
                if value and all(type(item) is int and 0 <= item < 256 for item in value):
                    yield bytes(value).decode('utf-8', 'replace')
                else:
                    for item in value:
                        yield from strings(item)
            elif isinstance(value, dict):
                for key, item in value.items():
                    yield key
                    yield from strings(item)

        for name in RECORDED.values():
            with self.subTest(fixture=name):
                events = [json.loads(line) for line in fixture_lines(name)]
                text = ''.join(part for event in events for part in strings(event))  # joins split chunks
                for needle in ('/Users/', '/private/', 'claude-501'):
                    self.assertNotIn(needle, text)
                self.assertNotRegex(text, r'/home/(?!user\b)')


if __name__ == '__main__':
    unittest.main()
