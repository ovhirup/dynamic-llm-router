"""Tests for CI workflow and README CI status/badge documentation (#29)."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CiWorkflowTests(unittest.TestCase):
    def test_ci_workflow_runs_unittest_on_prs_without_secrets_and_readme_documents_badge(self):
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text()
        self.assertIn('pull_request:', workflow)
        self.assertIn('python -m unittest discover -t . -s . -v', workflow)
        self.assertNotIn('secrets.', workflow)

        readme = (ROOT / 'README.md').read_text()
        self.assertIn('actions/workflows/ci.yml/badge.svg', readme)
        self.assertIn('.github/workflows/ci.yml', readme)
        self.assertIn('zero secrets required', readme)


if __name__ == '__main__':
    unittest.main()
