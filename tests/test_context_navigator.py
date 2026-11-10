"""Tests for Repo context navigator (#115): hierarchical retrieval, grep fallback, and recall metrics."""
from pathlib import Path
import subprocess
import unittest

from llm_router import context_navigator
from test_job_dispatch import (
    git,
    job_setup,
    job_teardown,
    job_select,
    job_worker,
    run_job_fixture,
)


class ContextNavigatorTests(unittest.TestCase):
    setUp = job_setup
    tearDown = job_teardown
    select = job_select
    worker = job_worker
    job = run_job_fixture

    def setUp(self):
        job_setup(self)
        for rel, content in [
            ('src/routing/selector.py', 'def select_worker(meters):\n    return "claude"\n'),
            ('src/routing/cooldown.py', 'def record_cooldown(worker, reset_at):\n    return reset_at\n'),
            ('docs/guide.md', '# Guide\nDocumentation for users.\n'),
            ('misc/hidden_helper.py', 'def special_token_calculator(x):\n    return x * 2\n'),
        ]:
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
            'commit', '-qm', 'seed tree')

    def test_hierarchical_navigation_and_grep_fallback_recover_pruned_folder(self):
        # top_folders=1 picks src/routing for the routing query, while grep_fallback recovers misc/hidden_helper.py
        nav = context_navigator.navigate(
            self.repo,
            'Update select_worker in selector.py and call special_token_calculator',
            top_folders=1,
            top_files=2,
            top_chunks=3,
            fallback_limit=2,
        )
        self.assertIn('src/routing/selector.py', nav['selected_files'])
        self.assertIn('misc/hidden_helper.py', nav['recovered_files'])
        self.assertIn('misc/hidden_helper.py', nav['selected_files'])
        self.assertEqual(
            context_navigator.compute_recall(nav['selected_files'], ['src/routing/selector.py', 'misc/hidden_helper.py']),
            1.0,
        )

    def assert_excluded_from_context(self, relative_path, marker):
        tree = context_navigator.build_hierarchy(self.repo)
        self.assertNotIn(relative_path, tree['files'])
        self.assertNotIn(relative_path, tree['chunks'])
        for folder in tree['folders'].values():
            self.assertNotIn(relative_path, folder['files'])
        query = f'select_worker {marker}'
        self.assertNotIn(relative_path, context_navigator.grep_fallback(tree, query, limit=20))
        navigation = context_navigator.navigate(self.repo, query, hierarchy=tree)
        self.assertNotIn(relative_path, navigation['selected_files'])
        self.assertNotIn(relative_path, navigation['recovered_files'])
        prompt = context_navigator.format_for_prompt(navigation)
        self.assertNotIn(relative_path, prompt)
        self.assertNotIn(marker, prompt)

    def test_gitignored_text_is_excluded_from_context(self):
        (self.repo / 'docs/.gitignore').write_text('draft*.md\n!draft-public.md\n')
        (self.repo / 'docs/draft-private.md').write_text('private_draft_marker\n')
        (self.repo / 'docs/draft-public.md').write_text('public_draft_marker\n')

        self.assert_excluded_from_context('docs/draft-private.md', 'private_draft_marker')
        self.assertIn('docs/draft-public.md', context_navigator.build_hierarchy(self.repo)['files'])

    def test_denylisted_tracked_path_is_excluded_from_context(self):
        relative_path = 'docs/auth.json'
        (self.repo / relative_path).write_text('{"example": "credential_fixture_marker"}\n')
        subprocess.run(['git', 'add', '-f', '--', relative_path], cwd=self.repo, check=True)

        self.assert_excluded_from_context(relative_path, 'credential_fixture_marker')

    def test_tracked_source_is_indexed_and_formatted_for_prompt(self):
        relative_path = 'src/routing/selector.py'
        tree = context_navigator.build_hierarchy(self.repo)
        self.assertIn(relative_path, tree['files'])
        navigation = context_navigator.navigate(self.repo, 'select_worker selector.py', hierarchy=tree)
        prompt = context_navigator.format_for_prompt(navigation)
        self.assertIn(f'### `{relative_path}`', prompt)
        self.assertIn('def select_worker(meters):', prompt)

    def test_run_job_behind_flag_injects_context_and_records_recall_metrics(self):
        self.policy['run_job']['context_navigator'] = {'enabled': True, 'top_folders': 2, 'top_files': 3}
        self.task.write_text('# Update selector.py\nChange select_worker in src/routing/selector.py.\n')
        capture = self.base / 'prompt.txt'
        self.worker(
            "import sys; open(%r,'w').write(sys.stdin.read()); "
            "open('src/routing/selector.py','a').write('# updated\\n')" % str(capture)
        )
        result = self.job(test_cmd='grep -q updated src/routing/selector.py')
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['context_navigator']['enabled'])
        self.assertIn('src/routing/selector.py', result['context_navigator']['selected_files'])
        self.assertEqual(result['context_navigator']['recall'], 1.0)
        self.assertIn('## Repo context navigator (DecisionRAG)', capture.read_text())

    def test_eval_recall_on_past_dlr_jobs_fixtures(self):
        dlr_root = Path(__file__).resolve().parents[1]
        tree = context_navigator.build_hierarchy(dlr_root)
        sample_tasks = [
            ('Update week_budget and pace in week_curve.py', ['src/llm_router/week_curve.py']),
            ('Fix evaluate_unit and out_of_scope in merge_queue.py', ['src/llm_router/merge_queue.py']),
            ('Add case_from_job and diff_paths in eval_corpus.py', ['src/llm_router/eval_corpus.py']),
        ]
        recalls = []
        for query, touched in sample_tasks:
            nav = context_navigator.navigate(dlr_root, query, top_folders=2, top_files=5, hierarchy=tree)
            recalls.append(context_navigator.compute_recall(nav['selected_files'], touched))
        self.assertEqual(recalls, [1.0, 1.0, 1.0])


if __name__ == '__main__':
    unittest.main()
