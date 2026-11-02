# Standing rules

Always loaded for every DLR worker and routing decision. Keep this list between 10 and 15 rules.

1. Never push, merge, or open a PR without explicit human approval.
2. Tests must pass with 0 failures and 0 skipped before a unit is reported done.
3. Stay within the declared file scope of the unit; out-of-scope edits are refused by the merge queue.
4. Cite a source (eval run id or URL) for every capability claim; unsourced claims are ignored.
5. Redact secrets, tokens, API keys, and personal data from logs, notes, and handoffs.
6. Report what was observed (command and actual output), never what was intended.
7. Never overwrite or delete user edits in the vault or the main checkout.
8. Work only inside the assigned git worktree; never touch the main checkout's tree, index, or branch.
9. Write a handoff note at every checkpoint and when stopping, so another worker can continue.
10. Prefer the cheapest worker that meets the task class's measured pass rate; escalate only on evidence.
11. Route on measured scorecards and eval results, never on hype or vendor claims alone.
12. Stop and escalate after the same verification fails twice; do not loop blind.
13. Do not bump dependencies, edit CI, or change auth/permissions without human sign-off.
