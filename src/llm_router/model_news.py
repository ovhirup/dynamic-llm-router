"""Weekly model-news digest from X bookmarks and release notes (#53).

Parent: #52 (Model-news adaptation).
Ingests saved X bookmark exports or release-note items, extracts candidate worker/task-class
capability claims (always requiring a source URL, per the vouch rule in `docs/ROUTER_BRAIN.md`),
writes a redacted weekly markdown digest to `<vault_dir>/research/<YYYY-Www>-model-news.md`, and
emits candidate capability updates that must pass the DLR evaluation harness (#61) before adoption.
Never routes on hype alone.
"""
from datetime import datetime, timezone
from pathlib import Path
import re

from . import config, security

WORKERS = ('claude', 'codex', 'grok-build', 'cursor', 'aider-ollama', 'nativ')
CLASSES = ('feature', 'bugfix', 'review', 'mechanical', 'draft', 'design')


def _infer_workers(text):
    lower = (text or '').lower()
    hits = []
    for w, keywords in (
        ('claude', ('claude', 'opus', 'sonnet', 'haiku', 'anthropic')),
        ('codex', ('codex', 'gpt-', 'openai', 'o3', 'o4')),
        ('grok-build', ('grok', 'xai', 'supergrok')),
        ('cursor', ('cursor', 'cursor-agent')),
        ('aider-ollama', ('ollama', 'qwen', 'aider')),
        ('nativ', ('nativ', 'mlx')),
    ):
        if any(k in lower for k in keywords):
            hits.append(w)
    return hits


def _infer_classes(text):
    lower = (text or '').lower()
    hits = []
    for cls, keywords in (
        ('feature', ('feature', 'architecture', 'coding', 'agent')),
        ('bugfix', ('bug', 'debug', 'fix', 'swe-bench')),
        ('review', ('review', 'critique', 'audit')),
        ('mechanical', ('refactor', 'mechanical', 'fast', 'edit')),
        ('draft', ('draft', 'summary', 'writing')),
        ('design', ('ui', 'design', 'swiftui', 'react', 'figma', 'css')),
    ):
        if any(k in lower for k in keywords):
            hits.append(cls)
    return hits or ['feature']


def normalize_item(raw):
    """Validate and normalize a bookmark or release-note item; requires a non-empty `url` / `source`."""
    if not isinstance(raw, dict):
        raise ValueError('digest item must be a dict')
    source = str(raw.get('url') or raw.get('source') or '').strip()
    if not source:
        raise ValueError('digest item requires a source URL (unsourced claims are rejected)')
    text = str(raw.get('text') or raw.get('summary') or raw.get('title') or '').strip()
    if not text:
        raise ValueError('digest item requires non-empty text or summary')
    title = str(raw.get('title') or text.splitlines()[0][:80]).strip()
    kind = 'bookmark' if 'x.com/' in source or 'twitter.com/' in source or raw.get('kind') == 'bookmark' else 'release_note'
    workers = list(raw.get('workers') or _infer_workers(title + ' ' + text))
    classes = list(raw.get('task_classes') or _infer_classes(title + ' ' + text))
    return {
        'kind': kind,
        'title': security.redact(title),
        'text': security.redact(text[:1000]),
        'source': security.redact(source),
        'published_at': str(raw.get('published_at') or raw.get('date') or ''),
        'workers': workers,
        'task_classes': classes,
    }


def extract_candidate_claims(items, verified_at=None):
    """Build candidate capability claims (requiring eval harness verification before adoption)."""
    today = str(verified_at or datetime.now(timezone.utc).date().isoformat())
    claims = []
    for raw in items or ():
        item = normalize_item(raw)
        for worker in item['workers']:
            for cls in item['task_classes']:
                claims.append({
                    'worker': worker,
                    'task_class': cls,
                    'claim': item['title'],
                    'source': item['source'],
                    'verified_at': today,
                    'status': 'candidate_requires_eval_harness',
                })
    return claims


def render_digest_markdown(week_label, normalized_items, candidate_claims):
    """Render the weekly model-news digest markdown note."""
    bookmarks = [i for i in normalized_items if i['kind'] == 'bookmark']
    releases = [i for i in normalized_items if i['kind'] != 'bookmark']
    lines = [
        f'# Weekly Model-News Digest ({week_label})',
        '',
        '> Policy: candidate capability updates must beat the baseline in the DLR evaluation harness (#61) before adoption. Never route on hype alone.',
        '',
        f'## X Bookmarks ({len(bookmarks)})',
    ]
    if bookmarks:
        for b in bookmarks:
            lines.append(f"- **{b['title']}** — {b['text']} ([source]({b['source']}))")
    else:
        lines.append('- None recorded this week.')

    lines += ['', f'## Release Notes ({len(releases)})']
    if releases:
        for r in releases:
            lines.append(f"- **{r['title']}** — {r['text']} ([source]({r['source']}))")
    else:
        lines.append('- None recorded this week.')

    lines += ['', f'## Candidate Capability Claims ({len(candidate_claims)})']
    if candidate_claims:
        for c in candidate_claims:
            lines.append(
                f"- `{c['worker']}` / `{c['task_class']}`: {c['claim']} "
                f"(source: `{c['source']}`, status: `{c['status']}`)"
            )
    else:
        lines.append('- No candidate capability changes.')
    return '\n'.join(lines) + '\n'


def write_weekly_digest(items, policy=None, vault_dir=None, now=None):
    """Normalize items, extract candidate claims, and write `<vault_dir>/research/<YYYY-Www>-model-news.md`."""
    now_dt = now or datetime.now(timezone.utc)
    iso_year, iso_week, _ = now_dt.isocalendar()
    week_label = f'{iso_year}-W{iso_week:02d}'
    normalized = [normalize_item(i) for i in (items or ())]
    claims = extract_candidate_claims(normalized, verified_at=now_dt.date().isoformat())
    markdown = render_digest_markdown(week_label, normalized, claims)

    root = Path(vault_dir) if vault_dir is not None else config.policy_path(policy or {}, 'run_job', 'vault_dir', 'vault')
    research_dir = root / 'research'
    research_dir.mkdir(parents=True, exist_ok=True)
    path = research_dir / f'{week_label}-model-news.md'
    path.write_text(security.redact(markdown))
    return {
        'week': week_label,
        'path': str(path),
        'items': len(normalized),
        'candidate_claims': claims,
    }
