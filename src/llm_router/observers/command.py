"""Quota from an explicitly configured command; strict, bounded and unknown on doubt."""
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path

from .. import config, security
from . import common


def observe(policy, now=None, runner=common.run_capture, worker=None):
    opts = config.quota_cfg(policy, worker) or {}
    try:
        if Path(opts['command'][0]).name in security.shim_names(policy.get('never') or []):
            return common.unknown('never')
        code, text = runner(opts['command'], opts.get('timeout_seconds', 20))
        if code is None:
            return common.unknown('timeout')
        if code != 0:
            return common.unknown('command_failed')
        matches = list(re.finditer(opts['regex'], text))
        if len(matches) != 1:
            return common.unknown('no_match' if not matches else 'multiple_matches')
        percent = float(matches[0]['percent'])
        if not 0 <= percent <= 100:
            return common.unknown('out_of_range')
        if opts.get('percent_is', 'used') == 'remaining':
            percent = 100 - percent
        observed = now or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            return common.unknown('invalid_timestamp')
        expires = observed + timedelta(seconds=opts.get('freshness_seconds', 1800))
        row = {'percent_used': percent, 'observed_at': common.iso(observed)}
        reset = matches[0].groupdict().get('reset')
        if reset:
            reset = datetime.fromisoformat(reset.replace('Z', '+00:00'))
            if reset.tzinfo is None or reset <= observed:
                return common.unknown('invalid_reset')
            row['reset_at'] = common.iso(reset)
            expires = min(expires, reset)
        row['expires_at'] = common.iso(expires)
        window = opts.get('window', 'monthly')
        return {'evidence': {'source_worker': worker, 'required_windows': [window],
                             'windows': {window: row}}, 'status': 'ok', 'reason': None,
                'cli_version': None, 'version_warning': None}
    except Exception:
        return common.unknown('parse_failed')
