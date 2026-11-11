import json
import unittest

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


class CheckQuotedPathIntegration(unittest.TestCase):
    def test_quoted_tab_filename_private_term_blocks_check_json(self):
        # Git quotes paths containing TAB; private term must still block check --json.
        case = test_check.CheckTests('test_path_diagnostic_masks_private_term')
        case.setUp()
        self.addCleanup(case.doCleanups)
        name = f'{TERM}\there.txt'
        (case.target / name).write_text('clean\n')
        result, stdout = case.check_json(1)
        self.assertEqual(result['leak_check']['status'], 'blocked')
        self.assertIn('private_term', {f['kind'] for f in result['leak_check']['findings']})
        self.assertNotIn(TERM, stdout)


if __name__ == '__main__':
    unittest.main()
