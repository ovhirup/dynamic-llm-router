# Router Brain vault

A plain-markdown Obsidian vault, tracked in git, written by the router and read by workers
(via an MCP filesystem/Obsidian server). See `docs/ROUTER_BRAIN.md` section (c).

## Layout
- `rules.md` — 10-15 standing rules, always loaded into every worker prompt.
- `jobs/` — one outcome note per job (see `jobs/README.md`).
- `handoffs/` — handoff notes between workers, linked to their job (see `handoffs/README.md`).
- `scorecards/` — worker x task-class scorecards (see `scorecards/README.md`).
- `research/` — model-news digests with sourced claims (see `research/README.md`).

Wiki pages beyond `rules.md` are pulled on demand, not always loaded (two-layer pattern).

## Vouch rule (sourced claims)
Every claim about a model or worker capability must cite its source: an eval run id
(e.g. `eval:<run-id>`) or a URL. Unsourced claims are not acted on; changes to
`capabilities.json` require a cited eval run showing a measured win.

`brain.init_vault()` (in `src/llm_router/brain.py`) recreates this skeleton idempotently and
never overwrites existing notes.
