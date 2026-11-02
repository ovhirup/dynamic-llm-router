"""Read-only app usage meters via accessibility-tree JSON snapshots (`agent-desktop`) (#95).

Extracts usage percentages and reset times from `agent-desktop` accessibility-tree JSON snapshots
for macOS desktop apps (`cursor`, `codex`, `grok-build`, `grok-bot`) into `meters.MeterRecord`:
  - Strictly read-only: only `snapshot` commands are permitted; `click`, `type`, `press`, `key`,
    and `set` actions are refused by code guard.
  - Any parse doubt (no match, multiple conflicting matches, out-of-range percentage, malformed JSON)
    returns `MeterRecord(..., percent_used=None, status='unknown')` so the worker is skipped rather
    than guessed.
  - Snapshots stay in memory only and are never logged or uploaded.
"""
from datetime import datetime, timezone
import json
import re

from .. import meters

FORBIDDEN_ACTIONS = {'click', 'double-click', 'right-click', 'type', 'press', 'key', 'set-value', 'focus', 'drag'}

APP_BUNDLES = {
    'cursor': 'com.todesktop.230313mzl4w4u92',
    'codex': 'com.openai.codex',
    'grok-build': 'ai.x.grok',
    'grok-bot': 'ai.x.grokbot',
}

APP_WINDOWS = {
    'cursor': 'monthly',
    'codex': 'weekly',
    'grok-build': 'weekly',
    'grok-bot': 'weekly',
}

_PERCENT_USED_RE = re.compile(r'(?i)\b(\d{1,3}(?:\.\d+)?)\s*%\s*(?:used|of\s+plan|of\s+limit|monthly|weekly)\b')
_FRACTION_RE = re.compile(r'(?i)\b(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*(?:requests|fast\s+requests|credits|messages)\b')
_RESET_ISO_RE = re.compile(r'(?i)\bresets?\s*(?:at|on|:)?\s*(\d{4}-\d{2}-\d{2}[T ][0-9:+.\-Z]+)')


def build_snapshot_command(worker, executable='agent-desktop'):
    """Build a strictly read-only `agent-desktop snapshot` argv for `worker`."""
    if worker not in APP_BUNDLES:
        raise ValueError(f'unsupported desktop app worker {worker!r}')
    argv = [str(executable), 'snapshot', '--app', APP_BUNDLES[worker], '--json']
    if any(arg.lower() in FORBIDDEN_ACTIONS for arg in argv):
        raise ValueError('accessibility meter reader is strictly read-only')
    return argv


def _collect_strings(node, out):
    if isinstance(node, dict):
        for key in ('name', 'label', 'title', 'value', 'description', 'text', 'help'):
            val = node.get(key)
            if isinstance(val, str) and val.strip():
                out.append(val.strip())
        for child in node.get('children') or ():
            _collect_strings(child, out)
    elif isinstance(node, list):
        for item in node:
            _collect_strings(item, out)


def _unknown(worker, now):
    window = APP_WINDOWS.get(worker, 'weekly')
    return meters.MeterRecord(
        worker=worker,
        window=window,
        percent_used=None,
        reset_at=None,
        read_at=now,
        source='accessibility',
        status='unknown',
    )


def parse_ax_snapshot(worker, snapshot_json, now=None):
    """Parse an `agent-desktop` accessibility tree JSON snapshot into a `meters.MeterRecord`.

    Returns `status='unknown'` on any doubt (missing, out-of-range, or conflicting readings).
    """
    now = now or datetime.now(timezone.utc)
    if worker not in APP_WINDOWS:
        return _unknown(worker, now)
    if isinstance(snapshot_json, (str, bytes)):
        try:
            tree = json.loads(snapshot_json)
        except (ValueError, TypeError):
            return _unknown(worker, now)
    elif isinstance(snapshot_json, dict):
        tree = snapshot_json
    else:
        return _unknown(worker, now)

    texts = []
    _collect_strings(tree, texts)
    if not texts:
        return _unknown(worker, now)

    percents = []
    resets = []
    for text in texts:
        for match in _PERCENT_USED_RE.finditer(text):
            val = float(match.group(1))
            if not 0.0 <= val <= 100.0:
                return _unknown(worker, now)
            percents.append(round(val, 2))
        for match in _FRACTION_RE.finditer(text):
            used, total = float(match.group(1)), float(match.group(2))
            if total <= 0 or used > total or used < 0:
                return _unknown(worker, now)
            percents.append(round((used / total) * 100.0, 2))
        for match in _RESET_ISO_RE.finditer(text):
            parsed = meters._parse(match.group(1).strip())
            if parsed is not None:
                resets.append(parsed)

    unique_percents = sorted(set(percents))
    if len(unique_percents) != 1:
        return _unknown(worker, now)
    reset_at = resets[0] if len(set(resets)) == 1 else None
    return meters.MeterRecord(
        worker=worker,
        window=APP_WINDOWS[worker],
        percent_used=unique_percents[0],
        reset_at=reset_at,
        read_at=now,
        source='accessibility',
        status='ok',
    )
