# Hark Evaluation: Task-Input Source (Not a Worker)

Status: decided (31 Oct 2026). Owner: Abhirup. Related: #15 (Local LLM integration), #73 / #76 (External tool integrations).

## 1. What Hark Is

**Hark** (`com.harklabs.hark.mac` 0.1.18, installed locally on the M4 16 GB Mac) is a local voice dictation and meeting transcription application with an MCP server (`hark-mcp`). It is **not** a generative coding LLM and cannot edit repositories, run tests, or act as a DLR execution worker.

## 2. Evaluation: Can `hark-mcp` Transcripts Seed Task Files?

**Yes — as an opt-in input source and context plugin (#76), never as a worker.**

- **Task-file seeding (`input_source`):** Dictated notes and meeting action items retrieved from `hark-mcp` can be formatted into `0600` markdown task files (`# <Title>`, `## Source`, `## Goal`, `## Action items`, `## Allowed paths`) after passing through `security.redact` and private-term filtering.
- **Meeting context lookup (`context`):** When a task references a recent design or planning discussion, a read-only `hark-mcp` search can attach a short redacted transcript excerpt to the task file before `llm-router run-job` dispatches it to a coding worker (`claude`, `codex`, `grok-build`, `cursor`, or `aider-ollama`).
- **RAM and privacy:** Hark's transcription runs locally on Apple Silicon; keeping it decoupled from `run_task.commands` ensures it never competes with `qwen2.5-coder:14b` or `nativ` under the router's coding worker loop.

## 3. Owner Decision Recorded

1. **No worker is added for Hark.** `hark` must never appear in `worker_order`, `class_order`, `run_task.commands`, or `unmetered_workers`.
2. **Use Hark strictly as an opt-in input/context plugin (`hark-mcp`, #76):** transcripts may seed markdown task files that are reviewed/redacted before `llm-router run-job` dispatches them to a real coding worker.
