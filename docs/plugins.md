# External Tool Integrations (Plugins)

DLR supports external productivity tools over the Model Context Protocol (MCP) or local commands via a declarative plugin manifest. Plugins extend input, context, notifications, and output storage without becoming coding workers.

## 1. Plugin capabilities

Every plugin declares one or more capabilities:

| Capability | Purpose | Reference example |
|---|---|---|
| `input_source` | Seeds markdown task files from external inputs (dictation, meeting transcripts, issues) | `hark` (`hark-mcp`, #19 / #76) |
| `context` | Supplies read-only notes, rules, or search results into worker prompts | `obsidian-vault` (#50 / #77) |
| `notifier` | Sends redacted job or daily meter summaries | `slack-notifier`, `email-notifier` (#78) |
| `output_sink` | Stores redacted job outcomes, handoffs, and scorecards | `obsidian-vault` (#48 / #77) |

## 2. Manifest schema

Packaged reference manifests live under `src/llm_router/examples/plugins/`. A plugin manifest is a JSON object:

```json
{
  "name": "sample-plugin",
  "capabilities": ["context", "notifier"],
  "transport": "mcp_stdio",
  "command": ["sample-plugin-mcp", "--stdio"],
  "description": "Example MCP plugin providing context notes and summary notifications.",
  "enabled": false,
  "permissions": ["read_vault", "post_summary"]
}
```

### Fields

- `name` (`[a-z][a-z0-9_-]{0,31}`): unique plugin identifier.
- `capabilities` (non-empty list): subset of `input_source`, `context`, `notifier`, `output_sink`.
- `transport`: `mcp_stdio` (JSON-RPC 2.0 over stdio), `command` (bounded subprocess), or `builtin`.
- `command`: argv list (required for `mcp_stdio` and `command`); linted with `security.lint_commands` so inline secrets and credential flags (`--api-key`, `--token`, etc.) are rejected.
- `enabled` (boolean, default `false`): every plugin is opt-in and disabled until explicitly enabled.
- `permissions` (list of strings): explicit permissions the plugin requests (e.g. `read_transcripts`, `read_vault`, `write_vault`, `post_summary`, `send_summary`).

## 3. Consent and secret redaction (#79)

1. **Opt-in only:** plugins never run unless `enabled: true` and the user has granted consent for each requested permission.
2. **No secrets in manifests or logs:** credentials must come from the host tool's environment allowlist, never from manifest `command` or `description`. Every plugin output and audit log line (`<state>/logs/plugins.jsonl`, mode `0600`) passes through `security.redact` and private-term filtering.
3. **Bounded execution:** MCP stdio calls run in their own process group with a wall-clock timeout so a hung plugin server never blocks routing.

## 4. Contributing a new plugin

1. Add a preset JSON manifest under `src/llm_router/examples/plugins/<name>.json` with `"enabled": false`.
2. Ensure `command` uses a bare executable name and declares only the minimum `capabilities` and `permissions` needed.
3. Add unit tests under `tests/` using a fake runner (no live network or external binaries in tests).
4. Run the full suite (`cd tests && python -m unittest discover -t . -s .`, 0 skipped required) and `llm-router scan .` before opening a draft PR.
