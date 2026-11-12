import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import TERM, load_policy
from llm_router import cli, handoff, job_dispatch, security, selection, unit_review
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
        self._assert_parent_gitlink_committed()
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

    def _assert_parent_gitlink_committed(self):
        row = self.git(self.repo, 'ls-tree', 'HEAD', 'vendor/lib').strip()
        self.assertTrue(row.startswith('160000'), row)
        self.assertIn('.gitmodules', self.git(self.repo, 'ls-tree', '--name-only', 'HEAD').splitlines())
        self.assertEqual(self.git(self.repo, 'status', '--porcelain').strip(), '')

    def _add_submodule(self, path):
        nested = self.root / 'nested-repo'
        repo(nested)
        self.commit(nested, 'sub init')
        self.git(self.repo, 'submodule', 'add', str(nested), path)
        self.commit(self.repo, 'add submodule')
        return self.repo / path

    def _nested_submodule(self, outer, inner_path):
        nested = self.root / 'deep-repo'
        repo(nested)
        self.commit(nested, 'deep init')
        self.git(outer, 'submodule', 'add', str(nested), inner_path)
        self.commit(outer, 'add nested submodule')
        if outer is self.repo:
            self._assert_parent_gitlink_committed()

    def invoke(self, *args):
        stdout = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(cli.config, 'load_policy', return_value=self.policy))
        stack.enter_context(mock.patch.object(job_dispatch, 'repo_settings',
                                             side_effect=lambda policy, p: self.settings
                                             if Path(p).resolve() == Path(self.target).resolve() else {}))
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

    def _empty_private_path_in_submodule(self, state):
        name = f'notes/{TERM}-empty.txt'
        (self.sub / 'notes').mkdir()
        (self.sub / name).touch()
        if state == 'staged':
            self.git(self.sub, 'add', name)
        elif state == 'committed':
            self.commit(self.sub, 'empty private path')
        elif state != 'untracked':
            raise AssertionError(state)
        result, stdout = self.check_json(1)
        self.assertEqual(
            result['leak_check']['findings'],
            [{'file': f'vendor/lib/notes/{security.mask(TERM)}-empty.txt (path)', 'kind': 'private_term'}],
        )
        self.assertNotIn(TERM, stdout)
        self.assertNotIn(TERM, json.dumps(result))

    def test_untracked_empty_private_path_in_submodule_is_blocked(self):
        self._empty_private_path_in_submodule('untracked')

    def test_staged_empty_private_path_in_submodule_is_blocked(self):
        self._empty_private_path_in_submodule('staged')

    def test_committed_empty_private_path_in_submodule_is_blocked(self):
        self._empty_private_path_in_submodule('committed')

    def test_planted_secret_after_committed_gitlink_blocks_with_empty_parent_diff(self):
        base = self.git(self.target, 'rev-parse', 'HEAD').strip()
        (self.target / 'vendor' / 'lib' / 'planted.txt').write_text(TERM + '\n')
        result, stdout = self.check_json(1, '--base', base)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertIn('vendor/lib', json.dumps(result['leak_check']['findings']))
        self.assertNotIn(TERM, stdout)

    def test_secret_in_modified_submodule_file_blocks_check(self):
        (self.target / 'vendor' / 'lib' / 'tracked.txt').write_text('key = ' + PLANTED_VALUE + '\n')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertIn('vendor/lib', json.dumps(result['leak_check']['findings']))
        self.assertNotIn(PLANTED_VALUE, stdout)

    def test_untracked_secret_in_submodule_blocks_check(self):
        (self.target / 'vendor' / 'lib' / 'secret.env').write_text(TERM + '\n')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertNotIn(TERM, stdout)

    def test_staged_submodule_secret_blocks_check(self):
        path = self.target / 'vendor' / 'lib' / 'staged.txt'
        path.write_text(TERM + '\n')
        self.git(self.target / 'vendor' / 'lib', 'add', 'staged.txt')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertNotIn(TERM, stdout)

    def test_nested_submodule_untracked_blocks_after_committed(self):
        self._nested_submodule(self.sub, 'nested/pkg')
        self.commit(self.target, 'bump vendor/lib gitlink for nested')
        (self.target / 'vendor' / 'lib' / 'nested' / 'pkg' / 'leaf.txt').write_text(TERM + '\n')
        base = self.git(self.target, 'rev-parse', 'HEAD').strip()
        result, _ = self.check_json(1, '--base', base)
        self.assertEqual(result['leak_check']['status'], 'blocked')

    def test_nested_submodule_tracked_blocks_after_committed(self):
        self._nested_submodule(self.sub, 'nested/pkg')
        path = self.target / 'vendor' / 'lib' / 'nested' / 'pkg' / 'tracked.txt'
        path.write_text(TERM + '\n')
        self.commit(self.target / 'vendor' / 'lib' / 'nested' / 'pkg')
        self.commit(self.target, 'bump nested gitlink')
        base = self.git(self.target, 'rev-parse', 'HEAD~1').strip()
        result, _ = self.check_json(1, '--base', base)
        self.assertEqual(result['leak_check']['status'], 'blocked')

    def test_submodule_committed_leak_blocks_vs_parent_base_before_bump(self):
        """Repro D: secret committed inside submodule; parent gitlink bumped."""
        pre_bump = self.git(self.target, 'rev-parse', 'HEAD').strip()
        (self.target / 'vendor' / 'lib' / 'leak.txt').write_text(TERM + '\n')
        self.commit(self.target / 'vendor' / 'lib')
        self.commit(self.target, 'bump submodule gitlink')
        result, stdout = self.check_json(1, '--base', pre_bump)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertNotIn(TERM, stdout)

    def test_submodule_with_secret_in_history_blocks_on_add(self):
        """Repro E: submodule history already contains a term when added on branch."""
        dirty = self.root / 'dirty-sub'
        repo(dirty)
        (dirty / 'history.txt').write_text(TERM + '\n')
        self.commit(dirty)
        branch = self.root / 'branch-project'
        shutil.copytree(self.repo, branch, symlinks=True)
        subprocess.run(['git', '-C', str(branch), 'checkout', '-qb', 'feature'], check=True)
        env = {**os.environ, 'GIT_ALLOW_PROTOCOL': 'file'}
        subprocess.run(['git', '-C', str(branch), 'submodule', 'add', str(dirty), 'vendor/extra'],
                       check=True, env=env)
        subprocess.run(['git', '-C', str(branch), 'add', '-A'], check=True, env=env)
        subprocess.run(['git', '-C', str(branch), '-c', 'user.name=t', '-c', 'user.email=t@t',
                        'commit', '-qm', 'add dirty submodule'], check=True, env=env)
        pre = subprocess.run(['git', '-C', str(branch), 'rev-parse', 'HEAD~1'],
                             capture_output=True, text=True).stdout.strip()
        self.target = branch
        self.settings = {'test_cmd': 'true', 'allow_no_tests': True}
        result, _ = self.check_json(1, '--base', pre)
        self.assertEqual(result['leak_check']['status'], 'blocked')

    def _binary_secret_payload(self):
        return b'\x00\xff' + b'key = ' + PLANTED_VALUE.encode() + b'\n'

    def test_submodule_committed_binary_nul_secret_blocks_check(self):
        path = self.target / 'vendor' / 'lib' / 'blob.bin'
        path.write_bytes(self._binary_secret_payload())
        self.commit(self.target / 'vendor' / 'lib')
        base = self.git(self.target, 'rev-parse', 'HEAD~1').strip()
        result, stdout = self.check_json(1, '--base', base)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertIn('vendor/lib', json.dumps(result['leak_check']['findings']))
        self.assertNotIn(PLANTED_VALUE, stdout)

    def test_submodule_staged_binary_nul_secret_blocks_check(self):
        path = self.target / 'vendor' / 'lib' / 'staged.bin'
        path.write_bytes(self._binary_secret_payload())
        self.git(self.target / 'vendor' / 'lib', 'add', 'staged.bin')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertNotIn(PLANTED_VALUE, stdout)

    def test_submodule_untracked_binary_nul_secret_blocks_check(self):
        path = self.target / 'vendor' / 'lib' / 'secret.bin'
        path.write_bytes(self._binary_secret_payload())
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertNotIn(PLANTED_VALUE, stdout)

    def test_submodule_untracked_secret_in_build_dir_blocks_check(self):
        build_dir = self.target / 'vendor' / 'lib' / 'build'
        build_dir.mkdir(parents=True)
        (build_dir / 'x.env').write_text(TERM + '\n')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertIn('vendor/lib/build', json.dumps(result['leak_check']['findings']))
        self.assertNotIn(TERM, stdout)

    def test_submodule_untracked_over_max_bytes_blocks_only_with_term(self):
        path = self.sub / 'oversized.txt'
        path.write_bytes(b'x' * 1_000_001 + b'\nplain\n')
        self.assertGreater(path.stat().st_size, 1_000_000)
        result, stdout = self.check_json(0)
        self.assertEqual(result['leak_check']['status'], 'clean')
        self.assertNotIn(TERM, stdout)
        path.write_bytes(b'x' * 1_000_001 + b'\n' + TERM.encode() + b'\n')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        blob = json.dumps(result['leak_check']['findings'])
        self.assertIn('vendor/lib/oversized.txt', blob)
        self.assertIn('private_term', blob)
        self.assertNotIn(TERM, stdout)

    def test_submodule_untracked_symlink_target_term_blocks(self):
        (self.sub / 'alias.txt').symlink_to(f'nested/{TERM}.txt')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        blob = json.dumps(result['leak_check']['findings'])
        self.assertIn('vendor/lib/alias.txt', blob)
        self.assertIn('private_term', blob)
        self.assertNotIn(TERM, stdout)

    def test_submodule_untracked_symlink_path_term_blocks_masked(self):
        (self.sub / f'{TERM}.txt').symlink_to('notes.txt')
        result, stdout = self.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        blob = json.dumps(result['leak_check']['findings'])
        self.assertIn('vendor/lib/', blob)
        self.assertIn('(path)', blob)
        self.assertIn(security.mask(TERM), blob)
        self.assertNotIn(TERM, stdout)
        self.assertTrue(any(item['kind'] == 'private_term' for item in result['leak_check']['findings']))

    def test_submodule_untracked_symlink_is_not_read_through(self):
        (self.sub / '.gitignore').write_text('ignored-payload.txt\nsecret-tree/\n')
        (self.sub / 'ignored-payload.txt').write_text(TERM + '\n')
        (self.sub / 'alias.txt').symlink_to('ignored-payload.txt')
        tree = self.sub / 'secret-tree'
        tree.mkdir()
        (tree / 'payload.txt').write_text(TERM + '\n')
        (self.sub / 'linked-tree').symlink_to('secret-tree', target_is_directory=True)
        result, stdout = self.check_json(0)
        self.assertEqual(result['leak_check']['status'], 'clean')
        self.assertNotIn(TERM, stdout)

    def test_submodule_symlink_outside_checkout_is_not_followed(self):
        outside = self.root / 'outside-secret.txt'
        outside.write_text(TERM + '\n')
        (self.sub / 'escape.txt').symlink_to(outside)
        result, stdout = self.check_json(2)
        self.assertIn('symlink', result['refused'])
        self.assertIsNone(result['leak_check'])
        self.assertNotIn(TERM, stdout)

    def test_submodule_leak_check_error_refuses(self):
        original = security.leak_check

        def flaky(*args, **kwargs):
            if Path(args[0]).resolve() == Path(self.sub).resolve():
                return {'status': 'error', 'error': 'OSError', 'findings': []}
            return original(*args, **kwargs)

        with mock.patch.object(security, 'leak_check', side_effect=flaky):
            result, _ = self.check_json(2)
        self.assertIn('refused', result)
        self.assertIn('submodule leak scan failed', result['refused'])


class SubmoduleProbeTests(SubmoduleCheckTests):
    """Eight-scenario reviewer probe; extends SubmoduleCheckTests."""

    def test_top_level_modified(self):
        self.test_secret_in_modified_submodule_file_blocks_check()

    def test_top_level_untracked(self):
        self.test_untracked_secret_in_submodule_blocks_check()

    def test_top_level_staged(self):
        self.test_staged_submodule_secret_blocks_check()

    def test_nested_untracked(self):
        self.test_nested_submodule_untracked_blocks_after_committed()

    def test_nested_tracked(self):
        self.test_nested_submodule_tracked_blocks_after_committed()

    def test_repro_d(self):
        self.test_submodule_committed_leak_blocks_vs_parent_base_before_bump()

    def test_repro_e(self):
        self.test_submodule_with_secret_in_history_blocks_on_add()


if __name__ == '__main__':
    unittest.main()
