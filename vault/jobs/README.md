# Job outcome notes

One note per job: `jobs/<job-id>.md`.

```markdown
---
job: <job-id>
issue: <issue number or none>
class: <task class>
worker: <worker name>
status: passed | failed | refused | escalated
started: <ISO-8601>
finished: <ISO-8601>
tokens: <total tokens or unknown>
---

## Outcome
One paragraph: what changed, and the verification command with its observed result.

## Links
- Handoff: [[handoffs/<job-id>]]
- Scorecard: [[scorecards/<worker>]]
```
