import json
import unittest
from unittest import mock

from helpers import TERM
from llm_router import security
import test_check


CAFE = 'caf\u00e9'


class DecodeTests(unittest.TestCase):
    def test_plain_ascii_unchanged(self):
        self.assertEqual(security.decode_git_path('b/readme.txt'), 'b/readme.txt')
        self.assertEqual(security._diff_path_name('b/readme.txt'), 'readme.txt')

    def test_unicode_octal_escapes(self):
        quoted = '"b/caf\\303\\251.txt"'
        self.assertEqual(security.decode_git_path(quoted), f'b/{CAFE}.txt')
        self.assertEqual(security._diff_path_name(quoted), f'{CAFE}.txt')

    def test_backslash_and_tab(self):
        quoted = '"b/back\\\\slash.txt"'
        self.assertEqual(security._diff_path_name(quoted), 'back\\slash.txt')
        quoted_tab = '"b/tab\\there.txt"'
        self.assertEqual(security._diff_path_name(quoted_tab), 'tab\there.txt')


class ScanDiffTests(unittest.TestCase):
    def _patch(self, path_header, body=''):
        return '\n'.join([
            f'diff --git a/x b/x',
            '--- a/x',
            f'+++ {path_header}',
            '@@ -0,0 +1 @@',
            f'+{body}',
        ])

    def test_unicode_private_term_in_quoted_path_blocks(self):
        quoted = f'"b/caf\\303\\251{TERM.lower()}.txt"'
        findings = security.scan_diff(self._patch(quoted), [TERM])
        kinds = {f['kind'] for f in findings}
        self.assertIn('private_term', kinds)
        paths = ' '.join(f['file'] for f in findings)
        self.assertNotIn(TERM, paths)
        self.assertIn('(path)', paths)

    def test_quoted_tab_path_masks_private_term(self):
        quoted = f'"b/{TERM}\\there.txt"'
        findings = security.scan_diff(self._patch(quoted, 'clean'), [TERM])
        self.assertTrue(findings)
        self.assertNotIn(TERM, json.dumps(findings))

    def test_ascii_path_still_scanned(self):
        findings = security.scan_diff(self._patch(f'b/{TERM}.txt', test_check.PLANTED_VALUE), [TERM])
        kinds = {f['kind'] for f in findings}
        self.assertIn('private_term', kinds)
        self.assertIn('openai_key', kinds)

    def test_real_top_level_a_directory_not_stripped_from_b_prefix(self):
        term = 'a/customer'
        for header in ('b/a/customer.txt', '"b/a/customer.txt"'):
            with self.subTest(header=header):
                findings = security.scan_diff(self._patch(header, 'clean'), [term])
                self.assertIn('private_term', {f['kind'] for f in findings})

    def test_real_top_level_b_directory_not_stripped_twice(self):
        term = 'b/vendor'
        for header in ('b/b/vendor.txt', '"b/b/vendor.txt"'):
            with self.subTest(header=header):
                findings = security.scan_diff(self._patch(header, 'clean'), [term])
                self.assertIn('private_term', {f['kind'] for f in findings})


class CheckQuotedPathIntegration(unittest.TestCase):
    def check_decoded_path(self, case, name, header, term):
        with mock.patch.object(security, 'decode_git_path', wraps=security.decode_git_path) as decode:
            result, stdout = case.check_json(1)
        # Only the check ran under the spy, so a direct pin cannot satisfy this.
        decode.assert_any_call(header)
        self.assertEqual(security.decode_git_path(header), f'b/{name}')
        self.assertTrue((case.target / name).is_file())
        shown = name.replace(term, security.mask(term))
        self.assertIn({'file': f'{shown} (path)', 'kind': 'private_term'},
                      result['leak_check']['findings'])
        return result, stdout

    def test_unicode_private_term_blocks_check_json(self):
        case = test_check.CheckTests('test_path_diagnostic_masks_private_term')
        case.setUp()
        self.addCleanup(case.doCleanups)
        term = CAFE
        terms = case.root / 'unicode-terms.txt'
        terms.write_text(term + '\n')
        case.policy['run_task']['private_terms_file'] = str(terms)
        (case.target / f'notes-{term}.txt').write_text('clean\n')
        result, stdout = self.check_decoded_path(
            case, f'notes-{term}.txt', '"b/notes-caf\\303\\251.txt"', term)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        blob = json.dumps(result['leak_check']['findings'], ensure_ascii=False)
        self.assertNotIn(term, stdout)
        self.assertNotIn(term, blob)
        self.assertIn(security.mask(term), blob)
        self.assertIn('private_term', blob)

    def test_quote_newline_and_backslash_names_block_check_json(self):
        paths = (
            (f'{TERM}"x.txt', f'"b/{TERM}\\"x.txt"'),
            (f'{TERM}\nx.txt', f'"b/{TERM}\\nx.txt"'),
            (f'{TERM}\\x.txt', f'"b/{TERM}\\\\x.txt"'),
            # Git leaves spaces unquoted in +++ headers.
            (f'{TERM} x.txt', f'b/{TERM} x.txt'),
        )
        for name, header in paths:
            with self.subTest(name=name):
                case = test_check.CheckTests('test_path_diagnostic_masks_private_term')
                case.setUp()
                self.addCleanup(case.doCleanups)
                (case.target / name).write_text('clean\n')
                result, stdout = self.check_decoded_path(case, name, header, TERM)
                self.assertEqual(result['leak_check']['status'], 'blocked')
                blob = json.dumps(result['leak_check']['findings'], ensure_ascii=False)
                self.assertNotIn(TERM, stdout)
                self.assertNotIn(TERM, blob)
                self.assertIn('private_term', blob)

    def test_quoted_tab_filename_private_term_blocks_check_json(self):
        # Git quotes paths containing TAB; private term must still block check --json.
        case = test_check.CheckTests('test_path_diagnostic_masks_private_term')
        case.setUp()
        self.addCleanup(case.doCleanups)
        name = f'{TERM}\there.txt'
        (case.target / name).write_text('clean\n')
        result, stdout = self.check_decoded_path(case, name, f'"b/{TERM}\\there.txt"', TERM)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertIn('private_term', {f['kind'] for f in result['leak_check']['findings']})
        self.assertNotIn(TERM, stdout)


if __name__ == '__main__':
    unittest.main()
