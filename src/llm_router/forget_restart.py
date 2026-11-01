"""Forget-and-restart policy for long retry loops (#100).

Based on 'Scaling Discovery Requires Forgetting' (arXiv 2610.20563): as retry history accumulates,
agents anchor on their earlier failed attempts. After `forget_after_attempts` failed iterations
(default `2`), DLR discards the accumulated session/attempt transcript history and seeds a fresh
worker session with only:
  1. The original task,
  2. The best candidate diff produced so far (`best_diff`), and
  3. Its score (`best_score` + short test summary).
"""
import re

from . import security

DEFAULT_FORGET_AFTER = 2
_PASS_COUNT_RE = re.compile(r'(\d+)\s+pass(?:ed)?', re.I)
_FAIL_COUNT_RE = re.compile(r'(\d+)\s+fail(?:ed|ures?)?', re.I)


def score_candidate(tests=None, diff_text='', changed_paths=()):
    """Score a failed or partial candidate attempt in [0.0, 1.0] so the best diff is retained."""
    tests = tests or {}
    if tests.get('status') == 'passed' and (diff_text or changed_paths):
        return 1.0
    tail = tests.get('tail') or ''
    passed_m = _PASS_COUNT_RE.search(tail)
    failed_m = _FAIL_COUNT_RE.search(tail)
    passed = int(passed_m.group(1)) if passed_m else 0
    failed = int(failed_m.group(1)) if failed_m else 0
    if passed + failed > 0:
        ratio = passed / (passed + failed)
    else:
        ratio = 0.2 if (diff_text or changed_paths) else 0.0
    size_bonus = 0.1 if (diff_text or changed_paths) else 0.0
    return round(min(0.95, ratio * 0.85 + size_bonus), 4)


class CandidateTracker:
    """Tracks candidate attempts across a retry loop and triggers periodic forget-and-restart."""

    def __init__(self, forget_after=DEFAULT_FORGET_AFTER):
        self.forget_after = max(1, int(forget_after))
        self.candidates = []

    def record(self, attempt, worker, diff_text, changed_paths=(), tests=None):
        score = score_candidate(tests=tests, diff_text=diff_text, changed_paths=changed_paths)
        entry = {
            'attempt': int(attempt),
            'worker': worker,
            'score': score,
            'diff': security.redact(str(diff_text or '')),
            'changed_paths': sorted(set(changed_paths or ())),
            'tests_status': (tests or {}).get('status') or 'not_run',
            'tests_tail': security.redact(((tests or {}).get('tail') or '')[-1000:]),
        }
        self.candidates.append(entry)
        return entry

    def best(self):
        if not self.candidates:
            return None
        return max(self.candidates, key=lambda c: (c['score'], c['attempt']))

    def should_forget(self, failed_attempts_count):
        """True when `failed_attempts_count` reaches a positive multiple of `forget_after`."""
        count = int(failed_attempts_count or 0)
        return count >= self.forget_after and (count % self.forget_after == 0)

    def build_fresh_seed_prompt(self, handoff_instructions=''):
        """Build a clean-slate prompt seeded ONLY with the best diff and its score (history discarded)."""
        best = self.best()
        parts = []
        if handoff_instructions:
            parts.append(handoff_instructions)
        if best is None or (not best['diff'].strip() and not best['changed_paths']):
            parts.append(
                '## Fresh Restart (History Discarded)\n'
                'Earlier attempts plateaued and their history was discarded. Start fresh from the task above.'
            )
            return '\n\n'.join(parts)
        diff_block = best['diff'][:15000] if best['diff'].strip() else (
            '(untracked/new files): ' + ', '.join(best['changed_paths'])
        )
        parts.append(
            '## Fresh Restart from Best Candidate (History Discarded)\n'
            'Earlier retry history was discarded to avoid anchoring on failed trajectories.\n'
            f"- Best candidate: attempt {best['attempt']} (`{best['worker']}`), score `{best['score']}` "
            f"(tests: `{best['tests_status']}`)\n"
            f"- Changed files: {', '.join(f'`{p}`' for p in best['changed_paths']) or 'none'}\n\n"
            '### Best Candidate Diff\n'
            f"```diff\n{diff_block}\n```"
        )
        if best['tests_tail']:
            parts.append(f"### Best Candidate Test Summary\n```\n{best['tests_tail']}\n```")
        return '\n\n'.join(parts)
