"""Tests for worker triage block (#116): prompt template, parsing into result JSON, handoff note, and missing case."""
from pathlib import Path
import unittest

from llm_router import handoff, triage
from test_job_dispatch import JobTests


class WorkerTriageTests(unittest.TestCase):
    setUp = JobTests.setUp
    tearDown = JobTests.tearDown
    select = JobTests.select
    worker = JobTests.worker
    job = JobTests.job

    def test_triage_block_in_worker_prompt_template_and_parsed_into_result_and_handoff(self):
        self.assertIn('Size: small | medium | large', handoff.WORKER_INSTRUCTIONS)
        self.policy['run_job']['handoffs_dir'] = str(self.base / 'handoffs')
        capture = self.base / 'prompt.txt'
        triage_out = (
            'Size: small — single file creation\n'
            'Tests: test_job_dispatch — covers out.txt\n'
            'Agents: solo — self-contained task\n'
            'Branch: router/add-feature in worktree\n'
        )
        self.worker(
            "import sys; open(%r,'a').write(sys.stdin.read()+'\\n===\\n'); "
            "print(%r); open('n.txt','a').write('x\\n')" % (str(capture), triage_out)
        )
        # First attempt fails tests (writes handoff note with triage), second passes.
        result = self.job(test_cmd='test $(wc -l < n.txt) -ge 2')
        self.assertTrue(result['ok'], result)
        self.assertIn('Triage block:', capture.read_text())
        self.assertIsNotNone(result['triage'])
        self.assertEqual(result['triage']['size'], 'small')
        self.assertEqual(result['triage']['size_why'], 'single file creation')
        self.assertEqual(result['triage']['tests'], 'test_job_dispatch')
        self.assertEqual(result['triage']['agents'], 'solo')
        self.assertEqual(result['triage']['branch'], 'router/add-feature in worktree')

        note_path = Path(result['handoffs'][0]['path'])
        parsed_note = handoff.parse(note_path.read_text())
        self.assertIn('Size: small', parsed_note['sections']['Triage'])
        self.assertIn('Agents: solo', parsed_note['sections']['Triage'])

    def test_missing_triage_block_is_handled_gracefully(self):
        self.assertIsNone(triage.parse_triage('just normal worker output without a triage block'))
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        self.assertIsNone(result['triage'])


if __name__ == '__main__':
    unittest.main()
