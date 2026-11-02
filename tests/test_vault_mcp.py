"""Tests for exposing the Obsidian vault to workers via MCP (#50)."""
import io
import json
from pathlib import Path
import tempfile
import unittest

from llm_router import vault_mcp


class VaultMcpServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / 'vault'
        (self.vault / 'jobs').mkdir(parents=True)
        secret = 'sk-' + 'p' * 30
        (self.vault / 'rules.md').write_text(f'- Always run tests\n- Redact {secret}\n')
        (self.vault / 'jobs' / 'job-1.md').write_text(f'# Job 1\nOutcome: passed with {secret}\n')

    def test_stdio_server_handles_initialize_tools_and_resources_and_redacts_secrets(self):
        secret = 'sk-' + 'p' * 30
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'read_rules', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
             'params': {'name': 'search_vault', 'arguments': {'query': 'Outcome'}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'resources/read', 'params': {'uri': 'vault://jobs/job-1.md'}},
        ]
        instream = io.StringIO('\n'.join(json.dumps(r) for r in requests) + '\n')
        outstream = io.StringIO()
        vault_mcp.serve_stdio(self.vault, instream=instream, outstream=outstream)
        responses = {json.loads(line)['id']: json.loads(line) for line in outstream.getvalue().splitlines()}

        self.assertEqual(responses[1]['result']['serverInfo']['name'], 'dlr-vault-mcp')
        tool_names = [t['name'] for t in responses[2]['result']['tools']]
        self.assertEqual(tool_names, ['read_rules', 'search_vault', 'read_note'])

        rules_text = responses[3]['result']['content'][0]['text']
        self.assertIn('Always run tests', rules_text)
        self.assertNotIn(secret, rules_text)

        hits = json.loads(responses[4]['result']['content'][0]['text'])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['path'], 'jobs/job-1.md')
        self.assertNotIn(secret, hits[0]['excerpt'])

        note_text = responses[5]['result']['contents'][0]['text']
        self.assertIn('# Job 1', note_text)
        self.assertNotIn(secret, note_text)

    def test_rejects_path_traversal_and_provides_worker_mcp_config(self):
        resp = vault_mcp.handle_rpc_request(
            self.vault,
            {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
             'params': {'name': 'read_note', 'arguments': {'path': '../outside.md'}}},
        )
        self.assertIn('error', resp)
        cfg = vault_mcp.worker_mcp_config(vault_root=self.vault)
        self.assertIn('dlr-vault', cfg['mcpServers'])


if __name__ == '__main__':
    unittest.main()
