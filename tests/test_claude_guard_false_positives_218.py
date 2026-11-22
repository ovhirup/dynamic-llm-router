"""#218: allow harmless rm-related text without weakening bypass coverage."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from llm_router import claude_guard

NODE = shutil.which('node')


class FalsePositive218Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.worktree = cls.root / 'job'
        cls.worktree.mkdir()
        cls.cwd = str(cls.worktree)
        cls.plugin = claude_guard.plugin_dir()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _py(self, command):
        return claude_guard.evaluate_tool_call('Bash', {'command': command}, worktree=self.cwd)

    def _node(self, command):
        if not NODE:
            raise unittest.SkipTest('node not available')
        event = {'tool_name': 'Bash', 'tool_input': {'command': command}}
        env = os.environ.copy()
        env['DLR_WORKTREE'] = self.cwd
        done = subprocess.run([NODE, str(self.plugin / 'guard.mjs')], input=json_dumps(event),
                              capture_output=True, text=True, env=env, cwd=self.cwd, timeout=30)
        return done.returncode, done.stdout, done.stderr

    def assert_allowed(self, command):
        verdict = self._py(command)
        self.assertTrue(verdict.get('allow'), (command, verdict))
        if NODE:
            code, out, err = self._node(command)
            self.assertEqual(code, 0, (command, out, err))

    def assert_blocked(self, command):
        verdict = self._py(command)
        self.assertFalse(verdict.get('allow'), (command, verdict))
        if NODE:
            code, out, err = self._node(command)
            self.assertEqual(code, 2, (command, out, err))

    def test_allow_pycache_glob_echo_and_inert_heredoc(self):
        self.assert_allowed('rm -rf */__pycache__')
        self.assert_allowed('echo rm -rf')
        self.assert_allowed('cat <<EOF\nrm -rf ~\nEOF')

    def test_close_neighbors_stay_blocked(self):
        self.assert_blocked('rm -rf /')
        self.assert_blocked('rm -rf ~')
        self.assert_blocked('rm -rf */')
        self.assert_blocked('echo x; rm -rf ~')
        self.assert_blocked('echo rm -rf ~ | sh')
        self.assert_blocked('$(echo rm) -rf ~')
        self.assert_blocked('cat <<EOF | sh\nrm -rf ~\nEOF')


def json_dumps(obj):
    import json
    return json.dumps(obj)


if __name__ == '__main__':
    unittest.main()