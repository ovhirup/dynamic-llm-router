"""Claude token meter via Claude Code mod hook (#102): enables the 85% handoff trigger for Claude.

Persists input, output, cache-read, and cache-creation token counts per turn plus a running total in
`<state>/state/claude_token_meter.json` (`0600`) so hot reloads never lose in-memory counters.
When the file is missing, `read_claude_token_meter` returns `{'status': 'unmetered', 'percent_used': None}`
so Claude gracefully falls back to unmetered behavior without modifying live config.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path

from . import config

DEFAULT_BUDGET_CAP = 200_000


def meter_path(policy=None):
    return config.policy_path(policy or {}, 'quota', 'claude_token_meter_file', 'state/claude_token_meter.json')


def budget_tokens(policy=None):
    claude_cfg = ((policy or {}).get('quota') or {}).get('claude') or {}
    budget = claude_cfg.get('budget_tokens', DEFAULT_BUDGET_CAP)
    return max(1, int(budget)) if type(budget) in (int, float) and budget > 0 else DEFAULT_BUDGET_CAP


def record_turn_usage(usage, path=None, policy=None, budget=None, now=None):
    """Record one turn's token usage (`input_tokens`, `output_tokens`, `cache_read_input_tokens`,
    `cache_creation_input_tokens`) into the persistent JSON file on disk (survives hot reloads).
    """
    if not isinstance(usage, dict):
        raise ValueError('usage must be a dict')
    target = Path(path) if path is not None else meter_path(policy)
    target.parent.mkdir(parents=True, exist_ok=True)
    inp = max(0, int(usage.get('input_tokens', 0) or 0))
    out = max(0, int(usage.get('output_tokens', 0) or 0))
    cache_read = max(0, int(usage.get('cache_read_input_tokens', 0) or 0))
    cache_create = max(0, int(usage.get('cache_creation_input_tokens', 0) or 0))
    turn_total = inp + out + cache_read + cache_create
    cap = max(1, int(budget if budget is not None else budget_tokens(policy)))
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')

    fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        raw = handle.read()
        try:
            state = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            state = {}
        if not isinstance(state, dict):
            state = {}
        turns = list(state.get('turns') or [])
        turns.append({
            'at': stamp,
            'input_tokens': inp,
            'output_tokens': out,
            'cache_read_input_tokens': cache_read,
            'cache_creation_input_tokens': cache_create,
            'total_tokens': turn_total,
        })
        running_input = int(state.get('input_tokens', 0) or 0) + inp
        running_output = int(state.get('output_tokens', 0) or 0) + out
        running_cache_read = int(state.get('cache_read_input_tokens', 0) or 0) + cache_read
        running_cache_create = int(state.get('cache_creation_input_tokens', 0) or 0) + cache_create
        running_total = running_input + running_output + running_cache_read + running_cache_create
        percent = min(100.0, round((running_total / cap) * 100.0, 2))
        updated = {
            'worker': 'claude',
            'status': 'ok',
            'updated_at': stamp,
            'budget_tokens': cap,
            'input_tokens': running_input,
            'output_tokens': running_output,
            'cache_read_input_tokens': running_cache_read,
            'cache_creation_input_tokens': running_cache_create,
            'total_tokens': running_total,
            'percent_used': percent,
            'turns': turns,
        }
        handle.seek(0)
        handle.truncate()
        json.dump(updated, handle, indent=2)
        handle.write('\n')
    os.chmod(target, 0o600)
    return updated


def read_claude_token_meter(policy=None, path=None):
    """Read the persisted Claude token meter; falls back to `status: 'unmetered'` when missing."""
    target = Path(path) if path is not None else meter_path(policy)
    if not target.is_file():
        return {'worker': 'claude', 'status': 'unmetered', 'percent_used': None, 'total_tokens': 0}
    try:
        data = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return {'worker': 'claude', 'status': 'unmetered', 'percent_used': None, 'total_tokens': 0}
    if not isinstance(data, dict) or type(data.get('percent_used')) not in (int, float):
        return {'worker': 'claude', 'status': 'unmetered', 'percent_used': None, 'total_tokens': 0}
    return data
