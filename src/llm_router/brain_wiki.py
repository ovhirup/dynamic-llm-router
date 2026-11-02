"""Router Brain wiki (#93): collect / update / audit wiki with verified notes.

Vault layout:
  - `raw/`  (immutable job logs, handoffs, research links — written once, never edited)
  - `wiki/` (maintained notes; every note carries `claim`, `source`, `verified_at`, `verified`)

Three roles (steps in code, not new bots):
  1. `collect(vault_root, raw_id, payload)`: write an immutable redacted record into `raw/<raw_id>.json`
     (refuses to overwrite an existing `raw/` file).
  2. `update_wiki_note(vault_root, topic, claim, source, verified_at, policy=None, notifier=None)`:
     write or update `wiki/<topic>.md`. If an existing verified note has a different `claim`, raises an
     alert (`alerts.raise_alert`) and records the conflict instead of silently overwriting.
  3. `audit_wiki(vault_root, now=None, max_age_days=30)`: scan `wiki/*.md` for stale notes (> 30 days
     since `verified_at`), conflicts, and orphan notes (missing `source` or citing a missing `raw/` file),
     marking stale/unsourced notes `verified: false`.
  4. `read_verified_notes(vault_root, now=None, max_age_days=30)`: return only verified, non-stale,
     conflict-free notes for routing consumption.
"""
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re

from . import alerts, security

STALE_DAYS = 30
SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,120}')


def _parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or '').strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace('Z', '+00:00')).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def collect(vault_root, raw_id, payload):
    """Write an immutable redacted JSON record into `<vault_root>/raw/<raw_id>.json`."""
    if not SAFE_ID.fullmatch(str(raw_id or '')):
        raise ValueError(f'invalid raw_id {raw_id!r}')
    raw_dir = Path(vault_root) / 'raw'
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f'{raw_id}.json'
    if path.exists():
        raise FileExistsError(f'raw record {raw_id!r} already exists and is immutable')
    clean = security.redact_obj(payload)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(clean, handle, indent=2, sort_keys=True)
        handle.write('\n')
    os.chmod(path, 0o400)  # read-only once collected
    return path


def _render_note(meta, body=''):
    lines = [
        '---',
        f'topic: {json.dumps(meta["topic"])}',
        f'claim: {json.dumps(meta["claim"])}',
        f'source: {json.dumps(meta["source"])}',
        f'verified_at: {json.dumps(meta["verified_at"])}',
        f'verified: {"true" if meta.get("verified", True) else "false"}',
    ]
    if meta.get('conflict'):
        lines.append(f'conflict: {json.dumps(meta["conflict"])}')
    lines += ['---', '', body.strip() or f'# {meta["topic"]}\n\n- Claim: {meta["claim"]}\n- Source: {meta["source"]}\n']
    return '\n'.join(lines) + '\n'


def parse_note(text):
    """Parse YAML-style frontmatter and body from a wiki markdown note."""
    if not (text or '').startswith('---\n'):
        return {'meta': {}, 'body': text or ''}
    parts = text.split('\n---\n', 1)
    if len(parts) != 2:
        return {'meta': {}, 'body': text}
    header, body = parts[0][4:], parts[1]
    meta = {}
    for line in header.splitlines():
        if ':' not in line:
            continue
        k, v = line.split(':', 1)
        k, v = k.strip(), v.strip()
        if v in ('true', 'false'):
            meta[k] = (v == 'true')
        else:
            try:
                meta[k] = json.loads(v)
            except json.JSONDecodeError:
                meta[k] = v.strip('"\'')
    return {'meta': meta, 'body': body}


def update_wiki_note(vault_root, topic, claim, source, verified_at=None, body='',
                     policy=None, notifier=None, now=None):
    """Create or update `<vault_root>/wiki/<topic>.md`; alert and preserve existing claim on conflict."""
    if not SAFE_ID.fullmatch(str(topic or '')):
        raise ValueError(f'invalid wiki topic {topic!r}')
    claim = security.redact(str(claim or '').strip())
    source = security.redact(str(source or '').strip())
    if not claim or not source:
        raise ValueError('wiki note requires non-empty claim and source')
    today = _parse_date(verified_at) or (_parse_date(now) or datetime.now(timezone.utc).date())
    wiki_dir = Path(vault_root) / 'wiki'
    wiki_dir.mkdir(parents=True, exist_ok=True)
    path = wiki_dir / f'{topic}.md'
    if path.is_file():
        existing = parse_note(path.read_text(errors='replace'))['meta']
        old_claim = existing.get('claim')
        if old_claim and old_claim != claim and existing.get('verified', True):
            conflict_msg = f'conflicting claim: {claim!r} (source: {source}) vs existing {old_claim!r}'
            existing['conflict'] = conflict_msg
            path.write_text(_render_note(existing))
            if policy is not None:
                alerts.raise_alert(
                    policy,
                    f'brain:conflict:{topic}',
                    'brain_wiki',
                    f'Router Brain wiki conflict on {topic}: {conflict_msg}',
                    level='warn',
                    notifier=notifier,
                )
            return {'status': 'conflict', 'path': str(path), 'meta': existing}
    meta = {
        'topic': str(topic),
        'claim': claim,
        'source': source,
        'verified_at': today.isoformat(),
        'verified': True,
    }
    path.write_text(_render_note(meta, body=body))
    return {'status': 'updated', 'path': str(path), 'meta': meta}


def audit_wiki(vault_root, now=None, max_age_days=STALE_DAYS):
    """Audit `<vault_root>/wiki/*.md` for stale (> max_age_days), conflicting, and orphan notes."""
    root = Path(vault_root)
    wiki_dir = root / 'wiki'
    raw_dir = root / 'raw'
    today = _parse_date(now) or datetime.now(timezone.utc).date()
    stale, conflicts, orphans, verified = [], [], [], []
    if not wiki_dir.is_dir():
        return {'verified': [], 'stale': [], 'conflicts': [], 'orphans': []}
    for path in sorted(wiki_dir.glob('*.md')):
        if path.name == 'README.md':
            continue
        parsed = parse_note(path.read_text(errors='replace'))
        meta, body = parsed['meta'], parsed['body']
        topic = meta.get('topic') or path.stem
        source = str(meta.get('source') or '').strip()
        vdate = _parse_date(meta.get('verified_at'))
        is_orphan = (not source) or (
            source.startswith('raw/') and not (root / source).is_file() and not (raw_dir / Path(source).name).is_file()
        )
        is_stale = (vdate is None) or ((today - vdate) > timedelta(days=int(max_age_days)))
        has_conflict = bool(meta.get('conflict'))
        if is_orphan:
            orphans.append(topic)
        if is_stale:
            stale.append(topic)
        if has_conflict:
            conflicts.append(topic)
        if is_orphan or is_stale:
            if meta.get('verified', True):
                meta['topic'] = topic
                meta.setdefault('claim', '')
                meta.setdefault('source', source)
                meta.setdefault('verified_at', vdate.isoformat() if vdate else '')
                meta['verified'] = False
                path.write_text(_render_note(meta, body=body))
        elif meta.get('verified', False) and not has_conflict:
            verified.append(topic)
    return {
        'verified': verified,
        'stale': stale,
        'conflicts': conflicts,
        'orphans': orphans,
    }


def read_verified_notes(vault_root, now=None, max_age_days=STALE_DAYS):
    """Run an audit pass and return only verified, non-stale, conflict-free wiki notes for routing."""
    audit_wiki(vault_root, now=now, max_age_days=max_age_days)
    wiki_dir = Path(vault_root) / 'wiki'
    if not wiki_dir.is_dir():
        return []
    out = []
    for path in sorted(wiki_dir.glob('*.md')):
        if path.name == 'README.md':
            continue
        meta = parse_note(path.read_text(errors='replace'))['meta']
        if meta.get('verified') is True and not meta.get('conflict') and meta.get('claim') and meta.get('source'):
            out.append(meta)
    return out
