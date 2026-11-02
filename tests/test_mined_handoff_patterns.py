"""Tests for mined handoff and token-saver patterns (#97)."""
from pathlib import Path
import unittest

from llm_router import handoff

ROOT = Path(__file__).resolve().parents[1]


class MinedHandoffPatternsTests(unittest.TestCase):
    def test_routing_strategy_documents_adopted_and_skipped_patterns(self):
        doc = (ROOT / 'docs' / 'ROUTING_STRATEGY.md').read_text()
        for phrase in (
            'grok-bot-token-saver',
            'crew-contract',
            'grok-bot-profiles',
            'Adopted',
            'Skipped',
        ):
            self.assertIn(phrase, doc)

    def test_handoff_header_supports_definition_of_done_and_stopping_rule(self):
        self.assertIn('definition_of_done', handoff.HEADER_KEYS)
        self.assertIn('stopping_rule', handoff.HEADER_KEYS)
        note = {
            'job_id': 'job-97',
            'worker': 'claude',
            'source': 'worker',
            'switch_reason': 'checkpoint',
            'definition_of_done': 'pytest -q passes with 0 skipped',
            'stopping_rule': 'max 3 workers or same failure twice',
            'sections': {
                'Goal': 'Mine handoff patterns',
                'Files touched': 'docs/ROUTING_STRATEGY.md',
                'Diff summary': '1 file changed',
                'Test status': 'passed',
                'Decisions': 'Keep compact notes',
                'Next steps': 'none',
                'Gotchas': 'none',
                'Meter': '25%',
                'Switch reason': 'checkpoint',
            },
        }
        rendered = handoff.render(note)
        parsed = handoff.parse(rendered)
        self.assertEqual(parsed['definition_of_done'], 'pytest -q passes with 0 skipped')
        self.assertEqual(parsed['stopping_rule'], 'max 3 workers or same failure twice')


if __name__ == '__main__':
    unittest.main()
