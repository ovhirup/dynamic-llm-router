import unittest

from llm_router import task_dag as dag


def unit(uid, scope, deps=None, workers=None):
    raw = {'id': uid, 'prompt': f'do {uid}', 'scope': scope}
    if deps is not None:
        raw['deps'] = deps
    if workers is not None:
        raw['workers'] = workers
    return raw


class ParseTests(unittest.TestCase):
    def test_normalises_scope_and_defaults(self):
        units = dag.parse_units([unit('a', ['src/b.py', 'docs/', 'src//b.py'])])
        self.assertEqual(units[0]['scope'], ['docs/', 'src/b.py'])
        self.assertEqual(units[0]['deps'], [])
        self.assertIsNone(units[0]['workers'])

    def test_rejects_bad_input(self):
        bad = [
            [],
            [unit('a', [])],
            [unit('a', ['/etc/passwd'])],
            [unit('a', ['../x'])],
            [unit('a', ['src/../x'])],
            [unit('a', ['.'])],
            [unit('A', ['x'])],
            [unit('a', ['x']), unit('a', ['y'])],
            [unit('a', ['x'], deps=['missing'])],
            [unit('a', ['x'], deps=['a'])],
            [{'id': 'a', 'prompt': ' ', 'scope': ['x']}],
            [{'id': 'a', 'prompt': 'p', 'scope': ['x'], 'extra': 1}],
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(dag.DagError):
                dag.parse_units(data)

    def test_rejects_cycle(self):
        with self.assertRaisesRegex(dag.DagError, 'cycle'):
            dag.parse_units([unit('a', ['x'], deps=['b']), unit('b', ['y'], deps=['a'])])


class OverlapTests(unittest.TestCase):
    def test_overlap_rules(self):
        self.assertTrue(dag.scopes_overlap(['src/a.py'], ['src/a.py']))
        self.assertTrue(dag.scopes_overlap(['src/'], ['src/a.py']))
        self.assertTrue(dag.scopes_overlap(['src/a.py'], ['src/']))
        self.assertTrue(dag.scopes_overlap(['src/'], ['src/']))
        self.assertTrue(dag.scopes_overlap(['src/pkg/'], ['src/pkg/']))
        self.assertFalse(dag.scopes_overlap(['src/a.py'], ['src/ab.py']))
        self.assertFalse(dag.scopes_overlap(['src/'], ['srcx/a.py']))
        self.assertFalse(dag.scopes_overlap(['src/a.py'], ['tests/a.py']))


class WaveTests(unittest.TestCase):
    def test_independent_units_share_a_wave(self):
        units = dag.parse_units([unit('a', ['src/a.py']), unit('b', ['src/b.py']), unit('c', ['tests/'])])
        self.assertEqual(dag.waves(units), [['a', 'b', 'c']])

    def test_dependencies_and_overlaps_serialise(self):
        units = dag.parse_units([
            unit('tests', ['tests/'], deps=['core']),
            unit('core', ['src/core.py']),
            unit('docs', ['docs/']),
            unit('core-docs', ['docs/core.md']),
        ])
        plan = dag.waves(units)
        self.assertEqual(plan, [['core', 'docs'], ['tests', 'core-docs']])
        by_id = {u['id']: u for u in units}
        for wave in plan:
            for i, x in enumerate(wave):
                for y in wave[i + 1:]:
                    self.assertFalse(dag.scopes_overlap(by_id[x]['scope'], by_id[y]['scope']))


class AssignTests(unittest.TestCase):
    def setUp(self):
        self.units = dag.parse_units([unit('a', ['a']), unit('b', ['b']), unit('c', ['c'])])

    def test_grok_bot_not_in_parallel_pool(self):
        self.assertNotIn('grok-bot', dag.PARALLEL_POOL)
        self.assertEqual(set(dag.PARALLEL_POOL), {'claude', 'codex', 'grok-build', 'cursor', 'local'})

    def test_pool_workers_preferred_over_grok_bot(self):
        got = dag.assign(self.units, {'grok-bot': 5, 'codex': 1, 'local': 2})
        self.assertEqual(got, {'a': 'codex', 'b': 'local', 'c': 'local'})

    def test_grok_bot_only_when_no_capable_worker_has_quota(self):
        got = dag.assign(self.units, {'grok-bot': 1, 'claude': 1, 'cursor': 0})
        self.assertEqual(got, {'a': 'claude', 'b': 'grok-bot', 'c': None})

    def test_grok_bot_when_quota_left_but_not_capable(self):
        units = dag.parse_units([unit('a', ['a'], workers=['codex', 'grok-bot'])])
        self.assertEqual(dag.assign(units, {'claude': 3, 'grok-bot': 1}), {'a': 'grok-bot'})
        self.assertEqual(dag.assign(units, {'codex': 1, 'grok-bot': 1}), {'a': 'codex'})

    def test_custom_capability(self):
        got = dag.assign(self.units, {'claude': 3, 'grok-bot': 3}, capable=lambda w, u: w != 'claude' or u['id'] == 'a')
        self.assertEqual(got, {'a': 'claude', 'b': 'grok-bot', 'c': 'grok-bot'})


if __name__ == '__main__':
    unittest.main()
