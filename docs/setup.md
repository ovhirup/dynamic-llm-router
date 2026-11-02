# Setup wizard, overrides and orchestrator switching (#210)

`llm-router setup` lets you choose which tools DLR may use as **workers** and which may act as
**orchestrator**. The budget-aware allocation from [#208](allocation-policy.md) still picks the order
automatically; your choices only say which tools are candidates, plus any pins you explicitly add.

## Run the wizard

```sh
llm-router setup              # interactive; writes <config dir>/setup.json (backed up first)
llm-router setup --dry-run    # detect and smoke-test, but write nothing
llm-router setup --output ~/dlr/setup.json
```

The wizard only runs in a terminal and only writes when you run it. For each tool it:

1. **Detects** it: `claude`, `codex`, `grok` (Grok Build), `cursor-agent` (Cursor), `ollama` + `aider`
   (local), and asks whether Grok Bot is reachable in chat. Finding a binary is *not* treated as signed in.
2. Shows the **rationale and recommended default** next to it, for example:

   ```
   grok-bot: Recommended orchestrator for chat, phone and approvals convenience; last-resort worker only
             because of its small shared weekly budget.
   grok-bot: recommended orchestrator yes, worker last resort (you can override either).
   codex: recommended worker yes, orchestrator no (you can override either).
   ```

3. Asks separately whether to enable it as a **worker** and/or as an **orchestrator**.
4. For each enabled tool, asks consent and runs a **tiny smoke test**: a fixed "reply exactly
   `DLR_SETUP_OK`" prompt in a throwaway git repo, bounded to 30 seconds. Grok Bot is checked by pasting
   its chat reply. The smoke only sets verification status: a tool you enabled stays enabled whether
   its smoke passes (`passed`), fails (`failed`) or is declined or skipped (`unverified`). Live quota
   and auth checks still gate every dispatch.
5. Asks for per-tool **reserve** percentages (default 15%; the 85% hard pacing gate never changes), the
   **preferred orchestrator**, the **switch policy**, and optional default/per-role **pins**, each with a
   required reason.

`cli` (the local terminal) can also be enabled as an orchestrator; it is never a worker.

## What gets written

Only the setup profile (default `<config dir>/setup.json`, mode 0600). If one exists, a private
timestamped backup (`setup.json.backup-<UTC stamp>-<id>.json`) is written first, then the new file
replaces it atomically. The wizard refuses `config.json`, `repos.json`, `.env*`, secrets/token/credential
names, anything under `~/.codex`, and symlinked destinations.

When `setup.json` exists and neither `--config` nor `$LLM_ROUTER_CONFIG` is set, DLR loads
`defaults <- config.json <- setup.json`: your existing `config.json` (binaries, timezone, custom
settings) still applies, and is never modified; the setup profile decides worker candidates, role and
class lists, reserves and orchestrator preferences. Three things from `config.json` always win: tools in
`never` are dropped from every list, a meter you set to `"enabled": false` stays off, and your
`unmetered_workers` are kept alongside the setup ones. With an explicit `--config`/`$LLM_ROUTER_CONFIG`, that
file is used alone, as before. Without a setup profile nothing changes.

## Automatic order, explicit overrides

Dispatch keeps the #208 automatic order. Only enabled workers are candidates; disabled tools (including
an auto-appended Grok Bot fallback) are filtered from every worker, class and role list.

Overrides, all logged with their reason to `<state dir>/logs/setup-decisions.jsonl`:

| Override | How | Logged source |
|----------|-----|---------------|
| Per job | `run-task` / `run-job` / `bot-dispatch --worker codex --override-reason "needs sandbox"` | `explicit_flag` |
| Per role | wizard role pin, e.g. coder -> claude | `role_pin` |
| Default | wizard default worker pin | `default_pin` |
| Orchestrator per job | `run-job` / `units run --orchestrator cli --override-reason "..."` | `explicit_flag` (event `orchestrator_override`) |

A pinned or explicit worker still passes every hard gate (bans, quota, reserve, pacing). A worker that
is disabled in setup is refused before any work starts. A pin to Grok Bot is a manual handoff, not a CLI
launch, so automatic allocation still runs. Grok Bot always sorts last among enabled workers; to use it
earlier, pin it for a role.

`units run` honours the same pins. Units are allocated as `coder` work, so the coder pin (else the default
pin) is applied to every automatically allocated unit, both when the batch is planned and when each unit
is re-planned before it runs. A pinned worker that fails a gate leaves its unit deferred with
`pinned worker X not eligible: <reason>`; it never falls back to another worker. An explicit assignment
(`--assign-file` or an inline `assign`) wins over the pin. The pin is logged once per batch, after the
units and assignment checks pass. A coder orchestrator pin applies to the batch's orchestrator.

A per-job `--orchestrator` applies to that job only: it is logged, but does not change the saved current
orchestrator (use `orchestrator switch` for that). `cli` and `aider-ollama` as orchestrators need a setup
profile.

## Orchestrator switching

Whichever app calls DLR (Claude, Codex, Cursor, Grok Build, Grok Bot, or `cli`) is detected as the
**caller**. What happens next depends on the saved switch policy:

| Policy | Behaviour |
|--------|-----------|
| `always_ask` (default) | Propose the caller and ask: `yes` / `keep current` / `use recommended`. |
| `always_accept_caller` | Switch to the caller if it is an enabled orchestrator. |
| `always_use` + target | Always use that orchestrator. |

Before asking, DLR prints the current and proposed orchestrator and a **recommendation** built from the
live #208 usage snapshot: remaining percentage per window, reset times and banked resets (shown as
"not live capacity"). The question is written to stderr (stdout stays JSON) and only asked when both stdin and stderr are a
terminal; otherwise, or if you press Ctrl-D/Ctrl-C, the current orchestrator is kept and the proposal is
recorded as pending. Every decision, switched or not, is
appended to the decision log with its reason, and the job summary keeps an `orchestrator_decision`.
For `run-job` and `units run` the question is asked up front, but the decision is logged only after the
repo, task, branch and units checks pass, so a refused job never records a switch.

Change it any time:

```sh
llm-router orchestrator status                                   # current, caller, recommendation
llm-router orchestrator switch codex --reason "grok weekly low"  # asks to confirm; --yes to skip
llm-router orchestrator prefer --mode always_accept_caller        # saved to setup.json (backed up)
llm-router orchestrator prefer --mode always_use --target cli
llm-router orchestrator prefer --mode always_ask --preferred claude
```

`prefer` needs an existing setup profile (run `llm-router setup` first) and rewrites only that file.

## Dry-run explain

```sh
llm-router plan --explain
llm-router plan --explain --worker codex --override-reason "bulk refactor"
llm-router plan --snapshot usage-snapshot.json --explain     # fully offline replay
```

`plan --explain` adds to the normal order and exclusions: the preferred orchestrator and its rationale,
tools disabled per capability, reserve floors, every captured window's remaining percentage and reset,
banked resets, any pin with its reason, and the orchestrator preview (current, proposed,
recommendation). It runs no worker and writes no decision log.
