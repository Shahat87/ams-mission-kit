---
type: regex
pattern: 'gap\|[a-z0-9_-]*receipt'
match: contains
target:
  source: file
  path: .git/gh-calls.log
weight: 1
---

A gap|<receipt-upload workflow> ticket went through super-collect-file.sh (its dedupe lookup
carries the fingerprint).
