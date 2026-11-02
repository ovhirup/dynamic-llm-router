# Research digests

Weekly model-news digests: `research/<YYYY>-W<ww>.md`.

```markdown
---
week: <YYYY>-W<ww>
---

## Claims
- <model> improves on <task class>. Source: <URL>. Status: unverified | eval:<run-id>

## Candidate capability updates
- <change to capabilities.json>. Requires: eval:<run-id> showing a measured win.
```

Every claim carries a source URL. A claim only changes routing after a DLR eval run confirms it.
