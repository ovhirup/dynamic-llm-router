"""Hark reference plugin (#76): convert local dictation/meeting transcripts into DLR task files.

Hark (`com.harklabs.hark.mac`) is a local dictation and meeting transcription tool, never a coding
LLM worker. It plugs into DLR as an opt-in `input_source` and `context` plugin over `hark-mcp`.
"""
import os
from pathlib import Path
import re

from . import security

MANIFEST = {
    'name': 'hark',
    'capabilities': ['input_source', 'context'],
    'transport': 'mcp_stdio',
    'command': ['hark-mcp'],
    'description': 'Hark local dictation and meeting transcription input source (never a coding worker).',
    'enabled': False,
    'permissions': ['read_transcripts'],
}


def _slugify(text, limit=40):
    slug = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return (slug[:limit].rstrip('-') or 'hark-task')


def _redact(text, private_terms=None):
    out = security.redact(str(text))
    for term in private_terms or ():
        if term:
            out = re.sub(re.escape(str(term)), security.REDACTED, out, flags=re.I)
    return out


def transcript_to_task(record, allowed_paths=None, private_terms=None):
    """Render a Hark dictation/meeting transcript dict into a redacted DLR markdown task string."""
    if not isinstance(record, dict):
        raise ValueError('transcript record must be a dict')
    raw_text = (record.get('transcript') or record.get('text') or '').strip()
    if not raw_text:
        raise ValueError('transcript record has no text')
    title = (record.get('title') or raw_text.splitlines()[0][:60]).strip('# ').strip() or 'Hark task'
    title = _redact(title, private_terms)
    body = _redact(raw_text, private_terms)
    source_id = _redact(str(record.get('source_id') or record.get('id') or 'local-dictation'), private_terms)
    recorded_at = _redact(str(record.get('recorded_at') or 'unspecified'), private_terms)
    actions = [_redact(str(item).strip(), private_terms)
               for item in (record.get('action_items') or []) if str(item).strip()]

    lines = [
        f'# {title}',
        '',
        '## Source',
        f'- Plugin: `hark` (`hark-mcp`, input_source)',
        f'- Transcript ID: `{source_id}`',
        f'- Recorded at: `{recorded_at}`',
        '',
        '## Goal',
        body,
    ]
    if actions:
        lines += ['', '## Action items'] + [f'- {item}' for item in actions]
    if allowed_paths:
        lines += ['', '## Allowed paths'] + [f'- {p}' for p in allowed_paths]
    return '\n'.join(lines) + '\n'


def write_task_from_transcript(record, out_dir, allowed_paths=None, private_terms=None):
    """Write a 0600 markdown task file into `out_dir` from a Hark transcript record; returns Path."""
    text = transcript_to_task(record, allowed_paths=allowed_paths, private_terms=private_terms)
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    slug = _slugify(record.get('title') or (record.get('transcript') or record.get('text') or '')[:40])
    source_id = _slugify(str(record.get('source_id') or record.get('id') or ''), limit=16)
    filename = f'{slug}-{source_id}.md' if source_id and source_id != 'hark-task' else f'{slug}.md'
    path = directory / filename
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(text)
    os.chmod(path, 0o600)
    return path
