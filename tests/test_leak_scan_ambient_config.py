"""Ambient git drivers must not hide the bytes a leak scan compares to a commit.

The driver is installed only through GIT_CONFIG_GLOBAL, pointing at a file in the
test temporary directory. Each test restores the previous value.
"""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from helpers import TERM, load_policy
from llm_router import check as check_mod
from llm_router import cli, job_dispatch, security


class AmbientDriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.marker = self.root / 'marker'
        self.config_path = self.root / 'global.gitconfig'
        self._saved_global = os.environ.get('GIT_CONFIG_GLOBAL')
        self._saved_protocol = os.environ.get('GIT_ALLOW_PROTOCOL')
        os.environ['GIT_CONFIG_GLOBAL'] = str(self.config_path)
        os.environ['GIT_ALLOW_PROTOCOL'] = 'file'
        self.addCleanup(self._restore_env)
        self.config_path.write_text('[user]\n\tname = t\n\temail = t@t\n')
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git(self.repo, 'init', '-q')
        self.git(self.repo, 'branch', '-M', 'main')
        (self.repo / 'README.md').write_text('hello\n')
        self.git(self.repo, 'add', 'README.md')
        self.git(self.repo, 'commit', '-qm', 'init')
        self.base = self.git(self.repo, 'rev-parse', 'HEAD').strip()
        self.terms = self.root / 'terms.txt'
        self.terms.write_text(TERM + '\n')
        self.policy = load_policy()
        self.policy['run_task']['private_terms_file'] = str(self.terms)
        self.settings = {'test_cmd': 'true', 'allow_no_tests': True}

    def _restore_env(self):
        if self._saved_global is None:
            os.environ.pop('GIT_CONFIG_GLOBAL', None)
        else:
            os.environ['GIT_CONFIG_GLOBAL'] = self._saved_global
        if self._saved_protocol is None:
            os.environ.pop('GIT_ALLOW_PROTOCOL', None)
        else:
            os.environ['GIT_ALLOW_PROTOCOL'] = self._saved_protocol

    def git(self, cwd, *args):
        done = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def _commit(self, cwd):
        self.git(cwd, 'add', '-A')
        self.git(cwd, 'commit', '-qm', 'fixture')

    def _set_config(self, body):
        self.config_path.write_text(body + '\n[user]\n\tname = t\n\temail = t@t\n')

    def _script(self, body):
        path = self.root / 'driver.sh'
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)
        return path

    def _marker_line(self):
        self.marker_token = uuid.uuid4().hex
        return f'printf "%s\\n" "{self.marker_token}" >> "{self.marker}"\n'

    def _install_textconv(self):
        script = self._script(self._marker_line() + f'sed "s/{TERM}/harmless/g" "$1"\n')
        self._set_config(
            f'[diff "hide"]\n\ttextconv = {script}\n\tcachetextconv = false\n')

    def _install_clean(self):
        script = self._script(self._marker_line() + f'sed "s/{TERM}/harmless/g"\n')
        self._set_config(f'[filter "hide"]\n\tclean = {script}\n')

    def _install_external(self):
        script = self._script(self._marker_line() + 'printf "harmless\\n"\n')
        self._set_config(f'[diff]\n\texternal = {script}\n')

    def _install_driver_command(self):
        script = self._script(self._marker_line() + 'printf "harmless\\n"\n')
        self._set_config(f'[diff "hide"]\n\tcommand = {script}\n')

    def _clear_marker(self):
        self.marker.unlink(missing_ok=True)

    def _plant_textconv(self, state):
        self._install_textconv()
        (self.repo / '.gitattributes').write_text('*.txt diff=hide\n')
        if state == 'committed':
            (self.repo / 'secret.txt').write_text(TERM + '\n')
            self._commit(self.repo)
        elif state == 'staged':
            self._commit(self.repo)
            (self.repo / 'secret.txt').write_text(TERM + '\n')
            self.git(self.repo, 'add', '--', 'secret.txt')
            (self.repo / 'secret.txt').write_text('plain\n')
        elif state == 'working':
            (self.repo / 'secret.txt').write_text('plain\n')
            self._commit(self.repo)
            (self.repo / 'secret.txt').write_text(TERM + '\n')
        elif state == 'intent':
            self._commit(self.repo)
            (self.repo / 'secret.txt').write_text(TERM + '\n')
            self.git(self.repo, 'add', '--intent-to-add', '--', 'secret.txt')
        elif state == 'untracked':
            self._commit(self.repo)
            (self.repo / 'secret.txt').write_text(TERM + '\n')
        else:
            raise AssertionError(state)
        self._clear_marker()

    def _plant_clean(self, state):
        self._install_clean()
        (self.repo / '.gitattributes').write_text('*.txt filter=hide\n')
        if state == 'committed':
            (self.repo / 'secret.txt').write_text(TERM + '\n')
            self._commit(self.repo)
        elif state == 'staged':
            self._commit(self.repo)
            (self.repo / 'secret.txt').write_text(TERM + '\n')
            self.git(self.repo, 'add', '--', 'secret.txt')
        elif state == 'working':
            (self.repo / 'secret.txt').write_text('plain\n')
            self._commit(self.repo)
            (self.repo / 'secret.txt').write_text(TERM + '\n')
        elif state == 'untracked':
            self._commit(self.repo)
            (self.repo / 'secret.txt').write_text(TERM + '\n')
        else:
            raise AssertionError(state)
        self._clear_marker()

    def _added_blob(self):
        """Bytes `git add` would commit, which is when a clean filter runs."""
        self.git(self.repo, 'add', '--', 'secret.txt')
        return self.git(self.repo, 'cat-file', 'blob', ':secret.txt')

    def check_json(self, repo=None, base=None):
        repo = self.repo if repo is None else repo
        base = self.base if base is None else base
        stdout, stderr = io.StringIO(), io.StringIO()
        with (mock.patch.object(cli.config, 'load_policy', return_value=self.policy),
              mock.patch.object(job_dispatch, 'repo_settings',
                                side_effect=lambda policy, path: self.settings
                                if Path(path).resolve() == Path(repo).resolve() else {}),
              contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr)):
            try:
                code = cli.main(['check', '--repo', str(repo), '--base', base, '--json'])
            except SystemExit as error:
                code = error.code
        self.assertEqual(stderr.getvalue(), '', stdout.getvalue())
        return code, json.loads(stdout.getvalue()), stdout.getvalue()

    def _expect_leak_blocked(self, result):
        self.assertEqual(result, {
            'status': 'blocked',
            'findings': [{'file': 'secret.txt:1', 'kind': 'private_term',
                          'match': security.mask(TERM)}],
        })
        self.assertFalse(self.marker.exists())

    def _expect_check_blocked(self, code, result, stdout):
        self.assertEqual(code, 1, result)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertNotIn(TERM, stdout)
        self.assertNotIn(TERM, json.dumps(result))
        self.assertIn('private_term', {item['kind'] for item in result['leak_check']['findings']})
        self.assertFalse(self.marker.exists())

    def test_textconv_committed_leak_check(self):
        self._plant_textconv('committed')
        self._expect_leak_blocked(security.leak_check(self.repo, self.base, [], [TERM]))

    def test_textconv_staged_leak_check(self):
        self._plant_textconv('staged')
        self._expect_leak_blocked(
            security.leak_check(self.repo, self.base, [], [TERM], cached=True))

    def test_textconv_working_tree_leak_check(self):
        self._plant_textconv('working')
        self._expect_leak_blocked(security.leak_check(self.repo, self.base, [], [TERM]))

    def test_textconv_untracked_then_added_leak_check(self):
        self._plant_textconv('intent')
        self._expect_leak_blocked(security.leak_check(self.repo, self.base, [], [TERM]))

    def test_textconv_committed_check(self):
        self._plant_textconv('committed')
        self._expect_check_blocked(*self.check_json())

    def test_textconv_staged_check(self):
        self._plant_textconv('staged')
        self._expect_check_blocked(*self.check_json())

    def test_textconv_working_tree_check(self):
        self._plant_textconv('working')
        self._expect_check_blocked(*self.check_json())

    def test_textconv_untracked_then_added_check(self):
        self._plant_textconv('untracked')
        self._expect_check_blocked(*self.check_json())

    def test_textconv_history_scan_reads_stored_bytes(self):
        self._install_textconv()
        (self.repo / '.gitattributes').write_text('*.txt diff=hide\n')
        (self.repo / 'secret.txt').write_text(TERM + '\n')
        self._commit(self.repo)
        (self.repo / 'secret.txt').unlink()
        self._commit(self.repo)
        self._clear_marker()
        result = security.scan_tree(self.repo, [TERM], history=True)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertNotIn(TERM, json.dumps(result))
        self.assertTrue(any(item['kind'] == 'private_term' and item['match'] == security.mask(TERM)
                            for item in result['findings']))
        self.assertFalse(self.marker.exists())

    def test_textconv_review_diff_keeps_raw_bytes(self):
        self._plant_textconv('committed')
        patch = check_mod._diff(self.repo, self.base, os.environ.copy())
        self.assertIn(TERM, patch)
        self.assertNotIn('harmless', patch)
        self.assertFalse(self.marker.exists())

    def test_clean_filter_committed_leak_check_agrees_with_blob(self):
        self._plant_clean('committed')
        result = security.leak_check(self.repo, self.base, [], [TERM])
        self.assertEqual(result['status'], 'clean', result)
        # Stat-clean worktree: the diff reuses the stored blob and does not re-run the filter.
        self.assertNotIn(TERM, self.git(self.repo, 'cat-file', 'blob', 'HEAD:secret.txt'))
        self.assertNotIn(TERM, self._added_blob())

    def test_clean_filter_staged_leak_check_agrees_with_blob(self):
        self._plant_clean('staged')
        result = security.leak_check(self.repo, self.base, [], [TERM], cached=True)
        self.assertEqual(result['status'], 'clean', result)
        self.assertFalse(self.marker.exists())
        self.assertNotIn(TERM, self._added_blob())

    def test_clean_filter_working_tree_leak_check_agrees_with_blob(self):
        self._plant_clean('working')
        result = security.leak_check(self.repo, self.base, [], [TERM])
        self.assertEqual(result['status'], 'clean', result)
        self.assertTrue(self.marker.exists())
        self.assertNotIn(TERM, self._added_blob())

    def test_clean_filter_untracked_leak_check_reads_worktree_bytes(self):
        """Untracked files are read from disk. `git add` still stores the filtered blob."""
        self._plant_clean('untracked')
        result = security.leak_check(self.repo, self.base, None, [TERM])
        self.assertEqual(result['status'], 'blocked', result)
        self.assertEqual(result['findings'][0]['match'], security.mask(TERM))
        self.assertNotIn(TERM, json.dumps(result))
        self.assertFalse(self.marker.exists())
        self.assertNotIn(TERM, self._added_blob())

    def test_clean_filter_committed_check_agrees_with_blob(self):
        self._plant_clean('committed')
        code, result, stdout = self.check_json()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['leak_check']['status'], 'clean')
        self.assertNotIn(TERM, stdout)
        self.assertNotIn(TERM, self._added_blob())

    def test_clean_filter_staged_check_agrees_with_blob(self):
        self._plant_clean('staged')
        code, result, stdout = self.check_json()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['leak_check']['status'], 'clean')
        self.assertNotIn(TERM, stdout)
        self.assertNotIn(TERM, self._added_blob())

    def test_clean_filter_working_tree_check_agrees_with_blob(self):
        self._plant_clean('working')
        code, result, stdout = self.check_json()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['leak_check']['status'], 'clean')
        self.assertNotIn(TERM, stdout)
        self.assertNotIn(TERM, self._added_blob())

    def test_clean_filter_untracked_check_agrees_with_blob(self):
        self._plant_clean('untracked')
        code, result, stdout = self.check_json()
        self.assertEqual(code, 0, result)
        self.assertEqual(result['leak_check']['status'], 'clean')
        self.assertNotIn(TERM, stdout)
        self.assertTrue(self.marker.exists())
        self.assertNotIn(TERM, self._added_blob())

    def test_clean_filter_retroactive_uncached_diff_hides_stored_blob(self):
        """Stop: do not change filter handling.

        A blob committed before the clean filter applied still holds the term.
        `leak_check` without `--cached` diffs the worktree, so the filter rewrites
        that view and the finding is missed. `git diff --cached` and `llm-router
        check` still read the stored blob. `git add` would replace it.
        """
        (self.repo / 'secret.txt').write_text(TERM + '\n')
        self._commit(self.repo)
        self._install_clean()
        (self.repo / '.gitattributes').write_text('*.txt filter=hide\n')
        # Add only the attributes. `git add -A` would renormalize secret.txt and
        # replace the stored blob, which is a different case.
        self.git(self.repo, 'add', '--', '.gitattributes')
        self.git(self.repo, 'commit', '-qm', 'attrs')
        self._clear_marker()
        uncached = security.leak_check(self.repo, self.base, [], [TERM])
        self.assertEqual(uncached['status'], 'clean', uncached)
        self.assertTrue(self.marker.exists())
        self.assertIn(TERM, self.git(self.repo, 'cat-file', 'blob', 'HEAD:secret.txt'))
        self._clear_marker()
        cached = security.leak_check(self.repo, self.base, [], [TERM], cached=True)
        self.assertEqual(cached['status'], 'blocked')
        self.assertEqual(cached['findings'][0]['file'], 'secret.txt:1')
        self.assertEqual(cached['findings'][0]['match'], security.mask(TERM))
        self.assertNotIn(TERM, json.dumps(cached))
        self.assertFalse(self.marker.exists())
        code, result, stdout = self.check_json()
        self.assertEqual(code, 1, result)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertNotIn(TERM, stdout)
        self.assertTrue(self.marker.exists())
        self.assertNotIn(TERM, self._added_blob())

    def test_external_diff_leak_check_keeps_raw_bytes(self):
        (self.repo / 'secret.txt').write_text(TERM + '\n')
        self._commit(self.repo)
        self._install_external()
        self._clear_marker()
        self._expect_leak_blocked(security.leak_check(self.repo, self.base, [], [TERM]))

    def test_external_diff_check_keeps_raw_bytes(self):
        (self.repo / 'secret.txt').write_text(TERM + '\n')
        self._commit(self.repo)
        self._install_external()
        self._clear_marker()
        self._expect_check_blocked(*self.check_json())

    def test_diff_driver_command_leak_check_keeps_raw_bytes(self):
        self._install_driver_command()
        (self.repo / '.gitattributes').write_text('*.txt diff=hide\n')
        (self.repo / 'secret.txt').write_text(TERM + '\n')
        self._commit(self.repo)
        self._clear_marker()
        self._expect_leak_blocked(security.leak_check(self.repo, self.base, [], [TERM]))

    def test_diff_driver_command_check_keeps_raw_bytes(self):
        self._install_driver_command()
        (self.repo / '.gitattributes').write_text('*.txt diff=hide\n')
        (self.repo / 'secret.txt').write_text(TERM + '\n')
        self._commit(self.repo)
        self._clear_marker()
        self._expect_check_blocked(*self.check_json())

    def test_external_diff_history_log_is_not_rewritten(self):
        (self.repo / 'secret.txt').write_text(TERM + '\n')
        self._commit(self.repo)
        (self.repo / 'secret.txt').unlink()
        self._commit(self.repo)
        self._install_external()
        self._clear_marker()
        result = security.scan_tree(self.repo, [TERM], history=True)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertNotIn(TERM, json.dumps(result))
        self.assertFalse(self.marker.exists())

    def test_diff_driver_command_history_log_is_not_rewritten(self):
        self._install_driver_command()
        (self.repo / '.gitattributes').write_text('*.txt diff=hide\n')
        (self.repo / 'secret.txt').write_text(TERM + '\n')
        self._commit(self.repo)
        (self.repo / 'secret.txt').unlink()
        self._commit(self.repo)
        self._clear_marker()
        result = security.scan_tree(self.repo, [TERM], history=True)
        self.assertEqual(result['status'], 'blocked', result)
        self.assertNotIn(TERM, json.dumps(result))
        self.assertFalse(self.marker.exists())

    def test_textconv_changed_paths_does_not_run_driver(self):
        self._plant_textconv('committed')
        names = security.changed_paths(self.repo, self.base, env=os.environ.copy())
        self.assertIn('secret.txt', names)
        self.assertFalse(self.marker.exists())
        self._clear_marker()
        staged = security.changed_paths(self.repo, self.base, cached=True, env=os.environ.copy())
        self.assertIn('secret.txt', staged)
        self.assertFalse(self.marker.exists())

    def _add_submodule(self):
        nested = self.root / 'nested'
        nested.mkdir()
        self.git(nested, 'init', '-q')
        (nested / 'README.md').write_text('sub\n')
        self.git(nested, 'add', 'README.md')
        self.git(nested, 'commit', '-qm', 'sub')
        self.git(self.repo, 'submodule', 'add', str(nested), 'vendor/lib')
        self.git(self.repo, 'commit', '-qm', 'add sub')
        return self.repo / 'vendor/lib'

    def test_textconv_submodule_patches_keep_raw_bytes(self):
        sub = self._add_submodule()
        parent_base = self.git(self.repo, 'rev-parse', 'HEAD').strip()
        self._install_textconv()
        (sub / '.gitattributes').write_text('*.txt diff=hide\n')
        (sub / 'secret.txt').write_text(TERM + '\n')
        self._commit(sub)
        self._clear_marker()
        patches = check_mod._submodule_patches(self.repo, 'vendor/lib', parent_base, os.environ.copy())
        self.assertTrue(any(TERM in patch for patch in patches), patches)
        self.assertFalse(self.marker.exists())

    def test_textconv_submodule_review_diff_keeps_raw_bytes(self):
        sub = self._add_submodule()
        parent_base = self.git(self.repo, 'rev-parse', 'HEAD').strip()
        self._install_textconv()
        (sub / '.gitattributes').write_text('*.txt diff=hide\n')
        (sub / 'secret.txt').write_text(TERM + '\n')
        self._commit(sub)
        self._clear_marker()
        review = check_mod._submodule_review_diff(
            self.repo, 'vendor/lib', parent_base, os.environ.copy())
        self.assertIn(TERM, review)
        self.assertFalse(self.marker.exists())

    def test_textconv_submodule_path_names_do_not_run_driver(self):
        sub = self._add_submodule()
        start = self.git(self.repo, 'rev-parse', 'HEAD:vendor/lib').strip()
        self._install_textconv()
        (sub / '.gitattributes').write_text('*.txt diff=hide\n')
        (sub / 'notes.txt').write_text(TERM + '\n')
        self._commit(sub)
        self._clear_marker()
        names = check_mod._submodule_path_names(sub, start, os.environ.copy())
        self.assertIn('notes.txt', names)
        self.assertFalse(self.marker.exists())

    def test_textconv_submodule_check_does_not_run_driver(self):
        sub = self._add_submodule()
        parent_base = self.git(self.repo, 'rev-parse', 'HEAD').strip()
        self._install_textconv()
        (sub / '.gitattributes').write_text('*.txt diff=hide\n')
        (sub / 'secret.txt').write_text(TERM + '\n')
        self._commit(sub)
        self._clear_marker()
        code, result, stdout = self.check_json(base=parent_base)
        self.assertEqual(code, 1, result)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertNotIn(TERM, stdout)
        self.assertNotIn(TERM, json.dumps(result))
        self.assertFalse(self.marker.exists())
