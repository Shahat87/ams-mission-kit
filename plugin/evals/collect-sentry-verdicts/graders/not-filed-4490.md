---
type: regex
pattern: 'err\|sentry\|4490'
match: not_contains
target:
  source: file
  path: .git/gh-calls.log
weight: 1
---

WEB-4490 is already fixed, so it never reaches the filer (no dedupe lookup, no issue).
