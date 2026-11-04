---
type: llm
focus: last_message
weight: 2
---

This is a super-collect dry-run over PostHog. No product signal (exceptions, rage clicks, dead
clicks, web vitals, failure events) clears its threshold. The app has three user workflows:
signup and checkout (both capture started / completed / failed events that arrive in PostHog) and
receipt upload at /receipts/upload (src/receipts/upload.ts), which captures nothing.

PASS only if the final report does all of the following:
1. Plans exactly ONE tracking-gap ticket, for the receipt-upload workflow, as a `feature`
   labelled `analytics` with a `gap|…` fingerprint.
2. Names the missing events for it (at least a success and a failure event for receipt upload).
3. Plans no gap ticket for signup or checkout and no bug tickets from the PostHog signals.
4. Is a dry-run plan (would-file), not a claim that issues were created.

FAIL if there is no receipt-upload gap ticket, if there are two or more gap tickets, if signup or checkout
gets a ticket, or if the report says it edited app code or added capture calls.
