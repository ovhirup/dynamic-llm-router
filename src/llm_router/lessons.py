"""Per-repo lessons file (#106): learn one constraint rule from each fixed failure and inject into future jobs.

When a job fails on an earlier attempt and later passes on a retry or failover hop, the router
extracts one short constraint-style rule (never a full transcript), files it under the owning layer:
  - `harness` (test environment, worktree, scope, leak check)
  - `loop`    (retry, assertion fix, worker continuation)
  - `graph`   (failover hop, worker selection, concurrency)
deduplicates and caps entries (`MAX_LESSONS = 20`), writes `<state>/lessons/<repo>.md` outside the
worktree, and injects the repo's lessons into future job prompts. Nothing is ever written into the
target repository itself.
"""
import os
from pathlib import Path
import re

from . import config, security

LAYERS = ('harness', 'loop', 'graph')
MAX_LESSONS = 20
MAX_RULE_CHARS = 200
_BULLET_RE = re.compile(r'^\s*-\s*\[(?P<layer>harness|loop|graph)\]\s*(?P<rule>.+?)\s*$', re.I)


def lessons_dir(policy=None):
    return config.policy_path(policy or {}, 'run_job', 'lessons_dir', 'lessons')


def repo_key(repo):
    name = Path(str(repo)).name.lower()
    return re.sub(r'[^a-z0-9._-]+', '-', name).strip('-') or 'repo'


def lessons_path(policy, repo):
    return lessons_dir(policy) / f'{repo_key(repo)}.md'


def load_lessons(policy, repo):
    """Return list of `{'layer': ..., 'rule': ...}` from `<state>/lessons/<repo>.md`."""
    path = lessons_path(policy, repo)
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(errors='replace').splitlines():
        match = _BULLET_RE.match(line)
        if match:
            entries.append({
                'layer': match.group('layer').lower(),
                'rule': security.redact(match.group('rule').strip()),
            })
    return entries


def _normalize(rule):
    cleaned = re.sub(r'\s+', ' ', (rule or '').strip())
    return security.redact(cleaned)[:MAX_RULE_CHARS]


def save_lessons(policy, repo, entries, max_lessons=MAX_LESSONS):
    """Write deduplicated, capped lessons to `<state>/lessons/<repo>.md` (`0600`)."""
    name = repo_key(repo)
    seen = set()
    deduped = []
    for item in entries:
        layer = (item.get('layer') or 'loop').lower()
        if layer not in LAYERS:
            layer = 'loop'
        rule = _normalize(item.get('rule', ''))
        if not rule:
            continue
        key = (layer, rule.lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append({'layer': layer, 'rule': rule})
    capped = deduped[-max(1, int(max_lessons)):]
    by_layer = {layer: [e['rule'] for e in capped if e['layer'] == layer] for layer in LAYERS}
    lines = [f'# Lessons for {name}', '']
    for layer in LAYERS:
        lines.append(f'## {layer}')
        for rule in by_layer[layer]:
            lines.append(f'- [{layer}] {rule}')
        lines.append('')
    path = lessons_path(policy, repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write('\n'.join(lines))
    os.chmod(path, 0o600)
    return path


def record_lesson(policy, repo, rule, layer='loop', max_lessons=MAX_LESSONS):
    """Append one rule under `layer` (`harness` / `loop` / `graph`) with dedup and cap."""
    existing = load_lessons(policy, repo)
    existing.append({'layer': layer, 'rule': rule})
    return save_lessons(policy, repo, existing, max_lessons=max_lessons)


def classify_layer(reason, switched_workers=False):
    text = (reason or '').lower()
    if switched_workers or 'quota' in text or 'auth' in text or 'cooldown' in text:
        return 'graph'
    if 'scope' in text or 'leak' in text or 'timeout' in text or 'worktree' in text:
        return 'harness'
    return 'loop'


def learn_from_fixed_job(policy, summary):
    """When a job passes after an earlier failed attempt or worker hop, record one constraint lesson."""
    if not isinstance(summary, dict) or not summary.get('ok'):
        return None
    attempts = summary.get('attempts') or []
    hops = summary.get('hops') or []
    failed_attempts = [a for a in attempts[:-1] if not a.get('ok') or a.get('tests') != 'passed']
    failed_hops = [h for h in hops if not h.get('ok')]
    if not failed_attempts and not failed_hops:
        return None
    repo = summary.get('repo') or 'repo'
    name = repo_key(repo)
    handoffs = summary.get('handoffs') or []
    gotcha = ''
    for item in reversed(handoffs):
        path = Path(item.get('path') or '/nonexistent')
        if path.is_file():
            text = path.read_text(errors='replace')
            if '## Gotchas' in text:
                section = text.split('## Gotchas', 1)[1].split('\n## ', 1)[0].strip()
                first_line = next((l.lstrip('-* ').strip() for l in section.splitlines()
                                   if l.strip() and l.strip() != 'none'), '')
                if first_line:
                    gotcha = first_line
                    break
    if failed_hops:
        first_fail = failed_hops[0]
        reason = first_fail.get('reason') or 'worker_failure'
        layer = classify_layer(reason, switched_workers=len({h.get('worker') for h in hops}) > 1)
        detail = gotcha or f"avoid {first_fail.get('worker')} {reason}; verify with `{summary.get('test_command') or 'tests'}`"
    else:
        first_att = failed_attempts[0]
        reason = f"tests {first_att.get('tests')}"
        layer = classify_layer(reason, switched_workers=False)
        detail = gotcha or f"ensure `{summary.get('test_command') or 'tests'}` passes on the first run before finishing"
    rule = f'{name}: {detail}'
    path = record_lesson(policy, repo, rule, layer=layer,
                         max_lessons=(policy.get('run_job') or {}).get('max_lessons', MAX_LESSONS))
    return {'path': str(path), 'layer': layer, 'rule': _normalize(rule)}


def render_for_prompt(policy, repo):
    """Format stored repo lessons for injection into a new job's prompt, or '' when none exist."""
    entries = load_lessons(policy, repo)
    if not entries:
        return ''
    lines = [f'## Repo lessons ({repo_key(repo)})',
             'Follow these constraint rules learned from earlier fixed failures in this repository:']
    for item in entries:
        lines.append(f"- [{item['layer']}] {item['rule']}")
    return '\n'.join(lines)
