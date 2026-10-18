# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [SemVer](https://semver.org/) (0.x: minor versions may break config).

## [0.3.0] - 2026-10-18

- Bring any worker: config-defined CLI adapters, prompt delivery, safe environment allowlists,
  working directories, success checks and command quota observers.
- Choose your own order with interactive init; packaged defaults select no workers.
- Claude Code is a built-in opt-in adapter with an experimental usage reader and per-worker env prefixes.
- Forced workers override ordering and eligibility while preserving denylist and pacing checks.
- Add aider, aider with Ollama, opencode and custom quota examples; update CI actions to v7.

## [0.2.0] - 2026-10-18

First public release, extracted from a private single-user setup and generalised.

### Added
- `llm-router` console command (pip-installable, standard library only, Python 3.10+).
- XDG config/state dirs (`LLM_ROUTER_CONFIG_DIR`, `LLM_ROUTER_STATE_DIR`, `LLM_ROUTER_CONFIG`), packaged defaults
  deep-merged with the user's `config.json`; `init`, `doctor`, `paths`.
- Generic `worker_order`, `never` denylist (with PATH shims), `unmetered_workers`, `binaries` overrides,
  configurable time zone and env-prefix stripping.
- Pluggable quota observers (`quota.<worker>.observer = "module:function"`), each run in an isolated child
  process with a hard timeout.
- `llm-router scan [--history]` to leak-check a tree before publishing.
- Optional Claude Code command template (off by default) and optional OpenAI-compatible `developer` worker.
- LaunchAgent template with a non-clashing default label; systemd user timer and cron examples.
- Unit tests with fake workers, fake `gh` and fixture screens; GitHub Actions CI.

### Fixed
- Cursor usage reader could hang a quota refresh: pipes held by grandchildren and pty reads without a
  deadline. Readers now kill the whole process group and return `unknown` (`hard_timeout`) instead.
- Grok Build usage reader: strict parse of the weekly line, reset must be within 8 days, clear reason codes
  (`cli_missing_or_no_version`, `capture_failed:*`, `parse_failed`).
- Leak check false positives on JSX `key=`, `event.key`, Swift `forKey:`, `CodingKeys`, `let key =`, lower-case
  identifiers and ignored untracked files (`node_modules`, build dirs). Vendor key shapes, `.env` assignments
  and quoted secret literals are still blocked.

### Changed
- Timestamps use the configured zone and drop the `_ist` field suffix (`started_at`, `expires_at`, `resets`, …).
- Grok Build and Cursor meters are opt-in (`enabled: false`) and marked experimental.
