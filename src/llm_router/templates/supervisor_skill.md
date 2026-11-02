---
name: wave-supervisor
description: Supervisor skill and operating rules for unattended DLR wave runs (overnight or long DAG jobs).
license: MIT
---

# Unattended Wave Supervisor Skill (`wave-supervisor`)

> Attribution: Adapted for Dynamic LLM Router (DLR) from `babysit-runs` in [swyxio/skills](https://github.com/swyxio/skills/tree/main/babysit-runs) (MIT License, Copyright (c) swyx).

Use this skill when supervising unattended DLR wave runs (`llm-router units run` or overnight DAG batches).

## Core Operating Rules

1. **Continuation brief (`continuation_brief` in job/batch metadata):**
   - Every wave run writes and updates a structured continuation brief on disk (`<state>/logs/waves/<batch_id>-brief.json`) containing:
     - `goal`
     - `mode` (`wave`)
     - `workers` (assigned worker/model per unit)
     - `shared_concurrency_ceiling` (e.g. `local=1` on 16 GB M4)
     - `quality_bar` (`tests_passed_zero_skipped`, `diff_in_scope`, `leak_clean`)
     - `retry_state`
     - `next_check_at`
   - Wakeups and resumptions read this brief from disk, never relying on chat memory or a stale PID.

2. **Adopt, don't duplicate:**
   - Before launching or re-dispatching a unit, inspect the unit's worktree (`HEAD != base_sha` or existing retained commit/output). If a live or completed run already produced the unit's commit, adopt it instead of re-running.

3. **Unknown delivery is not failure:**
   - A client or runner timeout (`timed_out: True` or `status == 'timeout'`) is not the provider's final failure when the worker already left retained output/commits in the unit worktree (`HEAD != base_sha` or retained changes). Inspect the retained worktree and logs before re-dispatching; never treat a timeout with retained output as a reason to duplicate the run or assume quota exhaustion.

4. **Shared ceiling:**
   - Concurrency caps (`run_task.concurrency`, e.g. `local=1` on the 16 GB Mac) apply across every runner sharing the constrained resource via cross-process lock files (`slot_dir`), never per runner.

5. **Stall alert (least-attention principle):**
   - If every worker has stopped or all eligible units in the wave are blocked/deferred (`succeeded == 0` when runnable units exist, or all remaining units are blocked/deferred), emit a single `stall_notice` (`cause` + `next_recovery_step`) and raise a router alert. Stay quiet when units are progressing normally.

6. **Spot-check with a stronger model:**
   - After a worker substitution/failover or repeated failure, mark the unit for stronger-model review (`claude` or `grok-bot`, pairing with #13 and #113).

## Guardrails (DLR rules always win)

- Never push, merge, or approve from a worker or wave runner.
- Never silently substitute a weaker model without recording the hop.
- Tests must run and pass with 0 skipped before any unit reaches `ready_for_human`.
