"""Tests for approval gates by price of undo (#105): risk tiers decided in code."""
import unittest

from llm_router import risk_tiers
from test_job_dispatch import (
    job_setup,
    job_teardown,
    job_select,
    job_worker,
    run_job_fixture,
)


class RiskTierTests(unittest.TestCase):
    def test_classifies_low_medium_and_high_diffs_in_code(self):
        low = risk_tiers.classify_diff(['docs/guide.md', 'tests/test_app.py'])
        self.assertEqual(low['risk_tier'], 'low')
        self.assertTrue(low['approval_required'])  # default: auto-merge for low stays OFF

        low_opted_in = risk_tiers.classify_diff(
            ['docs/guide.md', 'tests/test_app.py'],
            policy={'pr_gate': {'auto_approve_low_risk': True}},
        )
        self.assertEqual(low_opted_in['risk_tier'], 'low')
        self.assertFalse(low_opted_in['approval_required'])

        med = risk_tiers.classify_diff(['src/llm_router/selection.py', 'tests/test_selection.py'])
        self.assertEqual(med['risk_tier'], 'medium')
        self.assertTrue(med['approval_required'])

        for high_paths, diff_text in [
            (['.github/workflows/ci.yml'], ''),
            (['src/llm_router/security.py'], ''),
            (['repos.json'], ''),
            (['.env.production'], ''),
            (['docs/README.md'], '+ subprocess.run(["git", "push", "--force"])'),
        ]:
            with self.subTest(high_paths=high_paths, diff_text=diff_text):
                high = risk_tiers.classify_diff(
                    high_paths, diff_text=diff_text,
                    policy={'pr_gate': {'auto_approve_low_risk': True}},
                )
                self.assertEqual(high['risk_tier'], 'high')
                self.assertTrue(high['approval_required'])

    def test_frozen_node_is_not_rerun_when_head_matches(self):
        entry = {'unit': 'u1', 'head': 'abc123', 'status': 'ready_for_human', 'gate_reasons': []}
        self.assertTrue(risk_tiers.is_frozen(entry, 'abc123'))
        self.assertFalse(risk_tiers.is_frozen(entry, 'def456'))
        self.assertFalse(risk_tiers.is_frozen(dict(entry, status='blocked', gate_reasons=['tests_failed']), 'abc123'))


class JobRiskTierReportingTests(unittest.TestCase):
    setUp = job_setup
    tearDown = job_teardown
    select = job_select
    worker = job_worker
    job = run_job_fixture

    def test_job_json_reports_risk_tier_and_approval_required(self):
        self.worker("import pathlib; pathlib.Path('docs').mkdir(exist_ok=True); "
                    "open('docs/note.md','w').write('hello\\n')")
        low_job = self.job(test_cmd='test -f docs/note.md')
        self.assertTrue(low_job['ok'], low_job)
        self.assertEqual(low_job['risk_tier'], 'low')
        self.assertTrue(low_job['approval_required'])

        self.worker("import pathlib; pathlib.Path('.github/workflows').mkdir(parents=True, exist_ok=True); "
                    "open('.github/workflows/extra.yml','w').write('name: ci\\n')")
        high_job = self.job(test_cmd='test -f .github/workflows/extra.yml')
        self.assertTrue(high_job['ok'], high_job)
        self.assertEqual(high_job['risk_tier'], 'high')
        self.assertTrue(high_job['approval_required'])


if __name__ == '__main__':
    unittest.main()
