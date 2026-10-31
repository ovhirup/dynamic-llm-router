"""Worker cooldowns: a quota/limit/429 failure parks the worker until its meter resets.

The cooldown ends at the worker's meter reset_at (earliest reset of an exhausted window, else the
most-used window's reset) when that is in the future, otherwise FALLBACK after the failure.
State lives in <state_dir>/state/cooldowns.json as {worker: {"until", "set_at", "reason"}}.
"""
from datetime import datetime, timedelta, timezone
import re

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


_MONTHS = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
_RESETS = re.compile(r'(?i)(?:resets?|try again)\s+(?:at|on|after)?\s*'
                     r'(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})'
                     r'(?:st|nd|rd|th)?,?\s*(?:(?P<hour>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?)?')
_RETRY_AFTER = re.compile(r'(?i)(?:retry[- ]after|try again in)[:\s]+(?P<n>\d+)\s*(?P<unit>s|sec|seconds?|m|min|minutes?|h|hours?)?\b')


def reset_from_text(text, now, zone=None):
    """A future reset time named in a worker's error text (e.g. 'resets Nov 4, 12:17 PM',
    'Retry-After: 120'), interpreted in `zone` (default local); None when none is found."""
    if not text:
        return None
    match = _RETRY_AFTER.search(text)
    if match:
        unit = (match['unit'] or 's').lower()[0]
        seconds = int(match['n']) * {'s': 1, 'm': 60, 'h': 3600}[unit]
        return now + timedelta(seconds=seconds) if seconds > 0 else None
    match = _RESETS.search(text)
    if not match:
        return None
    zone = zone or datetime.now().astimezone().tzinfo
    hour, minute = int(match['hour'] or 0), int(match['min'] or 0)
    ampm = (match['ampm'] or '').lower().replace('.', '')
    if ampm == 'pm' and hour < 12:
        hour += 12
    elif ampm == 'am' and hour == 12:
        hour = 0
    local_now = now.astimezone(zone)
    try:
        when = datetime(local_now.year, _MONTHS[match['mon'].lower()[:3]], int(match['day']),
                        hour, minute, tzinfo=zone)
        if when <= now - timedelta(days=1):
            when = when.replace(year=when.year + 1)
    except ValueError:
        return None
    return when if when > now else None


def record(worker, now=None, observation=None, reason='quota_error', text=None, zone=None):
    """Cool `worker` until the reset named in its error `text`, else its meter reset (from
    `observation`, else its cached meter), else 1h."""
    now = now or datetime.now(timezone.utc)
    if observation is None:
        observation = common.load_cache(quota_service.cache_path(worker))
    until = reset_from_text(text, now, zone) or reset_at(observation, now) or now + FALLBACK
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
