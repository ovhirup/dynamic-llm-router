"""Strategy docs are landed and linked from README.md (issue #31)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FALLBACK = 'docs/STRATEGY_WORKERS_FALLBACK.md'
ROUTING = 'docs/ROUTING_STRATEGY.md'
ORDER = 'Claude > Codex > Grok Build > Cursor > local'


def links(text):
    return set(re.findall(r'\]\(([^)\s#]+)', text))


class StrategyDocsTest(unittest.TestCase):
    def test_docs_exist(self):
        for rel in (FALLBACK, ROUTING):
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_readme_links_both_strategy_docs(self):
        readme_links = links((ROOT / 'README.md').read_text(encoding='utf-8'))
        self.assertIn(FALLBACK, readme_links)
        self.assertIn(ROUTING, readme_links)

    def test_fallback_doc_matches_routing_strategy_order(self):
        fallback = (ROOT / FALLBACK).read_text(encoding='utf-8')
        routing = (ROOT / ROUTING).read_text(encoding='utf-8')
        self.assertIn(ORDER, fallback)
        self.assertIn('Claude (personal Claude Pro) > Codex > Grok Build > Cursor > local', routing)
        self.assertIn('ROUTING_STRATEGY.md', links(fallback))
        self.assertIn('STRATEGY_WORKERS_FALLBACK.md', links(routing))

    def test_class_table_respects_global_order(self):
        # Within each class row, the workers that appear must keep the global order (Grok Bot is out of band).
        rank = {name: i for i, name in enumerate(['Claude', 'Codex', 'Grok Build', 'Cursor', 'local'])}
        fallback = (ROOT / FALLBACK).read_text(encoding='utf-8')
        rows = [line for line in fallback.splitlines()
                if re.match(r'\| (feature|bugfix|review|mechanical|draft)', line)]
        self.assertEqual(len(rows), 5)
        for row in rows:
            cells = [c.strip() for c in row.strip('|').split('|')[1:]]
            seen = [rank[n] for c in cells for n in rank if c.startswith(n)]
            if row.startswith(('| mechanical', '| draft')):
                continue  # mechanical/draft intentionally prefer cheap or local workers (ROUTING_STRATEGY.md section 3)
            self.assertEqual(seen, sorted(seen), row)


if __name__ == '__main__':
    unittest.main()
