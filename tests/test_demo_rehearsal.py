"""Rehearsal for the demo command sequence. Fake workers, fake gh, fixture meters, local bare remote.

No real worker CLI, no network, no user config. Commands match the fenced sh blocks in docs/demo.md.
"""
import copy
import fcntl
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (points config and state at a throwaway directory before llm_router imports)
from helpers import load_policy
from test_pr_gate import FAKE_GH
from test_quota_service import FakeIO, evidence
from llm_router import cli, config

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'examples' / 'demo'
SETUP = DEMO / 'setup.sh'
PARALLEL = DEMO / 'run-parallel.sh'
DOC = ROOT / 'docs' / 'demo.md'
TEST_CMD = 'PYTHONPATH=src python3 -m unittest discover -s tests'
SELECT_REASON = 'demo: worker named by select'
PARALLEL_REASON = 'demo: one agent per clone'
REVIEW_NOTES = 'reviewed the diff'

CLAUDE_APPROVE = (
    'import json, sys\n'
    'sys.stdin.read()\n'
    'verdict = json.dumps({"schema_version": 1, "verdict": "approve", '
    '"notes": "diff in scope and tests passed"})\n'
    'print(json.dumps({"type": "result", "result": verdict}))\n'
)


def _apply_function(module_name, name, body):
    """Python source for a worker that adds one function and one unittest if they are absent."""
    return (
        'import sys\n'
        'from pathlib import Path\n'
        'sys.stdin.read()\n'
        f'hold = {body["hold"]!r}\n'
        'if hold:\n'
        '    import fcntl\n'
        '    print("waiting", flush=True)\n'
        '    with open(hold) as handle:\n'
        '        fcntl.flock(handle, fcntl.LOCK_SH)\n'
        f'source = Path("src/{module_name}.py")\n'
        'text = source.read_text() if source.exists() else ""\n'
        f'if "def {name}" not in text:\n'
        f'    source.parent.mkdir(parents=True, exist_ok=True)\n'
        f'    source.write_text(text.rstrip() + "\\n\\n\\n" + {body["function"]!r})\n'
        f'tests = Path({body["test_path"]!r})\n'
        'current = tests.read_text() if tests.exists() else ""\n'
        f'if "def test_{name}" not in current:\n'
        '    if current:\n'
        f'        current = current.replace("\\nif __name__", "\\n" + {body["method"]!r} + "\\nif __name__", 1)\n'
        '        tests.write_text(current)\n'
        '    else:\n'
        '        tests.parent.mkdir(parents=True, exist_ok=True)\n'
        f'        tests.write_text({body["new_test"]!r})\n'
    )


def function_script(module, name, expression, test_path, hold=''):
    params = 'a' if name == 'negate' else 'a, b'
    function = f'def {name}({params}):\n    return {expression}\n'
    if name == 'negate':
        method = f'    def test_{name}(self):\n        self.assertEqual({module}.{name}(3), -3)\n\n'
    elif name == 'subtract':
        method = f'    def test_{name}(self):\n        self.assertEqual({module}.{name}(5, 2), 3)\n\n'
    else:
        method = f'    def test_{name}(self):\n        self.assertEqual({module}.{name}(3, 4), 12)\n\n'
    new_test = (
        'import unittest\n'
        f'import {module}\n\n'
        f'class {module.title()}Tests(unittest.TestCase):\n'
        f'{method}'
        "if __name__ == '__main__':\n"
        '    unittest.main()\n'
    )
    return _apply_function(module, name, {
        'hold': hold, 'function': function, 'method': method, 'test_path': test_path, 'new_test': new_test,
    })


def claude_script(hold=''):
    return (
        'import json, sys\n'
        'from pathlib import Path\n'
        'prompt = sys.stdin.read()\n'
        f'hold = {hold!r}\n'
        'if hold:\n'
        '    import fcntl\n'
        '    print("waiting", flush=True)\n'
        '    with open(hold) as handle:\n'
        '        fcntl.flock(handle, fcntl.LOCK_SH)\n'
        'if "Review run-job" in prompt:\n'
        '    verdict = json.dumps({"schema_version": 1, "verdict": "approve", '
        '"notes": "diff in scope and tests passed"})\n'
        '    print(json.dumps({"type": "result", "result": verdict}))\n'
        '    raise SystemExit(0)\n'
        'readme = Path("README.md")\n'
        'text = readme.read_text()\n'
        'if "## Usage" not in text:\n'
        '    readme.write_text(text.rstrip() + "\\n\\n## Usage\\n\\n'
        '```python\\nimport calc\\ncalc.add(2, 3)\\n```\\n")\n'
    )


def git(cwd, *args):
    done = subprocess.run(
        ['git', '-C', str(cwd), '-c', 'user.name=Dynamic LLM Router', '-c', 'user.email=router@localhost',
         '-c', 'commit.gpgsign=false', *args],
        capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError(done.stderr or done.stdout)
    return done.stdout.strip()


def sh_router_lines(text):
    """llm-router command lines inside fenced sh blocks, with a leading VAR= prefix removed."""
    lines, fence = [], None
    for raw in text.splitlines():
        if fence is None and raw.startswith('```sh'):
            fence = True
            continue
        if fence and raw.startswith('```'):
            fence = None
            continue
        if not fence:
            continue
        line = raw.strip()
        if line.startswith('COLUMNS='):
            line = line.split(None, 1)[1]
        if line.startswith('llm-router '):
            lines.append(line)
    return lines


def skeleton(line):
    """Flags and literal arguments. Paths and $variables are filled in by the rehearsal."""
    kept = []
    for token in shlex.split(line)[1:]:
        if '$' in token or token.startswith('/'):
            continue
        kept.append(token)
    return kept


def contains_in_order(argv, tokens):
    index = 0
    for arg in argv:
        if index < len(tokens) and arg == tokens[index]:
            index += 1
    return index == len(tokens)


class _RoutingStdout(io.TextIOBase):
    """Send each thread's prints to that thread's buffer. redirect_stdout is process-global."""

    def __init__(self, underlying):
        self.underlying = underlying
        self.local = threading.local()

    def write(self, text):
        buf = getattr(self.local, 'buf', None)
        if buf is None:
            return self.underlying.write(text)
        return buf.write(text)

    def flush(self):
        buf = getattr(self.local, 'buf', None)
        if buf is None:
            self.underlying.flush()
        else:
            buf.flush()

    def isatty(self):
        return False


class DemoRehearsalTests(unittest.TestCase):
    invoked = []

    def setUp(self):
        for key in list(os.environ):
            if key.startswith('GIT_CONFIG_'):
                os.environ.pop(key)
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.policy = self.make_policy(self.base)
        self.observe = mock.patch(
            'llm_router.observers.observe_isolated',
            side_effect=AssertionError('refusing a live meter read in the rehearsal'))
        self.observe.start()
        self.addCleanup(self.observe.stop)
        self._saved_stdout = sys.stdout
        sys.stdout = _RoutingStdout(self._saved_stdout)
        self.addCleanup(self._restore_stdout)
        self.addCleanup(self.tmp.cleanup)

    def _restore_stdout(self):
        sys.stdout = self._saved_stdout

    def make_policy(self, base):
        policy = copy.deepcopy(load_policy(timezone='UTC'))
        terms = base / 'terms.txt'
        terms.write_text('Zephyrquill\n')
        run = policy['run_task']
        run.update(ledger=str(base / 'ledger.csv'), output_dir=str(base / 'out'),
                   private_terms_file=str(terms), block_log=str(base / 'block.log'),
                   shim_dir=str(base / 'shims'))
        policy['run_job'].update(worktree_root=str(base / 'wt'), lock_dir=str(base / 'locks'),
                                 jobs_dir=str(base / 'jobs'), repo_config=str(base / 'repos.json'),
                                 caffeinate=False)
        policy['alerts'] = {'file': str(base / 'alerts.jsonl'), 'summary': str(base / 'ALERTS.md'),
                            'macos_notification': False}
        unmetered = list(policy.get('unmetered_workers') or [])
        if 'claude' not in unmetered:
            unmetered.append('claude')
        policy['unmetered_workers'] = unmetered
        gh_dir = base / 'fakegh'
        gh_dir.mkdir()
        gh = gh_dir / 'gh'
        gh.write_text(FAKE_GH.format(python=sys.executable))
        gh.chmod(0o755)
        (gh_dir / 'responses.json').write_text(json.dumps({
            'pr list': {'stdout': '[]\n'},
            'pr create': {'stdout': 'https://github.com/example/sample/pull/7\n'},
        }))
        policy['pr_gate'] = {'approvals_file': str(base / 'approvals.jsonl'), 'gate_dir': str(base / 'gate'),
                             'reviews_dir': str(base / 'reviews'), 'gh_bin': str(gh), 'approval_ttl_hours': 24}
        self.gh_dir = gh_dir
        # Never leave a real CLI command in place. Tests install the script they need.
        for name in ('codex', 'grok-build', 'cursor', 'claude'):
            self.install(policy, name, 'import sys\nsys.exit(3)\n')
        return policy

    def install(self, policy, worker, script):
        commands = policy['run_task']['commands']
        tail = [arg for arg in commands.get(worker, []) if arg in ('-', '{prompt_file}', '{prompt_pointer}')]
        # install() may run twice; the second time the tail was already dropped. Put the delivery token back.
        if not tail:
            delivery = policy['run_task'].get('prompt_delivery', {}).get(worker)
            tail = {'stdin': ['-'], 'file': ['{prompt_file}'], 'pointer': ['{prompt_pointer}']}.get(delivery, [])
        commands[worker] = [sys.executable, '-c', script, *tail]

    def invoke(self, argv, policy=None):
        self.invoked.append(list(argv))
        buf = io.StringIO()
        sys.stdout.local.buf = buf
        try:
            code = cli.main(list(argv), policy=policy or self.policy)
        finally:
            sys.stdout.local.buf = None
        return code, buf.getvalue()

    def seed_codex_cache(self):
        now = datetime.now(timezone.utc)
        observation = {'evidence': evidence('codex', {
            'primary': (10, now + timedelta(hours=4)),
            'weekly': (10, now + timedelta(days=5)),
        }, observed=now, fresh_for=3600), 'cli_version': 'rehearsal', 'version_warning': None}
        path = config.state_dir() / 'state' / 'codex_quota_observation.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(observation))
        self.addCleanup(path.unlink, missing_ok=True)

    def prepare_demo(self):
        demo = self.base / 'dlr-demo'
        demo.mkdir()
        sample = demo / 'sample'
        done = subprocess.run(['sh', str(SETUP), str(sample)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr or done.stdout)
        again = subprocess.run(['sh', str(SETUP), str(sample)], capture_output=True, text=True)
        self.assertEqual(again.returncode, 1, again.stdout)
        self.assertIn('refusing:', again.stderr)
        tests = subprocess.run(
            ['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-q'],
            cwd=sample, env={**os.environ, 'PYTHONPATH': 'src'}, capture_output=True, text=True)
        self.assertEqual(tests.returncode, 0, tests.stderr)
        self.demo, self.sample = demo, sample
        return demo, sample

    def test_setup_script_commits_tasks_and_refuses_twice(self):
        from llm_router.job_dispatch import parse_allowed_paths
        demo, sample = self.prepare_demo()
        self.assertTrue((demo / 'repo-codex' / 'src' / 'calc.py').is_file())
        self.assertTrue((demo / 'repo-cursor' / 'tasks' / 'multiply.md').is_file())
        self.assertTrue((demo / 'repo-claude' / 'tasks' / 'docs.md').is_file())
        self.assertEqual(parse_allowed_paths((sample / 'tasks' / 'negate.md').read_text()),
                         ['src/calc.py', 'tests/test_calc.py'])
        self.assertEqual(parse_allowed_paths((sample / 'tasks' / 'multiply.md').read_text()),
                         ['src/mult.py', 'tests/test_mult.py'])
        self.assertEqual(parse_allowed_paths((sample / 'tasks' / 'docs.md').read_text()), ['README.md'])
        self.assertIn('subtract(5, 2)', (sample / 'tasks' / 'subtract.md').read_text())

    def test_status_and_select_use_fixture_meters(self):
        now = datetime.now(timezone.utc)
        policy = copy.deepcopy(self.policy)
        policy['quota']['grok-build']['enabled'] = True
        policy['quota']['cursor']['enabled'] = True
        policy['quota']['cursor']['consult'] = 'always'
        policy['grok_bot'] = {'billing': {'mode': 'separate'}, 'reserve_percent': 15,
                              'evidence': {'percent_used': 0, 'reset_at': (now + timedelta(days=6)).isoformat()}}
        codex = {'evidence': evidence('codex', {
            'primary': (10, now + timedelta(hours=4)),
            'weekly': (10, now + timedelta(days=5)),
        }, observed=now, fresh_for=3600)}
        grok = {'evidence': evidence('grok-build', {
            'weekly': (40, now + timedelta(days=3)),
        }, observed=now - timedelta(hours=3), fresh_for=60)}
        fake = FakeIO({'codex': codex, 'grok-build': grok, 'cursor': None},
                      {'codex': codex, 'grok-build': grok})
        with mock.patch('llm_router.quota_service.default_io', return_value=fake.io()):
            status_code, status = self.invoke(['status'], policy)
            select_code, selected = self.invoke(['select'], policy)
        self.assertEqual(status_code, 0, status)
        self.assertIn('codex', status)
        self.assertIn('no (stale)', status)
        self.assertIn('no (unknown)', status)
        self.assertEqual(select_code, 0, selected)
        result = json.loads(selected)
        self.assertEqual(result['worker'], 'codex')
        self.assertEqual(result['reason'], 'preferred_eligible_worker')
        self.assertEqual(result['reasons']['grok-build'], 'quota_expired')
        self.assertEqual(result['reasons']['cursor'], 'quota_unknown')

    def bind_sample_remote(self):
        remote = self.base / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', '--initial-branch=main', str(remote)], check=True)
        git(self.sample, 'remote', 'add', 'origin', str(remote))
        git(self.sample, 'push', '-q', 'origin', 'main')
        (self.base / 'repos.json').write_text(json.dumps({
            'sample': {'remote': 'origin', 'base': 'main', 'visibility': 'public', 'test_cmd': TEST_CMD},
        }))

    def test_gate_walkthrough_steps_3_to_7(self):
        self.prepare_demo()
        self.bind_sample_remote()
        self.seed_codex_cache()
        self.install(self.policy, 'codex', function_script('calc', 'negate', '-a', 'tests/test_calc.py'))
        self.install(self.policy, 'claude', CLAUDE_APPROVE)
        code, out = self.invoke(['scan', '--history', str(self.sample)])
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)['status'], 'clean')
        main_before = git(self.sample, 'rev-parse', 'main')
        code, out = self.invoke([
            'run-job', '--repo', str(self.sample), '--task-file', str(self.sample / 'tasks' / 'negate.md'),
            '--worker', 'codex', '--override-reason', SELECT_REASON, '--size', 'draft', '--test-cmd', TEST_CMD])
        self.assertEqual(code, 0, out)
        job = json.loads(out)
        self.assertTrue(job['ok'], job)
        self.assertEqual(job['status'], 'committed')
        self.assertTrue(job['branch'].startswith('router/'))
        self.assertEqual(job['tests_status'], 'passed')
        self.assertEqual(job['leak_check']['status'], 'clean')
        self.assertEqual(job['workers'][-1], 'codex')
        self.assertEqual(job['review']['verdict'], 'approve')
        self.assertEqual(job['review']['reviewer'], 'claude')
        self.assertNotEqual(job['review']['reviewer'], job['workers'][-1])
        head = job['commit']
        self.assertEqual(len(head), 40)
        self.assertEqual(git(self.sample, 'rev-parse', job['branch']), head)
        self.assertEqual(git(self.sample, 'rev-parse', 'main'), main_before)
        self.assertEqual(git(self.sample, 'rev-parse', 'HEAD'), main_before)

        code, reviewed = self.invoke(['review', job['job_id']])
        self.assertEqual(code, 0, reviewed)
        self.assertEqual(json.loads(reviewed)['leak_check'], 'clean')
        self.assertEqual(json.loads(reviewed)['head'], head)

        code, refused = self.invoke(['push', job['job_id']])
        self.assertEqual(code, 3, refused)
        self.assertIn(f'push refused: no push approval for {head[:10]}', refused)

        code, approved = self.invoke(['approve', job['job_id'], '--action', 'push', '--sha', head])
        self.assertEqual(code, 0, approved)
        self.assertEqual(json.loads(approved)['sha'], head)
        code, pushed = self.invoke(['push', job['job_id']])
        self.assertEqual(code, 0, pushed)
        body = json.loads(pushed)
        self.assertTrue(body['pushed'])
        self.assertTrue(body['pr']['draft'])
        self.assertEqual(body['pr']['number'], 7)
        calls = [json.loads(line) for line in (self.gh_dir / 'calls.jsonl').read_text().splitlines()]
        self.assertTrue(any(call[:2] == ['pr', 'create'] and '--draft' in call for call in calls))
        self.assertEqual(git(self.sample, 'ls-remote', 'origin', f'refs/heads/{job["branch"]}').split()[0], head)

        note = Path(job['worktree']) / 'NOTE.txt'
        note.write_text('head moved after approval\n')
        git(job['worktree'], 'add', '--', 'NOTE.txt')
        git(job['worktree'], 'commit', '-q', '-m', 'move the head after approval')
        moved = git(self.sample, 'rev-parse', job['branch'])
        self.assertNotEqual(moved, head)
        code, stale = self.invoke(['push', job['job_id']])
        self.assertEqual(code, 3, stale)
        self.assertIn('push refused: push approval was for an older commit', stale)
        self.assertEqual(git(self.sample, 'ls-remote', 'origin', f'refs/heads/{job["branch"]}').split()[0], head)
        self.assertEqual(git(self.sample, 'rev-parse', 'main'), main_before)

        # Claude unavailable: run-job leaves a grok-bot packet, and jobs --verdict records it.
        missing = str(self.base / 'missing-claude')
        self.policy['binaries']['claude'] = missing
        self.policy['run_task']['commands']['claude'] = [missing]
        clone = self.demo / 'repo-codex'
        code, pending_out = self.invoke([
            'run-job', '--repo', str(clone), '--task-file', str(clone / 'tasks' / 'negate.md'),
            '--worker', 'codex', '--override-reason', SELECT_REASON, '--size', 'draft', '--test-cmd', TEST_CMD])
        self.assertEqual(code, 0, pending_out)
        pending = json.loads(pending_out)
        self.assertEqual(pending['status'], 'committed', pending)
        self.assertEqual((pending['review']['status'], pending['review']['reviewer']), ('pending', 'grok-bot'))
        self.assertNotIn('verdict', pending['review'])
        code, recorded = self.invoke([
            'jobs', '--job', pending['job_id'], '--verdict', 'approve', '--reviewer', 'grok-bot',
            '--notes', REVIEW_NOTES])
        self.assertEqual(code, 0, recorded)
        saved = json.loads(recorded)
        self.assertEqual(saved['review']['verdict'], 'approve')
        self.assertEqual(saved['review']['reviewer'], 'grok-bot')
        self.assertNotEqual(saved['review']['reviewer'], 'codex')

    def test_parallel_watch_shows_three_lanes_then_sha_approvals(self):
        self.prepare_demo()
        self.seed_codex_cache()
        holds = {}
        handles = []
        for worker in ('codex', 'cursor', 'claude'):
            path = self.base / f'hold-{worker}'
            path.write_text('hold\n')
            handle = open(path)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            holds[worker] = str(path)
            handles.append(handle)
        self.install(self.policy, 'codex', function_script(
            'calc', 'subtract', 'a - b', 'tests/test_calc.py', hold=holds['codex']))
        self.install(self.policy, 'cursor', function_script(
            'mult', 'multiply', 'a * b', 'tests/test_mult.py', hold=holds['cursor']))
        self.install(self.policy, 'claude', claude_script(holds['claude']))
        previous = os.environ.get('COLUMNS')
        os.environ['COLUMNS'] = '160'
        self.addCleanup(lambda: os.environ.pop('COLUMNS') if previous is None else os.environ.__setitem__('COLUMNS', previous))

        specs = (
            ('codex', 'subtract'),
            ('cursor', 'multiply'),
            ('claude', 'docs'),
        )
        boxes, threads = [], []

        def run(argv, box):
            try:
                box['code'], box['out'] = self.invoke(argv)
            except Exception as error:  # surfaced after join
                box['error'] = error

        for worker, task in specs:
            repo = self.demo / f'repo-{worker}'
            argv = ['run-job', '--repo', str(repo), '--task-file', str(repo / 'tasks' / f'{task}.md'),
                    '--worker', worker, '--override-reason', PARALLEL_REASON, '--size', 'draft',
                    '--test-cmd', TEST_CMD]
            box = {}
            boxes.append(box)
            thread = threading.Thread(target=run, args=(argv, box))
            thread.start()
            threads.append(thread)

        seen, frame = False, ''
        deadline = time.time() + 45
        while time.time() < deadline:
            code, frame = self.invoke(['watch', '--once'])
            if (code == 0 and '3 running' in frame and all(f'repo-{name}' in frame for name in ('codex', 'cursor', 'claude'))):
                seen = True
                break
            time.sleep(0.05)
        for handle in handles:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        for thread in threads:
            thread.join(120)
        self.assertTrue(seen, frame)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        jobs = []
        for box in boxes:
            self.assertIsNone(box.get('error'), box.get('error'))
            self.assertEqual(box.get('code'), 0, box.get('out'))
            job = json.loads(box['out'])
            self.assertTrue(job['ok'], job)
            self.assertEqual(job['tests_status'], 'passed')
            self.assertEqual(job['leak_check']['status'], 'clean')
            self.assertTrue(job['branch'].startswith('router/'))
            self.assertEqual(len(job['commit']), 40)
            jobs.append(job)
        self.assertEqual({job['workers'][-1] for job in jobs}, {'codex', 'cursor', 'claude'})
        for job in jobs:
            if job['workers'][-1] == 'claude':
                self.assertFalse(job['review_required'])
            else:
                self.assertEqual(job['review']['reviewer'], 'claude')
                self.assertNotEqual(job['review']['reviewer'], job['workers'][-1])
                self.assertEqual(job['review']['verdict'], 'approve')
        code, finished = self.invoke(['watch', '--once'])
        self.assertEqual(code, 0, finished)
        self.assertIn('0 running', finished)
        for job in jobs:
            sha = job['commit']
            self.assertIn(sha, finished)
            self.assertIn(f'llm-router approve {job["job_id"]} --action push --sha {sha}', finished)
            self.assertIn('approval ?', finished)

    def test_zz_documented_commands_were_rehearsed(self):
        text = DOC.read_text()
        lines = sh_router_lines(text)
        self.assertGreaterEqual(len(lines), 8, lines)
        missing = []
        for line in lines:
            tokens = skeleton(line)
            if not any(contains_in_order(argv, tokens) for argv in self.invoked):
                missing.append((line, tokens))
        self.assertEqual(missing, [])
        script = PARALLEL.read_text()
        self.assertIn("REASON='demo: one agent per clone'", script)
        self.assertIn('llm-router watch --once', script)
        self.assertIn('subtract', script)
        self.assertIn('multiply', script)
        self.assertIn('docs', script)
        self.assertNotIn('/Users/', text)
        self.assertNotIn('/Users/', script)
        self.assertNotIn('/Users/', SETUP.read_text())


if __name__ == '__main__':
    unittest.main()
