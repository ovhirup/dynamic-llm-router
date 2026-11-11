import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import TERM, load_policy
from llm_router import cli, handoff, job_dispatch, selection, unit_review
from test_check import PLANTED_VALUE
from test_task_dispatch import repo


class SubmoduleCheckTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = repo(self.root / 'project')
        subprocess.run(['git', '-C', str(self.repo), 'branch', '-M', 'main'], check=True)
        self.sub = self._add_submodule('vendor/lib')
        terms = self.root / 'terms.txt'
        terms.write_text(TERM + '\n')
        self.policy = load_policy()
        self.policy['run_task']['private_terms_file'] = str(terms)
        self.settings = {'test_cmd': 'true', 'allow_no_tests': True}
        self.target = self.repo

    def git(self, cwd, *args):
        env = {**os.environ, 'GIT_ALLOW_PROTOCOL': 'file'}
        return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True,
                              text=True, env=env).stdout

    def commit(self, cwd, message='fixture'):
        self.git(cwd, 'add', '-A')
        if subprocess.run(['git', '-C', str(cwd), 'diff', '--cached', '--quiet']).returncode == 0:
            return
        self.git(cwd, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', message)

    def _add_submodule(self, path):
        nested = self.root / 'nested-repo'
        repo(nested)
        self.commit(nested, 'sub init')
        self.git(self.repo, 'submodule', 'add', str(nested), path)
        return self.repo / path

    def _nested_submodule(self, outer, inner_path):
        nested = self.root / 'deep-repo'
        repo(nested)
        self.commit(nested, 'deep init')
        self.git(outer, 'submodule', 'add', str(nested), inner_path)
        self.commit(outer, 'deep submodule')

    def invoke(self, *args):
        stdout = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(cli.config, 'load_policy', return_value=self.policy))
        stack.enter_context(mock.patch.object(job_dispatch, 'repo_settings',
                                             side_effect=lambda policy, p: self.settings if Path(p) == self.repo else {}))
        if '--review' not in args:
            stack.enter_context(mock.patch.object(selection, 'select', side_effect=AssertionError('offline')))
            stack.enter_context(mock.patch.object(handoff, 'meter_percent', side_effect=AssertionError('offline')))
            stack.enter_context(mock.patch.object(unit_review, 'default_reviewer', side_effect=AssertionError('offline')))
        stack.enter_context(contextlib.redirect_stdout(stdout))
        with stack:
            try:
                code = cli.main(['check', '--repo', str(self.target), *args])
            except SystemExit as error:
                code = error.code
        return code, stdout.getvalue()

    def check_json(self, expected, *args):
        code, stdout = self.invoke('--json', *args)
        self.assertEqual(code, expected, stdout)
        result = json.loads(stdout)
        self.assertEqual(result['ok'], expected == 0)
        return result, stdout

    def test_secret_in_modified_submodule_file_blocks_check(self):
        path = self.target / 'vendor' / 'lib' / 'tracked.txt'
        path.write_text('key = ' + PLANTED_VALUE + '\n')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        joined = json.dumps(result['leak_check']['findings'])
        self.assertIn('vendor/lib', joined)
        self.assertNotIn(PLANTED_VALUE, stdout)

    def test_untracked_secret_in_submodule_blocks_check(self):
        (self.target / 'vendor' / 'lib' / 'secret.env').write_text(TERM + '\n')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertNotIn(TERM, stdout)

    def test_nested_submodule_content_is_scanned(self):
        self._nested_submodule(self.sub, 'nested/pkg')
        nested = self.target / 'vendor' / 'lib' / 'nested' / 'pkg'
        (nested / 'leaf.txt').write_text('key = ' + PLANTED_VALUE + '\n')
        result, _ = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')

    def test_review_prompt_includes_submodule_diff(self):
        (self.target / 'DONE_CHECK.md').write_text('Submodule evidence is covered.\n')
        path = self.target / 'vendor' / 'lib' / 'reviewed.txt'
        path.write_text('visible change\n')
        prompts = []

        def review(name, packet, prompt, policy):
            prompts.append(prompt)
            return {'status': 'done',
                    'text': '{"schema_version":1,"verdict":"reject","notes":"intentional"}'}

        git_vars = set(job_dispatch._git(self.target, 'rev-parse', '--local-env-vars', check=True)
                       .stdout.splitlines())
        clean = {key: value for key, value in os.environ.items() if key not in git_vars}
        with (mock.patch.dict(os.environ, clean, clear=True),
              mock.patch.object(handoff, 'meter_percent', return_value=10),
              mock.patch.object(unit_review, 'default_reviewer', side_effect=review)):
            self.check_json(1, '--review')
        self.assertTrue(prompts)
        self.assertIn('reviewed.txt', prompts[0])
        self.assertIn('vendor/lib', prompts[0])


if __name__ == '__main__':
    unittest.main()
