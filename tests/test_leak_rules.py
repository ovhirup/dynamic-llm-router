"""Leak-check rules: code idioms are not secrets; real secrets are still caught (fix for false positives)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import TERM
from llm_router import security
from test_task_dispatch import repo

FAKE_OPENAI = 'sk-proj-' + 'A1b2C3d4E5f6G7h8I9j0K1l2'
FAKE_GH = 'ghp_' + 'abcdefghijklmnopqrstuvwxyz0123456789'
FAKE_AWS = 'AKIA' + 'ABCDEFGHIJKLMNOP'

NOT_SECRETS = [
    '<li key={item.id}>{item.name}</li>',
    "<Row key={`row-${index}`} />",
    'if (event.key === "Enter") submit()',
    "const handler = (e) => e.key === 'Escape' && close()",
    'coder.encode(name, forKey: .name)',
    'try container.decode(String.self, forKey: .token)',
    'defaults.set(value, forKey: "AUTH_TOKEN")',
    'enum CodingKeys: String, CodingKey { case token }',
    'let key = "theme"',
    'var key = cacheKey(for: url)',
    'const token = useToken()',
    'password: string;',
    'token = os.environ["GITHUB_TOKEN"]',
    'API_KEY=${API_KEY}',
    'SECRET_TOKEN: ${{ secrets.SECRET_TOKEN }}',
    'MAX_TOKENS=4096',
    'apiKey: process.env.OPENAI_API_KEY',
    'let apiKey = "short"',
    'SECRET_FLAGS = re.compile(r"^--token$")',
    'API_TOKEN = load_token(path)',
]
SECRETS = [
    ('OPENAI_API_KEY=' + 'abcd1234efgh5678', 'env_assignment'),
    ('export DB_PASSWORD="' + 'hunter2hunter2"', 'env_assignment'),
    ('"api_key": "' + 'zq8f7a6s5d4f3g2h1j"', 'quoted_secret'),
    ("password = '" + "correct-horse-battery'", 'quoted_secret'),
    ('client_secret: "' + '9f8e7d6c5b4a39281"', 'quoted_secret'),
    ('let apiKey = "' + 'abcd1234efgh5678"', 'quoted_secret'),
    ('<Item key={id} api_key="' + 'zq8f7a6s5d4f3g2h1j" />', 'quoted_secret'),
    (f'const k = "{FAKE_OPENAI}"', 'openai_key'),
    (f'<Foo key={{x}} token="{FAKE_GH}" />', 'github_token'),
    (f'aws = {FAKE_AWS}', 'aws_access_key'),
    ('Authorization: Bearer ' + 'abcdefghijklmnopqrstuvwx', 'bearer_token'),
]


class RuleTests(unittest.TestCase):
    def test_code_idioms_are_not_flagged(self):
        for line in NOT_SECRETS:
            with self.subTest(line=line):
                self.assertEqual([k for k, _, _ in security.find_secrets(line)], [])

    def test_real_secrets_still_flagged(self):
        for line, kind in SECRETS:
            with self.subTest(line=line):
                kinds = [k for k, _, _ in security.find_secrets(line)]
                self.assertIn(kind, kinds)
                self.assertNotIn(line.split('=')[-1].strip('"\' ;/>'), security.redact(line)) if kind != 'bearer_token' else None

    def test_user_allow_regex_only_suppresses_name_rules(self):
        allow = security.compile_allow([r'TEST_FIXTURE_KEY'])
        self.assertEqual(list(security.find_secrets('TEST_FIXTURE_KEY=' + 'abcd1234', allow)), [])
        self.assertTrue(list(security.find_secrets(f'TEST_FIXTURE_KEY={FAKE_GH}', allow)))


class SelfScanTests(unittest.TestCase):
    def test_package_source_and_tests_scan_clean(self):
        # The repo must pass its own publish check: fixtures are built at runtime, never stored as literals.
        root = Path(__file__).resolve().parent.parent
        files = [p for d in ('src', 'tests', 'docs', 'examples') for p in (root / d).rglob('*')
                 if p.is_file() and p.suffix in ('.py', '.json', '.md', '.txt', '.plist', '.mmd')]
        files += [root / n for n in ('README.md', 'pyproject.toml') if (root / n).exists()]
        self.assertTrue(files)
        found = [(str(p.relative_to(root)), hit) for p in files
                 for hit in security.scan_text(p.read_text(errors='replace'), [], ())]
        self.assertEqual(found, [])


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = repo(Path(self.tmp.name) / 'r')
        self.head = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD'],
                                   capture_output=True, text=True).stdout.strip()

    def test_ignored_and_vendor_dirs_are_skipped_but_real_files_scanned(self):
        (self.repo / '.gitignore').write_text('ignored/\n')
        for folder in ('node_modules/pkg', 'ignored', '.venv/lib', 'src'):
            (self.repo / folder).mkdir(parents=True)
        (self.repo / 'node_modules/pkg/index.js').write_text(f'const t = "{FAKE_GH}"\n')
        (self.repo / '.venv/lib/x.py').write_text(f'T = "{FAKE_GH}"\n')
        (self.repo / 'ignored/notes.txt').write_text(f'{TERM}\n')
        (self.repo / 'src/App.tsx').write_text('export const A = () => items.map(i => <li key={i.id}>{i.name}</li>)\n')
        (self.repo / 'src/Model.swift').write_text('enum CodingKeys: String, CodingKey { case id }\nlet key = "k"\n')
        self.assertEqual(sorted(security.untracked_files(self.repo)), ['.gitignore', 'src/App.tsx', 'src/Model.swift'])
        result = security.leak_check(self.repo, self.head, None, [TERM])
        self.assertEqual(result, {'status': 'clean', 'findings': []})
        (self.repo / 'src/config.ts').write_text(f'export const token = "{FAKE_GH}"\n')
        blocked = security.leak_check(self.repo, self.head, None, [TERM])
        self.assertEqual([(f['file'], f['kind']) for f in blocked['findings']], [('src/config.ts:1', 'github_token')])

    def test_scan_tree_reports_terms_in_content_and_paths(self):
        (self.repo / f'{TERM}-notes.md').write_text('hello\n')
        (self.repo / 'a.txt').write_text(f'built for {TERM.lower()}\n')
        result = security.scan_tree(self.repo, [TERM])
        files = sorted(f['file'] for f in result['findings'])
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('a.txt:1', files)
        self.assertTrue(any(f.endswith('(path)') for f in files))
        self.assertNotIn(TERM, str(result))


if __name__ == '__main__':
    unittest.main()
