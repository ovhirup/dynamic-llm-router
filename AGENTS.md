# Dynamic LLM Router — Shared Agent Map

Whichever coding tool or agent receives the prompt (`cursor`, `claude`, `codex`, `grok-build`, or `grok-bot`) starts from this map so project context stays consistent without manual strategy rewrites.

## Where things live

- **Architecture & routing strategy:** [`docs/ROUTING_STRATEGY.md`](docs/ROUTING_STRATEGY.md), [`docs/STRATEGY_WORKERS_FALLBACK.md`](docs/STRATEGY_WORKERS_FALLBACK.md), [`docs/ROUTER_BRAIN.md`](docs/ROUTER_BRAIN.md)
- **Standing decisions:** [`docs/decisions.md`](docs/decisions.md)
- **Project learnings:** [`docs/learnings.md`](docs/learnings.md)
- **Configuration & workers:** [`docs/configuration.md`](docs/configuration.md), [`docs/setup.md`](docs/setup.md), [`docs/workers.md`](docs/workers.md), [`docs/evaluation.md`](docs/evaluation.md)
- **Core package:** `src/llm_router/` (`cli.py`, `selection.py`, `task_dispatch.py`, `job_dispatch.py`, `wave_runner.py`, `merge_queue.py`, `pr_gate.py`)
- **Tests:** `tests/` (offline, deterministic, fake workers only)

## How to run tests

- Full suite, the same command CI runs: `cd tests && PYTHONPATH=../src python -m unittest discover -t . -s .`
  It must end with `OK` and **0 skipped**. A `FAILED tests/fixtures/gate/failing/test_bad.py` line is a fixture, not a failure.
- Use Python 3.10 to 3.13 (CI's range). Unset every `GIT_CONFIG_*` variable first.
- Leak scan: `llm-router scan .`

## Backlog and pull requests

- Priorities live in issue #34. If another issue disagrees with it, #34 wins.
- One done-check per PR, written before the code. A different model reviews it against that done-check.
- Write `Closes #N` only when the PR meets the issue's whole done-check. Otherwise write `Related: #N`.
- Push a feature branch and open a draft PR only when your brief says so. The owner merges.

## Hard guardrails

1. Never commit to, push to, merge into, or force-push `main`.
2. Never read, print, or edit secrets, `.env`, `~/.codex`, `~/.config/llm-router`, `config.json`, or `repos.json`.
3. Do not touch untracked `docs/pacing.md`, `src/llm_router/pacing.py`, or `tests/test_pacing.py`.
4. Tests must run and pass; skipped tests never count as passing.
