"""Issue #296: planted claim files cannot choose a lane, and text assembled after masking is masked again."""
import io
import json
import re
import unittest

from test_live_feed_v2 import SECRET, FeedFixture, sidecar_plan
from llm_router import allocation_dispatch, live_jobs_feed, security, watch


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


if __name__ == '__main__':
    unittest.main()
