"""Live worker events (#288): normalize each worker CLI's output stream into small, redacted events.

`parse(worker, line, stream, cwd)` turns one output line into zero or more events
`{worker, stream, kind, text}`. kind is one of KINDS. Lines that are not JSON, malformed JSON and unknown
event types become `raw`. Reasoning ("thinking") output and session bookkeeping are dropped, never shown.
Streamed text chunks are marked `partial` so `EventLog` can merge them into one message.

`EventLog` appends events, with a timestamp, to a 0600 JSONL file as the worker runs. Every event is
redacted (secrets and the policy's private terms) before it is written. Parsing never raises.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading

from . import security

KINDS = ('message', 'command', 'edit', 'test', 'error', 'done', 'raw')
TEXT_LIMIT = 200
_PARTIAL_LIMIT = 4000  # merged streaming text is flushed once it gets this long
_TEST_RE = re.compile(
    r'(?i)(\bpytest\b|\bunittest\b|\btox\b|\bnox\b|\b(npm|pnpm|yarn|bun)( run)? test\b|\bgo test\b|'
    r'\bcargo test\b|\bmake (check|test)\b|\bjest\b|\bvitest\b|\brspec\b|\bmvn test\b|\bgradle test\b|'
    r'\bswift test\b|\bxcodebuild test\b|\bctest\b)')
_SHELL_WRAP = re.compile(r"""^\S*/?(?:ba|z)?sh -l?c (['"])(.*)\1$""", re.S)
_CLAUDE_EDIT_TOOLS = {'Write', 'Edit', 'MultiEdit', 'NotebookEdit'}


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _event(worker, stream, kind, text, partial=False):
    event = {'worker': worker, 'stream': stream, 'kind': kind, 'text': str(text if text is not None else '')}
    if partial:
        event['partial'] = True
    return event


def _rel(path, cwd):
    path = str(path or '')
    if cwd and path:
        for root in {str(cwd), os.path.realpath(str(cwd))}:
            root = root.rstrip('/') + '/'
            if path.startswith(root):
                return path[len(root):]
    return path


def _command(worker, stream, command):
    command = str(command or '').strip()
    match = _SHELL_WRAP.match(command)
    if match:
        command = match.group(2)
    return _event(worker, stream, 'test' if _TEST_RE.search(command) else 'command', command)


def _tool(worker, stream, name, arg):
    return _event(worker, stream, 'command', f'{name}: {arg}' if arg else str(name))


def _finished(event):
    """`finished`, with the run time when the event has one (the result text repeats the last message)."""
    ms = event.get('duration_ms')
    return f'finished in {ms / 1000:.0f}s' if type(ms) in (int, float) else 'finished'


def _codex(worker, stream, event, cwd):
    kind = event.get('type')
    item = _dict(event.get('item'))
    item_type = item.get('type')
    if kind in ('thread.started', 'turn.started'):
        return []
    if kind == 'turn.completed':
        return [_event(worker, stream, 'done', 'finished')]
    if kind == 'turn.failed':
        error = _dict(event.get('error'))
        return [_event(worker, stream, 'error', error.get('message') or 'turn failed')]
    if kind == 'error':
        return [_event(worker, stream, 'error', event.get('message') or 'error')]
    if kind not in ('item.started', 'item.updated', 'item.completed'):
        return None
    if item_type == 'reasoning':
        return []
    if item_type == 'agent_message':
        return [_event(worker, stream, 'message', item.get('text'))] if kind == 'item.completed' else []
    if item_type == 'command_execution':
        if kind == 'item.started':
            return [_command(worker, stream, item.get('command'))]
        code = item.get('exit_code')
        if kind == 'item.completed' and code not in (0, None):
            return [_event(worker, stream, 'error', f'exit {code}: {_command(worker, stream, item.get("command"))["text"]}')]
        return []
    if item_type == 'file_change':
        if kind != 'item.completed':
            return []
        changes = _list(item.get('changes'))
        return [_event(worker, stream, 'edit', _rel(change.get('path'), cwd)) for change in changes
                if isinstance(change, dict)] or [_event(worker, stream, 'edit', 'files changed')]
    if item_type == 'error':
        return [_event(worker, stream, 'error', item.get('message'))]
    if item_type in ('mcp_tool_call', 'web_search'):
        if kind != 'item.started':
            return []
        arg = item.get('query') or '.'.join(str(item.get(key)) for key in ('server', 'tool') if item.get(key))
        return [_tool(worker, stream, item_type.replace('_', ' '), arg)]
    if item_type == 'todo_list':
        return []
    return None


def _cursor(worker, stream, event, cwd):
    kind = event.get('type')
    if kind in ('system', 'user', 'thinking'):
        return []
    if kind == 'assistant':
        message = _dict(event.get('message'))
        text = ''.join(part.get('text', '') for part in message.get('content') or ()
                       if isinstance(part, dict) and part.get('type') == 'text')
        return [_event(worker, stream, 'message', text)] if text.strip() else []
    if kind == 'result':
        if event.get('is_error') or event.get('subtype') not in (None, 'success'):
            return [_event(worker, stream, 'error', event.get('result') or event.get('subtype') or 'failed')]
        return [_event(worker, stream, 'done', _finished(event))]
    if kind != 'tool_call':
        return None
    calls = _dict(event.get('tool_call'))
    name = next((key for key in calls if key.endswith('ToolCall')), None)
    if name is None:
        return []
    call = _dict(calls[name])
    args = _dict(call.get('args'))
    if event.get('subtype') == 'started':
        if name == 'shellToolCall':
            return [_command(worker, stream, args.get('command'))]
        if name in ('editToolCall', 'writeToolCall', 'deleteToolCall'):
            return [_event(worker, stream, 'edit', _rel(args.get('path'), cwd))]
        return [_tool(worker, stream, name[:-len('ToolCall')], _rel(args.get('path') or args.get('pattern'), cwd))]
    if event.get('subtype') == 'completed':
        result = _dict(call.get('result'))
        if 'failure' in result or 'error' in result:
            detail = result.get('failure') or result.get('error')
            detail = _dict(detail)
            code = detail.get('exitCode')
            what = detail.get('command') or _rel(args.get('path'), cwd) or name
            return [_event(worker, stream, 'error', f'exit {code}: {what}' if code is not None else f'failed: {what}')]
        if 'rejected' in result:
            return [_event(worker, stream, 'error', f'rejected: {args.get("command") or name}')]
    return []


def _claude(worker, stream, event, cwd):
    kind = event.get('type')
    if kind == 'system':
        if event.get('subtype') == 'task_summary' and event.get('detail'):
            return [_event(worker, stream, 'message', event['detail'])]
        return []
    if kind in ('rate_limit_event', 'stream_event'):
        return []
    message = _dict(event.get('message'))
    content = _list(message.get('content'))
    if kind == 'assistant':
        events = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'text' and str(part.get('text', '')).strip():
                events.append(_event(worker, stream, 'message', part['text']))
            elif part.get('type') == 'tool_use':
                name, args = part.get('name'), _dict(part.get('input'))
                if name in _CLAUDE_EDIT_TOOLS:
                    events.append(_event(worker, stream, 'edit',
                                         _rel(args.get('file_path') or args.get('notebook_path'), cwd)))
                elif name == 'Bash':
                    events.append(_command(worker, stream, args.get('command')))
                else:
                    arg = args.get('file_path') or args.get('path') or args.get('pattern') or args.get('description')
                    events.append(_tool(worker, stream, name, _rel(arg, cwd)))
        return events
    if kind == 'user':
        return [_event(worker, stream, 'error', part.get('content') if isinstance(part.get('content'), str)
                       else 'tool error')
                for part in content if isinstance(part, dict) and part.get('type') == 'tool_result'
                and part.get('is_error')]
    if kind == 'result':
        if event.get('is_error') or event.get('subtype') != 'success':
            return [_event(worker, stream, 'error', event.get('result') or event.get('subtype') or 'failed')]
        return [_event(worker, stream, 'done', _finished(event))]
    return None


def _grok(worker, stream, event, cwd):
    kind = event.get('type')
    if kind in ('thought', 'available_commands', 'usage'):
        return []
    if kind == 'text':
        return [_event(worker, stream, 'message', event.get('data'), partial=True)]
    if kind == 'end':
        reason = event.get('stopReason')
        return [_event(worker, stream, 'done', 'finished' if reason in (None, 'end_turn') else f'finished ({reason})')]
    if kind == 'error':
        return [_event(worker, stream, 'error', event.get('message') or event.get('data') or 'error')]
    raw = _dict(event.get('rawInput'))
    if kind == 'tool_call':
        tool = event.get('kind')
        if tool == 'execute':
            return [_command(worker, stream, raw.get('command'))]
        if tool in ('edit', 'write', 'delete', 'move'):
            return [_event(worker, stream, 'edit', _rel(raw.get('file_path') or raw.get('path'), cwd))]
        arg = raw.get('file_path') or raw.get('path') or raw.get('pattern') or raw.get('query')
        return [_tool(worker, stream, event.get('title') or tool or 'tool', _rel(arg, cwd))]
    if kind == 'tool_call_update':
        output = _dict(event.get('rawOutput'))
        code = output.get('exit_code')
        if event.get('status') == 'failed' or code not in (0, None):
            what = output.get('command') or 'tool call'
            return [_event(worker, stream, 'error', f'exit {code}: {what}' if code not in (0, None) else f'failed: {what}')]
        return []
    return None


_PARSERS = {'codex': _codex, 'cursor': _cursor, 'claude': _claude, 'grok-build': _grok, 'grok': _grok}


def _clip(text, limit=TEXT_LIMIT):
    text = ' '.join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _raw(worker, stream, text):
    return _event(worker, stream, 'raw', _clip(text, 2 * TEXT_LIMIT))


def parse(worker, line, stream='stdout', cwd=None):
    """Zero or more events for one output line. Never raises; anything unrecognised becomes `raw`."""
    try:
        text = line.decode('utf-8', 'replace') if isinstance(line, bytes) else str(line)
        stripped = text.strip()
        if not stripped:
            return []
        if not stripped.startswith('{'):
            return [_raw(worker, stream, stripped)]
        try:
            event = json.loads(stripped)
        except ValueError:
            return [_raw(worker, stream, stripped)]
        if not isinstance(event, dict):
            return [_raw(worker, stream, stripped)]
        handler = _PARSERS.get(worker)
        events = handler(worker, stream, event, cwd) if handler else None
        if events is None:
            label = ' '.join(str(event[key]) for key in ('type', 'subtype') if isinstance(event.get(key), str))
            return [_raw(worker, stream, label or stripped)]
        return [item for item in events if item['kind'] in KINDS]
    except Exception:  # never let a strange line break a worker run
        return [_raw(worker, stream, str(line)[:2 * TEXT_LIMIT])]


def redact_text(text, private_terms=()):
    """Secrets and private terms removed, whitespace collapsed, capped at TEXT_LIMIT characters."""
    text = security.redact(' '.join(str(text).split()))
    for term in private_terms or ():
        if term:
            text = re.sub(re.escape(str(term)), security.REDACTED, text, flags=re.I)
    return _clip(text)


_FATAL = {'codex': ('turn.failed', 'error')}  # codex item-level errors are warnings (e.g. plugin config)


def last_error(worker, output, private_terms=()):
    """Redacted text of the last fatal `error` event in a worker's captured stdout, or None."""
    found = None
    fatal = _FATAL.get(worker)
    for line in (output or '').splitlines():
        if fatal is not None:
            try:
                kind = json.loads(line).get('type')
            except (ValueError, AttributeError):
                continue
            if kind not in fatal:
                continue
        for event in parse(worker, line):
            if event['kind'] == 'error' and event['text'].strip():
                found = event['text']
    return redact_text(found, private_terms) if found else None


class EventLog:
    """Append redacted live events for one worker attempt to a 0600 JSONL file. Thread-safe.

    `feed(stream, line)` is called for every output line as it arrives (from both stdout and stderr
    reader threads). Consecutive `partial` messages on one stream are merged into a single message.
    Any write error disables the log silently: live capture must never fail a worker run."""

    def __init__(self, path, worker, cwd=None, private_terms=(), clock=None):
        self.path = Path(path)
        self.worker, self.cwd = worker, cwd
        self.private_terms = tuple(term for term in private_terms or () if term)
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec='milliseconds'))
        self._lock = threading.Lock()
        self._pending = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        self._handle = os.fdopen(fd, 'a', encoding='utf-8')

    def feed(self, stream, line):
        with self._lock:
            if self._handle is None:
                return
            for event in parse(self.worker, line, stream, self.cwd):
                if event.pop('partial', False):
                    if self._pending is not None and self._pending['stream'] != stream:
                        self._flush()
                    if self._pending is None:
                        self._pending = event
                    else:
                        self._pending['text'] += event['text']
                    if len(self._pending['text']) >= _PARTIAL_LIMIT:
                        self._flush()
                    continue
                self._flush()
                self._write(event)

    def close(self):
        with self._lock:
            if self._handle is None:
                return
            self._flush()
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def _flush(self):
        pending, self._pending = self._pending, None
        if pending is not None and pending['text'].strip():
            self._write(pending)

    def _write(self, event):
        record = {'ts': self._clock(), **event, 'text': redact_text(event['text'], self.private_terms)}
        try:
            self._handle.write(json.dumps(record, ensure_ascii=False) + '\n')
            self._handle.flush()
        except (OSError, ValueError):
            try:
                self._handle.close()
            except (OSError, ValueError):
                pass
            self._handle = None


def read_tail(path, count=3, private_terms=(), tail_bytes=8192):
    """The last `count` events of an events file, re-redacted, as `"<kind>: <text>"` strings."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
            lines = handle.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return []
    if size > tail_bytes and lines:
        lines = lines[1:]
    shown = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('kind') in KINDS:
            shown.append(f'{event["kind"]}: {redact_text(event.get("text", ""), private_terms)}')
    return shown[-max(1, int(count)):]
