"""Claude guard mod (#101): block secrets reads, pushes, and destructive git inside headless Claude jobs.

Provides:
  1. `evaluate_tool_call(tool_name, tool_input, worktree=None)` (fails closed on any error):
     - Blocks Read/Edit/Write on `.env`, `.env.*` (allows `.env.example`), `~/.codex/**`,
       `~/.config/llm-router/**`, `*.p8`, `auth.json`.
     - Blocks `git push` (any), `git push --force`, `git reset --hard`, `git merge` into `main`/`master`,
       `gh pr merge`, and `gh pr review --approve`.
     - Blocks recursive `rm` outside the job `worktree` (any flag spelling, after newlines, continuations, `cd`,
       `bash -c`, `eval` or `$(...)`); unresolvable targets such as `$HOME` are blocked.
     - Paths are checked as written and after following symlinks; Grep/Glob/LS patterns and search roots that
       name or contain a secret are blocked (#215). Mirrors `mods/claude-guard/guard-lib.mjs`.
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


FILE_KEYS = ('file_path', 'path', 'notebook_path')
SEARCH_TOOLS = {'grep', 'glob', 'ls'}
SHELLS = {'sh', 'bash', 'zsh', 'dash', 'ksh'}
WRAPPERS = {'sudo', 'command', 'builtin', 'exec', 'nohup', 'time', 'nice', 'env', 'xargs', 'doas'}
FEEDERS = {'xargs', 'parallel', 'rush'}
# Recursive-capable readers and every option the guard understands, fully spelled (mirrors READERS in
# guard-lib.mjs). Anything else (an abbreviation, an unknown long option, -d/--directories, a path-list option such
# as --files-from, -x or -files0-from) means the guard cannot tell what is read: the call is blocked.
_GREP = {'short': 'rRnilvwxcHhsqoLzZaIFEGPUbTuyV', 'short_value': 'ABCmef', 'explicit': 'ef', 'recursive_short': 'rR',
         'long': {'--recursive', '--dereference-recursive', '--line-number', '--ignore-case', '--no-ignore-case',
                  '--invert-match', '--word-regexp', '--line-regexp', '--count', '--with-filename', '--no-filename',
                  '--no-messages', '--quiet', '--silent', '--only-matching', '--files-with-matches',
                  '--files-without-match', '--null', '--null-data', '--text', '--fixed-strings', '--extended-regexp',
                  '--basic-regexp', '--perl-regexp', '--byte-offset', '--initial-tab', '--line-buffered'},
         'long_value': {'--regexp', '--file', '--include', '--exclude', '--exclude-dir', '--max-count',
                        '--after-context', '--before-context', '--context'},
         'explicit_long': {'--regexp', '--file'}, 'recursive_long': {'--recursive', '--dereference-recursive'},
         'optional_eq': {'--color', '--colour'}, 'pattern': True, 'always': False}
READERS = {
    'grep': _GREP, 'egrep': _GREP, 'fgrep': _GREP,
    'rg': {'short': 'isSnNlcvwxFuLHzaoqUP0', 'short_value': 'egtTABCmjMfr', 'explicit': 'ef', 'recursive_short': '',
           'long': {'--ignore-case', '--smart-case', '--case-sensitive', '--line-number', '--no-line-number',
                    '--files-with-matches', '--files-without-match', '--count', '--count-matches', '--invert-match',
                    '--word-regexp', '--line-regexp', '--fixed-strings', '--hidden', '--no-ignore', '--follow',
                    '--json', '--files', '--vimgrep', '--heading', '--no-heading', '--with-filename', '--no-filename',
                    '--only-matching', '--multiline', '--pcre2', '--null', '--text', '--unrestricted'},
           'long_value': {'--regexp', '--file', '--glob', '--iglob', '--type', '--type-not', '--max-count',
                          '--context', '--after-context', '--before-context', '--max-depth', '--threads', '--replace',
                          '--max-filesize', '--sort', '--sortr'},
           'explicit_long': {'--regexp', '--file'}, 'recursive_long': set(), 'optional_eq': {'--color', '--colour'},
           'pattern': True, 'always': True},
    'ag': {'short': 'ilLcvwQsSuUfHtan', 'short_value': 'GgmACB', 'explicit': '', 'recursive_short': '',
           'long': {'--hidden', '--ignore-case', '--literal', '--files-with-matches', '--count', '--invert-match',
                    '--word-regexp', '--follow', '--unrestricted', '--nocolor', '--nogroup', '--filename',
                    '--nofilename', '--vimgrep', '--skip-vcs-ignores', '--all-text', '--all-types'},
           'long_value': {'--ignore', '--file-search-regex', '--max-count', '--context', '--after', '--before',
                          '--depth'},
           'explicit_long': set(), 'recursive_long': set(), 'optional_eq': {'--color'}, 'pattern': True,
           'always': True},
    'ack': {'short': 'ilLcvwQhHnks', 'short_value': 'ABCm', 'explicit': '', 'recursive_short': '',
            'long': {'--ignore-case', '--literal', '--count', '--invert-match', '--word-regexp', '--with-filename',
                     '--no-filename', '--follow', '--nofollow', '--nocolor', '--heading', '--noheading',
                     '--smart-case', '--break', '--nobreak', '--group', '--nogroup'},
            'long_value': {'--type', '--ignore-dir', '--max-count', '--context', '--after-context', '--before-context',
                           '--match'},
            'explicit_long': {'--match'}, 'recursive_long': set(), 'optional_eq': {'--color'}, 'pattern': True,
            'always': True},
    'tree': {'short': 'adflisughDFCnNpqQrtUvJxX', 'short_value': 'LPI', 'explicit': '', 'recursive_short': '',
             'long': {'--noreport', '--dirsfirst', '--prune', '--matchdirs', '--ignore-case', '--du', '--si'},
             'long_value': {'--charset', '--filelimit', '--timefmt', '--sort'}, 'explicit_long': set(),
             'recursive_long': set(), 'optional_eq': set(), 'pattern': False, 'always': True},
}
FIND_LEADING = {'-H', '-L', '-P', '-E', '-X', '-s', '-x', '-d'}
FIND_PRIMARIES = {'-name', '-iname', '-path', '-ipath', '-wholename', '-iwholename', '-regex', '-iregex', '-regextype',
                  '-type', '-xtype', '-maxdepth', '-mindepth', '-mtime', '-mmin', '-atime', '-amin', '-ctime', '-cmin',
                  '-newer', '-anewer', '-cnewer', '-size', '-empty', '-print', '-print0', '-printf', '-prune', '-not',
                  '-and', '-or', '-o', '-a', '-follow', '-xdev', '-mount', '-depth', '-perm', '-user', '-group',
                  '-nouser', '-nogroup', '-uid', '-gid', '-links', '-inum', '-samefile', '-ls', '-delete', '-quit',
                  '-readable', '-writable', '-executable', '-true', '-false', '-lname', '-ilname', '-noleaf',
                  '-daystart'}
FIND_EXEC = {'-exec', '-execdir', '-ok', '-okdir'}
GLOB_CHARS = re.compile(r'[*?[{]')
ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
GLOB_LIMIT = 20000
SEARCH_LIMIT = 20000
DOT_SECRETS = ('.env', '.env.local', '.codex')


def _home():
    return os.environ.get('HOME') or os.path.expanduser('~')


def _expand(text):
    """Plain concatenation (as guard-lib.mjs): `~/link/..` keeps its `..` until the link is followed."""
    return _home() if text == '~' else _home().rstrip('/') + '/' + text[2:] if text.startswith('~/') else text


def resolve_real(text, cwd):
    """Component-wise realpath, the same walk as guard-lib.mjs resolveReal: an existing symlink is resolved before
    the next component (so `link/../x` leaves from the link's target, as the kernel does); missing components are
    kept. A dangling or looping symlink makes the result unknowable: None, and callers block."""
    expanded = _expand(str(text))
    if os.path.isabs(expanded):
        current = '/'
    else:
        current = os.path.abspath(str(cwd))
        try:
            current = os.path.realpath(current, strict=True)
        except (OSError, RuntimeError):
            pass
    for segment in expanded.split('/'):
        if segment in ('', '.'):
            continue
        if segment == '..':
            current = os.path.dirname(current)
            continue
        nxt = os.path.join(current, segment)
        if os.path.islink(nxt):
            try:
                current = os.path.realpath(nxt, strict=True)
            except (OSError, RuntimeError):
                return None
        else:
            current = nxt
    return current


def _denied_name(text):
    norm = text.replace('\\', '/')
    name = norm.rsplit('/', 1)[-1].lower() if not norm.endswith('/') else norm.rstrip('/').rsplit('/', 1)[-1].lower()
    if name == '.env' or (name.startswith('.env.') and name != '.env.example'):
        return f'blocked secret file access: {name}'
    if name.endswith('.p8') or name == 'auth.json':
        return f'blocked credential file access: {name}'
    if re.search(r'(^|/)\.codex(/|$)', norm, re.I):
        return 'blocked access to ~/.codex'
    if re.search(r'(^|/)\.config/llm-router(/|$)', norm, re.I):
        return 'blocked access to ~/.config/llm-router'
    return None


def forbidden_path(raw, cwd):
    """A path is checked as written (spaces kept: `a ` and `a` are different files) and after following symlinks."""
    text = '' if raw is None else str(raw)
    if not text.strip():
        return None
    literal = _denied_name(_expand(text))
    if literal:
        return literal
    real = resolve_real(text, cwd)
    return f'blocked path through a dangling or looping symlink: {text}' if real is None else _denied_name(real)


def glob_regex(segment):
    """One glob segment as an anchored regex: * ? [..] [!..] {a,b}; dotfiles match like any name."""
    out, i = '', 0
    while i < len(segment):
        ch = segment[i]
        if ch == '*':
            out += '[^/]*'
        elif ch == '?':
            out += '[^/]'
        elif ch == '[':
            end = segment.find(']', i + 2)
            if end < 0:
                out += r'\['
            else:
                body = segment[i + 1:end]
                negate = body[:1] in ('!', '^')
                body = body[1:] if negate else body
                out += '[' + ('^' if negate else '') + re.sub(r'([\\\]^])', r'\\\1', body) + ']'
                i = end
        elif ch == '{':
            end = segment.find('}', i + 1)
            if end < 0:
                out += r'\{'
            else:
                alternatives = [glob_regex(alt).pattern[1:-1] for alt in segment[i + 1:end].split(',')]
                out += '(?:' + '|'.join(alternatives) + ')'
                i = end
        else:
            out += re.escape(ch)
        i += 1
    return re.compile('^' + out + '$', re.I)


def forbidden_glob(raw):
    """A glob or search pattern naming a secret: .env (not .env.example), .p8, auth.json, .codex or
    .config/llm-router; a dot-segment glob that can match .env/.env.local/.codex; or .config then llm-router."""
    text = _expand(str(raw if raw is not None else '').strip()).lower().replace('\\', '/')
    if not text:
        return None
    if '.config/llm-router' in text:
        return 'blocked pattern naming ~/.config/llm-router'
    segments = text.split('/')
    for k, segment in enumerate(segments):
        if '.env' in segment and segment != '.env.example':
            return f'blocked pattern naming secret files: {raw}'
        if '.p8' in segment or 'auth.json' in segment:
            return f'blocked pattern naming credential files: {raw}'
        if '.codex' in segment:
            return 'blocked pattern naming ~/.codex'
        if segment.startswith('.') and GLOB_CHARS.search(segment):
            rx = glob_regex(segment)
            if any(rx.match(name) for name in DOT_SECRETS):
                return f'blocked pattern that matches secret files: {raw}'
            if rx.match('.config') and k + 1 < len(segments) and glob_regex(segments[k + 1]).match('llm-router'):
                return 'blocked pattern that matches ~/.config/llm-router'
    return None


def _descendants(root):
    """Directories under root, root included, without following symlinks; None past GLOB_LIMIT."""
    found, k = [root], 0
    while k < len(found):
        try:
            entries = list(os.scandir(found[k]))
        except OSError:
            entries = []
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                found.append(os.path.join(found[k], entry.name))
            if len(found) > GLOB_LIMIT:
                return None
        k += 1
    return found


def expand_glob(word, cwd):
    """(overflow, matches): the glob expanded against the filesystem segment by segment (`**` = any depth)."""
    expanded = _expand(str(word))
    current = ['/' if os.path.isabs(expanded) else os.path.abspath(str(cwd))]
    for segment in expanded.split('/'):
        if segment in ('', '.'):
            continue
        if segment == '..':
            current = [os.path.dirname(p) for p in current]
        elif segment == '**':
            found = []
            for directory in current:
                below = _descendants(directory)
                if below is None:
                    return True, []
                found += below
                if len(found) > GLOB_LIMIT:
                    return True, []
            current = found
        elif not GLOB_CHARS.search(segment):
            current = [os.path.join(p, segment) for p in current]
        else:
            rx, found = glob_regex(segment), []
            for directory in current:
                try:
                    names = os.listdir(directory)
                except OSError:
                    names = []
                found += [os.path.join(directory, name) for name in names if rx.match(name)]
                if len(found) > GLOB_LIMIT:
                    return True, []
            current = found
    return False, current


def _forbidden_wildcard(word, cwd):
    if not GLOB_CHARS.search(word):
        return None
    named = forbidden_glob(word)
    if named:
        return named
    overflow, matches = expand_glob(word, cwd)
    if overflow:
        return f'blocked glob too broad to check: {word}'
    return next(filter(None, (forbidden_path(m, cwd) for m in matches)), None)


def _search_root_covering(root):
    """A search rooted at an ancestor of a secret directory (for example $HOME) reads it too."""
    for secret in (os.path.join(_home(), '.codex'), os.path.join(_home(), '.config', 'llm-router')):
        real = resolve_real(secret, '/')
        for candidate in {secret, real or secret}:
            if candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep):
                return f'blocked search rooted at {root}: it covers {candidate}'
    return None


def tree_reaches_secret(raw, cwd):
    """A recursive read of raw (grep -r, rg, find, the Grep tool) reaches a secret: the root covers ~/.codex or
    ~/.config/llm-router, or a walk (not following symlinks, checking where they point) meets a secret name."""
    root = resolve_real(raw, cwd)
    if root is None:
        return f'blocked recursive read through a dangling or looping symlink: {raw}'
    direct = _denied_name(root) or _search_root_covering(root)
    if direct:
        return direct
    seen, stack = 0, [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            entries = []
        for entry in entries:
            seen += 1
            if seen > SEARCH_LIMIT:
                return f'blocked recursive read too large to check: {raw} (narrow the path)'
            full = os.path.join(directory, entry.name)
            named = _denied_name(full)
            if named:
                return f'{named} (inside {raw})'
            if entry.is_symlink():
                try:
                    real = os.path.realpath(full, strict=True)
                except (OSError, RuntimeError):
                    continue  # a dangling link reads nothing
                why = _denied_name(real) or _search_root_covering(real)
                if why:
                    return f'{why} (via {full})'
            elif entry.is_dir(follow_symlinks=False):  # .git too: grep -r reads it, a planted link can hide there
                stack.append(full)
    return None


_ANSI_NAMED = {'n': '\n', 't': '\t', 'r': '\r', 'a': '\x07', 'b': '\b', 'e': '\x1b', 'E': '\x1b', 'f': '\f',
               'v': '\v', '\\': '\\', "'": "'", '"': '"', '?': '?'}


def _ansi_c(body):
    out, j = '', 0
    while j < len(body):
        if body[j] != '\\':
            out += body[j]
            j += 1
            continue
        j += 1
        n = body[j] if j < len(body) else None
        if n is None:
            out += '\\'
        elif n in _ANSI_NAMED:
            out += _ANSI_NAMED[n]
        elif n in 'xuU':
            m = re.match('[0-9a-fA-F]{1,%d}' % {'x': 2, 'u': 4, 'U': 8}[n], body[j + 1:])
            if m:
                out += chr(int(m.group(0), 16))
                j += len(m.group(0))
            else:
                out += '\\' + n
        elif n in '01234567':
            m = re.match('[0-7]{1,3}', body[j:])
            out += chr(int(m.group(0), 8))
            j += len(m.group(0)) - 1
        elif n == 'c' and j + 1 < len(body):
            j += 1
            out += chr(ord(body[j].upper()) ^ 64)
        else:
            out += '\\' + n
        j += 1
    return out


def parse_shell(script):
    """(commands, nested, piped, redirects): simple commands as lists of unquoted words, split on newline ; & | ( ),
    with line continuations joined. A redirection (`<` `>` `>>` `<>` `>&` `&>` `&>>` `>|` and an fd prefix such as
    `2>`) does not end the command and its target is not an operand: targets come back in `redirects`, parallel to
    `commands`. Braces stay in the word; $'...' (ANSI-C, decoded) and $"..." are strings; $(...), `...` and <(...)
    bodies come back as nested scripts and leave a '$' in the word. Mirrors guard-lib.mjs parseShell."""
    src = re.sub(r'\\\r?\n', '', str(script if script is not None else ''))
    commands, nested, redirects, words, word = [], [], [], [], None
    pending_redirects, taking_redirect = [], False
    i, n = 0, len(src)

    def substitution(start):  # src[start] is the '(' of $( or <(
        depth = 0
        for j in range(start, n):
            if src[j] == '(':
                depth += 1
            elif src[j] == ')':
                depth -= 1
                if depth == 0:
                    nested.append(src[start + 1:j])
                    return j + 1
        nested.append(src[start + 1:])
        return n

    def backtick(start):
        end = src.find('`', start + 1)
        nested.append(src[start + 1:end if end >= 0 else n])
        return end + 1 if end >= 0 else n

    def end_word():
        nonlocal word, taking_redirect
        if word is not None:
            if taking_redirect:
                pending_redirects.append(word)
                taking_redirect = False
            else:
                words.append(word)
        word = None

    piped, pipe_seen = [], False  # piped[n]: a '|' came anywhere before commands[n]

    def end_command(separator=None):
        nonlocal words, pipe_seen, pending_redirects, taking_redirect
        end_word()
        if words or pending_redirects:
            commands.append(words)
            piped.append(pipe_seen)
            redirects.append(list(pending_redirects))
        words, pending_redirects, taking_redirect = [], [], False
        if separator == '|':
            pipe_seen = True

    def consume_redirect(at):
        """Index after a redirection operator starting at `at`, or None when `src[at]` is not one."""
        if src.startswith('&>>', at):
            return at + 3
        if src.startswith('&>', at):
            return at + 2
        if at >= n or src[at] not in '<>':
            return None
        j = at + 1
        if src[at] == '>':
            if j < n and src[j] == '>':  # >>
                j += 1
            elif j < n and src[j] in '&|':  # >& >|
                j += 1
        elif j < n and src[j] == '<':  # << <<- <<<
            j += 1
            if j < n and src[j] in '<-':
                j += 1
        elif j < n and src[j] in '>&':  # <> <&
            j += 1
        return j

    while i < n:
        ch = src[i]
        if ch == '$' and src[i + 1:i + 2] == "'":
            j = i + 2
            while j < n and src[j] != "'":
                j += 2 if src[j] == '\\' else 1
            word = (word or '') + _ansi_c(src[i + 2:min(j, n)])
            i = j + 1
        elif ch == '$' and src[i + 1:i + 2] == '"':
            i += 1  # $"..." is a double-quoted string
        elif ch == '\\':
            word = (word or '') + src[i + 1:i + 2]
            i += 2
        elif ch == "'":
            end = src.find("'", i + 1)
            word = (word or '') + src[i + 1:end if end >= 0 else n]
            i = end + 1 if end >= 0 else n
        elif ch == '"':
            i += 1
            word = word or ''
            while i < n and src[i] != '"':
                if src[i] == '\\' and i + 1 < n:
                    word += src[i + 1]
                    i += 2
                elif src[i] == '$' and src[i + 1:i + 2] == '(':
                    word += '$'
                    i = substitution(i + 1)
                elif src[i] == '`':
                    word += '$'
                    i = backtick(i)
                else:
                    word += src[i]
                    i += 1
            i += 1
        elif ch in '$<' and src[i + 1:i + 2] == '(':
            word = (word or '') + '$'
            i = substitution(i + 1)
        elif ch == '`':
            word = (word or '') + '$'
            i = backtick(i)
        elif ch in ' \t':
            end_word()
            i += 1
        elif ch == '|' and src[i + 1:i + 2] == '|':
            end_command('||')
            i += 2
        elif (op_end := consume_redirect(i)) is not None and not (ch == '<' and src[i + 1:i + 2] == '('):
            # A digit glued to the operator (`2>file`) is its file descriptor. A digit that is already the target
            # of a redirect (`>2>file`) is a filename and stays with the redirect it belongs to.
            if taking_redirect and word is not None:
                end_word()
            elif ch != '&' and word is not None and word.isdigit():
                word = None
            else:
                end_word()
            taking_redirect = True
            i = op_end
        elif ch in '\n;&|()':
            end_command(ch)
            i += 1
        else:
            word = (word or '') + ch
            i += 1
    end_command()
    return commands, nested, piped, redirects


def _strip_prefix(words):
    i = 0
    while i < len(words):
        w = words[i]
        if ASSIGNMENT.match(w):
            i += 1
        elif os.path.basename(w) in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith('-') or ASSIGNMENT.match(words[i])):
                i += 1
        elif os.path.basename(w) == 'timeout':
            i += 2
        else:
            return words[i:]
    return []


def _check_rm(args, root, cwd, moved):
    """A recursive rm names every target statically; each resolves against the real cwd and stays inside the job
    root. An argument built at runtime ($VAR, $(...), `...`) could be a recursive flag or target: blocked."""
    dynamic = next((a for a in args if re.search(r'[$`]', a)), None)
    if dynamic is not None:
        return f'blocked rm with an argument built at runtime: {dynamic}'
    recursive, end_options, targets = False, False, []
    for arg in args:
        if not end_options and arg == '--':
            end_options = True
        elif not end_options and arg.startswith('--'):
            recursive = recursive or arg == '--recursive'
        elif not end_options and arg.startswith('-') and len(arg) > 1:
            recursive = recursive or bool(re.search('[rR]', arg[1:]))
        else:
            targets.append(arg)
    if not recursive:
        return None
    if not targets:
        return 'blocked recursive rm whose targets come at runtime (list explicit paths)'
    wt = resolve_real(root, '/') if root else None
    if not wt:
        return 'blocked recursive rm without a known job root'
    for target in targets:
        if re.search(r'[{}]', target):
            return f'blocked recursive rm with brace expansion: {target} (list explicit paths)'
        if target.startswith('~') and target != '~' and not target.startswith('~/'):
            return f'blocked recursive rm of {target}'
        if moved and not os.path.isabs(_expand(target)):
            return f'blocked recursive rm of a relative target after cd: {target}'
        segments = target.split('/')
        glob = bool(GLOB_CHARS.search(target))
        if glob and ('..' in segments or any(GLOB_CHARS.search(s) for s in segments[:-1])):
            return f'blocked recursive rm of a glob that can leave its directory: {target} (list explicit paths)'
        fixed = ('/'.join(segments[:-1]) or ('/' if target.startswith('/') else '.')) if glob else target
        real = resolve_real(fixed, cwd)
        if real is None:
            return f'blocked recursive rm through a dangling or looping symlink: {target}'
        if not (real.startswith(wt + os.sep) or (glob and real == wt)):
            return f'blocked rm -rf outside worktree: {target}'
    return None


def parse_reader(name, args):
    """{'bad': option} for the first option the guard does not fully understand, else {'recursive', 'paths'}. Only
    fully spelled, listed options pass, so no spelling can hide recursion or a path (mirrors parseReader)."""
    if name == 'find':
        return _parse_find(args)
    spec = READERS[name]
    plain, explicit, recursive, k = [], False, spec['always'], 0
    while k < len(args):
        arg = args[k]
        if arg == '--':
            plain += args[k + 1:]
            break
        if arg.startswith('--'):
            flag = arg.split('=', 1)[0]
            if flag in spec['long_value']:
                k += 0 if '=' in arg else 1
            elif flag in spec['optional_eq']:
                pass  # a bare flag or flag=value
            elif flag in spec['long'] and '=' not in arg:
                recursive = recursive or flag in spec['recursive_long']
                k += 1
                continue
            else:
                return {'bad': arg}
            explicit = explicit or flag in spec['explicit_long']
        elif arg.startswith('-') and len(arg) > 1:
            for c in range(1, len(arg)):
                letter = arg[c]
                if letter in spec['short_value']:
                    explicit = explicit or letter in spec['explicit']
                    if c == len(arg) - 1:
                        k += 1  # the value is the next word, else the rest of the cluster
                    break
                if letter not in spec['short']:
                    return {'bad': arg}
                recursive = recursive or letter in spec['recursive_short']
        else:
            plain.append(arg)
        k += 1
    return {'recursive': recursive, 'paths': plain[1:] if spec['pattern'] and not explicit else plain}


def _parse_find(args):
    k, paths = 0, []
    while k < len(args) and (args[k] in FIND_LEADING or re.fullmatch(r'-O\d', args[k]) or args[k] == '-f'):
        if args[k] == '-f':
            paths.append(args[k + 1] if k + 1 < len(args) else '.')
        k += 2 if args[k] == '-f' else 1
    while k < len(args) and not (args[k].startswith('-') or args[k] in ('(', '!', ')', ',')):
        paths.append(args[k])
        k += 1
    paths = paths or ['.']
    while k < len(args):
        arg = args[k]
        if arg in FIND_EXEC:  # the executed command's own words run until ';' or '+'
            while k + 1 < len(args) and args[k + 1] not in (';', '+'):
                k += 1
            k += 1
        elif arg.startswith('-') and arg not in FIND_PRIMARIES:
            return {'bad': arg}
        k += 1
    return {'recursive': True, 'paths': paths}


def _check_reader(name, args, cwd, fed):
    """One rule for grep/rg/ag/ack/find/tree: options the guard cannot fully parse are blocked; a recursive read fed
    by a pipe or a feeder (xargs, parallel, rush), or with an argument built at runtime, is blocked; every path it
    reads must not reach a secret."""
    parsed = parse_reader(name, args)
    if 'bad' in parsed:
        return f"blocked {name} with an option the guard cannot fully parse: {parsed['bad']} (spell options in full)"
    if not parsed['recursive']:
        return None
    if fed:
        return f'blocked recursive {name} fed by a pipe or xargs/parallel/rush: its paths come at runtime'
    dynamic = next((a for a in args if re.search(r'[$`]', a)), None)
    if dynamic is not None:
        return f'blocked recursive {name} with an argument built at runtime: {dynamic}'
    for target in parsed['paths'] or ['.']:
        why = tree_reaches_secret(target, cwd)
        if why:
            return why
    return None


def _check_git(name, args):
    if name == 'gh':
        if args[:2] == ['pr', 'merge']:
            return 'blocked gh pr merge in headless Claude job'
        if args[:2] == ['pr', 'review'] and any(a in ('--approve', '-a') for a in args):
            return 'blocked gh pr review --approve in headless Claude job'
        return None
    i = 0
    while i < len(args) and args[i].startswith('-'):
        i += 2 if args[i] in ('-C', '-c', '--git-dir', '--work-tree', '--namespace') else 1
    sub, rest = (args[i] if i < len(args) else None), args[i + 1:]
    if sub == 'push':
        return 'blocked git push in headless Claude job'
    if sub == 'reset' and '--hard' in rest:
        return 'blocked git reset --hard in headless Claude job'
    if sub == 'merge' and any(re.search(r'\b(?:main|master)\b', a) for a in rest):
        return 'blocked git merge into main/master'
    return None


def _check_script(script, root, cwd, depth=0):
    if depth > 8:
        return 'blocked deeply nested shell command'
    if FORBIDDEN_GIT_RE.search(re.sub(r'\s+', ' ', re.sub(r'\\\r?\n', ' ', str(script)))):
        return 'blocked destructive git/gh command in headless Claude job'
    commands, nested, piped, redirects = parse_shell(script)
    for inner in nested:
        why = _check_script(inner, root, cwd, depth + 1)
        if why:
            return why
    moved = False  # after cd/pushd/popd a relative path no longer resolves against the hook's cwd

    def scan(text):
        for piece in text.split('='):
            why = forbidden_path(piece, cwd) or _forbidden_wildcard(piece, cwd)
            if why:
                return why
        return None

    for index, raw in enumerate(commands):
        for word in list(raw) + redirects[index]:
            why = scan(word)
            if why:
                return why
        # Wrappers and keywords can put a command anywhere (nice -n 5 rm, then rm, xargs rm, ! rm), so shells, eval,
        # rm and recursive readers are recognised at every position, not only the first.
        for k, word in enumerate(raw):
            name, args = os.path.basename(word), raw[k + 1:]
            why = None
            if name in SHELLS:
                flag = next((j for j, a in enumerate(args) if re.fullmatch(r'-[A-Za-z]*c[A-Za-z]*', a)), None)
                if flag is not None and flag + 1 < len(args):
                    why = _check_script(args[flag + 1], root, cwd, depth + 1)
            elif name == 'eval':
                why = _check_script(' '.join(args), root, cwd, depth + 1)
            elif name == 'rm':
                why = _check_rm(args, root, cwd, moved)
            elif name in READERS or name == 'find':
                why = _check_reader(name, args, cwd,
                                    piped[index] or any(os.path.basename(w) in FEEDERS for w in raw[:k]))
            if why:
                return why
        words = _strip_prefix(raw)
        if not words:
            continue
        name = os.path.basename(words[0])
        why = _check_git(name, words[1:]) if name in ('git', 'gh') else None
        if why:
            return why
        moved = moved or name in ('cd', 'pushd', 'popd')
    return None


def evaluate_tool_call(tool_name, tool_input, worktree=None, cwd=None):
    """Return `{'allow': bool, 'reason': str | None}`. Mirrors mods/claude-guard/guard-lib.mjs evaluateEvent:
    `worktree` is the job root containment is checked against, `cwd` where relative paths resolve (default: the
    root). Fails closed (`allow: False`) on a malformed call or any error."""
    try:
        if not isinstance(tool_name, str) or not tool_name.strip():
            return {'allow': False, 'reason': 'malformed hook event: no tool_name'}
        if not isinstance(tool_input, dict):
            return {'allow': False, 'reason': 'malformed hook event: tool_input is not an object'}
        if not worktree:  # DLR_WORKTREE is required: no trusted job root, no allow
            return {'allow': False, 'reason': 'DLR_WORKTREE is not set: no trusted job root (failing closed)'}
        name, params = tool_name.lower(), tool_input
        root = str(worktree)
        base = str(cwd) if cwd else root or os.getcwd()
        for key in FILE_KEYS:
            why = forbidden_path(params.get(key), base)
            if why:
                return {'allow': False, 'reason': why}
        if name in SEARCH_TOOLS or 'glob' in params:
            search_root = resolve_real(params.get('path') or '.', base)
            if search_root is None:
                return {'allow': False, 'reason': 'blocked search through a dangling or looping symlink'}
            why = _search_root_covering(search_root)
            glob = str(params['glob']) if params.get('glob') else None
            grep_glob = (glob if '/' in glob else f'**/{glob}') if glob else None  # ripgrep: any depth
            patterns = [p for p in (grep_glob, params.get('pattern') if name == 'glob' else None) if p]
            for pattern in map(str, patterns):
                why = why or (_forbidden_wildcard(pattern, search_root) if GLOB_CHARS.search(pattern)
                              else forbidden_path(pattern, search_root))
            if not why and name == 'glob' and params.get('pattern'):
                why = forbidden_path(GLOB_CHARS.split(str(params['pattern']))[0] or '.', search_root)
            # the Grep tool always walks: its glob syntax is ripgrep's, which the guard does not fully model
            if not why and name == 'grep':
                why = tree_reaches_secret(search_root, base)
            if why:
                return {'allow': False, 'reason': why}
        if name in SHELL_TOOLS or isinstance(params.get('command'), str):
            why = _check_script(params.get('command') or '', root, base)
            if why:
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
