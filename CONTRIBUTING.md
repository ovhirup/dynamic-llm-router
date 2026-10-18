# Contributing

Thanks for helping. Small, focused pull requests are easiest to review.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -e .
cd tests && ../.venv/bin/python -m unittest discover -t . -s .
```

No third-party runtime dependencies, please. Anything new should work with the Python 3.10 standard library.

## Ground rules

- **Tests use fakes only.** Never call a real agent CLI, a real model, GitHub or the network from a test.
  Use the fake worker scripts, fake `gh` and fixture screens in `tests/`.
- **Unknown, not wrong.** A quota observer that is unsure must return `status: "unknown"` with a reason code.
  Never invent or extrapolate a percentage.
- **No secrets, no personal data.** No real keys, account ids, emails, home paths or private project names in code,
  tests, fixtures or commit messages. Run `llm-router scan . --history` before opening a PR. Fixture
  screens must be scrubbed captures.
- **Safety defaults stay safe.** New workers and observers are off by default. Prompts never go in argv.
  Don't read credential files, keychains or cookies, and don't call undocumented billing endpoints.
- Keep the gate strict: no force push, `--admin`, `--auto` or branch deletion paths.

## Adding a worker

1. Add a command template under `run_task.commands.<worker>` and a `prompt_delivery` (`stdin`, `file` or `pointer`).
2. Add a timeout and stall entry.
3. Add a quota observer (or document that the worker must be listed in `unmetered_workers`).
4. Add tests with a fake binary.

## Adding a quota observer

Write a function `observe(config: dict) -> dict` returning
`{"evidence": {...} | None, "status": "ok" | "unknown", "reason": str | None, "cli_version": str | None}`.
Put any subprocess call behind `llm_router.observers.common.run_capture` / `capture_pty` so timeouts kill the
process group. Add a scrubbed fixture and parser tests, including a malformed-screen test that yields `unknown`.
See [docs/observers.md](docs/observers.md).

## Releases

Update `CHANGELOG.md` and `src/llm_router/__init__.py`, then tag `vX.Y.Z`.
