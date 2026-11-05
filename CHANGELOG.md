# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/);
versions follow [SemVer](https://semver.org/) (0.x: minor versions may break config).

## [Unreleased]

- Add job phase traces (#236): spans for plan, dispatch, test, judge, fix, and approval wait record worker,
  model, tokens, a cost estimate, and exit status. Local JSONL is the default export. OTLP is the optional
  `otlp` extra, with no new core dependency. `dlr report traces --since` prints p50 and p95 latency and cost
  per worker per task class. The leak gate scans trace files.

- Add `llm-router setup` (#210): detects Claude, Codex, Grok Build, Cursor, Grok Bot and Ollama + Aider,
  enables each separately as worker and/or orchestrator with the rationale and recommended default shown,
  smoke-tests every enabled tool, and writes a backed-up `setup.json` layered over `config.json` (never
  edited). #208 still orders workers automatically; `--worker`/`--orchestrator` (with `--override-reason`)
  and saved role/default pins are logged to `logs/setup-decisions.jsonl`. The calling app is proposed as
  orchestrator with a live-usage recommendation and switched only on confirmation or a saved policy
  (`orchestrator switch`, `orchestrator prefer`). `plan --explain` shows why. See `docs/setup.md`.

- Add live stronger-model review for merge-queue units (#113): `llm-router units review --batch B [--unit U]`
  sends each `needs_review` entry's diff, scope and test evidence to claude (else a grok-bot handoff
  prompt) and records the verdict, bound to the head that was reviewed. `--verdict approve|reject` records an
  out-of-band verdict. Never pushes or merges.

- Add the evaluation harness (#61): `llm-router eval capture` builds a redacted replay corpus from
  run-job logs (#62); a scoring rubric covers tests, diff scope, reviewer score, tokens, quota % and time,
  with an adoption gate that never adopts on a tie or a regression (#63); `llm-router eval run` replays
  the corpus under a baseline and a candidate policy, offline with fake workers or live (`--yes`), and
  writes a JSON + Markdown A/B report (#64). See `docs/evaluation.md`.

- Refuse worker launches when the assembled prompt exceeds `run_task.max_prompt_chars` (default 100000),
  including brief, router text, Stackwich instructions and continuation items.

- Add a Claude-only fenced handoff with explicit effort flags, file-reading and checklist instructions,
  and a shared time budget across bounded headless continuations.
- Detect Claude quota from real limit signals: broad limit wording on a non-zero exit or stream error,
  only Claude's own limit messages at exit 0. Allow one paced eligible failover; stop on safety refusals.
- A quota failover from a Stackwich-required attempt only goes to a Stackwich-capable worker; otherwise
  it is skipped with a warning and the job ends `blocked_by_stackwich`.

## [0.4.0] - 2026-10-19

- Add optional worker-neutral Stackwich plan, execute, verify and review evidence.
- Gate commits on strict report validation, exact scope, passing tests and unchanged evidence/content.
- Bound correction to two cycles pinned to the same worker; retain reports outside commits.
- Preserve default-off configuration and ordinary execution for unsupported workers.
- Cover adversarial reports, scope, opt-in configuration, retries and commit gates.

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
