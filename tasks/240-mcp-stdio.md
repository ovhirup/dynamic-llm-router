# Issue #240: local MCP stdio server

Base: `f5a7e55b71991c0115916fd9922086e7a28f43b2` only.
Branch: `codex/240-mcp-stdio`. Separate worktree; local commits only.
Spec: https://github.com/ovhirup/dynamic-llm-router/issues/240 and the user's done-check.

## Done-check (written before implementation)

- [x] `llm-router mcp` / `dlr mcp`: stdio JSON-RPC, standard library only; no hard dependency changes.
- [x] Input and output JSON schemas and CLI parity contract tests for select_worker,
  quota_status, list_jobs, job_status, get_review, scan, check, list_pending_approvals, run_job.
- [x] Default tools/list contains the eight read-only tools; run_job requires boolean
  `mcp.enable_run_job: true`, with discovery and direct-call tests.
- [x] Calls to approve/push/merge and approval-store mutation attempts fail; approval store unchanged.
- [x] Existing redaction covers all responses/errors; fake key planted in a job log cannot escape.
- [x] Fake-worker run_job tests retain never, pacing, unknown-means-no, tests, leak scan and local commit.
- [x] Every MCP request/notification/tool call is audited in the existing decision log with tool/client.
- [ ] README config snippets for Claude Code and Cursor. Two real-client checks remain undone
  unless actually run; do not add MCP to Built.
- [x] Existing full suite plus new contracts run serially with `PYTHONPATH=src:tests`, zero skips.
- [x] Review diff and leak scan. Local commit and final report follow these gates;
  the final commit/state are recorded in HANDOFF.md and the final response.

## Implementation steps

1. Add test-first contracts and thin command adapter in `src/llm_router/mcp_server.py`.
   Invoke the CLI entry point with an already loaded policy; capture stdout/stderr so stdio
   carries JSON-RPC only. A fixed allowlist builds argv from strictly validated schemas.
   Output envelope: `{exit_code, data}`; data is the CLI JSON value or status table text.
2. Add CLI mcp entry, jobs `--job` projection, and pending-approvals inspection shared with MCP.
   Reuse existing approval_status; no approval writes, routing decisions or gate logic.
3. Add strict boolean opt-in validation and extend existing decision audit allowlist for MCP.
4. Add docs and tests, run full suite serially, inspect diff and scan, commit locally.

## Review focus

- Invalid arguments / unknown fields must fail before invoking a CLI command.
- Unknown tools, notifications, parse errors and errors must stay redacted and keep stdout clean.
- Explicit worker requests must traverse CLI preference handling and all existing safety gates.
- Missing, expired, moved-head and consumed approvals remain CLI decisions; listing never writes them.
- Config values such as strings or integers must never enable run_job.

## Verification commands and expected results

`PYTHONPATH=src:tests python3 -m pytest -q tests/test_mcp_server.py`: all contracts pass, zero skips.

`PYTHONPATH=src:tests python3 -m pytest -q`: all existing and new tests pass, zero skips.

`PYTHONPATH=src:tests python3 -m unittest discover -s tests`: pass, zero skips.

Repository leak scan using an isolated test policy: clean.

## Progress

- Worktree created from exact base; branch created; stale branch never read or checked out.
- Worktree relocated unchanged to `/private/tmp/dlr-240-mcp-stdio`: existing secret-path
  guard blocks repository context reads under the app-created `.codex` worktree location.
  App attachment cannot register the relocated checkout; Git worktree and branch remain intact.
- Host has `python3`, no `python` command. Initial sandbox baseline interrupted:
  821 passed, 18 infrastructure failures (nested sandbox/ps denied), 694 subtests.
  Unsandboxed run before relocation interrupted at 98 passed. Neither is a final gate.
- Test-first: initial contracts demonstrated missing server. Fixture corrections required
  after first two gates; advisor identified explicit-worker `read_meter` path. Assertions kept.
- Advisor found completed exact-SHA merges incorrectly pending; regression failed before fix.
- Contracts: `PYTHONPATH=src:tests python3 -m pytest -q tests/test_mcp_server.py`:
  **24 passed, zero skipped**, 3.09s. One existing Python 3.14 ctypes deprecation warning.
- Serial full default suite from relocated worktree, outside outer OS sandbox:
  `PYTHONPATH=src:tests python3 -m pytest -q`: **1089 passed, 778 subtests passed,
  zero skips**, 535.56s. One existing Python 3.14 ctypes deprecation warning.
- Independent verifier GATE PASS:
  - `PYTHONPATH=src:tests python3 -m unittest discover -s tests`: 1065 tests, OK,
    zero skips, 537.921s. Intentional failing gate-fixture subprocess is expected.
  - MCP contracts independently repeated: 24 passed, zero skips, 3.12s.
  - Isolated-policy leak scan: clean, 310 files, no findings (secret shapes, no private terms).
  - Diff/scope/dependency checks pass; no approval/publication dispatch paths.
- Packaged entrypoint smoke succeeded for `llm-router mcp`, `dlr mcp`, and
  `python3 -S -m llm_router mcp` each exposed eight read-only tools with empty stderr.
  Built/installed package with no runtime dependencies under a temporary prefix.
  Host lacked the dlr console script and setuptools; installed the already-declared
  setuptools build backend into a temporary directory only. Global installation unchanged.
- Research: pinned base already includes task_dispatch pacing and landed check gate.
- Real-client validation: INCOMPLETE; do not list under Built.
  - Temporary project configuration only; no client/account settings changed by this task.
  - Claude Code `--bare --mcp-config <temporary file> --strict-mcp-config mcp list`:
    timed out after 30s; no server handshake recorded. Initial attempt had variadic-option
    ordering error and was corrected before the timed attempt.
  - Cursor `--workspace <temporary project> mcp list-tools dlr-240-smoke`:
    exit 1, MCP server has not been approved. Did not approve it or bypass client trust.
  - These attempts are not successful interoperability checks. Scripted entrypoint smoke
    is separately reported and does not substitute for real clients.
- Final advisor review approved the implementation. The final scan flagged the prose
  phrase naming a successful check as a password-shaped assignment; rephrased the note
  and reran the unchanged leak scanner. No scanner allowlist or gate changes.

## Changed files

- `src/llm_router/mcp_server.py`: stdio protocol, tool schemas/allowlist, CLI adapter, audit and redaction.
- `src/llm_router/cli.py`: mcp command, shared policy injection, jobs --job and pending-approvals.
- `src/llm_router/config.py`, `src/llm_router/default_policy.json`: strict opt-in, disabled default.
- `src/llm_router/job_dispatch.py`: single-job view of existing jobs projection.
- `src/llm_router/pr_gate.py`: read-only pending projection over existing SHA-bound approval state.
- `src/llm_router/session_preferences.py`: extend existing decision log for MCP method/tool/client.
- `tests/test_mcp_server.py`: contract, safety, redaction, audit, discovery and protocol tests.
- `README.md`: Claude Code/Cursor snippets and explicit unvalidated status.
- `tasks/240-mcp-stdio.md`: pre-implementation done-check and observed validation results.
