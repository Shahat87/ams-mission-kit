---
type: regex
pattern: '## Problem[\s\S]*## Context[\s\S]*## Evidence[\s\S]*## Fix[\s\S]*## Acceptance Criteria[\s\S]*- \[ \][\s\S]*## Risk'
match: contains
target:
  source: file
  path: .git/gh-created.md
weight: 1
---

The filed bug uses the ticket format: Problem, Context, Evidence, Fix, Acceptance Criteria
(a checklist) and Risk, in that order.
