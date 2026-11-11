"""Local, serial, zero-dependency MCP stdio adapter for the existing CLI (#240).

Like vault_mcp, messages are newline-delimited JSON-RPC. A fixed command allowlist
and validated arguments are the entire adapter: routing and gates stay in the CLI.
"""
import contextlib
from copy import deepcopy
import io
import json
import re
import sys

from . import __version__, config, security, session_preferences

PROTOCOL_VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
STRING = {'type': 'string', 'minLength': 1}
BOOLEAN = {'type': 'boolean'}
JOB_ID = {'type': 'string', 'pattern': r'^[A-Za-z0-9._-]+$', 'minLength': 1}
SIZE = {'type': 'string', 'enum': ['draft', 'full']}
ROLE = {'type': 'string', 'enum': ['planner', 'coder', 'searcher', 'judge', 'advisor']}
LIMIT = {'type': 'integer', 'minimum': 1}


def _tool(name, command, properties, required=(), description='', text=False):
    return {'name': name, 'description': description or f'Same as llm-router {command}.',
            'inputSchema': {'type': 'object', 'properties': properties,
                            'required': list(required), 'additionalProperties': False},
            'outputSchema': {'type': 'object', 'properties': {
                'exit_code': {'type': 'integer'}, 'data': {'type': 'string' if text else 'object'}},
                'required': ['exit_code', 'data'], 'additionalProperties': False},
            'annotations': {'readOnlyHint': name != 'run_job', 'openWorldHint': False}}


TOOLS = [
    _tool('select_worker', 'select', {'size': SIZE, 'cached_only': BOOLEAN,
          'task_class': {'type': 'string', 'enum': list(config.TASK_CLASSES)}, 'role': ROLE}),
    _tool('quota_status', 'status', {}, text=True),
    _tool('list_jobs', 'jobs', {'limit': LIMIT, 'live_feed': BOOLEAN}),
    _tool('job_status', 'jobs --job', {'job_id': JOB_ID}, ('job_id',)),
    _tool('get_review', 'review', {'job_id': JOB_ID}, ('job_id',),
          'Generate the CLI review evidence bundle and cache; configured screenshots may run.'),
    _tool('scan', 'scan', {'path': STRING, 'history': BOOLEAN}, ('path',)),
    _tool('check', 'check --json', {'repo': STRING, 'base': STRING, 'review': BOOLEAN},
          description='Run the CLI check gate (tests on a disposable copy; review is opt-in).'),
    _tool('list_pending_approvals', 'pending-approvals', {}),
    _tool('run_job', 'run-job', {'repo': STRING, 'task_file': STRING, 'base': STRING,
          'branch': STRING, 'test_cmd': STRING, 'size': SIZE, 'worker': STRING,
          'task_class': {'type': 'string', 'enum': list(config.TASK_CLASSES)}, 'role': ROLE,
          'orchestrator': {'type': 'string', 'enum': list(session_preferences.ORCHESTRATORS)},
          'override_reason': STRING, 'keep': BOOLEAN, 'prune': BOOLEAN}, ('repo', 'task_file')),
]
COMMANDS = {'select_worker': 'select', 'quota_status': 'status', 'list_jobs': 'jobs',
            'job_status': 'jobs', 'get_review': 'review', 'scan': 'scan', 'check': 'check',
            'list_pending_approvals': 'pending-approvals', 'run_job': 'run-job'}


def _validate(arguments, schema):
    if not isinstance(arguments, dict):
        raise ValueError('tool arguments must be an object')
    properties = schema['properties']
    if arguments.keys() - properties.keys():
        raise ValueError('unknown tool arguments')
    if set(schema['required']) - arguments.keys():
        raise ValueError('missing required tool arguments')
    types = {'string': str, 'boolean': bool, 'integer': int}
    for name, value in arguments.items():
        spec = properties[name]
        if type(value) is not types[spec['type']]:
            raise ValueError(f'{name} must be {spec["type"]}')
        if 'enum' in spec and value not in spec['enum']:
            raise ValueError(f'invalid {name}')
        if isinstance(value, str) and (not value or '\0' in value):
            raise ValueError(f'invalid {name}')
        if 'pattern' in spec and not re.fullmatch(spec['pattern'], value):
            raise ValueError(f'invalid {name}')
        if 'minimum' in spec and value < spec['minimum']:
            raise ValueError(f'{name} must be at least {spec["minimum"]}')
    if arguments.get('keep') and arguments.get('prune'):
        raise ValueError('keep and prune are mutually exclusive')


def _argv(name, arguments):
    argv = [COMMANDS[name]]
    positional = []
    for key, value in arguments.items():
        if key == 'job_id':
            if name == 'job_status':
                argv.append('--job=' + value)
            else:
                positional.append(value)
        elif key == 'path':
            positional.append(value)
        else:
            flag = '--' + ('class' if key == 'task_class' else key.replace('_', '-'))
            if isinstance(value, bool):
                if value:
                    argv.append(flag)
            else:
                argv.append(flag + '=' + str(value))
    if name == 'check':
        argv.append('--json')
    if positional:
        argv.extend(['--', *positional])
    return argv


class Server:
    def __init__(self, policy):
        self.policy = policy
        self.client = 'unknown'
        self.tools = {tool['name']: deepcopy(tool) for tool in TOOLS
                      if tool['name'] != 'run_job' or
                      (isinstance(policy.get('mcp'), dict) and
                       policy['mcp'].get('enable_run_job') is True)}

    def _audit(self, method, tool=''):
        session_preferences.audit_event(self.policy, {'event': 'mcp_call', 'method': method,
                                                       'tool': tool, 'client': self.client})

    def _call(self, name, arguments):
        from . import cli
        _validate(arguments, self.tools[name]['inputSchema'])
        stdout, stderr = io.StringIO(), io.StringIO()
        # No client-controlled argv and no shell. CLI subprocesses already capture their output.
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(_argv(name, arguments), policy=self.policy)
        raw = stdout.getvalue()
        if name == 'quota_status':
            data = raw
        else:
            data = json.loads(raw)
        result = security.redact_obj({'exit_code': code, 'data': data})
        return {'content': [{'type': 'text', 'text': json.dumps(result)}],
                'structuredContent': result, 'isError': code != 0}

    def handle(self, request):
        req_id = None
        notification = False
        try:
            if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                    or not isinstance(request.get('method'), str)
                    or ('id' in request and type(request['id']) not in (str, int, type(None)))):
                self._audit('invalid_request')
                return self._error(None, -32600, 'Invalid Request')
            req_id = request.get('id')
            notification = req_id is None
            method = request['method']
            params = request.get('params', {})
            if not isinstance(params, dict):
                self._audit(method)
                raise ValueError('params must be an object')
            if method == 'initialize':
                info = params.get('clientInfo', {})
                if isinstance(info, dict) and isinstance(info.get('name'), str):
                    self.client = security.redact(info['name'])
            name = params.get('name', '') if method == 'tools/call' else ''
            self._audit(method, name if isinstance(name, str) else '')
            # Notifications cannot invoke tools or produce responses.
            if notification:
                return None
            if method == 'initialize':
                version = params.get('protocolVersion')
                result = {'protocolVersion': version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                          'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'dlr-mcp', 'version': __version__}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': list(self.tools.values())}
            elif method == 'tools/call':
                if not isinstance(name, str) or name not in self.tools:
                    return self._error(req_id, -32601, 'Unknown or disabled tool')
                result = self._call(name, params.get('arguments', {}))
            else:
                return self._error(req_id, -32601, 'Method not found')
            return security.redact_obj({'jsonrpc': '2.0', 'id': req_id, 'result': result})
        except ValueError as error:
            return None if notification else self._error(req_id, -32602, str(error))
        except Exception as error:
            return None if notification else self._error(req_id, -32000, str(error))

    @staticmethod
    def _error(req_id, code, message):
        return security.redact_obj({'jsonrpc': '2.0', 'id': req_id,
                                    'error': {'code': code, 'message': message}})

    def serve_stdio(self, instream=None, outstream=None):
        inp = instream if instream is not None else sys.stdin
        out = outstream if outstream is not None else sys.stdout
        for line in inp:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except ValueError:
                try:
                    self._audit('parse_error')
                    response = self._error(None, -32700, 'Parse error')
                except Exception as error:
                    response = self._error(None, -32000, str(error))
            else:
                response = self.handle(request)
            if response is not None:
                out.write(json.dumps(response, allow_nan=False) + '\n')
                out.flush()
