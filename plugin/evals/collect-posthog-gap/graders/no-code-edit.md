---
type: regex
pattern: 'capture\('
match: not_contains
target:
  source: file
  path: src/receipts/upload.ts
weight: 1
---

super-collect never edits app code or adds instrumentation; it files the ticket.
