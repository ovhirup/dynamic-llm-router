#!/bin/sh
# Start three run-jobs, one clone each, and save llm-router watch --once frames.
# Usage: run-parallel.sh DEMO_DIR
# DEMO_DIR is the parent created around the sample (it contains repo-codex, repo-cursor, repo-claude).
# Uses the llm-router command on PATH. No absolute paths.
set -eu
for v in $(env | grep -o '^GIT_CONFIG_[A-Z_0-9]*' || true); do
  unset "$v"
done
if [ "${1-}" = "" ]; then
  echo "usage: run-parallel.sh DEMO_DIR" >&2
  exit 2
fi
DEMO=$1
FRAMES=$DEMO/frames
RESULTS=$DEMO/results
mkdir -p "$FRAMES" "$RESULTS"
TEST_CMD='PYTHONPATH=src python3 -m unittest discover -s tests'
REASON='demo: one agent per clone'

run_one() {
  worker=$1
  task=$2
  (
    set +e
    llm-router run-job \
      --repo "$DEMO/repo-$worker" \
      --task-file "$DEMO/repo-$worker/tasks/$task.md" \
      --worker "$worker" \
      --override-reason "$REASON" \
      --size draft \
      --test-cmd "$TEST_CMD" \
      > "$RESULTS/$worker.json" 2> "$RESULTS/$worker.err"
    echo "exit=$?" >> "$RESULTS/$worker.err"
  ) &
}

run_one codex subtract
run_one cursor multiply
run_one claude docs

n=0
while [ "$n" -lt 180 ]; do
  n=$((n + 1))
  frame=$(printf '%s/frames/%03d.txt' "$DEMO" "$n")
  COLUMNS=160 llm-router watch --once > "$frame" || true
  ready=0
  for err in "$RESULTS"/*.err; do
    if [ -f "$err" ] && grep -q '^exit=' "$err"; then
      ready=$((ready + 1))
    fi
  done
  if [ "$ready" -ge 3 ]; then
    break
  fi
  sleep 5
done
wait || true
COLUMNS=160 llm-router watch --once > "$DEMO/frames/final.txt"
echo "frames: $n"
