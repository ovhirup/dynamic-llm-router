# Evaluation harness

Replay real past router jobs against another worker or routing policy and compare the two.
**Gate:** no routing or config change is adopted unless it beats the baseline here.

## 1. Capture a corpus (`eval_corpus.py`)

```sh
llm-router eval capture            # reads <state>/logs/jobs/*.json
llm-router eval capture --limit 50 --out /tmp/corpus.jsonl
```

Each job log becomes one JSONL case: repo, base SHA, task text (from the job, else the Goal of its first
handoff note), test command, size, and the recorded outcome as `baseline` (workers, status, tests,
changed paths from `diff_stat`, attempts, elapsed seconds, tokens if recorded, quota %, reviewer score).
Jobs that were refused, errored, had no tests, or lack repo/base/task text are kept but marked
`replayable: false` with the reasons. Unreadable logs are listed under `skipped`. The corpus is redacted
and written with mode 600.

## 2. Score (`eval_score.py`)

| part | weight | rule |
|---|---|---|
| tests | 0.5 | 1 if tests `passed`, else 0 (`skipped`/`not_run` never pass) |
| diff scope | 0.2 | Jaccard overlap with the baseline's changed paths; without a baseline diff, 1 up to 20 files then 20/n; 0 when tests fail |
| reviewer | 0.3 | reviewer score in 0..1 |

Missing parts are dropped and the remaining weights renormalised. Cost (tokens, quota %) and time are
reported raw. Per policy: pass rate, mean quality, total/mean tokens, mean quota %, median/mean time.

The gate adopts the candidate only if case counts match, pass rate and quality do not regress, and it is
strictly better on at least one of pass rate, quality, tokens, quota % or median time. Ties never adopt.

## 3. Run an A/B (`eval_harness.py`)

Policies file (baseline first):

```json
[{"name": "baseline", "worker_order": ["grok-build", "codex"]},
 {"name": "cursor-first", "worker_order": ["cursor", "codex"], "never": ["grok-build"]}]
```

A spec may pin `"worker"` (no failover) or give `"worker_order"`/`"never"` (up to 3 hops); live mode also
merges `"config"` overrides into the policy.

```sh
llm-router eval run --policies policies.json --fake-workers fake.json        # offline (default)
llm-router eval run --policies policies.json --mode live --yes               # real workers, real quota
```

- **offline**: nothing runs. Each worker has a fake profile (`pass_rate`, `tokens`, `quota_percent`,
  `elapsed_seconds`, `reviewer_score`, `scope` = `"baseline"` or a number of extra files). Pass/fail is a
  deterministic hash of (case, worker), so the same corpus always scores the same. A worker that ran the
  original job replays its recorded outcome (`--no-recorded` turns this off).
- **live**: each replayable case runs through `run-job` at its recorded base SHA in a fresh worktree
  (pruned afterwards; a committed branch is kept). Quota % is the cached-meter delta. Refused without `--yes`.

Reports are written to `<state>/logs/eval/reports/<stamp>-<baseline>-vs-<candidate>.{json,md}`. The command
exits 0 when the gate adopts the candidate, 1 when it keeps the baseline, 3 when refused.
