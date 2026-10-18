# Quota observers

The selector only routes to a worker whose meter has **fresh, valid evidence below its threshold**. An observer
produces that evidence. Every observer is run by `llm_router.observers.observe_isolated`: a child Python
process in its own session with a hard wall-clock timeout (`quota.<worker>.hard_timeout_seconds`). If it
hangs, the whole process group is killed and the reading is `unknown` with reason `hard_timeout`. A quota
refresh never blocks on a meter.

## Contract

```python
def observe(config: dict) -> dict:
    return {
        "status": "ok" | "unknown",
        "reason": None | "short_reason_code",
        "evidence": None | {"windows": {...}, "observed_at": "...", "expires_at": "...", ...},
        "cli_version": None | "1.2.3",
        "version_warning": None | "...",
    }
```

`config` is the merged policy. Register a plugin with `"quota": {"<worker>": {"observer": "my_pkg.mod:observe",
"enabled": true, "freshness_seconds": 900, "usage_threshold_percent": 85}}`. Results are normalised: a missing or
malformed field becomes `unknown`, and `unknown` is never eligible.

## Built-in observers

| Worker | Source | Status | Notes |
|--------|--------|--------|-------|
| `codex` | `codex app-server` JSON-RPC `account/rateLimits/read` | official, on | 5-hour (`primary`) and weekly windows; both must be fresh. |
| `grok-build` | interactive `grok --no-auto-update dashboard` → `/usage` screen, read in a pty | experimental, `enabled: false` | Parses exactly one `Weekly limit (<plan>) <bar> NN% Resets: <Month> <d>, HH:MM` line; the reset must be in the next 8 days. |
| `cursor` | interactive `cursor-agent --mode ask` → `/usage` screen in an empty scratch workspace | experimental, `enabled: false`, `consult: "fallback"` | Parses the Included / Auto / API "% used" rows and the reset date. Consulted only when earlier workers are unavailable. |

### Why screen reading, and its limits

At the time of writing neither the Grok CLI nor the Cursor CLI exposes a usage API, so the experimental observers
open each CLI's own usage screen exactly as you would, type `/usage` and read the text. They send no prompt, call
no model and read no credential file. Each run is bounded by `capture_timeout_seconds` plus the hard timeout.

These parsers are deliberately strict. A vendor UI change, an unexpected update prompt, a different plan label
layout or any doubt produces `status: "unknown"` with a reason code (`cli_missing_or_no_version`,
`capture_failed:<why>`, `parse_failed`, `hard_timeout`, …), never a guessed number. `llm-router status` shows the
reason, and `quota refresh` raises a stale alert after `stale_alert_minutes`. Pin `expected_cli_version` to get a
`version_warning` alert when the CLI changes.

Check the vendor's terms before enabling a screen-reading observer. Replace it with an official API observer
when one exists. The plugin hook is there for exactly that.

### Fixtures

`tests/fixtures/*.bin` are scrubbed raw pty captures used by the parser tests. When a CLI changes its screen,
add a new scrubbed capture and a test, and keep the old one if the old format can still appear.
