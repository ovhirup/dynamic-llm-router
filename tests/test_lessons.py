"""Tests for per-repo lessons file (#106): writing, deduping, capping, and injecting into future jobs."""
from pathlib import Path
import unittest

from llm_router import lessons
from test_job_dispatch import JobTests


class LessonsUnitTests(unittest.TestCase):
    setUp = JobTests.setUp
    tearDown = JobTests.tearDown
    select = JobTests.select
    worker = JobTests.worker
    job = JobTests.job
    assert_main_untouched = JobTests.assert_main_untouched

    def test_write_dedup_cap_and_layer_filing(self):
        self.policy['run_job']['lessons_dir'] = str(self.base / 'lessons')
        lessons.record_lesson(
            self.policy, self.repo,
            'main-repo: run test suites one at a time; test_security uses pgrep on a shared marker.',
            layer='harness',
        )
        # Duplicate is ignored
        lessons.record_lesson(
            self.policy, self.repo,
            'main-repo: run test suites one at a time; test_security uses pgrep on a shared marker.',
            layer='harness',
        )
        loaded = lessons.load_lessons(self.policy, self.repo)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]['layer'], 'harness')

        for i in range(10):
            lessons.record_lesson(self.policy, self.repo, f'rule-{i}', layer='loop', max_lessons=5)
        capped = lessons.load_lessons(self.policy, self.repo)
        self.assertEqual(len(capped), 5)
        self.assertEqual([e['rule'] for e in capped], [f'rule-{i}' for i in range(5, 10)])

    def test_fixed_failure_writes_lesson_outside_repo_and_injects_into_next_job(self):
        self.policy['run_job']['lessons_dir'] = str(self.base / 'lessons')
        capture = self.base / 'prompts.txt'
        self.worker("import sys; p=sys.stdin.read(); open(%r,'a').write(p+'\\n=====\\n'); "
                    "open('n.txt','a').write('x\\n')" % str(capture))
        before = JobTests.setUp  # keep reference
        state_before = self.policy['run_job']['lessons_dir']
        first = self.job(test_cmd='test $(wc -l < n.txt) -ge 2')
        self.assertTrue(first['ok'], first)
        self.assertIn('lesson_learned', first)
        lesson_file = Path(first['lesson_learned']['path'])
        self.assertTrue(lesson_file.is_file())
        self.assertTrue(str(lesson_file).startswith(state_before))
        self.assertFalse((self.repo / 'lessons').exists())

        # Second job receives the recorded lesson in its prompt
        capture.unlink(missing_ok=True)
        self.worker("import sys; open(%r,'w').write(sys.stdin.read()); open('out2.txt','w').write('ok\\n')"
                    % str(capture))
        second = self.job(test_cmd='test -f out2.txt')
        self.assertTrue(second['ok'], second)
        prompt_sent = capture.read_text()
        self.assertIn('## Repo lessons (main-repo)', prompt_sent)
        self.assertIn('[loop]', prompt_sent)


if __name__ == '__main__':
    unittest.main()
