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

- Full suite: `python -m pytest -q` (every test must pass with **0 skipped**)
- Unittest discovery: `PYTHONPATH=src python -m unittest discover -s tests`
- Leak scan: `llm-router scan .`

## Hard guardrails

1. Never commit to, push to, merge into, or force-push `main`.
2. Never read, print, or edit secrets, `.env`, `~/.codex`, `~/.config/llm-router`, `config.json`, or `repos.json`.
3. Do not touch untracked `docs/pacing.md`, `src/llm_router/pacing.py`, or `tests/test_pacing.py`.
4. Tests must run and pass; skipped tests never count as passing.
