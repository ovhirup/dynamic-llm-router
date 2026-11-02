import copy
import tempfile
import unittest
from pathlib import Path

from helpers import load_policy, TERM
from llm_router import brain, config

SECRET = 'sk-' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4'


class BrainOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.policy = copy.deepcopy(load_policy())
        self.policy['run_task']['private_terms_file'] = str(self.base / 'terms.txt')
        (self.base / 'terms.txt').write_text(TERM + '\n')
        self.policy['run_job']['vault_dir'] = str(self.base / 'vault')
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        handoffs = self.base / 'handoffs' / 'job-1'
        handoffs.mkdir(parents=True)
        self.note = handoffs / '20261102T000000Z-codex.md'
        self.note.write_text(f'## Goal\nShip it for {TERM}\n## Gotchas\nkey={SECRET}\n')

    def tearDown(self):
        self.tmp.cleanup()

    def summary(self, **kw):
        summary = {'job_id': 'job-1', 'repo': str(self.repo), 'worktree': str(self.base / 'wt' / 'job-1'),
                   'task': 'add-feature.md', 'status': 'committed', 'ok': True, 'workers': ['codex'],
                   'tests_status': 'passed', 'branch': 'router/add-feature', 'commit': 'abc123',
                   'started_at': '2026-11-02T00:00:00', 'finished_at': '2026-11-02T00:05:00',
                   'hops': [{'cycle': 1, 'worker': 'codex', 'reason': 'ok', 'ok': True}],
                   'test_command': 'python -m unittest', 'diff_stat': ' out.txt | 1 +',
                   'handoffs': [{'path': str(self.note), 'worker': 'codex'}],
                   'warnings': [f'{TERM} token={SECRET}']}
        summary.update(kw)
        return summary

    def test_vault_dir_resolution(self):
        self.assertEqual(brain.vault_dir(self.policy), self.base / 'vault')
        self.policy['brain'] = {'vault_dir': str(self.base / 'other')}
        self.assertEqual(brain.vault_dir(self.policy), self.base / 'other')
        default = copy.deepcopy(load_policy())
        default['run_job'].pop('vault_dir', None)
        self.assertEqual(brain.vault_dir(default), config.state_dir() / 'vault')

    def test_writes_note_with_frontmatter_sections_and_mirrors_handoffs(self):
        path = brain.write_job_outcome(self.policy, self.summary())
        self.assertEqual(path, self.base / 'vault' / 'jobs' / 'job-1.md')
        text = path.read_text()
        self.assertTrue(text.startswith('---\n'))
        front = text.split('---\n')[1]
        for key in brain.FRONTMATTER_KEYS:
            self.assertIn(f'\n{key}: ' if key != 'job_id' else 'job_id: ', front)
        self.assertIn('ok: true', front)
        self.assertIn('workers: ["codex"]', front)
        for section in ('## Summary', '## Hops', '## Tests', '## Diff stat', '## Handoffs', '## Warnings'):
            self.assertIn(section, text)
        self.assertIn('out.txt | 1 +', text)
        mirrored = self.base / 'vault' / 'handoffs' / 'job-1' / self.note.name
        self.assertTrue(mirrored.exists())
        self.assertIn('[[20261102T000000Z-codex]]', text)
        for written in (text, mirrored.read_text()):
            self.assertNotIn(SECRET, written)
            self.assertNotIn(TERM, written)
            self.assertNotIn(TERM.lower(), written.lower())
            self.assertIn('[REDACTED]', written)

    def test_refuses_vault_inside_target_repo(self):
        self.policy['run_job']['vault_dir'] = str(self.repo / 'vault')
        summary = self.summary()
        self.assertIsNone(brain.write_job_outcome(self.policy, summary))
        self.assertEqual(list(self.repo.iterdir()), [])
        self.assertTrue(any('inside the target repository' in w for w in summary['warnings']))

    def test_missing_handoff_and_empty_sections(self):
        summary = self.summary(handoffs=[{'path': str(self.base / 'gone.md')}], hops=[], warnings=[],
                               diff_stat='', commit=None, ok=False)
        text = brain.write_job_outcome(self.policy, summary).read_text()
        self.assertIn('commit: null', text)
        self.assertIn('## Warnings\n\n- none', text)


if __name__ == '__main__':
    unittest.main()
