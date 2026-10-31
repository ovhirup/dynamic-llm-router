"""Worker cooldowns: a quota/limit/429 failure parks the worker until its meter resets.

The cooldown ends at the worker's meter reset_at (earliest reset of an exhausted window, else the
most-used window's reset) when that is in the future, otherwise FALLBACK after the failure.
State lives in <state_dir>/state/cooldowns.json as {worker: {"until", "set_at", "reason"}}.
"""
from datetime import datetime, timedelta, timezone

from . import config, quota_service
from .observers import common

FALLBACK = timedelta(hours=1)


def path():
    return config.state_dir() / 'state' / 'cooldowns.json'


def load():
    data = common.load_cache(path())
    return data if isinstance(data, dict) else {}


def reset_at(observation, now):
    """The meter's next reset after `now`, or None when unknown."""
    evidence = quota_service.evidence_of(observation)
    until = quota_service.exhausted_until(evidence, now)
    if until is None:
        _, _, value = quota_service.peak(evidence)
        until = quota_service._parse(value) if value else None
    return until if until is not None and until > now else None


def record(worker, now=None, observation=None, reason='quota_error'):
    """Cool `worker` until its meter reset (from `observation`, else its cached meter), else 1h."""
    now = now or datetime.now(timezone.utc)
    if observation is None:
        observation = common.load_cache(quota_service.cache_path(worker))
    until = reset_at(observation, now) or now + FALLBACK
    state = {name: entry for name, entry in load().items() if active_until(entry, now)}
    state[worker] = {'until': until.isoformat(), 'set_at': now.isoformat(), 'reason': reason}
    common.save_cache(state, path())
    return until


def active_until(entry, now):
    until = quota_service._parse(entry.get('until')) if isinstance(entry, dict) else None
    return until if until is not None and until > now else None


def active(now=None, state=None):
    """{worker: until} for every cooldown still in force at `now`."""
    now = now or datetime.now(timezone.utc)
    state = load() if state is None else state
    return {name: until for name, entry in state.items() if (until := active_until(entry, now))}
