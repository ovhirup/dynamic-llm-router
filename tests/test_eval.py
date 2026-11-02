"""Evaluation harness (#62 corpus, #63 scoring, #64 runner): fixtures and fake workers only, offline."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import load_policy
from llm_router import cli, eval_corpus, eval_harness, eval_score

FIXTURES = Path(__file__).parent / 'fixtures' / 'eval'
FIRST, SECOND, REFUSED = 'repo-20261001-100000', 'repo-20261002-100000', 'repo-20261003-100000'


class EvalFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.jobs = base / 'jobs'
        shutil.copytree(FIXTURES / 'jobs', self.jobs)
        note = base / 'handoffs' / SECOND / 'note.md'
        note.parent.mkdir(parents=True)
        note.write_text('---\nworker: grok-build\n---\n\n## Goal\n# Fix the bug\nMake parse() accept spaces.\n')
        job = self.jobs / f'{SECOND}.json'
        job.write_text(job.read_text().replace('HANDOFF_PLACEHOLDER', str(note)))
        self.corpus = base / 'corpus.jsonl'
        self.policy = load_policy()
        self.capture = eval_corpus.capture(self.policy, source=self.jobs, out=self.corpus)
        self.cases = eval_corpus.load(self.corpus)
        self.by_id = {case['case_id']: case for case in self.cases}
        self.profiles = json.loads((FIXTURES / 'fake_workers.json').read_text())
        self.specs = json.loads((FIXTURES / 'policies.json').read_text())


class CorpusTests(EvalFixture):
    def test_capture_reads_every_job_and_skips_broken_logs(self):
        self.assertEqual(self.capture['cases'], 3)
        self.assertEqual(self.capture['replayable'], 2)
        self.assertEqual(self.capture['skipped'], [{'file': 'broken.json', 'why': 'JSONDecodeError'}])
        self.assertEqual([c['case_id'] for c in self.cases], [FIRST, SECOND, REFUSED])  # oldest first
        self.assertEqual(self.corpus.stat().st_mode & 0o777, 0o600)

    def test_case_keeps_replay_inputs_and_baseline_metrics(self):
        case = self.by_id[FIRST]
        self.assertTrue(case['replayable'])
        self.assertEqual(case['base_sha'], '1' * 40)
        self.assertEqual(case['task_text'], '# Add out.txt\nCreate out.txt.\n')
        self.assertEqual(case['baseline']['changed_paths'], ['out.txt', 'src/app.py'])
        self.assertEqual(case['baseline']['elapsed_seconds'], 300.0)
        self.assertEqual(case['baseline']['tokens'], 12000)
        self.assertEqual(case['baseline']['quota_percent'], 3.5)
        self.assertEqual(case['baseline']['workers'], ['codex'])

    def test_task_text_falls_back_to_handoff_goal_and_time_to_run_sum(self):
        case = self.by_id[SECOND]
        self.assertIn('Make parse() accept spaces.', case['task_text'])
        self.assertEqual(case['baseline']['elapsed_seconds'], 500.5)
        self.assertIsNone(case['baseline']['tokens'])
        self.assertEqual(case['baseline']['tests_status'], 'failed')

    def test_refused_job_is_not_replayable(self):
        case = self.by_id[REFUSED]
        self.assertFalse(case['replayable'])
        self.assertIn('task_text', case['not_replayable_because'])
        self.assertIn('status refused', case['not_replayable_because'])

    def test_limit_keeps_newest(self):
        result = eval_corpus.capture(self.policy, source=self.jobs, out=self.corpus, limit=1)
        self.assertEqual(result['cases'], 1)
        self.assertEqual(eval_corpus.load(self.corpus)[0]['case_id'], REFUSED)

    def test_diff_paths_parses_stat_and_binary_lines(self):
        stat = ' a.py | 3 ++-\n img.png | Bin 0 -> 12 bytes\n 2 files changed, 2 insertions(+)'
        self.assertEqual(eval_corpus.diff_paths(stat), ['a.py', 'img.png'])
        self.assertEqual(eval_corpus.diff_paths(''), [])


class ScoreTests(unittest.TestCase):
    def test_passing_in_scope_change_scores_full(self):
        result = eval_score.score({'tests_status': 'passed', 'changed_paths': ['a', 'b'], 'reviewer_score': 1.0},
                                  expected_paths=['a', 'b'])
        self.assertEqual(result['quality'], 1.0)
        self.assertTrue(result['passed'])

    def test_failed_tests_earn_no_scope_credit(self):
        result = eval_score.score({'tests_status': 'failed', 'changed_paths': ['a'], 'reviewer_score': 0.5},
                                  expected_paths=['a'])
        self.assertEqual(result['parts']['scope'], 0.0)
        self.assertAlmostEqual(result['quality'], 0.3 * 0.5 / 1.0)

    def test_scope_is_jaccard_against_expected_or_size_penalty(self):
        self.assertEqual(eval_score.scope_score(['a', 'b', 'c', 'd'], ['a', 'b']), 0.5)
        self.assertEqual(eval_score.scope_score(['x'] , None), 1.0)
        self.assertEqual(eval_score.scope_score([f'f{i}' for i in range(40)], None, max_files=20), 0.5)
        self.assertIsNone(eval_score.scope_score([], ['a']))

    def test_missing_parts_renormalise_weights(self):
        result = eval_score.score({'tests_status': 'passed', 'changed_paths': []})
        self.assertEqual(result['quality'], 1.0)  # only the tests part is known
        self.assertEqual(eval_score.score({'tests_status': 'skipped'})['quality'], 0.0)

    def test_aggregate_metrics(self):
        scores = [eval_score.score({'tests_status': 'passed', 'tokens': 100, 'quota_percent': 2,
                                    'elapsed_seconds': 10}),
                  eval_score.score({'tests_status': 'failed', 'tokens': 300, 'elapsed_seconds': 30})]
        metrics = eval_score.aggregate(scores)
        self.assertEqual(metrics['pass_rate'], 0.5)
        self.assertEqual(metrics['tokens_total'], 400)
        self.assertEqual(metrics['quota_percent_mean'], 2)
        self.assertEqual(metrics['elapsed_seconds_median'], 20.0)

    def test_gate_needs_a_strict_win_without_regression(self):
        base = {'cases': 2, 'pass_rate': 0.5, 'quality_mean': 0.5, 'tokens_total': 100,
                'quota_percent_mean': 2.0, 'elapsed_seconds_median': 10.0}
        self.assertFalse(eval_score.gate(base, dict(base))['adopt'])  # a tie never adopts
        self.assertEqual(eval_score.gate(base, dict(base, tokens_total=50))['reasons'], ['fewer tokens'])
        cheaper_but_worse = eval_score.gate(base, dict(base, tokens_total=50, pass_rate=0.0))
        self.assertFalse(cheaper_but_worse['adopt'])
        self.assertIn('pass rate regressed', cheaper_but_worse['reasons'])
        self.assertFalse(eval_score.gate(base, dict(base, cases=1, tokens_total=1))['adopt'])
        self.assertFalse(eval_score.gate(base, dict(base, tokens_total=None))['adopt'])  # no evidence

    def test_gate_never_adopts_when_both_sides_have_zero_pass_rate(self):
        zero_base = {'cases': 2, 'pass_rate': 0.0, 'quality_mean': 0.0, 'tokens_total': 200,
                     'quota_percent_mean': 2.0, 'elapsed_seconds_median': 30.0}
        faster_zero = dict(zero_base, tokens_total=50, quota_percent_mean=0.5, elapsed_seconds_median=2.0)
        verdict = eval_score.gate(zero_base, faster_zero)
        self.assertFalse(verdict['adopt'])
        self.assertEqual(verdict['verdict'], 'inconclusive')
        self.assertIn('0% pass rate', verdict['reasons'][0])


class OfflineHarnessTests(EvalFixture):
    def test_ab_report_adopts_cheaper_faster_candidate(self):
        report = eval_harness.run(self.cases, self.specs, profiles=self.profiles)
        self.assertEqual(report['cases'], 2)
        self.assertEqual(report['skipped'], [{'case_id': REFUSED, 'why': ['task_text', 'status refused']}])
        base, cand = (report['policies'][n]['metrics'] for n in ('baseline', 'cursor-first'))
        self.assertEqual(base['pass_rate'], 1.0)  # grok-build fails, codex picks it up
        self.assertEqual(cand['pass_rate'], 1.0)
        self.assertEqual(cand['tokens_total'], 18000)
        # case 1: fake grok-build (30000) then codex replayed from the log (12000); case 2: grok-build
        # replayed (no tokens recorded) then fake codex (15000)
        self.assertEqual(base['tokens_total'], 57000)
        self.assertTrue(report['gate']['adopt'])
        self.assertEqual(report['gate']['reasons'], ['fewer tokens', 'lower quota %', 'faster'])
        hops = [s['hops'] for s in report['policies']['baseline']['scores']]
        self.assertEqual(hops, [['grok-build', 'codex'], ['grok-build', 'codex']])

    def test_report_includes_per_case_comparison(self):
        report = eval_harness.run(self.cases, self.specs, profiles=self.profiles)
        comparison = report['comparison']
        self.assertEqual([row['case_id'] for row in comparison['cases']], [FIRST, SECOND])
        self.assertEqual(comparison['wins'] + comparison['losses'] + comparison['ties'], 2)
        self.assertEqual(comparison['deltas']['tokens'], 18000 - 57000)
        self.assertEqual(comparison['deltas']['passed'], 0)
        for row in comparison['cases']:
            self.assertTrue(row['baseline_passed'] and row['candidate_passed'])
            self.assertEqual(row['baseline_worker'], 'codex')
            self.assertEqual(row['candidate_worker'], 'cursor')
            self.assertEqual(row['tokens_delta'], row['candidate_tokens'] - row['baseline_tokens'])
            self.assertIn(row['winner'], ('candidate', 'baseline', 'tie'))
        markdown = eval_harness.render_markdown(report)
        self.assertIn('## Per-case comparison', markdown)
        self.assertIn('| metric | baseline | cursor-first | Delta |', markdown)
        self.assertIn('| tokens_total | 57000 | 18000 | -39000 |', markdown)
        self.assertIn(f'| {FIRST} | codex | yes |', markdown)
        self.assertIn(f'Candidate wins {comparison["wins"]}, losses {comparison["losses"]}', markdown)

    def test_compare_cases_counts_wins_losses_ties(self):
        def row(case_id, worker, passed, quality, tokens, elapsed):
            return {'case_id': case_id, 'worker': worker, 'passed': passed, 'quality': quality,
                    'tokens': tokens, 'elapsed_seconds': elapsed}

        base = [row('a', 'codex', False, 0.3, 100, 10.0), row('b', 'codex', True, 0.9, 100, 10.0),
                row('c', 'codex', True, 0.8, 100, 10.0), row('d', 'codex', True, 0.8, 100, 10.0),
                row('e', 'codex', True, 0.8, None, 10.0)]
        cand = [row('a', 'cursor', True, 0.8, 300, 20.0), row('b', 'cursor', True, 0.7, 50, 5.0),
                row('c', 'cursor', True, 0.8, 60, 10.0), row('d', 'cursor', True, 0.8, 100, 10.0),
                row('e', 'cursor', True, 0.8, 500, 12.5)]
        result = eval_harness.compare_cases(base, cand)
        self.assertEqual([r['winner'] for r in result['cases']],
                         ['candidate', 'baseline', 'candidate', 'tie', 'baseline'])
        self.assertEqual((result['wins'], result['losses'], result['ties']), (2, 2, 1))
        first = result['cases'][0]
        self.assertEqual((first['baseline_worker'], first['candidate_worker']), ('codex', 'cursor'))
        self.assertEqual((first['quality_delta'], first['tokens_delta'], first['elapsed_delta']), (0.5, 200, 10.0))
        self.assertIsNone(result['cases'][4]['tokens_delta'])  # unknown baseline tokens: no delta
        self.assertEqual(result['deltas'], {'passed': 1, 'quality': 0.3, 'tokens': 110, 'elapsed_seconds': 7.5})

    def test_reversed_comparison_keeps_baseline(self):
        report = eval_harness.run(self.cases, list(reversed(self.specs)), profiles=self.profiles)
        self.assertFalse(report['gate']['adopt'])
        self.assertIn('KEEP BASELINE', eval_harness.render_markdown(report))

    def test_offline_is_deterministic_and_recorded_outcomes_are_optional(self):
        profiles = {'codex': {'pass_rate': 0.5}}
        specs = [{'name': 'a', 'worker': 'codex'}, {'name': 'b', 'worker': 'codex'}]
        one = eval_harness.run(self.cases, specs, profiles=profiles, use_recorded=False)
        two = eval_harness.run(self.cases, specs, profiles=profiles, use_recorded=False)
        self.assertEqual(one, two)
        recorded = eval_harness.run(self.cases, specs, profiles=profiles)['policies']['a']['scores'][0]
        self.assertEqual(recorded['tokens'], 12000)  # codex ran FIRST: its logged outcome is replayed

    def test_pinned_worker_never_fails_over(self):
        self.assertEqual(eval_harness.order_for({'worker': 'cursor', 'worker_order': ['codex']}), ['cursor'])
        self.assertEqual(eval_harness.order_for({'worker_order': ['a', 'b', 'c', 'd'], 'never': ['b']}),
                         ['a', 'c', 'd'])

    def test_bad_specs_are_refused(self):
        for specs in ([], [{'name': 'a', 'worker': 'x'}], [{'name': 'a', 'worker': 'x'}, {'name': 'a', 'worker': 'y'}],
                      [{'name': 'a', 'worker': 'x'}, {'name': 'b'}]):
            with self.assertRaises(eval_harness.Refused):
                eval_harness.run(self.cases, specs)
        with self.assertRaises(eval_harness.Refused):
            eval_harness.run(self.cases, self.specs, mode='other')


class LiveHarnessTests(EvalFixture):
    def test_live_mode_needs_confirmation(self):
        with self.assertRaises(eval_harness.Refused):
            eval_harness.run(self.cases, self.specs, mode='live', policy=self.policy,
                             run_job=lambda *a, **k: self.fail('ran a worker'))

    def test_live_mode_replays_through_run_job(self):
        calls, readings = [], {'cursor': [10.0, 12.5], 'codex': [5.0, 5.0], 'grok-build': [1.0, 1.0]}

        def meter(worker):
            values = readings[worker]
            return values.pop(0) if len(values) > 1 else values[0]

        def fake_run_job(repo, task_file, **kwargs):
            calls.append(dict(kwargs, repo=repo, task_text=Path(task_file).read_text()))
            return {'job_id': f'live-{len(calls)}', 'workers': ['cursor'], 'tests_status': 'passed',
                    'status': 'committed', 'diff_stat': ' out.txt | 1 +\n 1 file changed',
                    'started_at': '2026-10-31T10:00:00+00:00', 'finished_at': '2026-10-31T10:01:00+00:00',
                    'attempts': [{'worker': 'cursor', 'runs': [{'worker': 'cursor', 'tokens': 700}]}]}

        specs = [{'name': 'pinned', 'worker': 'cursor'},
                 {'name': 'ordered', 'worker_order': ['cursor', 'codex'], 'config': {'run_job': {'max_retries': 0}}}]
        report = eval_harness.run(self.cases, specs, mode='live', confirm=True, policy=self.policy,
                                  run_job=fake_run_job, meter=meter)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0]['base'], '1' * 40)
        self.assertEqual(calls[0]['worker'], 'cursor')
        self.assertFalse(calls[0]['keep'])
        self.assertEqual(calls[0]['task_text'], '# Add out.txt\nCreate out.txt.\n')
        self.assertIsNone(calls[2]['worker'])
        self.assertEqual(calls[2]['policy']['worker_order'], ['cursor', 'codex'])
        self.assertEqual(calls[2]['policy']['run_job']['max_retries'], 0)
        self.assertEqual(self.policy['run_job'].get('max_retries', 1), load_policy()['run_job'].get('max_retries', 1))
        first = report['policies']['pinned']['scores'][0]
        self.assertEqual((first['tokens'], first['elapsed_seconds'], first['quota_percent']), (700, 60.0, 2.5))
        self.assertEqual(first['quota_delta_status'], 'ok')
        self.assertEqual(report['policies']['pinned']['metrics']['pass_rate'], 1.0)

    def test_unavailable_workers_excluded_from_pass_rate_and_gate_and_listed_in_report(self):
        def fake_run_job(repo, task_file, **kwargs):
            if kwargs.get('worker') == 'codex':
                return {
                    'job_id': 'unavail-1', 'workers': [], 'tests_status': None,
                    'status': 'refused', 'ok': False,
                    'warnings': ['codex meter unknown or stale: forced full-size work must be paced'],
                    'attempts': [{'attempt': 1, 'refused': 'codex meter unknown or stale'}],
                    'hops': [{'cycle': 1, 'worker': 'codex', 'reason': 'quota_error', 'ok': False, 'unavailable': True}],
                }
            return {
                'job_id': 'ok-1', 'workers': ['claude'], 'tests_status': 'passed',
                'status': 'committed', 'ok': True, 'diff_stat': ' out.txt | 1 +\n 1 file changed',
                'started_at': '2026-10-31T10:00:00+00:00', 'finished_at': '2026-10-31T10:01:00+00:00',
                'attempts': [{'worker': 'claude', 'runs': [{'worker': 'claude', 'tokens': 500}]}],
            }

        specs = [{'name': 'claude', 'worker': 'claude'}, {'name': 'codex', 'worker': 'codex'}]
        report = eval_harness.run(self.cases, specs, mode='live', confirm=True, policy=self.policy,
                                  run_job=fake_run_job, meter=lambda w: 10.0)
        codex_entry = report['policies']['codex']
        self.assertEqual(codex_entry['metrics']['cases'], 0)
        self.assertEqual(codex_entry['metrics']['unavailable'], 2)
        self.assertEqual(len(codex_entry['unavailable']), 2)
        self.assertTrue(all(s['status'] == 'unavailable' and s['quality'] is None for s in codex_entry['scores']))
        self.assertFalse(report['gate']['adopt'])
        md = eval_harness.render_markdown(report)
        self.assertIn('Unavailable workers (excluded from pass rate and gate)', md)
        self.assertIn('quota_error', md)

    def test_extracts_claude_stream_json_tokens_and_records_unknown_meter_delta_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / 'claude.log'
            log_path.write_text(
                '$ exit=0 timed_out=False elapsed=12.0s\n--- stdout\n'
                + json.dumps({'type': 'result', 'result': 'done', 'usage': {
                    'input_tokens': 100, 'output_tokens': 250,
                    'cache_read_input_tokens': 1000, 'cache_creation_input_tokens': 400,
                }}) + '\n--- stderr\n\n'
            )

            def fake_run_job(repo, task_file, **kwargs):
                return {
                    'job_id': 'claude-live-1', 'workers': ['claude'], 'tests_status': 'passed',
                    'status': 'committed', 'diff_stat': ' out.txt | 1 +\n 1 file changed',
                    'started_at': '2026-10-31T10:00:00+00:00', 'finished_at': '2026-10-31T10:00:12+00:00',
                    'attempts': [{'worker': 'claude', 'runs': [{'worker': 'claude', 'output_path': str(log_path)}]}],
                }

            specs = [{'name': 'claude', 'worker': 'claude'}, {'name': 'claude-2', 'worker': 'claude'}]
            report = eval_harness.run(self.cases, specs, mode='live', confirm=True, policy=self.policy,
                                      run_job=fake_run_job, meter=lambda w: None)
            first = report['policies']['claude']['scores'][0]
            self.assertEqual(first['tokens'], 1750)
            self.assertIsNone(first['quota_percent'])
            self.assertEqual(first['quota_delta_status'], 'unknown')
            self.assertIn('meter stale or unknown for claude', first['quota_delta_reason'])
            self.assertEqual(report['policies']['claude']['metrics']['tokens_total'], 3500)


class CliTests(EvalFixture):
    def test_eval_capture_and_run_write_the_ab_report(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['eval', 'capture', '--jobs-dir', str(self.jobs), '--out', str(self.corpus)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())['replayable'], 2)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['eval', 'run', '--policies', str(FIXTURES / 'policies.json'), '--corpus',
                             str(self.corpus), '--fake-workers', str(FIXTURES / 'fake_workers.json')])
        result = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(result['gate']['adopt'])
        self.assertIn('**Gate: ADOPT**', Path(result['report']['markdown']).read_text())
        self.assertEqual(json.loads(Path(result['report']['json']).read_text())['candidate'], 'cursor-first')

    def test_eval_run_live_without_yes_is_refused(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['eval', 'run', '--mode', 'live', '--policies', str(FIXTURES / 'policies.json'),
                             '--corpus', str(self.corpus)])
        self.assertEqual(code, 3)
        self.assertIn('--yes', json.loads(out.getvalue())['refused'])

    def test_eval_size_override_forces_draft_and_records_in_report(self):
        seen_sizes = []

        def fake_run_job(repo, task_file, **kwargs):
            seen_sizes.append(kwargs.get('size'))
            return {
                'job_id': f'draft-{len(seen_sizes)}', 'workers': ['cursor'], 'tests_status': 'passed',
                'status': 'committed', 'diff_stat': ' out.txt | 1 +\n 1 file changed',
                'started_at': '2026-10-31T10:00:00+00:00', 'finished_at': '2026-10-31T10:00:30+00:00',
                'attempts': [{'worker': 'cursor', 'runs': [{'worker': 'cursor', 'tokens': 300}]}],
            }

        report = eval_harness.run(self.cases, self.specs, mode='live', confirm=True, policy=self.policy,
                                  run_job=fake_run_job, meter=lambda w: None, size='draft')
        self.assertEqual(report['size_override'], 'draft')
        self.assertEqual(seen_sizes, ['draft', 'draft', 'draft', 'draft'])
        self.assertIn('size=draft', eval_harness.render_markdown(report))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['eval', 'run', '--size', 'draft', '--policies', str(FIXTURES / 'policies.json'),
                             '--corpus', str(self.corpus), '--fake-workers', str(FIXTURES / 'fake_workers.json')])
        self.assertEqual(code, 0)
        cli_result = json.loads(out.getvalue())
        self.assertEqual(cli_result['size_override'], 'draft')
        saved = json.loads(Path(cli_result['report']['json']).read_text())
        self.assertEqual(saved['size_override'], 'draft')


CLASSES = {'feature', 'bugfix', 'review', 'mechanical', 'draft'}


class EvalSetTests(unittest.TestCase):
    SPECS = [{'name': 'base', 'worker_order': ['grok-build', 'codex']}, {'name': 'cand', 'worker': 'codex'}]

    def test_default_eval_set_covers_every_class_and_is_replayable(self):
        cases = eval_corpus.default_eval_set()
        self.assertEqual({case['task_class'] for case in cases}, CLASSES)
        required = ('case_id', 'task_class', 'repo', 'base_sha', 'task', 'task_text', 'test_command', 'size',
                    'baseline')
        for case in cases:
            self.assertTrue(case['replayable'], case['case_id'])
            for key in required:
                self.assertTrue(case.get(key), f'{case["case_id"]} missing {key}')
        self.assertEqual(len({case['case_id'] for case in cases}), len(cases))
        bugfix = eval_corpus.filter_by_class(cases, 'bugfix')
        self.assertTrue(bugfix)
        self.assertTrue(all(case['task_class'] == 'bugfix' for case in bugfix))
        self.assertEqual(len(eval_corpus.filter_by_class(cases)), len(cases))

    def test_case_from_job_keeps_task_class(self):
        self.assertEqual(eval_corpus.case_from_job({'job_id': 'j', 'task_class': 'review'})['task_class'], 'review')
        self.assertIsNone(eval_corpus.case_from_job({'job_id': 'j'})['task_class'])

    def test_run_reports_by_class_metrics_and_filters_by_class(self):
        cases = eval_corpus.default_eval_set()
        report = eval_harness.run(cases, self.SPECS, profiles={'codex': {'pass_rate': 1.0}})
        for name in ('base', 'cand'):
            by_class = report['policies'][name]['by_class']
            self.assertEqual(set(by_class), CLASSES)
            self.assertEqual(sum(m['cases'] for m in by_class.values()), report['cases'])
        only = eval_harness.run(cases, self.SPECS, profiles={'codex': {'pass_rate': 1.0}}, task_class='review')
        expected = len(eval_corpus.filter_by_class(cases, 'review'))
        self.assertEqual(only['cases'], expected)
        self.assertEqual(only['task_class'], 'review')
        self.assertEqual(set(only['policies']['cand']['by_class']), {'review'})
        self.assertEqual(only['policies']['cand']['by_class']['review']['cases'], expected)


class EvalSetCliTests(EvalFixture):
    def test_eval_run_class_uses_packaged_eval_set(self):
        policies = Path(self.tmp.name) / 'policies.json'
        policies.write_text(json.dumps(EvalSetTests.SPECS))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(['eval', 'run', '--policies', str(policies), '--eval-set', '--class', 'mechanical',
                             '--fake-workers', str(FIXTURES / 'fake_workers.json')])
        self.assertIn(code, (0, 1))
        result = json.loads(out.getvalue())
        self.assertEqual(result['task_class'], 'mechanical')
        self.assertEqual(result['cases'], len(eval_corpus.filter_by_class(eval_corpus.default_eval_set(), 'mechanical')))
        self.assertEqual(set(result['by_class']['cand']), {'mechanical'})
        saved = json.loads(Path(result['report']['json']).read_text())
        self.assertEqual(set(saved['policies']['base']['by_class']), {'mechanical'})

    def test_unknown_class_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(['eval', 'run', '--policies', 'x.json', '--eval-set', '--class', 'nope'])


if __name__ == '__main__':
    unittest.main()
