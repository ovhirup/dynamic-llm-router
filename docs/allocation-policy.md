# Budget-aware allocation (#208)

DLR recomputes automatic worker order from fresh quota evidence before a task and
before each failover. The configured `worker_order` or class order supplies the
candidate list and breaks exact score ties. Existing bans, account checks, health,
cooldowns, quota thresholds, role restrictions and Grok pool pacing still apply.
Explicit workers and Stackwich pins retain their authority and are explained as
overrides. Grok Bot remains a serial orchestrator fallback.

## Inspect and replay

```sh
llm-router plan --role coder
llm-router plan --class review --cached-only --json
llm-router plan --snapshot usage-snapshot.json --role judge
```

`plan` runs no task. Its human output gives the order, score components, exclusions
and explicit-action recommendations. JSON adds the complete versioned `snapshot`
so the output can itself be replayed with `--snapshot`. Offline replay loads no
user settings or observers and uses the saved `as_of`, never the current clock.
`--class` applies when collecting a plan; it cannot change a saved candidate list.
Use `--size draft` to inspect draft eligibility.

`--explain` adds setup preferences, disabled capabilities, reserves, captured
windows and resets, pins and the orchestrator preview; `--worker`/`--orchestrator`
preview an override without logging it. See [setup.md](setup.md).

The normal collector path uses fresh cache first. A fallback-only observer may
contribute fresh cached evidence without a live consultation. Unknown, stale,
malformed or future evidence cannot make a metered worker eligible.

## Snapshot version 1

| Field | Meaning |
| --- | --- |
| `schema_version`, `as_of` | Version 1 and the explicit timezone-aware decision time. |
| `configured_order` | Ordered, unique worker IDs. |
| `workers` | Provider/pool mapping, existing gate verdict/reason, quota threshold, local/unmetered flags. |
| `providers` | Shared windows, required-window IDs, independent pools, reserves, reset banks and on-demand cap. |
| `role_order` | Frozen preference lists for planner, coder, searcher, judge and advisor. |
| `pacing` | Frozen Grok pool members and week-curve verdict. |
| `reset_rule` | Null by default; optionally `recommend_before_expiry` with `within_hours`. |

Each window carries `kind` (`5h`, `weekly`, `monthly` or `unknown`), `percent_used`,
`observed_at`, `expires_at` and `reset_at`. Original window IDs are retained:
`primary` and `session` normalize to 5h; Cursor's `included`, `auto` and `api`
normalize to monthly. Missing values remain unknown. Pools have their own
`windows` and `required_windows`; sibling pools are never combined.

A provider has `reserve_percent` (default at least 15), optional explicit
`hard_reserve_percent`, nullable `banked_resets` and nullable `on_demand`.
Bank entries contain `id`, positive integer `count`, and aware `expires_at`.
On-demand metadata contains `enabled`, `used` and `cap` in the same units.

The pure adapter accepts already collected evidence and explicit provider metadata.
Programmatic policy inputs use `allocation.worker_pools` to map a worker to its
actual configured provider/pool and `allocation.providers` for supplied metadata;
`build_snapshot(..., provider_usage=...)` also accepts supplied provider readings.
A mapping describes the pool the worker's configured command actually uses. It
does not change the command's model or guess a pool from a generic percentage.
No user configuration files are rewritten.

Existing collectors do not yet supply banked resets or the named Cursor Models /
Other Models split. Those values remain unknown unless explicitly supplied. The
existing Cursor collector's conjunctive required windows remain conservative.

Shared observations merge without summing allowance: newest observation wins;
simultaneous readings retain maximum usage and earliest expiry. Conflicting
reset/kind information or ambiguous readings fail closed. Required windows are
unioned, and older metadata cannot overwrite fresh exhaustion.

## Eligibility and scoring

Any exhausted required window blocks its provider/pool until reset. Passing a
reset time does not imply zero usage: a new reading for the new period is needed.
Ordinary work is excluded when remaining allowance is at or below the provider's
reserve. Planner, judge and advisor can access a separately declared hard-role
reserve only within existing quota gates. This is admission control from observed
usage, not a forecast of how much a running task will consume.

For eligible workers:

```text
score = 50 * urgency + 40 * role_fit + 10 * headroom
urgency = 1 / (1 + hours until earliest relevant future reset)
role_fit = 1 - role_rank / max(nonfallback_role_count - 1, 1)
headroom = min((100 - used_percent - applicable_reserve) / (100 - applicable_reserve))
```

Components are in [0, 1]. Unknown reset gives urgency 0. Without a requested role,
role fit is 0. Declared unmetered workers have headroom 1. The tightest relevant
window determines headroom. Exact ties keep configured order without rounding.
Grok week-curve constraints then adjust the numerical order; both score and final
rank are explained. Judge and advisor cannot select local workers.

Disabled or unknown on-demand spending permits no spillover. An explicitly routed
`on_demand` pool requires enabled spending and known `used < cap`. Exhausted or
unknown caps block that route. No paid top-ups or automatic pool changes occur.

Bank metadata alone provides no capacity. The explicit rule
`{"name": "recommend_before_expiry", "within_hours": 24}` may emit an
`action_required` recommendation for an unexpired reset approaching expiry. DLR
never redeems it automatically; only confirmed fresh usage can change eligibility.

## Example fixed snapshot

This example uses package defaults and supplied observations, without reading
personal settings or contacting a provider:

```python
import json
from datetime import datetime
from pathlib import Path
from llm_router.config import defaults
from llm_router.usage_snapshot import build_snapshot

policy = defaults()
policy['worker_order'] = ['codex']
evidence = {'source_worker': 'codex', 'required_windows': ['primary'], 'windows': {
    'primary': {'percent_used': 20, 'observed_at': '2026-11-02T11:59:00+00:00',
                'expires_at': '2026-11-02T12:15:00+00:00',
                'reset_at': '2026-11-02T17:00:00+00:00'}}}
snapshot = build_snapshot(policy, datetime.fromisoformat('2026-11-02T12:00:00+00:00'),
                          ['codex'], ['codex'], {'codex': 'quota_available'},
                          {'codex': evidence})
Path('usage-snapshot.json').write_text(json.dumps(snapshot, indent=2))
```

## Dispatch records and Notch

Tasks and jobs retain the current `allocation`, its `allocation_history`, and each
actual attempt's plan. Before a worker runs, DLR atomically publishes a private
`<stamp>-allocation.json` sidecar beside its output logs. These records contain
minimal redacted explanations, without full snapshots or prompts.

`jobs --live-feed` exposes those explanations for running jobs, recent jobs and
history, including before the first output log exists. The actual Notch rendering
code lives in other repositories; this change supplies DLR's feed fields.

The fully default automatic wave path uses the same allocation policy, filtered by
parallel-pool membership, unit worker restrictions and capability. It rechecks
before unit launch and persists changed worker assignments in the batch manifest.
Explicit assignments and injected assignment/quota callbacks retain their contracts.
