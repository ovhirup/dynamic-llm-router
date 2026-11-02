"""MCP stdio client adapter for DLR plugins (#75).

Communicates with a local Model Context Protocol (MCP) server over stdio using newline-delimited
JSON-RPC 2.0 messages (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`,
`resources/list`, `resources/read`). Runs each invocation in a bounded child process (or injected
transport for unit tests), redacts all returned text through `security.redact`, and refuses
disabled or non-MCP plugins.
"""
import json
import subprocess

from . import security, task_dispatch as td

PROTOCOL_VERSION = '2024-11-05'


class McpError(RuntimeError):
    """MCP transport or JSON-RPC protocol error."""


def build_requests(method, params=None, request_id=2):
    """Build the newline-delimited JSON-RPC 2.0 handshake + request payload."""
    messages = [
        {
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {},
                'clientInfo': {'name': 'dynamic-llm-router', 'version': '0.4.0'},
            },
        },
        {
            'jsonrpc': '2.0',
            'method': 'notifications/initialized',
            'params': {},
        },
        {
            'jsonrpc': '2.0',
            'id': request_id,
            'method': method,
            'params': params or {},
        },
    ]
    return '\n'.join(json.dumps(msg, separators=(',', ':')) for msg in messages) + '\n'


def parse_response(stdout, request_id=2):
    """Extract the JSON-RPC response matching `request_id` from newline-delimited stdout."""
    for line in (stdout or '').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict) or message.get('id') != request_id:
            continue
        if 'error' in message and message['error'] is not None:
            err = message['error']
            msg = err.get('message') if isinstance(err, dict) else str(err)
            raise McpError(f'MCP error: {security.redact(str(msg))}')
        if 'result' in message:
            return security.redact_obj(message['result'])
    raise McpError('no matching JSON-RPC response from MCP server')


class McpClientAdapter:
    """Bounded stdio MCP client for an enabled plugin manifest."""

    def __init__(self, manifest, policy=None, runner=td.run_bounded, timeout=30):
        if not isinstance(manifest, dict) or not manifest.get('name'):
            raise McpError('invalid plugin manifest')
        if not manifest.get('enabled', False):
            raise McpError(f"plugin {manifest['name']!r} is disabled (opt-in required)")
        if manifest.get('transport', 'mcp_stdio') != 'mcp_stdio':
            raise McpError(f"plugin {manifest['name']!r} does not use mcp_stdio transport")
        command = manifest.get('command')
        if not isinstance(command, list) or not command:
            raise McpError(f"plugin {manifest['name']!r} has no command configured")
        self.manifest = dict(manifest)
        self.policy = policy
        self.runner = runner
        self.timeout = timeout

    def _call(self, method, params=None, cwd='.'):
        payload = build_requests(method, params=params, request_id=2)
        env = None
        if self.policy is not None:
            env, _ = td.worker_environment(self.policy)
        result = self.runner(
            self.manifest['command'],
            cwd,
            self.timeout,
            5,
            input_text=payload,
            env=env,
        )
        if result.get('timed_out'):
            raise McpError(f"MCP plugin {self.manifest['name']!r} timed out")
        if result.get('returncode', 0) != 0 and not (result.get('stdout') or '').strip():
            stderr = security.redact((result.get('stderr') or '').strip())[:300]
            raise McpError(f"MCP plugin {self.manifest['name']!r} exited {result.get('returncode')}: {stderr}")
        return parse_response(result.get('stdout', ''), request_id=2)

    def list_tools(self, cwd='.'):
        res = self._call('tools/list', {}, cwd=cwd)
        return res.get('tools', []) if isinstance(res, dict) else []

    def call_tool(self, name, arguments=None, cwd='.'):
        if not name or not isinstance(name, str):
            raise McpError('tool name must be a non-empty string')
        return self._call('tools/call', {'name': name, 'arguments': arguments or {}}, cwd=cwd)

    def list_resources(self, cwd='.'):
        res = self._call('resources/list', {}, cwd=cwd)
        return res.get('resources', []) if isinstance(res, dict) else []

    def read_resource(self, uri, cwd='.'):
        if not uri or not isinstance(uri, str):
            raise McpError('resource uri must be a non-empty string')
        return self._call('resources/read', {'uri': uri}, cwd=cwd)
