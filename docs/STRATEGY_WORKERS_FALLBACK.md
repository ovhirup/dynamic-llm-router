# DLR worker and fallback strategy

Status: proposed (31 Oct 2026). Owner: Abhirup. Supersedes the "ban Grok-family in unknown mode" rule on `grokbot/week-curve-phase1` (commit 4595557), which must be reworked, not merged as is.

## 1. Roles

- **Grok Bot: orchestrator.** It is never restricted. It writes the task file, picks class and size, calls the router, reviews the diff, and does the work itself when it is the best available option. Its weekly pool resets Saturdays 10:49 IST; its meter is tracked like any other and paced, never banned.
- **Workers:** executors called through `llm-router run-job` (later `bot-dispatch`). Each worker runs in a worktree on its own branch. No worker pushes, merges or approves.

## 2. Worker ranking

Default fallback order when the preferred worker is out of tokens (owner's order): **Claude > Codex > Grok Build > Cursor > local** (updated 31 Oct 2026; see ROUTING_STRATEGY.md). Cursor sits after Grok Build: strong for editor-scale edits, but it is a seat with a monthly cap and no headless CLI contract yet.

| Class | 1st | 2nd | 3rd | 4th | Last |
|---|---|---|---|---|---|
| feature | Claude | Grok Build | Cursor | Codex | Grok Bot (self) |
| bugfix | Claude | Grok Build | Codex | Cursor | Grok Bot |
| review | Grok Bot | Claude | Grok Build | Codex | — (never local) |
| mechanical | Grok Build | Codex | Cursor | local (Nativ/aider-ollama) | Grok Bot |
| draft (wording) | local | Grok Bot | Claude | — | — |

Rules: capability decides the order; available tokens decide who is eligible. A worker only moves up when a higher one is out, never because it is cheaper. Research is never routed to a worker.

## 3. Token-aware routing

1. **Meter first.** Before each job, read every worker's meter (5-hour and weekly, `percent_used`, `reset_at`). A meter older than 30 min or unreadable is `unknown`.
2. **Eligibility:** weekly < cap (default 85%, configurable per worker) and 5-hour < 80%. `unknown` means try the worker last among cloud workers; it does not mean banned (no fall-closed for the orchestrator's own family).
3. **Pacing (week curve):** allowed = 85 × elapsed fraction of the 7-day window. A worker ahead of the curve drops one rank instead of being banned. In the last 24 h before reset the reserve is released to 95%.
4. **Failover on exhaustion:** a 429/quota/limit error puts the worker in cooldown until its `reset_at` (or 1 h if unknown) and the job moves to the next eligible worker. One retry per worker, at most 3 workers per job.
5. **Never idle:** if every cloud worker is out, use local for mechanical/draft; for anything else Grok Bot does it itself. A job is refused only for policy reasons (push, merge, research, secrets), never for quota.
6. **Size control:** split jobs larger than one focused change before dispatch; send `--size draft` for wording.

## 4. Quality gates (every job, every worker)

- The repo's existing test suite passes in the worktree (`tests_status` must be `passed`).
- The leak check is clean against `private_terms.txt` plus secret patterns.
- The diff stays within the paths the task file names; unrelated edits fail the job.
- **Review step:** work from Codex, Cursor or local is reviewed by a stronger model (Grok Bot or Claude) before Abhirup sees it. Local-model output is always reviewed and never auto-committed to a shared branch.
- Final gate: Abhirup's explicit yes for any push, merge, PR or approval code.

## 5. Meter monitoring

| Worker | Source | Cadence |
|---|---|---|
| Codex | ChatGPT Plus usage meter (router observer) | before each job + every 2 h |
| Grok Build | SuperGrok usage meter | before each job + every 2 h |
| Cursor | Cursor Pro usage page (plan limit, on-demand = $0) | daily |
| Claude | Claude Code `/usage` on the personal account | before each job |
| Grok Bot | Settings → Usage & Billing | daily + before self-work |
| Local | `ollama list` / Nativ server health, RAM | before each job |

Alerts: one message when any worker crosses 75% weekly, when a worker becomes `unknown`, and a daily summary at 09:00 IST. No alert for normal pacing.

## 6. Claude account safety

The personal Claude Pro account is allowed; the employer account is never used. Before every Claude job, the router checks the signed-in account (`claude` auth status / account email) against an allow-list held outside the repo. A mismatch or unreadable result marks Claude ineligible for that job and logs why. The check runs every job; it is never cached across sessions.

## 7. Local models (M4, 16 GB)

- **aider-ollama** with `qwen2.5-coder:14b` (now downloaded, ~9 GB): mechanical edits only.
- **Nativ** (`io.github.blaizzy.nativ` 0.4.3, installed): a local MLX model server with OpenAI- and Anthropic-compatible APIs. It can be a second local backend, pointed at by aider or an OpenAI-compatible worker.
- **Hark** (`com.harklabs.hark.mac` 0.1.18, installed): local dictation and meeting transcription, **not** a coding LLM. It is useful as an input (dictated task files, meeting search via `hark-mcp`), not as a worker.
- Only one ≥14B model is loaded at a time, to stay within 16 GB.

## 8. Industry practice used

From the LiteLLM Router docs (docs.litellm.ai/docs/routing): ordered priority with automatic fall-through on 429/errors, per-deployment cooldowns, retry policies by error type, pre-call eligibility checks, and usage (TPM/RPM, daily quota) tracking that moves requests off exhausted deployments. DLR applies the same patterns at the level of subscription seats instead of API keys.
