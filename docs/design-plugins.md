# Design Task Class & Design Plugins (`design`)

## 1. Overview

DLR supports a `design` task class (`--class design`) for jobs that touch UI (`SwiftUI`, `React`, `CSS`, `Tailwind`, layout/component screens). Because UI quality depends on strong reasoning and design-system context rather than raw typing speed, `design` tasks route to strong cloud workers first (`claude > grok-build > cursor > codex > grok-bot`) and never fall back to small local models without review.

## 2. Candidate Evaluation for DLR (#90)

| Candidate | Role | Evaluation for DLR | Verdict |
|---|---|---|---|
| **Framelink Figma MCP** (`GLips/Figma-Context-MCP`) | Read-only Figma layout/token context over MCP stdio | Supplies structured layout frames, spacing, and colour tokens directly into worker context without screenshots | **Adopt as optional `context` plugin** (`src/llm_router/examples/plugins/figma-context.json`, `enabled: false` by default) |
| **Impeccable** (`pbakaus/impeccable`) | Design critique & anti-slop rules | Rules (no purple gradients, no centered hero, no emoji icons, strict typography hierarchy) are lightweight and deterministic | **Fold rules directly into DLR's UI prompt guardrails** (#92) rather than requiring a separate server |
| **UI UX Pro Max skill** (`nextlevelbuilder/ui-ux-pro-max-skill`) | General frontend prompt templates | Overlaps with DLR's shared skills (#96) and increases prompt token overhead if loaded globally | **Keep optional per-task**; load only for `design` class jobs |

Out of scope for DLR (belong to Lakshly/site projects, not the router): video (HyperFrames), Blender 3D, and animation libraries.

## 3. Plugin Manifest Example

`src/llm_router/examples/plugins/figma-context.json`:

```json
{
  "name": "figma-context",
  "capabilities": ["context"],
  "transport": "mcp_stdio",
  "command": ["figma-developer-mcp", "--stdio"],
  "description": "Opt-in Framelink Figma MCP design context plugin for UI/design tasks (SwiftUI, React, CSS).",
  "enabled": false,
  "permissions": ["read_design_context"]
}
```

Credentials (such as `FIGMA_API_KEY`) must be passed only through the host environment allowlist (`env.allow`), never stored in `config.json` or the plugin manifest.
