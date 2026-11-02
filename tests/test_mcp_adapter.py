"""Tests for MCP client adapter for plugins (#75)."""
import json
import unittest

from llm_router import mcp_adapter


def fake_runner(responses):
    """Return a runner(argv, cwd, timeout, grace, input_text=..., env=...) returning canned JSON-RPC lines."""
    seen = []

    def run(argv, cwd, timeout, grace=5, input_text=None, env=None, **kw):
        seen.append({'argv': argv, 'cwd': cwd, 'input_text': input_text})
        if callable(responses):
            return responses(argv, input_text)
        lines = '\n'.join(json.dumps(r) for r in responses) + '\n'
        return {'returncode': 0, 'timed_out': False, 'stdout': lines, 'stderr': '', 'elapsed_seconds': 0.1}

    run.seen = seen
    return run


MANIFEST = {
    'name': 'hark',
    'capabilities': ['input_source', 'context'],
    'transport': 'mcp_stdio',
    'command': ['hark-mcp'],
    'enabled': True,
}


class McpClientAdapterTests(unittest.TestCase):
    def test_disabled_or_non_mcp_plugin_is_refused(self):
        with self.assertRaisesRegex(mcp_adapter.McpError, 'disabled'):
            mcp_adapter.McpClientAdapter(dict(MANIFEST, enabled=False))
        with self.assertRaisesRegex(mcp_adapter.McpError, 'mcp_stdio'):
            mcp_adapter.McpClientAdapter(dict(MANIFEST, transport='command'))

    def test_list_tools_and_call_tool_redact_secrets(self):
        secret = 'sk-' + 'b' * 30
        runner = fake_runner([
            {'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': '2024-11-05'}},
            {'jsonrpc': '2.0', 'id': 2, 'result': {
                'content': [{'type': 'text', 'text': f'transcript with {secret}'}],
            }},
        ])
        client = mcp_adapter.McpClientAdapter(MANIFEST, runner=runner)
        result = client.call_tool('search_transcripts', {'query': 'routing'})
        sent = [json.loads(line) for line in runner.seen[0]['input_text'].splitlines()]
        self.assertEqual([m['method'] for m in sent],
                         ['initialize', 'notifications/initialized', 'tools/call'])
        self.assertEqual(sent[2]['params'], {'name': 'search_transcripts', 'arguments': {'query': 'routing'}})
        text = result['content'][0]['text']
        self.assertNotIn(secret, text)

    def test_rpc_error_and_timeout_raise_mcp_error(self):
        err_runner = fake_runner([
            {'jsonrpc': '2.0', 'id': 2, 'error': {'code': -32601, 'message': 'Method not found'}},
        ])
        client = mcp_adapter.McpClientAdapter(MANIFEST, runner=err_runner)
        with self.assertRaisesRegex(mcp_adapter.McpError, 'Method not found'):
            client.list_tools()

        timeout_runner = fake_runner(lambda *a: {'returncode': -15, 'timed_out': True, 'stdout': '', 'stderr': ''})
        timed_client = mcp_adapter.McpClientAdapter(MANIFEST, runner=timeout_runner)
        with self.assertRaisesRegex(mcp_adapter.McpError, 'timed out'):
            timed_client.list_resources()


if __name__ == '__main__':
    unittest.main()
