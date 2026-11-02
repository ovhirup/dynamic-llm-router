"""Expose the Router Brain Obsidian vault to workers via a read-only MCP stdio server (#50).

Parent: #46 (Router Brain).
Provides a lightweight, zero-dependency JSON-RPC 2.0 MCP server (`python -m llm_router.vault_mcp --vault <dir>`
or `llm-router vault-mcp --vault <dir>`) that exposes the Obsidian vault (`rules.md`, `jobs/`, `handoffs/`,
`scorecards/`, `research/`) to workers in read-only mode:
  - `initialize`
  - `tools/list` (`read_rules`, `search_vault`, `read_note`)
  - `tools/call`
  - `resources/list`
  - `resources/read` (`vault://<rel_path>`)
All paths are strictly confined to `vault_dir` and all returned content is passed through `security.redact`.
"""
import argparse
import json
from pathlib import Path
import sys

from . import config, security

PROTOCOL_VERSION = '2024-11-05'
ALLOWED_TOP = {'rules.md', 'README.md', 'jobs', 'handoffs', 'scorecards', 'research', 'wiki'}

TOOLS = [
    {
        'name': 'read_rules',
        'description': 'Read the standing Router Brain rules (rules.md).',
        'inputSchema': {'type': 'object', 'properties': {}},
    },
    {
        'name': 'search_vault',
        'description': 'Search markdown notes in the Router Brain Obsidian vault by keyword.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string'},
                'limit': {'type': 'integer', 'default': 5},
            },
            'required': ['query'],
        },
    },
    {
        'name': 'read_note',
        'description': 'Read one markdown note inside the Router Brain vault by relative path.',
        'inputSchema': {
            'type': 'object',
            'properties': {'path': {'type': 'string'}},
            'required': ['path'],
        },
    },
]


def _resolve_safe_note(vault_root, rel_path):
    root = Path(vault_root).expanduser().resolve()
    rel = str(rel_path or '').strip().removeprefix('vault://').lstrip('/')
    if not rel or '..' in Path(rel).parts:
        raise ValueError(f'invalid vault path {rel_path!r}')
    top = Path(rel).parts[0]
    if top not in ALLOWED_TOP:
        raise ValueError(f'vault path {rel!r} is outside allowed vault folders')
    target = (root / rel).resolve()
    if not target.is_relative_to(root) or target.is_symlink() or not target.is_file():
        raise ValueError(f'vault note {rel!r} not found')
    return target


def list_vault_notes(vault_root):
    root = Path(vault_root).expanduser().resolve()
    if not root.is_dir():
        return []
    resources = []
    for path in sorted(root.rglob('*.md')):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts[0] not in ALLOWED_TOP:
            continue
        rel_str = rel.as_posix()
        resources.append({
            'uri': f'vault://{rel_str}',
            'name': rel_str,
            'mimeType': 'text/markdown',
        })
    return resources


def search_vault(vault_root, query, limit=5):
    root = Path(vault_root).expanduser().resolve()
    needle = str(query or '').strip().lower()
    if not needle or not root.is_dir():
        return []
    hits = []
    for res in list_vault_notes(root):
        rel = res['name']
        path = root / rel
        text = path.read_text(errors='replace')
        if needle in text.lower() or needle in rel.lower():
            hits.append({
                'path': rel,
                'uri': res['uri'],
                'excerpt': security.redact(text[:400]),
            })
            if len(hits) >= max(1, int(limit or 5)):
                break
    return hits


def handle_rpc_request(vault_root, request):
    """Handle one JSON-RPC 2.0 request dict; returns a response dict (or None for notifications)."""
    if not isinstance(request, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    method = request.get('method')
    req_id = request.get('id')
    params = request.get('params') or {}
    if req_id is None and method == 'notifications/initialized':
        return None
    try:
        if method == 'initialize':
            result = {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {'tools': {}, 'resources': {}},
                'serverInfo': {'name': 'dlr-vault-mcp', 'version': '0.4.0'},
            }
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'resources/list':
            result = {'resources': list_vault_notes(vault_root)}
        elif method == 'resources/read':
            target = _resolve_safe_note(vault_root, params.get('uri'))
            text = security.redact(target.read_text(errors='replace'))
            result = {'contents': [{'uri': params.get('uri'), 'mimeType': 'text/markdown', 'text': text}]}
        elif method == 'tools/call':
            tool_name = params.get('name')
            args = params.get('arguments') or {}
            if tool_name == 'read_rules':
                rules_path = Path(vault_root).expanduser().resolve() / 'rules.md'
                text = security.redact(rules_path.read_text(errors='replace')) if rules_path.is_file() else ''
                result = {'content': [{'type': 'text', 'text': text}]}
            elif tool_name == 'search_vault':
                hits = search_vault(vault_root, args.get('query', ''), limit=args.get('limit', 5))
                result = {'content': [{'type': 'text', 'text': json.dumps(hits)}]}
            elif tool_name == 'read_note':
                target = _resolve_safe_note(vault_root, args.get('path'))
                text = security.redact(target.read_text(errors='replace'))
                result = {'content': [{'type': 'text', 'text': text}]}
            else:
                return {'jsonrpc': '2.0', 'id': req_id,
                        'error': {'code': -32601, 'message': f'Unknown tool: {tool_name}'}}
        else:
            return {'jsonrpc': '2.0', 'id': req_id,
                    'error': {'code': -32601, 'message': f'Method not found: {method}'}}
        return {'jsonrpc': '2.0', 'id': req_id, 'result': security.redact_obj(result)}
    except Exception as exc:
        return {'jsonrpc': '2.0', 'id': req_id,
                'error': {'code': -32000, 'message': security.redact(str(exc))}}


def serve_stdio(vault_root, instream=None, outstream=None):
    """Run the stdio JSON-RPC loop over `instream` -> `outstream`."""
    inp = instream if instream is not None else sys.stdin
    out = outstream if outstream is not None else sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        else:
            resp = handle_rpc_request(vault_root, req)
        if resp is not None:
            out.write(json.dumps(resp) + '\n')
            out.flush()


def worker_mcp_config(policy=None, vault_root=None):
    """Return the MCP server configuration dict workers can attach to read the vault."""
    root = str(Path(vault_root).expanduser().resolve()) if vault_root is not None else \
        str(config.policy_path(policy or {}, 'run_job', 'vault_dir', 'vault'))
    return {
        'mcpServers': {
            'dlr-vault': {
                'command': sys.executable,
                'args': ['-m', 'llm_router.vault_mcp', '--vault', root],
            }
        }
    }


def main(argv=None):
    parser = argparse.ArgumentParser(prog='llm_router.vault_mcp')
    parser.add_argument('--vault', required=True, help='path to the Obsidian vault directory')
    args = parser.parse_args(argv)
    serve_stdio(args.vault)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
