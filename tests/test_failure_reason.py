import json
import unittest
from pathlib import Path

from llm_router.task_dispatch import failure_fields, failure_reason, stderr_tail


class StderrTailTests(unittest.TestCase):
    def test_tail_prefers_stderr_and_trims(self):
        self.assertEqual(stderr_tail('boom\n', 'out'), 'boom')
        self.assertEqual(stderr_tail('', 'only stdout\n'), 'only stdout')
        self.assertIsNone(stderr_tail('', ''))
        long = 'x' * 5000 + 'END'
        self.assertEqual(len(stderr_tail(long)), 2000)
        self.assertTrue(stderr_tail(long).endswith('END'))

    def test_failure_fields_uses_last_failed_attempt(self):
        attempts = [{'worker': 'a', 'ok': False, 'failure_reason': 'old', 'stderr_tail': 'old'},
                    {'worker': 'b', 'ok': False, 'stderr_tail': 'trace\nlast line'}]
        self.assertEqual(failure_fields(attempts), {'failure_reason': None, 'stderr_tail': 'trace\nlast line',
                                                    'error': 'b: last line'})
        self.assertEqual(failure_fields([{'worker': 'a', 'ok': True}]), {})


class FailingWorkerJobTests(unittest.TestCase):
    """Issue #14: a worker exiting 1 with stderr leaves its root cause in logs/jobs/<job_id>.json."""

    def test_job_json_records_stderr_tail_and_error(self):
        from test_job_dispatch import JobTests
        case = JobTests('test_three_failed_workers_end_the_job')
        case.setUp()
        try:
            for name in ('codex', 'grok-build', 'cursor'):
                case.worker("import sys; print('Traceback (most recent call last):', file=sys.stderr); "
                            "print('ImportError: dlopen(_spropack.so) failed', file=sys.stderr); sys.exit(1)", name)
            result = case.job(test_cmd='true', select=case.select_all)
            self.assertFalse(result['ok'])
            saved = json.loads((Path(case.base) / 'jobs' / f'{result["job_id"]}.json').read_text())
            for doc in (result, saved):
                self.assertEqual(doc['failure_reason'], 'ImportError: dlopen(_spropack.so) failed')
                self.assertIn('Traceback', doc['stderr_tail'])
                self.assertIn('ImportError: dlopen(_spropack.so) failed', doc['error'])
                attempt = doc['attempts'][0]
                self.assertEqual(attempt['failure_reason'], 'ImportError: dlopen(_spropack.so) failed')
                self.assertIn('ImportError', attempt['stderr_tail'])
                run = attempt['runs'][0]
                self.assertEqual(run['returncode'], 1)
                self.assertIn('ImportError', run['stderr_tail'])
        finally:
            case.tearDown()


class FailureReasonTests(unittest.TestCase):
    def test_prefers_exception_line(self):
        err = 'Traceback (most recent call last):\n  File "x.py", line 1\nImportError: dlopen(_spropack.so) failed\n'
        self.assertEqual(failure_reason(err), 'ImportError: dlopen(_spropack.so) failed')

    def test_falls_back_to_last_line_and_stdout(self):
        self.assertEqual(failure_reason('', 'starting\nmodel not found\n'), 'model not found')
        self.assertEqual(failure_reason('warn\nbye\n'), 'bye')

    def test_empty_and_trim(self):
        self.assertIsNone(failure_reason('', ''))
        self.assertEqual(len(failure_reason('Error: ' + 'x' * 1000)), 300)


if __name__ == '__main__':
    unittest.main()
