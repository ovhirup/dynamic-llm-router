"""Tests for evidence-gated unit loop (#140): baseline first, checks.md evidence gate, 3-strike block, and resume."""
from pathlib import Path
import tempfile
import unittest

from llm_router import evidence_loop


class EvidenceGatedUnitLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'loop-state'

    def test_baseline_recorded_before_unit_edits_and_regression_is_reverted(self):
        events = []
        reverted = []
        ev_file = Path(self.tmp.name) / 'ev-u1.log'
        ev_file.write_text('tests passed\n')

        def baseline_runner():
            events.append('baseline')
            return {'status': 'pass', 'score': 0.80, 'evidence': str(ev_file)}

        def unit_runner(unit, attempt, feedback):
            events.append(f"{unit['id']}-a{attempt}")
            if attempt == 1:
                # Regresses below baseline 0.80 -> must be reverted
                return {'ok': True, 'score': 0.50, 'evidence': str(ev_file),
                        'revert': lambda: reverted.append((unit['id'], attempt))}
            return {'ok': True, 'score': 0.90, 'evidence': str(ev_file)}

        units = [{'id': 'u1', 'scope': ['src/a.py'], 'check': 'pytest -q'}]
        report = evidence_loop.run_evidence_loop(
            self.state, units, unit_runner, baseline_runner=baseline_runner, resume=False)
        self.assertEqual(events[0], 'baseline')
        self.assertEqual(reverted, [('u1', 1)])
        self.assertEqual(report['done'], ['u1'])
        self.assertTrue(report['units']['u1']['reverted'])
        self.assertTrue(Path(report['files']['baseline']).is_file())

    def test_unit_blocked_after_three_strikes_while_independent_units_continue(self):
        ev_file = Path(self.tmp.name) / 'ev-ok.log'
        ev_file.write_text('passed\n')
        attempts = {'bad': 0, 'good': 0}

        def unit_runner(unit, attempt, feedback):
            attempts[unit['id']] += 1
            if unit['id'] == 'bad':
                return {'ok': False, 'reason': 'assertion failed'}
            return {'ok': True, 'score': 1.0, 'evidence': str(ev_file)}

        units = [
            {'id': 'bad', 'scope': ['src/bad.py'], 'check': 'test_bad'},
            {'id': 'good', 'scope': ['src/good.py'], 'check': 'test_good'},
        ]
        report = evidence_loop.run_evidence_loop(self.state, units, unit_runner, resume=False)
        self.assertEqual(attempts['bad'], 3)
        self.assertEqual(attempts['good'], 1)
        self.assertEqual(report['blocked'], ['bad'])
        self.assertEqual(report['done'], ['good'])
        self.assertEqual(len(report['failed_attempts']), 3)

    def test_checks_md_never_marks_pass_without_evidence_path(self):
        row_missing = evidence_loop.normalize_check_row('u1', 'pytest', 'pass', evidence_path=None)
        self.assertEqual(row_missing['status'], 'unresolved')

        row_nonexistent = evidence_loop.normalize_check_row('u1', 'pytest', 'pass', evidence_path='/nonexistent/ev.log')
        self.assertEqual(row_nonexistent['status'], 'unresolved')

        ev_file = Path(self.tmp.name) / 'real-ev.log'
        ev_file.write_text('ok\n')
        row_valid = evidence_loop.normalize_check_row('u1', 'pytest', 'pass', evidence_path=str(ev_file))
        self.assertEqual(row_valid['status'], 'pass')

    def test_resume_from_progress_md_picks_up_at_next_unit(self):
        ev_file = Path(self.tmp.name) / 'ev.log'
        ev_file.write_text('ok\n')
        evidence_loop.write_progress(self.state, done=['u1'], running=[], blocked=[], next_unit='u2')
        ran = []

        def unit_runner(unit, attempt, feedback):
            ran.append(unit['id'])
            return {'ok': True, 'score': 1.0, 'evidence': str(ev_file)}

        units = [
            {'id': 'u1', 'scope': ['src/1.py'], 'check': 'c1'},
            {'id': 'u2', 'scope': ['src/2.py'], 'check': 'c2', 'deps': ['u1']},
        ]
        report = evidence_loop.run_evidence_loop(self.state, units, unit_runner, resume=True)
        self.assertEqual(ran, ['u2'])
        self.assertEqual(report['done'], ['u1', 'u2'])


if __name__ == '__main__':
    unittest.main()
