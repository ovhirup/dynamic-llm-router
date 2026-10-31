import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from helpers import load_policy
from llm_router import claude_handoff, handoff
from llm_router import task_dispatch as td
import test_job_dispatch  # never bind JobTests here: unittest would collect and re-run it
from test_job_dispatch import git
from test_task_dispatch import repo

VALID = {'worker': 'codex', 'sections': {
    'Goal': 'Create out.txt', 'Files touched': '- out.txt', 'Diff summary': 'out.txt | 1 +',
    'Test status': 'passed', 'Decisions': 'plain text file', 'Next steps': 'none, done',
    'Gotchas': 'none', 'Meter': '40%', 'Switch reason': 'checkpoint'}}


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.policy = load_policy()
        self.policy['run_job']['handoffs_dir'] = str(self.base / 'handoffs')

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_parse_round_trip(self):
        note = dict(VALID, job_id='job-1', source='worker', switch_reason='quota_error', branch='router/x')
        text = handoff.render(note)
        self.assertTrue(text.startswith('---\njob_id: job-1\n'))
        for name in handoff.SECTIONS:
            self.assertIn(f'## {name}\n', text)
        parsed = handoff.parse(text)
        self.assertEqual(parsed['sections'], VALID['sections'])
        self.assertEqual((parsed['job_id'], parsed['switch_reason'], parsed['branch']),
                         ('job-1', 'quota_error', 'router/x'))
        self.assertEqual(handoff.files_listed(parsed), ['out.txt'])

    def test_path_layout_and_read_latest(self):
        directory = handoff.job_dir(self.policy, 'job-1')
        self.assertEqual(directory, self.base / 'handoffs' / 'job-1')
        first = handoff.write_note(directory, VALID, now=datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))
        later = dict(VALID, worker='grok-build')
        second = handoff.write_note(directory, later, now=datetime(2026, 10, 31, 12, 5, tzinfo=timezone.utc))
        self.assertEqual(first.name, '20261031T120000000000Z-codex.md')
        self.assertEqual(second.name, '20261031T120500000000Z-grok-build.md')
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        latest = handoff.read_latest(directory)
        self.assertEqual((latest['worker'], latest['path']), ('grok-build', str(second)))
        self.assertIsNone(handoff.read_latest(self.base / 'missing'))

    def test_default_dir_is_under_the_state_dir(self):
        from llm_router import config
        self.assertEqual(handoff.job_dir(load_policy(), 'j'), config.state_dir() / 'handoffs' / 'j')

    def test_validate_note(self):
        self.assertEqual(handoff.validate_note(VALID, {'out.txt'}), [])
        self.assertEqual(handoff.validate_note(None, set()), ['note missing'])
        empty = {'sections': dict(VALID['sections'], **{'Next steps': '  '})}
        self.assertIn("section 'Next steps' missing or empty", handoff.validate_note(empty, {'out.txt'}))
        self.assertIn("file 'out.txt' not in the diff", handoff.validate_note(VALID, {'other.txt'}))

    def test_invalid_worker_note_falls_back_to_router_note(self):
        checkout = repo(self.base / 'r')
        base_sha = git(checkout, 'rev-parse', 'HEAD')
        (checkout / 'a.txt').write_text('a\n')
        (checkout / handoff.NOTE_FILE).write_text('## Goal\nsomething\n')  # missing sections
        log = self.base / 'tests.log'
        log.write_text('lots of output\nAssertionError: LASTFAILMARK\n')
        worker_note = handoff.collect_worker_note(checkout)
        self.assertFalse((checkout / handoff.NOTE_FILE).exists())  # never left in the tree
        note, path = handoff.checkpoint(self.policy, 'job-2', checkout, base_sha, 'codex', 'timeout',
                                        {'a.txt'}, worker_note=worker_note, test_status='failed',
                                        test_log=log, meter=72)
        self.assertEqual(note['source'], 'router')
        stored = handoff.parse(path.read_text())
        self.assertEqual((stored['source'], stored['switch_reason']), ('router', 'timeout'))
        self.assertEqual(handoff.validate_note(stored, {'a.txt'}), [])
        self.assertIn('a.txt', stored['sections']['Files touched'])
        self.assertIn('LASTFAILMARK', stored['sections']['Test status'])
        self.assertEqual(stored['sections']['Meter'], '72%')
        self.assertIn('Worker note rejected', stored['sections']['Gotchas'])

    def test_valid_worker_note_is_kept(self):
        checkout = repo(self.base / 'r')
        (checkout / 'out.txt').write_text('x\n')
        note, _ = handoff.checkpoint(self.policy, 'job-3', checkout, git(checkout, 'rev-parse', 'HEAD'),
                                     'codex', 'stalled', {'out.txt'}, worker_note=VALID)
        self.assertEqual((note['source'], note['sections']['Switch reason']), ('worker', 'stalled'))
        self.assertEqual(VALID['sections']['Switch reason'], 'checkpoint')  # caller's note not mutated

    def test_threshold(self):
        self.assertTrue(handoff.threshold_crossed(self.policy, 85))
        self.assertFalse(handoff.threshold_crossed(self.policy, 84.9))
        self.assertFalse(handoff.threshold_crossed(self.policy, None))
        self.policy['run_job']['handoff_threshold_percent'] = 60
        self.assertTrue(handoff.threshold_crossed(self.policy, 61))

    def test_render_for_prompt(self):
        text = handoff.render_for_prompt(dict(VALID, switch_reason='quota_error', source='router'))
        self.assertIn('same worktree and branch', text)
        self.assertIn('switch reason: quota_error', text)
        self.assertIn('## Next steps', text)
        self.assertEqual(handoff.render_for_prompt(None), '')

    def test_claude_handoff_compatibility(self):
        for name in ('build_prompt', 'clean', 'effort', 'inspect', 'continuation_limit'):
            self.assertIs(getattr(handoff, name), getattr(claude_handoff, name))
        prompt = claude_handoff.build_prompt('Do it. Think step by step.', 'medium', 60)
        self.assertIn('Effort: this run is medium', prompt)
        self.assertNotIn('step by step', prompt)
        stream = json.dumps({'type': 'result', 'result': 'done\n- [ ] more'})
        self.assertEqual(claude_handoff.inspect(stream)['open_items'], ['- [ ] more'])


class JobHandoffTests(unittest.TestCase):
    _jobs = vars(test_job_dispatch.JobTests)
    setUp, tearDown = _jobs['setUp'], _jobs['tearDown']
    select, select_all, worker, job = _jobs['select'], _jobs['select_all'], _jobs['worker'], _jobs['job']
    del _jobs

    def notes(self, result):
        return [handoff.parse(Path(item['path']).read_text()) for item in result['handoffs']]

    def test_note_written_before_every_failover_hop(self):
        self.policy['run_job']['handoffs_dir'] = str(self.base / 'handoffs')
        capture = self.base / 'grok.txt'
        self.worker("import sys; print('Usage limit reached', file=sys.stderr); sys.exit(1)")
        self.worker("import sys, os, glob, subprocess; "
                    "notes = glob.glob(%r + '/*/*.md'); "
                    "branch = subprocess.run(['git', 'branch', '--show-current'], capture_output=True, text=True).stdout.strip(); "
                    "open(%r, 'w').write(open(sys.argv[1]).read() + '\\nNOTES=%%d\\nCWD=%%s\\nBRANCH=%%s' %% "
                    "(len(notes), os.getcwd(), branch)); open('out.txt', 'w').write('hi\\n')"
                    % (str(self.base / 'handoffs'), str(capture)), 'grok-build')
        result = self.job(test_cmd='test -f out.txt', select=self.select_all)
        self.assertTrue(result['ok'], result)
        seen = capture.read_text()
        self.assertIn('NOTES=1', seen)  # codex's note existed before grok-build started
        self.assertIn(f'CWD={Path(result["worktree"]).resolve()}', seen)  # same worktree
        self.assertIn(f'BRANCH={result["branch"]}', seen)  # same branch, no fresh checkout
        self.assertIn('Handoff: you continue a job started by codex', seen)
        self.assertIn('switch reason: quota_error', seen)
        self.assertIn(handoff.NOTE_FILE, seen)  # every worker is told to write its note
        first = self.notes(result)[0]
        self.assertEqual((first['worker'], first['switch_reason'], first['source']),
                         ('codex', 'quota_error', 'router'))
        self.assertEqual(Path(result['handoffs'][0]['path']).parent, self.base / 'handoffs' / result['job_id'])

    def test_every_hop_gets_a_note_when_all_workers_fail(self):
        result = self.job(test_cmd='true', select=self.select_all)
        self.assertFalse(result['ok'])
        hops = [(n['worker'], n['source']) for n in self.notes(result)]
        self.assertEqual(hops, [('codex', 'router'), ('grok-build', 'router')])  # cursor is the last stop

    def test_retry_cycle_reuses_worktree_and_passes_the_note(self):
        cwds, prompts = [], []
        def run_task(task, cwd, **kwargs):
            cwds.append(Path(cwd))
            prompts.append(kwargs['extra_prompt'])
            return td.run_task(task, cwd, **kwargs)
        self.worker("open('n.txt','a').write('x\\n')")
        result = self.job(test_cmd='test $(wc -l < n.txt) -ge 2', run_task=run_task)
        self.assertTrue(result['ok'], result)
        self.assertEqual(cwds, [Path(result['worktree'])] * 2)
        self.assertIn(handoff.NOTE_FILE, prompts[0])
        self.assertIn('Handoff: you continue a job started by codex', prompts[1])
        self.assertIn('switch reason: tests failed', prompts[1])
        latest = handoff.read_latest(Path(result['handoffs'][-1]['path']).parent)
        self.assertEqual((latest['source'], latest['branch']), ('router', result['branch']))
        self.assertIn('n.txt', latest['sections']['Files touched'])

    def test_worker_note_collected_and_never_committed(self):
        body = handoff.render(VALID).replace('\n', '\\n')
        self.worker("open('out.txt','w').write('hi\\n'); open(%r,'w').write('%s')" % (handoff.NOTE_FILE, body))
        result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        committed = git(self.repo, 'show', '--name-only', '--format=', result['commit']).split()
        self.assertEqual(committed, ['out.txt'])
        self.assertFalse((Path(result['worktree']) / handoff.NOTE_FILE).exists())
        [note] = self.notes(result)
        self.assertEqual((note['source'], note['switch_reason']), ('worker', 'checkpoint'))
        self.assertNotIn(str(Path(result['worktree'])), result['handoffs'][0]['path'].rsplit('/', 1)[0])

    def test_meter_threshold_triggers_a_note(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='test -f out.txt', meter=lambda name: 91)
        self.assertTrue(result['ok'], result)
        [note] = self.notes(result)
        self.assertEqual((note['worker'], note['switch_reason']), ('codex', 'meter_threshold 91%'))
        self.assertEqual(note['sections']['Meter'], '91%')
        self.assertTrue(any('meter at 91%' in w for w in result['warnings']), result['warnings'])

    def test_below_threshold_writes_no_note_on_clean_success(self):
        self.worker("open('out.txt','w').write('hi\\n')")
        result = self.job(test_cmd='test -f out.txt', meter=lambda name: 50)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['handoffs'], [])


if __name__ == '__main__':
    unittest.main()
