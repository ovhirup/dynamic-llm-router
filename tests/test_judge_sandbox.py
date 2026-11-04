"""The judge sandbox hides the private directory from the process it wraps."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from llm_router import judge_sandbox as js


class JudgeSandboxTests(unittest.TestCase):
    def test_allow_list_never_covers_the_secret_or_its_ancestors(self):
        secret = tempfile.mkdtemp()
        sibling = tempfile.mkdtemp()
        try:
            secret_real = os.path.realpath(secret)
            allowed = js.paths_outside(secret)
            self.assertIn(os.path.realpath(sibling), allowed)
            self.assertNotIn(os.path.realpath(os.path.dirname(secret_real)), allowed)
            for path in allowed:
                self.assertFalse(secret_real == path or secret_real.startswith(path + os.sep)
                                 or path.startswith(secret_real + os.sep))
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
