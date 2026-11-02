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


def live_outcome(case, spec, base_policy, run_job, meter):
    """Run the case through run-job once under `spec` and turn the job summary into an outcome."""
    policy = config.deep_merge(base_policy, spec.get('config') or {})
    if spec.get('worker_order'):
        policy['worker_order'] = list(spec['worker_order'])
    if spec.get('never') is not None:
        policy['never'] = list(spec['never'])
    with tempfile.TemporaryDirectory(prefix='llm-router-eval-') as tmp:
        task = Path(tmp) / (case.get('task') or f'{case["case_id"]}.md')
        task.write_text(case['task_text'])
        before = {w: meter(w) for w in order_for(spec)}
        job = run_job(case['repo'], str(task), base=case['base_sha'], worker=spec.get('worker'),
                      size=case.get('size') or 'full', keep=False, policy=policy)
    workers = job.get('workers') or []
    hops_list = job.get('hops') or []
    all_runs_unavailable = bool(hops_list) and all(h.get('unavailable') for h in hops_list)
    attempts = job.get('attempts') or []
    refused_attempt = any(a.get('refused') for a in attempts if isinstance(a, dict))
    status = job.get('status')
    unavailable = (status in ('refused', 'handoff_to_orchestrator')
                   or all_runs_unavailable
                   or (not job.get('ok') and refused_attempt and not workers))
    reason = None
    if unavailable:
        reason = (job.get('reason')
                  or (hops_list[-1].get('reason') if hops_list else None)
                  or next((a.get('refused') for a in reversed(attempts) if isinstance(a, dict) and a.get('refused')), None)
                  or (job.get('warnings') or [None])[-1]
                  or status
                  or 'unavailable')
    after = {w: meter(w) for w in workers}
    spent = [after[w] - before[w] for w in workers
             if type(before.get(w)) in (int, float) and type(after.get(w)) in (int, float)]
    if not workers:
        quota_delta_status = 'no_worker_ran'
        quota_delta_reason = job.get('reason') or job.get('status') or 'no worker ran'
    elif spent:
        quota_delta_status = 'ok'
        quota_delta_reason = None
    else:
        missing = [w for w in workers if type(before.get(w)) not in (int, float) or type(after.get(w)) not in (int, float)]
        quota_delta_status = 'unknown'
        quota_delta_reason = f'meter stale or unknown for {", ".join(missing)}'
    observed = eval_corpus.case_from_job(job)['baseline']
    return {'case_id': case['case_id'], 'worker': workers[-1] if workers else spec.get('worker'),
            'tests_status': 'unavailable' if unavailable else job.get('tests_status'),
            'changed_paths': observed['changed_paths'],
            'tokens': observed['tokens'], 'quota_percent': round(sum(spent), 2) if spent else None,
            'quota_delta_status': quota_delta_status, 'quota_delta_reason': quota_delta_reason,
            'elapsed_seconds': observed['elapsed_seconds'], 'reviewer_score': job.get('reviewer_score'),
            'hops': workers or [h['worker'] for h in hops_list if h.get('worker')],
            'job_id': job.get('job_id'),
            'status': 'unavailable' if unavailable else status,
            'unavailable': unavailable,
            'reason': reason}


def _delta(base, cand, digits=4):
    """candidate - baseline, or None when either side is not a number."""
    if type(base) not in (int, float) or type(cand) not in (int, float):
        return None
    return round(cand - base, digits)


def _winner(base, cand):
    """Per-case verdict: tests pass first, then quality, then fewer tokens, then less time."""
    if base['passed'] != cand['passed']:
        return 'candidate' if cand['passed'] else 'baseline'
    for key, better in (('quality', 1), ('tokens', -1), ('elapsed_seconds', -1)):
        diff = _delta(base.get(key), cand.get(key))
        if diff:
            return 'candidate' if diff * better > 0 else 'baseline'
    return 'tie'


def compare_cases(baseline_scores, candidate_scores):
    """Side-by-side per-case comparison of two policies' score() lists (matched by case_id)."""
    by_id = {s.get('case_id'): s for s in candidate_scores}
    rows, counts = [], {'candidate': 0, 'baseline': 0, 'tie': 0}
    for base in baseline_scores:
        cand = by_id.get(base.get('case_id'))
        if cand is None:
            continue
        winner = _winner(base, cand)
        counts[winner] += 1
        rows.append({'case_id': base.get('case_id'),
                     'baseline_worker': base.get('worker'), 'candidate_worker': cand.get('worker'),
                     'baseline_passed': base['passed'], 'candidate_passed': cand['passed'],
                     'baseline_quality': base.get('quality'), 'candidate_quality': cand.get('quality'),
                     'baseline_tokens': base.get('tokens'), 'candidate_tokens': cand.get('tokens'),
                     'baseline_elapsed_seconds': base.get('elapsed_seconds'),
                     'candidate_elapsed_seconds': cand.get('elapsed_seconds'),
                     'quality_delta': _delta(base.get('quality'), cand.get('quality')),
                     'tokens_delta': _delta(base.get('tokens'), cand.get('tokens')),
                     'elapsed_delta': _delta(base.get('elapsed_seconds'), cand.get('elapsed_seconds')),
                     'winner': winner})

    def total(key):
        values = [r[key] for r in rows if r[key] is not None]
        return round(sum(values), 4) if values else None

    deltas = {'passed': sum(r['candidate_passed'] for r in rows) - sum(r['baseline_passed'] for r in rows),
              'quality': total('quality_delta'), 'tokens': total('tokens_delta'),
              'elapsed_seconds': total('elapsed_delta')}
    return {'cases': rows, 'wins': counts['candidate'], 'losses': counts['baseline'], 'ties': counts['tie'],
            'deltas': deltas}


def run(cases, specs, mode='offline', profiles=None, policy=None, confirm=False, run_job=None, meter=None,
        use_recorded=True, task_class=None):
    """Replay `cases` under both specs and return the A/B report dict (nothing is written).

    `task_class` restricts the run to cases of that class; each policy entry carries `by_class` metrics so a
    capability update for one class is judged on that class's cases.
    """
    specs = validate_specs(specs)
    if mode not in ('offline', 'live'):
        raise Refused('mode must be offline or live')
    if mode == 'live' and not confirm:
        raise Refused('live mode runs real workers and spends quota: pass --yes to confirm')
    cases = eval_corpus.filter_by_class(cases, task_class)
    replayable = [c for c in cases if c.get('replayable')]
    skipped = [{'case_id': c.get('case_id'), 'why': c.get('not_replayable_because')}
               for c in cases if not c.get('replayable')]
    if mode == 'live':
        policy = policy or config.load_policy()
        if run_job is None:
            from .job_dispatch import run_job
        if meter is None:
            from .handoff import meter_percent as meter
    report = {'mode': mode, 'baseline': specs[0]['name'], 'candidate': specs[1]['name'],
              'cases': len(replayable), 'skipped': skipped, 'task_class': task_class, 'policies': {}}
    for spec in specs:
        outcomes = [fake_outcome(c, spec, profiles or {}, use_recorded) if mode == 'offline'
                    else live_outcome(c, spec, policy, run_job, meter) for c in replayable]
        scores = [dict(eval_score.score(o, c['baseline'].get('changed_paths')), hops=o.get('hops'))
                  for o, c in zip(outcomes, replayable)]
        unavailable_rows = [{'case_id': s.get('case_id'), 'worker': s.get('worker'), 'reason': s.get('reason')}
                            for s in scores if s.get('unavailable')]
        grouped = {}
        for score, case in zip(scores, replayable):
            if case.get('task_class'):
                grouped.setdefault(case['task_class'], []).append(score)
        report['policies'][spec['name']] = {
            'spec': spec, 'metrics': eval_score.aggregate(scores), 'scores': scores,
            'unavailable': unavailable_rows,
            'by_class': {name: eval_score.aggregate(class_scores) for name, class_scores in sorted(grouped.items())}}
    report['gate'] = eval_score.gate(report['policies'][specs[0]['name']]['metrics'],
                                     report['policies'][specs[1]['name']]['metrics'])
    report['comparison'] = compare_cases(report['policies'][specs[0]['name']]['scores'],
                                         report['policies'][specs[1]['name']]['scores'])
    return security.redact_obj(report)


def render_markdown(report):
    names = [report['baseline'], report['candidate']]
    keys = ['cases', 'unavailable', 'pass_rate', 'quality_mean', 'tokens_total', 'tokens_mean', 'quota_percent_mean',
            'elapsed_seconds_median', 'elapsed_seconds_mean']
    lines = [f'# Router eval: {names[0]} (baseline) vs {names[1]} ({report["mode"]})', '',
             f'| metric | {names[0]} | {names[1]} | Delta |', '|---|---|---|---|']
    def cell(v):
        if v is None:
            return '-'
        if type(v) is float and v.is_integer() and abs(v) >= 2:  # 57000.0 -> 57000; keep 1.0/0.0 ratios
            v = int(v)
        return str(v)

    def signed(v):
        return f'+{cell(v)}' if type(v) in (int, float) and v > 0 else cell(v)
    for key in keys:
        values = [report['policies'][n]['metrics'].get(key) for n in names]
        lines.append(f'| {key} | ' + ' | '.join(cell(v) for v in values) + f' | {signed(_delta(*values))} |')
    verdict = 'ADOPT' if report['gate']['adopt'] else 'KEEP BASELINE'
    lines += ['', f'**Gate: {verdict}** ({"; ".join(report["gate"]["reasons"])})', '']
    unavail = [(n, row) for n in names for row in report['policies'][n].get('unavailable') or []]
    if unavail:
        lines.append('### Unavailable workers (excluded from pass rate and gate)')
        for policy_name, row in unavail:
            lines.append(f'- `{policy_name}` / `{row.get("case_id")}` ({row.get("worker")}): {row.get("reason")}')
        lines.append('')
    comparison = report.get('comparison')
    if comparison:
        deltas = comparison['deltas']
        lines += ['## Per-case comparison', '',
                  f'Candidate wins {comparison["wins"]}, losses {comparison["losses"]}, ties {comparison["ties"]} '
                  f'(deltas: passed {signed(deltas["passed"])}, quality {signed(deltas["quality"])}, '
                  f'tokens {signed(deltas["tokens"])}, elapsed_seconds {signed(deltas["elapsed_seconds"])})', '',
                  '| case_id | baseline worker | pass | quality | tokens | time (s) '
                  '| candidate worker | pass | quality | tokens | time (s) | winner |',
                  '|---|---|---|---|---|---|---|---|---|---|---|---|']
        for row in comparison['cases']:
            sides = [[row[f'{side}_worker'], 'yes' if row[f'{side}_passed'] else 'no', row[f'{side}_quality'],
                      row[f'{side}_tokens'], row[f'{side}_elapsed_seconds']] for side in ('baseline', 'candidate')]
            lines.append(f'| {row["case_id"]} | ' + ' | '.join(cell(v) for v in sides[0] + sides[1])
                         + f' | {row["winner"]} |')
        lines.append('')
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
