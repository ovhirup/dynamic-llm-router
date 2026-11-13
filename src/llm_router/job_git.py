"""Router-owned git directory. Job trees are read only as descriptor-relative bytes.

The caller creates the job directory, serializes use of one instance, and stops
worker processes before `snapshot`. This is not a sandbox against another process
running as the same user. Nothing here runs git with the job directory as a work
tree, and the only subprocess boundary is `JobGit._git`.
"""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys


class JobGitRefused(RuntimeError):
    """A short refusal. A refusal never publishes."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


# Git's HFS-ignorable set (utf8.c is_hfs_dotgit), checked against git 2.54 fsck.
_IGNORABLE = frozenset(chr(code) for start, end in (
    (0x200C, 0x200F), (0x202A, 0x202E), (0x206A, 0x206F), (0xFEFF, 0xFEFF))
    for code in range(start, end + 1))
_DEFAULT_CAPS = {'file_bytes': 16 * 1024 * 1024, 'total_bytes': 256 * 1024 * 1024,
                 'depth': 128, 'entries': 100_000}
_BLOB_MODES = (b'100644', b'100755', b'120000', b'160000')
_TIMEOUT = 300


def _dotgit(name):
    """True for a path component the brief treats as `.git`.

    Case-fold, drop HFS-ignorable code points, ignore a NTFS alternate-data-stream
    suffix, strip trailing dots and spaces, then match `.git` or the 8.3 name
    `git~1`. Git's own HFS and NTFS checks are separate, so this combined rule
    also refuses `git` + U+200C + `~1`, which git 2.54 accepts. The cross-check
    test records that pair; do not widen the rule here.
    """
    folded = ''.join(char for char in name.casefold() if char not in _IGNORABLE)
    return folded.split(':', 1)[0].rstrip('. ') in ('.git', 'git~1')


def _open_file(directory, name):
    """Open one directory entry without following a link or blocking on a pipe."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    # guard:rule3-nofollow
    flags |= os.O_NOFOLLOW
    # endguard:rule3-nofollow
    # guard:rule3-nonblock
    flags |= os.O_NONBLOCK
    # endguard:rule3-nonblock
    return os.open(name, flags, dir_fd=directory)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _subcommand(args):
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
    raise JobGitRefused('git command missing')


def _write_all(fd, blob):
    view = memoryview(blob)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise JobGitRefused('fill could not write the blob')
        view = view[written:]


def _read_capped(fd, limit):
    chunks = []
    total = 0
    while total <= limit:
        chunk = os.read(fd, min(65536, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise JobGitRefused('file byte cap exceeded')
        chunks.append(chunk)
    return b''.join(chunks)


class JobGit:
    """Bare repository and private index under `<state_dir>/job-git/<job_id>.git`.

    `policy_caps` may set positive integers `file_bytes`, `total_bytes`, `depth`
    (a root entry has depth 1) and `entries` (directories and symlinks count;
    the top-level `.git` does not). `commit` takes `identity` keys `name` and
    `email`. Diff helpers compare the base to one tree or commit, or two
    explicit objects when `other` is given.
    """

    def __init__(self, git_dir, base_sha, branch, policy_caps=None):
        if sys.platform == 'win32':
            raise JobGitRefused('Windows is not supported')
        self.git_dir = Path(git_dir).absolute()
        self.index_file = self.git_dir / 'router-index'
        self.base_sha = base_sha
        self.branch = branch
        self.policy_caps = dict(_DEFAULT_CAPS)
        if policy_caps:
            if set(policy_caps) - set(_DEFAULT_CAPS):
                raise JobGitRefused('unknown policy cap')
            self.policy_caps.update(policy_caps)
        if any(type(value) is not int or value <= 0 for value in self.policy_caps.values()):
            raise JobGitRefused('policy caps must be positive integers')
        self._root_identity = None
        self._gitlinks = {}

    def _git(self, *args, data=None, git_dir=None, index=False):
        command = _subcommand(args)
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_DIR=str(git_dir or self.git_dir),
                   GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM='/dev/null',
                   GIT_CONFIG_GLOBAL='/dev/null', GIT_NO_REPLACE_OBJECTS='1',
                   GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='file',
                   GIT_TERMINAL_PROMPT='0', GIT_ATTR_NOSYSTEM='1')
        if index:
            env['GIT_INDEX_FILE'] = str(self.index_file)
        argv = ['git', '--no-replace-objects',
                '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                '-c', 'commit.gpgsign=false', '-c', 'core.attributesFile=/dev/null',
                '-c', f'alias.{command}={command}', *args]
        try:
            result = subprocess.run(
                argv, input=data, stdin=None if data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                timeout=_TIMEOUT, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise JobGitRefused(f'git {command} failed') from exc
        return result.stdout

    @staticmethod
    def _oid(value):
        if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value):
            raise JobGitRefused('expected a full object id')
        return value

    def _ref(self, branch):
        ref = 'refs/heads/' + branch
        self._git('check-ref-format', ref)
        return ref

    def _source_dir(self, source_repo):
        source = Path(source_repo).absolute()
        git_path = source / '.git'
        if git_path.is_symlink() or (git_path.exists() and not git_path.is_dir()):
            raise JobGitRefused('source git directory must be a real directory')
        candidate = git_path if git_path.is_dir() else source
        resolved = Path(os.fsdecode(self._git(
            'rev-parse', '--absolute-git-dir', git_dir=candidate)).strip())
        if resolved.is_symlink() or not resolved.is_dir():
            raise JobGitRefused('source git directory is unavailable')
        return resolved

    def _keep_core_config(self):
        """Replace the cloned config, including its origin URL, with a bare core."""
        (self.git_dir / 'config').write_text(
            '[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n',
            encoding='utf-8')

    @classmethod
    def create(cls, source_repo, base_sha, branch, state_dir, job_id, policy_caps=None):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', job_id):
            raise JobGitRefused('invalid job id')
        directory = Path(state_dir) / 'job-git' / (job_id + '.git')
        instance = cls(directory, base_sha, branch, policy_caps)
        instance._oid(base_sha)
        instance._ref(branch)
        parent = directory.parent
        if parent.is_symlink():
            raise JobGitRefused('router git directory is unavailable')
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        created = False
        try:
            directory.mkdir(mode=0o700)
            created = True
            os.chmod(directory, 0o700)
            instance._git('clone', '--bare', '--local', '--no-hardlinks', '--template=',
                          '--', str(Path(source_repo).absolute()), str(directory))
            instance._keep_core_config()
            refs = instance._git('for-each-ref', '--format=%(refname)').splitlines()
            if refs:
                instance._git('update-ref', '--stdin',
                              data=b''.join(b'delete ' + ref + b'\n' for ref in refs))
            instance._git('cat-file', '-e', base_sha + '^{commit}')
            instance._git('update-ref', 'refs/heads/' + branch, base_sha)
            instance._git('symbolic-ref', 'HEAD', 'refs/heads/' + branch)
            instance._git('reflog', 'expire', '--expire=all', '--all')
            instance._git('-c', 'repack.updateServerInfo=false', 'repack', '-ad')
            instance._git('prune', '--expire=now')
            alternates = directory / 'objects' / 'info' / 'alternates'
            if alternates.is_symlink() or alternates.exists():
                raise JobGitRefused('router repository has alternates')
            os.chmod(directory, 0o700)
        except JobGitRefused:
            if created:
                shutil.rmtree(directory)
            raise
        except (OSError, ValueError) as exc:
            if created:
                shutil.rmtree(directory)
            raise JobGitRefused('router git directory already exists or is unavailable') from exc
        except BaseException:
            if created:
                shutil.rmtree(directory)
            raise
        return instance

    def _components(self, path):
        parts = path.split(b'/')
        if not parts or any(part in (b'', b'.', b'..') for part in parts):
            raise JobGitRefused('fill refused unsafe path or filesystem collision')
        # guard:rule2-base
        if any(_dotgit(os.fsdecode(part)) for part in parts):
            raise JobGitRefused('base contains a .git-like path')
        # endguard:rule2-base
        return parts

    def _entries(self):
        listed = []
        raw = self._git('ls-tree', '-r', '-z', '--full-tree', self.base_sha)
        for record in raw.split(b'\0'):
            if not record:
                continue
            meta, path = record.split(b'\t', 1)
            mode, _kind, oid = meta.split()
            if mode not in _BLOB_MODES:
                raise JobGitRefused('unsupported base entry mode')
            self._components(path)
            listed.append((mode, oid, path))
        return listed

    def _root(self, job_dir):
        try:
            return os.open(job_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise JobGitRefused('job directory cannot be opened') from exc

    def _ensure_dir(self, parent_fd, parent_path, name, created):
        path = name if not parent_path else parent_path + b'/' + name
        if path not in created:
            try:
                os.mkdir(name, 0o755, dir_fd=parent_fd)
            except FileExistsError as exc:
                raise JobGitRefused('fill refused unsafe path or filesystem collision') from exc
            created.add(path)
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                       dir_fd=parent_fd), path

    def fill(self, job_dir):
        """Write the base tree into an existing empty directory. No filters, no checkout."""
        self._root_identity = None
        self._gitlinks = {}
        root = None
        try:
            root = self._root(job_dir)
            if os.listdir(root):
                raise JobGitRefused('fill requires an empty directory')
            info = os.fstat(root)
            entries = self._entries()
            gitlinks = {path: oid for mode, oid, path in entries if mode == b'160000'}
            created = {b''}
            for mode, oid, path in entries:
                parts = self._components(path)
                parent = root
                parent_path = b''
                opened = []
                try:
                    folders = parts if mode == b'160000' else parts[:-1]
                    for name in folders:
                        parent, parent_path = self._ensure_dir(parent, parent_path, name, created)
                        opened.append(parent)
                    if mode == b'160000':
                        continue
                    blob = self._git('cat-file', 'blob', oid.decode('ascii'))
                    leaf = parts[-1]
                    if mode == b'120000':
                        os.symlink(blob, leaf, dir_fd=parent)
                    else:
                        fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
                        try:
                            _write_all(fd, blob)
                            os.fchmod(fd, 0o755 if mode == b'100755' else 0o644)
                        finally:
                            os.close(fd)
                finally:
                    for fd in reversed(opened):
                        os.close(fd)
            self._gitlinks = gitlinks
            self._root_identity = (info.st_dev, info.st_ino)
        except JobGitRefused:
            self._root_identity = None
            self._gitlinks = {}
            raise
        except (OSError, ValueError) as exc:
            self._root_identity = None
            self._gitlinks = {}
            raise JobGitRefused('fill refused unsafe path or filesystem collision') from exc
        finally:
            if root is not None:
                os.close(root)

    def _under_gitlink(self, path):
        for link in self._gitlinks:
            if path == link or path.startswith(link + b'/'):
                return True
        return False

    def _walk(self, root, visit):
        entries = {}

        def walk(directory, prefix, depth):
            for name in sorted(os.listdir(directory)):
                if not prefix and name == '.git':
                    continue
                # guard:rule2-walk
                if _dotgit(name):
                    raise JobGitRefused('.git-like path')
                # endguard:rule2-walk
                encoded = os.fsencode(name)
                path = encoded if not prefix else prefix + b'/' + encoded
                if depth > self.policy_caps['depth']:
                    raise JobGitRefused('depth cap exceeded')
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                entries[path] = _signature(info)
                if len(entries) > self.policy_caps['entries']:
                    raise JobGitRefused('entry cap exceeded')
                if stat.S_ISDIR(info.st_mode):
                    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=directory)
                    try:
                        opened = os.fstat(fd)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise JobGitRefused('directory changed before open')
                        walk(fd, path, depth + 1)
                    finally:
                        os.close(fd)
                else:
                    if self._under_gitlink(path):
                        raise JobGitRefused('file under a gitlink')
                    visit(directory, name, path, info)

        walk(root, b'', 1)
        return entries

    def _hash_entry(self, directory, name, info, total):
        if stat.S_ISLNK(info.st_mode):
            content = os.readlink(os.fsencode(name), dir_fd=directory)
            mode = b'120000'
        elif stat.S_ISREG(info.st_mode):
            # guard:rule5-nlink
            if info.st_nlink != 1:
                raise JobGitRefused('hard-linked file')
            # endguard:rule5-nlink
            fd = _open_file(directory, name)
            try:
                actual = os.fstat(fd)
                # guard:rule3-fstat
                if (not stat.S_ISREG(actual.st_mode) or
                        (actual.st_dev, actual.st_ino, actual.st_size, actual.st_nlink) !=
                        (info.st_dev, info.st_ino, info.st_size, info.st_nlink)):
                    raise JobGitRefused('file changed before open')
                # endguard:rule3-fstat
                if actual.st_size > self.policy_caps['file_bytes']:
                    raise JobGitRefused('file byte cap exceeded')
                content = _read_capped(fd, self.policy_caps['file_bytes'])
            finally:
                os.close(fd)
            mode = b'100755' if info.st_mode & stat.S_IXUSR else b'100644'
        else:
            # guard:rule5-special
            raise JobGitRefused('special file')
            # endguard:rule5-special
            return None
        if len(content) > self.policy_caps['file_bytes']:
            raise JobGitRefused('file byte cap exceeded')
        total[0] += len(content)
        if total[0] > self.policy_caps['total_bytes']:
            raise JobGitRefused('total byte cap exceeded')
        oid = self._git('hash-object', '-w', '--no-filters', '--stdin', data=content).strip()
        return mode, oid

    def snapshot(self, job_dir):
        """Hash checked descriptor bytes and build a tree in the private index.

        One `update-index --index-info` takes every cache entry. `hash-object`
        stays per file because each blob is the bytes read from a checked
        descriptor, not a path git is allowed to open.
        """
        root = None
        try:
            root = self._root(job_dir)
            info = os.fstat(root)
            if self._root_identity != (info.st_dev, info.st_ino):
                raise JobGitRefused('job directory identity changed')
            total = [0]
            records = [b'160000 ' + oid + b'\t' + path + b'\0'
                       for path, oid in self._gitlinks.items()]

            def visit(directory, name, path, expected):
                mode, oid = self._hash_entry(directory, name, expected, total)
                records.append(mode + b' ' + oid + b'\t' + path + b'\0')

            before = self._walk(root, visit)
            after = self._walk(root, lambda *_args: None)
            # guard:rule9-second-pass
            if before != after:
                raise JobGitRefused('job tree changed during snapshot')
            # endguard:rule9-second-pass
            index_args = ('-c', 'core.ignorecase=false', '-c', 'core.precomposeunicode=false')
            self._git(*index_args, 'read-tree', '--empty', index=True)
            if records:
                self._git(*index_args, 'update-index', '-z', '--index-info',
                          data=b''.join(records), index=True)
            return self._git(*index_args, 'write-tree', index=True).decode('ascii').strip()
        except JobGitRefused:
            raise
        except (OSError, ValueError) as exc:
            raise JobGitRefused('snapshot filesystem race or unsafe entry') from exc
        finally:
            if root is not None:
                os.close(root)

    def commit(self, tree_sha, message, identity):
        tree_sha = self._oid(tree_sha)
        try:
            name, email = identity['name'], identity['email']
        except (KeyError, TypeError) as exc:
            raise JobGitRefused('invalid commit identity') from exc
        if (not isinstance(name, str) or not isinstance(email, str) or not name.strip() or
                not email.strip() or any(char in name + email for char in '\n\r\0<>')):
            raise JobGitRefused('invalid commit identity')
        if not isinstance(message, str) or '\0' in message:
            raise JobGitRefused('invalid commit message')
        commit = self._git('-c', f'user.name={name}', '-c', f'user.email={email}',
                           'commit-tree', tree_sha, '-p', self.base_sha,
                           data=message.encode('utf-8')).decode('ascii').strip()
        self._oid(commit)
        self._git('update-ref', self._ref(self.branch), commit, self.base_sha)
        return commit

    def _trees(self, target, other):
        left, right = (self.base_sha, target) if other is None else (target, other)
        return self._oid(left) + '^{tree}', self._oid(right) + '^{tree}'

    def changed_paths(self, tree_or_commit, other=None):
        output = self._git('diff', '--no-ext-diff', '--no-textconv', '--no-color',
                           '--name-only', '-z', *self._trees(tree_or_commit, other), '--')
        return [os.fsdecode(path) for path in output.split(b'\0') if path]

    def diff_text(self, tree_or_commit, other=None):
        return os.fsdecode(self._git(
            'diff', '--unified=0', '--no-color', '--no-textconv', '--no-ext-diff',
            *self._trees(tree_or_commit, other), '--'))

    def diff_stat(self, tree_or_commit, other=None):
        return os.fsdecode(self._git(
            'diff', '--stat', '--no-color', '--no-textconv', '--no-ext-diff',
            *self._trees(tree_or_commit, other), '--'))

    def publish(self, source_repo, commit_sha, branch):
        """Fetch exactly one commit into the source and create one absent branch."""
        commit_sha = self._oid(commit_sha)
        ref = self._ref(branch)
        if self._git('cat-file', '-t', commit_sha).strip() != b'commit':
            raise JobGitRefused('publish requires a commit')
        # guard:publish-ancestor
        self._git('merge-base', '--is-ancestor', self.base_sha, commit_sha)
        # endguard:publish-ancestor
        source = self._source_dir(source_repo)
        # guard:publish-exists
        if self._git('for-each-ref', '--format=%(refname)', ref, git_dir=source).strip():
            raise JobGitRefused('publish branch already exists')
        # endguard:publish-exists
        self._git(
            # guard:publish-fsck
            '-c', 'fetch.fsckObjects=true', '-c', 'transfer.fsckObjects=true',
            '-c', 'fetch.fsck.hasDotgit=error',
            # endguard:publish-fsck
            'fetch', '--no-tags', '--no-write-fetch-head', '--no-recurse-submodules',
            '--no-auto-maintenance', '--refmap=', '--', str(self.git_dir), commit_sha,
            git_dir=source)
        # guard:publish-ancestor
        self._git('merge-base', '--is-ancestor', self.base_sha, commit_sha, git_dir=source)
        # endguard:publish-ancestor
        update = ['update-ref', ref, commit_sha]
        # guard:publish-zero
        update.append('0' * len(commit_sha))
        # endguard:publish-zero
        self._git(*update, git_dir=source)

    def destroy(self):
        """Remove this instance's repository, including its private index."""
        if self.git_dir.is_symlink():
            raise JobGitRefused('router git directory is a symlink')
        shutil.rmtree(self.git_dir)
