---
name: code-review
description: Evidence-only diff review: check scope, regressions, and test coverage.
---
- Judge only the produced diff and test output, never commit-message claims.
- Name at most the 3 highest-impact defects with `file:line` evidence and a concrete local fix.
- Verify that every changed path stays within the declared task scope.
