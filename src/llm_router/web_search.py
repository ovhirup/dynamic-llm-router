"""Opt-in `web_search` tool for local workers and the Router Brain collector (#94).

Evaluated candidates:
  1. `smart-web-search` / `Searchlight` (no API key required; DuckDuckGo/SearXNG/local fetch):
     Selected as the default local provider (`provider: "searchlight"`), off by default (`enabled: False`).
  2. `mcp-web-search` (`tickernelz`, multi-provider MCP stdio): supported as an alternative MCP
     command provider when configured.
  3. `exa-mcp-server` (`exa-labs/exa-mcp-server`): requires a paid API key (`EXA_API_KEY`); kept
     optional via environment variable only, never storing keys in the repository.

Every fetched result is redacted through `security.redact` and persisted as an immutable record
in `<vault_root>/raw/<slug>-<date>.json` (`0400`) with `source_url` and `fetched_at` so it
satisfies the #93 verified-notes rule.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from . import security

CANDIDATE_EVALUATION = {
    'searchlight': {
        'requires_api_key': False,
        'selected_default': True,
        'summary': 'Keyless web search + markdown extraction; selected as default opt-in tool.',
    },
    'mcp-web-search': {
        'requires_api_key': False,
        'selected_default': False,
        'summary': 'Multi-provider MCP stdio server; supported when configured.',
    },
    'exa-mcp-server': {
        'requires_api_key': True,
        'selected_default': False,
        'summary': 'Hosted Exa search; optional only when user supplies EXA_API_KEY in env.',
    },
}


class WebSearchRefused(ValueError):
    """Web search is disabled (off by default) or invalid."""


def is_enabled(policy=None):
    return bool(((policy or {}).get('web_search') or {}).get('enabled', False))


def _slug(text, limit=32):
    cleaned = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return (cleaned[:limit].rstrip('-') or 'search')


def store_raw_result(vault_root, query, results, now=None):
    """Save fetched search pages to `<vault_root>/raw/<slug>-<YYYYMMDD>-<hash>.json` (`0400`) with URL + date."""
    stamp_dt = now or datetime.now(timezone.utc)
    date_str = stamp_dt.strftime('%Y%m%d')
    iso_str = stamp_dt.isoformat(timespec='seconds')
    normalized = []
    for item in results or ():
        if not isinstance(item, dict) or not item.get('url'):
            continue
        normalized.append({
            'title': security.redact(str(item.get('title') or item['url'])),
            'source_url': security.redact(str(item['url'])),
            'snippet': security.redact(str(item.get('snippet') or item.get('content') or '')[:2000]),
            'fetched_at': str(item.get('fetched_at') or iso_str),
        })
    payload = security.redact_obj({
        'query': str(query),
        'fetched_at': iso_str,
        'results': normalized,
    })
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:10]
    raw_dir = Path(vault_root) / 'raw'
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f'{_slug(query)}-{date_str}-{digest}.json'
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write('\n')
        os.chmod(path, 0o400)
    return {'raw_path': str(path), 'fetched_at': iso_str, 'results': normalized}


def search_and_collect(query, policy=None, vault_root=None, fetcher=None, now=None):
    """Run an opt-in `web_search` query, store results in `raw/` with source URL + date, and format citations."""
    if not is_enabled(policy):
        raise WebSearchRefused('web_search tool is disabled by default; set web_search.enabled=true to opt in')
    if not query or not str(query).strip():
        raise WebSearchRefused('search query must be non-empty')
    raw_results = fetcher(query) if fetcher is not None else []
    stored = store_raw_result(vault_root or Path('.'), query, raw_results, now=now)
    citations = [
        f"- [{r['title']}]({r['source_url']}) (fetched {r['fetched_at']}): {r['snippet']}"
        for r in stored['results']
    ]
    return {
        'query': str(query),
        'raw_path': stored['raw_path'],
        'fetched_at': stored['fetched_at'],
        'results': stored['results'],
        'cited_context': '\n'.join(['## Web Search Sources', *citations]) if citations else '',
    }
