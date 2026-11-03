"""Claude guard mod (#101): block secrets reads, pushes, and destructive git inside headless Claude jobs.

Provides:
  1. `evaluate_tool_call(tool_name, tool_input, worktree=None)` (fails closed on any error):
     - Blocks Read/Edit/Write on `.env`, `.env.*` (allows `.env.example`), `~/.codex/**`,
       `~/.config/llm-router/**`, `*.p8`, `auth.json`.
     - Blocks `git push` (any), `git push --force`, `git reset --hard`, `git merge` into `main`/`master`,
       `gh pr merge`, and `gh pr review --approve`.
     - Blocks `rm -rf` outside the job `worktree`.
  2. `check_claude_version(version_output, minimum=(2, 1, 287))` checking `claude --version >= 2.1.287`.
  3. `plugin_dir()` returning the packaged DLR Claude guard plugin directory (`mods/claude-guard`),
     and `record_blocked_attempt(log_path, reason)` writing blocked attempts to the job log.
"""
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import subprocess

from . import security

MIN_VERSION = (2, 1, 287)
FILE_TOOLS = {'read', 'edit', 'write', 'multiedit', 'notebookedit', 'read_file', 'write_file', 'edit_file'}
SHELL_TOOLS = {'bash', 'sh', 'shell', 'run_terminal_cmd', 'execute_command'}

FORBIDDEN_GIT_RE = re.compile(
    r'(?i)(?:'
    r'\bgit\s+(?:\S+\s+)*push\b|'
    r'\bgit\s+(?:\S+\s+)*reset\s+--hard\b|'
    r'\bgit\s+(?:\S+\s+)*merge\b[^\n;|&]*\b(?:main|master)\b|'
    r'\bgh\s+pr\s+merge\b|'
    r'\bgh\s+pr\s+review\b[^\n;|&]*--approve\b'
    r')'
)
RM_RF_RE = re.compile(r'\brm\s+-(?:[A-Za-z]*r[A-Za-z]*f|[A-Za-z]*f[A-Za-z]*r|--recursive)\s+(.+)$')


def parse_version(text):
    """Extract `(major, minor, patch)` from a `claude --version` string, or None."""
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', str(text or ''))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def version_supported(version_text, minimum=MIN_VERSION):
    parsed = parse_version(version_text)
    return parsed is not None and parsed >= tuple(minimum)


def probe_claude_version(executable, runner=subprocess.run):
    try:
        done = runner([str(executable), '--version'], stdin=subprocess.DEVNULL,
                      capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or done.stderr or '').strip()


def plugin_dir():
    return Path(__file__).resolve().parent / 'mods' / 'claude-guard'


def _is_forbidden_path(raw_path):
    text = str(raw_path or '').strip()
    if not text:
        return False, None
    expanded = os.path.expanduser(text)
    path = Path(expanded)
    name = path.name.lower()
    norm = expanded.replace('\\', '/')
    if name == '.env' or (name.startswith('.env.') and name != '.env.example'):
        return True, f'blocked secret file access: {path.name}'
    if name.endswith('.p8') or name == 'auth.json':
        return True, f'blocked credential file access: {path.name}'
    if '/.codex/' in norm or norm.endswith('/.codex') or text.startswith('~/.codex'):
        return True, 'blocked access to ~/.codex'
    if '/.config/llm-router/' in norm or norm.endswith('/.config/llm-router') or text.startswith('~/.config/llm-router'):
        return True, 'blocked access to ~/.config/llm-router'
    return False, None


def _check_rm_rf(command, worktree):
    for segment in re.split(r'[;&|\n]+', command or ''):
        match = RM_RF_RE.search(segment.strip())
        if not match:
            continue
        targets = [t.strip().strip('"\'') for t in match.group(1).split() if not t.startswith('-')]
        if not worktree:
            return False, 'blocked rm -rf without an active worktree boundary'
        wt = Path(worktree).expanduser().resolve()
        for target in targets:
            resolved = (wt / os.path.expanduser(target)).resolve() if not os.path.isabs(os.path.expanduser(target)) \
                else Path(os.path.expanduser(target)).resolve()
            if resolved == wt or not resolved.is_relative_to(wt):
                return False, f'blocked rm -rf outside worktree: {target}'
    return True, None


def evaluate_tool_call(tool_name, tool_input, worktree=None):
    """Return `{'allow': bool, 'reason': str | None}`. Fails closed (`allow: False`) on any error."""
    try:
        name = str(tool_name or '').strip().lower()
        params = tool_input if isinstance(tool_input, dict) else {}
        if name in FILE_TOOLS or any(k in params for k in ('file_path', 'path', 'notebook_path')):
            candidate = params.get('file_path') or params.get('path') or params.get('notebook_path') or ''
            blocked, why = _is_forbidden_path(candidate)
            if blocked:
                return {'allow': False, 'reason': why}
        if name in SHELL_TOOLS or 'command' in params:
            cmd = str(params.get('command') or '')
            if FORBIDDEN_GIT_RE.search(cmd):
                return {'allow': False, 'reason': 'blocked destructive git/gh command in headless Claude job'}
            for token in re.split(r'[\s"\'=]+', cmd):
                blocked, why = _is_forbidden_path(token)
                if blocked:
                    return {'allow': False, 'reason': why}
            ok, why = _check_rm_rf(cmd, worktree)
            if not ok:
                return {'allow': False, 'reason': why}
        return {'allow': True, 'reason': None}
    except Exception as exc:  # fail closed on any unexpected hook error
        return {'allow': False, 'reason': f'claude_guard_internal_error: {type(exc).__name__}'}


def record_blocked_attempt(log_path, reason, tool_name=''):
    """Append a redacted blocked-attempt line to `log_path`."""
    if not log_path:
        return
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    line = f'{stamp} claude-guard blocked tool={security.redact(str(tool_name))} reason={security.redact(str(reason))}\n'
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line)
