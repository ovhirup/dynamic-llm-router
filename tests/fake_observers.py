"""Fake observers for isolation tests (imported in a child process by observe_isolated)."""
import time

EVIDENCE = {'source_worker': 'codex', 'required_windows': ['weekly'], 'windows': {'weekly': {
    'percent_used': 12, 'observed_at': '2026-10-17T06:35:00Z', 'expires_at': '2026-10-17T06:46:00Z',
    'reset_at': '2026-10-20T08:21:00Z'}}}


def ok(policy, worker=None):
    return {'evidence': EVIDENCE, 'cli_version': '1.2.3', 'version_warning': None}


def hang(policy, worker=None):
    time.sleep(60)


def crash(policy, worker=None):
    raise RuntimeError('boom /secret/path')


def garbage(policy, worker=None):
    return 'not a dict'
