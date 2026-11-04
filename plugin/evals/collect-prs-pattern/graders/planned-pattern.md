---
type: regex
pattern: 'prs\|[^|" ]+\|'
match: contains
target:
  source: file
  path: .git/gh-calls.log
weight: 1
---

A prs|<boundary>|<cause> ticket went through super-collect-file.sh.
