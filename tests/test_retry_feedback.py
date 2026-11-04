"""Tests for converging retry feedback (#103): EXPECTED/SAW/FENCE/DEAD ENDS and same-failure-twice stop."""
import json
from pathlib import Path
import unittest

from llm_router import retry_feedback
from test_job_dispatch import (
    job_setup,
    job_teardown,
    job_worker,
    run_job_fixture,
)


class RetryFeedbackTests(unittest.TestCase):
    def test_fingerprint_normalizes_volatile_paths_and_line_numbers(self):
        one = retry_feedback.failure_fingerprint({
            'status': 'failed',
            'tail': 'FAIL: test_parse (test_mod.ParseTests)\n  File "/tmp/wt-1/a.py", line 12\nAssertionError: 1 != 2\n',
        })
        two = retry_feedback.failure_fingerprint({
            'status': 'failed',
            'tail': 'FAIL: test_parse (test_mod.ParseTests)\n  File "/var/folders/xy/wt-2/a.py", line 99\nAssertionError: 1 != 2\n',
        })
        self.assertEqual(one['id'], two['id'])
        self.assertEqual(one['failing_tests'], ['test_parse'])
        self.assertIn('AssertionError: 1 != 2', one['error_line'])
        self.assertTrue(retry_feedback.same_failure_twice([one, two]))

    def test_retry_prompt_contains_all_four_sections(self):
        prompt = retry_feedback.build_retry_prompt(
            {'status': 'failed', 'command': 'pytest -q', 'tail': 'FAILED test_x.py::test_a\nAssertionError: bad'},
            attempt={'ok': True},
            fence_paths=['src/app.py', 'tests/test_x.py'],
            dead_ends=['attempt 1 (codex): tests failed — AssertionError: bad'],
            task_goal='# Fix parser\nHandle whitespace.\n',
        )
        for section in ('### EXPECTED', '### SAW', '### FENCE', '### DEAD ENDS'):
            self.assertIn(section, prompt)
        self.assertIn('`src/app.py`', prompt)
        self.assertIn('attempt 1 (codex): tests failed', prompt)


class SameFailureTwiceJobTests(unittest.TestCase):
    setUp = job_setup
    tearDown = job_teardown
    worker = job_worker
    job = run_job_fixture

    def select(self, chars, **kw):
        return {'eligible': ['codex', 'grok-build', 'cursor'],
                'meters': {'codex': 10, 'grok-build': 10, 'cursor': 10}, 'reason': 'test'}

    def test_same_failure_twice_stops_early_and_persists_retry_state(self):
        self.policy['run_job']['max_retries'] = 4
        self.policy['run_job']['handoffs_dir'] = str(self.base / 'handoffs')
        for name in ('codex', 'grok-build', 'cursor'):
            self.worker("open('out.txt','w').write('same\\n')", name)
        result = self.job(test_cmd="python3 -c 'assert False, \"identical boom\"'")
        self.assertFalse(result['ok'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'same_failure_twice')
        self.assertTrue(result.get('same_failure_twice'))
        self.assertEqual(len(result['attempts']), 2)
        state_file = Path(self.base / 'handoffs' / result['job_id'] / 'retry_state.json')
        self.assertTrue(state_file.is_file())
        saved = json.loads(state_file.read_text())
        self.assertTrue(saved['same_failure_twice'])
        self.assertEqual(len(saved['fingerprints']), 2)
        self.assertEqual(saved['fingerprints'][0]['id'], saved['fingerprints'][1]['id'])


if __name__ == '__main__':
    unittest.main()
