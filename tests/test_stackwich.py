"""Adversarial checks for the optional evidence contract; no real workers."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from llm_router import stackwich as sw


class ReportTests(unittest.TestCase):
    def test_valid_report(self):
        self.assertEqual(sw.validate(sw.SCHEMA, {'out.txt'}, 1), [])
        report = copy.deepcopy(sw.SCHEMA)
        report['cycles'] = 2
        self.assertEqual(sw.validate(report, {'out.txt'}, 2), [])

    def test_malformed_and_missing_fields(self):
        for report in (None, [], 1, {}, {'plan': []}):
            with self.subTest(report=report):
                self.assertTrue(sw.validate(report, {'out.txt'}, 1))
        for section in ('plan', 'execute', 'verify', 'review'):
            report = copy.deepcopy(sw.SCHEMA)
            report[section] = None
            self.assertTrue(sw.validate(report, {'out.txt'}, 1))

    def test_rejection_classes(self):
        cases = [('cycles', None, True), ('cycles', None, 0), ('cycles', None, 3),
                 ('cycles', None, 2), ('plan', 'files', []), ('plan', 'steps', ['']),
                 ('plan', 'verification', []), ('plan', 'verification', [{'command': 'x'}]),
                 ('verify', 'commands_run', []), ('verify', 'gate', 'FAIL'),
                 ('verify', 'scope_check', 'FAIL'), ('review', 'verdict', 'REVISE'),
                 ('review', 'items', ['fix']), ('review', 'items', None)]
        for section, key, value in cases:
            with self.subTest(section=section, key=key, value=value):
                report = copy.deepcopy(sw.SCHEMA)
                if key is None:
                    report[section] = value
                else:
                    report[section][key] = value
                self.assertTrue(sw.validate(report, {'out.txt'}, 1))
        for code in (False, True, 1, '0', None):
            report = copy.deepcopy(sw.SCHEMA)
            report['verify']['commands_run'][0]['exit_code'] = code
            self.assertTrue(sw.validate(report, {'out.txt'}, 1))
        report = copy.deepcopy(sw.SCHEMA)
        report['verify']['commands_run'][0]['command'] = 'different'
        self.assertTrue(sw.validate(report, {'out.txt'}, 1))

    def test_paths_and_exact_scope(self):
        for path in ('/abs', '../x', 'a/../x', 'a//x', './x', 'x/', 'a\\x', '.git/config',
                     'a/.GIT/config', sw.REPORT_PATH, ''):
            with self.subTest(path=path):
                report = copy.deepcopy(sw.SCHEMA)
                report['plan']['files'] = report['execute']['changed_files'] = [path]
                self.assertTrue(sw.validate(report, {path}, 1))
        for actual in (set(), {'extra', 'out.txt'}, {'other'}):
            self.assertTrue(sw.validate(sw.SCHEMA, actual, 1))
        report = copy.deepcopy(sw.SCHEMA)
        report['plan']['files'] *= 2
        self.assertTrue(sw.validate(report, {'out.txt'}, 1))
        report = copy.deepcopy(sw.SCHEMA)
        report['execute']['changed_files'] = ['different']
        self.assertTrue(sw.validate(report, {'out.txt'}, 1))

    def test_safe_fresh_bounded_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / sw.REPORT_PATH
            path.parent.mkdir()
            path.write_text(json.dumps(sw.SCHEMA))
            self.assertEqual(sw.load(tmp)[0], sw.SCHEMA)
            sw.prepare(tmp)
            self.assertFalse(path.exists())
            self.assertEqual(sw.evidence(tmp, {'out.txt'}, 1)['status'], 'blocked')
            with self.assertRaises(ValueError):
                sw.prepare(tmp, tracked=True)
            path.symlink_to(Path(tmp) / 'elsewhere')
            with self.assertRaises(ValueError):
                sw.prepare(tmp)
            with self.assertRaises(ValueError):
                sw.load(tmp)
            path.unlink()
            path.parent.rmdir()
            path.parent.symlink_to(Path(tmp))
            with self.assertRaises(ValueError):
                sw.prepare(tmp)
            path.parent.unlink()
            path.parent.mkdir()
            path.mkdir()
            with self.assertRaises(ValueError):
                sw.prepare(tmp)
            path.rmdir()
            path.write_text('x' * (sw.MAX_REPORT_BYTES + 1))
            self.assertEqual(sw.evidence(tmp, {'out.txt'}, 1)['status'], 'blocked')
            path.write_text('{bad')
            self.assertEqual(sw.evidence(tmp, {'out.txt'}, 1)['status'], 'blocked')
