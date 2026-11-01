"""Fail-first check for bug tasks (#104): reproducing test must be red on base, green after fix.

When a task is classified as a bug fix (`task_class == 'bugfix'` or the task file declares
`type: bug` / `type: bugfix` or a `# Fix bug:` / `# Bug:` title when `fail_first` is enabled),
the router verifies that:
  1. At least one test file (`tests/**` or `test_*.py` / `*_test.*`) was added or updated,
  2. With only the test changes applied on top of `base_sha` (product files restored to `base_sha`),
     the test command FAILS (`base_status == 'failed'`, red on base), and
  3. With the full fix restored, the test command PASSES (`after_status == 'passed'`, green after).
If the reproducing test already passes on base, or no reproducing test was added/changed, the job is
flagged `'check_proves_nothing'` and cannot pass.
"""
from pathlib import Path
import re
import shutil
import tempfile

_TYPE_BUG = re.compile(r'^\s*\*{0,2}type\*{0,2}\s*:\s*bug(?:fix)?\b', re.I | re.M)
_TITLE_BUG = re.compile(r'^\s*#\s*(?:bug(?:fix)?\b|fix\s+bug\b)', re.I)


def is_bug_task(task_text, task_class=None, policy=None):
    """True when the task explicitly opts into fail-first bug verification."""
    if _TYPE_BUG.search(task_text or ''):
        return True
    first = next((line for line in (task_text or '').splitlines() if line.strip()), '')
    if _TITLE_BUG.match(first):
        return True
    if task_class == 'bugfix' and ((policy or {}).get('run_job') or {}).get('fail_first_bugfix', False):
        return True
    return False


def is_test_path(path):
    parts = Path(path).parts
    name = Path(path).name.lower()
    return ('tests' in parts or 'test' in parts
            or name.startswith('test_') or name.endswith(('_test.py', '.test.js', '.test.ts', 'tests.swift')))


def split_test_and_product_paths(changed_paths):
    tests = sorted(p for p in (changed_paths or ()) if is_test_path(p))
    product = sorted(p for p in (changed_paths or ()) if not is_test_path(p))
    return tests, product


def verify_fail_first(policy, worktree, base_sha, worker_paths, test_cmd, test_timeout,
                      log_path, run_tests_fn, git_fn):
    """Run the new/updated tests against `base_sha` product code, then restore the fix.

    Returns {'ok', 'status', 'reason'?, 'test_paths', 'product_paths', 'base_tests'}.
    """
    worktree = Path(worktree)
    test_paths, product_paths = split_test_and_product_paths(worker_paths)
    if not test_paths:
        return {
            'ok': False,
            'status': 'check_proves_nothing',
            'reason': 'check proves nothing: bug task must add or update a reproducing test',
            'test_paths': [],
            'product_paths': product_paths,
            'base_tests': None,
        }
    tracked = set(filter(None, git_fn(worktree, 'ls-tree', '-r', '--name-only', '-z', base_sha).stdout.split('\0')))
    with tempfile.TemporaryDirectory(prefix='llm-router-failfirst-') as backup_dir:
        backup = Path(backup_dir)
        saved_product = {}
        for rel in product_paths:
            src = worktree / rel
            if src.is_symlink():
                saved_product[rel] = ('symlink', src.readlink())
                src.unlink()
            elif src.is_file():
                dst = backup / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                saved_product[rel] = ('file', dst)
                src.unlink()
            else:
                saved_product[rel] = ('deleted', None)
            if rel in tracked:
                git_fn(worktree, 'checkout', base_sha, '--', rel)
        try:
            base_tests = run_tests_fn(policy, worktree, test_cmd, test_timeout, log_path)
        finally:
            for rel, (kind, payload) in saved_product.items():
                dest = worktree / rel
                if dest.exists() or dest.is_symlink():
                    dest.unlink()
                if kind == 'file':
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(payload, dest)
                elif kind == 'symlink':
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.symlink_to(payload)
    if base_tests.get('status') == 'passed':
        return {
            'ok': False,
            'status': 'check_proves_nothing',
            'reason': 'check proves nothing: reproducing test already passed on base commit',
            'test_paths': test_paths,
            'product_paths': product_paths,
            'base_tests': base_tests,
        }
    return {
        'ok': True,
        'status': 'red_on_base',
        'test_paths': test_paths,
        'product_paths': product_paths,
        'base_tests': base_tests,
    }
