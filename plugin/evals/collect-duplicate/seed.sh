#!/usr/bin/env bash
# Seeds a Sentry collect run where the only candidate, WEB-4411 (checkout crash, still
# happening), is already an open ticket: #301 in Backlog, filed by an earlier collect run with
# fingerprint err|sentry|4411. The right outcome is a "Seen again" comment on #301 and no new
# issue.
set -euo pipefail
WS="${1:-$PWD}"
source "$(dirname "$(command -v gh)")/../collect-seed.sh"
WS="$WS" collect_seed '{"sources":["sentry"],"sentry":{"org":"acme","project":"web","host":"https://us.sentry.io","min_events":2}}'
jq '[.[] | select(.id == "4411")]' .git/collect-fixture/sentry-issues.json > .git/collect-fixture/x && mv .git/collect-fixture/x .git/collect-fixture/sentry-issues.json

gh_fixture issues.json <<'JSON'
[{"number": 301, "title": "🐛 [bug] checkout: Checkout crashes when the last item is removed", "state": "OPEN",
  "url": "https://github.com/example/shop/issues/301",
  "labels": [{"name": "bug"}, {"name": "source:collect"}, {"name": "collect:sentry"}],
  "body": "## Board summary\n**medium** bug · owner `unassigned`\n\n## Problem\nCheckout crashes with `TypeError: Cannot read properties of undefined (reading 'items')` when the last item is removed.\n\n## Context\n- **Where:** Checkout, `/checkout`\n  - a. Add one item\n  - b. Open `/checkout`\n  - c. Remove it\n\n## Evidence\nSentry WEB-4411 · 22 events, 9 users (first run)\n\n## Fix\nGuard the empty cart in buildSummary.\n\n## Acceptance Criteria\n- [ ] Removing the last item shows the empty-cart message.\n\n## Risk\n🟢 Low\n\n<!-- super-collect-fingerprint: err|sentry|4411 -->\n<!-- super-collect-source: sentry -->"},
 {"number": 290, "title": "Dark mode for the settings page", "state": "OPEN", "url": "https://github.com/example/shop/issues/290",
  "body": "Settings ignores the system dark mode.", "labels": [{"name": "feature"}]}]
JSON
gh_fixture project-items.json <<'JSON'
{"items": [
  {"id": "PVTI_301", "status": "Backlog", "title": "🐛 [bug] checkout: Checkout crashes when the last item is removed",
   "content": {"type": "Issue", "number": 301, "title": "🐛 [bug] checkout: Checkout crashes when the last item is removed", "url": "https://github.com/example/shop/issues/301"}},
  {"id": "PVTI_290", "status": "Backlog", "title": "Dark mode for the settings page",
   "content": {"type": "Issue", "number": 290, "title": "Dark mode for the settings page", "url": "https://github.com/example/shop/issues/290"}}],
 "totalCount": 2}
JSON
gh_fixture prs-merged.json <<'JSON'
[]
JSON
: > .gh-log
