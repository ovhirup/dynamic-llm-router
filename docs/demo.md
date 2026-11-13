# Demo (draft)

Recorded from one real run, unedited except trimmed waiting time, which is labelled.

This page is a draft until that recording exists. Rehearsal output is not pasted here. Exit codes below are expected properties of the commands, checked by `tests/test_demo_rehearsal.py`.

## What the demo shows

The router reads live quota and names the worker it would pick, including a worker it skips because the meter is stale or unknown. A job then runs in a worktree, passes tests, passes a leak check, and commits on a `router/*` branch, and a stronger, different model reviews that commit. Nothing is pushed until a person approves that exact commit: a push without approval is refused, an approval is bound to the head SHA, and a later commit makes that approval stale.

## What is real and what is not

Real workers and real quota are what the recording uses. The rehearsal test does not: it drives the same commands through fake workers, fixture meters, a local bare remote, and a fake `gh`, and it never calls a model or the network.

Jobs in the same repository share one `.git` today, so `run-job` locks that repository and jobs there run one at a time. Each demo agent has its own clone for that reason. Job isolation is in progress under #213.

No Desktop app automation and no Notch UI.

## Prerequisites

- `llm-router` on `PATH`, from a checkout that already has `llm-router watch` (the #288 slice 2 command).
- The worker CLIs you intend to show, already signed in. The recording does not mock them.
- A `repos.json` entry for the sample, added before the push steps, keyed by the sample directory's name. `push` checks that remote before it checks the approval, so a missing entry is a different refusal.

```json
{
  "sample": {
    "remote": "origin",
    "base": "main",
    "visibility": "public",
    "test_cmd": "PYTHONPATH=src python3 -m unittest discover -s tests"
  }
}
```

`origin` is the sample's real remote. The rehearsal uses a local bare repository instead, and the suite's fake `gh`.

## Setup

```sh
DEMO=~/dlr-demo
mkdir -p "$DEMO"
sh examples/demo/setup.sh "$DEMO/sample"
```

That creates `$DEMO/sample` (a tiny Python package, `DONE_CHECK.md`, and `tasks/`, committed) and three clones beside it: `$DEMO/repo-codex`, `$DEMO/repo-cursor`, `$DEMO/repo-claude`. A second run refuses because the directory exists. Expected exit on that second run: 1. The message starts `refusing:`.

How the sample is built is also in [examples/demo/README.md](../examples/demo/README.md).

Leak-scan the sample before any job. Expected exit when the tree is clean: 0.

```sh
llm-router scan --history "$DEMO/sample"
```

```text
TO RECORD: scan --history reporting the sample clean
```

## Walk-through

Set `$WORKER` to the worker step 2 names (the rehearsal's fixture meters name `codex`). `$JOB` and `$HEAD` come from the `run-job` JSON (`job_id`, and `commit` as the full 40-character SHA).

### 1. Status

Live meters for the configured workers. Expected exit: 0.

```sh
llm-router status
```

```text
TO RECORD: the status table, one row per configured worker, with freshness and eligibility
```

### 2. Select

Which worker is picked and why. A stale meter is skipped with reason `quota_expired`. An unknown meter is skipped with reason `quota_unknown`. Expected exit: 0.

```sh
llm-router select
```

```text
TO RECORD: the selected worker, the reason, and one worker skipped for a stale or unknown meter
```

### 3. Run the job

The task is `tasks/negate.md`, committed inside the sample. The job runs in a worktree, runs the unittest suite, leak-checks, and commits on a `router/*` branch. It does not push. `--worker` is the worker step 2 named, and `--override-reason` is stored with that override. Expected exit when the job commits: 0.

```sh
llm-router run-job --repo "$DEMO/sample" --task-file "$DEMO/sample/tasks/negate.md" --worker "$WORKER" --override-reason "demo: worker named by select" --size draft --test-cmd "PYTHONPATH=src python3 -m unittest discover -s tests"
```

```text
TO RECORD: run-job JSON with status committed, tests passed, leak check clean, a router/* branch, and the commit SHA
```

### 4. Cross-model review

This branch does the stronger-model review inside `run-job` (`job_dispatch.review_required` and `_stronger_review`, the single-job review from #263 and #267). It is not a merge-queue unit batch. Codex, Cursor, and local output require it. Claude's own output does not get this review, so if step 2 names Claude, the walk-through cannot show a cross-model review: run it when `select` names another worker, or state in this write-up that the worker was chosen by hand and why. Claude reviews first. If Claude records `approve`, the job JSON has `review.reviewer` set to `claude` and `review.verdict` set to `approve`, and no second command is required for the verdict.

If Claude is unavailable, `run-job` leaves `review.status` as `pending` and `review.reviewer` as `grok-bot`, and writes a prompt file. The stronger model reviews that packet out of band. Record the verdict with a second command. Expected exit when the review was pending: 0.

```sh
llm-router jobs --job "$JOB" --verdict approve --reviewer grok-bot --notes "reviewed the diff"
```

```text
TO RECORD: the job JSON naming the worker and a different reviewer, with verdict approve
```

`approve` still refuses until an evidence bundle exists for the current head. That bundle is a separate command from the model review. Expected exit when the leak check in the bundle is clean: 0.

```sh
llm-router review "$JOB"
```

```text
TO RECORD: the review bundle summary for this head, including the leak check
```

### 5. Push without approval

Expected exit: 3. Expected refusal text: `push refused: no push approval for` followed by the first 10 characters of the head SHA.

```sh
llm-router push "$JOB"
```

```text
TO RECORD: the refusal JSON and the exit code 3
```

### 6. Approve this SHA, then push

The approval is bound to the full head SHA. Expected exit of `approve`: 0. Expected exit of `push`: 0. The push is not a force push. It opens a draft pull request.

```sh
llm-router approve "$JOB" --action push --sha "$HEAD"
```

```text
TO RECORD: the approval record whose sha is the full head
```

```sh
llm-router push "$JOB"
```

```text
TO RECORD: push JSON with pushed true and a draft pull request URL
```

### 7. Stale approval

Add a new commit on the `router/*` branch after the approval (the rehearsal commits in the job's worktree). Push again with the same command. Expected exit: 3. Expected refusal text: `push refused: push approval was for an older commit`.

```sh
llm-router push "$JOB"
```

```text
TO RECORD: the stale-approval refusal JSON and the exit code 3
```

## Parallel run

Three agents, three clones, because a second `run-job` in one repository is refused while the first holds the repository lock. The write-up says this plainly: jobs in the same repository share one `.git` today, each demo agent has its own clone for that reason, and job isolation is in progress under #213.

`examples/demo/run-parallel.sh` runs the three commands below and saves `watch --once` frames. You can run the script, or run the commands yourself.

```sh
sh examples/demo/run-parallel.sh "$DEMO"
```

```sh
llm-router run-job --repo "$DEMO/repo-codex" --task-file "$DEMO/repo-codex/tasks/subtract.md" --worker codex --override-reason "demo: one agent per clone" --size draft --test-cmd "PYTHONPATH=src python3 -m unittest discover -s tests"
```

```sh
llm-router run-job --repo "$DEMO/repo-cursor" --task-file "$DEMO/repo-cursor/tasks/multiply.md" --worker cursor --override-reason "demo: one agent per clone" --size draft --test-cmd "PYTHONPATH=src python3 -m unittest discover -s tests"
```

```sh
llm-router run-job --repo "$DEMO/repo-claude" --task-file "$DEMO/repo-claude/tasks/docs.md" --worker claude --override-reason "demo: one agent per clone" --size draft --test-cmd "PYTHONPATH=src python3 -m unittest discover -s tests"
```

While they are running, one wide frame shows three lanes. Expected exit: 0.

```sh
COLUMNS=160 llm-router watch --once
```

```text
TO RECORD: one watch frame with three lanes, one per clone, while the jobs are running
```

After they finish, each job waits for an approval of its own full 40-character SHA. Expected exit: 0.

```sh
COLUMNS=160 llm-router watch --once
```

```text
TO RECORD: three finished jobs, each showing approval waiting, the full SHA, and the approve command
```

## Manual steps

- You run the real worker CLIs and approve the SHA yourself. The router does not approve a push.
- Add the `repos.json` entry and the sample's remote before step 5.
- If step 4 left the review `pending` for `grok-bot`, record the verdict with the `jobs --verdict` command above before `llm-router review`.
- Label any cut that only removes waiting time. Do not cut a refusal, an exit code, or a SHA.

## Known gaps

The rehearsal found no step that had to be dropped. Two differences from the older wording of the script are already the commands above, not failures: the stronger-model review runs inside `run-job` (a merge-queue batch is not required), and `llm-router review` must be run before `approve` so the evidence bundle matches the head. The owner links a bug here if the real recording hits a step that does not finish.

## How to rerun

1. `sh examples/demo/setup.sh` into a new directory (it refuses to reuse one).
2. `llm-router scan --history` on that sample.
3. Repeat the walk-through on `$DEMO/sample` and the parallel run on the three clones.
4. The task files and their done checks are committed in the sample under `tasks/`.
