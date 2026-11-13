# Demo sample

Public sample repository for the recording described in [docs/demo.md](../../docs/demo.md). That write-up is a draft until the recording exists.

Create the sample and three sibling clones (no network, no installs):

```sh
DEMO=~/dlr-demo
sh examples/demo/setup.sh "$DEMO/sample"
```

`setup.sh` writes a tiny Python package, a passing unittest suite, `DONE_CHECK.md`, and the task files, commits them, and clones the commit to `repo-codex`, `repo-cursor`, and `repo-claude` beside the sample. A second run refuses if that directory or a clone already exists.

The task files in `tasks/` are copied into the sample and committed there. Each one has its own done check and an allowed-paths section.

`run-parallel.sh` starts three `run-job`s, one per clone, and saves `llm-router watch --once` frames under the demo directory. It uses the `llm-router` command on `PATH`.
