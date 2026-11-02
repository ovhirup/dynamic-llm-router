# Bring any worker

You choose your own worker order. These config fragments are also packaged under
`src/llm_router/examples/workers/`; merge a fragment into `config.json` and add its worker to `worker_order`.
Flags must be checked against your installed CLI version before use.

## 1. Per-worker reference (meter source, cap, and class rank)

Default fallback order when the preferred worker is out of quota:
**Claude > Codex > Grok Build > Cursor > local (`nativ` / `aider-ollama`)**, with **Grok Bot** as the last-resort orchestrator fallback.

| Worker | Meter source | Default cap (`usage_threshold_percent`) | Concurrency cap | Class ranks (`feature` / `bugfix` / `review` / `mechanical` / `draft`) |
|---|---|---|---|---|
| `claude` | Claude Code `/usage` on personal Pro account (`llm_router.observers.claude:observe`) | 85% (session & weekly; 5h < 80%) | 2–3 | 1st `feature`, 1st `bugfix`, 2nd `review`, 3rd `draft` |
| `codex` | ChatGPT Plus `codex app-server` `account/rateLimits/read` (`llm_router.observers.codex:observe`) | 85% (`primary` & `weekly`) | 2 | 4th `feature`, 3rd `bugfix`, 4th `review`, 2nd `mechanical` |
| `grok-build` | SuperGrok `grok dashboard` `/usage` + week-curve pacing (`llm_router.observers.grok_build:observe`) | 85% weekly (week-curve paced; 95% in last 24h) | 2 | 2nd `feature`, 2nd `bugfix`, 3rd `review`, 1st `mechanical` |
| `cursor` | Cursor Pro `cursor-agent` `/usage` (`llm_router.observers.cursor:observe`, `consult: "fallback"`) | 85% monthly (on-demand = $0) | 2 | 3rd `feature`, 4th `bugfix`, 3rd `mechanical` |
| `nativ` / `aider-ollama` / `developer` | Local server health (`/v1/models` or `/api/tags`) + RAM guard | Unmetered (local) | 1 (shared across all local models) | 4th `mechanical`, 1st `draft` (never `review`) |
| `grok-bot` | Manual/daily Settings -> Usage & Billing (`grok_bot.evidence`) | Week-curve paced, never banned | Serial fallback only (never in parallel pool) | 1st `review`, 2nd `draft`, last-resort fallback for all classes |

## 2. Claude account safety rule

Only the personal Claude Pro account is allowed; an employer account is never used for routed jobs.
Before every Claude job (when `claude_account_check` is enabled), `claude_account.check()` inspects the
signed-in profile (`~/.claude.json`) against the configured personal allowlist (`personal_domains` or
exact `allowed_emails` outside the repo). Any mismatch, employer domain, or unreadable profile marks
`claude` ineligible for that job (`claude_account_<reason>`) and logs the reason. The check runs before
every job and is never cached across sessions.

## 3. Local models and 16 GB RAM notes (Apple Silicon M4)

- **`aider-ollama`** (`src/llm_router/examples/workers/aider-ollama.json`): runs `aider` with
  `--model ollama_chat/qwen2.5-coder:14b` (~9 GB RAM); used for `mechanical` and `draft` tasks.
  On macOS 27, install with `scipy>=1.16` (`uv tool install --force aider-chat --with 'scipy>=1.16'`).
- **`nativ`** (`io.github.blaizzy.nativ`): local MLX server exposing OpenAI- and Anthropic-compatible
  endpoints (`/v1/models`, `/v1/chat/completions`), tested with
  `mlx-community/Qwen2.5-Coder-14B-Instruct-4bit` (~9 GB RAM).
- **`developer`**: built-in OpenAI-compatible HTTP draft worker (`developer.enabled` + `developer.endpoint`)
  for short draft text (`<= draft_max_chars`).
- **Single-model RAM guard:** on a 16 GB machine, only **one** `>=14B` local model may be loaded at a time
  (`concurrency: 1` across local workers). A second concurrent local job is refused/deferred with `local_busy`.
- **Hark (`com.harklabs.hark.mac`):** local dictation and meeting transcription (`hark-mcp`), used only as a
  task-input source plugin (#19 / #76), **never** as a coding LLM worker.

## 4. Packaged worker presets

- [aider](../src/llm_router/examples/workers/aider.json): `aider --yes-always --no-auto-commits --no-gitignore --message-file {prompt_file}`;
  file delivery, explicitly unmetered.
- [aider with Ollama](../src/llm_router/examples/workers/aider-ollama.json): adds `--model ollama_chat/qwen2.5-coder:14b`;
  allows parent `OLLAMA_API_BASE` plus essential environment variables, explicitly unmetered.
- [Nativ](../src/llm_router/examples/workers/nativ.json): aider against a local [Nativ](https://github.com/Blaizzy/nativ)
  (`io.github.blaizzy.nativ` 0.4.3) MLX server through its OpenAI-compatible API:
  `aider ... --openai-api-base {base_url}/v1 --model openai/{model} --message-file {prompt_file}`;
  file delivery, `concurrency: 1`, explicitly unmetered. Worker fields:
  - `base_url` (default `http://127.0.0.1:8080`): an `http://` or `https://` URL; expands `{base_url}`.
  - `model` (default `mlx-community/Qwen2.5-Coder-14B-Instruct-4bit`): non-empty; expands `{model}`.

  Both are stored as `run_task.local_models.<name>`. Before a worker with a `base_url` is marked eligible,
  selection GETs `<base_url>/v1/models` (2 s timeout, no proxies, no redirects). An unreachable server,
  non-200 reply or invalid JSON gives reason `server_not_running`; a model missing from `data[].id`
  (exact or `/`-suffix match) gives `model_not_present`. Tested with
  `mlx-community/Qwen2.5-Coder-14B-Instruct-4bit`, which uses about 9 GB of RAM on a 16 GB M4 Mac;
  start Nativ and load the model before routing work to it.
- [opencode](../src/llm_router/examples/workers/opencode.json): `opencode run {prompt}`, explicit arg delivery.
  Warning: prompt passed in argv: visible to other local users in the process list.
- [custom quota command](../src/llm_router/examples/workers/custom-with-quota.json): made-up sample-worker usage command,
  named percent regex, monthly window, timeouts and freshness. Replace it with your own CLI.

## Role-based routing & direct Claude Code tips (#135)

DLR can split work by role (`--role {planner,coder,searcher,judge,advisor}`):
- `planner`, `judge`, and `advisor` route to the strongest available worker (`claude > grok-build > codex`).
- `coder` routes to the next worker with budget (`codex > grok-build > cursor > claude > aider-ollama`).
- `searcher` routes to local/cheaper workers first (`aider-ollama > developer > cursor`).
- When a `coder` hits repeated identical errors or a loop, DLR escalates a transcript summary to the `advisor` role and feeds its guidance back to the `coder`.
- For direct interactive Claude Code sessions, use `/model opusplan` (Opus plans, Sonnet executes), delegate file/codebase searches to a Haiku subagent (`CLAUDE_CODE_SUBAGENT_MODEL=haiku`), and keep autocompact enabled so long transcripts never burn Opus quota.

`llm-router init --workers aider,opencode --non-interactive` installs the selected presets and marks workers
without enabled meters unmetered (not paced). An installed Ollama CLI offers the `aider-ollama` preset;
it still requires `aider` and a configured local model. No detection step executes a CLI.

## Single local model guard (`local_busy`)

A 16 GB RAM machine cannot hold two >=14B local models at once, so the router never runs two local model
jobs concurrently. Local model workers are `developer`, `aider-ollama`, `nativ` and `local`, plus any worker
whose name starts with `aider-ollama` or `nativ`. Besides its own `run_task.concurrency` slot, every local
model job takes an exclusive, non-blocking flock on `local_model.lock` under `run_task.slot_dir` (shared
across all router processes, released when the job ends or its process exits).

If another local model job already holds that lock, the worker is skipped with the warning
`<worker> skipped: local_busy (another local model job is running)` and the run fails over to the next
eligible worker. If no other worker can run, the job is refused with
`local_busy: another local model job is already running` and reason `local_busy`. The wave runner defers
such units (`deferred_slot_full`). Remote workers are never affected.

See [configuration](configuration.md) for safe environment allowlists, cwd constraints, success checks,
command quota settings and forced-worker semantics. All workers retain denylist shims and leak checks.
