---
type: regex
pattern: 'opt_ready'
match: not_contains
target:
  source: file
  path: .git/gh-calls.log
weight: 1
---

super-collect files into the holding column only; nothing is moved to Ready.
