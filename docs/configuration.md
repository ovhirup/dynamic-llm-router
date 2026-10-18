# Configuration reference

The packaged defaults live in [`src/llm_router/default_policy.json`](../src/llm_router/default_policy.json).
Your `config.json` is deep-merged on top: objects merge key by key, and lists and scalars replace. Keys starting
with `_why` are comments. Run `llm-router doctor` after editing.

## Locations

| Variable | Meaning |
|----------|---------|
| `LLM_ROUTER_CONFIG` | Config file path (else `<config dir>/config.json`; a missing file means "defaults only"). |
| `LLM_ROUTER_CONFIG_DIR` | Config dir (else `$XDG_CONFIG_HOME/llm-router`, else `~/.config/llm-router`). |
| `LLM_ROUTER_STATE_DIR` | Caches, ledger, logs, approvals, worktrees (else `$XDG_STATE_HOME/llm-router`, else `~/.local/state/llm-router`). |

Relative paths in the config resolve against the **state dir**, except `run_task.private_terms_file` and
`run_job.repo_config`, which resolve against the **config dir**. `~` is expanded.

## Top-level keys

| Key | Default | Meaning |
|-----|---------|---------|
| `timezone` | `"local"` | IANA zone (e.g. `"Europe/Berlin"`) for display, quota resets and screen parsing. |
| `worker_order` | `[]` | User-chosen preference order; empty means no workers configured. Run `llm-router init`. |
| `never` | `[]` | CLIs that must never run. They are refused as workers and shadowed by refusing PATH shims inside worker environments. |
| `ineligible` | `{}` | `{"worker": "reason"}`: temporarily take a worker out without editing the order. |
| `unmetered_workers` | `[]` | Workers allowed without a quota observer (run unpaced, with a warning). |
| `binaries` | all `null` | Absolute paths for `codex`, `grok`, `cursor`, `claude`; `null` uses `PATH`. |

## `quota.<worker>`

`observer` (`module:function`), `enabled`, `experimental`, `freshness_seconds`, `usage_threshold_percent`,
`hard_timeout_seconds`, `capture_timeout_seconds` / `app_server_timeout_seconds`, `consult`
(`always` | `fallback`), `meter` (display name), `expected_cli_version`, and for codex `required_windows`.

## `run_task`

| Key | Meaning |
|-----|---------|
| `commands.<worker>` | argv template. Placeholders: `{codex} {grok} {cursor} {claude} {cwd} {prompt_file} {prompt_pointer}`. Also `{repo}` (checkout root); `{prompt}` requires explicit `arg` delivery. |
| `prompt_delivery.<worker>` | `stdin`, `file` (0600 temp file), explicit `arg` (task exposed in process list), or `pointer` (argv carries only a pointer to a 0600 file). |
| `timeouts_seconds`, `stall_seconds`, `kill_grace_seconds` | Wall-clock limit, no-output watchdog and SIGTERM→SIGKILL grace. |
| `pacing.warn_percent` / `pacing.refuse_full_percent` | 70 / 85 by default. |
| `prompt_prefix` | Text prepended to every task. |
| `quota_error_pattern` | Regex that marks worker output as a quota failure (triggers the one failover). |
| `protect_paths.<worker>` | Files restored after that worker runs (e.g. a hand-written `HANDOFF.md`). |
| `private_terms_file` | Your private names for the leak gate (config dir). |
| `strip_env_prefixes` | Env var prefixes removed from worker environments. |
| `ledger`, `extra_ledgers`, `output_dir`, `shim_dir`, `block_log` | Output locations (state dir). |

## `leak_check`

`skip_untracked_dirs` (vendor/build dirs never scanned when untracked), `allow_regexes` (suppress **only** the
name-based rules such as `.env` assignments and quoted secret literals, never vendor key shapes or private
terms), `max_file_bytes`.

## `run_job`

`worktree_root`, `branch_prefix` (`router/`), `lock_dir`, `jobs_dir`, `repo_config` (`repos.json`),
`test_timeout_seconds`, `test_stall_seconds`, `test_output_tail_chars`, `max_retries` (1), `commit_identity`,
`caffeinate` (macOS only).

### `repos.json`

Keyed by absolute repo path or by repo directory name:

```json
{
  "my-app": {
    "test_cmd": "npm test --silent",
    "test_timeout_seconds": 600,
    "screenshot_cmd": "a headless command that writes PNGs into $SCREENSHOT_DIR",
    "remote": "origin",
    "base": "main",
    "github_repo": "your-org/my-app",
    "visibility": "private",
    "merge_method": "squash",
    "required_checks": ["build", "test"]
  }
}
```

`visibility: "public"` also greps every changed file and the branch name for private terms, and refuses if the
terms file is missing.

## `quota_refresh`, `alerts`, `launch_agent`, `pr_gate`, `developer`

See the inline `_why` notes in the defaults file. Notable: `pr_gate.approval_ttl_hours` (24),
`alerts.macos_notification` (false), `launch_agent.label` (`dev.llm-router.quota`), and
`developer.api_key_env` (the **name** of an env var; the key itself is never stored in config).

## `workers.<name>`

Names match `[a-z][a-z0-9_-]{0,31}`. Built-in names override adapter settings. At load, definitions
materialise into the existing run_task maps and quota configuration.

| Field | Default / behavior |
|-------|--------------------|
| `command` | Required argv list for a new worker. argv[0] is a bare PATH name or absolute executable. |
| `prompt_delivery` | `stdin`; also `file`, `pointer`, `arg`. `{prompt}` only works with explicit `arg`. |
| `cwd` | `.`; relative checkout subdirectory. Absolute paths, `..` components and symlink escapes are refused. |
| `env` | Without `allow`, inherit today's environment after prefix filtering. See below. |
| `timeout_seconds` / `stall_seconds` | 900 / 300. |
| `success` | `{"exit_codes": [0], "require_regex": null, "fail_regex": null, "require_changes": false}`. |
| `quota` | Optional command observer settings below. Without a meter, auto-selection requires `unmetered_workers`. |

Placeholders: `{cwd}`, `{repo}`, `{prompt_file}`, `{prompt_pointer}`, built-in binary placeholders,
and `{prompt}` for arg delivery only. Files are owner-only and removed after each attempt. Every arg-delivery
run and doctor report warns: "prompt passed in argv: visible to other local users in the process list".

`env.allow` is a list of exact names or fnmatch globs. When present, only matching variables and essentials
(PATH, HOME, USER, LOGNAME, SHELL, LANG, LC_*, TERM, TMPDIR) pass through. Refusing shims remain first on PATH.
`env.set` supplies literal non-secret settings; secret-looking names or values are rejected at load.
Pass secrets from the parent environment through `allow`, never literal config. Global
`run_task.strip_env_prefixes` still applies; `run_task.env_keep_prefixes.<worker>` can retain prefixes.
Default: `{"claude": ["ANTHROPIC_", "CLAUDE_"]}`; other workers strip these plus LITELLM_.

Success checks inspect stdout+stderr. Attempts record `success_check`: `exit_code`, `fail_regex`,
`require_regex_missing`, `no_changes`, or null on success. Changes are compared before/after each attempt,
including when an allowed dirty checkout already contains changes. Timeouts remain failures.

A command quota block has `command` (argv), `regex` (exactly one match with named `percent` group),
`window` (`monthly`), `percent_is` (`used` or `remaining`), `timeout_seconds` (20),
`hard_timeout_seconds` (40), `freshness_seconds` (1800), `usage_threshold_percent` (85), and `enabled` (true).
Optional named `reset` is an ISO timestamp with a timezone. Readings outside 0–100 or ambiguous output are unknown.
See [examples](workers.md) and [observers](observers.md).

## Forced workers

| Condition | `run-task --worker X` / `run-job --worker X` |
|-----------|--------------------------------------------|
| Order, `ineligible`, fallback consult | Ignored; defined workers outside order work too. |
| `never` or blocked executable basename | Exit 3: `X is in never; remove it from never to use it`. |
| Full-size at refuse threshold | Refused; pacing still applies. |
| Full-size unknown meter | Refused unless explicitly unmetered. |
| Draft-size unknown meter | Allowed with warning. |
| Failure | No failover. |

`"never": ["claude"]` also blocks `claude-code`, custom workers running either basename, and installs
refusing shims for both. Quota refresh only considers workers in your order; forced workers read only their own meter on demand.

## Init

Interactive `llm-router init` detects PATH binaries without executing them and asks for worker order.
Claude is excluded from the default; its explicit opt-in and experimental meter prompts default No.
`--workers codex,aider --non-interactive` explicitly chooses order. `--never claude`, `--meters codex,cursor`,
`--dry-run` and `--force` are supported. Non-interactive init without --workers exits 2. Force makes an
owner-only timestamped config backup; repos.json and private_terms.txt are written only when missing.
