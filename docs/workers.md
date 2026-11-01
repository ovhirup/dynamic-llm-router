# Bring any worker

You choose your own worker order. These config fragments are also packaged under
`src/llm_router/examples/workers/`; merge a fragment into config.json and add its worker to worker_order.
Flags must be checked against your installed CLI version before use.

- [aider](../examples/workers/aider.json): `aider --yes-always --no-auto-commits --no-gitignore --message-file {prompt_file}`;
  file delivery, explicitly unmetered.
- [aider with Ollama](../examples/workers/aider-ollama.json): adds `--model ollama_chat/qwen2.5-coder:14b`;
  allows parent `OLLAMA_API_BASE` plus essential environment variables, explicitly unmetered.
- [opencode](../examples/workers/opencode.json): `opencode run {prompt}`, explicit arg delivery.
  Warning: prompt passed in argv: visible to other local users in the process list.
- [custom quota command](../examples/workers/custom-with-quota.json): made-up sample-worker usage command,
  named percent regex, monthly window, timeouts and freshness. Replace it with your own CLI.

## Role-based routing & direct Claude Code tips (#135)

DLR can split work by role (`--role {planner,coder,searcher,judge,advisor}`):
- `planner`, `judge`, and `advisor` route to the strongest available worker (`claude > grok-build > codex`).
- `coder` routes to the next worker with budget (`codex > grok-build > cursor > claude > aider-ollama`).
- `searcher` routes to local/cheaper workers first (`aider-ollama > developer > cursor`).
- When a `coder` hits repeated identical errors or a loop, DLR escalates a transcript summary to the `advisor` role and feeds its guidance back to the `coder`.
- For direct interactive Claude Code sessions, use `/model opusplan` (Opus plans, Sonnet executes), delegate file/codebase searches to a Haiku subagent (`CLAUDE_CODE_SUBAGENT_MODEL=haiku`), and keep autocompact enabled so long transcripts never burn Opus quota.

`llm-router init --workers aider,opencode --non-interactive` installs the selected presets and marks workers
without enabled meters unmetered (not paced). An installed Ollama CLI offers the aider-ollama preset;
it still requires aider and a configured local model. No detection step executes a CLI.

See [configuration](configuration.md) for safe environment allowlists, cwd constraints, success checks,
command quota settings and forced-worker semantics. All workers retain denylist shims and leak checks.
