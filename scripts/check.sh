#!/usr/bin/env bash
# Repository "done" gate (DLR #216).
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if command -v llm-router >/dev/null 2>&1; then
  exec llm-router gate --repo "$ROOT"
fi
exec python -m llm_router.gate --repo "$ROOT"
