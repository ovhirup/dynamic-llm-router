import re
import tempfile
import unittest
from pathlib import Path

from llm_router import brain

REPO = Path(__file__).resolve().parents[1]
RULE = re.compile(r'^\d+\. ', re.M)


class TrackedVaultTests(unittest.TestCase):
    def test_repo_vault_skeleton(self):
        vault = REPO / 'vault'
        self.assertTrue((vault / 'README.md').is_file())
        for name in brain.VAULT_DIRS:
            self.assertTrue((vault / name).is_dir(), name)
            marker = (vault / name / 'README.md').is_file() or (vault / name / '.gitkeep').is_file()
            self.assertTrue(marker, name)
        rules = (vault / 'rules.md').read_text()
        self.assertTrue(10 <= len(RULE.findall(rules)) <= 15)

    def test_default_rules_count(self):
        self.assertTrue(10 <= len(RULE.findall(brain.DEFAULT_RULES)) <= 15)


class InitVaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_vault_dir_resolution(self):
        self.assertEqual(brain.vault_dir(None), Path('vault'))
        self.assertEqual(brain.vault_dir({}), Path('vault'))
        self.assertEqual(brain.vault_dir({'brain': {'vault_dir': '/x/brain'}}), Path('/x/brain'))

    def test_creates_skeleton(self):
        root = self.base / 'v'
        created = brain.init_vault(root)
        for name in brain.VAULT_DIRS:
            self.assertTrue((root / name).is_dir())
        self.assertEqual((root / 'rules.md').read_text(), brain.DEFAULT_RULES)
        self.assertIn(root / 'rules.md', created)

    def test_uses_policy_path(self):
        root = self.base / 'from-policy'
        brain.init_vault(policy={'brain': {'vault_dir': str(root)}})
        self.assertTrue((root / 'rules.md').is_file())
        self.assertTrue((root / 'jobs').is_dir())

    def test_idempotent_and_keeps_user_edits(self):
        root = self.base / 'v'
        brain.init_vault(root)
        (root / 'rules.md').write_text('my rules\n')
        (root / 'jobs' / 'j1.md').write_text('note\n')
        self.assertEqual(brain.init_vault(root), [])
        self.assertEqual((root / 'rules.md').read_text(), 'my rules\n')
        self.assertEqual((root / 'jobs' / 'j1.md').read_text(), 'note\n')

    def test_fills_partial_vault(self):
        root = self.base / 'v'
        (root / 'jobs').mkdir(parents=True)
        created = brain.init_vault(root)
        self.assertNotIn(root / 'jobs', created)
        self.assertIn(root / 'research', created)

    def test_refuses_file_path(self):
        target = self.base / 'file'
        target.write_text('x')
        with self.assertRaises(NotADirectoryError):
            brain.init_vault(target)


if __name__ == '__main__':
    unittest.main()
