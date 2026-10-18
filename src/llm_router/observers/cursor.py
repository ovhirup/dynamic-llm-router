"""Cursor CLI (cursor-agent) monthly usage observer. EXPERIMENTAL, opt-in.

Runs `cursor-agent --mode ask` in a pty inside an empty scratch workspace under the state dir, types
/usage (plan meters and reset date), and parses strictly. No prompt is sent and no credential file is
read. Hard deadlines everywhere: a hang (for example a self-update on launch) yields 'unknown'.
"""
from datetime import datetime, timedelta, timezone
import re

from .. import config
from . import common

MONTHS = 'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()
BAR = r'[\u2588\u2591\u2592\u2593 ]{0,300}'
SCREEN = re.compile(
    r'Usage \u2022 (?P<plan>[A-Za-z+]{1,12}) Resets (?P<month>' + '|'.join(MONTHS) + r') (?P<day>\d{1,2}) '
    r'Monthly plan and on-demand usage Category Current Usage '
    r'Included (?P<included>\d{1,3})% used ' + BAR +
    r'Auto (?P<auto>\d{1,3})% used ' + BAR +
    r'API (?P<api>\d{1,3})% used')
STEPS = [(b'/usage', 3), (b'\r', 12)]
WARMUP_SECONDS = 15
VERSION_TIMEOUT = 15


def cli_version(policy, runner=common.run_capture):
    code, out = runner([config.binary(policy, 'cursor'), '--version'], VERSION_TIMEOUT)
    match = re.fullmatch(r'\s*(\d{4}\.\d{2}\.\d{2}-[0-9a-f]{6,12})\s*', out or '') if code == 0 else None
    return match.group(1) if match else None


def scratch_dir():
    path = config.state_dir() / 'state' / 'cursor-probe'
    path.mkdir(parents=True, exist_ok=True)
    return path


def capture_screen(policy, timeout_seconds):
    cursor, scratch = config.binary(policy, 'cursor'), scratch_dir()
    return common.capture_pty([cursor, '--mode', 'ask', '--workspace', str(scratch), '--trust'],
                              timeout_seconds, STEPS, WARMUP_SECONDS, cwd=str(scratch))


def parse_screen(raw, observed_at, freshness_seconds, zone=None):
    """Strict parse; percentages are USED. The reset has no time, so use 00:00 local (earliest)."""
    try:
        if observed_at.tzinfo is None or not common.valid_timeout(freshness_seconds):
            return None
        text = common.screen_text(raw)
        matches = list(SCREEN.finditer(text))
        if len(matches) != 1:
            return None
        match = matches[0]
        used = {name: int(match[name]) for name in ('included', 'auto', 'api')}
        if any(not 0 <= value <= 100 for value in used.values()):
            return None
        local = observed_at.astimezone(zone or observed_at.tzinfo)
        resets = []
        for year in (local.year, local.year + 1):
            try:
                reset = datetime(year, MONTHS.index(match['month']) + 1, int(match['day']), tzinfo=local.tzinfo)
            except ValueError:
                continue
            if observed_at < reset <= observed_at + timedelta(days=32):
                resets.append(reset)
        if len(resets) != 1:
            return None
        reset = resets[0]
        stamp = {'observed_at': common.iso(observed_at),
                 'expires_at': common.iso(min(observed_at + timedelta(seconds=freshness_seconds), reset)),
                 'reset_at': common.iso(reset)}
        return {'source_worker': 'cursor', 'required_windows': ['included', 'auto', 'api'],
                'windows': {name: {'percent_used': value, **stamp} for name, value in used.items()}}
    except Exception:
        return None


def observe(policy, now=None, capture=None, version=None, worker='cursor'):
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
        warning = f'cursor-agent version changed: expected {expected}, got {v}'
    started = datetime.now(timezone.utc) if now is None else now
    try:
        raw = (capture or (lambda seconds: capture_screen(policy, seconds)))(opts.get('capture_timeout_seconds', 40))
    except Exception as error:
        return common.unknown('capture_failed:' + type(error).__name__, cli_version=v, version_warning=warning)
    evidence = parse_screen(raw, started, opts.get('freshness_seconds', 3960), config.tz(policy))
    if evidence is None:
        return common.unknown('parse_failed', cli_version=v, version_warning=warning)
    return {'evidence': evidence, 'status': 'ok', 'reason': None, 'cli_version': v, 'version_warning': warning}
