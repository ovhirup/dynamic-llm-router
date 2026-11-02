# Graph Memory Evaluation: Graphiti / Zep vs Letta vs Plain-Markdown Obsidian Vault (#51)

Status: decided (31 Oct 2026). Parent: #46 (Router Brain). *Note: external memory frameworks may age quickly.*

## 1. Context & Constraints

DLR runs on an Apple Silicon M4 Mac with **16 GB unified RAM** alongside local 14B coding models (`qwen2.5-coder:14b` / `nativ` at ~9 GB) and zero third-party Python dependencies (`pyproject.toml` `dependencies = []`). The Router Brain stores job outcomes (`jobs/`), handoffs (`handoffs/`), worker scorecards (`scorecards/`), research digests (`research/`), and standing rules (`rules.md` + `wiki/`).

## 2. Comparison

| Criterion | Plain-Markdown Obsidian Vault (`vault/`) | Graphiti / Zep (`getzep/graphiti`) | Letta (`letta-ai/letta`) |
|---|---|---|---|
| **Infrastructure & RAM** | Zero daemon; plain `.md`/`.json` files in git (< 5 MB RAM) | Requires Neo4j / FalkorDB graph DB + embedding/LLM extraction calls (~1–2 GB RAM + DB process) | Requires stateful Letta server + Postgres/SQLite + LLM token calls per memory write |
| **Token cost per update** | **0 model tokens** (written deterministically by router code) | Multiple LLM calls per episode for entity/edge extraction and deduplication | LLM calls per memory edit (`core_memory_append` / `archival_memory_insert`) |
| **Auditability & git diff** | Native `git diff`, Obsidian view, and `0400`/`0600` file permissions | Opaque graph database state; harder to diff or leak-scan with `llm-router scan` | Database-backed state; not human-diffable in git |
| **Temporal validity** | Handled by `verified_at` dates + 30-day nightly audit (`brain_wiki.py`, #93) | Built-in bi-temporal edge invalidation (`valid_at` / `invalid_at`) | Agent-managed memory blocks without strict source-citation enforcement |

## 3. Decision

1. **Keep the plain-markdown Obsidian vault (`vault/` + `brain_wiki.py` #93 + `vault_mcp.py` #50) as the sole Router Brain store.** It uses zero model tokens, zero background daemons, and zero external dependencies while preserving 100% git auditability and secret scanning.
2. **Defer Graphiti/Zep and Letta:** Do not add a graph DB or Letta server on the 16 GB M4 Mac. Revisit temporal graph indexing only if the vault exceeds **10,000+ notes** and keyword/hierarchical search (`context_navigator.py`, #115) becomes a measured bottleneck in the evaluation harness (#61).
