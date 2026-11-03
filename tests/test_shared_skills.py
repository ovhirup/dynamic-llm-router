"""Tests for shared Agent Skills + baseline Claude Code plugins per task class (#96)."""
import unittest

from helpers import load_policy
from llm_router import config, shared_skills


class SharedSkillsTests(unittest.TestCase):
    def test_plugins_enabled_per_task_class_not_globally(self):
        policy = load_policy()
        self.assertEqual(
            shared_skills.plugins_for_class(policy, 'feature'),
            ['superpowers-tdd', 'context7', 'code-simplifier'],
        )
        self.assertEqual(
            shared_skills.plugins_for_class(policy, 'bugfix'),
            ['superpowers-tdd', 'code-review'],
        )
        self.assertEqual(shared_skills.plugins_for_class(policy, 'review'), ['code-review'])
        self.assertEqual(shared_skills.plugins_for_class(policy, 'mechanical'), ['code-simplifier'])
        self.assertEqual(shared_skills.plugins_for_class(policy, 'draft'), [])
        self.assertEqual(shared_skills.plugins_for_class(policy, None), [])

    def test_shared_skills_folder_and_before_after_token_savings(self):
        folder = shared_skills.skills_dir()
        self.assertTrue(folder.is_dir())
        for name in ('superpowers-tdd', 'code-review', 'code-simplifier'):
            self.assertTrue((folder / f'{name}.md').is_file())

        policy = load_policy()
        for cls in config.TASK_CLASSES:
            with self.subTest(task_class=cls):
                comparison = shared_skills.compare_per_class_vs_global_tokens(cls, policy=policy)
                self.assertTrue(comparison['equal_or_lower_tokens'])
                self.assertGreater(comparison['tokens_saved'], 0)


if __name__ == '__main__':
    unittest.main()
