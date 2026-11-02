"""Evidence-gated unit loop (#140): baseline first, checks ledger, 3-strike block, resumable graph & progress.

Enforces Karpathy/autoresearch-style evidence gates across unit DAG runs:
  1. `record_baseline(state_dir, check_runner)`: runs baseline checks BEFORE any unit edits code
     and writes `baseline.md` (recording `'no baseline'` if no check exists yet).
  2. `write_graph_plan(state_dir, units)`: writes `graph.md` with one input, one output, and one
     acceptance check per unit.
  3. `write_checks_ledger(state_dir, rows)`: writes `checks.md` (`unit | requirement | status | evidence`).
     Hard rule: a row without a non-empty `evidence` file path is ALWAYS marked `'unresolved'`,
     never `'pass'`.
  4. Keep only wins: `evaluate_against_baseline(baseline_score, candidate_score, revert_fn)` reverts
     the unit's worktree if its check score regresses below `baseline_score`.
  5. Three strikes: `run_evidence_loop(...)` retries a failing unit up to `MAX_STRIKES = 3` times
     with scoped feedback (`"fix this unit only"`); after 3 failed strikes, marks the unit `'blocked'`
     and continues running independent units in the graph.
  6. Resumable `progress.md`: `write_progress` / `read_progress` persist `done`, `running`, `blocked`,
     `failed_attempts`, and `next_unit` so a resuming worker picks up at `next_unit`.
"""
import json
from pathlib import Path

from . import security

MAX_STRIKES = 3


def record_baseline(state_dir, check_runner=None):
    """Run baseline checks before any unit edits and write `<state_dir>/baseline.md`."""
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'baseline.md'
    if check_runner is None:
        info = {'has_baseline': False, 'status': 'no baseline', 'score': None, 'evidence': None}
        path.write_text('# Baseline\n\n- Status: no baseline\n')
        return info
    raw = check_runner() or {}
    status = str(raw.get('status') or ('pass' if raw.get('ok') else 'fail'))
    score = float(raw['score']) if type(raw.get('score')) in (int, float) else (1.0 if status == 'pass' else 0.0)
    evidence = str(raw.get('evidence') or '').strip() or None
    lines = [
        '# Baseline',
        '',
        f'- Status: {security.redact(status)}',
        f'- Score: {score}',
        f'- Evidence: {security.redact(evidence or "none")}',
        '',
    ]
    path.write_text('\n'.join(lines))
    return {'has_baseline': True, 'status': status, 'score': score, 'evidence': evidence, 'path': str(path)}


def write_graph_plan(state_dir, units):
    """Write `<state_dir>/graph.md` with one input, one output, and one acceptance check per unit."""
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'graph.md'
    lines = [
        '# Unit Graph Plan',
        '',
        '| Unit | Input | Output | Acceptance Check | Deps |',
        '|---|---|---|---|---|',
    ]
    for unit in units or ():
        uid = security.redact(str(unit.get('id') or 'unit'))
        inp = security.redact(str(unit.get('input') or ', '.join(unit.get('scope') or ('repo',))))
        out = security.redact(str(unit.get('output') or ', '.join(unit.get('scope') or ('diff',))))
        chk = security.redact(str(unit.get('check') or 'pytest -q'))
        deps = ', '.join(unit.get('deps') or ()) or '-'
        lines.append(f'| {uid} | {inp} | {out} | `{chk}` | {deps} |')
    path.write_text('\n'.join(lines) + '\n')
    return path


def normalize_check_row(unit_id, requirement, requested_status, evidence_path=None):
    """Enforce the evidence rule: missing or non-existent evidence_path forces status to `'unresolved'`."""
    ev_str = str(evidence_path or '').strip()
    has_evidence = bool(ev_str and Path(ev_str).is_file())
    if requested_status == 'pass' and not has_evidence:
        status = 'unresolved'
    elif requested_status in ('pass', 'fail', 'unresolved'):
        status = requested_status
    else:
        status = 'unresolved'
    return {
        'unit': str(unit_id),
        'requirement': security.redact(str(requirement or 'unit check')),
        'status': status,
        'evidence': ev_str if has_evidence else None,
    }


def write_checks_ledger(state_dir, rows):
    """Write `<state_dir>/checks.md` (`unit | requirement | status | evidence`)."""
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'checks.md'
    normalized = [
        normalize_check_row(r.get('unit'), r.get('requirement'), r.get('status'), r.get('evidence'))
        for r in (rows or ())
    ]
    lines = [
        '# Checks Ledger',
        '',
        '| Unit | Requirement | Status | Evidence |',
        '|---|---|---|---|',
    ]
    for row in normalized:
        lines.append(
            f"| {row['unit']} | {row['requirement']} | {row['status']} | {row['evidence'] or 'unresolved (no evidence)'} |"
        )
    path.write_text('\n'.join(lines) + '\n')
    return {'path': str(path), 'rows': normalized}


def evaluate_against_baseline(baseline_score, candidate_score, revert_fn=None):
    """Keep only wins: if `candidate_score < baseline_score`, call `revert_fn()` and reject the change."""
    if baseline_score is not None and type(candidate_score) in (int, float) and candidate_score < float(baseline_score):
        if revert_fn is not None:
            revert_fn()
        return {'kept': False, 'reverted': True, 'reason': f'regressed vs baseline ({candidate_score} < {baseline_score})'}
    return {'kept': True, 'reverted': False, 'reason': 'matches_or_beats_baseline'}


def write_progress(state_dir, done, running, blocked, next_unit, failed_attempts=(), decisions=()):
    """Write `<state_dir>/progress.md` (with embedded JSON state block for deterministic resume)."""
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / 'progress.md'
    state = security.redact_obj({
        'done': list(done or ()),
        'running': list(running or ()),
        'blocked': list(blocked or ()),
        'next_unit': next_unit,
        'failed_attempts': list(failed_attempts or ()),
        'decisions': list(decisions or ()),
    })
    lines = [
        '# Unit Loop Progress',
        '',
        f"- Done: {', '.join(state['done']) or 'none'}",
        f"- Running: {', '.join(state['running']) or 'none'}",
        f"- Blocked: {', '.join(state['blocked']) or 'none'}",
        f"- Next unit: {state['next_unit'] or 'none'}",
        '',
        '```json',
        json.dumps(state, indent=2, sort_keys=True),
        '```',
        '',
    ]
    path.write_text('\n'.join(lines))
    return path


def read_progress(state_dir):
    """Read `<state_dir>/progress.md` and return the parsed state dict (or empty defaults)."""
    path = Path(state_dir) / 'progress.md'
    if not path.is_file():
        return {'done': [], 'running': [], 'blocked': [], 'next_unit': None, 'failed_attempts': [], 'decisions': []}
    text = path.read_text(errors='replace')
    if '```json' in text:
        block = text.split('```json', 1)[1].split('```', 1)[0].strip()
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass
    return {'done': [], 'running': [], 'blocked': [], 'next_unit': None, 'failed_attempts': [], 'decisions': []}


def run_evidence_loop(state_dir, units, unit_runner, baseline_runner=None, max_strikes=MAX_STRIKES, resume=True):
    """Execute `units` with baseline-first check, 3-strike cap per unit, revert on regression, and progress resume."""
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    baseline = record_baseline(root, baseline_runner)
    write_graph_plan(root, units)

    prior = read_progress(root) if resume else {'done': [], 'blocked': [], 'failed_attempts': []}
    done = list(prior.get('done') or [])
    blocked = list(prior.get('blocked') or [])
    failed_attempts = list(prior.get('failed_attempts') or [])
    check_rows = []
    unit_reports = {}

    pending = [u for u in (units or ()) if u['id'] not in done and u['id'] not in blocked]
    for idx, unit in enumerate(pending):
        uid = unit['id']
        unmet_deps = [d for d in (unit.get('deps') or ()) if d not in done]
        if unmet_deps:
            blocked.append(uid)
            check_rows.append({'unit': uid, 'requirement': unit.get('check', 'deps'), 'status': 'fail', 'evidence': None})
            unit_reports[uid] = {'status': 'blocked_by_dependency', 'attempts': 0, 'reverted': False}
            continue

        next_after = pending[idx + 1]['id'] if idx + 1 < len(pending) else None
        write_progress(root, done, [uid], blocked, next_after, failed_attempts=failed_attempts)

        strikes = 0
        unit_passed = False
        reverted_any = False
        last_evidence = None
        while strikes < max_strikes:
            strikes += 1
            feedback = f'Fix this unit only ({uid}); strike {strikes}/{max_strikes}.' if strikes > 1 else ''
            outcome = unit_runner(unit, strikes, feedback) or {}
            ev_path = outcome.get('evidence')
            cand_score = outcome.get('score')
            revert_fn = outcome.get('revert')

            gate = evaluate_against_baseline(baseline.get('score'), cand_score, revert_fn=revert_fn)
            if gate['reverted']:
                reverted_any = True
                failed_attempts.append({'unit': uid, 'attempt': strikes, 'reason': gate['reason']})
                continue

            row = normalize_check_row(
                uid,
                unit.get('check') or 'acceptance_check',
                'pass' if outcome.get('ok') else 'fail',
                evidence_path=ev_path,
            )
            last_evidence = row['evidence']
            if row['status'] == 'pass':
                unit_passed = True
                check_rows.append(row)
                break
            failed_attempts.append({
                'unit': uid,
                'attempt': strikes,
                'reason': outcome.get('reason') or ('missing_evidence' if outcome.get('ok') else 'check_failed'),
            })

        if unit_passed:
            done.append(uid)
            unit_reports[uid] = {'status': 'done', 'attempts': strikes, 'reverted': reverted_any, 'evidence': last_evidence}
        else:
            blocked.append(uid)
            check_rows.append({'unit': uid, 'requirement': unit.get('check') or 'acceptance_check',
                               'status': 'unresolved' if last_evidence is None else 'fail', 'evidence': last_evidence})
            unit_reports[uid] = {'status': 'blocked', 'attempts': strikes, 'reverted': reverted_any, 'evidence': last_evidence}

    write_progress(root, done, [], blocked, None, failed_attempts=failed_attempts)
    ledger = write_checks_ledger(root, check_rows)
    return {
        'baseline': baseline,
        'done': done,
        'blocked': blocked,
        'failed_attempts': failed_attempts,
        'checks': ledger['rows'],
        'units': unit_reports,
        'files': {
            'baseline': str(root / 'baseline.md'),
            'graph': str(root / 'graph.md'),
            'checks': str(root / 'checks.md'),
            'progress': str(root / 'progress.md'),
        },
    }
