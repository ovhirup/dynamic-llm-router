"""Issue #296 section 3: bounded gate/approvals reads, symlinked output parents, look-alike private terms."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from helpers import TERM
from test_live_feed_v2 import FeedFixture, SHA
from llm_router import live_jobs_feed, pr_gate


def within(seconds, func):
    """Run `func` in a thread; fail instead of hanging if it blocks."""
    box = {}
    thread = threading.Thread(target=lambda: box.update(value=func()), daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), 'read blocked'
    return box['value']


class GateReadTests(FeedFixture):
    JOB = 'alpha-20261112-175000'

    def gate_file(self):
        return self.base / 'gate' / f'{self.JOB}.json'

    def good_gate(self):
        return {'job_id': self.JOB, 'merged': {'sha': SHA}}

    def test_regular_gate_still_loads(self):
        self.gate_file().write_text(json.dumps(self.good_gate()))
        self.assertEqual(pr_gate.load_gate(self.policy, self.JOB), self.good_gate())

    def test_oversize_gate_is_ignored(self):
        self.gate_file().write_text(json.dumps({**self.good_gate(), 'pad': 'x' * (pr_gate.GATE_BYTES + 1)}))
        self.assertEqual(pr_gate.load_gate(self.policy, self.JOB), {'job_id': self.JOB})

    def test_symlinked_gate_is_ignored(self):
        real = self.base / 'real.json'
        real.write_text(json.dumps(self.good_gate()))
        self.gate_file().symlink_to(real)
        self.assertEqual(pr_gate.load_gate(self.policy, self.JOB), {'job_id': self.JOB})

    def test_directory_gate_is_ignored(self):
        self.gate_file().mkdir()
        self.assertEqual(pr_gate.load_gate(self.policy, self.JOB), {'job_id': self.JOB})

    def test_fifo_gate_does_not_hang(self):
        os.mkfifo(self.gate_file())
        self.assertEqual(within(5, lambda: pr_gate.load_gate(self.policy, self.JOB)), {'job_id': self.JOB})

    def test_non_object_gate_is_ignored(self):
        self.gate_file().write_text('[1, 2]')
        self.assertEqual(pr_gate.load_gate(self.policy, self.JOB), {'job_id': self.JOB})


class ApprovalReadTests(FeedFixture):
    def path(self):
        return self.base / 'approvals.jsonl'

    def test_regular_file_still_reads(self):
        self.approve('alpha-20261112-175000', SHA)
        self.assertEqual(len(pr_gate.approval_records(self.policy)), 1)

    def test_oversize_file_gives_no_records(self):
        self.approve('alpha-20261112-175000', SHA)
        with self.path().open('a') as handle:
            handle.write('\n' + ' ' * pr_gate.APPROVALS_BYTES)
        self.assertEqual(pr_gate.approval_records(self.policy), [])

    def test_symlinked_file_gives_no_records(self):
        real = self.base / 'real.jsonl'
        self.approve('alpha-20261112-175000', SHA)
        self.path().rename(real)
        self.path().symlink_to(real)
        self.assertEqual(pr_gate.approval_records(self.policy), [])

    def test_directory_gives_no_records(self):
        self.path().mkdir()
        self.assertEqual(pr_gate.approval_records(self.policy), [])

    def test_fifo_does_not_hang(self):
        os.mkfifo(self.path())
        self.assertEqual(within(5, lambda: pr_gate.approval_records(self.policy)), [])

    def test_a_bad_file_never_approves(self):
        job = 'alpha-20261112-175000'
        self.finished(job)
        real = self.base / 'real.jsonl'
        self.approve(job, SHA)
        self.path().rename(real)
        self.path().symlink_to(real)
        gates = self.find(self.feed(), job)['gates']
        self.assertNotEqual(gates['approval']['state'], 'passed')


class SymlinkedOutputTests(FeedFixture):
    A = 'alpha-20261112-175000'
    S = '20261112-175000'

    def setup_job(self):
        self.running(self.A, self.S)
        self.events(self.S, 1, 'codex', [('message', 'alpha-events')])
        self.assertEqual([e['text'] for e in self.find(self.feed(), self.A)['events']], ['alpha-events'])

    def assert_empty_lane(self):
        job = self.find(self.feed(), self.A)
        self.assertEqual(job['events'], [])
        self.assertEqual(job['preview'], [])
        self.assertEqual(job['workers'], [])

    def test_output_dir_itself_a_symlink(self):
        self.setup_job()
        real = self.base / 'real-out'
        self.out.rename(real)
        self.out.symlink_to(real, target_is_directory=True)
        self.assert_empty_lane()

    def test_symlinked_parent_of_output_dir(self):
        nested = self.base / 'nest'
        nested.mkdir()
        (nested / 'out').mkdir()
        self.out = nested / 'out'
        self.policy['run_task']['output_dir'] = str(self.out)
        self.setup_job()
        elsewhere = self.base / 'elsewhere'
        nested.rename(elsewhere)
        nested.symlink_to(elsewhere, target_is_directory=True)
        self.assert_empty_lane()

    def test_read_bounded_refuses_a_symlinked_parent(self):
        real = self.base / 'real'
        real.mkdir()
        (real / 'f.log').write_text('hello')
        (self.base / 'link').symlink_to(real, target_is_directory=True)
        self.assertEqual(live_jobs_feed.read_bounded(real / 'f.log'), (b'hello', False))
        self.assertIsNone(live_jobs_feed.read_bounded(self.base / 'link' / 'f.log'))


class LookAlikeTests(unittest.TestCase):
    TERMS = ('acme', 'Project Falcon')

    def mask(self, text):
        return live_jobs_feed.clean_text(text, self.TERMS)

    def test_fullwidth_term_is_masked(self):
        self.assertEqual(self.mask('see ＡＣＭＥ now'), 'see [REDACTED] now'.replace('[REDACTED]', live_jobs_feed.security.REDACTED))

    def test_cyrillic_and_greek_lookalikes_are_masked(self):
        red = live_jobs_feed.security.REDACTED
        for text in ('acmе', 'асме', 'αcmε', 'ΑСМΕ', 'аcme'):
            with self.subTest(text=text):
                self.assertEqual(self.mask(f'x {text} y'), f'x {red} y')

    def test_multiword_term_with_lookalikes(self):
        red = live_jobs_feed.security.REDACTED
        self.assertEqual(self.mask('Рroject  Fаlcon!'), f'{red}!')

    def test_original_characters_are_replaced_and_second_pass_is_a_noop(self):
        for text in ('ＡＣＭＥ-acme', 'preacmеpost', 'ａｃｍｅ ａｃｍｅ'):
            once = self.mask(text)
            self.assertNotIn('acme', once.casefold())
            self.assertFalse(any(ord(ch) > 127 for ch in once), once)
            self.assertEqual(self.mask(once), once)

    def test_other_text_is_untouched(self):
        self.assertEqual(self.mask('привет мир ＡＢＣ'), 'привет мир ABC'.replace('ABC', 'ＡＢＣ'))

    def test_ascii_is_unchanged(self):
        red = live_jobs_feed.security.REDACTED
        self.assertEqual(self.mask('an ACME and acme'), f'an {red} and {red}')


if __name__ == '__main__':
    unittest.main()
