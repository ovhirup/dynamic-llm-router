"""EXPERIMENTAL Claude /usage screen reader. Layout is unverified; unknown on any doubt."""
from datetime import datetime, timedelta, timezone
import re

from .. import config
from . import common

STEPS = [(b'/usage', 1.5), (b'\r', 8)]
WARMUP_SECONDS = 6


def capture_screen(policy, timeout_seconds):
    return common.capture_pty([config.binary(policy, 'claude')], timeout_seconds,
                              STEPS, WARMUP_SECONDS)


def parse_screen(raw, observed_at, freshness_seconds, zone=None):
    try:
        if observed_at.tzinfo is None or not common.valid_timeout(freshness_seconds):
            return None
        # Retain line boundaries: a percentage must belong to the named usage row.
        text = common.ANSI.sub(b' ', raw).decode('utf-8', 'strict')
        windows = {}
        for window, label in [('session', 'Current session'), ('weekly', 'Current week (all models)')]:
            matches = re.findall(re.escape(label) + r'[^\r\n]*?\b(\d{1,3})% used', text)
            if len(matches) != 1 or not 0 <= int(matches[0]) <= 100:
                return None
            windows[window] = {'percent_used': int(matches[0]), 'observed_at': common.iso(observed_at),
                               'expires_at': common.iso(observed_at + timedelta(seconds=freshness_seconds))}
        return {'source_worker': 'claude', 'required_windows': ['session', 'weekly'], 'windows': windows}
    except Exception:
        return None


def observe(policy, now=None, capture=None, worker='claude'):
    opts = config.quota_cfg(policy, worker) or {}
    observed = now or datetime.now(timezone.utc)
    try:
        raw = (capture or (lambda seconds: capture_screen(policy, seconds)))(opts.get('capture_timeout_seconds', 25))
    except Exception as error:
        return common.unknown('capture_failed:' + type(error).__name__)
    evidence = parse_screen(raw, observed, opts.get('freshness_seconds', 1800))
    if evidence is None:
        return common.unknown('parse_failed')
    return {'evidence': evidence, 'status': 'ok', 'reason': None,
            'cli_version': None, 'version_warning': None}
