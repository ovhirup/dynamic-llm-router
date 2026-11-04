"""Tests for shared agent map (`AGENTS.md`, `CLAUDE.md`, `docs/decisions.md`, `docs/learnings.md`) (#133)."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from llm_router import agent_map, cli
from test_job_dispatch import JobTests, git

ROOT = Path(__file__).resolve().parents[1]


class AgentMapTests(unittest.TestCase):
    setUp = JobTests.setUp
    tearDown = JobTests.tearDown
    select = JobTests.select
    worker = JobTests.worker
    job = JobTests.job

    def test_repo_map_decisions_and_learnings_files_exist_and_are_concise(self):
        for rel in ('AGENTS.md', 'CLAUDE.md', 'docs/decisions.md', 'docs/learnings.md'):
            path = ROOT / rel
            self.assertTrue(path.is_file(), rel)
            text = path.read_text()
            self.assertGreater(len(text), 40)
            self.assertLess(len(text), 4000)
        self.assertIn('AGENTS.md', (ROOT / 'CLAUDE.md').read_text())

    def test_init_map_cli_scaffolds_files_without_overwriting_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(['init-map', tmp])
            self.assertEqual(code, 0)
            res = json.loads(out.getvalue())
            self.assertEqual(len(res['written']), 4)
            (Path(tmp) / 'AGENTS.md').write_text('custom map\n')

            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                cli.main(['init-map', tmp])
            res2 = json.loads(out2.getvalue())
            self.assertEqual(len(res2['skipped_existing']), 4)
            self.assertEqual((Path(tmp) / 'AGENTS.md').read_text(), 'custom map\n')

    def test_workers_receive_agent_map_in_run_job_prompt(self):
        agent_map.init_map(self.repo)
        shared_memory = (ROOT / 'AGENTS.md').read_text().strip()
        (self.repo / 'AGENTS.md').write_text(shared_memory + '\n')
        git(self.repo, 'add', '.')
        git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost',
            'commit', '-qm', 'add agent map')
        capture = self.base / 'prompt.txt'
        self.worker("import sys; open(%r,'w').write(sys.stdin.read()); open('out.txt','w').write('ok\\n')"
                    % str(capture))
        result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        prompt = capture.read_text()
        self.assertIn('## Repository Agent Map', prompt)
        self.assertIn('### `AGENTS.md`', prompt)
        self.assertIn('## Decisions', shared_memory)
        self.assertIn('## Open', shared_memory)
        self.assertEqual(prompt.count(shared_memory), 1)
        self.assertIn('### `docs/decisions.md`', prompt)
        self.assertIn('### `docs/learnings.md`', prompt)


if __name__ == '__main__':
    unittest.main()
