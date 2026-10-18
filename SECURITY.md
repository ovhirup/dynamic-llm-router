# Security policy

## Reporting

Please report vulnerabilities privately with GitHub's "Report a vulnerability" (Security advisories) on this
repository rather than in a public issue. Include the version (`llm-router --version`), OS and a minimal repro.

## Threat model (summary)

`llm-router` launches AI coding agents with **write access to a git worktree** and can push branches and
merge PRs **after human approval**. It aims to:

- never put prompts, secrets or private names in process arguments, logs, commits, branch names or PRs;
- never run a CLI you put in `never`, even when a worker tries to spawn it;
- never read credential stores (auth files, keychains, browser cookies) or call undocumented billing APIs;
- never push or merge without an approval bound to the exact reviewed commit, and never force-push;
- fail closed: unknown meters, missing private-terms files on public repos and leak hits all block.

Out of scope: what a worker CLI does with the access you grant it inside the worktree, and vendor-side data
handling. Review the worker command templates and run on a machine and account you are comfortable with.

The leak check is a safety net, not a guarantee. Keep secrets out of repositories in the first place.
