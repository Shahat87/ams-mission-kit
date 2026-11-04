#!/usr/bin/env bash
# Seeds a PostHog collect run (dry-run). No signal clears its threshold (vitals are under the
# limits, exceptions and rage clicks are a handful of people), so the only ticket is a tracking
# gap: of the app's user workflows, signup and checkout capture started / completed / failed
# events that arrive in PostHog, and receipt upload (/receipts/upload, src/receipts/upload.ts:
# no capture calls, no events). Expected: exactly one `feature` ticket, label analytics,
# fingerprint gap|<receipt-upload workflow>, and no app code edited.
set -euo pipefail
WS="${1:-$PWD}"
source "$(dirname "$(command -v gh)")/../collect-seed.sh"
WS="$WS" collect_seed '{"sources":["posthog"],"posthog":{"host":"https://us.posthog.com","project_id":4242}}'

gh_fixture issues.json <<'JSON'
[{"number": 290, "title": "Dark mode for the settings page", "state": "OPEN", "url": "https://github.com/example/shop/issues/290",
  "body": "Settings ignores the system dark mode.", "labels": [{"name": "feature"}]}]
JSON
gh_fixture project-items.json <<'JSON'
{"items": [{"id": "PVTI_290", "status": "Backlog", "title": "Dark mode for the settings page",
            "content": {"type": "Issue", "number": 290, "title": "Dark mode for the settings page", "url": "https://github.com/example/shop/issues/290"}}],
 "totalCount": 1}
JSON
gh_fixture prs-merged.json <<'JSON'
[]
JSON
: > .gh-log
