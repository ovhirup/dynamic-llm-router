"""Repository quality gate (DLR #216): configured lint/type checks, then pytest with 0 skipped."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
_COUNT_LABEL = (
    'subtests passed|subtests failed|subtests skipped|'
    'xpassed|xfailed|passed|failed|skipped|deselected|'
    'warnings|warning|errors|error'
)
_DURATION = r'in \d+(?:\.\d+)?s(?: \([^)]+\))?'
_STATS_BODY = (
    rf'(?:\d+ (?:{_COUNT_LABEL}))(?:, (?:\d+ (?:{_COUNT_LABEL})))* {_DURATION}'
)
_SUMMARY_BODY = rf'(?:{_STATS_BODY}|no tests ran {_DURATION})'
_EQUALS_SUMMARY = re.compile(rf'^=+ ({_SUMMARY_BODY}) =+$')
_QUIET_SUMMARY = re.compile(rf'^({_SUMMARY_BODY})$')
_PASS_SPOOF = re.compile(r'\d+ passed\b')
_PROGRESS_ONLY = re.compile(r'^[.sFxXpPeE]+$')
_PERCENT = re.compile(r'\[\d+%\]')


def _count(label: str, line: str) -> int:
    match = re.search(rf'(\d+) {re.escape(label)}', line)
    return int(match.group(1)) if match else 0


def _normalize_line(raw: str) -> str:
    """Drop ANSI so a reset code between a count and its label still parses."""
    cleaned = _ANSI_RE.sub(' ', raw)
    return re.sub(r'[ \t]+', ' ', cleaned).strip()


def _summary_body(line: str) -> str | None:
    """Return the stats text of an authentic pytest final summary line."""
    match = _EQUALS_SUMMARY.match(line) or _QUIET_SUMMARY.match(line)
    return match.group(1) if match else None


def _untrustworthy_stdout(output: str) -> bool:
    """True when output spoofs a pass count or shows progress with no summary."""
    for raw in output.splitlines():
        line = _normalize_line(raw)
        if not line or _summary_body(line):
            continue
        if _PASS_SPOOF.search(line) or _PROGRESS_ONLY.match(line) or _PERCENT.search(line):
            return True
    return False


def parse_pytest_summary(output: str) -> dict:
    """Extract counts from pytest's final summary line only.

    Authentic lines are the ``=== ... in Ns ===`` footer or the last quiet
    ``N passed in Ns`` line. Warning text and other diagnostics are ignored.
    """
    counts = {'passed': 0, 'failed': 0, 'skipped': 0, 'subtests': 0, 'errors': 0,
              'xpassed': 0, 'xfailed': 0, 'authentic': False}
    bodies = []
    for raw in output.splitlines():
        body = _summary_body(_normalize_line(raw))
        if body is not None:
            bodies.append(body)
    if not bodies:
        return counts
    counts['authentic'] = True
    stats = re.sub(rf'\s+{_DURATION}$', '', bodies[-1])
    counts['subtests'] = _count('subtests passed', stats)
    without_subtests = re.sub(r'\d+ subtests (?:passed|failed|skipped),?\s*', '', stats)
    counts['passed'] = _count('passed', without_subtests)
    counts['failed'] = _count('failed', without_subtests)
    counts['skipped'] = _count('skipped', without_subtests)
    counts['errors'] = _count('error', without_subtests)
    counts['xpassed'] = _count('xpassed', without_subtests)
    counts['xfailed'] = _count('xfailed', without_subtests)
    return counts


def evaluate_pytest(exit_code: int, output: str) -> dict:
    summary = parse_pytest_summary(output)
    reasons = []
    if exit_code != 0:
        reasons.append(f'pytest exit {exit_code}')
    if not summary['authentic']:
        if _untrustworthy_stdout(output):
            reasons.append('no authentic pytest summary')
        elif exit_code == 0:
            reasons.append('no tests reported passed')
    else:
        if summary['failed'] or summary['errors']:
            reasons.append(f"{summary['failed']} failed, {summary['errors']} errors")
        if summary['skipped']:
            reasons.append(f"{summary['skipped']} skipped")
        if summary['xpassed']:
            reasons.append(f"{summary['xpassed']} xpassed")
        if summary['xfailed']:
            reasons.append(f"{summary['xfailed']} xfailed")
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
    return {
        'argv': argv,
        'returncode': proc.returncode,
        'stdout': proc.stdout or '',
        'stderr': proc.stderr or '',
    }


def _extend_streams(lines: list[str], step: dict) -> None:
    for stream in (step['stdout'], step['stderr']):
        if stream.strip():
            lines.extend(stream.rstrip().splitlines())


def pytest_command(repo_root: Path, target: Path | None = None,
                   junitxml: Path | None = None) -> list[str]:
    """Quiet pytest with config addopts cleared.

    ``-rA`` and ``-p no:cacheprovider`` are intentionally absent: report chars
    and the cache plugin are not required for the final summary line.
    ``junitxml`` is the private report the gate counts; it is not part of the
    default argv so callers can still inspect the base command.
    """
    tests = target or repo_root / 'tests'
    argv = [
        sys.executable, '-m', 'pytest', '-q', '--color=no', '-o', 'addopts=', str(tests),
    ]
    if junitxml is not None:
        argv.extend(['--junitxml', str(junitxml)])
    return argv


def parse_junit_counts(path: Path) -> dict | None:
    """tests/skipped/failures/errors from a pytest junitxml file, or None.

    The file must be a regular non-symlink. A missing, huge, or malformed
    report is not a summary.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        with os.fdopen(fd, 'rb') as handle:
            raw = handle.read(8_000_001)
    except OSError:
        return None
    if not raw or len(raw) > 8_000_000:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    suites = [root] if root.tag == 'testsuite' else list(root.findall('testsuite'))
    if not suites:
        return None

    def total(name: str) -> int:
        count = 0
        for suite in suites:
            value = suite.get(name)
            if value is None or not value.isdigit():
                raise ValueError(name)
            count += int(value)
        return count

    try:
        return {
            'tests': total('tests'),
            'skipped': total('skipped'),
            'failures': total('failures'),
            'errors': total('errors'),
        }
    except ValueError:
        return None


def _junit_matches(summary: dict, junit: dict) -> bool:
    """Stdout footer counts agree with the junitxml report.

    Pytest records an xfailed test as skipped in junitxml and as xfailed in
    the footer. An xpassed test counts in the footer separately and inside
    the junit tests total.
    """
    if not summary.get('authentic'):
        return False
    # Pytest's footer lists passing subtests beside the test total. JUnit
    # folds those same subtests into tests=.
    total = (summary['passed'] + summary['failed'] + summary['skipped']
             + summary['errors'] + summary['xpassed'] + summary['xfailed']
             + summary['subtests'])
    return (junit['tests'] == total
            and junit['failures'] == summary['failed']
            and junit['errors'] == summary['errors']
            and junit['skipped'] == summary['skipped'] + summary['xfailed'])


def _apply_junit(summary: dict, junit: dict) -> None:
    """Prefer the junitxml counts once they agree with the stdout footer."""
    summary['failed'] = junit['failures']
    summary['errors'] = junit['errors']
    summary['skipped'] = junit['skipped'] - summary['xfailed']
    summary['passed'] = (junit['tests'] - junit['failures'] - junit['errors']
                         - junit['skipped'] - summary['xpassed'] - summary['subtests'])


def _report_junit_counts(summary: dict, junit: dict) -> None:
    """On a mismatch, the returned counts are JUnit's, skips included."""
    summary['failed'] = junit['failures']
    summary['errors'] = junit['errors']
    summary['skipped'] = junit['skipped']
    summary['passed'] = (junit['tests'] - junit['failures'] - junit['errors']
                         - junit['skipped'])


def _mismatch_reason(summary: dict, junit: dict) -> str:
    return (
        'junitxml does not match the stdout summary: '
        f'junit tests={junit["tests"]} skipped={junit["skipped"]} '
        f'failures={junit["failures"]} errors={junit["errors"]}; '
        f'stdout passed={summary["passed"]} failed={summary["failed"]} '
        f'skipped={summary["skipped"]} errors={summary["errors"]} '
        f'subtests={summary["subtests"]}'
    )


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
    # -qq / -s from the environment can drop the summary or let a test print a
    # fake "N passed" line. The gate's own argv is the whole pytest command.
    run_env.pop('PYTEST_ADDOPTS', None)
    existing = run_env.get('PYTHONPATH', '')
    extra = os.pathsep.join(filter(None, [str(repo_root / 'src'), str(repo_root / 'tests'), existing]))
    run_env['PYTHONPATH'] = extra

    lines: list[str] = []
    steps_ok = True

    if not skip_lint:
        for argv in configured_lint_commands(repo_root):
            lines.append(f'==> {" ".join(argv)}')
            step = _run_step(argv, repo_root, run_env)
            _extend_streams(lines, step)
            if step['returncode'] != 0:
                steps_ok = False
                lines.append(f'lint failed: {" ".join(argv)} (exit {step["returncode"]})')
                break

    # Counts come from a junitxml report in a private 0700 directory. The
    # stdout footer is the cross-check. Stderr is shown and never parsed, so
    # a test cannot forge a passing footer with capsys.disabled().
    directory = tempfile.mkdtemp()
    try:
        os.chmod(directory, 0o700)
        junit_path = Path(directory) / 'results.xml'
        pytest_argv = pytest_command(repo_root, pytest_target, junit_path)
        lines.append(f'==> {" ".join(pytest_argv)}')
        pytest_step = _run_step(pytest_argv, repo_root, run_env)
        _extend_streams(lines, pytest_step)
        verdict = evaluate_pytest(pytest_step['returncode'], pytest_step['stdout'])
        junit = parse_junit_counts(junit_path)
        if junit is None:
            verdict['reasons'].append('missing junitxml summary')
            verdict['ok'] = False
        elif not _junit_matches(verdict, junit):
            verdict['reasons'].append(_mismatch_reason(verdict, junit))
            verdict['ok'] = False
            _report_junit_counts(verdict, junit)
        else:
            _apply_junit(verdict, junit)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
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
