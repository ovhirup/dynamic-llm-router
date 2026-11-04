# ADR 235 — Optional orchestrator adapters

DLR owns worker selection, test evidence, review, and worktree safety. Framework integrations need access to the same job operations so that their scheduling and tracing can be evaluated against the native runner. Keeping framework imports in optional adapters preserves the zero-dependency core.

`llm_router.adapters.Orchestrator` is a structural Python protocol. Pass an implementation to `run_job(..., adapter=adapter)`. With no adapter argument, `NativeOrchestrator` runs the existing operations directly. The separate `orchestrator` argument still selects the caller identity for budget handoffs.

| Hook | Native operation |
| --- | --- |
| `plan` | Assemble the repository context before the first attempt. |
| `dispatch` | Run the initial worker cycle using DLR's selection and failover. |
| `test` | Run the configured tests, including the fail-first baseline when required. |
| `judge` | Prepare the stronger-model review and invoke the reviewer when required. |
| `fix` | Dispatch a retry or correction with DLR's existing feedback. |

Each hook receives a callable and its arguments. An adapter can run the operation as a synchronous framework node. It must call the operation once, return its result, and propagate exceptions to the runner. Test and fix hooks can recur when DLR retries. The callbacks are scoped to the active job and worktree. DLR retains phase ordering, retry decisions, validation, cleanup, and approval gates. Adapters are trusted in-process code.

Future framework implementations belong under `src/llm_router/adapters/`. The optional install groups are `dynamic-llm-router[langgraph]`, `dynamic-llm-router[crewai]`, and `dynamic-llm-router[agent-framework]`. Their Python requirements come from the upstream packages. These extras supply dependencies for future adapters. The native runner uses none of them.

Related [adapter issue #235](https://github.com/ovhirup/dynamic-llm-router/issues/235) and [external integrations epic #73](https://github.com/ovhirup/dynamic-llm-router/issues/73).
