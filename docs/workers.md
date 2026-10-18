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

`llm-router init --workers aider,opencode --non-interactive` installs the selected presets and marks workers
without enabled meters unmetered (not paced). An installed Ollama CLI offers the aider-ollama preset;
it still requires aider and a configured local model. No detection step executes a CLI.

See [configuration](configuration.md) for safe environment allowlists, cwd constraints, success checks,
command quota settings and forced-worker semantics. All workers retain denylist shims and leak checks.
