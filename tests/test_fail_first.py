"""Tests for fail-first bug task verification (#104): red on base, green after fix."""
import unittest

from llm_router import fail_first
from test_job_dispatch import JobTests, git


class FailFirstBugCheckTests(unittest.TestCase):
    setUp = JobTests.setUp
    tearDown = JobTests.tearDown
    select = JobTests.select
    worker = JobTests.worker
    job = JobTests.job
    assert_main_untouched = JobTests.assert_main_untouched

    def setUp(self):
        JobTests.setUp(self)
        (self.repo / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
        (self.repo / 'tests').mkdir(exist_ok=True)
        (self.repo / 'tests' / 'test_calc.py').write_text(
            'import unittest, calc\nclass T(unittest.TestCase):\n'
            '    def test_zero(self): self.assertEqual(calc.add(2, 0), 2)\n'
        )
        git(self.repo, 'add', 'calc.py', 'tests/test_calc.py')
        git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
            'commit', '-qm', 'buggy calc')
        self.task.write_text('# Fix bug: calc.add subtracts instead of adding\ntype: bug\nFix calc.add.\n')

    def test_bug_task_detects_type_and_title(self):
        self.assertTrue(fail_first.is_bug_task('type: bug\nFix x'))
        self.assertTrue(fail_first.is_bug_task('# Fix bug: broken parser\n'))
        self.assertFalse(fail_first.is_bug_task('# Add feature\nCreate out.txt\n'))

    def test_red_on_base_and_green_after_fix_passes_and_commits(self):
        self.worker(
            "open('calc.py','w').write('def add(a, b):\\n    return a + b\\n'); "
            "open('tests/test_calc.py','w').write("
            "'import unittest, calc\\nclass T(unittest.TestCase):\\n"
            "    def test_add(self): self.assertEqual(calc.add(2, 3), 5)\\n')"
        )
        result = self.job(test_cmd=f'{self.policy["run_task"]["commands"]["codex"][0]} -m unittest discover -s tests')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'committed')
        self.assertEqual(result['fail_first']['status'], 'verified')
        self.assertEqual(result['fail_first']['base_tests']['status'], 'failed')
        self.assertEqual(result['fail_first']['after_status'], 'passed')

    def test_test_already_green_on_base_is_rejected_as_check_proves_nothing(self):
        self.policy['run_job']['max_retries'] = 0
        self.worker(
            "open('calc.py','w').write('def add(a, b):\\n    return a + b\\n'); "
            "open('tests/test_calc.py','w').write("
            "'import unittest, calc\\nclass T(unittest.TestCase):\\n"
            "    def test_zero(self): self.assertEqual(calc.add(3, 0), 3)\\n')"
        )
        result = self.job(test_cmd=f'{self.policy["run_task"]["commands"]["codex"][0]} -m unittest discover -s tests')
        self.assertFalse(result['ok'])
        self.assertIsNone(result['commit'])
        self.assertEqual(result['tests_status'], 'check_proves_nothing')
        self.assertIn('check proves nothing', result['reason'])

    def test_bug_fix_without_reproducing_test_is_rejected(self):
        self.policy['run_job']['max_retries'] = 0
        self.worker("open('calc.py','w').write('def add(a, b):\\n    return a + b\\n')")
        result = self.job(test_cmd=f'{self.policy["run_task"]["commands"]["codex"][0]} -m unittest discover -s tests')
        self.assertFalse(result['ok'])
        self.assertIsNone(result['commit'])
        self.assertEqual(result['tests_status'], 'check_proves_nothing')
        self.assertIn('reproducing test', result['reason'])


if __name__ == '__main__':
    unittest.main()
