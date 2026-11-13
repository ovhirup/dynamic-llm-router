#!/bin/sh
# Create the public sample repository used by the demo, then clone it three times.
# Usage: setup.sh DIRECTORY
# Refuses if DIRECTORY or a sibling clone already exists. No network and no installs.
set -eu
for v in $(env | grep -o '^GIT_CONFIG_[A-Z_0-9]*' || true); do
  unset "$v"
done
if [ "${1-}" = "" ]; then
  echo "usage: setup.sh DIRECTORY" >&2
  exit 2
fi
TARGET=$1
PARENT=$(dirname "$TARGET")
for path in "$TARGET" "$PARENT/repo-codex" "$PARENT/repo-cursor" "$PARENT/repo-claude"; do
  if [ -e "$path" ]; then
    echo "refusing: $path already exists" >&2
    exit 1
  fi
done
HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
mkdir -p "$PARENT"
mkdir "$TARGET" "$TARGET/src" "$TARGET/tests" "$TARGET/tasks"
cp "$HERE/tasks/"*.md "$TARGET/tasks/"
cat > "$TARGET/README.md" <<'EOF'
# Sample

A tiny calculator for the Dynamic LLM Router demo. Nothing here is private.

## Functions

- `calc.add(a, b)` returns the sum.
EOF
cat > "$TARGET/DONE_CHECK.md" <<'EOF'
# Done check

- `add(2, 3)` is `5`
- `PYTHONPATH=src python3 -m unittest discover -s tests` exits 0
- Task files for a rerun are in `tasks/`
EOF
cat > "$TARGET/src/calc.py" <<'EOF'
def add(a, b):
    return a + b
EOF
cat > "$TARGET/tests/test_calc.py" <<'EOF'
import unittest

import calc


class CalcTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(calc.add(2, 3), 5)


if __name__ == '__main__':
    unittest.main()
EOF
( cd "$TARGET" && PYTHONPATH=src python3 -m unittest discover -s tests -q )
git -C "$TARGET" init -q --initial-branch=main
git -C "$TARGET" add -- README.md DONE_CHECK.md src tests tasks
git -C "$TARGET" -c user.name='Dynamic LLM Router' -c user.email='router@localhost' -c commit.gpgsign=false \
  commit -q -m 'Add the sample calculator'
for worker in codex cursor claude; do
  git clone -q "$TARGET" "$PARENT/repo-$worker"
done
echo "sample ready: $TARGET"
