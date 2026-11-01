# Bring any worker

You choose your own worker order. These config fragments are also packaged under
`src/llm_router/examples/workers/`; merge a fragment into config.json and add its worker to worker_order.
Flags must be checked against your installed CLI version before use.

- [aider](../examples/workers/aider.json): `aider --yes-always --no-auto-commits --no-gitignore --message-file {prompt_file}`;
  file delivery, explicitly unmetered.
- [aider with Ollama](../examples/workers/aider-ollama.json): adds `--model ollama_chat/qwen2.5-coder:14b`;
  allows parent `OLLAMA_API_BASE` plus essential environment variables, explicitly unmetered.
- [Nativ](../examples/workers/nativ.json): aider against a local [Nativ](https://github.com/Blaizzy/nativ)
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
- [opencode](../examples/workers/opencode.json): `opencode run {prompt}`, explicit arg delivery.
  Warning: prompt passed in argv: visible to other local users in the process list.
- [custom quota command](../examples/workers/custom-with-quota.json): made-up sample-worker usage command,
  named percent regex, monthly window, timeouts and freshness. Replace it with your own CLI.

`llm-router init --workers aider,opencode --non-interactive` installs the selected presets and marks workers
without enabled meters unmetered (not paced). An installed Ollama CLI offers the aider-ollama preset;
it still requires aider and a configured local model. No detection step executes a CLI.

See [configuration](configuration.md) for safe environment allowlists, cwd constraints, success checks,
command quota settings and forced-worker semantics. All workers retain denylist shims and leak checks.
