# Runbook: What to Do When Every Cloud Worker Is Out

Keep this procedure under one page. DLR's **never-idle rule** guarantees a job is never refused for quota alone: when every cloud worker (`claude`, `codex`, `grok-build`, `cursor`) is exhausted or cooling down, mechanical and draft work routes to local models, and everything else hands back to the orchestrator (`grok-bot`).

## 1. Immediate Steps

1. **Check live meters and cooldowns:**
   ```bash
   llm-router status
   llm-router alerts
   llm-router select --class feature
   ```
   - Confirm whether workers are genuinely at `100%` (`quota_exhausted` / `cooldown_until_<ISO>`), above the `85%` full-size cap (`over_threshold`), or simply `stale` (`> 30 min`).
   - If meters are stale rather than exhausted, refresh once: `llm-router quota refresh --once`.
   - If a worker is between `70%` and `85%` (or in the last 24h before reset when the week-curve reserve releases to `95%`), dispatch small wording tasks with `--size draft`.

2. **Route `mechanical` and `draft` work to local models (M4, 16 GB RAM):**
   - Ensure Ollama (`ollama list` showing `qwen2.5-coder:14b`) or Nativ (`/v1/models`) is running.
   - Only **one** `>=14B` local model runs at a time (`concurrency: 1`) so RAM stays within 16 GB.
   - Dispatch with `--class mechanical` or `--class draft --size draft`:
     ```bash
     llm-router run-job --repo ~/dev/<repo> --task-file task.md --class mechanical
     ```

3. **Hand off `feature`, `bugfix`, and `review` work to Grok Bot (or the active orchestrator):**
   - When all cloud workers are out for a non-mechanical class, `run-job --class <feature|bugfix|review>` returns `"status": "handoff_to_orchestrator"` with `"worker": "grok-bot"` and `"task_file"` instead of failing.
   - Execute the change directly in the orchestrator session on a branch, run `python -m pytest -q` (0 skipped), and keep the commit local until human approval.

## 2. Reset Windows & Cadence Reference

| Worker / Pool | Window & Reset Schedule | Where to Check |
|---|---|---|
| **Claude (personal Pro)** | 5-hour rolling session + 7-day weekly window | `claude` -> `/usage` or `llm-router status` |
| **Codex (ChatGPT Plus)** | 5-hour primary window + 7-day weekly window | `codex app-server` (`llm-router quota refresh --worker codex`) |
| **Grok Build (SuperGrok)** | 7-day rolling window (week-curve paced; 95% floor in last 24h) | `grok dashboard` -> `/usage` |
| **Grok Bot (orchestrator)** | Weekly pool; resets **Saturdays 10:49 IST** | Settings -> Usage & Billing (`python -m llm_router.meters set grok-bot ...`) |
| **Cursor Pro** | Monthly billing cycle (1st of month; on-demand cap = $0) | Cursor Settings -> Usage (`cursor-agent` `/usage`) |
| **Local (`aider-ollama` / `nativ`)** | No token quota (16 GB RAM ceiling, concurrency = 1) | `ollama ps` / `curl http://127.0.0.1:8080/v1/models` |
