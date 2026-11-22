"""#314: job_dispatch git calls that read the worktree/index use _isolated_git_env."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fake_worker import FakeWorkerHarness
from helpers import load_policy
from llm_router import job_dispatch


def _git_subcommand(args):
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == '-c':
            index += 2
            continue
        if arg.startswith('-'):
            index += 1
            continue
        if re.fullmatch(r'[a-z0-9-]+', arg):
            return arg
        break
    return ''


class GitEnv314Tests(unittest.TestCase):
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
        self.base_sha = self.git(self.repo, 'rev-parse', 'HEAD').strip()
        self.policy = load_policy()
        rt, rj = self.policy['run_task'], self.policy['run_job']
        rt.update(ledger=str(self.root / 'ledger.csv'), output_dir=str(self.root / 'worker-out'),
                  block_log=str(self.root / 'block.log'), shim_dir=str(self.root / 'shims'))
        rj.update(worktree_root=str(self.root / 'wt'), lock_dir=str(self.root / 'locks'),
                  jobs_dir=str(self.root / 'jobs'), repo_config=str(self.root / 'repos.json'))
        self.policy['alerts'] = {'file': str(self.root / 'alerts.jsonl'), 'macos_notification': False}
        rj['caffeinate'] = False
        for name in ('codex', 'grok-build', 'cursor'):
            FakeWorkerHarness('fail').install(self.policy, name)

    def _restore_env(self):
        if self._saved_global is None:
            os.environ.pop('GIT_CONFIG_GLOBAL', None)
        else:
            os.environ['GIT_CONFIG_GLOBAL'] = self._saved_global
        if self._saved_protocol is None:
            os.environ.pop('GIT_ALLOW_PROTOCOL', None)
        else:
            os.environ['GIT_ALLOW_PROTOCOL'] = self._saved_protocol

    def git(self, cwd, *args, env=None):
        run_env = os.environ.copy() if env is None else env
        done = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, env=run_env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def _script(self, body):
        path = self.root / 'driver.sh'
        path.write_text('#!/bin/sh\n' + body)
        path.chmod(0o755)
        return path

    def _marker_line(self):
        self.marker_token = uuid.uuid4().hex
        return f'printf "%s\\n" "{self.marker_token}" >> "{self.marker}"\n'

    def _install_clean(self):
        script = self._script(self._marker_line() + 'sed "s/DIRTY/filtered/g"\n')
        self.config_path.write_text(
            f'[filter "hide"]\n\tclean = {script}\n[user]\n\tname = t\n\temail = t@t\n')

    def _plant_stat_dirty(self, worktree):
        """Tracked file whose worktree bytes differ from the index (stat-dirty)."""
        self._install_clean()
        (worktree / '.gitattributes').write_text('*.txt filter=hide\n')
        (worktree / 'secret.txt').write_text('DIRTY\n')
        self.git(worktree, 'add', '--', '.gitattributes', 'secret.txt')
        self.git(worktree, 'commit', '-qm', 'track secret')
        (worktree / 'secret.txt').write_text('DIRTY\n')
        self.marker.unlink(missing_ok=True)

    def _assert_isolated(self, env):
        self.assertIsNotNone(env)
        self.assertEqual(env.get('GIT_CONFIG_GLOBAL'), os.devnull)
        self.assertEqual(env.get('GIT_CONFIG_SYSTEM'), '/dev/null')
        self.assertEqual(env.get('GIT_CONFIG_NOSYSTEM'), '1')

    def _worktree_reads(self, worktree):
        """Git steps run-job uses to read the job tree/index."""
        job_dispatch.changed_paths(worktree)
        self.assertFalse(self.marker.exists(), 'changed_paths ran ambient clean filter')
        self.marker.unlink(missing_ok=True)

        job_dispatch._has_changes(worktree)
        self.assertFalse(self.marker.exists(), 'status in _has_changes ran ambient clean filter')
        self.marker.unlink(missing_ok=True)

        job_dispatch.remove_bytecode_caches(worktree)
        self.assertFalse(self.marker.exists(), 'ls-tree ran ambient driver')
        self.marker.unlink(missing_ok=True)

        job_dispatch._git(worktree, 'status', '--porcelain', '--untracked-files=all', env=os.environ.copy())
        self.assertTrue(self.marker.exists(), 'control: ambient status must run clean filter')
        self.marker.unlink(missing_ok=True)

        iso = job_dispatch._isolated_git_env()
        job_dispatch._git(worktree, 'diff', '--no-color', '--no-ext-diff', '--no-textconv', self.base_sha, env=iso)
        self.assertFalse(self.marker.exists(), 'isolated diff ran ambient driver')
        self.marker.unlink(missing_ok=True)

        job_dispatch._git(worktree, 'reset', '-q', env=iso)
        self.assertFalse(self.marker.exists(), 'reset ran ambient driver')
        self.marker.unlink(missing_ok=True)

    def test_stat_dirty_clean_filter_not_run_on_tree_reads(self):
        worktree = self.repo
        self._plant_stat_dirty(worktree)
        self._worktree_reads(worktree)

    def test_run_job_commit_path_uses_isolated_git(self):
        task = self.root / 'task.md'
        task.write_text('# Add out.txt\n')
        code = "open('out.txt','w').write('ok\\n')"
        self.policy['run_task']['commands']['codex'] = [sys.executable, '-c', code, '-']
        select = lambda _chars, **_kw: {'eligible': ['codex'], 'meters': {'codex': 10}, 'reason': 'test'}
        sites = []

        real_git = job_dispatch._git

        def spy(cwd, *args, **kwargs):
            cmd = _git_subcommand(args)
            if kwargs.get('env') is not None and cmd in (
                    'status', 'diff', 'reset', 'commit', 'add', 'ls-tree'):
                sites.append((cmd, kwargs['env'].get('GIT_CONFIG_GLOBAL')))
            return real_git(cwd, *args, **kwargs)

        with mock.patch.object(job_dispatch, '_git', side_effect=spy):
            result = job_dispatch.run_job(self.repo, task, policy=self.policy, test_cmd='test -f out.txt',
                                          select=select)
        self.assertEqual(result['status'], 'committed', result)
        for cmd, global_cfg in sites:
            self.assertEqual(global_cfg, os.devnull, f'git {cmd} was not isolated')
        self.assertTrue(any(cmd == 'add' for cmd, _ in sites), f'missing isolated staging: {sites}')
        self.assertTrue(any(cmd == 'commit' for cmd, _ in sites), f'missing isolated commit: {sites}')

    def test_spy_records_isolated_env_at_tree_read_sites(self):
        worktree = self.repo
        self._plant_stat_dirty(worktree)
        recorded = []
        real_git = job_dispatch._git
        tree_cmds = frozenset(('status', 'diff', 'reset', 'commit', 'add', 'ls-tree'))

        def spy(cwd, *args, **kwargs):
            cmd = _git_subcommand(args)
            if cmd in tree_cmds and kwargs.get('env'):
                recorded.append((cmd, list(args), kwargs['env'].get('GIT_CONFIG_GLOBAL')))
            return real_git(cwd, *args, **kwargs)

        with mock.patch.object(job_dispatch, '_git', side_effect=spy):
            job_dispatch.main_state(worktree)
            job_dispatch.changed_paths(worktree)
            job_dispatch._has_changes(worktree)
            job_dispatch.remove_bytecode_caches(worktree)
            iso = job_dispatch._isolated_git_env()
            job_dispatch._git(worktree, 'diff', '--no-color', self.base_sha, env=iso)
            job_dispatch._git(worktree, 'reset', '-q', env=iso)
            (worktree / 'extra.txt').write_text('x\n')
            job_dispatch._git(worktree, 'add', 'extra.txt', env=iso)
            identity = self.policy['run_job']['commit_identity']
            job_dispatch._git(worktree, '-c', f'user.name={identity["name"]}',
                              '-c', f'user.email={identity["email"]}', 'commit', '-q', '-m', 'probe',
                              env=iso)

        cmds = {entry[0] for entry in recorded}
        self.assertIn('status', cmds)
        self.assertIn('ls-tree', cmds)
        self.assertIn('diff', cmds)
        self.assertIn('reset', cmds)
        self.assertIn('commit', cmds)
        for cmd, _argv, global_cfg in recorded:
            self.assertEqual(global_cfg, os.devnull, f'git {cmd} env was not isolated')


if __name__ == '__main__':
    unittest.main()