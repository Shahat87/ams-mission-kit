## Problem
Receipt upload sends no PostHog events, so failed uploads are invisible and nobody can tell how many people finish an upload.

## Context
- **Where:** Receipts, `/receipts/upload` (`src/receipts/upload.ts`)
- Capture calls found: `signup_started`, `signup_completed`, `signup_failed` in `src/signup.ts`; none under `src/receipts/`.
- Missing: a success event, a failure event. Silent signals: `dead_click`, `survey`.

## Fix
Add `receipt_upload_started`, `receipt_upload_completed` and `receipt_upload_failed` captures in `src/receipts/upload.ts`. No file names, amounts or merchant text in properties.

## Acceptance Criteria
- [ ] `receipt_upload_completed` fires once per successful upload with `{file_type, size_kb, duration_ms}`.
- [ ] `receipt_upload_failed` fires on every failed upload with `{error_code, file_type}`.
- [ ] No property carries the file name, receipt text or any user PII.

## Risk
🟢 **Low** · three capture calls; no change to the upload itself.
