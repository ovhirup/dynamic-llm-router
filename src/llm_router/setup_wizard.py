"""First-run setup with separate capabilities and explicitly consented tiny smokes (#210)."""
from copy import deepcopy
from importlib import resources
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from . import claude_handoff, config, roles, security, setup_profile, task_dispatch

MARKER = 'DLR_SETUP_OK'
SMOKE_PROMPT = 'Reply exactly DLR_SETUP_OK. Do not edit files, run tools, or access other repositories.'
RATIONALES = {
    'claude': 'Capable cloud worker for design and bulk work; its allowance should be reserved for useful work.',
    'codex': 'Capable cloud worker for code and bulk work; live usage gates still apply.',
    'grok-build': 'Capable cloud worker for bulk work; it may share a small weekly budget with Grok Bot.',
    'cursor': 'Capable cloud worker for bulk work; independent model pools and spending caps stay explicit.',
    'aider-ollama': 'Cheap, private local work through Aider + Ollama; it depends on the server and selected model.',
    'grok-bot': 'Recommended orchestrator for chat, phone and approvals convenience; last-resort worker only because of its small shared weekly budget.',
    'cli': 'The current local CLI can coordinate work; enabling it does not verify any cloud sign-in.'}


def detect_tools(which=None, env=None):
    """Detect executable availability only; installation never implies authenticated usage."""
    which = shutil.which if which is None else which
    environment = os.environ if env is None else env
    binaries = {name: which(name, path=environment.get('PATH'))
                for name in ('claude', 'codex', 'grok', 'cursor-agent', 'ollama', 'aider')}
    required = {'claude': ('claude',), 'codex': ('codex',), 'grok-build': ('grok',),
                'cursor': ('cursor-agent',), 'aider-ollama': ('ollama', 'aider'), 'grok-bot': ()}
    return {name: {'installed': all(binaries[binary] for binary in required[name]) if required[name] else None,
                   'binaries': {binary: binaries[binary] for binary in required[name]},
                   'auth': 'unknown', 'rationale': RATIONALES[name]}
            for name in setup_profile.WORKERS}


def _local_preset(model=None):
    data = json.loads(resources.files('llm_router').joinpath('examples/workers/aider-ollama.json').read_text())
    worker = deepcopy(data['workers']['aider-ollama'])
    if model is not None:
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]*', model):
            raise config.ConfigError('local model must be an Ollama model name')
        model = model if model.startswith('ollama_chat/') else 'ollama_chat/' + model
        worker['command'][worker['command'].index('--model') + 1] = model
    return {'workers': {'aider-ollama': worker}, 'unmetered_workers': ['aider-ollama']}


def _assistant_marker(stdout):
    """Accept an exact plain/known assistant response, never echoed prompts or user events."""
    found = False
    for line in stdout.splitlines():
        if line.strip() == MARKER:
            found = True
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get('is_error') is True or event.get('type') == 'error':
            return False
        kind = event.get('type')
        if kind == 'result' and event.get('result') == MARKER:
            found = True
        elif kind == 'assistant' or (kind == 'message' and event.get('role') == 'assistant'):
            message = event.get('message', event)
            if not isinstance(message, dict) or message.get('role', 'assistant') != 'assistant':
                continue
            content = message.get('content')
            if content == MARKER or (isinstance(content, list) and any(
                    isinstance(item, dict) and item.get('type') == 'text' and item.get('text') == MARKER
                    for item in content)):
                found = True
        elif kind == 'item.completed':
            item = event.get('item') or {}
            if isinstance(item, dict) and item.get('type') == 'agent_message' and item.get('text') == MARKER:
                found = True
    return found


def smoke_tool(worker, policy=None, *, runner=None, env=None, input_fn=None, output_fn=None,
               timeout_seconds=25, model=None):
    """Run one bounded fixed challenge in a throwaway Git workspace, or verify a manual chat reply."""
    input_fn, output_fn = (input if input_fn is None else input_fn), (print if output_fn is None else output_fn)
    runner = task_dispatch.run_bounded if runner is None else runner
    failed = {'passed': False, 'status': 'failed', 'auth': 'unknown'}
    if worker == 'grok-bot':
        output_fn('In Grok Bot chat, send: Reply exactly ' + MARKER)
        passed = input_fn('Paste the exact reply from Grok Bot: ').strip() == MARKER
        return {'passed': passed, 'status': 'passed' if passed else 'failed',
                'auth': 'user_verified' if passed else 'unknown', 'reason': 'manual_chat_challenge'}
    if worker not in setup_profile.WORKERS or worker == 'cli':
        raise config.ConfigError('unsupported smoke worker')
    if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
        raise config.ConfigError('smoke timeout must be positive and at most 30 seconds')
    # Only trusted packaged templates are used, never a project or personal command override.
    smoke_policy = config.defaults()
    if worker == 'aider-ollama':
        smoke_policy = config.deep_merge(smoke_policy, _local_preset(model))
    if policy and policy.get('binaries'):
        smoke_policy['binaries'] = deepcopy(policy['binaries'])
    config.validate(smoke_policy)
    environment = dict(os.environ if env is None else env)
    for key in list(environment):
        if key.startswith('GIT_'):
            del environment[key]
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    try:
        with tempfile.TemporaryDirectory(prefix='dlr-setup-smoke-') as temporary:
            cwd = Path(temporary)
            subprocess.run([shutil.which('git') or 'git', 'init', '-q', str(cwd)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, env=environment)
            mode = task_dispatch.delivery(smoke_policy, worker)
            prompt_file = task_dispatch.write_prompt_file(cwd, 'setup', worker, SMOKE_PROMPT) if mode in ('file', 'pointer') else None
            argv = task_dispatch.build_argv(smoke_policy, worker, cwd, prompt_file,
                                            prompt=SMOKE_PROMPT, repo=cwd, env=environment, size='draft')
            result = runner(argv, cwd, timeout_seconds, 2,
                            input_text=SMOKE_PROMPT if mode == 'stdin' else None, env=environment)
            if result.get('timed_out'):
                return dict(failed, reason='timeout')
            if result.get('returncode') != 0:
                return dict(failed, reason='command_failed')
            stdout, stderr = result.get('stdout') or '', result.get('stderr') or ''
            if worker == 'claude' and (claude_handoff.inspect(stdout, stderr, 0)['failure'] is not None
                    or re.search(r'usage limit|rate limit|quota|out of credits|out of extra usage', stderr, re.I)):
                return dict(failed, reason='provider_refused_or_quota')
            if not _assistant_marker(stdout):
                return dict(failed, reason='response_not_verified')
            return {'passed': True, 'status': 'passed', 'auth': 'verified', 'reason': 'exact_response_marker'}
    except (OSError, ValueError, subprocess.SubprocessError):
        return dict(failed, reason='launch_failed')


def build_profile(tools, *, reserves=None, preferred_orchestrator=None, switch_policy=None,
                  worker_overrides=None, orchestrator_overrides=None, model=None):
    """Build a pure validated profile from explicit requests; smoke results are verification only."""
    tools = deepcopy(tools)
    active = set(tools)
    workers = [name for name in setup_profile.WORKERS if name in active and
               (tools[name].get('requested_capabilities') or {}).get('worker') is True]
    orchestrators = [name for name in setup_profile.ORCHESTRATORS if name in active and
                    (tools[name].get('requested_capabilities') or {}).get('orchestrator') is True]
    usable = set(workers) | set(orchestrators)
    if preferred_orchestrator is None:
        preferred_orchestrator = 'grok-bot' if 'grok-bot' in orchestrators else next(iter(orchestrators), None)
    local = _local_preset(model) if 'aider-ollama' in usable else {'workers': {}, 'unmetered_workers': []}
    fragment = {'setup': {'version': 1, 'enabled_workers': workers, 'enabled_orchestrators': orchestrators,
                           'preferred_orchestrator': preferred_orchestrator,
                           'switch_policy': deepcopy(switch_policy or {'mode': 'always_ask', 'target': None}),
                           'worker_overrides': deepcopy(worker_overrides or {'default': None, 'roles': {}}),
                           'orchestrator_overrides': deepcopy(orchestrator_overrides or {'roles': {}}), 'tools': tools},
                'worker_order': workers, 'quota': {name: {'enabled': True} for name in setup_profile.CLOUD_WORKERS if name in usable},
                'unmetered_workers': local['unmetered_workers'], 'workers': local['workers'],
                'class_order': {name: list(workers) for name in config.TASK_CLASSES},
                'role_order': {role: [name for name in roles.role_order({}, role) if name in workers]
                               for role in setup_profile.ROLES},
                'allocation': {'providers': {name: {'reserve_percent': (reserves or {}).get(name, 15)}
                                             for name in setup_profile.WORKERS if name in usable}}}
    return setup_profile.validate_profile(fragment)


def _recommended(name, installed):
    """The default answers the wizard offers, stated next to the rationale."""
    if name == 'grok-bot':
        return 'orchestrator yes, worker last resort'
    return ('worker yes' if installed else 'worker no (not installed)') + ', orchestrator no'


def _yes(input_fn, output_fn, prompt, default=False):
    while True:
        answer = input_fn(prompt + (' [Y/n]: ' if default else ' [y/N]: ')).strip().lower()
        if not answer:
            return default
        if answer in ('y', 'yes', 'n', 'no'):
            return answer in ('y', 'yes')
        output_fn('Enter yes or no.')


def _choice(input_fn, output_fn, prompt, choices, default=None, auto=False):
    while True:
        label = default if default is not None else 'auto' if auto else 'none'
        answer = input_fn(prompt + f' ({", ".join(choices) or "none"}) [{label}]: ').strip()
        if not answer or (auto and answer == 'auto'):
            return default
        if answer in choices:
            return answer
        output_fn('Choose an enabled option' + (' or auto.' if auto else '.'))


def _reason(input_fn, output_fn, label):
    while True:
        answer = input_fn('Reason for ' + label + ': ').strip()
        if answer:
            return security.redact(answer)
        output_fn('An explicit override requires a nonempty reason.')


def run_setup(output=None, *, input_fn=None, output_fn=None, which=None, runner=None, env=None, dry_run=False):
    """Guide an interactive setup; only the new guarded setup profile is written (never on dry_run)."""
    input_fn, output_fn = (input if input_fn is None else input_fn), (print if output_fn is None else output_fn)
    detected = detect_tools(which, env)
    tools, smoke = deepcopy(detected), {}
    output_fn('Default preferred orchestrator: Grok Bot when available, for chat, phone and approvals convenience.')
    output_fn('Worker and orchestrator permissions are independent. Installation is not sign-in verification.')
    model = None
    smoke_policy = config.defaults()
    for name in setup_profile.WORKERS:
        tool = tools[name]
        output_fn(name + ': ' + RATIONALES[name] + ' Installed: ' + str(tool['installed']) + '; auth: unknown.')
        output_fn(name + ': recommended ' + _recommended(name, tool['installed']) + ' (you can override either).')
        available = _yes(input_fn, output_fn, name + ': available in chat', False) if name == 'grok-bot' else tool['installed']
        tool['available'] = bool(available)
        worker = _yes(input_fn, output_fn, name + ': enable as worker', bool(available))  # grok-bot: last resort
        orchestrator = _yes(input_fn, output_fn, name + ': enable as orchestrator', name == 'grok-bot' and bool(available))
        tool['requested_capabilities'] = {'worker': worker, 'orchestrator': orchestrator}
        tool['smoke'] = {'passed': False, 'status': 'not_requested', 'auth': 'unknown', 'reason': 'disabled'}
        if worker or orchestrator:
            consent = _yes(input_fn, output_fn, name + ': run tiny smoke (may spend a small amount of allowance)', False)
            if not consent:
                tool['smoke'].update(status='unverified', reason='consent_declined')
            elif name == 'grok-bot' and not available:
                tool['smoke'].update(status='unverified', reason='chat_unavailable')
            else:
                if name == 'aider-ollama':
                    model = input_fn('Local Ollama model [qwen2.5-coder:14b]: ').strip() or 'qwen2.5-coder:14b'
                for placeholder, binary in config.WORKER_BINARY.items():
                    if placeholder == name:
                        executable = next(iter(tool['binaries'].values()), None)
                        if executable:
                            smoke_policy.setdefault('binaries', {})[binary] = executable
                tool['smoke'] = smoke_tool(name, smoke_policy, runner=runner, env=env,
                                            input_fn=input_fn, output_fn=output_fn, model=model)
            tool['auth'] = tool['smoke']['auth']
            if not tool['smoke']['passed']:
                output_fn(name + ': enabled but unverified (smoke ' + tool['smoke']['status'] + ': '
                          + tool['smoke']['reason'] + '); live quota and auth checks still apply at dispatch.')
        smoke[name] = deepcopy(tool['smoke'])
    output_fn('cli: ' + RATIONALES['cli'])
    cli_enabled = _yes(input_fn, output_fn, 'cli: enable as orchestrator', False)
    tools['cli'] = {'installed': True, 'auth': 'not_required',
                     'requested_capabilities': {'worker': False, 'orchestrator': cli_enabled},
                     'smoke': {'passed': cli_enabled, 'status': 'passed' if cli_enabled else 'not_requested',
                               'auth': 'not_required', 'reason': 'current_cli_no_cloud_auth'}}
    preliminary = build_profile(tools, model=model)
    setup = preliminary['setup']
    reserves = {}
    for name in setup_profile.WORKERS:
        if name not in set(setup['enabled_workers']) | set(setup['enabled_orchestrators']):
            continue
        while True:
            answer = input_fn(f'Reserve for {name} percent [15] (keep allowance for hard work; hard gates stay 85): ').strip()
            try:
                reserve = float(answer) if answer else 15
                if math.isfinite(reserve) and 0 <= reserve < 100:
                    reserves[name] = reserve
                    break
            except ValueError:
                pass
            output_fn('Reserve must be finite and at least 0 but below 100.')
    orchestrators, workers = setup['enabled_orchestrators'], setup['enabled_workers']
    preferred = (_choice(input_fn, output_fn, 'Preferred orchestrator', orchestrators,
                         setup['preferred_orchestrator']) if orchestrators else None)
    mode = _choice(input_fn, output_fn, 'Switch policy', setup_profile.SWITCH_MODES, 'always_ask')
    target = _choice(input_fn, output_fn, 'Fixed orchestrator target', orchestrators, preferred) if mode == 'always_use' else None
    worker_overrides, orchestrator_overrides = {'default': None, 'roles': {}}, {'roles': {}}
    if workers:
        pinned = _choice(input_fn, output_fn, 'Default worker pin', workers, auto=True)
        if pinned is not None:
            worker_overrides['default'] = {'worker': pinned, 'reason': _reason(input_fn, output_fn, 'default worker pin')}
    if (workers or orchestrators) and _yes(input_fn, output_fn, 'Configure per-role overrides?', False):
        for role in setup_profile.ROLES:
            if workers:
                pinned = _choice(input_fn, output_fn, 'Worker pin for ' + role, workers, auto=True)
                if pinned is not None:
                    worker_overrides['roles'][role] = {'worker': pinned, 'reason': _reason(input_fn, output_fn, role + ' worker pin')}
            if orchestrators:
                pinned = _choice(input_fn, output_fn, 'Orchestrator pin for ' + role, orchestrators, auto=True)
                if pinned is not None:
                    orchestrator_overrides['roles'][role] = {'orchestrator': pinned, 'reason': _reason(input_fn, output_fn, role + ' orchestrator pin')}
    fragment = build_profile(tools, reserves=reserves, preferred_orchestrator=preferred,
                              switch_policy={'mode': mode, 'target': target}, worker_overrides=worker_overrides,
                              orchestrator_overrides=orchestrator_overrides, model=model)
    if dry_run:
        config.validate(config.deep_merge(config.defaults(), fragment))
        output_fn('Dry run: setup profile validated but not written.')
        return {'config': fragment, 'detected': detected, 'smoke': smoke, 'written': None, 'backup': None}
    written = setup_profile.write_profile(fragment, output)
    output_fn('Wrote setup profile: ' + written['path'])
    if written['backup']:
        output_fn('Backed up previous setup profile: ' + written['backup'])
    return {'config': fragment, 'detected': detected, 'smoke': smoke,
            'written': written['path'], 'backup': written['backup']}
