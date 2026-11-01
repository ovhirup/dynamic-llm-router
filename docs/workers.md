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
