"""The judge sandbox hides the private directory from the process it wraps."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from llm_router import judge_sandbox as js

MARKER = 'marker-228-prompt-body'


class JudgeSandboxTests(unittest.TestCase):
    def _private_tree(self):
        root = tempfile.mkdtemp(prefix='dlr-judge-')
        parent = os.path.join(root, 'parent')
        private = os.path.join(parent, 'private')
        os.makedirs(private)
        prompt = os.path.join(private, 'prompt.md')
        verdict = os.path.join(private, 'verdict.json')
        Path(prompt).write_text(MARKER + '\n')
        Path(verdict).write_text('')
        return root, parent, private, prompt, verdict

    def _assert_rename_does_not_reveal(self, private, verdict, prompt, src, dst, read_path):
        if shutil.which('sandbox-exec') is None:
            profile = js.macos_profile(private, verdict)
            real_private = os.path.realpath(private)
            self.assertIn('(deny file-write* (subpath "%s"))' % real_private, profile)
            self.assertIn('(deny file-write* (literal "%s"))' % os.path.realpath(prompt), profile)
            self.assertIn('(deny file-write* (literal "%s"))' % os.path.dirname(real_private), profile)
            return
        code = (
            'import os\n'
            'from pathlib import Path\n'
            'try:\n'
            '    os.rename({src!r}, {dst!r})\n'
            'except OSError:\n'
            '    raise SystemExit(0)\n'
            'try:\n'
            '    print(Path({read_path!r}).read_text())\n'
            'except OSError:\n'
            '    raise SystemExit(0)\n'
        ).format(src=src, dst=dst, read_path=read_path)
        argv = js.wrap([sys.executable, '-c', code], private, verdict)
        self.assertIsNotNone(argv)
        done = subprocess.run(argv, capture_output=True, text=True)
        self.assertNotIn(MARKER, done.stdout)
        self.assertTrue(os.path.isfile(prompt), 'rename moved the prompt before it could be read')
        self.assertIn(MARKER, Path(prompt).read_text())

    def test_child_cannot_rename_prompt_file_out_and_read_it(self):
        root, _parent, private, prompt, verdict = self._private_tree()
        try:
            escaped = os.path.join(root, 'prompt.md')
            self._assert_rename_does_not_reveal(private, verdict, prompt, prompt, escaped, escaped)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_child_cannot_rename_private_directory_out_and_read_it(self):
        root, _parent, private, prompt, verdict = self._private_tree()
        try:
            escaped = os.path.join(root, 'renamed-private')
            read_path = os.path.join(escaped, 'prompt.md')
            self._assert_rename_does_not_reveal(private, verdict, prompt, private, escaped, read_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_child_cannot_rename_parent_directory_out_and_read_it(self):
        root, parent, private, prompt, verdict = self._private_tree()
        try:
            escaped = os.path.join(root, 'renamed-parent')
            read_path = os.path.join(escaped, 'private', 'prompt.md')
            self._assert_rename_does_not_reveal(private, verdict, prompt, parent, escaped, read_path)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_sibling_symlink_to_root_is_not_allowlisted(self):
        parent = tempfile.mkdtemp(prefix='dlr-judge-')
        secret = os.path.join(parent, 'secret')
        sibling = os.path.join(parent, 'sibling')
        link = os.path.join(parent, 'root-link')
        os.mkdir(secret)
        os.mkdir(sibling)
        os.symlink('/', link)
        try:
            allowed = js.paths_outside(secret)
            self.assertIn(os.path.realpath(sibling), allowed)
            self.assertEqual([path for path in allowed if os.path.realpath(path) == '/'], [])
            secret_real = Path(os.path.realpath(secret))
            for path in allowed:
                resolved = Path(os.path.realpath(path))
                self.assertFalse(
                    secret_real.is_relative_to(resolved) or resolved.is_relative_to(secret_real),
                    path,
                )
        finally:
            os.unlink(link)
            shutil.rmtree(parent)

    def test_allow_list_never_covers_the_secret_or_its_ancestors(self):
        secret = tempfile.mkdtemp()
        sibling = tempfile.mkdtemp()
        try:
            secret_real = os.path.realpath(secret)
            allowed = js.paths_outside(secret)
            self.assertIn(os.path.realpath(sibling), allowed)
            self.assertNotIn(os.path.realpath(os.path.dirname(secret_real)), allowed)
            secret_path = Path(secret_real)
            for path in allowed:
                resolved = Path(os.path.realpath(path))
                self.assertFalse(secret_path.is_relative_to(resolved) or resolved.is_relative_to(secret_path))
        finally:
            os.rmdir(secret)
            os.rmdir(sibling)

    def test_wrap_uses_the_platform_sandbox(self):
        private = tempfile.mkdtemp()
        verdict = Path(private) / 'verdict.json'
        verdict.write_text('')
        try:
            argv = js.wrap([sys.executable, '-c', 'print(1)'], private, verdict)
            self.assertIsNotNone(argv)
            if sys.platform == 'darwin':
                self.assertTrue(argv[0].endswith('sandbox-exec'))
                self.assertIn('deny file-read*', argv[2])
                self.assertIn(os.path.realpath(private), argv[2])
                self.assertNotIn('nonce', argv[2])
            else:
                self.assertEqual(argv[0], sys.executable)
                self.assertIn('confine', argv[2])
                self.assertEqual(argv[3], os.path.realpath(private))
        finally:
            shutil.rmtree(private)


if __name__ == '__main__':
    unittest.main()
