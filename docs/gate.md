# Quality gate (`llm-router gate`)

Workers finish a task only after the gate passes on the repo checkout they changed.

## Run

From the repository root (editable install recommended):

```bash
llm-router gate
# or
./scripts/check.sh
```

`scripts/check.sh` uses `llm-router` when it is on `PATH`, otherwise `python -m llm_router.gate`.

Paste the **last lines** of the output into your handoff or PR comment, including the `GATE: PASS` or `GATE: FAIL` line and the `summary:` line when present.

Example (pass):

```
==> /usr/bin/python3 -m pytest -q tests
........................................................................ [100%]
1364 passed, 331 subtests passed in 534.07s (0:08:54)
GATE: PASS
summary: 1364 passed, 331 subtests, 0 skipped
```

## What fails the gate

- Any pytest failure or non-zero pytest exit
- Any **skipped** test (0 skipped required)
- Any **xpassed** (unexpected pass on an xfail test)
- Any **xfailed** (expected-fail tests do not count as strict green)
- Collection or run **errors** in the summary
- No positive **passed** count (`no tests reported passed`)
- Stdout that suppresses or spoofs the summary (`no authentic pytest summary`)
- A missing junitxml report, or one that disagrees with the stdout footer
- A footer that appears only on stderr (never parsed)

**Warnings** in the pytest summary do not fail the gate by themselves.

## Authentic summary and a positive passed count

The gate counts tests, skipped, failures, and errors from a pytest `--junitxml` report written in a private temporary directory. It cross-checks that report against pytest's stdout footer only: the final `=== ... in Ns ===` line, or, under `-q`, the last line of the form `N passed in Ns`. Passing subtests are part of the JUnit `tests` total and part of that cross-check. Stderr is displayed and never parsed, so a `capsys.disabled()` write of `2 passed in 0.01s` cannot hide a skip. A missing report fails the gate. A report that disagrees with the stdout footer fails the gate, reports the JUnit counts (skips included), and shows both sides. The footer must still report a positive passed count. Zero passed tests fail the gate even when the exit code is 0.

An empty collection exits pytest with code 5 and prints a summary such as `no tests ran in 0.00s`. The gate reports `pytest exit 5`.

These fail with `no tests reported passed` when pytest still exits 0:

- a missing summary
- warnings only, including a warning whose text is `1 skipped cache refresh` (that phrase is not a skip)
- empty output

A line like `worker diagnostic: 2 passed` is not a summary. If `-q -s` or `PYTEST_ADDOPTS` suppresses the real footer, or only that spoof is left, the gate fails with `no authentic pytest summary`.

ANSI escapes are stripped before parsing, including a reset code between a count and its label (`1`, reset, `skipped`).

The gate runs pytest with `-q`, `--color=no`, `-o addopts=`, and `--junitxml` pointed at the private report. It does not pass `-rA` or `-p no:cacheprovider`, and it removes `PYTEST_ADDOPTS` so those flags cannot hide the summary.

Configured linters in `pyproject.toml` (`[tool.ruff]`, `[tool.mypy]`, `[tool.pyright]`) run first when the matching CLI is on `PATH`.

## CI vs gate

GitHub Actions CI (`.github/workflows/ci.yml`) runs `python -m unittest discover` from `tests/`. The gate runs quiet pytest on `tests/` with `PYTHONPATH=src:tests`, ignoring `PYTEST_ADDOPTS` and config `addopts`. Treat **`llm-router gate`** as the local definition of done; aligning CI to the same harness is a follow-up.
