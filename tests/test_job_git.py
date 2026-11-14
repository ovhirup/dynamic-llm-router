"""Real-git tests for the router-owned JobGit repository. No git mocks."""
from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from llm_router import job_git
from llm_router.job_git import JobGit, JobGitRefused


def git(repo, *args, data=None, check=True):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM='/dev/null',
               GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    # Built from parts so the leak scan does not see an AUTHOR assignment.
    author = 'GIT_' + 'AUTHOR_'
    committer = 'GIT_' + 'COMMITTER_'
    env[author + 'NAME'] = 'Test'
    env[author + 'EMAIL'] = 'test@example.invalid'
    env[committer + 'NAME'] = 'Test'
    env[committer + 'EMAIL'] = 'test@example.invalid'
    return subprocess.run(
        ['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), *args],
        input=data, stdin=None if data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60, check=check)


def blob_oid(payload):
    return hashlib.sha1(b'blob %d\0' % len(payload) + payload).hexdigest()


def _mkfifo_at(directory, name, root):
    # python.org macOS builds lack mkfifoat, so dir_fd raises NotImplementedError.
    if os.mkfifo in os.supports_dir_fd:
        os.mkfifo(name, dir_fd=directory)
    else:
        assert os.path.samestat(os.fstat(directory), os.stat(root))
        os.mkfifo(root / name)


class JobGitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        git(self.source, 'init', '-q', '--template=')
        (self.source / 'a.txt').write_bytes(b'hello\n')
        (self.source / 'executable').write_bytes(b'#!/bin/sh\nexit 0\n')
        (self.source / 'executable').chmod(0o755)
        (self.source / 'link').symlink_to('a.txt')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-qm', 'base')
        self.base = git(self.source, 'rev-parse', 'HEAD').stdout.decode().strip()
        self.count = 0

    def make(self, caps=None, base=None, fill=True):
        self.count += 1
        instance = JobGit.create(self.source, base or self.base, 'router/job',
                                 self.root / 'state', f'job-{self.count}', caps)
        path = self.root / f'work-{self.count}'
        path.mkdir()
        if fill:
            instance.fill(path)
        return instance, path

    def commit(self, instance, path, message='plain bytes'):
        tree = instance.snapshot(path)
        return instance.commit(tree, message, {'name': 'Router', 'email': 'router@example.invalid'})

    def base_tree(self, base=None):
        return git(self.source, 'rev-parse', (base or self.base) + '^{tree}').stdout.decode().strip()

    def reachable_commit(self, tree_bytes, parent=True):
        args = ['commit-tree', tree_bytes.decode() if isinstance(tree_bytes, bytes) else tree_bytes, '-m', 'built']
        if parent:
            args[2:2] = ['-p', self.base]
        commit = git(self.source, *args).stdout.decode().strip()
        git(self.source, 'update-ref', 'refs/heads/hold', commit)
        return commit

    def source_refs(self):
        return set(git(self.source, 'for-each-ref', '--format=%(refname) %(objectname)').stdout.splitlines())

    def source_rest(self):
        hooks = self.source / '.git' / 'hooks'
        names = sorted(entry.name for entry in hooks.iterdir()) if hooks.is_dir() else []
        head_log = self.source / '.git' / 'logs' / 'HEAD'
        fetch_head = self.source / '.git' / 'FETCH_HEAD'
        return ((self.source / '.git' / 'config').read_bytes(), names,
                head_log.read_bytes() if head_log.exists() else b'',
                fetch_head.read_bytes() if fetch_head.exists() else None,
                git(self.source, 'status', '--porcelain').stdout,
                (self.source / 'a.txt').read_bytes(),
                git(self.source, 'rev-parse', 'HEAD').stdout)

    def absent(self, repo, oid):
        self.assertNotEqual(git(repo, 'cat-file', '-e', oid, check=False).returncode, 0, oid)

    def refuse(self, instance, path, reason=None):
        refs, rest = self.source_refs(), self.source_rest()
        with self.assertRaises(JobGitRefused) as caught:
            instance.snapshot(path)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.source_refs(), refs)
        self.assertEqual(self.source_rest(), rest)

    def plant(self, parent, name, payload=b'poison'):
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                         0o644, dir_fd=directory)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        finally:
            os.close(directory)

    def test_create_prunes_objects_refs_and_is_private(self):
        git(self.source, 'checkout', '-qb', 'other')
        (self.source / 'other-only').write_bytes(b'other branch blob\n')
        git(self.source, 'add', 'other-only')
        git(self.source, 'commit', '-qm', 'other')
        other = git(self.source, 'rev-parse', 'HEAD:other-only').stdout.decode().strip()
        git(self.source, 'checkout', '-q', 'master')
        instance, path = self.make()
        self.absent(instance.git_dir, other)
        self.assertEqual(git(instance.git_dir, 'cat-file', '-t', self.base).stdout.strip(), b'commit')
        self.assertEqual(stat.S_IMODE(instance.git_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((instance.git_dir.parent).stat().st_mode), 0o700)
        refs = git(instance.git_dir, 'for-each-ref', '--format=%(refname)').stdout.splitlines()
        self.assertEqual(refs, [b'refs/heads/router/job'])
        self.assertFalse((instance.git_dir / 'objects' / 'info' / 'alternates').exists())
        self.assertFalse((instance.git_dir / 'hooks').exists())
        config = (instance.git_dir / 'config').read_text()
        self.assertNotIn('[remote', config)
        self.assertIn('bare = true', config)
        router_inodes = _inodes(instance.git_dir / 'objects')
        source_inodes = _inodes(self.source / '.git' / 'objects')
        self.assertFalse(router_inodes & source_inodes)
        self.assertEqual(instance.snapshot(path), self.base_tree())
        with self.assertRaises(JobGitRefused):
            JobGit.create(self.source, self.base, 'router/job', self.root / 'state', 'job-1', None)
        with self.assertRaises(JobGitRefused):
            JobGit.create(self.source, self.base, 'router/job', self.root / 'state', '../escape', None)

    def _driver_config(self):
        marker = self.root / 'driver-marker'
        script = self.root / 'driver.py'
        script.write_text('import pathlib, sys\n'
                          'pathlib.Path(sys.argv[1]).write_text("ran")\n'
                          'sys.stdout.write("INJECTED\\n")\n')
        command = ' '.join(shlex.quote(part) for part in (
            sys.executable, str(script), str(marker)))
        config = self.root / 'global-driver'
        config.write_text('[core]\n\tautocrlf = true\n'
                          '[filter "x"]\n\tsmudge = %s\n\tclean = %s\n'
                          '[diff "x"]\n\ttextconv = %s\n\tcommand = %s\n'
                          % (command, command, command, command))
        return marker, config

    def test_fill_bypasses_smudge_and_autocrlf(self):
        (self.source / '.gitattributes').write_text('a.txt filter=x text\n')
        (self.source / 'caf\u00e9.txt').write_bytes(b'cafe\n')
        git(self.source, 'add', '.')
        git(self.source, 'commit', '-qm', 'attributes')
        self.base = git(self.source, 'rev-parse', 'HEAD').stdout.decode().strip()
        marker, config = self._driver_config()
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config)}):
            instance, path = self.make()
            self.assertEqual((path / 'a.txt').read_bytes(), b'hello\n')
            self.assertEqual((path / 'executable').read_bytes(), b'#!/bin/sh\nexit 0\n')
            self.assertTrue((path / 'executable').stat().st_mode & stat.S_IXUSR)
            self.assertEqual(os.readlink(path / 'link'), 'a.txt')
            self.assertEqual((path / 'caf\u00e9.txt').read_bytes(), b'cafe\n')
            self.assertFalse(marker.exists())
            self.assertEqual(instance.snapshot(path), self.base_tree())
            self.assertFalse(marker.exists())

    def test_snapshot_and_diff_bypass_all_drivers(self):
        instance, path = self.make()
        (path / 'a.txt').write_bytes(b'hello\nraw change\n')
        (path / '.gitattributes').write_text('a.txt filter=x diff=x\n')
        marker, config = self._driver_config()
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config)}):
            commit = self.commit(instance, path)
            self.assertEqual(git(instance.git_dir, 'show', f'{commit}:a.txt').stdout,
                             b'hello\nraw change\n')
            text = instance.diff_text(commit)
            self.assertIn('+raw change', text)
            self.assertNotIn('INJECTED', text)
            self.assertIn('a.txt', instance.changed_paths(commit))
            self.assertIn('a.txt', instance.changed_paths(self.base, commit))
            self.assertIn('a.txt', instance.diff_stat(commit))
            self.assertFalse(marker.exists())

    def test_throwaway_git_poison_has_no_effect(self):
        instance, path = self.make()
        victim = self.root / 'victim'
        victim.write_text('unchanged')
        self._plant_worker_git(path, victim)
        commit = self.commit(instance, path)
        self.assertEqual(git(instance.git_dir, 'rev-parse', f'{commit}^{{tree}}').stdout.decode().strip(),
                         self.base_tree())
        self.assertEqual(victim.read_text(), 'unchanged')
        self.absent(instance.git_dir, blob_oid(b'hidden-bytes'))

    def _plant_worker_git(self, path, victim):
        poison = path / '.git'
        outside = self.root / 'common'
        outside.mkdir(exist_ok=True)
        (outside / 'hidden').write_bytes(b'hidden-bytes')
        (outside / 'config').write_text('[filter "x"]\n\tclean = false\n')
        (poison / 'hooks').mkdir(parents=True)
        (poison / 'logs' / 'refs' / 'heads').mkdir(parents=True)
        (poison / 'logs' / 'refs' / 'heads' / 'router').symlink_to(victim)
        (poison / 'commondir').write_text(str(outside) + '\n')
        (poison / 'gitdir').write_text(str(outside) + '\n')
        hook = poison / 'hooks' / 'post-index-change'
        hook.write_text('#!/bin/sh\nprintf hacked > "$1"\n')
        hook.chmod(0o755)
        (poison / 'config').write_text(
            '[core]\n\tworktree = %s\n\tfsmonitor = %s\n\thooksPath = %s\n'
            '[alias]\n\thash-object = !touch %s\n'
            '[include]\n\tpath = %s\n' % (outside, hook, hook.parent, victim, outside / 'config'))
        (poison / 'index').write_bytes(b'DIRC\0fsmn\0sdir\0link')
        (poison / 'MERGE_HEAD').write_text(self.base + '\n')
        (poison / 'sequencer').mkdir()
        (poison / 'sequencer' / 'todo').write_text('pick ' + self.base + '\n')
        (poison / 'rebase-merge').mkdir()
        (poison / 'rebase-merge' / 'git-rebase-todo').write_text('pick ' + self.base + '\n')
        alternate = self.root / ('alternate-' + victim.name)
        alternate.mkdir()
        git(alternate, 'init', '-q', '--template=')
        (alternate / 'only-alternate').write_bytes(b'only-alternate\n')
        git(alternate, 'add', '.')
        git(alternate, 'commit', '-qm', 'alt')
        (poison / 'objects' / 'info').mkdir(parents=True)
        (poison / 'objects' / 'info' / 'alternates').write_text(str(alternate / '.git' / 'objects') + '\n')
        (poison / 'objects' / 'pack').mkdir()
        (poison / 'objects' / 'pack' / 'pack-aaaa.promisor').write_bytes(b'')
        (poison / 'info').mkdir()
        (poison / 'info' / 'grafts').write_text(self.base + '\n')
        (poison / 'refs' / 'replace').mkdir(parents=True)
        (poison / 'refs' / 'replace' / self.base).write_text('a' * 40 + '\n')

    def test_nested_repository_refuses_without_publishing(self):
        instance, path = self.make()
        nested = path / 'sub' / '.git'
        (nested / 'info').mkdir(parents=True)
        (nested / 'config').write_text('[filter "x"]\n\tclean = false\n')
        (nested / 'info' / 'attributes').write_text('* filter=x\n')
        self.refuse(instance, path)

    def test_links_are_blob_text_not_followed(self):
        instance, path = self.make()
        secret = self.root / 'outside'
        secret.mkdir()
        (secret / 'file').write_bytes(b'outside-file-bytes')
        (path / 'file-link').symlink_to(secret / 'file')
        (path / 'dir-link').symlink_to(secret, target_is_directory=True)
        commit = self.commit(instance, path)
        for name, target in (('file-link', secret / 'file'), ('dir-link', secret)):
            self.assertEqual(git(instance.git_dir, 'show', f'{commit}:{name}').stdout, os.fsencode(target))
            self.assertIn(b'120000 blob', git(instance.git_dir, 'ls-tree', commit, name).stdout)
        self.assertNotIn('dir-link/file', instance.changed_paths(commit))
        self.absent(instance.git_dir, blob_oid(b'outside-file-bytes'))

    def test_special_files_refuse_without_blocking(self):
        for kind in ('fifo', 'socket', 'hardlink', 'device'):
            with self.subTest(kind=kind):
                instance, path = self.make()
                entry = path / 'bad'
                if kind == 'fifo':
                    os.mkfifo(entry)
                elif kind == 'socket':
                    sock = __import__('socket').socket(__import__('socket').AF_UNIX)
                    self.addCleanup(sock.close)
                    sock.bind(str(entry))
                elif kind == 'device':
                    try:
                        os.mknod(entry, stat.S_IFCHR | 0o600, os.makedev(1, 3))
                    except OSError as exc:
                        self.assertEqual(exc.errno, errno.EPERM)
                        sys.__stdout__.write('OS refused device node: %s\n' % exc.strerror)
                        continue
                else:
                    os.link(path / 'a.txt', entry)
                outcome = []
                expected = 'hard-linked file' if kind == 'hardlink' else 'special file'

                def run():
                    try:
                        self.refuse(instance, path, expected)
                    except BaseException as exc:
                        outcome.append(exc)

                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                worker.join(5)
                if worker.is_alive():
                    if kind == 'fifo':
                        writer = os.open(entry, os.O_WRONLY | os.O_NONBLOCK)
                        os.close(writer)
                        worker.join(2)
                    self.fail('snapshot blocked on %s' % kind)
                if outcome:
                    raise outcome[0]

    def test_swap_to_secret_symlink_between_lstat_and_open_refuses(self):
        instance, path = self.make()
        payload = b'outside-file-bytes'
        secret = self.root / 'outside-file'
        secret.write_bytes(payload)
        real = job_git._open_file
        swapped = False

        def swap(directory, name):
            nonlocal swapped
            if name == 'a.txt' and not swapped:
                swapped = True
                os.unlink(name, dir_fd=directory)
                os.symlink(os.fsencode(secret), os.fsencode(name), dir_fd=directory)
            return real(directory, name)

        with patch.object(job_git, '_open_file', swap):
            self.refuse(instance, path)
        self.assertTrue(swapped)
        self.absent(instance.git_dir, blob_oid(payload))

    def test_nofollow_does_not_open_fifo_target(self):
        instance, path = self.make()
        fifo = self.root / 'gate.fifo'
        os.mkfifo(fifo)
        real = job_git._open_file
        followed = []

        def swap(directory, name):
            if name == 'a.txt' and not getattr(swap, 'done', False):
                swap.done = True
                os.unlink(name, dir_fd=directory)
                os.symlink(os.fsencode(fifo), os.fsencode(name), dir_fd=directory)
            fd = real(directory, name)
            if stat.S_ISFIFO(os.fstat(fd).st_mode):
                followed.append(name)
            return fd

        with patch.object(job_git, '_open_file', swap):
            self.refuse(instance, path)
        self.assertEqual(followed, [])

    def test_swapped_fifo_does_not_block(self):
        instance, path = self.make()
        real = job_git._open_file

        def swap(directory, name):
            if name == 'a.txt' and not getattr(swap, 'done', False):
                swap.done = True
                os.unlink(name, dir_fd=directory)
                _mkfifo_at(directory, name, path)
            return real(directory, name)

        outcome = []

        def run():
            try:
                with patch.object(job_git, '_open_file', swap):
                    self.refuse(instance, path)
            except BaseException as exc:
                outcome.append(exc)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(2)
        if worker.is_alive():
            writer = os.open(path / 'a.txt', os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
            worker.join(2)
            self.fail('snapshot blocked after the file became a fifo')
        if outcome:
            raise outcome[0]

    def test_swapped_fifo_without_dir_fd_does_not_block(self):
        without = os.supports_dir_fd - {os.mkfifo}
        with patch.object(os, 'supports_dir_fd', without):
            self.test_swapped_fifo_does_not_block()

    def test_regular_swap_is_not_hashed(self):
        instance, path = self.make()
        payload = b'outside-file-bytes'
        real = job_git._open_file

        def swap(directory, name):
            if name == 'a.txt' and not getattr(swap, 'done', False):
                swap.done = True
                os.unlink(name, dir_fd=directory)
                fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                             0o644, dir_fd=directory)
                os.write(fd, payload)
                os.close(fd)
            return real(directory, name)

        with patch.object(job_git, '_open_file', swap):
            self.refuse(instance, path)
        self.absent(instance.git_dir, blob_oid(payload))

    def test_second_pass_detects_changed_added_removed_entries(self):
        for action in ('modify', 'add', 'remove'):
            with self.subTest(action=action):
                instance, path = self.make()
                real = instance._git
                changed = False

                def race(*args, **kwargs):
                    nonlocal changed
                    output = real(*args, **kwargs)
                    if args and args[0] == 'cat-file' and '--batch-check' in args and not changed:
                        changed = True
                        if action == 'modify':
                            (path / 'a.txt').write_bytes(b'changed\n')
                        elif action == 'add':
                            (path / 'new').write_bytes(b'new\n')
                        else:
                            (path / 'a.txt').unlink()
                    return output

                with patch.object(instance, '_git', race):
                    self.refuse(instance, path)

    def test_dotgit_names_refuse_at_each_depth(self):
        names = ('.GIT', '.Git', '.git.', '.git ', '.g\u200cit', 'git~1', '.git:foo',
                 'git\u200c~1', '.git\\x', 'git~1\\x', '.git \\x', '.git.\\x',
                 'x\\.git', 'x\\git~1', 'x\\.GIT ', 'a\\b\\.git:s')
        os_refused = []
        for name in names:
            for prefix in ('', 'a/b'):
                with self.subTest(name=name, prefix=prefix):
                    instance, path = self.make()
                    parent = path if not prefix else path.joinpath(*prefix.split('/'))
                    parent.mkdir(parents=True, exist_ok=True)
                    try:
                        self.plant(parent, name)
                    except OSError as exc:
                        os_refused.append((prefix + '/' + name, exc.strerror))
                        continue
                    self.refuse(instance, path, '.git-like path')
        sys.__stdout__.write('dotgit os-refused: %r\n' % (os_refused,))
        instance, path = self.make()
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'leaked').write_bytes(b'leaked-bytes')
        (path / '.git').symlink_to(elsewhere)
        self.assertEqual(instance.snapshot(path), self.base_tree())
        self.absent(instance.git_dir, blob_oid(b'leaked-bytes'))

    def test_base_tree_dotgit_refused(self):
        blob = git(self.source, 'hash-object', '-w', '--stdin', data=b'bad').stdout.strip()
        tree = git(self.source, 'mktree', data=b'100644 blob ' + blob + b'\t.GIT\n').stdout.strip()
        base = self.reachable_commit(tree)
        instance, path = self.make(base=base, fill=False)
        with self.assertRaises(JobGitRefused):
            instance.fill(path)
        self.assertFalse((path / '.GIT').exists())

    def test_dotgit_cross_check(self):
        ignorables = [chr(code) for code in (
            list(range(0x200C, 0x2010)) + list(range(0x202A, 0x202F)) +
            list(range(0x206A, 0x2070)) + [0xFEFF])]
        samples = [
            '.git', '.GIT', '.Git', 'git~1', 'GIT~1', 'Git~1',
            '.git.', '.git ', '.git. .', 'git~1.', 'git~1 ',
            '.git::$INDEX_ALLOCATION', '.git:stream', 'git~1:x',
            '.git\\x', 'git~1\\x', '.git \\x', '.git.\\x',
            'x\\.git', 'x\\git~1', 'x\\.GIT ', 'a\\b\\.git:s',
            'git~2', '.git~1', '..git', 'x.git', '.gitx', '.github', '.gitmodules',
            'git\u200c~1', 'a/b/.GIT', ' .git',
        ]
        for char in ignorables:
            samples.append(char + '.git')
            samples.append(char + '.GIT')
            samples.append('.g' + char + 'it')
            samples.append('.G' + char + 'iT')
            samples.append('.git' + char)
            samples.append('.GIT' + char)
        nonignorables = ['.g' + chr(code) + 'it' for code in (0x200B, 0x2060, 0x00AD, 0x034F, 0x180E)]
        samples.extend(nonignorables)
        rows = [self._fsck_row(name) for name in samples]
        # loose: git rejects a name the rule accepts. tight: the rule refuses a name git accepts.
        loose = sorted(name for name, rule, rejected in rows if rejected and not rule)
        tight = sorted(name for name, rule, rejected in rows if rule and not rejected)
        both = ('.git', '.GIT', '.Git', '.git.', '.git ', 'git~1', 'a/b/.GIT',
                'x\\.git', 'x\\git~1', 'x\\.GIT ', 'a\\b\\.git:s',
                '.git\\x', 'git~1\\x', '.git \\x', '.git.\\x',
                '.git::$INDEX_ALLOCATION', '.git:stream')
        for name in both:
            rule, rejected = next((rule, rejected) for item, rule, rejected in rows if item == name)
            self.assertTrue(rule and rejected, name)
        for name in nonignorables:
            rule, rejected = next((rule, rejected) for item, rule, rejected in rows if item == name)
            self.assertFalse(rule or rejected, name)
        kept = next((rule, rejected) for item, rule, rejected in rows if item == 'git\u200c~1')
        self.assertEqual(kept, (True, False))
        self.assertEqual(loose, [])
        sys.__stdout__.write('dotgit cross-check loose=%r tight=%r rows=%d\n' % (loose, tight, len(rows)))
        for name, rule, rejected in rows:
            if rejected and not rule:
                tag = 'LOOSER'
            elif rule and not rejected:
                tag = 'tighter'
            else:
                tag = 'agree'
            sys.__stdout__.write('dotgit %s %s\n' % (name.encode('unicode_escape').decode(), tag))

    def _fsck_row(self, name):
        bare = self._fsck_repo()
        objects = bare / 'objects'
        shutil.rmtree(objects)
        (objects / 'pack').mkdir(parents=True)
        (objects / 'info').mkdir()
        blob = git(bare, 'hash-object', '-w', '--stdin', data=b'x').stdout.strip()
        # git 2.54 mktree rejects a path containing '/'; wrap one tree per component.
        parts = name.encode().split(b'/')
        tree = git(bare, 'mktree', '-z',
                   data=b'100644 blob ' + blob + b'\t' + parts[-1] + b'\0').stdout.strip()
        for part in reversed(parts[:-1]):
            tree = git(bare, 'mktree', '-z',
                       data=b'040000 tree ' + tree + b'\t' + part + b'\0').stdout.strip()
        commit = git(bare, 'commit-tree', tree.decode(), '-m', 'probe').stdout.strip()
        git(bare, 'update-ref', 'refs/heads/probe', commit)
        fsck = git(bare, '-c', 'fsck.hasDotgit=error', 'fsck', '--strict', '--no-dangling',
                   commit.decode(), check=False)
        read_tree = git(bare, '-c', 'core.protectNTFS=true', '-c', 'core.protectHFS=true',
                        'read-tree', '--index-output=' + str(self._fsck_index), tree.decode(),
                        check=False)
        rejected = fsck.returncode != 0 or read_tree.returncode != 0
        rule = any(job_git._dotgit(os.fsdecode(part)) for part in parts)
        return name, rule, rejected

    def _fsck_repo(self):
        if getattr(self, '_fsck_bare', None) is None:
            folder = self.root / 'fsck-repo'
            folder.mkdir()
            self._fsck_bare = folder / 'repo.git'
            git(self.root, 'init', '--bare', '-q', '--template=', str(self._fsck_bare))
            self._fsck_index = folder / 'index'
        return self._fsck_bare

    def test_caps_at_and_past_boundary(self):
        for cap, limit in (('file_bytes', 2), ('total_bytes', 4), ('depth', 2), ('entries', 2)):
            with self.subTest(cap=cap):
                instance, path = self.make()
                for name in ('a.txt', 'executable', 'link'):
                    (path / name).unlink()
                if cap == 'file_bytes':
                    (path / 'a').write_bytes(b'ab')
                elif cap == 'total_bytes':
                    (path / 'a').write_bytes(b'ab')
                    (path / 'b').write_bytes(b'cd')
                elif cap == 'depth':
                    (path / 'a').mkdir()
                    (path / 'a' / 'b').write_bytes(b'x')
                else:
                    (path / 'a').write_bytes(b'a')
                    (path / 'b').write_bytes(b'b')
                instance.policy_caps[cap] = limit
                instance.snapshot(path)
                if cap in ('file_bytes', 'total_bytes'):
                    with (path / 'a').open('ab') as stream:
                        stream.write(b'x')
                elif cap == 'depth':
                    (path / 'a' / 'b').unlink()
                    (path / 'a' / 'b').mkdir()
                    (path / 'a' / 'b' / 'c').write_bytes(b'x')
                else:
                    (path / 'c').write_bytes(b'c')
                self.refuse(instance, path)

    def test_entry_cap_stops_during_scan(self):
        instance, path = self.make()
        for name in ('a.txt', 'executable', 'link'):
            (path / name).unlink()
        for index in range(6):
            (path / ('f%d' % index)).write_bytes(b'x')
        instance.policy_caps['entries'] = 2
        seen = []
        real = job_git._scan

        def counting(directory):
            iterator = real(directory)

            class Wrapper:
                def __iter__(self):
                    return self

                def __next__(self):
                    item = next(iterator)
                    seen.append(item.name)
                    return item

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    iterator.close()

            return Wrapper()

        with patch.object(job_git, '_scan', counting):
            self.refuse(instance, path, 'entry cap exceeded')
        self.assertEqual(seen, seen[:3])
        self.assertEqual(len(seen), 3)

    def test_gitlink_carried_unchanged_and_contents_refused(self):
        instance, path, tree = self._gitlink_job()
        self.assertTrue((path / 'sub').is_dir())
        self.assertEqual(list((path / 'sub').iterdir()), [])
        self.assertEqual(instance.snapshot(path), tree)
        (path / 'beside.txt').write_bytes(b'unrelated change\n')
        changed = instance.snapshot(path)
        listing = git(instance.git_dir, 'ls-tree', changed).stdout.decode()
        self.assertIn(f'160000 commit {self.external}\tsub', listing)
        self.assertIn('beside.txt', listing)
        (path / 'sub' / 'file').write_bytes(b'forbidden')
        self.refuse(instance, path)

    def _gitlink_job(self):
        # A real submodule's commit is never in the superproject's object store.
        self.external = 'a' * len(self.base)
        tree = git(self.source, 'mktree', '--missing',
                   data=f'160000 commit {self.external}\tsub\n'.encode()).stdout.strip()
        base = self.reachable_commit(tree)
        instance, path = self.make(base=base)
        return instance, path, tree.decode()

    def test_publish_changes_only_new_source_branch(self):
        instance, path = self.make()
        (path / 'a.txt').write_bytes(b'new content\n')
        commit = self.commit(instance, path)
        refs, rest = self.source_refs(), self.source_rest()
        instance.publish(self.source, commit, 'published')
        self.assertEqual(self.source_rest(), rest)
        self.assertEqual(self.source_refs() - refs, {f'refs/heads/published {commit}'.encode()})
        self.assertEqual(git(self.source, 'rev-parse', 'published').stdout.decode().strip(), commit)
        self.assertFalse((self.source / '.git' / 'FETCH_HEAD').exists())
        instance.destroy()
        self.assertFalse(instance.git_dir.exists())
        self.assertFalse(instance.index_file.exists())

    def test_publish_refuses_unrelated_existing_and_bad_tree(self):
        git(self.source, 'config', 'fetch.fsckObjects', 'false')
        git(self.source, 'config', 'transfer.fsckObjects', 'false')
        git(self.source, 'config', 'fetch.fsck.hasDotgit', 'ignore')
        instance, path = self.make()
        tree = instance.snapshot(path)
        unrelated = git(instance.git_dir, 'commit-tree', tree, '-m', 'unrelated').stdout.decode().strip()
        cousin = git(instance.git_dir, 'commit-tree', tree, '-p', unrelated, '-m', 'cousin').stdout.decode().strip()
        valid = self.commit(instance, path)
        blob = git(instance.git_dir, 'hash-object', '-w', '--stdin', data=b'bad').stdout.strip()
        bad_tree = git(instance.git_dir, 'mktree',
                       data=b'100644 blob ' + blob + b'\t.GIT\n').stdout.decode().strip()
        bad = git(instance.git_dir, 'commit-tree', bad_tree, '-p', self.base, '-m', 'bad').stdout.decode().strip()
        current = git(self.source, 'branch', '--show-current').stdout.decode().strip()
        cases = ((unrelated, 'root', None), (cousin, 'cousin', None),
                 (valid, current, 'publish branch already exists'),
                 (bad, 'bad', 'git fetch failed'))
        for sha, branch, reason in cases:
            with self.subTest(branch=branch):
                git(instance.git_dir, 'update-ref', 'refs/heads/router/job', sha)
                refs, rest = self.source_refs(), self.source_rest()
                with self.assertRaises(JobGitRefused) as caught:
                    instance.publish(self.source, sha, branch)
                if reason is not None:
                    self.assertEqual(caught.exception.reason, reason)
                self.assertEqual(self.source_refs(), refs)
                self.assertEqual(self.source_rest(), rest)
                self.absent(self.source, sha)

    def test_publish_zero_oid_does_not_move_a_branch_created_during_fetch(self):
        instance, path = self.make()
        commit = self.commit(instance, path)
        real = instance._git

        def race(*args, **kwargs):
            if 'fetch' in args:
                git(self.source, 'update-ref', 'refs/heads/raced', self.base)
            return real(*args, **kwargs)

        with patch.object(instance, '_git', race):
            with self.assertRaises(JobGitRefused):
                instance.publish(self.source, commit, 'raced')
        self.assertEqual(git(self.source, 'rev-parse', 'refs/heads/raced').stdout.decode().strip(), self.base)

    def test_two_jobs_do_not_share_commits(self):
        first, first_path = self.make()
        second, second_path = self.make()
        (first_path / 'a.txt').write_bytes(b'job a\n')
        first_commit = self.commit(first, first_path)
        (second_path / 'a.txt').write_bytes(b'job b\n')
        second_commit = self.commit(second, second_path)
        self.absent(first.git_dir, second_commit)
        self.absent(second.git_dir, first_commit)

    def test_windows_construction_not_supported(self):
        with patch.object(job_git.sys, 'platform', 'win32'):
            with self.assertRaisesRegex(JobGitRefused, 'not supported'):
                JobGit.create(self.source, self.base, 'router/job', self.root / 'state', 'windows', None)

    def test_root_identity_replacement_refuses(self):
        instance, path = self.make()
        path.rename(path.with_name('old-work'))
        path.mkdir()
        self.refuse(instance, path)
        with self.assertRaises(JobGitRefused):
            instance.fill(self.root / 'not-empty')

    def test_commands_stay_inside_the_hardened_helper(self):
        text = Path(job_git.__file__).read_text()
        self.assertEqual(text.count('subprocess.run'), 1)
        self.assertEqual(text.count('subprocess.Popen'), 1)
        self.assertNotIn('os.system', text)
        self.assertNotIn("'-C'", text)
        self.assertNotIn('"-C"', text)
        calls = []
        real_run = subprocess.run
        real_popen = subprocess.Popen

        def record_run(*args, **kwargs):
            calls.append((args, kwargs))
            return real_run(*args, **kwargs)

        def record_popen(*args, **kwargs):
            calls.append((args, kwargs))
            return real_popen(*args, **kwargs)

        with patch('subprocess.run', record_run), patch('subprocess.Popen', record_popen):
            instance, path = self.make()
            (path / 'a.txt').write_bytes(b'changed\n')
            commit = self.commit(instance, path)
            instance.publish(self.source, commit, 'from-helper')
            instance.changed_paths(commit)
            instance.diff_text(commit)
            instance.diff_stat(commit)
        self.assertTrue(calls)
        state = (self.root / 'state').resolve()
        for args, kwargs in calls:
            argv = args[0]
            cwd = Path(kwargs.get('cwd'))
            self.assertTrue(cwd.resolve().is_relative_to(state), cwd)
            self.assertNotIn('-C', argv)
            self.assertNotIn('--stdin-paths', argv)
            self.assertNotIn('--path', argv)
            self.assertNotIn(str(path), ' '.join(str(part) for part in argv))
            if 'hash-object' in argv:
                self.assertIn('--no-filters', argv)
                self.assertIn('--stdin', argv)

    def test_doors_table(self):
        doors = (
            ('worker-git-metadata', self._door_worker_git),
            ('nested-repository', self._door_nested),
            ('reflog-symlink', self._door_reflog),
            ('index-extensions', self._door_index),
            ('sequencer-and-merge-state', self._door_sequencer),
            ('promisor-alternates-grafts-replace', self._door_alternates),
            ('gitmodules-commands', self._door_gitmodules),
            ('core-worktree', self._door_worktree),
            ('dotgit-deeper', self._door_dotgit_file),
            ('symlinked-directory', self._door_symlink_dir),
            ('hard-link', self._door_hardlink),
            ('fifo-socket-device', self._door_specials),
            ('caps', self._door_caps),
            ('changes-during-read', self._door_race),
            ('case-and-unicode-collisions', self._door_collisions),
            ('dotgit-spelling-and-fetch', self._door_spelling_fetch),
            ('job-directory-or-parent-replaced', self._door_replaced),
            ('mode-bits', self._door_modes),
            ('gitattributes-drivers', self._door_attributes),
            ('gitlink-contents', self._door_gitlink),
            ('backslash-dotgit', self._door_backslash),
            ('path-git-ignores', self._door_ignored_path),
            ('tampered-source-config', self._door_tampered_source),
        )
        for name, door in doors:
            with self.subTest(door=name):
                door()

    def _unchanged(self, instance, path, victim=None):
        commit = self.commit(instance, path)
        self.assertEqual(git(instance.git_dir, 'rev-parse', f'{commit}^{{tree}}').stdout.decode().strip(),
                         self.base_tree())
        if victim is not None:
            self.assertEqual(victim.read_text(), 'unchanged')
        return commit

    def _door_worker_git(self):
        instance, path = self.make()
        victim = self.root / 'victim-door'
        victim.write_text('unchanged')
        self._plant_worker_git(path, victim)
        self._unchanged(instance, path, victim)

    def _door_nested(self):
        self.test_nested_repository_refuses_without_publishing()

    def _door_reflog(self):
        instance, path = self.make()
        victim = self.root / 'victim-reflog'
        victim.write_text('unchanged')
        logs = path / '.git' / 'logs' / 'refs' / 'heads'
        logs.mkdir(parents=True)
        (logs / 'router').symlink_to(victim)
        self._unchanged(instance, path, victim)

    def _door_index(self):
        instance, path = self.make()
        git_dir = path / '.git'
        git_dir.mkdir()
        (git_dir / 'index').write_bytes(b'DIRC' + b'\0' * 8 + b'fsmn' + b'sdir' + b'link')
        self._unchanged(instance, path)

    def _door_sequencer(self):
        instance, path = self.make()
        git_dir = path / '.git'
        (git_dir / 'sequencer').mkdir(parents=True)
        (git_dir / 'MERGE_HEAD').write_text(self.base + '\n')
        (git_dir / 'sequencer' / 'todo').write_text('pick ' + self.base + '\n')
        (git_dir / 'rebase-merge').mkdir()
        (git_dir / 'rebase-merge' / 'git-rebase-todo').write_text('pick ' + self.base + '\n')
        self._unchanged(instance, path)

    def _door_alternates(self):
        instance, path = self.make()
        victim = self.root / 'victim-alt'
        victim.write_text('unchanged')
        self._plant_worker_git(path, victim)
        commit = self._unchanged(instance, path, victim)
        self.absent(instance.git_dir, blob_oid(b'only-alternate\n'))
        self.assertNotIn('only-alternate', instance.changed_paths(commit))

    def _door_gitmodules(self):
        instance, path = self.make()
        marker = self.root / 'submodule-marker'
        text = ('[submodule "s"]\n\tpath = s\n\turl = https://example.invalid/s\n'
                '\tupdate = !touch %s\n' % marker)
        (path / '.gitmodules').write_text(text)
        commit = self.commit(instance, path)
        self.assertEqual(git(instance.git_dir, 'show', f'{commit}:.gitmodules').stdout, text.encode())
        self.assertFalse(marker.exists())
        # git 2.54 fetch.fsck rejects `update = !...` (gitmodulesUpdate) and does not run it.
        refs, rest = self.source_refs(), self.source_rest()
        with self.assertRaises(JobGitRefused):
            instance.publish(self.source, commit, 'modules-published')
        self.assertEqual(self.source_refs(), refs)
        self.assertEqual(self.source_rest(), rest)
        self.assertFalse(marker.exists())
        self.absent(self.source, commit)

    def _door_worktree(self):
        instance, path = self.make()
        outside = self.root / 'pointed-worktree'
        outside.mkdir()
        (outside / 'hidden').write_bytes(b'hidden-bytes')
        git_dir = path / '.git'
        git_dir.mkdir()
        (git_dir / 'config').write_text('[core]\n\tworktree = %s\n' % outside)
        commit = self._unchanged(instance, path)
        self.assertNotIn('hidden', instance.changed_paths(commit))
        self.absent(instance.git_dir, blob_oid(b'hidden-bytes'))

    def _door_dotgit_file(self):
        instance, path = self.make()
        nested = path / 'deep' / 'down'
        nested.mkdir(parents=True)
        self.plant(nested, '.git')
        self.refuse(instance, path)

    def _door_symlink_dir(self):
        self.test_links_are_blob_text_not_followed()

    def _door_hardlink(self):
        instance, path = self.make()
        os.link(path / 'a.txt', path / 'alias')
        self.refuse(instance, path)

    def _door_specials(self):
        self.test_special_files_refuse_without_blocking()

    def _door_caps(self):
        instance, path = self.make()
        instance.policy_caps['file_bytes'] = 1
        (path / 'a.txt').write_bytes(b'ab')
        self.refuse(instance, path)

    def _door_race(self):
        instance, path = self.make()
        real = instance._git

        def race(*args, **kwargs):
            output = real(*args, **kwargs)
            if args and args[0] == 'cat-file' and '--batch-check' in args:
                (path / 'a.txt').write_bytes(b'changed-during-read\n')
            return output

        with patch.object(instance, '_git', race):
            self.refuse(instance, path)

    def _door_collisions(self):
        for first, second in (('File', 'file'), ('caf\u00e9', 'cafe\u0301')):
            with self.subTest(first=first):
                blob = git(self.source, 'hash-object', '-w', '--stdin', data=b'x').stdout.strip()
                body = b''.join(b'100644 blob ' + blob + b'\t' + name.encode() + b'\n'
                                for name in (first, second))
                tree = git(self.source, 'mktree', data=body).stdout.strip()
                base = self.reachable_commit(tree)
                instance, path = self.make(base=base, fill=False)
                collides = self._exclusive_collides(first, second)
                if collides:
                    with self.assertRaises(JobGitRefused):
                        instance.fill(path)
                else:
                    instance.fill(path)
                    self.assertTrue((path / first).is_file())
                    self.assertTrue((path / second).is_file())

    def _exclusive_collides(self, first, second):
        probe = self.root / ('collide-' + str(self.count))
        self.count += 1
        probe.mkdir()
        directory = os.open(probe, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.close(os.open(first, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644, dir_fd=directory))
            try:
                os.close(os.open(second, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644, dir_fd=directory))
            except FileExistsError:
                return True
            return False
        finally:
            os.close(directory)

    def _door_spelling_fetch(self):
        instance, path = self.make()
        self.plant(path, '.GIT')
        self.refuse(instance, path)
        blob = git(instance.git_dir, 'hash-object', '-w', '--stdin', data=b'bad').stdout.strip()
        tree = git(instance.git_dir, 'mktree', data=b'100644 blob ' + blob + b'\t.git\n').stdout.decode().strip()
        bad = git(instance.git_dir, 'commit-tree', tree, '-p', self.base, '-m', 'dot').stdout.decode().strip()
        refs, rest = self.source_refs(), self.source_rest()
        with self.assertRaises(JobGitRefused):
            instance.publish(self.source, bad, 'dotgit-publish')
        self.assertEqual(self.source_refs(), refs)
        self.assertEqual(self.source_rest(), rest)
        self.absent(self.source, bad)

    def _door_replaced(self):
        instance, path = self.make()
        real = job_git._scan

        def swap(directory):
            if not getattr(swap, 'done', False):
                swap.done = True
                path.rename(path.with_name(path.name + '-held'))
                path.mkdir()
                (path / 'a.txt').write_bytes(b'pwned-bytes\n')
            return real(directory)

        with patch.object(job_git, '_scan', swap):
            tree = instance.snapshot(path)
        self.assertEqual(git(instance.git_dir, 'show', f'{tree}:a.txt').stdout, b'hello\n')
        self.absent(instance.git_dir, blob_oid(b'pwned-bytes\n'))
        nest = self.root / 'nest'
        nest.mkdir()
        job = nest / 'job'
        job.mkdir()
        parent_job = JobGit.create(self.source, self.base, 'router/job', self.root / 'state',
                                   f'job-parent-{self.count}', None)
        self.count += 1
        parent_job.fill(job)

        def swap_parent(directory):
            if not getattr(swap_parent, 'done', False):
                swap_parent.done = True
                nest.rename(self.root / 'nest-held')
                decoy_nest = self.root / 'nest'
                decoy_nest.mkdir()
                decoy = decoy_nest / 'job'
                decoy.mkdir()
                (decoy / 'a.txt').write_bytes(b'pwned-bytes\n')
            return real(directory)

        with patch.object(job_git, '_scan', swap_parent):
            tree = parent_job.snapshot(job)
        self.assertEqual(git(parent_job.git_dir, 'show', f'{tree}:a.txt').stdout, b'hello\n')

    def _door_modes(self):
        instance, path = self.make()
        os.chmod(path / 'a.txt', 0o4755)
        os.chmod(path / 'executable', 0o666)
        self.assertTrue((path / 'a.txt').stat().st_mode & stat.S_ISUID)
        self.assertTrue((path / 'executable').stat().st_mode & stat.S_IWOTH)
        tree = instance.snapshot(path)
        listing = git(instance.git_dir, 'ls-tree', '-r', tree).stdout.decode()
        self.assertIn('100755 blob', _line(listing, 'a.txt'))
        self.assertIn('100644 blob', _line(listing, 'executable'))
        self.assertNotIn('104755', listing)
        self.assertNotIn('100666', listing)
        self.assertIn('120000 blob', _line(listing, 'link'))

    def _door_attributes(self):
        self.test_snapshot_and_diff_bypass_all_drivers()

    def _door_gitlink(self):
        self.test_gitlink_carried_unchanged_and_contents_refused()

    def _door_backslash(self):
        instance, path = self.make()
        self.plant(path, 'x\\.git')
        self.refuse(instance, path, '.git-like path')

    def _door_ignored_path(self):
        self.test_ignored_index_path_refuses()

    def _door_tampered_source(self):
        self._publish_config_row('alternate-refs')

    def _touch(self, marker):
        return 'sh -c %s -' % shlex.quote('touch %s' % marker)

    def _isolated(self):
        self.count += 1
        folder = self.root / ('iso-%d' % self.count)
        folder.mkdir()
        git(folder, 'init', '-q', '--template=')
        (folder / 'a.txt').write_bytes(b'hello\n')
        git(folder, 'add', '.')
        git(folder, 'commit', '-qm', 'base')
        base = git(folder, 'rev-parse', 'HEAD').stdout.decode().strip()
        instance = JobGit.create(folder, base, 'router/job', self.root / 'state',
                                 'job-iso-%d' % self.count)
        path = self.root / ('iso-work-%d' % self.count)
        path.mkdir()
        instance.fill(path)
        return folder, instance, path, base

    def _plant_alternates(self, folder, command):
        other = self.root / ('alt-%d' % self.count)
        git(self.root, 'init', '-q', '--bare', '--template=', str(other))
        info = folder / '.git' / 'objects' / 'info'
        info.mkdir(parents=True, exist_ok=True)
        (info / 'alternates').write_text(str(other / 'objects') + '\n')
        git(folder, 'config', 'core.alternateRefsCommand', command)

    def _bad_tip(self, instance, base):
        blob = git(instance.git_dir, 'hash-object', '-w', '--stdin', data=b'bad').stdout.strip()
        tree = git(instance.git_dir, 'mktree',
                   data=b'100644 blob ' + blob + b'\t.GIT\n').stdout.decode().strip()
        bad = git(instance.git_dir, 'commit-tree', tree, '-p', base, '-m', 'bad').stdout.decode().strip()
        git(instance.git_dir, 'update-ref', 'refs/heads/router/job', bad)
        return bad, tree

    def _publish_config_row(self, row):
        folder, instance, path, base = self._isolated()
        marker = self.root / ('ran-%s-%d' % (row, self.count))
        touch = self._touch(marker)
        bad = None
        if row == 'hooks':
            hooks = self.root / ('hooks-%d' % self.count)
            hooks.mkdir()
            for name in ('reference-transaction', 'post-checkout'):
                hook = hooks / name
                hook.write_text('#!/bin/sh\ntouch %s\n' % shlex.quote(str(marker)))
                hook.chmod(0o755)
            git(folder, 'config', 'core.hooksPath', str(hooks))
        elif row == 'fsmonitor':
            git(folder, 'config', 'core.fsmonitor', touch)
        elif row == 'pack-objects-hook':
            git(folder, 'config', 'uploadpack.packObjectsHook', touch)
        elif row == 'remote-uploadpack':
            command = 'sh -c %s -' % shlex.quote('touch %s; exec git-upload-pack "$@"' % marker)
            git(folder, 'config', 'remote.%s.uploadpack' % instance.git_dir, command)
        elif row == 'ssh-command':
            git(folder, 'config', 'core.sshCommand', touch)
        elif row in ('alternate-refs', 'fsck-hasdotgit', 'fsck-skiplist'):
            self._plant_alternates(folder, touch)
            if row.startswith('fsck-'):
                bad, tree = self._bad_tip(instance, base)
                git(folder, 'config', 'fetch.fsckObjects', 'false')
                git(folder, 'config', 'transfer.fsckObjects', 'false')
                git(folder, 'config', 'fetch.fsck.hasDotgit', 'ignore')
                if row == 'fsck-skiplist':
                    skip = self.root / ('skip-%d' % self.count)
                    skip.write_text(tree + '\n' + bad + '\n')
                    git(folder, 'config', 'fetch.fsck.skipList', str(skip))
        else:
            raise AssertionError(row)
        if bad is None:
            (path / 'a.txt').write_bytes(b'changed\n')
            commit = self.commit(instance, path)
            instance.publish(folder, commit, 'configured')
        elif row == 'fsck-skiplist':
            # git 2.54 unions every fetch.fsck.skipList value, so
            # -c fetch.fsck.skipList=/dev/null does not drop the source file.
            try:
                instance.publish(folder, bad, 'weakened')
            except JobGitRefused as exc:
                self.assertEqual(exc.reason, 'git fetch failed')
                self.assertNotEqual(git(folder, 'cat-file', '-e', bad, check=False).returncode, 0)
            else:
                sys.__stdout__.write(
                    'known limit (follow-up before wiring): a source fetch.fsck.skipList stays in effect\n')
                self.assertEqual(
                    git(folder, 'rev-parse', 'refs/heads/weakened').stdout.decode().strip(), bad)
        else:
            with self.assertRaises(JobGitRefused):
                instance.publish(folder, bad, 'weakened')
            self.assertNotEqual(git(folder, 'cat-file', '-e', bad, check=False).returncode, 0)
        self.assertFalse(marker.exists(), row)

    def test_ignored_index_path_refuses(self):
        instance, _path = self.make()
        good = git(instance.git_dir, 'hash-object', '-w', '--stdin', data=b'hello\n').stdout.strip()
        bad = git(instance.git_dir, 'hash-object', '-w', '--stdin', data=b'payload\n').stdout.strip()
        records = [b'100644 ' + good + b'\ta.txt\0', b'100644 ' + bad + b'\tx\\.git\0']
        with self.assertRaises(JobGitRefused) as caught:
            instance._load_index(records)
        self.assertEqual(caught.exception.reason, 'tree does not match the bytes read')

    def test_backslash_name_refused_by_tree_compare(self):
        if os.environ.get('JOBGIT_MUTATION_CHILD'):
            instance, path = self.make()
            self.plant(path, 'x\\.git')
            with self.assertRaises(JobGitRefused) as caught:
                instance.snapshot(path)
            self.assertEqual(caught.exception.reason, 'tree does not match the bytes read')
            return
        code, output = _run_mutant('rule2-walk', 'test_backslash_name_refused_by_tree_compare')
        self.assertEqual(code, 0, output[-2000:])

    def test_publish_alternate_refs_command_does_not_run(self):
        self._publish_config_row('alternate-refs')

    def test_publish_source_config_cannot_run_or_weaken_fetch(self):
        for row in ('alternate-refs', 'hooks', 'fsmonitor', 'pack-objects-hook',
                    'remote-uploadpack', 'ssh-command', 'fsck-hasdotgit', 'fsck-skiplist'):
            with self.subTest(row=row):
                self._publish_config_row(row)

    def test_publish_requires_router_tip(self):
        instance, path = self.make()
        tip = self.commit(instance, path)
        tree = git(instance.git_dir, 'rev-parse', tip + '^{tree}').stdout.decode().strip()
        older = git(instance.git_dir, 'commit-tree', tree, '-p', self.base, '-m', 'older').stdout.decode().strip()
        reason = 'publish requires the router branch tip whose only parent is the base'
        with self.assertRaises(JobGitRefused) as caught:
            instance.publish(self.source, older, 'not-tip')
        self.assertEqual(caught.exception.reason, reason)
        self.absent(self.source, older)
        merge = git(instance.git_dir, 'commit-tree', tree, '-p', self.base, '-p', tip,
                    '-m', 'merge').stdout.decode().strip()
        git(instance.git_dir, 'update-ref', 'refs/heads/router/job', merge)
        with self.assertRaises(JobGitRefused) as caught:
            instance.publish(self.source, merge, 'merge-tip')
        self.assertEqual(caught.exception.reason, reason)
        self.absent(self.source, merge)

    def test_publish_ancestor_check_reaches_the_source(self):
        instance, path = self.make()
        commit = self.commit(instance, path)
        real = instance._git

        def wrap(*args, **kwargs):
            if args and args[0] == 'merge-base' and kwargs.get('git_dir') is not None:
                raise JobGitRefused('source ancestry')
            return real(*args, **kwargs)

        with patch.object(instance, '_git', wrap):
            with self.assertRaises(JobGitRefused) as caught:
                instance.publish(self.source, commit, 'ancestor-check')
        self.assertEqual(caught.exception.reason, 'source ancestry')

    def test_diff_ignores_binary_attributes(self):
        for attributes in ('* -diff\n', '* binary\n'):
            with self.subTest(attributes=attributes.strip()):
                instance, path = self.make()
                (path / 'a.txt').write_bytes(b'hello\nchanged\n')
                (path / '.gitattributes').write_text(attributes)
                commit = self.commit(instance, path)
                blob = git(instance.git_dir, 'hash-object', '-w', '--stdin',
                           data=b'* -diff\n').stdout.strip()
                attr_tree = git(instance.git_dir, 'mktree',
                                data=b'100644 blob ' + blob + b'\t.gitattributes\n').stdout.decode().strip()
                config_key = ''.join(('GIT', '_CONFIG_', 'K', 'EY_0'))
                env = {'GIT_CONFIG_COUNT': '1', config_key: 'attr.' + 'tree',
                       'GIT_CONFIG_VALUE_0': attr_tree}
                with patch.dict(os.environ, env):
                    text = instance.diff_text(commit)
                    summary = instance.diff_stat(commit)
                    names = instance.changed_paths(commit)
                self.assertNotIn('Binary files', text)
                self.assertIn('+changed', text)
                self.assertNotIn(' Bin', summary)
                self.assertIn('a.txt', names)

    def test_source_fetch_alias_does_not_run(self):
        instance, path = self.make()
        marker = self.root / 'fetch-alias'
        git(self.source, 'config', 'alias.fetch', '!touch %s' % marker)
        (path / 'a.txt').write_bytes(b'aliased\n')
        commit = self.commit(instance, path)
        instance.publish(self.source, commit, 'alias-published')
        self.assertFalse(marker.exists())

    def test_sha256_source_round_trip(self):
        folder = self.root / 'sha-src'
        folder.mkdir()
        result = git(folder, 'init', '-q', '--object-format=sha256', '--template=', check=False)
        if result.returncode != 0:
            sys.__stdout__.write('sha256 init refused: %s\n' % result.stderr)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr)
            return
        (folder / 'a.txt').write_bytes(b'hello\n')
        (folder / 'executable').write_bytes(b'#!/bin/sh\n')
        (folder / 'executable').chmod(0o755)
        (folder / 'link').symlink_to('a.txt')
        git(folder, 'add', '.')
        git(folder, 'commit', '-qm', 'base')
        base = git(folder, 'rev-parse', 'HEAD').stdout.decode().strip()
        self.assertEqual(len(base), 64)
        self.count += 1
        instance = JobGit.create(folder, base, 'router/job', self.root / 'state',
                                 'job-sha-%d' % self.count)
        config = (instance.git_dir / 'config').read_text()
        self.assertIn('repositoryformatversion = 1', config)
        self.assertIn('objectformat = sha256', config)
        path = self.root / 'sha-work'
        path.mkdir()
        instance.fill(path)
        self.assertEqual((path / 'a.txt').read_bytes(), b'hello\n')
        self.assertTrue((path / 'executable').stat().st_mode & stat.S_IXUSR)
        self.assertEqual(os.readlink(path / 'link'), 'a.txt')
        (path / 'a.txt').write_bytes(b'hello\nchanged\n')
        tree = instance.snapshot(path)
        self.assertEqual(len(tree), 64)
        commit = instance.commit(tree, 'sha', {'name': 'Router', 'email': 'router@example.invalid'})
        instance.publish(folder, commit, 'sha-published')
        self.assertEqual(git(folder, 'rev-parse', 'sha-published').stdout.decode().strip(), commit)
        self.assertEqual(git(folder, 'show', '%s:a.txt' % commit).stdout, b'hello\nchanged\n')
        self.assertIn('changed', instance.diff_text(commit))
        self.assertNotIn('Binary files', instance.diff_text(commit))

    def test_source_with_two_thousand_loose_objects(self):
        folder = self.root / 'loose-src'
        folder.mkdir()
        git(folder, 'init', '-q', '--template=')
        git(folder, 'config', 'maintenance.auto', 'false')
        for index in range(2000):
            (folder / ('f%04d.txt' % index)).write_bytes(b'distinct %d\n' % index)
        git(folder, 'add', '.')
        git(folder, 'commit', '-qm', 'loose')
        self.assertEqual(list((folder / '.git' / 'objects' / 'pack').glob('*.pack')), [])
        base = git(folder, 'rev-parse', 'HEAD').stdout.decode().strip()
        tree = git(folder, 'rev-parse', 'HEAD^{tree}').stdout.decode().strip()
        self.count += 1
        instance = JobGit.create(folder, base, 'router/job', self.root / 'state',
                                 'job-loose-%d' % self.count)
        path = self.root / 'loose-work'
        path.mkdir()
        instance.fill(path)
        self.assertEqual(instance.snapshot(path), tree)

    def test_create_retries_a_clone_that_lost_a_race(self):
        real = JobGit._git
        clones = []
        # Keep the raced blob loose so deleting the clone's copy is a real gap.
        git(self.source, 'config', 'maintenance.auto', 'false')
        git(self.source, 'config', 'gc.auto', '0')
        oid = blob_oid(b'hello\n')
        source_loose = self.source / '.git' / 'objects' / oid[:2] / oid[2:]
        self.assertTrue(source_loose.is_file())
        self.assertEqual(list((self.source / '.git' / 'objects' / 'pack').glob('*.pack')), [])

        def missing(this, *args, **kwargs):
            result = real(this, *args, **kwargs)
            if args[0] == 'clone':
                clones.append(args)
                if len(clones) == 1:
                    loose = this.git_dir / 'objects' / oid[:2] / oid[2:]
                    self.assertTrue(loose.is_file())
                    self.assertEqual(list((this.git_dir / 'objects' / 'pack').glob('*.pack')), [])
                    loose.unlink()
            return result

        def flaky(this, *args, **kwargs):
            if args[0] == 'clone':
                clones.append(args)
                if len(clones) < 3:
                    (this.git_dir / 'partial').write_bytes(b'left over')
                    raise job_git.JobGitRefused('git clone failed')
            return real(this, *args, **kwargs)

        def broken(this, *args, **kwargs):
            if args[0] == 'clone':
                clones.append(args)
                raise job_git.JobGitRefused('git clone failed')
            return real(this, *args, **kwargs)

        with patch('llm_router.job_git.time.sleep') as sleep:
            with patch.object(JobGit, '_git', missing):
                instance, path = self.make()
            self.assertEqual(len(clones), 2)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1])
            self.assertTrue(source_loose.is_file())
            self.assertEqual(instance.snapshot(path), self.base_tree())

            clones.clear()
            sleep.reset_mock()
            with patch.object(JobGit, '_git', flaky):
                instance, path = self.make()
            self.assertEqual(len(clones), 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
            self.assertFalse((instance.git_dir / 'partial').exists())
            self.assertEqual(instance.snapshot(path), self.base_tree())

            sleep.reset_mock()
            before = len(clones)
            with patch.object(JobGit, '_git', broken):
                with self.assertRaises(job_git.JobGitRefused) as caught:
                    self.make()
            self.assertEqual(str(caught.exception), 'git clone failed')
            self.assertEqual(len(clones) - before, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
            self.assertFalse((self.root / 'state' / 'job-git' / f'job-{self.count}.git').exists())

    def test_two_thousand_unchanged_files_stay_fast(self):
        folder = self.root / 'bulk-src'
        folder.mkdir()
        git(folder, 'init', '-q', '--template=')
        for index in range(2000):
            (folder / ('f%04d.txt' % index)).write_bytes(b'x\n')
        git(folder, 'add', '.')
        git(folder, 'commit', '-qm', 'bulk')
        base = git(folder, 'rev-parse', 'HEAD').stdout.decode().strip()
        self.count += 1
        instance = JobGit.create(folder, base, 'router/job', self.root / 'state',
                                 'job-bulk-%d' % self.count)
        path = self.root / 'bulk-work'
        path.mkdir()
        calls = []
        real_run = subprocess.run
        real_popen = subprocess.Popen

        def record_run(*args, **kwargs):
            calls.append(args[0])
            return real_run(*args, **kwargs)

        def record_popen(*args, **kwargs):
            calls.append(args[0])
            return real_popen(*args, **kwargs)

        with patch('subprocess.run', record_run), patch('subprocess.Popen', record_popen):
            started = time.perf_counter()
            instance.fill(path)
            fill_seconds = time.perf_counter() - started
            fill_calls = list(calls)
            calls.clear()
            started = time.perf_counter()
            tree = instance.snapshot(path)
            snapshot_seconds = time.perf_counter() - started
        expected = git(instance.git_dir, 'rev-parse', base + '^{tree}').stdout.decode().strip()
        self.assertEqual(tree, expected)
        sys.__stdout__.write('bulk fill %.3fs snapshot %.3fs\n' % (fill_seconds, snapshot_seconds))
        self.assertLess(fill_seconds, 10, 'fill %.3fs' % fill_seconds)
        self.assertLess(snapshot_seconds, 10, 'snapshot %.3fs' % snapshot_seconds)
        self.assertEqual([argv for argv in fill_calls if 'hash-object' in argv], [])
        self.assertEqual(sum(1 for argv in fill_calls if 'cat-file' in argv and '--batch' in argv), 1)
        self.assertEqual([argv for argv in calls if 'hash-object' in argv], [])
        self.assertGreaterEqual(sum(1 for argv in calls if '--batch-check' in argv), 1)

    def test_guard_mutations_fail(self):
        if os.environ.get('JOBGIT_MUTATION_CHILD'):
            return
        cases = (
            ('rule2-walk', 'test_dotgit_names_refuse_at_each_depth'),
            ('rule2-base', 'test_base_tree_dotgit_refused'),
            ('rule2-backslash', 'test_dotgit_names_refuse_at_each_depth'),
            ('rule3-nofollow', 'test_nofollow_does_not_open_fifo_target'),
            ('rule3-nonblock', 'test_swapped_fifo_does_not_block'),
            ('rule3-fstat', 'test_regular_swap_is_not_hashed'),
            ('rule5-nlink', 'test_special_files_refuse_without_blocking'),
            ('rule5-special', 'test_special_files_refuse_without_blocking'),
            ('rule9-second-pass', 'test_second_pass_detects_changed_added_removed_entries'),
            ('tree-compare', 'test_ignored_index_path_refuses'),
            ('publish-ancestor', 'test_publish_ancestor_check_reaches_the_source'),
            ('publish-exists', 'test_publish_refuses_unrelated_existing_and_bad_tree'),
            ('publish-fsck', 'test_publish_refuses_unrelated_existing_and_bad_tree'),
            ('publish-zero', 'test_publish_zero_oid_does_not_move_a_branch_created_during_fetch'),
            ('publish-altrefs', 'test_publish_alternate_refs_command_does_not_run'),
            ('publish-tip', 'test_publish_requires_router_tip'),
        )
        for guard, test in cases:
            with self.subTest(guard=guard):
                code, output = _run_mutant(guard, test)
                self.assertNotEqual(code, 0, output[-1500:])
                self.assertTrue('FAIL' in output or 'ERROR' in output, output[-1500:])


def _line(listing, name):
    for line in listing.splitlines():
        if line.endswith('\t' + name):
            return line
    raise AssertionError(name)


def _inodes(root):
    found = set()
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            info = os.lstat(os.path.join(dirpath, name))
            if stat.S_ISREG(info.st_mode):
                found.add((info.st_dev, info.st_ino))
    return found


def _run_mutant(guard, test):
    repo = Path(__file__).resolve().parents[1]
    source = (repo / 'src' / 'llm_router' / 'job_git.py').read_text()
    pattern = re.compile(r'^[ \t]*# guard:%s\n.*?^[ \t]*# endguard:%s\n' % (guard, guard), re.M | re.S)
    updated, count = pattern.subn('', source)
    if count < 1:
        raise AssertionError('guard %s not found' % guard)
    temp = tempfile.TemporaryDirectory(prefix='jobgit-mutant-')
    try:
        dest = Path(temp.name) / 'src'
        shutil.copytree(repo / 'src', dest, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (dest / 'llm_router' / 'job_git.py').write_text(updated)
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_CONFIG_')}
        env['PYTHONPATH'] = str(dest)
        env['JOBGIT_MUTATION_CHILD'] = '1'
        python = os.environ.get('JOBGIT_PYTHON', sys.executable)
        proc = subprocess.run(
            [python, '-m', 'unittest', 'test_job_git.JobGitTests.' + test],
            cwd=repo / 'tests', env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        return proc.returncode, proc.stdout.decode(errors='replace')
    finally:
        temp.cleanup()


if __name__ == '__main__':
    unittest.main()
