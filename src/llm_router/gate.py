"""Repository quality gate (DLR #216): configured lint/type checks, then pytest with 0 skipped."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

def _count(label: str, line: str) -> int:
    match = re.search(rf'(\d+) {re.escape(label)}', line)
    return int(match.group(1)) if match else 0


def parse_pytest_summary(output: str) -> dict:
    """Extract passed/failed/skipped/subtests from pytest's final summary line."""
    counts = {'passed': 0, 'failed': 0, 'skipped': 0, 'subtests': 0, 'errors': 0}
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not re.search(r'\d+ (passed|failed|skipped|error)', line):
            continue
        counts['subtests'] = _count('subtests passed', line)
        without_subtests = re.sub(r'\d+ subtests passed,?\s*', '', line)
        counts['passed'] = _count('passed', without_subtests)
        counts['failed'] = _count('failed', line)
        counts['skipped'] = _count('skipped', line)
        counts['errors'] = _count('error', line)
        return counts
    return counts


def evaluate_pytest(exit_code: int, output: str) -> dict:
    summary = parse_pytest_summary(output)
    reasons = []
    if exit_code != 0:
        reasons.append(f'pytest exit {exit_code}')
    if summary['failed'] or summary['errors']:
        reasons.append(f"{summary['failed']} failed, {summary['errors']} errors")
    if summary['skipped']:
        reasons.append(f"{summary['skipped']} skipped")
    if summary['passed'] == 0 and summary['skipped'] == 0 and exit_code == 0:
        reasons.append('no tests reported passed')
    ok = not reasons
    return {'ok': ok, 'reasons': reasons, **summary}


def configured_lint_commands(repo_root: Path) -> list[list[str]]:
    """Return argv lists for linters declared in pyproject.toml [tool.*], if the binary exists."""
    pyproject = repo_root / 'pyproject.toml'
    if not pyproject.is_file():
        return []
    try:
        text = pyproject.read_text()
    except OSError:
        return []
    cmds: list[list[str]] = []
    if '[tool.ruff' in text and shutil.which('ruff'):
        cmds.append(['ruff', 'check', str(repo_root)])
    if '[tool.mypy' in text and shutil.which('mypy'):
        cmds.append(['mypy', str(repo_root / 'src')])
    if '[tool.pyright' in text and shutil.which('pyright'):
        cmds.append(['pyright', str(repo_root)])
    return cmds


def _run_step(argv: list[str], cwd: Path, env: dict) -> dict:
    proc = subprocess.run(
        argv, cwd=str(cwd), env=env, capture_output=True, text=True, errors='replace',
    )
    combined = (proc.stdout or '') + (proc.stderr or '')
    return {'argv': argv, 'returncode': proc.returncode, 'output': combined}


def pytest_command(repo_root: Path, target: Path | None = None) -> list[str]:
    tests = target or repo_root / 'tests'
    return [sys.executable, '-m', 'pytest', '-q', str(tests)]


def run_gate(
    repo_root: Path,
    *,
    pytest_target: Path | None = None,
    skip_lint: bool = False,
    env: dict | None = None,
) -> dict:
    repo_root = repo_root.resolve()
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    existing = run_env.get('PYTHONPATH', '')
    extra = os.pathsep.join(filter(None, [str(repo_root / 'src'), str(repo_root / 'tests'), existing]))
    run_env['PYTHONPATH'] = extra

    lines: list[str] = []
    steps_ok = True

    if not skip_lint:
        for argv in configured_lint_commands(repo_root):
            lines.append(f'==> {" ".join(argv)}')
            step = _run_step(argv, repo_root, run_env)
            if step['output'].strip():
                lines.extend(step['output'].rstrip().splitlines())
            if step['returncode'] != 0:
                steps_ok = False
                lines.append(f'lint failed: {" ".join(argv)} (exit {step["returncode"]})')
                break

    pytest_argv = pytest_command(repo_root, pytest_target)
    lines.append(f'==> {" ".join(pytest_argv)}')
    pytest_step = _run_step(pytest_argv, repo_root, run_env)
    if pytest_step['output'].strip():
        lines.extend(pytest_step['output'].rstrip().splitlines())
    verdict = evaluate_pytest(pytest_step['returncode'], pytest_step['output'])
    ok = steps_ok and verdict['ok']
    banner = 'GATE: PASS' if ok else 'GATE: FAIL'
    lines.append(banner)
    if ok:
        lines.append(
            f'summary: {verdict["passed"]} passed, {verdict["subtests"]} subtests, '
            f'{verdict["skipped"]} skipped'
        )
    elif verdict['reasons']:
        lines.append('reason: ' + '; '.join(verdict['reasons']))
    return {
        'ok': ok,
        'banner': banner,
        'lines': lines,
        'exit_code': 0 if ok else 1,
        'passed': verdict['passed'],
        'subtests': verdict['subtests'],
        'skipped': verdict['skipped'],
        'failed': verdict['failed'],
    }


def cli_main(args) -> int:
    result = run_gate(
        Path(args.repo),
        pytest_target=Path(args.pytest_target).resolve() if args.pytest_target else None,
        skip_lint=args.skip_lint,
    )
    for line in result['lines']:
        print(line)
    return result['exit_code']


def register_parser(commands) -> None:
    cmd = commands.add_parser(
        'gate',
        help='Run configured lint/type checks and the full test suite (0 skipped required)',
    )
    cmd.add_argument('--repo', default='.', help='repository root (default: .)')
    cmd.add_argument('--pytest-target', help='pytest path (default: <repo>/tests); for tests only')
    cmd.add_argument('--skip-lint', action='store_true', help='skip pyproject-configured linters')


if __name__ == '__main__':  # pragma: no cover
    from .cli import build_parser

    parser, _ = build_parser()
    raise SystemExit(cli_main(parser.parse_args(['gate', *sys.argv[1:]])))
