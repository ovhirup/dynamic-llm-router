"""Job phase spans. JSONL is the default export. OTLP runs only when the policy asks for it."""

from datetime import datetime, timezone

from . import NativeOrchestrator
from .. import security, traces

_SIDE_EFFECT = {
    'plan': 'read_only',
    'dispatch': 'mutating',
    'test': 'read_only',
    'judge': 'read_only',
    'fix': 'mutating',
    'approval_wait': 'read_only',
}
_COST_PER_MILLION_TOKENS = {
    'claude': 3.0,
    'codex': 1.5,
    'cursor': 1.5,
    'grok-build': 2.0,
    'grok-bot': 2.0,
}
_OK_EXIT = {'ok', 0, 'passed', 'approve', 'approved', 'pending', 'not_required'}


def estimate_cost(worker, tokens):
    """Rough USD estimate from a per-worker rate. Missing token counts have no estimate."""
    if type(tokens) not in (int, float) or type(tokens) is bool:
        return None
    rate = _COST_PER_MILLION_TOKENS.get(str(worker or ''), 2.0)
    return round(float(tokens) / 1_000_000 * rate, 6)


def _count(value):
    if type(value) in (int, float) and type(value) is not bool:
        return int(value)
    return None


def _tokens_in(value):
    if not isinstance(value, dict):
        return None
    found = _count(value.get('tokens'))
    if found is not None:
        return found
    total, seen = 0, False
    for attempt in value.get('attempts') or []:
        if not isinstance(attempt, dict):
            continue
        for run in attempt.get('runs') or []:
            if not isinstance(run, dict):
                continue
            count = _count(run.get('tokens'))
            if count is not None:
                total += count
                seen = True
    return total if seen else None


def _worker(summary, result):
    if isinstance(result, dict):
        if result.get('worker'):
            return result.get('worker')
        workers = result.get('workers') or []
        if workers:
            return workers[-1]
    workers = (summary or {}).get('workers') or []
    return workers[-1] if workers else None


def _model(policy, worker, result):
    if isinstance(result, dict) and result.get('model'):
        return result.get('model')
    local = (((policy or {}).get('run_task') or {}).get('local_models') or {}).get(worker) or {}
    if isinstance(local, dict) and local.get('model'):
        return local['model']
    return worker


def _exit_status(result, error):
    if error is not None:
        return 'error'
    if not isinstance(result, dict):
        return 'ok'
    code = result.get('returncode')
    if type(code) in (int, float) and type(code) is not bool:
        return int(code)
    if result.get('verdict'):
        return str(result['verdict'])
    if result.get('status'):
        return str(result['status'])
    if result.get('ok') is False:
        return 'failed'
    return 'ok'


def phase_attributes(policy, summary, result, error=None):
    """worker, model, tokens, cost estimate, and exit status for one phase."""
    worker = _worker(summary, result)
    tokens = _tokens_in(result)
    if tokens is None:
        tokens = _tokens_in(summary)
    explicit = result.get('cost_estimate') if isinstance(result, dict) else None
    if type(explicit) not in (int, float) or type(explicit) is bool:
        explicit = estimate_cost(worker, tokens)
    return {
        'worker': worker,
        'model': _model(policy, worker, result),
        'tokens': tokens,
        'cost_estimate': explicit,
        'exit_status': _exit_status(result, error),
        'task_class': (summary or {}).get('task_class'),
    }


class TracingOrchestrator:
    """Record one span around each orchestrator hook, then call the inner runner once."""

    def __init__(self, policy, summary, inner=None, task_class=None):
        self.policy = policy or {}
        self.summary = summary if isinstance(summary, dict) else {}
        self.inner = inner or NativeOrchestrator()
        if task_class is not None and self.summary.get('task_class') is None:
            self.summary['task_class'] = task_class

    def _warn(self, text):
        self.summary.setdefault('warnings', []).append(security.redact(str(text))[:300])

    def _export(self, span):
        mode = (self.policy.get('run_job') or {}).get('trace_exporter', 'jsonl')
        if mode not in ('otlp', 'both'):
            return
        try:
            from . import otlp
            endpoint = (self.policy.get('run_job') or {}).get('otlp_endpoint')
            otlp.export_span(span, endpoint=endpoint)
        except Exception as error:
            self._warn(f'otlp export failed: {type(error).__name__}: {error}')

    def _record(self, phase, started, finished, result, error):
        try:
            attrs = phase_attributes(self.policy, self.summary, result, error)
            job_id = self.summary.get('job_id') or 'job'
            span = traces.record_span(
                self.policy, job_id, name=phase, kind=phase,
                parent_span_id=f'{job_id}-root',
                side_effect=_SIDE_EFFECT[phase],
                status='ok' if error is None and attrs['exit_status'] in _OK_EXIT else 'failed',
                worker=attrs['worker'],
                model=attrs['model'],
                tokens=attrs['tokens'],
                cost_estimate=attrs['cost_estimate'],
                exit_status=attrs['exit_status'],
                task_class=attrs['task_class'],
                error=None if error is None else f'{type(error).__name__}: {error}',
                started_at=started.isoformat(timespec='seconds'),
                finished_at=finished.isoformat(timespec='seconds'),
            )
        except Exception as record_error:
            self._warn(f'trace not recorded: {type(record_error).__name__}: {record_error}')
            return
        self._export(span)

    def _call(self, phase, operation, *args, **kwargs):
        started = datetime.now(timezone.utc)
        try:
            result = getattr(self.inner, phase)(operation, *args, **kwargs)
        except Exception as error:
            self._record(phase, started, datetime.now(timezone.utc), None, error)
            raise
        self._record(phase, started, datetime.now(timezone.utc), result, None)
        return result

    def plan(self, operation, /, *args, **kwargs):
        return self._call('plan', operation, *args, **kwargs)

    def dispatch(self, operation, /, *args, **kwargs):
        return self._call('dispatch', operation, *args, **kwargs)

    def test(self, operation, /, *args, **kwargs):
        return self._call('test', operation, *args, **kwargs)

    def judge(self, operation, /, *args, **kwargs):
        return self._call('judge', operation, *args, **kwargs)

    def fix(self, operation, /, *args, **kwargs):
        return self._call('fix', operation, *args, **kwargs)


def _as_stamp(value, fallback):
    if isinstance(value, datetime):
        stamp = value
    elif value:
        stamp = datetime.fromisoformat(str(value))
    else:
        stamp = fallback
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def record_approval_wait(policy, job, exit_status, finished_at=None):
    """Append the approval-wait span for a committed job or a later approve/deny."""
    finished = _as_stamp(finished_at, datetime.now(timezone.utc))
    started = _as_stamp((job or {}).get('finished_at') or (job or {}).get('started_at'), finished)
    summary = dict(job or {})
    attrs = phase_attributes(policy, summary, {'status': exit_status})
    job_id = summary.get('job_id') or 'job'
    return traces.record_span(
        policy, job_id, name='approval_wait', kind='approval_wait',
        parent_span_id=f'{job_id}-root', side_effect='read_only',
        status='ok' if exit_status in ('approved', 'not_required', 'pending') else 'failed',
        worker=attrs['worker'], model=attrs['model'], tokens=attrs['tokens'],
        cost_estimate=attrs['cost_estimate'], exit_status=exit_status,
        task_class=attrs['task_class'],
        started_at=started.isoformat(timespec='seconds'),
        finished_at=finished.isoformat(timespec='seconds'),
    )
