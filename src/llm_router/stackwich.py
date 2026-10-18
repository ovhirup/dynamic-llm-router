"""Optional worker-neutral, in-session verification evidence contract."""
import hashlib
import json
import os
from pathlib import Path
import stat

REPORT_PATH = '.router-stackwich/report.json'
MAX_REPORT_BYTES = 1_000_000
SCHEMA = {'plan': {'files': ['out.txt'], 'steps': ['Create out.txt'],
                  'verification': [{'command': 'test -f out.txt', 'expected': 'exit 0'}]},
          'execute': {'changed_files': ['out.txt']},
          'verify': {'commands_run': [{'command': 'test -f out.txt', 'exit_code': 0}],
                     'scope_check': 'PASS', 'gate': 'PASS'},
          'review': {'verdict': 'APPROVE', 'items': []}, 'cycles': 1}


def status(policy, worker):
    options = policy['run_task'].get('stackwich', {}).get(worker, {})
    if not options.get('enabled', False):
        return 'disabled'
    return 'required' if worker != 'developer' and options.get('supported', False) else 'unsupported'


def prompt(cycle):
    return ('## Stackwich verification required\nRun plan → execute → verify → review in this session. '
            'Do not commit, push, or mutate Git metadata. Write ' + REPORT_PATH +
            f' with cycles equal to {cycle}. Plan exact product paths before editing; report every change. '
            'Run concrete verification commands and record actual exit codes; review all changes and resolve '
            'every review item. Use this exact JSON structure (replace example values):\n' +
            json.dumps(dict(SCHEMA, cycles=cycle)))


def _safe_path(cwd):
    path = Path(cwd) / REPORT_PATH
    parent = path.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise ValueError('report parent must be a directory, never a symlink')
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('report must be a regular file, never a symlink')
    return path


def prepare(cwd, tracked=False):
    if tracked:
        raise ValueError('report path is already tracked')
    path = _safe_path(cwd)
    path.unlink(missing_ok=True)


def load(cwd):
    """No-follow, bounded read; callers must first complete their leak scan."""
    path = _safe_path(cwd)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('report must be a regular file')
        raw = handle.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError('report is too large')
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def canonical(path):
    return (isinstance(path, str) and bool(path) and not path.startswith('/') and
            '\\' not in path and '\x00' not in path and
            all(part not in ('', '.', '..') and part.lower() != '.git' for part in path.split('/')) and
            path != REPORT_PATH)


def validate(report, actual, cycle):
    errors = []
    def strings(value):
        return isinstance(value, list) and bool(value) and all(isinstance(x, str) and x.strip() for x in value)
    def paths(value):
        return strings(value) and all(canonical(x) for x in value) and len(set(value)) == len(value)
    if not isinstance(report, dict):
        return ['report must be an object']
    if type(report.get('cycles')) is not int or report['cycles'] not in (1, 2) or report['cycles'] != cycle:
        errors.append('cycles must equal the current attempt (1 or 2)')
    plan, execute, verify, review = [report.get(k) for k in ('plan', 'execute', 'verify', 'review')]
    if not all(isinstance(x, dict) for x in (plan, execute, verify, review)):
        return errors + ['plan, execute, verify and review must be objects']
    if not paths(plan.get('files')) or not paths(execute.get('changed_files')):
        errors.append('plan and execute require unique canonical product paths')
    elif set(plan['files']) != set(execute['changed_files']) or set(plan['files']) != set(actual):
        errors.append('actual product changes, execute.changed_files and plan.files must match')
    if not strings(plan.get('steps')):
        errors.append('plan requires nonempty steps')
    planned = plan.get('verification')
    if not isinstance(planned, list) or not planned or not all(isinstance(x, dict) and
            all(isinstance(x.get(k), str) and x[k].strip() for k in ('command', 'expected')) for x in planned):
        errors.append('plan requires concrete verification commands and expected results')
        planned = []
    ran = verify.get('commands_run')
    if not isinstance(ran, list) or not ran or not all(isinstance(x, dict) and
            isinstance(x.get('command'), str) and x['command'].strip() and
            type(x.get('exit_code')) is int and x['exit_code'] == 0 for x in ran):
        errors.append('verification commands must all succeed with integer exit code 0')
        ran = []
    if any(x['command'] not in {r['command'] for r in ran} for x in planned):
        errors.append('missing planned verification command evidence')
    if verify.get('gate') != 'PASS' or verify.get('scope_check') != 'PASS':
        errors.append('gate and scope_check must be PASS')
    if review.get('verdict') != 'APPROVE' or review.get('items') != []:
        errors.append('review must APPROVE with no unresolved items')
    return errors


def evidence(cwd, actual, cycle):
    from . import security
    result = {'status': 'blocked', 'report_path': REPORT_PATH, 'cycle': cycle,
              'gate': None, 'review': None, 'scope_check': None, 'errors': [], 'report': None}
    try:
        report, digest = load(cwd)
        result['errors'] = validate(report, actual, cycle)
        result['digest'] = digest
        result['report'] = security.redact_obj(report)
        if isinstance(report, dict):
            verify, review = report.get('verify'), report.get('review')
            if isinstance(verify, dict):
                result.update(gate=verify.get('gate'), scope_check=verify.get('scope_check'))
            if isinstance(review, dict):
                result['review'] = review.get('verdict')
    except (OSError, ValueError, RecursionError):
        result['errors'] = ['report missing, unsafe, oversized, or malformed']
    if not result['errors']:
        result['status'] = 'verified'
    return security.redact_obj(result)


def fingerprints(cwd, paths):
    result = {}
    for name in paths:
        path = Path(cwd) / name
        if path.is_symlink():
            result[name] = ('symlink', os.readlink(path))
        elif path.is_file():
            result[name] = ('file', path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            result[name] = ('absent',)
    return result
