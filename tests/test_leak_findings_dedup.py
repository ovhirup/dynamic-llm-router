"""#314: cached index/worktree observations report each location and rule once."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from helpers import TERM
from llm_router import security


class CachedFindingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('GIT_CONFIG_')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_CONFIG_NOSYSTEM='1')
        self.git('init', '-q')
        self.path = self.repo / 'a.txt'

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), '-c', 'user.name=Fixture',
                               '-c', 'user.email=fixture@example.invalid', *args],
                              env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def stage_change(self, before, after):
        self.path.write_text(before)
        self.git('add', 'a.txt')
        self.git('commit', '-qm', 'fixture base')
        base = self.git('rev-parse', 'HEAD')
        self.path.write_text(after)
        self.git('add', 'a.txt')
        return security.leak_check(self.repo, base, [], [TERM], cached=True)

    def finding(self, line):
        return {'file': f'a.txt:{line}', 'kind': 'private_term', 'match': security.mask(TERM)}

    def test_added_line_observed_in_index_and_worktree_is_reported_once(self):
        result = self.stage_change('hello\n', TERM + '\n')
        self.assertEqual(result, {'status': 'blocked', 'findings': [self.finding(1)]})

    def test_distinct_locations_survive_in_first_observation_order(self):
        result = self.stage_change(TERM + '\nold\n', TERM + '\n' + TERM + '\n')
        # The new diff line is observed first, then the unchanged worktree line.
        self.assertEqual(result, {'status': 'blocked',
                                  'findings': [self.finding(2), self.finding(1)]})

    def test_distinct_rules_at_one_location_keep_their_masked_matches(self):
        planted = 'ghp_' + 'x' * 36
        result = self.stage_change('hello\n', planted + ' ' + TERM + '\n')
        self.assertEqual(result, {'status': 'blocked', 'findings': [
            {'file': 'a.txt:1', 'kind': 'github_token', 'match': security.mask(planted)},
            self.finding(1),
        ]})
