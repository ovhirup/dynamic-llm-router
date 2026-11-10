"""Bypass tests for the Claude guard mod (#215), run against both the Python mirror and the real node hook.

Every case runs twice: through `claude_guard.evaluate_tool_call` and through `node guard.mjs` fed the PreToolUse
event on stdin, the way Claude Code runs it (exit 2 blocks, exit 0 allows). The fixture lives under the system
temp dir, which on macOS is a symlink (/var -> /private/var), with a fake HOME holding the secrets.
"""
import glob
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

from helpers import load_policy
from llm_router import claude_guard, task_dispatch as td

NODE = shutil.which('node')


def blocked(tool, tool_input, cwd=None):
    """cwd: a worktree subdirectory the call runs from (default: the worktree itself)."""
    return (tool, tool_input, False, cwd)


def allowed(tool, tool_input, cwd=None):
    return (tool, tool_input, True, cwd)


CASES = [
    # rm -rf split by newlines, continuations, separate or upper-case flags, long flags, variables, symlinks
    blocked('Bash', {'command': 'rm -rf {outside}\necho done'}),
    blocked('Bash', {'command': 'rm -rf \\\n{outside}'}),
    blocked('Bash', {'command': 'rm -r -f {outside}'}),
    blocked('Bash', {'command': 'rm -Rf {outside}'}),
    blocked('Bash', {'command': 'rm --recursive --force {outside}'}),
    blocked('Bash', {'command': 'rm -rf "$HOME"'}),
    blocked('Bash', {'command': 'rm -rf escape/'}),
    blocked('Bash', {'command': 'bash -c "rm -r -f {outside}"'}),
    blocked('Bash', {'command': 'echo "$(rm -rf {outside})"'}),
    blocked('Bash', {'command': 'cd /; rm -rf {outside}'}),
    blocked('Bash', {'command': 'cd {outside}/.. && rm -rf outside'}),
    # rm hidden behind keywords, wrappers with option values, xargs, braces, ~user, globs that climb, symlink + ..
    blocked('Bash', {'command': 'if true; then rm -rf {outside}; fi'}),
    blocked('Bash', {'command': '! rm -rf {outside}'}),
    blocked('Bash', {'command': 'nice -n 5 rm -rf {outside}'}),
    blocked('Bash', {'command': 'sudo -u root rm -rf {outside}'}),
    blocked('Bash', {'command': 'timeout -s KILL 5 rm -rf {outside}'}),
    blocked('Bash', {'command': 'caffeinate rm -rf {outside}'}),
    blocked('Bash', {'command': 'echo {outside} | xargs rm -rf'}),
    blocked('Bash', {'command': 'rm -rf {{outside},}'}),
    blocked('Bash', {'command': 'rm -rf ~root/x'}),
    blocked('Bash', {'command': 'rm -rf ./*/../../x'}),
    blocked('Bash', {'command': 'rm -rf */x'}),
    blocked('Bash', {'command': 'rm -rf escape/../victim'}),
    # ANSI-C quoting, variable-built flags, nested shells (#215 revise 1)
    blocked('Bash', {'command': "$'rm' -rf {outside}"}),
    blocked('Bash', {'command': "rm $'-rf' {outside}"}),
    blocked('Bash', {'command': "$'\\x72m' -rf {outside}"}),
    blocked('Bash', {'command': 'f=rf; rm -$f {outside}'}),
    blocked('Bash', {'command': 'F=-rf; rm $F {outside}'}),
    blocked('Bash', {'command': "sh -c 'rm -rf {outside}'"}),
    blocked('Bash', {'command': 'bash -lc "rm -rf {outside}"'}),
    blocked('Bash', {'command': 'env bash -c "cd ..; rm -rf outside"'}),
    blocked('Bash', {'command': '/bin/bash --norc -c "rm -r -f {outside}"'}),
    blocked('Bash', {'command': 'rm -rf dang/../victim'}),
    # a redirection is not a separator: Bash still passes every later operand to rm (#215)
    blocked('Bash', {'command': 'rm -r -f inside >/dev/null ../outside'}),
    blocked('Bash', {'command': 'rm -r -f inside </dev/null ../outside'}),
    blocked('Bash', {'command': 'rm -r -f inside >>/dev/null ../outside'}),
    blocked('Bash', {'command': 'rm -r -f inside 2>/dev/null ../outside'}),
    blocked('Bash', {'command': 'rm -r -f inside &>/dev/null ../outside'}),
    allowed('Bash', {'command': 'rm -rf build/cache >/dev/null'}),
    # secrets reached through symlinks or Grep/Glob patterns and search roots
    blocked('Read', {'file_path': 'notes.txt'}),
    blocked('Read', {'file_path': 'cfglink/config.json'}),
    blocked('Bash', {'command': 'cat notes.txt'}),
    blocked('Bash', {'command': 'cat cfglink/config.json'}),
    blocked('Grep', {'pattern': 'sk-', 'glob': '**/.env*'}),
    blocked('Grep', {'pattern': 'sk-', 'path': 'cfglink'}),
    blocked('Grep', {'pattern': 'sk-', 'path': '{home}'}),
    blocked('Glob', {'pattern': '**/*.p8'}),
    blocked('Glob', {'pattern': '{home}/.codex/*'}),
    blocked('Glob', {'pattern': '*', 'path': '{home}/.config/llm-router'}),
    blocked('Grep', {'pattern': 'sk-', 'glob': '.e?v'}),
    blocked('Bash', {'command': 'cat .e?v'}),
    blocked('Bash', {'command': 'cat {home}/.cod?x/auth.json'}),
    blocked('Bash', {'command': 'cat {home}/.config/llm-rout*/config.json'}),
    blocked('Bash', {'command': 'cat cfglink/../llm-router/config.json'}),
    # recursive searches that reach secrets (the worktree holds .env and config/keys/auth.json)
    blocked('Bash', {'command': 'grep -r sk- ~'}),
    blocked('Bash', {'command': 'grep -rn sk- {home}'}),
    blocked('Bash', {'command': 'rg sk- {home}'}),
    blocked('Bash', {'command': 'grep -R SECRET .'}),
    blocked('Bash', {'command': 'find . -name "*.json" -exec cat {} +'}),
    blocked('Glob', {'pattern': '**/*.json'}),
    blocked('Grep', {'pattern': 'sk-', 'glob': '**/*.json'}),
    blocked('Grep', {'pattern': 'SECRET'}),
    # recursive readers fed at runtime, find's leading options, attached grep patterns, negated globs, .git
    # (run from src/, which holds no secret, so a fallback check of "." cannot hide a missed path)
    blocked('Bash', {'command': 'echo {home} | xargs grep -r sk-'}, cwd='src'),
    blocked('Bash', {'command': 'echo {home} | xargs rg sk-'}, cwd='src'),
    blocked('Bash', {'command': 'grep -r sk- $DIR'}, cwd='src'),
    blocked('Bash', {'command': 'grep -r sk- "$(echo ~)"'}, cwd='src'),
    blocked('Bash', {'command': "find -L {home} -name '*.toml'"}, cwd='src'),
    blocked('Bash', {'command': 'find -H -P {home}'}, cwd='src'),
    blocked('Bash', {'command': 'grep -r -esk- {home}'}, cwd='src'),
    blocked('Bash', {'command': 'grep -resk- {home}'}, cwd='src'),
    blocked('Bash', {'command': 'grep -rfpats.txt {home}'}, cwd='src'),
    blocked('Grep', {'pattern': 'SECRET', 'glob': '!*.py'}),
    blocked('Grep', {'pattern': 'SECRET', 'path': 'vendor', 'glob': '!*.md'}),
    blocked('Bash', {'command': 'grep -R sk- vendor'}),
    # final rule: a reader whose options are not all fully spelled known options is blocked; so is a recursive
    # reader after a pipe or a runtime feeder; the Grep tool always walks (review vectors on 8c7dc1f)
    blocked('Bash', {'command': 'grep --reg=sk- app.py'}, cwd='src'),
    blocked('Bash', {'command': 'grep -r --fi=pats {home}'}, cwd='src'),
    blocked('Bash', {'command': 'grep -drecurse sk- {home}'}, cwd='src'),
    blocked('Bash', {'command': 'grep --directories recurse sk- {home}'}, cwd='src'),
    blocked('Bash', {'command': 'grep --recur sk- {home}'}, cwd='src'),
    blocked('Grep', {'pattern': 'sk-', 'path': 'config', 'glob': '*.{py,{json,txt}}'}),
    blocked('Bash', {'command': 'echo {home} | parallel grep -r sk-'}, cwd='src'),
    blocked('Bash', {'command': 'find -files0-from list.txt'}, cwd='src'),
    blocked('Bash', {'command': 'cat list.txt | grep -r sk-'}, cwd='src'),
    blocked('Bash', {'command': 'ack -x sk-'}, cwd='src'),
    blocked('Bash', {'command': 'rg --files-from list.txt sk-'}, cwd='src'),
    # quoted names with trailing spaces, ~/link/.., dangling symlinks, other letter case
    blocked('Read', {'file_path': 'notes2.txt '}),
    blocked('Bash', {'command': 'cat "notes2.txt "'}),
    blocked('Read', {'file_path': '~/link/../config.toml'}),
    blocked('Bash', {'command': 'cat ~/link/../config.toml'}),
    blocked('Read', {'file_path': 'dang/../x'}),
    blocked('Read', {'file_path': '{home}/.Codex/config.toml'}),
    # still blocked, as before
    blocked('Read', {'file_path': '{home}/.codex/auth.json'}),
    blocked('Bash', {'command': 'git push origin HEAD'}),
    blocked('Bash', {'command': 'git -C . push'}),
    blocked('Bash', {'command': 'git reset --hard HEAD~1'}),
    blocked('Bash', {'command': 'gh pr merge 42 --squash'}),
    blocked('Bash', {'command': 'rm -rf {outside}'}),
    # ordinary work stays allowed
    allowed('Bash', {'command': 'rm -rf build/cache'}),
    allowed('Bash', {'command': 'rm -r -f build/cache\necho ok'}),
    allowed('Bash', {'command': 'rm -rf build/*'}),
    allowed('Bash', {'command': 'python -m pytest -q'}),
    allowed('Bash', {'command': 'git log --oneline -3'}),
    allowed('Read', {'file_path': '.env.example'}),
    allowed('Read', {'file_path': 'src/app.py'}),
    allowed('Grep', {'pattern': 'def ', 'path': 'src'}),
    allowed('Glob', {'pattern': '**/*.py'}),
    allowed('Bash', {'command': 'ls src/*.py'}),
    allowed('Bash', {'command': 'git rm -r --cached build/cache'}),
    allowed('Bash', {'command': 'grep -rn def src'}),
    allowed('Bash', {'command': 'rg def src'}),
    allowed('Grep', {'pattern': 'def', 'path': 'src', 'glob': '*.py'}),
    allowed('Grep', {'pattern': 'def', 'path': 'src', 'glob': '!*.md'}),
    allowed('Bash', {'command': 'find . -name "*.py"'}, cwd='src'),
    allowed('Bash', {'command': 'find . -name "*.py" -exec grep -n def {} +'}, cwd='src'),
    allowed('Bash', {'command': 'git log --oneline | grep fix'}),
    allowed('Bash', {'command': 'grep --color=auto -n def src/app.py'}),
    allowed('Bash', {'command': 'grep -r --include=*.py --line-number def src'}),
    allowed('Bash', {'command': 'grep -rn -e def src'}),
]

RESOLVE_PARITY = ['/tmp/../x', 'escape/../y', 'cfglink/../z', 'missing/../w', 'notes.txt', '.', '~/.codex/../q',
                  '~/link/../q', 'dang/../w', 'notes2.txt ']
MALFORMED = ['{}', '{"tool_name": "Bash"}', '{"tool_name": "Bash", "tool_input": []}',
             '{"tool_name": "Bash", "tool_input": "ls"}', '{"tool_input": {"command": "ls"}}',
             '{"tool_name": "", "tool_input": {"command": "ls"}}']


def discover_pretooluse_commands(root):
    """PreToolUse hook commands the way Claude Code finds them: .claude-plugin/plugin.json's `hooks` (a path or an
    inline object), else the default hooks/hooks.json."""
    manifest = json.loads((root / '.claude-plugin' / 'plugin.json').read_text())
    hooks = manifest.get('hooks', './hooks/hooks.json')
    config = json.loads((root / hooks).read_text()) if isinstance(hooks, str) else hooks
    config = config.get('hooks', config)
    return [hook['command'] for entry in config.get('PreToolUse', []) for hook in entry['hooks']
            if hook.get('type') == 'command' and entry.get('matcher', '*') in ('*', '')]


class GuardFixture:
    """worktree/ (src/app.py, build/cache/, inside/, .env, .env.example, config/keys/auth.json, notes.txt and
    'notes2.txt ' -> ~/.codex/auth.json, cfglink -> ~/.config/llm-router, escape -> ../outside, dang -> a missing
    path), outside/, home/.codex (+ sub/), home/link -> ~/.codex/sub, home/.config/llm-router."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()  # under /var/folders on macOS, i.e. through a symlink
        root = Path(self.tmp.name)
        self.home, self.worktree, self.outside = root / 'home', root / 'worktree', root / 'outside'
        (self.home / '.codex').mkdir(parents=True)
        (self.home / '.codex' / 'auth.json').write_text('{}')
        (self.home / '.config' / 'llm-router').mkdir(parents=True)
        (self.home / '.config' / 'llm-router' / 'config.json').write_text('{}')
        for path in ('src', 'build/cache', 'inside'):
            (self.worktree / path).mkdir(parents=True)
        (self.worktree / 'src' / 'app.py').write_text('')
        (self.worktree / '.env.example').write_text('')
        (self.worktree / '.env').write_text('SECRET=1\n')
        self.outside.mkdir()
        (self.worktree / 'notes.txt').symlink_to(self.home / '.codex' / 'auth.json')
        (self.worktree / 'cfglink').symlink_to(self.home / '.config' / 'llm-router')
        (self.worktree / 'escape').symlink_to(self.outside)
        (self.worktree / 'notes2.txt ').symlink_to(self.home / '.codex' / 'auth.json')
        (self.worktree / 'dang').symlink_to(root / 'missing-dir' / 'a' / 'b')
        (self.worktree / 'config' / 'keys').mkdir(parents=True)
        (self.worktree / 'config' / 'keys' / 'auth.json').write_text('{}')
        (self.home / '.codex' / 'sub').mkdir()
        (self.home / 'link').symlink_to(self.home / '.codex' / 'sub')
        (self.worktree / 'vendor' / '.git').mkdir(parents=True)  # a symlink to a secret hidden inside .git
        (self.worktree / 'vendor' / '.git' / 'creds').symlink_to(self.home / '.codex' / 'auth.json')
        (self.worktree / 'vendor' / 'lib.py').write_text('')

    def fill(self, value):
        if isinstance(value, dict):
            return {k: self.fill(v) for k, v in value.items()}
        return value.replace('{outside}', str(self.outside)).replace('{home}', str(self.home))

    def env(self):
        return dict(os.environ, HOME=str(self.home), DLR_WORKTREE=str(self.worktree))

    def run_hook(self, guard, event, raw=None, cwd=None, env=None):
        return subprocess.run(['node', str(guard)], input=raw if raw is not None else json.dumps(event),
                              capture_output=True, text=True, cwd=cwd or self.worktree, env=env or self.env(),
                              timeout=30)

    def cleanup(self):
        self.tmp.cleanup()


class GuardBypassTests(unittest.TestCase):
    def setUp(self):
        self.fx = GuardFixture()
        self.addCleanup(self.fx.cleanup)
        self.guard = claude_guard.plugin_dir() / 'guard.mjs'
        self.assertIsNotNone(NODE, 'node must be installed: the guard hook runs under node (a skip is not a pass)')

    def test_python_mirror_blocks_every_bypass(self):
        old_home = os.environ.get('HOME')
        os.environ['HOME'] = str(self.fx.home)
        self.addCleanup(lambda: os.environ.__setitem__('HOME', old_home) if old_home is not None
                        else os.environ.pop('HOME', None))
        for tool, tool_input, allow, sub in CASES:
            tool_input = self.fx.fill(tool_input)
            with self.subTest(tool=tool, input=tool_input, cwd=sub):
                verdict = claude_guard.evaluate_tool_call(tool, tool_input, worktree=self.fx.worktree,
                                                          cwd=self.fx.worktree / sub if sub else None)
                self.assertEqual(verdict['allow'], allow, verdict)

    def test_node_hook_blocks_every_bypass(self):
        for tool, tool_input, allow, sub in CASES:
            tool_input = self.fx.fill(tool_input)
            where = self.fx.worktree / sub if sub else self.fx.worktree
            with self.subTest(tool=tool, input=tool_input, cwd=sub):
                done = self.fx.run_hook(self.guard, {'tool_name': tool, 'tool_input': tool_input, 'cwd': str(where)},
                                        cwd=where)
                self.assertEqual(done.returncode, 0 if allow else 2, done.stderr)
                if allow:
                    self.assertEqual((done.stdout, done.stderr), ('', ''), 'an allow prints nothing, grants nothing')
                else:
                    self.assertIn('dlr-claude-guard', done.stderr)

    def test_hook_runs_from_paths_with_spaces_and_symlinks(self):
        spaced = Path(self.fx.tmp.name) / 'plugin dir with spaces' / 'claude-guard'
        shutil.copytree(claude_guard.plugin_dir(), spaced)
        linked = Path(self.fx.tmp.name) / 'guard-link'
        linked.symlink_to(spaced, target_is_directory=True)
        event = {'tool_name': 'Read', 'tool_input': {'file_path': str(self.fx.home / '.codex' / 'auth.json')}}
        for guard in (spaced / 'guard.mjs', linked / 'guard.mjs', Path(os.path.realpath(spaced)) / 'guard.mjs'):
            with self.subTest(guard=str(guard)):
                done = self.fx.run_hook(guard, event)
                self.assertEqual(done.returncode, 2, f'guard did not run from {guard}: {done.stderr}')

    def test_python_and_node_resolve_paths_the_same_way(self):
        old_home = os.environ.get('HOME')
        os.environ['HOME'] = str(self.fx.home)
        self.addCleanup(lambda: os.environ.__setitem__('HOME', old_home) if old_home is not None
                        else os.environ.pop('HOME', None))
        lib = (claude_guard.plugin_dir() / 'guard-lib.mjs').as_uri()
        script = (f"const m = await import({json.dumps(lib)}); const [wt, ...items] = JSON.parse(process.argv[1]);"
                  "console.log(JSON.stringify(items.map((t) => m.resolveReal(t, wt))));")
        done = subprocess.run(['node', '--input-type=module', '-e', script,
                               json.dumps([str(self.fx.worktree), *RESOLVE_PARITY])],
                              capture_output=True, text=True, env=self.fx.env(), timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        python = [claude_guard.resolve_real(t, self.fx.worktree) for t in RESOLVE_PARITY]
        self.assertEqual(json.loads(done.stdout), python)

    def test_plugin_layout_and_hook_discovery(self):
        root = claude_guard.plugin_dir()
        manifest = json.loads((root / '.claude-plugin' / 'plugin.json').read_text())
        self.assertEqual(manifest['name'], 'dlr-headless-guard')
        self.assertFalse((root / 'plugin.json').exists(), 'a root plugin.json is never loaded by Claude Code')
        commands = discover_pretooluse_commands(root)
        self.assertEqual(len(commands), 1, commands)
        self.assertIn('${CLAUDE_PLUGIN_ROOT}/guard.mjs', commands[0])
        self.assertTrue(commands[0].rstrip().endswith('|| exit 2'), commands[0])
        # every plugin file ships in the wheel: setuptools package-data globs (glob skips dotfiles for '*')
        text = (Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text()
        if tomllib is not None:
            patterns = tomllib.loads(text)['tool']['setuptools']['package-data']['llm_router']
        else:
            block = text.split('[tool.setuptools.package-data]', 1)[1].split('\n[', 1)[0]
            line = next(item for item in block.splitlines() if item.strip().startswith('llm_router'))
            patterns = re.findall(r'"([^"]*)"', line)
        package = root.parents[1]
        shipped = {Path(p) for pattern in patterns for p in glob.glob(str(package / pattern))}
        for path in root.rglob('*'):
            if path.is_file():
                with self.subTest(file=str(path.relative_to(package))):
                    self.assertIn(path, shipped)

    def test_plugin_hook_command_fails_closed_when_node_or_guard_breaks(self):
        command = discover_pretooluse_commands(claude_guard.plugin_dir())[0]
        root = Path(self.fx.tmp.name) / 'plugin'
        shutil.copytree(claude_guard.plugin_dir(), root)
        block_event = json.dumps({'tool_name': 'Read', 'tool_input': {'file_path': '.env'}})
        allow_event = json.dumps({'tool_name': 'Read', 'tool_input': {'file_path': 'src/app.py'}})

        def run(event, **env):
            return subprocess.run(['/bin/sh', '-c', command], input=event, capture_output=True, text=True,
                                  cwd=self.fx.worktree, env=dict(self.fx.env(), CLAUDE_PLUGIN_ROOT=str(root), **env),
                                  timeout=30).returncode
        self.assertEqual(run(block_event), 2)
        self.assertEqual(run(allow_event), 0)
        self.assertEqual(run(allow_event, PATH=str(Path(self.fx.tmp.name) / 'no-node-here')), 2)  # node missing
        (root / 'guard.mjs').write_text('syntax error(\n')
        self.assertEqual(run(allow_event), 2)  # guard.mjs cannot even parse

    def test_malformed_events_fail_closed(self):
        for raw in MALFORMED:
            with self.subTest(stdin=raw):
                done = self.fx.run_hook(self.guard, None, raw=raw)
                self.assertEqual(done.returncode, 2, done.stderr)
        for tool, tool_input in ((None, {'command': 'ls'}), ('', {'command': 'ls'}), ('Bash', None),
                                 ('Bash', []), ('Bash', 'ls')):
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertFalse(claude_guard.evaluate_tool_call(tool, tool_input, worktree=self.fx.worktree)['allow'])

    def test_job_root_is_pinned_and_relative_targets_resolve_against_the_real_cwd(self):
        parent = self.fx.worktree.parent  # Claude ran `cd ..` in an earlier Bash call
        outside = {'tool_name': 'Bash', 'tool_input': {'command': 'rm -rf outside'}, 'cwd': str(parent)}
        self.assertEqual(self.fx.run_hook(self.guard, outside, cwd=parent).returncode, 2)  # DLR_WORKTREE pins it
        verdict = claude_guard.evaluate_tool_call('Bash', {'command': 'rm -rf outside'}, worktree=self.fx.worktree,
                                                  cwd=parent)
        self.assertFalse(verdict['allow'], verdict)
        self.assertTrue(claude_guard.evaluate_tool_call('Bash', {'command': 'rm -rf build/cache'},
                                                        worktree=self.fx.worktree, cwd=self.fx.worktree)['allow'])
    def test_missing_job_root_fails_closed(self):
        # DLR_WORKTREE is required: without it every call is blocked, even an ordinary read (no temp-dir state)
        env = {k: v for k, v in self.fx.env().items() if k != 'DLR_WORKTREE'}
        for event in ({'session_id': 's-1', 'tool_name': 'Read', 'tool_input': {'file_path': 'src/app.py'},
                       'cwd': str(self.fx.worktree)},
                      {'tool_name': 'Bash', 'tool_input': {'command': 'ls'}, 'cwd': str(self.fx.worktree)}):
            with self.subTest(event=event):
                done = self.fx.run_hook(self.guard, event, env=env)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn('DLR_WORKTREE', done.stderr)
        self.assertFalse(claude_guard.evaluate_tool_call('Read', {'file_path': 'src/app.py'})['allow'])
        self.assertNotIn('tmpdir', (claude_guard.plugin_dir() / 'guard-lib.mjs').read_text())

    def test_dispatch_pins_the_job_root_for_the_guard(self):
        policy = load_policy()
        policy['run_task']['claude_guard_mod'] = True
        env = {}
        argv = td.build_argv(policy, 'claude', self.fx.worktree, repo=self.fx.worktree, env=env,
                             claude_version='2.1.309')
        self.assertIn('--plugin-dir', argv)
        self.assertEqual(env.get('DLR_WORKTREE'), str(self.fx.worktree))

    def test_hook_fails_closed_on_bad_input_or_missing_parts(self):
        for raw in ('', 'not json', '[]'):
            with self.subTest(stdin=raw):
                self.assertEqual(self.fx.run_hook(self.guard, None, raw=raw).returncode, 2)
        broken = Path(self.fx.tmp.name) / 'broken'
        shutil.copytree(claude_guard.plugin_dir(), broken)
        (broken / 'guard-lib.mjs').unlink(missing_ok=True)
        done = self.fx.run_hook(broken / 'guard.mjs', {'tool_name': 'Bash', 'tool_input': {'command': 'ls'}})
        self.assertEqual(done.returncode, 2, done.stderr)


if __name__ == '__main__':
    unittest.main()
