---
type: regex
pattern: 'Seen again'
match: contains
target:
  source: file
  path: .git/gh-comments.md
weight: 1
---

The comment is the filer's "Seen again" note carrying the new evidence.
