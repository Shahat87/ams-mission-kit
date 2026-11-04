---
type: regex
pattern: 'gap\|[a-z0-9_-]*signup'
match: not_contains
target:
  source: file
  path: .git/gh-calls.log
weight: 1
---

Signup already sends started / completed / failed events, so it gets no gap ticket.
