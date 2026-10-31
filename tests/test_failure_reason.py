import unittest

from llm_router.task_dispatch import failure_reason


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
