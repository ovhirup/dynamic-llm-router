# Check an existing Git checkout

```sh
llm-router check [--repo PATH] [--base REF] [--review] [--json]
```

`--repo` defaults to the current directory. An explicit `--base` names the
comparison commit. Otherwise the command uses the merge base of HEAD and the
locally known default branch. It looks for `origin/HEAD`, then `main` or
`master` in the local and origin refs. Pass `--base` for other layouts or when
the checkout lacks the history needed to find a merge base.

The command runs the configured repository tests. Like `run-job`, it detects
the test command from the comparison base when none is configured and respects
`allow_no_tests`. Tests run in a disposable copy of the tracked and non-ignored
working files. Install the dependencies the test command needs in the calling
environment and use a test command that works from the copy.
The leak gate scans added lines in the committed, staged, and current working diffs
using the configured secret and private-term rules. Deleting a file whose name
contains a private term still blocks the gate: the name remains in the diff and
in published history.

Tests and leak scanning are the default gates. DLR makes no model or network
calls in this mode. Configured test commands run with their own normal
permissions. For an offline check, use tests that run offline.

For `--review`, put the acceptance criteria in `DONE_CHECK.md` at the repository
root. The existing live Claude reviewer evaluates the diff against that file
after the local gates pass. It requires a cached Claude meter and returns the
existing approve or reject verdict.

| Exit | Meaning |
| --- | --- |
| `0` | All requested gates passed. |
| `1` | Tests, the leak gate, or review failed. |
| `2` | The check was refused or could not run. |

Use `--json` to get a single result object on stdout. Leak findings identify
their file and rule without including the matching value.

## GitHub Actions

Install DLR and the repository's test dependencies before the gate step.
For a pull request, check out the history needed to compare with its base.

```yaml
- uses: actions/checkout@v7
  with:
    fetch-depth: 0
# Install DLR and the project's test dependencies here.
- name: Run DLR gates
  env:
    BASE_BRANCH: ${{ github.base_ref }}
  run: |
    base=$(git merge-base HEAD "origin/$BASE_BRANCH")
    llm-router check --repo "$GITHUB_WORKSPACE" --base "$base" --json
```

## Git pre-push hook

Install this script as the executable `pre-push` in your Git hooks directory.
It checks pushes of the current checkout and requires a clean tree so the tests
run on the same content being pushed.

```sh
#!/bin/sh
set -eu
head=$(git rev-parse HEAD)
while read -r local_ref local_oid remote_ref remote_oid; do
    [ "$local_ref" = '(delete)' ] && continue
    if [ "$local_oid" != "$head" ] || [ -n "$(git --no-optional-locks status --porcelain)" ]; then
        echo 'DLR check requires the pushed commit to be checked out with a clean tree.' >&2
        exit 2
    fi
    if git cat-file -e "$remote_oid^{commit}" 2>/dev/null; then
        llm-router check --base "$remote_oid" </dev/null || exit $?
    else
        llm-router check </dev/null || exit $?
    fi
done
```

## Use as an app's check command

Set the app's check command to `llm-router check --json` and its working
directory to the branch or worktree the app wants checked. If the app runs the
command from elsewhere, pass that worktree with `--repo PATH`. Let the app use
the exit status to decide whether the check passed.
