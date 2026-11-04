"""Keep a same-UID judge child from reading the verifier's private directory.

A private HOME or a 0700 temp directory does not work: the child is the same user, so it can
open the directory and read prompt.md. A container is not available on the macOS host or on
Ubuntu CI. This module wraps the judge instead:

- macOS: `sandbox-exec` allows the process's normal filesystem access and denies `file-read*`
  and `file-write*` (rename and unlink included) on the private directory and the files in it.
  The same `file-write*` deny is applied to each ancestor directory itself, so the child cannot
  rename the prompt, its directory, or a parent out from under the read rule. The verdict file
  stays writable in place (`file-write-data` only) and cannot be renamed out. The runner reads
  it after the process exits.
- Linux: Landlock (no user namespace, so it still works where AppArmor blocks unprivileged
  `unshare`). Every other path keeps read and write. Allow-list entries are real paths. A path
  is omitted when it is the private directory, inside it, or an ancestor of it, including `/`
  (`Path.is_relative_to` on the resolved paths, both ways). A symlink to `/` resolves to `/`
  and is rejected. The verdict file is write-only. Rule file descriptors are opened with
  `O_PATH`, so a named pipe among those entries cannot stall the judge before it writes the
  verdict. The caller points TMPDIR at a sibling directory so the judge's own temp files are
  not created beside the nonce.
- If neither sandbox can be applied, the caller must not run the judge.
"""
import ctypes
import os
from pathlib import Path
import shutil
import sys

_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_WRITE_FILE = 1 << 1
_TRUNCATE = 1 << 14
_RULE_PATH_BENEATH = 1
_CREATE_VERSION = 1
# x86_64 and aarch64, the architectures Ubuntu CI and Apple Silicon Linux use.
_LANDLOCK_SYSCALLS = {
    'x86_64': (444, 445, 446),
    'aarch64': (444, 445, 446),
    'arm64': (444, 445, 446),
}


def supported():
    """True when this process can wrap a judge. Does not confine the caller."""
    if sys.platform == 'darwin':
        return shutil.which('sandbox-exec') is not None
    if sys.platform == 'linux':
        return landlock_abi() is not None
    return False


def wrap(argv, private_dir, verdict_path):
    """Argv that runs `argv` with `private_dir` unreadable. None when no sandbox is available."""
    argv = [str(arg) for arg in argv]
    if not argv or not argv[0]:
        return None
    private = os.path.realpath(private_dir)
    verdict = os.path.realpath(verdict_path)
    if sys.platform == 'darwin':
        exe = shutil.which('sandbox-exec')
        if not exe:
            return None
        return [exe, '-p', macos_profile(private, verdict), *argv]
    if sys.platform == 'linux' and landlock_abi() is not None:
        script = (
            'import os, sys\n'
            'from llm_router.judge_sandbox import confine\n'
            'if not confine(sys.argv[1], sys.argv[2]):\n'
            '    raise SystemExit(126)\n'
            'os.execv(sys.argv[3], sys.argv[3:])\n'
        )
        return [sys.executable, '-c', script, private, verdict, *argv]
    return None


def _quoted(path):
    text = os.path.realpath(path).replace('\\', '\\\\').replace('"', '\\"')
    return '"' + text + '"'


def macos_profile(private_dir, verdict_path=None):
    """Seatbelt profile: normal access, no reads or renames of the private directory.

    ``file-write*`` on the directory covers the files inside it, including rename and unlink.
    Each ancestor gets that deny on the directory inode only, so the child cannot rename the
    directory out from under the rule. The verdict file may still be written in place.
    """
    private = os.path.realpath(private_dir)
    rules = [
        '(version 1)',
        '(allow default)',
        '(deny file-read* (subpath %s))' % _quoted(private),
        '(deny file-write* (subpath %s))' % _quoted(private),
    ]
    try:
        names = os.listdir(private)
    except OSError:
        names = []
    for name in names:
        rules.append('(deny file-write* (literal %s))' % _quoted(os.path.join(private, name)))
    parent = os.path.dirname(private)
    seen = set()
    while parent and parent not in seen:
        seen.add(parent)
        rules.append('(deny file-write* (literal %s))' % _quoted(parent))
        if parent == os.sep:
            break
        parent = os.path.dirname(parent)
    if verdict_path:
        # In-place contents only. file-write* would also allow renaming the verdict aside.
        rules.append('(allow file-write-data (literal %s))' % _quoted(verdict_path))
    return '\n'.join(rules) + '\n'


def paths_outside(secret):
    """Real paths that may stay readable: every sibling of each ancestor, never the secret or an ancestor."""
    secret = os.path.realpath(secret)
    allowed = []
    seen = set()
    current = '/'
    for part in Path(secret).parts[1:]:
        try:
            names = os.listdir(current)
        except OSError:
            names = []
        step = os.path.join(current, part) if current != '/' else os.path.join('/', part)
        try:
            step_real = os.path.realpath(step)
        except OSError:
            step_real = step
        for name in names:
            path = os.path.join(current, name) if current != '/' else os.path.join('/', name)
            try:
                real = os.path.realpath(path)
            except OSError:
                continue
            if real in seen or real == step_real or _covers(real, secret):
                continue
            seen.add(real)
            allowed.append(real)
        current = step
    return allowed


def _covers(path, secret):
    """True when either resolved path is the other or contained in it.

    ``secret.startswith(path + os.sep)`` treats ``/`` as the prefix ``//`` and would allow it.
    """
    left = Path(os.path.realpath(path))
    right = Path(os.path.realpath(secret))
    return left.is_relative_to(right) or right.is_relative_to(left)


def landlock_abi():
    """Landlock ABI version, or None when the kernel has no Landlock."""
    if sys.platform != 'linux':
        return None
    numbers = _LANDLOCK_SYSCALLS.get(os.uname().machine)
    if numbers is None:
        return None
    result, err = _syscall(numbers[0], None, ctypes.c_size_t(0), _CREATE_VERSION)
    if result is None or result < 1 or err:
        return None
    return int(result)


def confine(private_dir, verdict_path):
    """Confine this process. The private directory becomes unreadable; the verdict file stays writable."""
    abi = landlock_abi()
    numbers = _LANDLOCK_SYSCALLS.get(os.uname().machine) if sys.platform == 'linux' else None
    if abi is None or numbers is None:
        return False
    handled = _READ_FILE | _READ_DIR | _WRITE_FILE
    if abi >= 3:
        handled |= _TRUNCATE
    attr = _RulesetAttr(handled)
    ruleset, err = _syscall(numbers[0], ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset is None or err:
        return False
    ruleset = int(ruleset)
    try:
        for path in paths_outside(private_dir):
            _allow(numbers[1], ruleset, path, handled)
        write = _WRITE_FILE | (_TRUNCATE if abi >= 3 else 0)
        if not _allow(numbers[1], ruleset, verdict_path, write):
            return False
        if _libc().prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS, required by landlock_restrict_self
            return False
        restricted, err = _syscall(numbers[2], ruleset, 0)
        return restricted is not None and not err
    finally:
        os.close(ruleset)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [('handled_access_fs', ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def _syscall(number, *args):
    ctypes.set_errno(0)
    result = _libc().syscall(ctypes.c_long(number), *args)
    err = ctypes.get_errno()
    if result < 0:
        return None, err or -1
    return result, 0


def _allow(add_rule, ruleset, path, access):
    if ctypes.sizeof(_PathBeneath) != 12:
        return False
    # O_RDONLY on a FIFO blocks until some other process opens it for write.
    # O_PATH names the inode for the Landlock rule and returns immediately.
    flags = getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    if hasattr(os, 'O_PATH'):
        flags |= os.O_PATH
    else:
        flags |= os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return False
    try:
        rule = _PathBeneath(access, fd)
        result, err = _syscall(add_rule, ruleset, _RULE_PATH_BENEATH, ctypes.byref(rule), 0)
        return result is not None and not err
    finally:
        os.close(fd)
