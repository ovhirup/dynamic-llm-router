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

**Warnings** in the pytest summary do not fail the gate by themselves.

Configured linters in `pyproject.toml` (`[tool.ruff]`, `[tool.mypy]`, `[tool.pyright]`) run first when the matching CLI is on `PATH`.

## CI vs gate

GitHub Actions CI (`.github/workflows/ci.yml`) runs `python -m unittest discover` from `tests/`. The gate runs `python -m pytest -q tests/` with `PYTHONPATH=src:tests`. Treat **`llm-router gate`** as the local definition of done; aligning CI to the same harness is a follow-up.
