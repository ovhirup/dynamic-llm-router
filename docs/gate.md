# Quality gate (`llm-router gate`)

Workers finish a task only after the gate passes on the repo checkout they changed.

## Run

From the repository root (editable install recommended):

```bash
llm-router gate
# or
./scripts/check.sh
```

Paste the **last lines** of the output into your handoff or PR comment, including the `GATE: PASS` or `GATE: FAIL` line and the `summary:` line when present.

Example (pass):

```
==> /usr/bin/python3 -m pytest -q tests
........................................................................ [100%]
1364 passed, 331 subtests passed in 534.07s (0:08:54)
GATE: PASS
summary: 1364 passed, 331 subtests, 0 skipped
```

Any failure, non-zero pytest exit, or **any skipped test** yields `GATE: FAIL` and a non-zero exit code.

Configured linters in `pyproject.toml` (`[tool.ruff]`, `[tool.mypy]`, `[tool.pyright]`) run first when the matching CLI is on `PATH`.
