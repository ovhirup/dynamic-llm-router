"""Router-owned git directory. Job trees are read only as descriptor-relative bytes.

The caller creates the job directory, serializes use of one instance, and stops
worker processes before `snapshot`. This is not a sandbox against another process
running as the same user. Nothing here runs git with the job directory as a work
tree, and the only subprocess boundary is `JobGit._git`.

Job paths are never passed to git (`--stdin-paths`, `--path`, or any path
argument): git would open them by path and the check-then-open race would return.

`publish` forces `fetch.fsck.hasDotgit=error` and also passes
`fetch.fsck.skipList=/dev/null`. On git 2.54 that key is multi-valued, so a
skip list set in the source is still applied. Other per-message
`fetch.fsck.<id>` settings in the source are not overridden.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import threading
import time


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
# `git hash-object -t tree --stdin` of an empty tree, sha1 then sha256.
_EMPTY_TREE = {
    40: '4b825dc642cb6eb9a060e54bf8d69288fbee4904',
    64: '6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321',
}


def _dotgit(name):
    """True for a path component the brief treats as `.git`.

    Case-fold, drop HFS-ignorable code points, then split on backslash. Any
    piece, cut at the first colon and with trailing dots and spaces removed,
    that equals `.git` or `git~1` is refused. Git's own HFS and NTFS checks
    are separate, so this also refuses `git` + U+200C + `~1`, which git 2.54
    accepts. Tighter is allowed; do not widen the rule here.
    """
    folded = ''.join(char for char in name.casefold() if char not in _IGNORABLE)
    pieces = [folded]
    # guard:rule2-backslash
    pieces = folded.split('\\')
    # endguard:rule2-backslash
    return any(piece.split(':', 1)[0].rstrip('. ') in ('.git', 'git~1') for piece in pieces)


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


def _scan(directory):
    """Yield directory entries from a descriptor, one at a time."""
    return os.scandir(directory)


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


def _blob_id(data, oid_len):
    digest = hashlib.sha256 if oid_len == 64 else hashlib.sha1
    return digest(b'blob %d\0' % len(data) + data).hexdigest().encode('ascii')


def _read_line(stream, limit=512):
    buf = bytearray()
    while len(buf) <= limit:
        char = stream.read(1)
        if char == b'\n':
            return bytes(buf)
        if not char:
            raise JobGitRefused('cat-file framing mismatch')
        buf += char
    raise JobGitRefused('cat-file framing mismatch')


def _read_exact(stream, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = stream.read(size - len(buf))
        if not chunk:
            raise JobGitRefused('cat-file framing mismatch')
        buf += chunk
    return bytes(buf)


def _read_blob(stream, oid):
    line = _read_line(stream)
    parts = line.split(b' ')
    if len(parts) != 3 or parts[0] != oid or parts[1] != b'blob' or not parts[2].isdigit():
        raise JobGitRefused('cat-file framing mismatch')
    data = _read_exact(stream, int(parts[2]))
    if _read_exact(stream, 1) != b'\n':
        raise JobGitRefused('cat-file framing mismatch')
    return data


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

    def _cwd(self):
        if self.git_dir.is_dir() and not self.git_dir.is_symlink():
            return str(self.git_dir)
        for candidate in (self.git_dir.parent.parent, self.git_dir.parent):
            if candidate.is_dir() and not candidate.is_symlink():
                return str(candidate)
        raise JobGitRefused('router git directory is unavailable')

    def _git(self, *args, data=None, git_dir=None, index=False, reader=None):
        command = _subcommand(args)
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_DIR=str(git_dir or self.git_dir),
                   GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM='/dev/null',
                   GIT_CONFIG_GLOBAL='/dev/null', GIT_NO_REPLACE_OBJECTS='1',
                   GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='file',
                   GIT_TERMINAL_PROMPT='0', GIT_ATTR_NOSYSTEM='1')
        if index:
            env['GIT_INDEX_FILE'] = str(self.index_file)
        # Git ignores an alias that shadows a builtin (`git -c alias.version='!echo X' version` prints only the version).
        argv = ['git', '--no-replace-objects',
                '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                '-c', 'commit.gpgsign=false', '-c', 'core.attributesFile=/dev/null',
                *args]
        cwd = self._cwd()
        if reader is not None:
            return self._git_read(argv, env, cwd, data, reader, command)
        try:
            result = subprocess.run(
                argv, input=data, stdin=None if data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
                timeout=_TIMEOUT, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise JobGitRefused(f'git {command} failed') from exc
        return result.stdout

    def _git_read(self, argv, env, cwd, data, reader, command):
        """Stream one git command. A writer thread feeds stdin so a full pipe cannot deadlock."""
        started = time.monotonic()
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=cwd)

        def feed():
            try:
                view = memoryview(data or b'')
                while view:
                    written = proc.stdin.write(view)
                    if written <= 0:
                        break
                    view = view[written:]
                proc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        timer = threading.Timer(_TIMEOUT, proc.kill)
        timer.daemon = True
        timer.start()
        try:
            try:
                reader(proc.stdout)
                if proc.stdout and not proc.stdout.closed:
                    proc.stdout.close()
                remaining = _TIMEOUT - (time.monotonic() - started)
                code = proc.wait(timeout=max(1, remaining))
            except JobGitRefused:
                if proc.poll() is None:
                    proc.kill()
                raise
            except (OSError, subprocess.SubprocessError) as exc:
                if proc.poll() is None:
                    proc.kill()
                raise JobGitRefused(f'git {command} failed') from exc
            if code != 0:
                raise JobGitRefused(f'git {command} failed')
        finally:
            timer.cancel()
            thread.join(timeout=5)
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None and not pipe.closed:
                    try:
                        pipe.close()
                    except OSError:
                        pass

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
        """Replace the cloned config, keeping the format version and extensions."""
        listed = self._git('config', '--list', '--local').decode('utf-8', 'replace')
        version = '0'
        extensions = []
        for line in listed.splitlines():
            if '=' not in line:
                continue
            key, value = line.split('=', 1)
            if any(ord(char) < 32 for char in key + value):
                raise JobGitRefused('refusing unsafe repository extension')
            if key == 'core.repositoryformatversion':
                version = value
            elif key.startswith('extensions.'):
                extensions.append((key.split('.', 1)[1], value))
        if not re.fullmatch(r'\d+', version):
            raise JobGitRefused('unsupported repository format')
        lines = ['[core]', '\trepositoryformatversion = %s' % version,
                 '\tfilemode = true', '\tbare = true']
        if extensions:
            lines.append('[extensions]')
            for key, value in extensions:
                lines.append('\t%s = %s' % (key, value))
        (self.git_dir / 'config').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    @classmethod
    def create(cls, source_repo, base_sha, branch, state_dir, job_id, policy_caps=None):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', job_id):
            raise JobGitRefused('invalid job id')
        directory = Path(state_dir) / 'job-git' / (job_id + '.git')
        instance = cls(directory, base_sha, branch, policy_caps)
        instance._oid(base_sha)
        parent = directory.parent
        if parent.is_symlink():
            raise JobGitRefused('router git directory is unavailable')
        parent.mkdir(parents=True, exist_ok=True)
        os.chmod(parent, 0o700)
        instance._ref(branch)
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

    def _place(self, root, mode, path, data, created):
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
                return
            leaf = parts[-1]
            if mode == b'120000':
                os.symlink(data, leaf, dir_fd=parent)
            else:
                fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                             os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
                try:
                    _write_all(fd, data)
                    os.fchmod(fd, 0o755 if mode == b'100755' else 0o644)
                finally:
                    os.close(fd)
        finally:
            for fd in reversed(opened):
                os.close(fd)

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
            created = {b''}

            def reader(stdout):
                for mode, oid, path in entries:
                    data = None if mode == b'160000' else _read_blob(stdout, oid)
                    self._place(root, mode, path, data, created)
                if stdout.read(1):
                    raise JobGitRefused('cat-file framing mismatch')

            blobs = [oid for mode, oid, _path in entries if mode != b'160000']
            if blobs:
                self._git('cat-file', '--batch', data=b''.join(oid + b'\n' for oid in blobs),
                          reader=reader)
            else:
                for mode, _oid, path in entries:
                    self._place(root, mode, path, None, created)
            self._gitlinks = {path: oid for mode, oid, path in entries if mode == b'160000'}
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
            with _scan(directory) as scanned:
                for entry in scanned:
                    name = entry.name
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
                        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                     os.O_CLOEXEC, dir_fd=directory)
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

    def _read_entry(self, directory, name, info, total):
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
            content = b''
            mode = b'100644'
            # guard:rule5-special
            raise JobGitRefused('special file')
            # endguard:rule5-special
        if len(content) > self.policy_caps['file_bytes']:
            raise JobGitRefused('file byte cap exceeded')
        total[0] += len(content)
        if total[0] > self.policy_caps['total_bytes']:
            raise JobGitRefused('total byte cap exceeded')
        return mode, content

    def _record_items(self, records):
        items = []
        for record in records:
            body = record[:-1] if record.endswith(b'\0') else record
            meta, path = body.split(b'\t', 1)
            mode, oid = meta.split(b' ')
            items.append((mode, oid, path))
        return items

    def _batch_check(self, expected):
        """Return blob ids that are absent. Refuse on a framing or type mismatch."""
        oids = list(expected)
        if not oids:
            return set()
        missing = []

        def reader(stdout):
            for oid in oids:
                parts = _read_line(stdout).split(b' ')
                if parts == [oid, b'missing']:
                    if expected[oid] != b'blob':
                        raise JobGitRefused('object missing from the router repository')
                    missing.append(oid)
                    continue
                if (len(parts) != 3 or parts[0] != oid or parts[1] != expected[oid] or
                        not parts[2].isdigit()):
                    raise JobGitRefused('cat-file framing mismatch')
            if stdout.read(1):
                raise JobGitRefused('cat-file framing mismatch')

        self._git('cat-file', '--batch-check', data=b''.join(oid + b'\n' for oid in oids),
                  reader=reader)
        return set(missing)

    def _ensure_objects(self, root, records, path_oids):
        expected = {}
        for mode, oid, _path in self._record_items(records):
            expected[oid] = b'commit' if mode == b'160000' else b'blob'
        missing = self._batch_check(expected)
        if missing:
            found = set()

            def visit(directory, name, path, info):
                oid = path_oids.get(path)
                if oid not in missing:
                    return
                _mode, content = self._read_entry(directory, name, info, [0])
                if _blob_id(content, len(self.base_sha)) != oid:
                    raise JobGitRefused('job tree changed during snapshot')
                written = self._git('hash-object', '-w', '--no-filters', '--stdin',
                                    data=content).strip()
                if written != oid:
                    raise JobGitRefused('blob id mismatch')
                found.add(oid)

            self._walk(root, visit)
            if not missing <= found:
                raise JobGitRefused('job tree changed during snapshot')
        if self._batch_check(expected):
            raise JobGitRefused('blob missing after write')

    def _assert_tree(self, tree, records):
        expected = set(self._record_items(records))
        actual = set()
        raw = self._git('ls-tree', '-r', '-z', '--full-tree', tree)
        for record in raw.split(b'\0'):
            if not record:
                continue
            meta, path = record.split(b'\t', 1)
            mode, _kind, oid = meta.split(b' ')
            actual.add((mode, oid, path))
        if actual != expected:
            raise JobGitRefused('tree does not match the bytes read')

    def _load_index(self, records):
        index_args = ('-c', 'core.ignorecase=false', '-c', 'core.precomposeunicode=false')
        self._git(*index_args, 'read-tree', '--empty', index=True)
        if records:
            self._git(*index_args, 'update-index', '-z', '--index-info',
                      data=b''.join(records), index=True)
        tree = self._git(*index_args, 'write-tree', index=True).decode('ascii').strip()
        # guard:tree-compare
        self._assert_tree(tree, records)
        # endguard:tree-compare
        return tree

    def snapshot(self, job_dir):
        """Hash checked descriptor bytes and build a tree in the private index.

        Each blob id is computed in Python from the bytes just read. One
        `cat-file --batch-check` asks which of those ids the router repository
        already has. Only a missing blob is written, with `hash-object --stdin`
        of the bytes read on the second open, and every id is checked again
        afterwards. One `update-index --index-info` then takes every cache entry.
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
            path_oids = {}

            def visit(directory, name, path, expected):
                mode, content = self._read_entry(directory, name, expected, total)
                oid = _blob_id(content, len(self.base_sha))
                path_oids[path] = oid
                records.append(mode + b' ' + oid + b'\t' + path + b'\0')

            before = self._walk(root, visit)
            self._ensure_objects(root, records, path_oids)
            after = self._walk(root, lambda *_args: None)
            # guard:rule9-second-pass
            if before != after:
                raise JobGitRefused('job tree changed during snapshot')
            # endguard:rule9-second-pass
            return self._load_index(records)
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

    def _empty_tree(self):
        try:
            return _EMPTY_TREE[len(self.base_sha)]
        except KeyError as exc:
            raise JobGitRefused('unsupported object format') from exc

    def changed_paths(self, tree_or_commit, other=None):
        output = self._git('-c', 'attr.tree=' + self._empty_tree(),
                           'diff', '--text', '--no-ext-diff', '--no-textconv', '--no-color',
                           '--name-only', '-z', *self._trees(tree_or_commit, other), '--')
        return [os.fsdecode(path) for path in output.split(b'\0') if path]

    def diff_text(self, tree_or_commit, other=None):
        return os.fsdecode(self._git(
            '-c', 'attr.tree=' + self._empty_tree(),
            'diff', '--text', '--unified=0', '--no-color', '--no-textconv', '--no-ext-diff',
            *self._trees(tree_or_commit, other), '--'))

    def diff_stat(self, tree_or_commit, other=None):
        return os.fsdecode(self._git(
            '-c', 'attr.tree=' + self._empty_tree(),
            'diff', '--text', '--stat', '--no-color', '--no-textconv', '--no-ext-diff',
            *self._trees(tree_or_commit, other), '--'))

    def publish(self, source_repo, commit_sha, branch):
        """Fetch exactly one commit into the source and create one absent branch."""
        commit_sha = self._oid(commit_sha)
        ref = self._ref(branch)
        if self._git('cat-file', '-t', commit_sha).strip() != b'commit':
            raise JobGitRefused('publish requires a commit')
        # guard:publish-tip
        tip = self._git('rev-parse', self._ref(self.branch)).decode().strip()
        parents = self._git('rev-parse', commit_sha + '^@').split()
        if tip != commit_sha or parents != [self.base_sha.encode()]:
            raise JobGitRefused('publish requires the router branch tip whose only parent is the base')
        # endguard:publish-tip
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
            '-c', 'fetch.fsck.skipList=/dev/null',
            # endguard:publish-fsck
            # guard:publish-altrefs
            '-c', 'core.alternateRefsCommand=true',
            # endguard:publish-altrefs
            'fetch', '--upload-pack=git-upload-pack', '--no-tags', '--no-write-fetch-head',
            '--no-recurse-submodules', '--no-auto-maintenance', '--refmap=',
            '--', str(self.git_dir), commit_sha,
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
