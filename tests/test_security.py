import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from helpers import load_policy, TERM
from llm_router import config
from llm_router import security
from llm_router import task_dispatch as td
from test_task_dispatch import repo

# Fake secret-shaped values built at runtime so this file itself holds no literal key.
FAKE_OPENAI = 'sk-proj-' + 'A1b2C3d4E5f6G7h8I9j0K1l2'
FAKE_GH = 'ghp_' + 'abcdefghijklmnopqrstuvwxyz0123456789'
FAKE_PEM = '-----BEGIN RSA ' + 'PRIVATE KEY-----\nTk9UQVJFQUxLRVlKVVNUQVRFU1RGSVhUVVJFTk9UQVJFQUxLRVk=\n-----END RSA ' + 'PRIVATE KEY-----'


class LintTests(unittest.TestCase):
    def test_committed_policy_passes(self):
        load_policy()

    def test_rejects_prompt_in_argv(self):
        with self.assertRaisesRegex(security.LintError, 'process list'):
            security.lint_commands({'codex': ['{codex}', 'exec', '{prompt}']})
        with self.assertRaisesRegex(security.LintError, 'process list'):
            security.lint_commands({'codex': ['{codex}', '--msg={prompt}']})

    def test_rejects_unknown_placeholder_secret_flag_and_inline_secret(self):
        with self.assertRaisesRegex(security.LintError, 'unknown placeholder'):
            security.lint_commands({'x': ['{cursor}', '{api_key}']})
        for flag in ('--api-key', '--api_key=abc', '--token', '-key'):
            with self.assertRaisesRegex(security.LintError, 'credential flag'):
                security.lint_commands({'x': ['{cursor}', flag]})
        for value in (FAKE_OPENAI, 'Bearer ' + 'x' * 30, 'OPENAI_API_KEY=' + 'abcd1234efgh'):
            with self.assertRaisesRegex(security.LintError, 'inline secret'):
                security.lint_commands({'x': ['{cursor}', value]})

    def test_load_policy_lints(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'run_task': {'commands': {'codex': ['{codex}', 'exec', '{prompt}']}}}))
            with self.assertRaises(security.LintError):
                config.load_policy(path)


class RedactionTests(unittest.TestCase):
    def test_patterns(self):
        text = (f'key {FAKE_OPENAI} gh {FAKE_GH}\nAuthorization: Bearer {"t" * 24}\n'
                'export DB_PASSWORD="' f'hunter2hunter2"\nAPI_KEY=abc123xyz\n{FAKE_PEM}\nmax_tokens=4096 plain words')
        out = security.redact(text)
        for secret in (FAKE_OPENAI, FAKE_GH, 't' * 24, 'hunter2hunter2', 'abc123xyz', 'MIIBOgIBAAJB'):
            self.assertNotIn(secret, out)
        self.assertIn('DB_PASSWORD="[REDACTED]"', out)
        self.assertIn('max_tokens=4096 plain words', out)

    def test_redact_obj(self):
        self.assertEqual(security.redact_obj({'a': [f'x {FAKE_GH}'], 'n': 3}), {'a': ['x [REDACTED]'], 'n': 3})


class ShimTests(unittest.TestCase):
    def test_claude_refused_and_logged_env_scrubbed(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = security.ensure_claude_shims(Path(tmp) / 'shims', Path(tmp) / 'block.log')
            env = security.worker_env(shim, {'PATH': '/usr/bin:/bin', 'ANTHROPIC_API_KEY': 'x',
                                             'CLAUDE_CONFIG_DIR': 'y', 'LITELLM_MASTER_KEY': 'z', 'HOME': '/h'})
            self.assertEqual(sorted(env), ['HOME', 'PATH'])
            self.assertTrue(env['PATH'].startswith(str(shim) + os.pathsep))
            for name in ('claude', 'claude-code'):
                done = subprocess.run(['sh', '-c', f'{name} --print secret-arg'], env=env,
                                      capture_output=True, text=True)
                self.assertEqual(done.returncode, 97)
            log = (Path(tmp) / 'block.log').read_text()
            self.assertEqual(log.count('blocked'), 2)
            self.assertNotIn('secret-arg', log)
            self.assertEqual(oct((shim / 'claude').stat().st_mode & 0o777), '0o700')


class LeakCheckTests(unittest.TestCase):
    def test_diff_and_untracked_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = repo(Path(tmp) / 't')
            (target / 'README.md').write_text('hello\nship Zephyrquill now\n')
            (target / 'new').mkdir()
            (target / 'new' / 'cfg.env').write_text(f'GITHUB_TOKEN={FAKE_GH}\n')
            (target / 'blob.bin').write_bytes(b'\0Zephyrquill')
            head = subprocess.run(['git', '-C', str(target), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
            result = security.leak_check(target, head, ['new/', 'blob.bin'], ['zephyrquill'])
            self.assertEqual(result['status'], 'blocked')
            files = {f['file']: f for f in result['findings']}
            self.assertEqual(files['README.md:2']['kind'], 'private_term')
            self.assertEqual(files['new/cfg.env:1']['kind'], 'github_token')
            self.assertNotIn('blob.bin:1', files)
            self.assertNotIn(FAKE_GH, json.dumps(result))
            self.assertNotIn('zephyrquill', json.dumps(result).lower())
            self.assertTrue((target / 'new' / 'cfg.env').exists())  # never deletes

    def test_clean_and_missing_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = repo(Path(tmp) / 't')
            (target / 'README.md').write_text('hello\nworld\n')
            result = security.leak_check(target, None, [], None)
            self.assertEqual(result['status'], 'clean')
            self.assertIn('note', result)
            self.assertEqual(security.load_terms(Path(tmp) / 'none.txt'), None)


class RunTaskSecurityTests(unittest.TestCase):
    MARK = 'PROMPT-ARGV-MARKER-' + 'q7z'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = repo(base / 'target')
        self.task = base / 'task.md'
        self.task.write_text(f'Do the thing {self.MARK} and API_KEY=' f'abcd{"9" * 20}\n')
        self.policy = copy.deepcopy(load_policy(never=['claude']))
        self.policy['run_task']['ledger'] = str(base / 'ledger.csv')
        self.policy['run_task']['private_terms_file'] = str(base / 'terms.txt')
        (base / 'terms.txt').write_text('# test\nZephyrquill\n')
        self.policy['run_task']['block_log'] = str(base / 'block.log')
        self.policy['run_task']['shim_dir'] = str(base / 'shims')
        self.out = base / 'out'

    def tearDown(self):
        self.tmp.cleanup()

    def select(self, chars, **kw):
        return {'eligible': ['codex', 'grok-build', 'cursor'], 'meters': {'codex': 10, 'grok-build': 10, 'cursor': 10},
                'reason': 'test'}

    def run_with(self, worker, code, watch=True):
        policy = copy.deepcopy(self.policy)
        template = policy['run_task']['commands'][worker]
        policy['run_task']['commands'][worker] = [sys.executable, '-c', code] + [
            arg for arg in template if arg in ('{prompt_file}', '{prompt_pointer}', '-')]
        seen, result = [], {}

        def go():
            result['summary'] = td.run_task(self.task, self.repo, worker=worker, select=self.select,
                                            policy=policy, output_root=self.out)
        thread = threading.Thread(target=go)
        thread.start()
        while thread.is_alive():
            # pgrep -f matches full command lines but prints only PIDs (no other process's args).
            found = subprocess.run(['pgrep', '-f', self.MARK], capture_output=True, text=True)
            seen.append(found.returncode == 0)
            time.sleep(0.1)
        thread.join()
        return result['summary'], seen

    def test_stdin_delivery_keeps_prompt_off_argv(self):
        code = ("import sys,time; d=sys.stdin.read(); open('got.txt','w').write(d); time.sleep(1.5)")
        summary, seen = self.run_with('codex', code)
        self.assertTrue(summary['attempts'][0]['ok'])
        self.assertEqual(summary['attempts'][0]['prompt_delivery'], 'stdin')
        self.assertGreater(len(seen), 5)
        self.assertFalse(any(seen))
        self.assertIn(self.MARK, (self.repo / 'got.txt').read_text())

    def test_file_delivery_0600_and_removed(self):
        code = ("import os,stat,sys,time; p=sys.argv[1]; "
                "open('got.txt','w').write(oct(os.stat(p).st_mode & 0o777)+'|'+p+'|'+open(p).read()); time.sleep(1.5)")
        summary, seen = self.run_with('grok-build', code)
        mode, path, body = (self.repo / 'got.txt').read_text().split('|', 2)
        self.assertEqual(mode, '0o600')
        self.assertIn('/.git/router-prompts/', path)
        self.assertIn(self.MARK, body)
        self.assertFalse(Path(path).exists())
        self.assertFalse(any(seen))
        self.assertNotIn('router-prompts', ' '.join(summary['untracked']))

    def test_pointer_delivery_argv_has_only_pointer(self):
        code = ("import re,sys,time; p=re.search(r'file (\\S+)\\.', sys.argv[1]).group(1); "
                "open('got.txt','w').write(open(p).read()); time.sleep(1.5)")
        summary, seen = self.run_with('cursor', code)
        self.assertEqual(summary['attempts'][0]['prompt_delivery'], 'pointer')
        self.assertIn(self.MARK, (self.repo / 'got.txt').read_text())
        self.assertFalse(any(seen))

    def test_private_term_written_by_worker_is_flagged(self):
        code = "open('notes.md','w').write('built for Zephyrquill\\n')"
        summary, _ = self.run_with('codex', code, watch=False)
        self.assertTrue(summary['attempts'][0]['ok'])
        self.assertFalse(summary['ok'])
        self.assertTrue(summary['blocked_by_leak_check'])
        self.assertEqual(summary['leak_check']['findings'],
                         [{'file': 'notes.md:1', 'kind': 'private_term', 'match': 'Ze***l'}])
        self.assertTrue((self.repo / 'notes.md').exists())
        self.assertIn('leak-blocked', Path(self.policy['run_task']['ledger']).read_text())

    def test_worker_output_and_summary_redacted_and_claude_blocked(self):
        code = (f"import subprocess,sys; print('leaked {FAKE_GH}'); sys.stderr.write('Bearer {'z' * 30}\\n'); "
                "r=subprocess.run(['claude','-p','x']); print('claude rc', r.returncode)")
        summary, _ = self.run_with('codex', code, watch=False)
        log = Path(summary['output_path']).read_text()
        self.assertNotIn(FAKE_GH, log)
        self.assertNotIn('z' * 30, log)
        self.assertIn('claude rc 97', log)
        self.assertEqual(summary['blocked_worker_attempts'], 1)
        self.assertEqual(summary['leak_check']['status'], 'clean')

    def test_worker_env_has_no_claude_vars(self):
        os.environ['ANTHROPIC_TEST_ONLY'] = '1'
        try:
            code = "import os,json; open('env.json','w').write(json.dumps(sorted(k for k in os.environ if k.startswith(('ANTHROPIC_','CLAUDE_')))))"
            self.run_with('codex', code, watch=False)
        finally:
            del os.environ['ANTHROPIC_TEST_ONLY']
        self.assertEqual(json.loads((self.repo / 'env.json').read_text()), [])


if __name__ == '__main__':
    unittest.main()
