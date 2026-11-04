## Problem
Checkout crashes with `TypeError: Cannot read properties of undefined (reading 'items')` when the last item is removed on the checkout page.

## Context
- **Where:** Checkout, `/checkout`
  - a. Add one item to the cart
  - b. Open `/checkout`
  - c. Remove the item from the order summary
- **Who:** any shopper who empties the cart on the checkout page

## Evidence
<details><summary>12 rows · Sentry WEB-4411 · 37 events, 14 users in 14 days</summary>

| Row | Value |
|---|---|
| Error + stack | `TypeError` · `src/checkout/summary.ts:12` buildSummary ← `src/checkout/CheckoutPage.tsx:31` |
| Request / trace ID | event `9f2c41aa0b7e4c1f8d3e2a1b0c9d8e7f` |
| Sentry | https://acme.sentry.io/issues/4411/ |
| PostHog replay | n/a — no replay linked to this event |
| Logs | n/a — client-side error, no server log |
| Screenshots | n/a — crash replaces the page |
| HAR / API sample | `GET /checkout` |
| Env + release | production · `web@1.4.2` |
| First / last seen | 12 days ago · 2 hours ago |
| Users affected | 14 users, 37 events |
| Steps | 1. add one item 2. open checkout 3. remove it |
| Expected / actual | empty-cart message / white screen |

</details>

## Fix
`buildSummary` reads `cart.items` before the cart reloads; guard the empty cart and render the empty state.

## Acceptance Criteria
- [ ] Removing the last item on `/checkout` shows the empty-cart message, no error.
- [ ] A unit test for `buildSummary` with an empty cart passes.

## Risk
🟢 **Low** · one guard clause in the summary builder.
