#!/usr/bin/env bash
# Repository "done" gate (DLR #216). Prefer: llm-router gate --repo "$(cd "$(dirname "$0")/.." && pwd)"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec llm-router gate --repo "$ROOT"
