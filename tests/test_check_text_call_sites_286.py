"""Pin `--text` at each of the five `git diff` call sites in llm_router.check (#286).

Each test plants a file whose first bytes contain a NUL plus a private term in exactly one
location and asserts that the diff returned by that single call site contains the term. Without
`--text`, git prints "Binary files differ" at that site and the assertion fails. The call sites
are exercised directly (not through `check()`), because the later leak scan covers some locations
a second way and would mask a missing flag.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import TERM
from llm_router import check as check_mod
from test_task_dispatch import repo

BINARY = b'\x00' + TERM.encode() + b'\n'
EMPTY_HOME_ENV = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}


def _env(tmp):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(EMPTY_HOME_ENV, HOME=str(tmp), GIT_ALLOW_PROTOCOL='file', GIT_OPTIONAL_LOCKS='0')
    return env


class TextCallSiteBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.env = _env(self.root)

    def git(self, cwd, *args):
        return subprocess.run(['git', '-c', 'protocol.file.allow=always', '-c', 'user.name=t',
                               '-c', 'user.email=t@t', '-C', str(cwd), *args],
                              check=True, capture_output=True, text=True, env=self.env).stdout

    def commit(self, cwd, message='fixture'):
        self.git(cwd, 'add', '-A')
        self.git(cwd, 'commit', '-qm', message)

    def assert_term_in(self, patch):
        self.assertIn(TERM, patch)
        self.assertNotIn('Binary files', patch)


class SnapshotTextCallSiteTests(TextCallSiteBase):
    def setUp(self):
        super().setUp()
        self.repo = repo(self.root / 'project')
        self.git(self.repo, 'branch', '-M', 'main')
        self.base = self.git(self.repo, 'rev-parse', 'HEAD').strip()

    def snapshot(self):
        head = self.git(self.repo, 'rev-parse', 'HEAD').strip()
        patches, _ = check_mod._snapshot(self.repo, self.root / 'scratch', self.base, head,
                                         self.env, False, [], [])
        (committed, _), (staged, _), (working, _) = patches
        return committed, staged, working

    def test_committed_diff_keeps_nul_file_as_text(self):
        (self.repo / 'README.md').write_bytes(BINARY)
        self.commit(self.repo)
        # Only HEAD retains the term; the index and worktree are restored to the base.
        self.git(self.repo, 'restore', '--source', self.base, '--staged', '--worktree', 'README.md')
        committed, staged, working = self.snapshot()
        self.assert_term_in(committed)
        self.assertNotIn(TERM, staged)

    def test_staged_diff_keeps_nul_file_as_text(self):
        path = self.repo / 'README.md'
        path.write_bytes(BINARY)
        self.git(self.repo, 'add', 'README.md')
        # Only the index retains the term; HEAD matches the base and the worktree is clean.
        path.write_text('hello\n')
        committed, staged, working = self.snapshot()
        self.assert_term_in(staged)
        self.assertNotIn(TERM, committed)

    def test_working_tree_diff_keeps_nul_file_as_text(self):
        # A tracked, unstaged change: neither HEAD nor the index holds the term.
        (self.repo / 'README.md').write_bytes(BINARY)
        committed, staged, working = self.snapshot()
        self.assert_term_in(working)
        self.assertNotIn(TERM, committed)
        self.assertNotIn(TERM, staged)


class SubmodulePatchTextCallSiteTests(TextCallSiteBase):
    def setUp(self):
        super().setUp()
        self.repo = repo(self.root / 'project')
        nested = repo(self.root / 'nested-repo')
        self.git(self.repo, 'submodule', 'add', str(nested), 'vendor/lib')
        self.commit(self.repo, 'add submodule')
        self.sub = self.repo / 'vendor' / 'lib'
        self.base = self.git(self.repo, 'rev-parse', 'HEAD').strip()

    def patches(self):
        return check_mod._submodule_patches(self.repo, 'vendor/lib', self.base, self.env)

    def test_submodule_worktree_diff_keeps_nul_file_as_text(self):
        # Unstaged change in a tracked submodule file: the submodule index and HEAD stay clean.
        (self.sub / 'README.md').write_bytes(BINARY)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assert_term_in(patches[0])

    def test_submodule_index_diff_keeps_nul_file_as_text(self):
        path = self.sub / 'README.md'
        path.write_bytes(BINARY)
        self.git(self.sub, 'add', 'README.md')
        # Restore the worktree so only the submodule index holds the term. The first patch
        # (worktree vs. start) is then empty; the --cached patch is the one under test.
        path.write_text('hello\n')
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assert_term_in(patches[0])


if __name__ == '__main__':
    unittest.main()
