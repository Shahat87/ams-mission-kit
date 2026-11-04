---
type: regex
pattern: 'super-collect-fingerprint: err\|sentry\|4411'
match: contains
target:
  source: file
  path: .git/gh-created.md
weight: 1
---

WEB-4411 was filed through super-collect-file.sh (the router stamps its fingerprint).
