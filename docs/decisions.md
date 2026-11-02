# Standing Decisions

Dated log of binding engineering and operational rules for Dynamic LLM Router (DLR).

- **2026-10-18 — Worktree isolation & human gate:** Every `run-job` executes in a fresh git worktree on its own branch; `push` and `merge` require explicit human approval bound to the exact head SHA (`src/llm_router/pr_gate.py`).
- **2026-10-31 — Tests never skip:** Every job and PR must run the repository test suite (`python -m pytest -q`), and skipped tests never count as passing.
- **2026-10-31 — Live-budget worker order & pacing:** Worker eligibility comes from fresh meter readings (`claude > codex > grok-build > cursor > local`); `grok-bot` is never in the parallel pool and acts only as last-resort fallback.
- **2026-10-31 — Zero secrets in repo or logs:** All worker outputs, handoff notes, and job summaries pass through `security.redact` and `security.leak_check`. Never read or modify `.env`, `~/.codex`, `~/.config/llm-router`, `config.json`, or `repos.json`.
- **2026-11-01 — Untracked pacing files untouched:** Leave untracked `docs/pacing.md`, `src/llm_router/pacing.py`, and `tests/test_pacing.py` untouched unless explicitly requested.
- **2026-11-01 — Dynamic orchestrator (#120):** Whoever receives the prompt orchestrates; DLR hands off when the active orchestrator's budget runs low without requiring manual strategy-doc edits.
