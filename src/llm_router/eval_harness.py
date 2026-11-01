"""Evaluation harness, part 3 (#64): replay a corpus under two routing policies and write an A/B report.

A policy spec is a dict: {"name": ..., "worker": pinned worker or null, "worker_order": [...],
"never": [...], "config": {policy overrides, live mode only}}. The first spec is the baseline.

Modes:
  offline  fake workers only, nothing is executed. Each worker has a profile (pass_rate, tokens,
           quota_percent, elapsed_seconds, reviewer_score, scope); pass/fail is a deterministic hash of
           (case, worker), and a failed worker fails over to the next one in the policy's order. When the
           chosen worker is the one that ran the job, its recorded outcome is replayed instead.
  live     runs every replayable case through run-job (real workers, real quota) in a fresh worktree
           at the recorded base SHA. Refused unless confirm=True.

Gate (eval_score.gate): a routing/config change is adopted only if it beats the baseline here.
"""
import hashlib
import json
import re
import tempfile
from pathlib import Path

from . import config, eval_corpus, eval_score, security

MAX_HOPS = 3
DEFAULT_PROFILE = {'pass_rate': 0.5, 'tokens': 20000, 'quota_percent': 2.0, 'elapsed_seconds': 300.0,
                   'reviewer_score': None, 'scope': 'baseline'}


class Refused(Exception):
    pass


def reports_dir(policy):
    return config.policy_path(policy, 'run_job', 'eval_reports_dir', 'logs/eval/reports')


def validate_specs(specs):
    if not isinstance(specs, list) or len(specs) != 2:
        raise Refused('give exactly two policy specs: baseline first, candidate second')
    names = [spec.get('name') if isinstance(spec, dict) else None for spec in specs]
    if not all(names) or len(set(names)) != 2:
        raise Refused('each policy spec needs a distinct "name"')
    for spec in specs:
        if not spec.get('worker') and not spec.get('worker_order'):
            raise Refused(f'policy {spec["name"]!r} needs "worker" or "worker_order"')
    return specs


def order_for(spec):
    """Workers the policy would try, in order (a pinned worker never fails over)."""
    if spec.get('worker'):
        return [spec['worker']]
    never = set(spec.get('never') or [])
    return [w for w in spec.get('worker_order') or [] if w not in never][:MAX_HOPS]


def _roll(case_id, worker):
    """Deterministic number in [0, 1) for (case, worker): the same corpus always scores the same."""
    digest = hashlib.sha256(f'{case_id}\0{worker}'.encode()).hexdigest()
    return int(digest[:8], 16) / 0x100000000


def _recorded(case, worker):
    base = case['baseline']
    workers = base.get('workers') or []
    if not workers or workers[-1] != worker:
        return None
    return {'worker': worker, 'tests_status': base.get('tests_status'),
            'changed_paths': base.get('changed_paths') or [], 'tokens': base.get('tokens'),
            'quota_percent': base.get('quota_percent'), 'elapsed_seconds': base.get('elapsed_seconds'),
            'reviewer_score': base.get('reviewer_score'), 'recorded': True}


def fake_outcome(case, spec, profiles, use_recorded=True):
    """Offline result of `spec` on `case` with fake workers (accumulating cost across failovers)."""
    tokens = quota = elapsed = 0.0
    hops, last = [], None
    for worker in order_for(spec):
        hops.append(worker)
        recorded = _recorded(case, worker) if use_recorded else None
        if recorded:
            last = recorded
        else:
            profile = dict(DEFAULT_PROFILE, **(profiles.get(worker) or {}))
            passed = _roll(case['case_id'], worker) < profile['pass_rate']
            expected = case['baseline'].get('changed_paths') or [f'{case["case_id"]}.change']
            paths = expected if profile['scope'] == 'baseline' else expected + [f'extra/{worker}-{i}' for i in range(int(profile['scope']))]
            last = {'worker': worker, 'tests_status': 'passed' if passed else 'failed',
                    'changed_paths': paths, 'tokens': profile['tokens'],
                    'quota_percent': profile['quota_percent'], 'elapsed_seconds': profile['elapsed_seconds'],
                    'reviewer_score': profile['reviewer_score'] if passed else None, 'recorded': False}
        tokens += last['tokens'] or 0
        quota += last['quota_percent'] or 0
        elapsed += last['elapsed_seconds'] or 0
        if last['tests_status'] == 'passed':
            break
    if last is None:
        return {'case_id': case['case_id'], 'worker': None, 'tests_status': 'not_run', 'changed_paths': [],
                'tokens': None, 'quota_percent': None, 'elapsed_seconds': None, 'hops': []}
    return dict(last, case_id=case['case_id'], tokens=tokens or last['tokens'],
                quota_percent=quota or last['quota_percent'], elapsed_seconds=elapsed or last['elapsed_seconds'],
                hops=hops)


def live_outcome(case, spec, base_policy, run_job, meter, size=None):
    """Run the case through run-job once under `spec` and turn the job summary into an outcome."""
    policy = config.deep_merge(base_policy, spec.get('config') or {})
    if spec.get('worker_order'):
        policy['worker_order'] = list(spec['worker_order'])
    if spec.get('never') is not None:
        policy['never'] = list(spec['never'])
    effective_size = size or case.get('size') or 'full'
    with tempfile.TemporaryDirectory(prefix='llm-router-eval-') as tmp:
        task = Path(tmp) / (case.get('task') or f'{case["case_id"]}.md')
        task.write_text(case['task_text'])
        before = {w: meter(w) for w in order_for(spec)}
        job = run_job(case['repo'], str(task), base=case['base_sha'], worker=spec.get('worker'),
                      size=effective_size, keep=False, policy=policy)
    workers = job.get('workers') or []
    spent = [meter(w) - before[w] for w in workers
             if type(before.get(w)) in (int, float) and type(meter(w)) in (int, float)]
    observed = eval_corpus.case_from_job(job)['baseline']
    return {'case_id': case['case_id'], 'worker': workers[-1] if workers else None,
            'size': effective_size,
            'tests_status': job.get('tests_status'), 'changed_paths': observed['changed_paths'],
            'tokens': observed['tokens'], 'quota_percent': round(sum(spent), 2) if spent else None,
            'elapsed_seconds': observed['elapsed_seconds'], 'reviewer_score': job.get('reviewer_score'),
            'hops': workers, 'job_id': job.get('job_id'), 'status': job.get('status')}


def run(cases, specs, mode='offline', profiles=None, policy=None, confirm=False, run_job=None, meter=None,
        use_recorded=True, size=None):
    """Replay `cases` under both specs and return the A/B report dict (nothing is written)."""
    specs = validate_specs(specs)
    if mode not in ('offline', 'live'):
        raise Refused('mode must be offline or live')
    if size is not None and size not in ('draft', 'full'):
        raise Refused('size override must be draft or full')
    if mode == 'live' and not confirm:
        raise Refused('live mode runs real workers and spends quota: pass --yes to confirm')
    replayable = [c for c in cases if c.get('replayable')]
    skipped = [{'case_id': c.get('case_id'), 'why': c.get('not_replayable_because')}
               for c in cases if not c.get('replayable')]
    if mode == 'live':
        policy = policy or config.load_policy()
        if run_job is None:
            from .job_dispatch import run_job
        if meter is None:
            from .handoff import meter_percent as meter
    report = {'mode': mode, 'size_override': size, 'baseline': specs[0]['name'], 'candidate': specs[1]['name'],
              'cases': len(replayable), 'skipped': skipped, 'policies': {}}
    for spec in specs:
        outcomes = [dict(fake_outcome(c, spec, profiles or {}, use_recorded), size=size or c.get('size') or 'full')
                    if mode == 'offline'
                    else live_outcome(c, spec, policy, run_job, meter, size=size) for c in replayable]
        scores = [dict(eval_score.score(o, c['baseline'].get('changed_paths')), hops=o.get('hops'), size=o.get('size'))
                  for o, c in zip(outcomes, replayable)]
        report['policies'][spec['name']] = {'spec': spec, 'metrics': eval_score.aggregate(scores),
                                            'scores': scores}
    report['gate'] = eval_score.gate(report['policies'][specs[0]['name']]['metrics'],
                                     report['policies'][specs[1]['name']]['metrics'])
    return security.redact_obj(report)


def render_markdown(report):
    names = [report['baseline'], report['candidate']]
    keys = ['cases', 'pass_rate', 'quality_mean', 'tokens_total', 'tokens_mean', 'quota_percent_mean',
            'elapsed_seconds_median', 'elapsed_seconds_mean']
    size_tag = f', size={report["size_override"]}' if report.get('size_override') else ''
    lines = [f'# Router eval: {names[0]} (baseline) vs {names[1]} ({report["mode"]}{size_tag})', '',
             f'| metric | {names[0]} | {names[1]} |', '|---|---|---|']
    for key in keys:
        values = [report['policies'][n]['metrics'].get(key) for n in names]
        lines.append(f'| {key} | ' + ' | '.join('-' if v is None else str(v) for v in values) + ' |')
    verdict = 'ADOPT' if report['gate']['adopt'] else 'KEEP BASELINE'
    lines += ['', f'**Gate: {verdict}** ({"; ".join(report["gate"]["reasons"])})', '']
    if report['skipped']:
        lines.append(f'Skipped {len(report["skipped"])} non-replayable case(s).')
    return '\n'.join(lines) + '\n'


def write_report(report, policy, stamp):
    directory = reports_dir(policy)
    directory.mkdir(parents=True, exist_ok=True)
    safe = lambda name: re.sub(r'[^A-Za-z0-9_.-]+', '-', str(name))[:40]
    base = directory / f'{stamp}-{safe(report["baseline"])}-vs-{safe(report["candidate"])}'
    json_path, md_path = base.with_suffix('.json'), base.with_suffix('.md')
    json_path.write_text(json.dumps(report, indent=2))
    md_path.write_text(render_markdown(report))
    return {'json': str(json_path), 'markdown': str(md_path)}
