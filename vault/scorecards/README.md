# Scorecards

One note per worker: `scorecards/<worker>.md`, with one row per task class.

```markdown
---
worker: <worker name>
updated: <ISO-8601>
---

| class | jobs | pass rate | review fixes / job | tokens / unit | source |
|-------|------|-----------|--------------------|---------------|--------|
| docs  | 12   | 0.92      | 0.3                | 18k           | eval:<run-id> |
```

Every row cites its source (eval run id or job ids). Rows without a source are not used for routing.
