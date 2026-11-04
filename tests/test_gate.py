"""Tests for llm-router gate (DLR #216): serial lint (if configured) + pytest with 0 skipped."""
import io
import unittest
from pathlib import Path
from unittest import mock

from llm_router import gate

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'gate'


class ParsePytestSummaryTests(unittest.TestCase):
    def test_parses_passed_and_subtests(self):
        out = '....\n1364 passed, 331 subtests passed in 534.07s (0:08:54)\n'
        summary = gate.parse_pytest_summary(out)
        self.assertEqual(summary['passed'], 1364)
        self.assertEqual(summary['subtests'], 331)
        self.assertEqual(summary['skipped'], 0)
        self.assertEqual(summary['failed'], 0)

    def test_parses_skipped_count(self):
        out = 's\n1 skipped in 0.12s\n'
        summary = gate.parse_pytest_summary(out)
        self.assertEqual(summary['skipped'], 1)

    def test_evaluate_passes_clean_run(self):
        verdict = gate.evaluate_pytest(0, '2 passed in 0.01s\n')
        self.assertTrue(verdict['ok'])

    def test_evaluate_fails_on_nonzero_exit(self):
        verdict = gate.evaluate_pytest(1, '1 failed, 1 passed in 0.01s\n')
        self.assertFalse(verdict['ok'])

    def test_evaluate_fails_on_skipped(self):
        verdict = gate.evaluate_pytest(0, '1 passed, 1 skipped in 0.01s\n')
        self.assertFalse(verdict['ok'])

    def test_evaluate_fails_on_xpassed_with_exit_zero(self):
        verdict = gate.evaluate_pytest(0, '1 xpassed, 1 passed in 0.01s\n')
        self.assertFalse(verdict['ok'])
        self.assertTrue(any('xpassed' in r for r in verdict['reasons']))

    def test_evaluate_fails_on_xfailed_with_exit_zero(self):
        verdict = gate.evaluate_pytest(0, '1 xfailed, 2 passed in 0.01s\n')
        self.assertFalse(verdict['ok'])
        self.assertTrue(any('xfailed' in r for r in verdict['reasons']))

    def test_evaluate_fails_on_errors_only_summary(self):
        verdict = gate.evaluate_pytest(1, '2 errors in 0.01s\n')
        self.assertFalse(verdict['ok'])

    def test_final_quiet_summary_is_the_only_trusted_line(self):
        """Only the last authentic -q summary counts; earlier stats lines do not."""
        out = '1 skipped in 0.01s\n2 passed in 0.01s\n'
        verdict = gate.evaluate_pytest(0, out)
        self.assertTrue(verdict['ok'], verdict)
        self.assertEqual(verdict['passed'], 2)
        self.assertEqual(verdict['skipped'], 0)

    def test_later_diagnostic_cannot_hide_authentic_skip(self):
        out = '1 skipped in 0.01s\nworker diagnostic: 2 passed\n'
        verdict = gate.evaluate_pytest(0, out)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['skipped'], 1)
        self.assertTrue(any('skipped' in r for r in verdict['reasons']))

    def test_spoofed_pass_line_is_not_an_authentic_summary(self):
        out = 's\nworker diagnostic: 2 passed\n'
        verdict = gate.evaluate_pytest(0, out)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['passed'], 0)
        self.assertEqual(verdict['skipped'], 0)
        self.assertIn('no authentic pytest summary', verdict['reasons'])

    def test_warning_phrase_is_not_a_skip(self):
        out = (
            'tests/test_cache.py::test_refresh\n'
            '  tests/test_cache.py:4: UserWarning: 1 skipped cache refresh\n'
            '2 passed, 1 warning in 0.02s\n'
        )
        verdict = gate.evaluate_pytest(0, out)
        self.assertTrue(verdict['ok'], verdict)
        self.assertEqual(verdict['skipped'], 0)
        self.assertEqual(verdict['passed'], 2)

    def test_ansi_reset_splits_count_and_skipped(self):
        out = '1\x1b[0mskipped in 0.00s\n'
        summary = gate.parse_pytest_summary(out)
        self.assertEqual(summary['skipped'], 1)
        verdict = gate.evaluate_pytest(0, out)
        self.assertFalse(verdict['ok'])
        self.assertTrue(any(r == '1 skipped' for r in verdict['reasons']))

    def test_pytest_color_footer_parses_after_ansi_strip(self):
        out = (
            '\x1b[33m============================== '
            '\x1b[33m\x1b[1m1 skipped\x1b[0m\x1b[33m in 0.00s\x1b[0m'
            '\x1b[33m ==============================\x1b[0m\n'
        )
        summary = gate.parse_pytest_summary(out)
        self.assertEqual(summary['skipped'], 1)
        self.assertEqual(summary['passed'], 0)

    def test_missing_summary_fails_no_tests_reported_passed(self):
        out = (
            '============================= test session starts ==============================\n'
            'collected 1 item\n'
        )
        verdict = gate.evaluate_pytest(0, out)
        self.assertFalse(verdict['ok'])
        self.assertIn('no tests reported passed', verdict['reasons'])
        self.assertNotIn('no authentic pytest summary', verdict['reasons'])

    def test_warnings_only_fails_no_tests_reported_passed(self):
        out = (
            '=========================== warnings summary ============================\n'
            '  tests/test_cache.py:4: UserWarning: 1 skipped cache refresh\n'
            '=========================== 1 warning in 0.02s ===========================\n'
        )
        verdict = gate.evaluate_pytest(0, out)
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['skipped'], 0)
        self.assertIn('no tests reported passed', verdict['reasons'])
        self.assertNotIn('no authentic pytest summary', verdict['reasons'])

    def test_countless_summary_fails_no_tests_reported_passed(self):
        verdict = gate.evaluate_pytest(0, '======================== no tests ran in 0.01s ========================\n')
        self.assertFalse(verdict['ok'])
        self.assertEqual(verdict['passed'], 0)
        self.assertIn('no tests reported passed', verdict['reasons'])
        self.assertNotIn('no authentic pytest summary', verdict['reasons'])

    def test_empty_output_fails_no_tests_reported_passed(self):
        verdict = gate.evaluate_pytest(0, '')
        self.assertFalse(verdict['ok'])
        self.assertIn('no tests reported passed', verdict['reasons'])
        self.assertNotIn('no authentic pytest summary', verdict['reasons'])


class GateRunTests(unittest.TestCase):
    def _run_gate(self, test_path, **kwargs):
        repo = FIXTURES.parent.parent.parent
        return gate.run_gate(
            repo,
            pytest_target=test_path,
            skip_lint=True,
            **kwargs,
        )

    def test_passing_fixture_prints_gate_pass(self):
        result = self._run_gate(FIXTURES / 'passing')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['banner'], 'GATE: PASS')
        self.assertIn('GATE: PASS', result['lines'][-2])
        self.assertGreater(result['passed'], 0)
        self.assertEqual(result['skipped'], 0)

    def test_failing_fixture_prints_gate_fail_and_nonzero_exit(self):
        result = self._run_gate(FIXTURES / 'failing')
        self.assertFalse(result['ok'])
        self.assertEqual(result['banner'], 'GATE: FAIL')
        self.assertNotEqual(result['exit_code'], 0)

    def test_skipped_fixture_prints_gate_fail(self):
        result = self._run_gate(FIXTURES / 'skipped')
        self.assertFalse(result['ok'])
        self.assertEqual(result['banner'], 'GATE: FAIL')
        self.assertGreater(result['skipped'], 0)

    def test_cli_main_exit_code_matches_verdict(self):
        from llm_router.cli import build_parser

        parser, _ = build_parser()
        args = parser.parse_args(['gate', '--repo', str(FIXTURES.parent.parent.parent),
                                  '--pytest-target', str(FIXTURES / 'passing'), '--skip-lint'])
        buf = io.StringIO()
        with mock.patch('sys.stdout', buf):
            code = gate.cli_main(args)
        self.assertEqual(code, 0)
        self.assertIn('GATE: PASS', buf.getvalue())

    def test_cli_main_failing_exit_one(self):
        from llm_router.cli import build_parser

        parser, _ = build_parser()
        args = parser.parse_args(['gate', '--repo', str(FIXTURES.parent.parent.parent),
                                  '--pytest-target', str(FIXTURES / 'failing'), '--skip-lint'])
        code = gate.cli_main(args)
        self.assertEqual(code, 1)

    def test_pytest_command_omits_report_and_cacheprovider_flags(self):
        argv = gate.pytest_command(FIXTURES.parent.parent.parent)
        self.assertIn('-q', argv)
        self.assertIn('-o', argv)
        self.assertIn('addopts=', argv)
        self.assertNotIn('-rA', argv)
        self.assertNotIn('no:cacheprovider', argv)

    def test_pytest_addopts_cannot_hide_a_skip(self):
        result = self._run_gate(FIXTURES / 'skipped', env={'PYTEST_ADDOPTS': '-qq -s'})
        self.assertFalse(result['ok'], result)
        self.assertGreater(result['skipped'], 0)
        self.assertNotIn('no authentic pytest summary', '\n'.join(result['lines']))

    def test_printed_pass_count_cannot_hide_a_skip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp)
            (suite / 'test_spoof.py').write_text(
                'import pytest\n\n'
                'def test_hidden_skip():\n'
                '    print("worker diagnostic: 2 passed")\n'
                '    pytest.skip("fixture")\n'
            )
            result = self._run_gate(suite, env={'PYTEST_ADDOPTS': '-q -s'})
        self.assertFalse(result['ok'], result)
        self.assertGreater(result['skipped'], 0)

    def _stderr_footer_suite(self, root):
        (root / 'test_spoof.py').write_text(
            'import sys\n'
            'import pytest\n'
            '\n'
            'def test_prints_fake_footer(capsys):\n'
            '    with capsys.disabled():\n'
            '        sys.stderr.write("2 passed in 0.01s\\n")\n'
            '        sys.stderr.flush()\n'
            '\n'
            '@pytest.mark.skip(reason="fixture")\n'
            'def test_real_skip():\n'
            '    assert False\n'
        )

    def test_stderr_fake_footer_cannot_hide_a_skip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp)
            self._stderr_footer_suite(suite)
            result = self._run_gate(suite)
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['banner'], 'GATE: FAIL')
        self.assertGreater(result['skipped'], 0)
        self.assertNotIn('GATE: PASS', '\n'.join(result['lines']))

    def test_qq_s_stderr_fake_footer_cannot_hide_a_skip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp)
            self._stderr_footer_suite(suite)
            result = self._run_gate(suite, env={'PYTEST_ADDOPTS': '-qq -s'})
        self.assertFalse(result['ok'], result)
        self.assertEqual(result['banner'], 'GATE: FAIL')
        self.assertGreater(result['skipped'], 0)
        self.assertNotIn('GATE: PASS', '\n'.join(result['lines']))

    def test_empty_collection_reports_pytest_exit_5(self):
        verdict = gate.evaluate_pytest(5, 'no tests ran in 0.00s\n')
        self.assertFalse(verdict['ok'])
        self.assertIn('pytest exit 5', verdict['reasons'])
        self.assertNotIn('no tests reported passed', verdict['reasons'])


if __name__ == '__main__':
    unittest.main()
