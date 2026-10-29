"""Claude-specific regressions; fake runners only."""
import json
import unittest

from helpers import load_policy
from llm_router import claude_handoff as ch, security, task_dispatch as td
import test_workers


class BriefTests(unittest.TestCase):
    def test_brief(self):
        prompt = ch.build_prompt('Think carefully before answering.\nFix bug.', 'medium', 99,
                                 elapsed=4, prefix='router extra', extra='job extra', tail='Stackwich tail')
        self.assertIn('Budget: 99s. Elapsed: 4s.', prompt)
        self.assertIn('Read the relevant files', prompt)
        self.assertIn('hard cap of 3', prompt)
        self.assertNotIn('Think carefully before answering.', prompt)
        self.assertIn('Fix bug.', prompt)
        self.assertIn('<pasted_content id="', prompt)
        self.assertNotIn('<external_content>', prompt)
        self.assertLess(prompt.index('</pasted_content>'), prompt.index('router extra'))
        self.assertTrue(prompt.endswith('Stackwich tail'))
        self.assertEqual(ch.clean('\n'.join(['think step by step', 'THINK DEEPLY', 'think carefully',
            'show your reasoning', 'show your chain of thought', 'write out your internal reasoning', 'task'])), 'task')
        self.assertEqual(ch.clean('Fix the off-by-one error in parser.py. Think carefully about the boundary.'),
                         'Fix the off-by-one error in parser.py.')
        self.assertEqual(ch.clean('Think step by step! Then update the README and add a test.'),
                         'Then update the README and add a test.')
        self.assertEqual(ch.clean('  indented line stays as is'), '  indented line stays as is')

    def test_effort(self):
        for template in (['claude', '-p'], ['claude', '--effort', 'max'],
                         ['claude', '--effort=xhigh'], ['claude', '--effort', '{claude_effort}']):
            for size, expected in [('draft', 'low'), ('full', 'medium')]:
                policy = load_policy()
                policy['run_task']['commands']['claude'] = template
                security.lint_commands({'claude': template})
                argv = td.build_argv(policy, 'claude', '/tmp', size=size)
                if any(arg.startswith('--effort=') for arg in template):
                    self.assertEqual(argv[-1], '--effort=' + expected)
                else:
                    self.assertEqual(argv[-2:], ['--effort', expected])
                    self.assertEqual(argv.count('--effort'), 1)
                self.assertNotIn('--effort=xhigh', argv)
        policy['run_task']['commands']['claude'] = ['claude', '--effort', 'max']
        policy['run_task']['claude_allow_raised_effort'] = True
        self.assertEqual(td.build_argv(policy, 'claude', '/tmp')[-1], 'max')

    def test_effort_before_option_terminator(self):
        for options in ([], ['--effort', 'max'], ['--effort=xhigh']):
            policy = load_policy()
            policy['run_task']['commands']['claude'] = ['claude', '-p'] + options + ['--', '{prompt}']
            policy['run_task']['prompt_delivery']['claude'] = 'arg'
            argv = td.build_argv(policy, 'claude', '/tmp', prompt='literal --effort max')
            self.assertEqual(argv[argv.index('--') + 1:], ['literal --effort max'])
            self.assertIn('--effort=medium' if options == ['--effort=xhigh'] else 'medium',
                          argv[:argv.index('--')])

    def test_stream_precedence_and_failures(self):
        assistant = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '- [ ] old'}]}}
        result = {'type': 'result', 'result': '- [x] finished'}
        self.assertEqual(ch.inspect('\n'.join(map(json.dumps, [assistant, result])))['open_items'], [])
        self.assertEqual(ch.inspect(json.dumps(assistant))['open_items'], ['- [ ] old'])
        for event in ({'type': 'refusal'}, {'type': 'result', 'stop_reason': 'refusal'},
                      {'type': 'assistant', 'message': {'stop_reason': 'refusal'}}):
            self.assertEqual(ch.inspect(json.dumps(event))['failure'], 'refusal')
        self.assertEqual(ch.inspect(json.dumps({'type': 'result', 'is_error': True, 'result': 'failed'}))['failure'], 'stream_error')
        for text in ['usage limit', 'rate limit', 'quota', 'out of credits', 'limit reached', 'exhausted',
                     'out of extra usage'] + [f"You've hit your {kind} limit" for kind in ['session', 'weekly', 'opus', 'sonnet', 'fable']]:
            self.assertEqual(ch.inspect(text, returncode=1)['failure'], 'quota')

    def test_quota_needs_real_limit_signal(self):
        for text in ["You've hit your weekly limit", 'Claude AI usage limit reached|1761750000',
                     '5-hour limit reached ∙ resets 3pm', "You're out of extra usage"]:
            with self.subTest(text=text):
                self.assertEqual(ch.inspect(text)['failure'], 'quota')
        self.assertEqual(ch.inspect('done', stderr="You've hit your session limit")['failure'], 'quota')
        for text in ['Added backoff for rate limit and quota errors.',
                     'The pool is exhausted only on shutdown; usage limit checks pass.',
                     'Fixed early exit when the retry limit reached zero.']:
            with self.subTest(text=text):
                self.assertIsNone(ch.inspect(text)['failure'])
        self.assertIsNone(ch.inspect('done', stderr='rate limit warning')['failure'])
        self.assertEqual(ch.inspect(json.dumps({'type': 'result', 'is_error': True, 'result': 'out of credits'}))['failure'], 'quota')
        self.assertEqual(ch.inspect('quota', returncode=1)['failure'], 'quota')


class DispatchTests(unittest.TestCase):
    setUp = test_workers.WorkerTests.setUp

    def run_claude(self, outputs, **options):
        self.seen = []
        worker = options.pop('worker', None)
        codex_meter = options.pop('codex_meter', 10)
        stackwich = options.pop('stackwich', None)
        pinned = options.pop('pinned', False)
        failover_stackwich = options.pop('failover_stackwich', False)
        policy = load_policy(worker_order=['claude', 'codex'], unmetered_workers=['claude'],
            workers={'claude': {'command': ['fake-claude'], 'prompt_delivery': 'stdin',
                                **({'stackwich': stackwich} if stackwich else {})}},
            run_task={'ledger': str(self.base / 'ledger.csv'), 'shim_dir': str(self.base / 'shims'),
                      'block_log': str(self.base / 'blocks'), 'timeouts_seconds': {'claude': 100}, **options})
        if failover_stackwich:
            policy['run_task'].setdefault('stackwich', {})['codex'] = {'enabled': True, 'supported': True}
        def runner(argv, cwd, timeout, grace, **kwargs):
            self.seen.append((argv, kwargs['input_text'], timeout))
            return {'returncode': 0, 'timed_out': False, 'elapsed_seconds': 2,
                    'stdout': '', 'stderr': '', **outputs.pop(0)}
        return td.run_task(self.task, self.repo, policy=policy, runner=runner, worker=worker, _claude_pinned=pinned,
            select=lambda *a, **kw: {'eligible': ['claude', 'codex', 'cursor'], 'meters': {'codex': codex_meter}},
            output_root=self.base / 'out')

    def test_other_worker_prompts_unchanged(self):
        for name in ('codex', 'grok-build', 'cursor'):
            with self.subTest(worker=name):
                policy = load_policy(run_task={'ledger': str(self.base / 'ledger.csv'),
                    'shim_dir': str(self.base / 'shims'), 'block_log': str(self.base / 'blocks')})
                seen = []
                def runner(argv, cwd, *args, **kwargs):
                    prompt = kwargs['input_text']
                    if prompt is None:
                        prompt = next((self.repo / '.git/router-prompts').iterdir()).read_text()
                    seen.append(prompt)
                    return {'returncode': 0, 'timed_out': False, 'elapsed_seconds': 0,
                            'stdout': 'done', 'stderr': ''}
                result = td.run_task(self.task, self.repo, policy=policy, runner=runner,
                    select=lambda *a, **kw: {'eligible': [name], 'meters': {name: 10}},
                    extra_prompt='router extra', output_root=self.base / 'out')
                self.assertTrue(result['ok'])
                self.assertEqual(seen, [policy['run_task']['prompt_prefix'] + '\n\n' +
                                       self.task.read_text() + '\n\nrouter extra'])

    def test_zero_exit_quota(self):
        result = self.run_claude([{'stdout': json.dumps({'type': 'result', 'is_error': True,
            'result': "You've hit your session limit · resets 3pm"})}, {'stdout': 'done'}])
        self.assertEqual([a['worker'] for a in result['attempts']], ['claude', 'codex'])
        self.assertEqual(result['attempts'][0]['success_check'], 'quota')
        self.assertFalse(result['attempts'][0]['ok'])
        self.assertTrue(result['ok'])
        self.assertEqual(self.seen[1][1], load_policy()['run_task']['prompt_prefix'] + '\n\n' + self.task.read_text())

    def assert_exit_zero_quota_failover(self, text):
        for stdout in (text, json.dumps({'type': 'result', 'is_error': True, 'result': text})):
            with self.subTest(stdout=stdout):
                result = self.run_claude([{'stdout': stdout}, {'stdout': 'done'}])
                self.assertEqual([r['worker'] for r in result['attempts']], ['claude', 'codex'])
                first = result['attempts'][0]
                self.assertEqual(first['returncode'], 0)
                self.assertEqual(first['success_check'], 'quota')
                self.assertTrue(first['quota_error'])
                self.assertFalse(first['ok'])
                self.assertTrue(result['ok'])

    def test_weekly_limit_exit_zero(self):
        self.assert_exit_zero_quota_failover("You've hit your weekly limit")

    def test_opus_limit_exit_zero(self):
        self.assert_exit_zero_quota_failover("You've hit your Opus limit")

    def test_refusal_is_not_quota(self):
        text = "I can't help with that request."
        for stdout in (text, json.dumps({'type': 'result', 'is_error': True, 'result': text})):
            with self.subTest(stdout=stdout):
                result = self.run_claude([{'stdout': stdout}])
                self.assertEqual(len(result['attempts']), 1)
                self.assertEqual(result['attempts'][0]['success_check'], 'refusal')
                self.assertFalse(result['attempts'][0]['quota_error'])
                self.assertFalse(result['ok'])

    def test_refusal_mentioning_quota_words_stays_refusal(self):
        text = "I can't help with that: bypassing the rate limit or quota is not something I will do."
        structured = json.dumps({'type': 'result', 'stop_reason': 'refusal', 'result': 'Not doing this; usage limit evasion.'})
        for stdout in (text, structured):
            with self.subTest(stdout=stdout):
                result = self.run_claude([{'stdout': stdout}])
                self.assertEqual(len(result['attempts']), 1, 'a refusal never fails over')
                self.assertEqual(result['attempts'][0]['success_check'], 'refusal')
                self.assertFalse(result['attempts'][0]['quota_error'])

    def test_quota_failover_is_paced_and_only_once(self):
        result = self.run_claude([{'stdout': 'Usage limit reached'}], codex_meter=90)
        self.assertEqual(len(result['attempts']), 1)
        self.assertTrue(any('failover skipped' in warning for warning in result['warnings']))
        result = self.run_claude([{'stdout': "You've hit your session limit"}, {'returncode': 1, 'stdout': 'quota'}])
        self.assertEqual([r['worker'] for r in result['attempts']], ['claude', 'codex'])
        self.assertFalse(result['ok'])

    def test_refusal(self):
        result = self.run_claude([{'stdout': "I can't help with that request."}])
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual(result['attempts'][0]['success_check'], 'refusal')
        self.assertFalse(result['ok'])

    def test_stackwich_quota_fails_over_to_stackwich_worker(self):
        result = self.run_claude([{'returncode': 1, 'stderr': 'Usage limit reached'}, {'stdout': 'done'}],
                                 stackwich={'enabled': True, 'supported': True}, failover_stackwich=True)
        self.assertEqual([a['worker'] for a in result['attempts']], ['claude', 'codex'])
        self.assertEqual(result['attempts'][0]['stackwich']['status'], 'required')
        self.assertEqual(result['attempts'][0]['success_check'], 'quota')
        self.assertEqual(result['attempts'][1]['stackwich']['status'], 'required')
        self.assertIn('Stackwich verification required', self.seen[1][1])

    def test_stackwich_quota_without_stackwich_worker_does_not_fail_over(self):
        for extra in ({}, {'pinned': True, 'worker': 'claude'}):
            with self.subTest(extra=extra):
                result = self.run_claude([{'stdout': "You've hit your weekly limit"}],
                                         stackwich={'enabled': True, 'supported': True}, **extra)
                self.assertEqual(len(self.seen), 1)
                self.assertEqual([a['worker'] for a in result['attempts']], ['claude'])
                self.assertTrue(result['attempts'][0]['quota_error'])
                self.assertFalse(result['ok'])
                self.assertIn('failover skipped: codex cannot run Stackwich', result['warnings'])

    def test_mentioning_quota_words_is_not_quota(self):
        result = self.run_claude([{'stdout': 'Added backoff for rate limit and quota errors.'}])
        self.assertEqual(len(result['attempts']), 1)
        self.assertFalse(result['attempts'][0]['quota_error'])
        self.assertIsNone(result['attempts'][0]['success_check'])
        self.assertTrue(result['ok'])
        result = self.run_claude([{'returncode': 1, 'stdout': 'quota'}, {'stdout': 'done'}])
        self.assertEqual([a['worker'] for a in result['attempts']], ['claude', 'codex'])
        self.assertTrue(result['attempts'][0]['quota_error'])

    def test_forced_quota(self):
        result = self.run_claude([{'stdout': 'out of extra usage'}], worker='claude')
        self.assertEqual(len(result['attempts']), 1)
        self.assertFalse(result['ok'])

    def test_continuation(self):
        result = self.run_claude([{'stdout': '- [ ] fix bug'}, {'stdout': '- [x] fix bug'}])
        self.assertTrue(result['ok'])
        self.assertEqual(result['attempts'][0]['continuations'], 1)
        self.assertEqual(self.seen[1][2], 98)
        self.assertIn('Elapsed: 2s.', self.seen[1][1])
        self.assertIn('These checklist items remain open:\n- [ ] fix bug', self.seen[1][1])
        self.assertNotIn('--resume', self.seen[1][0])

    def test_fail_regex_stops_continuation(self):
        result = self.run_claude([{'stdout': 'FAILED: verification rejected\n- [ ] fix bug'}, {'stdout': '- [x] fix bug'}],
                                 worker='claude', success={'claude': {'fail_regex': 'FAILED'}})
        self.assertEqual(len(self.seen), 1, 'no continuation after a configured failure signal')
        self.assertEqual(result['attempts'][0]['success_check'], 'fail_regex')
        self.assertFalse(result['ok'])

    def test_caps(self):
        for configured, calls in [(None, 3), (0, 1), (100, 4)]:
            with self.subTest(configured=configured):
                options = {} if configured is None else {'claude_continuations': configured}
                result = self.run_claude([{'stdout': '- [ ] pending'}] * calls + [{'stdout': 'done'}], **options)
                self.assertEqual(len(self.seen), calls + 1)  # exhausted checklist is failed, then ordinary failover
                self.assertEqual(result['attempts'][0]['success_check'], 'checklist_open')
                self.assertEqual(result['attempts'][0]['continuations'], calls - 1)

    def test_stops(self):
        for output in [{'stdout': 'done'}, {'stdout': '- [ ] fix', 'timed_out': True},
                       {'stdout': '- [ ] fix', 'returncode': 1},
                       {'stdout': "I am unable to help\n- [ ] fix"},
                       {'stdout': "You've hit your weekly limit\n- [ ] fix"},
                       {'stdout': '- [ ] fix', 'elapsed_seconds': 100}]:
            with self.subTest(output=output):
                self.run_claude([output], worker='claude')
                self.assertEqual(len(self.seen), 1)


class JobIntegrationTests(unittest.TestCase):
    # All workers are fixture Python programs, in isolated temporary repositories.
    import test_job_dispatch as fixtures
    setUp = fixtures.JobTests.setUp
    tearDown = fixtures.JobTests.tearDown
    worker = fixtures.JobTests.worker
    job = fixtures.JobTests.job
    supported = fixtures.StackwichJobTests.supported

    def select(self, *args, **kwargs):
        return {'eligible': ['claude', 'codex'], 'meters': {'codex': 10}}

    def configure_claude(self, code):
        import sys
        self.policy['unmetered_workers'].append('claude')
        self.policy['run_task']['commands']['claude'] = [sys.executable, '-c', code]
        self.policy['run_task'].setdefault('stackwich', {})['claude'] = {'enabled': True, 'supported': True}

    def test_quota_failover_keeps_stackwich(self):
        self.configure_claude('print("Usage limit reached")')
        self.supported()
        result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'committed')
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual([r['worker'] for r in result['attempts'][0]['runs']], ['claude', 'codex'])
        self.assertEqual(result['attempts'][0]['runs'][1]['stackwich']['status'], 'required')
        self.assertEqual(result['stackwich']['status'], 'verified')

    def test_quota_without_stackwich_replacement_blocks(self):
        from pathlib import Path
        self.configure_claude('print("Usage limit reached")')
        self.worker("open('out.txt', 'w').write('done')")
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_stackwich')
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual([r['worker'] for r in result['attempts'][0]['runs']], ['claude'])
        self.assertIn('failover skipped: codex cannot run Stackwich', result['attempts'][0]['warnings'])
        self.assertFalse((Path(result['worktree']) / 'out.txt').exists())

    def test_refusal_and_terminal_quota_do_not_repeat_job(self):
        for text in ["I can't help with that request.", 'Usage limit reached']:
            with self.subTest(text=text):
                self.configure_claude('print(' + repr(text) + ')')
                result = self.job(test_cmd='true')
                self.assertFalse(result['ok'])
                self.assertEqual(len(result['attempts']), 1)
                self.assertEqual(result['attempts'][0]['runs'][0]['success_check'],
                                 'refusal' if text.startswith('I ') else 'quota')
                self.assertEqual(result['status'], 'blocked_by_stackwich')

    def correction_quota_claude(self):
        marker = self.base / 'claude-calls'
        self.configure_claude(
            'from pathlib import Path; p=Path(' + repr(str(marker)) + '); '
            'already=p.exists(); p.write_text("called"); '
            'print("Usage limit reached" if already else "done")')

    def test_correction_quota_fails_over_to_stackwich_worker(self):
        self.correction_quota_claude()
        self.supported()
        result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['status'], 'committed')
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual([r['worker'] for r in result['attempts'][1]['runs']], ['claude', 'codex'])
        self.assertEqual(result['attempts'][1]['runs'][0]['success_check'], 'quota')
        self.assertEqual(result['stackwich']['status'], 'verified')
        self.assertEqual(result['stackwich']['cycle'], 2)

    def test_correction_quota_without_stackwich_replacement_blocks(self):
        self.correction_quota_claude()
        self.worker("open('out.txt', 'w').write('done')")
        result = self.job(test_cmd='true')
        self.assertEqual(result['status'], 'blocked_by_stackwich')
        self.assertEqual(len(result['attempts']), 2)
        self.assertEqual([r['worker'] for r in result['attempts'][1]['runs']], ['claude'])
