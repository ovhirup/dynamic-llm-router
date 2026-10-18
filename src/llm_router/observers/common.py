"""Shared observer plumbing: bounded subprocess capture, pty screen capture, ANSI stripping, caches.

Every helper here has a hard deadline and never waits unboundedly on a child or its pipes, so a
hung vendor CLI degrades to an 'unknown' reading instead of blocking a refresh.
"""
from datetime import timezone
import fcntl
import json
import math
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time

ANSI = re.compile(rb'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[()][A-Z0-9]|\x1b[=>]')


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def unknown(reason, **extra):
    """The single 'no usable reading' shape: selection treats it as ineligible, never as a guess."""
    return {'evidence': None, 'status': 'unknown', 'reason': reason, 'cli_version': None,
            'version_warning': None, **extra}


def screen_text(raw):
    """Bytes from a TUI -> one line of plain text (escapes and box-drawing removed). Raises on bad UTF-8."""
    text = ANSI.sub(b' ', raw).decode('utf-8', 'strict')
    return ' '.join(re.sub(r'[\u2500-\u257f]', ' ', text).split())


def valid_timeout(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _kill_group(pid, sig):
    for kill in (os.killpg, os.kill):
        try:
            kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def run_capture(argv, timeout, cwd=None, env=None, input_text=None):
    """Run argv in its own session; return (returncode or None on timeout, stdout text).

    Unlike subprocess.run(timeout=...), a grandchild that keeps the pipe open cannot make this block:
    on timeout the whole process group is killed and the pipes are closed without waiting for EOF."""
    if not valid_timeout(timeout):
        raise ValueError('invalid timeout')
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if input_text is not None
                                   else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
    except OSError:
        return None, ''
    try:
        out, _ = process.communicate(input_text.encode() if input_text is not None else None, timeout=timeout)
        return process.returncode, out.decode('utf-8', 'replace')
    except subprocess.TimeoutExpired:
        _kill_group(process.pid, signal.SIGKILL)
        for stream in (process.stdout, process.stdin):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return None, ''
    finally:
        _kill_group(process.pid, signal.SIGKILL)


def capture_pty(argv, timeout_seconds, steps, warmup_seconds, cwd=None, env_updates=None,
                rows=50, cols=160):
    """Start argv on a pty, wait warmup, then type each (keys, wait) step; return captured bytes.

    Hard deadline for the whole capture; the child session is always killed and reaped (bounded)."""
    if not valid_timeout(timeout_seconds):
        raise ValueError('Invalid capture timeout')
    if not Path(argv[0]).exists():
        raise FileNotFoundError(argv[0])
    deadline = time.monotonic() + timeout_seconds
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.environ.update({'TERM': 'xterm-256color', 'COLUMNS': str(cols), 'LINES': str(rows),
                               **(env_updates or {})})
            if cwd:
                os.chdir(cwd)
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            os.execv(argv[0], argv)
        finally:
            os._exit(127)
    captured = bytearray()

    def pump(seconds, retain=True):
        end = min(deadline, time.monotonic() + seconds)
        while (remaining := end - time.monotonic()) > 0:
            if select.select([fd], [], [], min(0.2, remaining))[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return False
                if not chunk:
                    return False
                if retain:
                    captured.extend(chunk)
        return True

    def send(keys):
        # Never block on a full pty input buffer: write only when writable, before the deadline.
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], min(1.0, remaining))[1]:
            return False
        try:
            os.write(fd, keys)
            return True
        except OSError:
            return False

    try:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
        except OSError:
            pass
        alive = pump(warmup_seconds, retain=False)
        for keys, seconds in steps:
            if not alive or time.monotonic() >= deadline or not send(keys):
                break
            alive = pump(seconds)
        return bytes(captured)
    finally:
        try:
            os.set_blocking(fd, False)
            os.write(fd, b'\x1b')
        except OSError:
            pass
        try:
            os.close(fd)  # close before reaping: a child with unread pty output can block in exit
        except OSError:
            pass
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
            _kill_group(pid, sig)
            if sig != signal.SIGKILL:
                time.sleep(0.2)
        end = time.monotonic() + 3
        while time.monotonic() < end:
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if done:
                break
            time.sleep(0.05)


def save_cache(observation, path):
    """Atomically save a minimized observation with owner-only permissions."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
            temporary = handle.name
            os.fchmod(handle.fileno(), 0o600)
            json.dump(observation, handle, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def load_cache(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError, UnicodeError):
        return None
