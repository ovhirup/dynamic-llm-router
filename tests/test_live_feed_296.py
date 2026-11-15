"""Issue #296: planted claim files cannot choose a lane, and text assembled after masking is masked again."""
import contextlib
import io
import json
import re
import unittest

from test_live_feed_v2 import SECRET, FeedFixture, sidecar_plan
from llm_router import allocation_dispatch, cli, live_jobs_feed, security, watch


class PlantedClaimTests(FeedFixture):
    """Section 1. An empty lane is acceptable; another job's events are not."""

    A = 'alpha-20261112-175000'
    B = 'beta-20261112-175000'
    S = '20261112-175000'
    T = '20261112-175000-2'

    def texts(self, job_id):
        return [event['text'] for event in self.find(self.feed(), job_id)['events']]

    def pair(self):
        self.running(self.A, self.S)
        self.running(self.B, self.T)
        self.events(self.S, 1, 'codex', [('message', 'alpha-events')])
        self.events(self.T, 1, 'claude', [('message', 'beta-events')])

    def test_unreadable_own_claim_plus_a_claim_naming_us_on_their_stamp(self):
        self.pair()
        (self.out / f'{self.S}-job.json').write_text('{')
        (self.out / f'{self.T}-job.json').write_text(json.dumps({'job_id': self.A}))
        self.assertEqual(self.texts(self.A), ['alpha-events'])
        self.assertEqual(self.texts(self.B), ['beta-events'])

    def test_planted_claim_for_a_job_that_has_no_claim(self):
        self.running(self.A)
        self.events(self.S, 1, 'codex', [('message', 'alpha-events')])
        self.events(self.T, 1, 'claude', [('message', 'planted-beta-events')])
        (self.out / f'{self.T}-job.json').write_text(json.dumps({'job_id': self.A}))
        self.assertEqual(self.texts(self.A), ['alpha-events'])

    def test_a_claim_naming_a_copied_onto_every_stamp(self):
        self.pair()
        self.events(f'{self.S}-3', 1, 'claude', [('message', 'extra-events')])
        for name in (self.S, self.T, f'{self.S}-3'):
            (self.out / f'{name}-job.json').write_text(json.dumps({'job_id': self.A}))
        self.assertEqual(self.texts(self.A), ['alpha-events'])
        self.assertEqual(self.texts(self.B), ['beta-events'])

    def test_deleting_a_claim_does_not_show_the_other_job(self):
        self.pair()
        (self.out / f'{self.S}-job.json').unlink()
        self.assertEqual(self.texts(self.A), ['alpha-events'])
        self.assertEqual(self.texts(self.B), ['beta-events'])

    def test_replacing_a_claim_with_a_symlink_does_not_show_the_other_job(self):
        self.pair()
        claim = self.out / f'{self.S}-job.json'
        claim.unlink()
        claim.symlink_to(self.out / f'{self.T}-job.json')
        self.assertEqual(self.texts(self.A), ['alpha-events'])
        self.assertNotIn('beta-events', self.texts(self.A))

    def test_replacing_a_claim_with_a_directory_does_not_show_the_other_job(self):
        self.pair()
        claim = self.out / f'{self.S}-job.json'
        claim.unlink()
        claim.mkdir()
        self.assertEqual(self.texts(self.A), ['alpha-events'])
        self.assertEqual(self.texts(self.B), ['beta-events'])

    def test_claims_naming_a_nonexistent_job_do_not_move_a_lane(self):
        self.pair()
        self.events(f'{self.S}-3', 1, 'claude', [('message', 'ghost-events')])
        (self.out / f'{self.S}-3-job.json').write_text(json.dumps({'job_id': 'ghost-20261112-180000'}))
        (self.out / f'{self.T}-job.json').write_text(json.dumps({'job_id': 'ghost-20261112-180000'}))
        self.assertEqual(self.texts(self.A), ['alpha-events'])
        self.assertEqual(self.texts(self.B), ['beta-events'])
        self.assertNotIn('ghost-events', self.texts(self.A) + self.texts(self.B))

    def _poisoned_stamp_record(self, jobs, out):
        """A and B own distinct stamps. A's record is then rewritten to B's stamp."""
        jobs.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        self.policy['run_job']['jobs_dir'] = str(jobs)
        self.policy['run_task']['output_dir'] = str(out)
        self.out = out
        alpha, beta = 'alpha-20261112-175000', 'beta-20261112-175100'
        stamp_a, stamp_b = '20261112-175000', '20261112-175100'
        for job_id, stamp, worker, text in (
                (alpha, stamp_a, 'codex', 'alpha-events'),
                (beta, stamp_b, 'claude', 'beta-events')):
            (self.base / 'wt' / job_id).mkdir()
            live_jobs_feed.write_stamp_record(jobs, job_id, stamp)
            self.events(stamp, 1, worker, [('message', text)])
        record = jobs / live_jobs_feed.STAMP_RECORD_DIR / f'{alpha}.json'
        record.write_text(json.dumps({'job_id': alpha, 'output_stamp': stamp_b}) + '\n')
        self.assertIn(stamp_b, record.read_text())
        self.assertEqual(self.texts(alpha), ['alpha-events'])
        self.assertEqual(self.texts(beta), ['beta-events'])
        self.assertNotIn('beta-events', self.texts(alpha))

    def test_jobs_dir_inside_output_dir_cannot_rewrite_a_lane(self):
        out = self.base / 'logs'
        self._poisoned_stamp_record(out / 'jobs', out)

    def test_output_dir_inside_jobs_dir_cannot_rewrite_a_lane(self):
        # The output directory is the stamp-record directory, which lives inside jobs_dir.
        jobs = self.base / 'jobs'
        self._poisoned_stamp_record(jobs, jobs / live_jobs_feed.STAMP_RECORD_DIR)

    def test_case_variant_output_dir_cannot_rewrite_a_lane(self):
        # `<base>/LOGS` and `<base>/logs/jobs` are one tree on a case-insensitive disk, and the case-folded
        # prefix says they overlap on a case-sensitive disk too. Not skipped: a skip would not count.
        self._poisoned_stamp_record(self.base / 'logs' / 'jobs', self.base / 'LOGS')


def _two_lanes(term_now='LEFT'):
    """Two running lanes. At width >= 100 the now-row joins as `LEFT │ ▶ RIGHT`."""
    left = {'repo': 'demo', 'worker': 'codex', 'workers': ['codex'], 'elapsed_seconds': 0,
            'state': 'running', 'now': 'x' * 41 + term_now, 'events': [], 'gates': {}}
    right = {**left, 'now': 'RIGHT'}
    return {'updated_at': '2026-11-12T18:00:00+00:00', 'running': [left, right], 'recent': [], 'history': []}


def _sidecar_value():
    """A credential-shaped sidecar value. Split so this file stores no secret assignment."""
    return 'synthetic' + 'credentialvalue'


class AssembledTextTests(FeedFixture):
    """Section 2. A second masking pass leaves the serialised feed and every frame line unchanged."""

    TERM = 'LEFT │ ▶ RIGHT'

    def test_a_password_sidecar_is_masked_in_emitted_json(self):
        stamp = '20261112-175000'
        self.running(f'demo-{stamp}', stamp)
        plan = sidecar_plan('codex')
        secret = _sidecar_value()
        plan['reasons'] = {'password': secret}
        allocation_dispatch.write_sidecar(self.out, f'{stamp}-a1', {'allocation': plan})
        feed = self.feed()
        stream = io.StringIO()
        watch.run(policy=self.policy, once=True, as_json=True, stream=stream, build=lambda **_kw: feed)
        serialised = stream.getvalue()
        self.assertNotIn(secret, serialised)
        self.assertEqual(list(security.find_secrets(serialised)), [])
        self.assertEqual(serialised, live_jobs_feed.final_mask(serialised))
        emitted = live_jobs_feed.emit_feed_json(feed)
        self.assertNotIn(secret, emitted)
        self.assertEqual(list(security.find_secrets(emitted)), [])
        self.assertEqual(emitted, live_jobs_feed.final_mask(emitted))

    def test_a_separator_term_joined_across_lanes_is_masked(self):
        feed = _two_lanes()
        joined = re.compile(r'LEFT\s+│\s+▶\s+RIGHT')
        for width in (80, 100, 160):
            frame = watch.render(feed, width, terms=(self.TERM,))
            self.assertIsNone(joined.search(frame), frame)
            for line in frame.splitlines():
                self.assertEqual(line, live_jobs_feed.mask_text(line, (self.TERM,)))
                self.assertEqual(list(security.find_secrets(line)), [])
            if width >= 100:
                self.assertIn(security.REDACTED, frame)

    def test_serialised_feed_and_frame_lines_are_stable_under_a_second_mask(self):
        terms = ('Zephyrquill', self.TERM)
        (self.base / 'terms.txt').write_text('\n'.join(terms) + '\n')
        stamp = '20261112-175000'
        self.running(f'demo-{stamp}', stamp)
        self.events(stamp, 1, 'codex', [('message', f'{SECRET} Zephyrquill')])
        plan = sidecar_plan('codex')
        secret = _sidecar_value()
        plan['reasons'] = {'password': secret}
        allocation_dispatch.write_sidecar(self.out, f'{stamp}-a1', {'allocation': plan})
        feed = self.feed()
        stream = io.StringIO()
        watch.run(policy=self.policy, once=True, as_json=True, stream=stream,
                  build=lambda **_kw: feed)
        serialised = stream.getvalue()
        self.assertNotIn(SECRET, serialised)
        self.assertNotIn(secret, serialised)
        self.assertNotIn('Zephyrquill', serialised)
        self.assertNotIn(self.TERM, serialised)
        self.assertEqual(serialised, live_jobs_feed.final_mask(serialised))
        self.assertEqual(list(security.find_secrets(serialised)), [])
        lanes = _two_lanes()
        lanes['running'][0]['events'] = [{'ts': '', 'worker': 'codex', 'attempt': 1, 'kind': 'message',
                                           'text': SECRET}]
        for width in (80, 100, 160):
            frame = watch.render(lanes, width, terms=terms)
            self.assertNotIn(SECRET, frame)
            self.assertIsNone(re.search(r'LEFT\s+│\s+▶\s+RIGHT', frame))
            for line in frame.splitlines():
                self.assertEqual(line, live_jobs_feed.mask_text(line, terms))
                self.assertEqual(list(security.find_secrets(line)), [])

    def _assert_stable_json(self, text):
        try:
            parsed = json.loads(text)
        except ValueError as error:
            self.fail(f'emitted feed is not JSON: {error}')
        self.assertEqual(list(security.find_secrets(text)), [])
        self.assertEqual(text.strip(), live_jobs_feed.emit_feed_json(parsed))
        self.assertEqual(text, live_jobs_feed.final_mask(text))

    def test_an_escaped_quote_in_event_text_stays_valid_json(self):
        stamp = '20261112-175000'
        self.running(f'demo-{stamp}', stamp)
        self.events(stamp, 1, 'codex', [('message', 'AUTH:abc"x')])
        feed = self.feed()
        stream = io.StringIO()
        watch.run(policy=self.policy, once=True, as_json=True, stream=stream, build=lambda **_kw: feed)
        self._assert_stable_json(stream.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['jobs', '--live-feed'], policy=self.policy)
        self.assertEqual(code, 0)
        self._assert_stable_json(out.getvalue())

    def test_a_non_ascii_value_that_is_not_a_secret_is_not_masked(self):
        stamp = '20261112-175000'
        self.running(f'demo-{stamp}', stamp)
        self.events(stamp, 1, 'codex', [('message', 'TOKEN=é b'), ('message', 'é')])
        out = live_jobs_feed.emit_feed_json(self.feed())
        self.assertIn('TOKEN=é b', out)
        self.assertIn('"é"', out)
        self.assertEqual(list(security.find_secrets(out)), [])
        self.assertEqual(out, live_jobs_feed.emit_feed_json(json.loads(out)))
        self.assertEqual(out, live_jobs_feed.final_mask(out))

    def test_a_secret_shaped_sidecar_key_is_masked_in_emitted_json(self):
        stamp = '20261112-175000'
        self.running(f'demo-{stamp}', stamp)
        plan = sidecar_plan('codex')
        plan['reasons'] = {'AUTH:abc"': 'fine'}
        allocation_dispatch.write_sidecar(self.out, f'{stamp}-a1', {'allocation': plan})
        feed = self.feed()
        stream = io.StringIO()
        watch.run(policy=self.policy, once=True, as_json=True, stream=stream, build=lambda **_kw: feed)
        self._assert_stable_json(stream.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['jobs', '--live-feed'], policy=self.policy)
        self.assertEqual(code, 0)
        self._assert_stable_json(out.getvalue())

    def test_a_term_inside_the_placeholder_does_not_grow(self):
        terms = ('Ed',)
        (self.base / 'terms.txt').write_text('Ed\n')
        stamp = '20261112-175000'
        self.running(f'demo-{stamp}', stamp)
        self.events(stamp, 1, 'codex', [('message', 'handed to Ed for review')])
        synthetic = {
            'updated_at': '2026-11-12T18:00:00+00:00',
            'running': [{
                'repo': 'demo', 'worker': 'codex', 'workers': ['codex'], 'elapsed_seconds': 0,
                'state': 'running', 'now': 'note for Ed',
                'events': [{'ts': '', 'worker': 'codex', 'attempt': 1, 'kind': 'message',
                            'text': 'handed to Ed for review'}],
                'gates': {},
            }],
            'recent': [], 'history': [],
        }
        for source in (self.feed(), synthetic):
            for width in (80, 100, 160):
                frame = watch.render(source, width, terms=terms)
                for line in frame.splitlines():
                    self.assertEqual(line, live_jobs_feed.mask_text(line, terms))
                    self.assertEqual(list(security.find_secrets(line)), [])
                    self.assertLessEqual(len(line), width)
        repeated = 'Ed ' * 200
        pattern = live_jobs_feed._term_patterns(terms)[0]
        once = security.REDACTED.join(
            pattern.sub(security.REDACTED, part) for part in security.redact(repeated).split(security.REDACTED))
        self.assertEqual(live_jobs_feed.mask_text(repeated, terms), once)

    def test_a_term_joined_across_lanes_cannot_leave_a_secret_shaped_line(self):
        terms = ('AB │ ▶ RIGHT',)
        for pad in range(30, 46):
            left = {'repo': 'demo', 'worker': 'codex', 'workers': ['codex'], 'elapsed_seconds': 0,
                    'state': 'running', 'now': 'x' * pad + ' PASSWORD=AB', 'events': [], 'gates': {}}
            right = {**left, 'now': 'RIGHTzzzz'}
            feed = {'updated_at': '2026-11-12T18:00:00+00:00', 'running': [left, right],
                    'recent': [], 'history': []}
            for width in (100, 160):
                with self.subTest(pad=pad, width=width):
                    for line in watch.render(feed, width, terms=terms).splitlines():
                        self.assertEqual(line, live_jobs_feed.mask_text(line, terms))
                        self.assertEqual(list(security.find_secrets(line)), [])


if __name__ == '__main__':
    unittest.main()
