"""Grok Build (grok CLI) weekly usage observer. EXPERIMENTAL, opt-in.

How it works: there is no official usage API for the Grok CLI today, so this starts the interactive
`grok --no-auto-update dashboard` TUI in a pseudo-terminal, types `/usage`, captures the screen and
parses exactly one line of the form

    Weekly limit (<plan>) ████░░░░ 66% Resets: October 20, 13:51

The percentage is USED. The reset time is read in the configured timezone (default: the system's).
No prompt is sent, no model is called, no credential file is read.

Robustness rules: hard capture deadline; strict parse (exactly one match, 0-100%, a reset within the
next 8 days); anything else returns status 'unknown' with a reason, never a guessed number.
"""
from datetime import datetime, timedelta, timezone
import re

from .. import config
from . import common

MONTHS = 'January February March April May June July August September October November December'.split()
# The bar may be split by a colour escape (rendered as a space) when run without a login shell env.
WEEKLY = re.compile(r'Weekly limit \((?P<plan>[^)]{1,40})\) [█░][█░ ]{8,80}[█░] '
                    r'(?P<pct>\d{1,3})% Resets: (?P<month>' + '|'.join(MONTHS) +
                    r') (?P<day>\d{1,2}), (?P<hh>\d{2}):(?P<mm>\d{2})')
STEPS = [(b'/usage', 1.5), (b'\r', 3), (b'\r', 8)]
WARMUP_SECONDS = 6


def cli_version(policy, runner=common.run_capture):
    code, out = runner([config.binary(policy, 'grok'), '--no-auto-update', 'version'], 10)
    match = re.search(r'\d+\.\d+\.\d+', out or '') if code == 0 else None
    return match.group() if match else None


def capture_screen(policy, timeout_seconds):
    grok = config.binary(policy, 'grok')
    return common.capture_pty([grok, '--no-auto-update', 'dashboard'], timeout_seconds, STEPS, WARMUP_SECONDS)


def parse_screen(raw, observed_at, freshness_seconds, zone=None):
    """Strict parse -> evidence dict, or None when anything is off."""
    try:
        if observed_at.tzinfo is None or not common.valid_timeout(freshness_seconds):
            return None
        text = common.screen_text(raw)
        matches = list(WEEKLY.finditer(text))
        if 'Usage limit' not in text or len(matches) != 1:
            return None
        match = matches[0]
        pct = int(match['pct'])
        if not 0 <= pct <= 100:
            return None
        local = observed_at.astimezone(zone or observed_at.tzinfo)
        resets = []
        for year in (local.year - 1, local.year, local.year + 1):
            try:
                reset = datetime(year, MONTHS.index(match['month']) + 1, int(match['day']),
                                 int(match['hh']), int(match['mm']), tzinfo=local.tzinfo)
            except ValueError:
                continue
            if observed_at < reset <= observed_at + timedelta(days=8):
                resets.append(reset)
        if len(resets) != 1:
            return None
        reset = resets[0]
        return {'source_worker': 'grok-build', 'required_windows': ['weekly'], 'windows': {
            'weekly': {'percent_used': pct, 'observed_at': common.iso(observed_at),
                       'expires_at': common.iso(min(observed_at + timedelta(seconds=freshness_seconds), reset)),
                       'reset_at': common.iso(reset)}}}
    except Exception:
        return None


def observe(policy, now=None, capture=None, version=None, worker='grok-build'):
    opts = config.quota_cfg(policy, worker) or {}
    try:
        v = (version or (lambda: cli_version(policy)))()
    except Exception:
        v = None
    if v is None:
        return common.unknown('cli_missing_or_no_version')
    warning = None
    expected = opts.get('expected_cli_version')
    if expected and v != expected:
        warning = f'grok CLI version changed: expected {expected}, got {v}'
    started = datetime.now(timezone.utc) if now is None else now
    try:
        raw = (capture or (lambda seconds: capture_screen(policy, seconds)))(opts.get('capture_timeout_seconds', 25))
    except Exception as error:
        return common.unknown('capture_failed:' + type(error).__name__, cli_version=v, version_warning=warning)
    evidence = parse_screen(raw, started, opts.get('freshness_seconds', 1260), config.tz(policy))
    if evidence is None:
        return common.unknown('parse_failed', cli_version=v, version_warning=warning)
    return {'evidence': evidence, 'status': 'ok', 'reason': None, 'cli_version': v, 'version_warning': warning}
