"""Security helpers for run-task: template lint, redaction, denylist shims, leak check.

Pure functions plus small filesystem helpers. Nothing here reads credentials; patterns
only recognise secret *shapes* so they can be masked or flagged.
"""
import os
from pathlib import Path
import re
import stat
import subprocess

# Secret shapes (vendor key prefixes, bearer tokens, private keys, JWTs, cloud keys).
SECRET_PATTERNS = [
    ('private_key', re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)')),
    ('anthropic_key', re.compile(r'\bsk-ant-[A-Za-z0-9_\-]{16,}')),
    ('openai_key', re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{20,}')),
    ('xai_key', re.compile(r'\bxai-[A-Za-z0-9]{20,}')),
    ('cursor_key', re.compile(r'\bkey_[A-Fa-f0-9]{32,}')),
    ('github_token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})')),
    ('slack_token', re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}')),
    ('aws_access_key', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b')),
    ('google_api_key', re.compile(r'\bAIza[0-9A-Za-z_\-]{35}')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}')),
    ('bearer_token', re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*')),
]
# .env-style `NAME = value` lines where the key is an UPPER_SNAKE name that looks secret (case-sensitive on
# purpose: `key={id}` in JSX; a value followed by `(` is a call such as re.compile(...), not a literal;
# also, `event.key`, Swift `forKey:` / `CodingKeys` / `let key =` are code, not secrets).
ENV_ASSIGNMENT = re.compile(
    r'((?<![A-Za-z0-9_.$])(?:export\s+)?[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|CREDENTIAL|AUTH|PRIVATE)[A-Z0-9_]*'
    r'["\']?\s*[=:]\s*)(["\']?)([^\s"\'#,;{}()]{4,})\2(?![^\s"\'#,;{}()]*\()')
# Config-style secret names in any case (api_key, client_secret, "password": ...) with a QUOTED literal
# value of 8+ characters. A bare `key`/`token` identifier never matches; neither does an unquoted value.
QUOTED_SECRET = re.compile(
    r'(?i)((?<![A-Za-z0-9_.$])["\']?(?:[a-z0-9]+[_-])*(?:api[_-]?key|secret[_-]?key|access[_-]?key|private[_-]?key|'
    r'client[_-]?secret|auth[_-]?token|access[_-]?token|refresh[_-]?token|bearer[_-]?token|secret|password|passwd)'
    r'["\']?\s*(?:=|:|=>)\s*)(["\'])([^\s"\']{8,})\2')
# Code idioms that look like assignments to a secret-ish name but are not (only ever suppress the
# name-based rules above, never the vendor-prefix SECRET_PATTERNS).
CODE_IDIOMS = [
    re.compile(r'\bkey=\{'),                       # JSX/React key prop
    re.compile(r'\.key\b'),                         # event.key, item.key
    re.compile(r'\bforKey:'),                       # Swift/ObjC KVC and Codable
    re.compile(r'\bCodingKeys?\b'),                 # Swift Codable
    re.compile(r'\b(?:let|var|const)\s+key\s*='),   # local variable named key
]
SECRET_FLAGS = re.compile(r'(?i)^--?(api[-_]?key|token|secret|password|auth|bearer|key)(=.*)?$')
ALLOWED_PLACEHOLDERS = {'{codex}', '{grok}', '{cursor}', '{claude}', '{cwd}', '{prompt_file}', '{prompt_pointer}'}
PLACEHOLDER = re.compile(r'\{[^{}]*\}')
REDACTED = '[REDACTED]'


class LintError(ValueError):
    """A command template is unsafe to run."""


def find_secrets(text, allow=()):
    """Yield (kind, start, end) for every secret-shaped span in text.

    allow: extra compiled regexes; a line matching one suppresses only the name-based rules."""
    spans = []
    for kind, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end()))
            yield kind, match.start(), match.end()
    for kind, pattern in (('env_assignment', ENV_ASSIGNMENT), ('quoted_secret', QUOTED_SECRET)):
        for match in pattern.finditer(text):
            start, end = match.start(3), match.end(3)
            value = match.group(3)
            if any(s < end and start < e for s, e in spans):
                continue
            if value.isdigit() or PLACEHOLDER.fullmatch(value) or _looks_like_reference(value):
                continue
            if _code_idiom(text, match.start(), match.end(), allow):
                continue
            spans.append((start, end))
            yield kind, start, end


def _code_idiom(text, start, end, allow=()):
    """True when a known code idiom (or a user allow regex) overlaps this match on its line."""
    line_start = text.rfind('\n', 0, start) + 1
    line_end = text.find('\n', end)
    line = text[line_start:None if line_end < 0 else line_end]
    s, e = start - line_start, end - line_start
    return any(m.start() < e and s < m.end() for p in list(CODE_IDIOMS) + list(allow) for m in p.finditer(line))


def compile_allow(patterns):
    return [re.compile(p) for p in patterns or []]


def _looks_like_reference(value):
    return (value.startswith(('$', 'os.environ', 'process.env', '${', '<', '%(')) or
            value.lower() in {'none', 'null', 'true', 'false', 'redacted', '[redacted]', 'changeme', 'xxxxxxxx'})


def redact(text):
    """Mask every secret-shaped span; keeps surrounding text readable."""
    if not isinstance(text, str) or not text:
        return text
    spans = sorted(set((start, end) for _, start, end in find_secrets(text)))
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + REDACTED + text[end:]
    return text


def redact_obj(value):
    """Redact strings anywhere inside a JSON-like structure."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_obj(item) for key, item in value.items()}
    return value


def lint_commands(commands, allowed=ALLOWED_PLACEHOLDERS):
    """Reject argv {prompt}, unknown placeholders, secret flags and inline secrets."""
    if not isinstance(commands, dict) or not commands:
        raise LintError('run_task.commands must be a non-empty object')
    for worker, argv in commands.items():
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise LintError(f'{worker}: template must be a non-empty list of strings')
        for arg in argv:
            for token in PLACEHOLDER.findall(arg):
                if token == '{prompt}' and token not in allowed:
                    raise LintError(f'{worker}: {{prompt}} in argv exposes the task in the process list; '
                                    'use stdin, {prompt_file} or {prompt_pointer}')
                if token not in allowed:
                    raise LintError(f'{worker}: unknown placeholder {token}')
            if SECRET_FLAGS.match(arg):
                raise LintError(f'{worker}: credential flag {arg.split("=")[0]} not allowed; use the CLI\'s env var')
            if any(True for _ in find_secrets(arg)):
                raise LintError(f'{worker}: inline secret-shaped value in template')
    return True


# ---- hard block for denylisted CLIs ---------------------------------------------------

ALIASES = {'claude': ('claude', 'claude-code')}
SHIM_SCRIPT = '''#!/bin/sh
# Router hard block: this CLI is on the denylist ("never"). Logs the attempt, never the arguments.
printf '%s blocked %s cwd=%s argc=%s\\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$(basename "$0")" "$PWD" "$#" >> "{log}"
echo "router: $(basename "$0") is blocked for dispatch workers" >&2
exit 97
'''
STRIP_PREFIXES = ('ANTHROPIC_', 'CLAUDE_', 'LITELLM_')


def shim_names(never):
    names = []
    for name in never or []:
        for alias in ALIASES.get(name, (name,)):
            if alias not in names and re.fullmatch(r'[A-Za-z0-9._-]+', alias):
                names.append(alias)
    return names


def ensure_block_shims(shim_dir, log_path, never=('claude',)):
    """Write refusing shims for every denylisted CLI (and remove stale ones) in a 0700 dir."""
    shim_dir = Path(shim_dir)
    shim_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(shim_dir, 0o700)
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    body = SHIM_SCRIPT.replace('{log}', str(log_path).replace('"', ''))
    wanted = shim_names(never)
    for existing in shim_dir.iterdir():
        if existing.is_file() and existing.name not in wanted and existing.read_text(errors='replace') == body:
            existing.unlink()
    for name in wanted:
        path = shim_dir / name
        if not path.exists() or path.read_text() != body:
            path.write_text(body)
        os.chmod(path, stat.S_IRWXU)
    return shim_dir


def ensure_claude_shims(shim_dir, log_path):
    """Back-compat helper: block claude/claude-code."""
    return ensure_block_shims(shim_dir, log_path, ('claude',))


def worker_env(shim_dir, base=None, strip_prefixes=STRIP_PREFIXES):
    """Copy of the environment with the block shims first on PATH and listed env prefixes removed."""
    prefixes = tuple(p.upper() for p in strip_prefixes or ())
    env = {key: value for key, value in (os.environ if base is None else base).items()
           if not (prefixes and key.upper().startswith(prefixes))}
    env['PATH'] = str(shim_dir) + os.pathsep + env.get('PATH', '/usr/bin:/bin')
    return env


def count_lines(path):
    try:
        with open(path, 'rb') as handle:
            return sum(1 for _ in handle)
    except FileNotFoundError:
        return 0


# ---- leak check --------------------------------------------------------------------

def load_terms(path):
    """Private terms, one per line; '#' comments and blanks ignored. Missing file -> None."""
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return None
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith('#')]


def mask(value):
    value = str(value)
    if len(value) <= 4:
        return value[:1] + '***'
    return value[:2] + '***' + value[-1:] if len(value) > 8 else value[:2] + '***'


def scan_text(text, terms, allow=()):
    """Return [(line_number, kind, masked)] for secrets and private terms in text."""
    findings = []
    lowered_terms = [(term, term.lower()) for term in terms or []]
    for number, line in enumerate(text.splitlines(), 1):
        for kind, start, end in find_secrets(line, allow):
            findings.append((number, kind, mask(line[start:end])))
        lower = line.lower()
        for term, low in lowered_terms:
            if low in lower:
                findings.append((number, 'private_term', mask(term)))
    return findings


def _added_lines(cwd, base, cached=False):
    """Map file -> [(new_line_number, text)] for lines added relative to base (or empty tree)."""
    args = ['git', '-C', str(cwd), 'diff', '--no-color', '--no-ext-diff', '-U0'] + (['--cached'] if cached else [])
    args += [base] if base else ['4b825dc642cb6eb9a060e54bf8d69288fbee4904']
    out = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         errors='replace', timeout=60).stdout
    added, current, number = {}, None, 0
    for line in out.splitlines():
        if line.startswith('+++ '):
            current = None if line[4:] == '/dev/null' else line[4:].removeprefix('b/')
        elif line.startswith('@@'):
            match = re.search(r'\+(\d+)', line)
            number = int(match.group(1)) if match else 0
        elif current and line.startswith('+'):
            added.setdefault(current, []).append((number, line[1:]))
            number += 1
    return added


DEFAULT_SKIP_DIRS = ('node_modules', '.venv', 'venv', '__pycache__', '.git', 'dist', 'build', '.next',
                     'Pods', 'DerivedData', '.build', 'target', 'coverage')


def untracked_files(cwd, skip_dirs=DEFAULT_SKIP_DIRS):
    """Untracked, NOT ignored files (git ls-files --others --exclude-standard), minus vendor/build dirs."""
    out = subprocess.run(['git', '-C', str(cwd), 'ls-files', '-z', '--others', '--exclude-standard'],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace',
                         timeout=60).stdout
    skip = set(skip_dirs or ())
    return [name for name in out.split('\0') if name and not (set(Path(name).parts[:-1]) & skip)]


def _skipped(name, skip_dirs):
    return bool(set(Path(name).parts[:-1]) & set(skip_dirs or ()))


def leak_check(cwd, base, untracked, terms, max_bytes=1_000_000, cached=False, allow=(),
               skip_dirs=DEFAULT_SKIP_DIRS):
    """Scan added diff lines and untracked (non-ignored) files; never modifies anything.

    untracked=None lists them with git (ignored files and vendor/build dirs are skipped)."""
    findings = []
    try:
        if untracked is None:
            untracked = untracked_files(cwd, skip_dirs)
        for name, lines in _added_lines(cwd, base, cached).items():
            for number, text in lines:
                for _, kind, masked in scan_text(text, terms, allow):
                    findings.append({'file': f'{name}:{number}', 'kind': kind, 'match': masked})
        for name in untracked:
            if _skipped(name, skip_dirs) or Path(name).name in (skip_dirs or ()):
                continue
            path = Path(cwd) / name
            if path.is_dir():
                files = [p for p in path.rglob('*') if p.is_file()
                         and not _skipped(str(p.relative_to(cwd)), skip_dirs)]
            else:
                files = [path]
            for file in files:
                if file.is_symlink() or not file.is_file() or file.stat().st_size > max_bytes:
                    continue
                data = file.read_bytes()
                if b'\0' in data[:8192]:
                    continue
                rel = file.relative_to(cwd)
                for number, kind, masked in scan_text(data.decode('utf-8', 'replace'), terms, allow):
                    findings.append({'file': f'{rel}:{number}', 'kind': kind, 'match': masked})
    except Exception as error:  # report, don't guess clean
        return {'status': 'error', 'error': type(error).__name__, 'findings': findings}
    status = 'blocked' if findings else 'clean'
    result = {'status': status, 'findings': findings}
    if terms is None:
        result['note'] = 'private terms file missing; secret scan only'
    return result


def scan_tree(root, terms, allow=(), max_bytes=1_000_000, history=False):
    """Scan every tracked file at HEAD plus untracked non-ignored files (and optionally all history).

    Used by `llm-router scan` before publishing a repository."""
    root = Path(root)
    findings = []
    listed = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    if listed.returncode != 0:
        names = [str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()
                 and not _skipped(str(p.relative_to(root)), DEFAULT_SKIP_DIRS)]
    else:
        names = [n for n in listed.stdout.split('\0') if n]
    for name in names:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
            continue
        data = path.read_bytes()
        if b'\0' in data[:8192]:
            continue
        shown_name = name
        for term in terms or []:
            shown_name = re.sub(re.escape(term), mask(term), shown_name, flags=re.I)
        for number, kind, masked in scan_text(data.decode('utf-8', 'replace'), terms, allow):
            findings.append({'file': f'{shown_name}:{number}', 'kind': kind, 'match': masked})
        hits = [t for t in terms or [] if t.lower() in name.lower()]
        if hits:
            shown = name
            for term in hits:
                shown = re.sub(re.escape(term), mask(term), shown, flags=re.I)
            findings += [{'file': f'{shown} (path)', 'kind': 'private_term', 'match': mask(t)} for t in hits]
    if history and listed.returncode == 0:
        log = subprocess.run(['git', '-C', str(root), 'log', '--all', '-p', '--no-color', '--format=%H%n%an <%ae>%n%B'],
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace', timeout=300)
        for number, kind, masked in scan_text(log.stdout, terms, allow):
            findings.append({'file': f'git-history:{number}', 'kind': kind, 'match': masked})
    return {'status': 'blocked' if findings else 'clean', 'files_scanned': len(names), 'findings': findings,
            **({'note': 'no private terms given; secret scan only'} if terms is None else {})}
