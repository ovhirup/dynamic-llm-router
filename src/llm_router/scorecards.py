"""Worker scorecards per task class (docs/ROUTER_BRAIN.md section (c)).

Each worker gets `<scorecards_dir>/<worker>.json` (machine-readable totals per task class) and a
rendered `<scorecards_dir>/<worker>.md` table. `class_score` feeds `scheduler.capability`.
"""
import json
import os
import re

from . import config

COLUMNS = ('runs', 'passed', 'pass_rate', 'review_fixes', 'tokens_mean', 'elapsed_seconds_mean')


def scorecards_dir(policy):
    return config.policy_path(policy, 'run_job', 'scorecards_dir', 'vault/scorecards')


def _safe(worker):
    name = re.sub(r'[^A-Za-z0-9._-]', '_', str(worker))
    if name in ('', '.', '..'):
        raise ValueError(f'invalid worker name: {worker!r}')
    return name


def _json_path(policy, worker):
    return scorecards_dir(policy) / f'{_safe(worker)}.json'


def load_scorecard(policy, worker):
    try:
        data = json.loads(_json_path(policy, worker).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _derive(row):
    runs = row['runs']
    row['pass_rate'] = row['passed'] / runs if runs else 0.0
    row['tokens_mean'] = row['tokens_total'] / row['tokens_runs'] if row['tokens_runs'] else None
    row['elapsed_seconds_mean'] = (row['elapsed_seconds_total'] / row['elapsed_runs']
                                   if row['elapsed_runs'] else None)
    return row


def _fmt(value, column=None):
    if value is None:
        return '-'
    if column == 'tokens_mean':
        return str(int(round(value)))
    if isinstance(value, float):
        return f'{value:.2f}'
    return str(value)


def render_markdown(worker, card):
    lines = [f'# Scorecard: {worker}', '', '| task_class | ' + ' | '.join(COLUMNS) + ' |',
             '|' + '---|' * (len(COLUMNS) + 1)]
    for task_class in sorted((card.get('classes') or {})):
        row = card['classes'][task_class]
        lines.append(f'| {task_class} | ' + ' | '.join(_fmt(row.get(c), c) for c in COLUMNS) + ' |')
    return '\n'.join(lines) + '\n'


def _write(path, text):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)


def record_outcome(policy, worker, task_class, passed, tokens=None, elapsed_seconds=None, review_fixes=0):
    directory = scorecards_dir(policy)
    directory.mkdir(parents=True, exist_ok=True)
    card = load_scorecard(policy, worker)
    card['worker'] = worker
    classes = card.setdefault('classes', {})
    row = classes.setdefault(str(task_class), {})
    for key in ('runs', 'passed', 'review_fixes', 'tokens_total', 'tokens_runs',
                'elapsed_seconds_total', 'elapsed_runs'):
        row.setdefault(key, 0)
    row['runs'] += 1
    row['passed'] += 1 if passed else 0
    row['review_fixes'] += int(review_fixes or 0)
    if tokens is not None:
        row['tokens_total'] += tokens
        row['tokens_runs'] += 1
    if elapsed_seconds is not None:
        row['elapsed_seconds_total'] += elapsed_seconds
        row['elapsed_runs'] += 1
    _derive(row)
    _write(_json_path(policy, worker), json.dumps(card, indent=2, sort_keys=True) + '\n')
    _write(directory / f'{_safe(worker)}.md', render_markdown(worker, card))
    return card


def class_score(policy, worker, task_class):
    row = (load_scorecard(policy, worker).get('classes') or {}).get(str(task_class)) or {}
    runs = row.get('runs') or 0
    if not isinstance(runs, (int, float)) or runs <= 0:
        return None
    return min(max(float(row.get('passed') or 0) / runs, 0.0), 1.0)
