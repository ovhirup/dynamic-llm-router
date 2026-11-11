"""MCP contracts: real CLI paths, isolated state, fake workers only."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime

import pytest

from helpers import load_policy
from llm_router import cli, config, security, session_preferences
import test_job_dispatch as job_fixtures
import test_pr_gate as gate_fixtures
from test_pr_gate import JOB, NOW


@pytest.fixture
def server_type():
    from llm_router.mcp_server import Server
    return Server


@pytest.fixture
def harness(tmp_path, monkeypatch):
    case = job_fixtures.JobTests()
    case.setUp()
    monkeypatch.setenv('LLM_ROUTER_STATE_DIR', str(tmp_path / 'state'))
    case.policy['mcp'] = {'enable_run_job': True}
    case.policy['run_job']['vault_dir'] = str(case.base / 'vault')
    case.policy['run_job']['handoffs_dir'] = str(case.base / 'handoffs')
    case.policy['pr_gate']['approvals_file'] = str(case.base / 'approvals.jsonl')
    yield case
    case.tearDown()


def rpc(server, method, params=None, request_id=1):
    return server.handle({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}})


def call(server, name, **arguments):
    response = rpc(server, 'tools/call', {'name': name, 'arguments': arguments})
    assert 'error' not in response, response
    result = response['result']
    assert json.loads(result['content'][0]['text']) == result['structuredContent']
    validate(arguments, server.tools[name]['inputSchema'])
    validate(result['structuredContent'], server.tools[name]['outputSchema'])
    return result


def cli_result(policy, argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(argv, policy=policy)
    try:
        data = json.loads(output.getvalue())
    except json.JSONDecodeError:
        data = output.getvalue()
    return security.redact_obj({'exit_code': code, 'data': data})


def validate(value, schema):
    """Check the JSON-schema subset used by tool contracts, without test extras."""
    types = {'object': dict, 'array': list, 'string': str, 'integer': int, 'boolean': bool, 'null': type(None)}
    assert isinstance(value, types[schema['type']]), (value, schema)
    if schema['type'] == 'object':
        assert set(schema.get('required', ())) <= value.keys()
        if schema.get('additionalProperties') is False:
            assert value.keys() <= schema['properties'].keys()
        for key, item in value.items():
            if key in schema.get('properties', {}):
                validate(item, schema['properties'][key])


def test_discovery_requires_boolean_opt_in(server_type, harness):
    policy = harness.policy
    expected = {'select_worker', 'quota_status', 'list_jobs', 'job_status', 'get_review',
                'scan', 'check', 'list_pending_approvals'}
    for value in (None, False, 'true', 1, {}, []):
        policy['mcp'] = {} if value is None else {'enable_run_job': value}
        server = server_type(policy)
        tools = rpc(server, 'tools/list')['result']['tools']
        assert {t['name'] for t in tools} == expected
        assert 'error' in rpc(server, 'tools/call', {'name': 'run_job', 'arguments': {}})
    policy['mcp'] = {'enable_run_job': True}
    tools = rpc(server_type(policy), 'tools/list')['result']['tools']
    assert {t['name'] for t in tools} == expected | {'run_job'}
    for tool in tools:
        assert tool['inputSchema']['type'] == 'object'
        assert tool['inputSchema']['additionalProperties'] is False
        assert tool['outputSchema']['type'] == 'object'


@pytest.mark.parametrize('tool,args,argv', [
    ('select_worker', {'cached_only': True, 'size': 'draft'}, ['select', '--cached-only', '--size=draft']),
    ('quota_status', {}, ['status']),
    ('list_jobs', {'limit': 7}, ['jobs', '--limit=7']),
    ('job_status', {'job_id': 'sample'}, ['jobs', '--job=sample']),
    ('list_pending_approvals', {}, ['pending-approvals']),
    ('run_job', {'repo': '/missing', 'task_file': '/missing'},
     ['run-job', '--repo=/missing', '--task-file=/missing']),
])
def test_cli_contracts(server_type, harness, monkeypatch, tool, args, argv):
    from llm_router import selection
    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)
    monkeypatch.setattr(selection, 'datetime', FixedClock)
    # A raw job-log key must never escape via the jobs projection.
    jobs = harness.base / 'jobs'
    jobs.mkdir(exist_ok=True)
    (jobs / 'sample.json').write_text(json.dumps({'job_id': 'sample', 'status': 'failed',
        'branch': 'sk-' + 'q7W6e5R4t3Y2u1I0o9P8a7S6d5F4', 'worktree': '/missing'}))
    server = server_type(harness.policy)
    result = call(server, tool, **args)
    assert result['structuredContent'] == cli_result(harness.policy, argv)
    assert result['isError'] == (result['structuredContent']['exit_code'] != 0)
    definition = next(t for t in rpc(server, 'tools/list')['result']['tools'] if t['name'] == tool)
    validate(args, definition['inputSchema'])
    validate(result['structuredContent'], definition['outputSchema'])
    assert 'q7W6e5R4t3Y2u1I0o9P8a7S6d5F4' not in json.dumps(result)


def test_scan_contract_and_redaction(server_type, harness):
    target = harness.base / 'scan'
    target.mkdir()
    (target / 'note.txt').write_text('ordinary text\n')
    server = server_type(harness.policy)
    assert call(server, 'scan', path=str(target))['structuredContent'] == cli_result(
        harness.policy, ['scan', str(target)])
    (target / 'note.txt').write_text('sk-' + 'F3d4S5a6P7o8I9u0Y1t2R3e4W5q6')
    result = call(server, 'scan', path=str(target))
    assert result['isError']
    assert result['structuredContent']['data']['status'] == 'blocked'
    assert 'F3d4S5a6P7o8I9u0Y1t2R3e4W5q6' not in json.dumps(result)


def test_check_contract_runs_real_gate(server_type, harness):
    settings = harness.base / 'repo-settings.json'
    harness.policy['run_job']['repo_config'] = str(settings)
    settings.write_text(json.dumps({str(harness.repo.resolve()): {'test_cmd': 'true'}}))
    result = call(server_type(harness.policy), 'check', repo=str(harness.repo))
    expected = cli_result(harness.policy, ['check', '--repo=' + str(harness.repo), '--json'])
    # Check IDs and temporary paths are per invocation; compare the stable gate verdicts.
    assert result['structuredContent']['exit_code'] == expected['exit_code']
    assert result['structuredContent']['data']['ok'] == expected['data']['ok']
    assert result['structuredContent']['data']['tests']['status'] == 'passed'
    assert result['structuredContent']['data']['leak_check']['status'] == 'clean'


def test_review_contract_and_pending_never_change_approvals(server_type, tmp_path, monkeypatch):
    from llm_router import pr_gate
    case = gate_fixtures.GateTests()
    case.setUp()
    try:
        monkeypatch.setenv('LLM_ROUTER_STATE_DIR', str(tmp_path / 'state'))
        monkeypatch.setattr(pr_gate, 'now_local', lambda now=None, policy=None: NOW)
        approvals = pr_gate._approvals_file(case.policy)
        approvals.parent.mkdir(parents=True, exist_ok=True)
        approvals.write_text('')
        before = approvals.read_bytes()
        server = server_type(case.policy)
        review = call(server, 'get_review', job_id=JOB)['structuredContent']
        assert review == cli_result(case.policy, ['review', JOB])
        assert Path(review['data']['bundle'], 'tests.txt').is_file()
        pending = call(server, 'list_pending_approvals')['structuredContent']
        assert pending == cli_result(case.policy, ['pending-approvals'])
        assert pending['data']['pending'][0]['job_id'] == JOB
        assert pending['data']['pending'][0]['command'].startswith('llm-router approve ')
        assert approvals.read_bytes() == before
        # The existing review fixture plants a fake key in the test log.
        assert 'abcdefghijklmnopqrstuvwxyz123456' not in json.dumps(review)
        assert 'abcdefghijklmnopqrstuvwxyz123456' not in Path(review['data']['bundle'], 'tests.txt').read_text()
    finally:
        case.tearDown()


def test_no_approval_or_arbitrary_cli_escape(server_type, harness):
    approvals = Path(harness.policy['pr_gate']['approvals_file'])
    approvals.write_text('original approval store\n')
    server = server_type(harness.policy)
    for name in ('approve', 'push', 'merge', 'deny', 'undo', 'approve_request', 'cli', 'run-job'):
        assert 'error' in rpc(server, 'tools/call', {'name': name, 'arguments': {'job_id': 'sample'}})
    for name, args in [('list_jobs', {'argv': ['approve', 'sample']}),
                       ('scan', {'path': '.', 'approve': True}),
                       ('run_job', {'repo': '.', 'task_file': 'task', 'push': True}),
                       ('job_status', {'job_id': '../../approvals'}),
                       ('list_jobs', {'limit': True}), ('list_jobs', {'limit': -1}),
                       ('check', {'review': 'true'}), ('select_worker', {'size': 'anything'})]:
        assert 'error' in rpc(server, 'tools/call', {'name': name, 'arguments': args})
    assert approvals.read_text() == 'original approval store\n'


def test_completed_merge_is_not_pending(server_type, tmp_path, monkeypatch):
    from llm_router import pr_gate
    case = gate_fixtures.GateTests()
    case.setUp()
    try:
        monkeypatch.setenv('LLM_ROUTER_STATE_DIR', str(tmp_path / 'state'))
        pr_gate.save_gate(case.policy, JOB, {'pr': {'pushed_sha': case.head}, 'merged': {'sha': case.head}})
        approvals = pr_gate._approvals_file(case.policy)
        approvals.write_text(json.dumps({'type': 'used', 'job': JOB, 'action': 'merge', 'sha': case.head}) + '\n')
        before = approvals.read_bytes()
        result = call(server_type(case.policy), 'list_pending_approvals')['structuredContent']
        assert result['data']['pending'] == []
        assert approvals.read_bytes() == before
    finally:
        case.tearDown()


def test_calls_are_audited_with_client_and_tool(server_type, harness):
    server = server_type(harness.policy)
    rpc(server, 'initialize', {'protocolVersion': '2025-06-18',
        'clientInfo': {'name': 'contract-client', 'version': '1'}})
    rpc(server, 'notifications/initialized', request_id=None)
    rpc(server, 'tools/list')
    call(server, 'list_jobs')
    rpc(server, 'tools/call', {'name': 'approve'})
    records = [json.loads(line) for line in session_preferences._audit_path(harness.policy).read_text().splitlines()]
    assert [(r['method'], r['tool'], r['client']) for r in records] == [
        ('initialize', '', 'contract-client'), ('notifications/initialized', '', 'contract-client'),
        ('tools/list', '', 'contract-client'), ('tools/call', 'list_jobs', 'contract-client'),
        ('tools/call', 'approve', 'contract-client')]
    assert all(r['event'] == 'mcp_call' for r in records)


def test_stdio_framing_errors_notifications_and_clean_stdout(server_type, harness):
    server = server_type(harness.policy)
    source = io.StringIO('bad json\n[]\n' + json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n' +
        json.dumps({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                    'params': {'name': 'quota_status', 'arguments': {}}}) + '\n')
    output = io.StringIO()
    server.serve_stdio(source, output)
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(messages) == 3
    assert messages[0]['error']['code'] == -32700
    assert messages[1]['error']['code'] == -32600
    assert messages[2]['id'] == 5
    assert messages[2]['result']['structuredContent']['exit_code'] == 0


@pytest.mark.parametrize('worker,meter,reason', [('claude', 10, 'never'),
    ('codex', None, 'unknown'), ('codex', 90, 'refusing new full-size')])
def test_run_job_safety_matches_cli(server_type, harness, monkeypatch, worker, meter, reason):
    from llm_router import selection
    monkeypatch.setattr(selection, 'select', lambda *a, **kw: {
        'eligible': [], 'meters': {'codex': meter}, 'reason': 'test'})
    monkeypatch.setattr(selection, 'read_meter', lambda policy, name, **kw: {
        'eligible': [], 'meters': {name: meter},
        'reason': 'quota_unknown' if meter is None else 'quota_exhausted_or_over_threshold',
        'source': 'cache'})
    harness.policy['never'] = ['claude']
    args = {'repo': str(harness.repo), 'task_file': str(harness.task), 'worker': worker,
            'override_reason': 'contract test', 'test_cmd': 'true'}
    result = call(server_type(harness.policy), 'run_job', **args)['structuredContent']
    expected = cli_result(harness.policy, ['run-job', '--repo=' + args['repo'], '--task-file=' + args['task_file'],
        '--worker=' + worker, '--override-reason=contract test', '--test-cmd=true'])
    assert result['exit_code'] == expected['exit_code']
    # Jobs have different IDs, paths and timestamps; the safety verdict must match.
    for key in ('ok', 'status', 'refused', 'error', 'tests_status', 'workers'):
        assert result['data'].get(key) == expected['data'].get(key)
    assert result['exit_code'] != 0
    assert reason in json.dumps(result).lower()
    assert not (harness.repo / 'out.txt').exists()


def test_run_job_fake_worker_commits_locally(server_type, harness, monkeypatch):
    from llm_router import selection
    monkeypatch.setattr(selection, 'select', harness.select)
    harness.worker("open('out.txt','w').write('hi\\n')")
    result = call(server_type(harness.policy), 'run_job', repo=str(harness.repo),
        task_file=str(harness.task), test_cmd='test -f out.txt')['structuredContent']
    assert result['exit_code'] == 0, result
    data = result['data']
    assert data['status'] == 'committed'
    assert data['tests_status'] == 'passed'
    assert data['leak_check']['status'] == 'clean'
    assert not (harness.repo / 'out.txt').exists()
    assert data['main_checkout_untouched']


def test_mcp_cli_entry_and_alias(tmp_path):
    policy = tmp_path / 'policy.json'
    policy.write_text('{}')
    env = dict(os.environ, PYTHONPATH='src:tests', LLM_ROUTER_CONFIG=str(policy),
               LLM_ROUTER_CONFIG_DIR=str(tmp_path / 'config'), LLM_ROUTER_STATE_DIR=str(tmp_path / 'state'))
    request = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}) + '\n'
    done = subprocess.run([sys.executable, '-m', 'llm_router', 'mcp'], input=request,
                          capture_output=True, text=True, env=env, timeout=15)
    assert done.returncode == 0, done.stderr
    assert len(json.loads(done.stdout)['result']['tools']) == 8
    assert done.stderr == ''


@pytest.mark.parametrize('value', ['true', 1, None, [], {}])
def test_config_rejects_non_boolean_write_flag(value):
    with pytest.raises(config.ConfigError, match='mcp'):
        config.validate(load_policy(mcp={'enable_run_job': value}))
