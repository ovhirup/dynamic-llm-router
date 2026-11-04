"""Versioned output contracts, one format repair, and fixture before/after counts (#237)."""
import json
from pathlib import Path
import unittest

from llm_router import stackwich
from llm_router.adapters import structured_outputs as outputs
from test_batch_runner import PASS, BatchTests
from test_job_dispatch import StackwichJobTests

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests' / 'fixtures' / 'structured_outputs' / 'batch_verdicts.json'


def _payload(record, key):
    repeat = record.get(f'{key}_repeat')
    value = record.get(key)
    if repeat:
        return 'x' * repeat
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _invalid(raw, schema, limit):
    try:
        outputs.parse_json(raw, schema, limit)
    except outputs.InvalidStructuredOutput:
        return True
    return False


class SchemaTests(unittest.TestCase):
    def test_schemas_are_versioned(self):
        for schema in (outputs.WORKER_REPORT_V1, outputs.BATCH_VERDICT_V1, outputs.UNIT_VERDICT_V1):
            self.assertEqual(schema['$schema'], 'https://json-schema.org/draft/2020-12/schema')
            self.assertIn(f':v{outputs.VERSION}', schema['$id'])
            self.assertEqual(schema['properties']['schema_version']['const'], outputs.VERSION)

    def test_malformed_partial_and_oversized_outputs_fail(self):
        cases = (
            (outputs.BATCH_VERDICT_V1, outputs.MAX_BATCH_VERDICT_BYTES,
             {'schema_version': 1, 'wrong': 0, 'crash': 0, 'nonce': 'n'}),
            (outputs.UNIT_VERDICT_V1, outputs.MAX_UNIT_VERDICT_BYTES,
             {'schema_version': 1, 'verdict': 'approve', 'notes': 'ok'}),
            (outputs.WORKER_REPORT_V1, outputs.MAX_REPORT_BYTES,
             json.loads(json.dumps(stackwich.SCHEMA))),
        )
        for schema, limit, valid in cases:
            with self.subTest(schema=schema['$id']):
                self.assertEqual(outputs.parse_json(json.dumps(valid), schema, limit)['schema_version'], 1)
                with self.assertRaises(outputs.InvalidStructuredOutput) as malformed:
                    outputs.parse_json('{', schema, limit)
                self.assertEqual(str(malformed.exception), 'malformed_json')
                partial = dict(valid)
                partial.pop(next(iter(partial)))
                with self.assertRaises(outputs.InvalidStructuredOutput) as missing:
                    outputs.parse_json(json.dumps(partial), schema, limit)
                self.assertEqual(str(missing.exception), 'schema_mismatch')
                with self.assertRaises(outputs.InvalidStructuredOutput) as huge:
                    outputs.parse_json('x' * (limit + 1), schema, limit)
                self.assertEqual(str(huge.exception), 'oversized_output')


class FixtureCountTests(unittest.TestCase):
    def test_before_and_after_invalid_verdict_counts(self):
        records = json.loads(FIXTURE.read_text())
        self.assertGreaterEqual(len(records), 6)
        before = after = 0
        for record in records:
            first = _invalid(_payload(record, 'output'), outputs.BATCH_VERDICT_V1,
                             outputs.MAX_BATCH_VERDICT_BYTES)
            if not first:
                continue
            before += 1
            if _invalid(_payload(record, 'repair'), outputs.BATCH_VERDICT_V1,
                        outputs.MAX_BATCH_VERDICT_BYTES):
                after += 1
        self.assertEqual((len(records), before, after), (6, 5, 2))
        self.assertLess(after, before)


class BatchRepairTests(BatchTests):
    def test_format_repair_does_not_spend_a_fix_round(self):
        self.verdicts['alpha'] = [{'text': 'not json'}, PASS]
        summary = self.batch()
        alpha = self.job(summary, 'alpha')
        self.assertEqual(alpha['status'], 'merged', summary)
        self.assertEqual([key for key, _prompt in self.verifier_calls if key == 'alpha'], ['alpha', 'alpha'])
        self.assertEqual(self.fixer_calls, [])

    def test_second_invalid_verdict_is_a_typed_failure(self):
        self.verdicts['alpha'] = [{'text': 'not json'}, {'text': 'still not json'}]
        summary = self.batch()
        alpha = self.job(summary, 'alpha')
        self.assertEqual(alpha['status'], 'invalid_verdict')
        self.assertEqual(len([key for key, _prompt in self.verifier_calls if key == 'alpha']), 2)
        self.assertEqual(self.fixer_calls, [])


class ReportRepairTests(unittest.TestCase):
    setUp = StackwichJobTests.setUp
    tearDown = StackwichJobTests.tearDown
    select = StackwichJobTests.select
    worker = StackwichJobTests.worker
    job = StackwichJobTests.job
    def _worker(self, repair_text):
        from llm_router import stackwich
        self.policy['run_task'].setdefault('stackwich', {})['codex'] = {'enabled': True, 'supported': True}
        code = (
            "import json,re,sys,pathlib; p=sys.stdin.read(); "
            "cycle=int(re.search(r'cycles equal to (\\d+)',p).group(1)); "
            "open('out.txt','w').write('hi\\n'); "
            "pathlib.Path('.router-stackwich').mkdir(exist_ok=True); "
            f"r={stackwich.SCHEMA!r}; r['cycles']=cycle; "
            f"text={repair_text}; "
            "pathlib.Path('.router-stackwich/report.json').write_text(text)"
        )
        self.worker(code)

    def test_invalid_report_repairs_once_inside_the_cycle(self):
        self._worker("'{' if 'Worker report format repair' not in p else json.dumps(r)")
        result = self.job(test_cmd='test -f out.txt')
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual([row['status'] for row in result['output_validation']],
                         ['invalid_report', 'report_repaired'])

    def test_second_invalid_report_blocks_without_a_product_retry(self):
        self.policy['run_job']['max_retries'] = 8
        self._worker("'{'")
        result = self.job(test_cmd='test -f out.txt')
        self.assertEqual(result['status'], 'blocked_by_stackwich', result)
        self.assertEqual(result['reason'], 'invalid_report')
        self.assertEqual(len(result['attempts']), 1)
        self.assertEqual([row['attempt'] for row in result['output_validation']], [1, 2])
