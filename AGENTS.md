# Dynamic LLM Router — Shared Agent Map

Whichever coding tool or agent receives the prompt (`cursor`, `claude`, `codex`, `grok-build`, or `grok-bot`) starts from this map so project context stays consistent without manual strategy rewrites.

## Decisions

- 2026-11-04 — Workers never approve their own fixes.
- 2026-11-04 — A green test run has zero skipped tests.
- 2026-11-04 — Stop after the third REVISE on the same change and hand it back to the user.
- 2026-11-04 — Never stage `*.orig` or `*.rej` files.
- 2026-11-04 — Keep secrets out of shared memory.

Each month, remove lines no worker used and move settled items from Open to Decisions.

## Open

- #219 shared memory awaits review by the repository maintainer.

## Where things live

- **Architecture & routing strategy:** [`docs/ROUTING_STRATEGY.md`](docs/ROUTING_STRATEGY.md), [`docs/STRATEGY_WORKERS_FALLBACK.md`](docs/STRATEGY_WORKERS_FALLBACK.md), [`docs/ROUTER_BRAIN.md`](docs/ROUTER_BRAIN.md)
- **Standing decisions:** [`docs/decisions.md`](docs/decisions.md)
- **Project learnings:** [`docs/learnings.md`](docs/learnings.md)
- **Configuration & workers:** [`docs/configuration.md`](docs/configuration.md), [`docs/setup.md`](docs/setup.md), [`docs/workers.md`](docs/workers.md), [`docs/evaluation.md`](docs/evaluation.md)
- **Core package:** `src/llm_router/` (`cli.py`, `selection.py`, `task_dispatch.py`, `job_dispatch.py`, `wave_runner.py`, `merge_queue.py`, `pr_gate.py`)
- **Tests:** `tests/` (offline, deterministic, fake workers only)

## How to run tests

- Full suite: `python -m pytest -q` (every test must pass with **0 skipped**)
- Unittest discovery: `PYTHONPATH=src python -m unittest discover -s tests`
- Leak scan: `llm-router scan .`

## Hard guardrails

1. Never commit to, push to, merge into, or force-push `main`.
2. Never read, print, or edit secrets, `.env`, `~/.codex`, `~/.config/llm-router`, `config.json`, or `repos.json`.
3. Do not touch untracked `docs/pacing.md`, `src/llm_router/pacing.py`, or `tests/test_pacing.py`.
4. Tests must run and pass; skipped tests never count as passing.
