import json
import tempfile
import unittest
from pathlib import Path

from helpers import load_policy
from llm_router import claude_account as ca
from llm_router import selection


class ClaudeAccountTests(unittest.TestCase):
    def test_read_profile_only_non_secret_fields(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'c.json'
            p.write_text(json.dumps({'oauthAccount': {'emailAddress': 'Me@Gmail.com', 'organizationType': 'claude_pro',
                                                      'accessToken': 'nope'}}))
            self.assertEqual(ca.read_profile(p), {'email': 'me@gmail.com', 'org_type': 'claude_pro'})
            p.write_text('not json')
            self.assertIsNone(ca.read_profile(p))
        self.assertIsNone(ca.read_profile(Path('/nonexistent/x.json')))

    def test_classify(self):
        self.assertEqual(ca.classify({'email': 'a@gmail.com'}), 'personal')
        self.assertEqual(ca.classify({'email': 'a@corp.example'}), 'work')
        self.assertEqual(ca.classify({'email': 'a@corp.example'}, {'personal_emails': ['A@corp.example']}), 'personal')
        self.assertEqual(ca.classify({'email': 'a@gmail.com'}, {'personal_domains': ['me.dev']}), 'work')
        self.assertEqual(ca.classify(None), 'unknown')
        self.assertEqual(ca.classify({'email': None}), 'unknown')

    def test_check_off_by_default(self):
        self.assertIsNone(ca.check({}, lambda: {'email': 'a@corp.example'}))

    def test_selection_marks_claude_ineligible(self):
        policy = load_policy()
        policy['worker_order'] = ['claude']
        policy['unmetered_workers'] = ['claude']
        policy.setdefault('quota', {}).pop('claude', None)
        policy['claude_account_check'] = {'enabled': True}
        for profile, reason in (({'email': 'a@corp.example'}, 'claude_account_work'),
                                (None, 'claude_account_unknown')):
            out = selection.select(10, policy=policy, io={}, claude_profile_loader=lambda p=profile: p)
            self.assertIsNone(out['worker'])
            self.assertEqual(out['reasons']['claude'], reason)
        out = selection.select(10, policy=policy, io={}, claude_profile_loader=lambda: {'email': 'a@gmail.com'})
        self.assertNotIn(out['reasons'].get('claude'), ('claude_account_work', 'claude_account_unknown'))


if __name__ == '__main__':
    unittest.main()
