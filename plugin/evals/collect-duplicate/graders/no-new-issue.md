---
type: regex
pattern: 'issue create'
match: not_contains
target:
  source: file
  path: .git/gh-calls.log
weight: 1
---

No second card for the same fingerprint.
