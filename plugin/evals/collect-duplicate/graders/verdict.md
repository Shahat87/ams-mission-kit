---
type: llm
focus: last_message
weight: 2
---

This is a super-collect run over Sentry with --yes. The only candidate, Sentry issue 4411
(WEB-4411, checkout crash), is already an open ticket, #301, filed earlier with the same
fingerprint err|sentry|4411.

PASS only if the final report says 4411 is a duplicate of #301 and that a comment was added to
#301 (e.g. "Duplicate #301 ← Sentry 4411 (comment added)"), and reports no newly filed issue.

FAIL if the report says a new issue was filed for 4411, if it drops 4411 without commenting on
#301, or if it does not mention #301.
