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

    def test_evaluate_fails_when_skipped_only_on_earlier_summary_line(self):
        """Later 'N passed' decoy line must not hide an earlier skipped count."""
        out = '1 skipped in 0.01s\n2 passed in 0.01s\n'
        verdict = gate.evaluate_pytest(0, out)
        self.assertFalse(verdict['ok'])


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


if __name__ == '__main__':
    unittest.main()
