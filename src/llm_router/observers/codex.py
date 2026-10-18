"""Codex quota observer (official interface): `codex app-server` JSON-RPC `account/rateLimits/read`.

Read-only: no prompt, no auth file access, never triggers paid top-ups. Retains only selector evidence
(percent used, observed/expiry/reset times for the 5-hour and weekly windows).
"""
from datetime import datetime, timedelta, timezone
import json
import math
import queue
import subprocess
import threading
import time

from .. import config
from . import common

_iso = common.iso


def read_evidence(policy, now=None, runner=None, worker='codex'):
    """Return minimized evidence or None; runner is a Popen-compatible factory."""
    child = None
    try:
        started = datetime.now(timezone.utc) if now is None else now
        if not isinstance(started, datetime) or started.tzinfo is None:
            return None
        options = config.quota_cfg(policy, worker)
        freshness, timeout = options['freshness_seconds'], options['app_server_timeout_seconds']
        if any(type(x) not in (int, float) or not math.isfinite(x) or x <= 0
               for x in (freshness, timeout)):
            return None
        deadline = time.monotonic() + timeout
        child = (runner or subprocess.Popen)(
            [config.binary(policy, 'codex'), 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        messages = queue.Queue()

        def read_lines():
            try:
                for line in child.stdout:
                    try:
                        messages.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                messages.put(None)

        threading.Thread(target=read_lines, daemon=True).start()

        def send(message):
            child.stdin.write(json.dumps({'jsonrpc': '2.0', **message}) + '\n')
            child.stdin.flush()

        def receive(identifier):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                message = messages.get(timeout=remaining)
                if message is None:
                    return None
                if isinstance(message, dict) and message.get('id') == identifier:
                    return message.get('result') if 'error' not in message else None

        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'llm-router', 'version': '1'}}})
        if receive(1) is None:
            return None
        send({'method': 'initialized'})
        send({'id': 2, 'method': 'account/rateLimits/read', 'params': None})
        result = receive(2)
        if not isinstance(result, dict) or result.get('ordinaryUsageAllowed') is not True:
            return None
        snapshot = (result['rateLimitsByLimitId']['codex'] if 'rateLimitsByLimitId' in result
                    else result['rateLimits'])
        if snapshot.get('rateLimitReachedType') is not None or snapshot.get('spendControlReached') is True:
            return None
        windows = {}
        for source, name, duration in [('primary', 'primary', 300), ('secondary', 'weekly', 10080)]:
            window = snapshot[source]
            used, reset = window['usedPercent'], window['resetsAt']
            if (type(window['windowDurationMins']) is not int or window['windowDurationMins'] != duration
                    or type(used) is not int or not 0 <= used <= 100
                    or type(reset) is not int or reset <= started.timestamp()):
                return None
            reset_at = datetime.fromtimestamp(reset, timezone.utc)
            windows[name] = {'percent_used': used, 'observed_at': _iso(started),
                             'expires_at': _iso(min(started + timedelta(seconds=freshness), reset_at)),
                             'reset_at': _iso(reset_at)}
        return {'source_worker': 'codex', 'required_windows': ['primary', 'weekly'], 'windows': windows}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, queue.Empty):
        return None
    finally:
        if child is not None:
            try:
                child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    common._kill_group(child.pid, 9)
                    try:
                        child.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                for stream in (child.stdin, child.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass


def cli_version(policy, runner=common.run_capture):
    code, out = runner([config.binary(policy, 'codex'), '--version'], 15)
    parts = (out or '').strip().split()
    return parts[-1] if code == 0 and parts else None


def observe(policy, now=None, runner=None, version=None, worker='codex'):
    """Normalised observation: {'evidence', 'status', 'reason', 'cli_version', 'version_warning'}."""
    evidence = read_evidence(policy, now=now, runner=runner, worker=worker)
    try:
        v = (version or (lambda: cli_version(policy)))()
    except Exception:
        v = None
    if evidence is None:
        return common.unknown('app_server_unavailable_or_limit_reached', cli_version=v)
    return {'evidence': evidence, 'status': 'ok', 'reason': None, 'cli_version': v, 'version_warning': None}
