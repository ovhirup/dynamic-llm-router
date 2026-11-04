"""Keep a same-UID judge child from reading the verifier's private directory.

A private HOME or a 0700 temp directory does not work: the child is the same user, so it can
open the directory and read prompt.md. A container is not available on the macOS host or on
Ubuntu CI. This module wraps the judge instead:

- macOS: `sandbox-exec` allows the process's normal filesystem access and denies `file-read*`
  on the private directory. The judge can still write the verdict file. The runner reads it
  after the process exits.
- Linux: Landlock (no user namespace, so it still works where AppArmor blocks unprivileged
  `unshare`). Every other path keeps read and write. The private directory is not in that
  allow list. The verdict file is write-only. The caller points TMPDIR at a sibling directory
  so the judge's own temp files are not created beside the nonce.
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
        return [exe, '-p', macos_profile(private), *argv]
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


def macos_profile(private_dir):
    """Seatbelt profile: normal access, no reads under the private directory."""
    path = '"' + os.path.realpath(private_dir).replace('\\', '\\\\').replace('"', '\\"') + '"'
    return '(version 1)\n(allow default)\n(deny file-read* (subpath %s))\n' % path


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
    return path == secret or secret.startswith(path + os.sep) or path.startswith(secret + os.sep)


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
    flags = os.O_RDONLY | getattr(os, 'O_CLOEXEC', 0)
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
