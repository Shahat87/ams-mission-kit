#!/usr/bin/env bash
# Seeds a Sentry collect run. Two issues clear the thresholds:
#   4411 — checkout crash, 37 events / 14 users, last seen 2 hours ago, still in the code → file.
#   4490 — avatar null crash, last seen 11 days ago; PR #377 fixed it 10 days ago (web@1.4.0)
#          and the code has the guard → drop-fixed.
# 4502 (1 event) is under min_events and never becomes a candidate. Nothing on the board matches.
set -euo pipefail
WS="${1:-$PWD}"
source "$(dirname "$(command -v gh)")/../collect-seed.sh"
WS="$WS" collect_seed '{"sources":["sentry"],"sentry":{"org":"acme","project":"web","host":"https://us.sentry.io","min_events":2}}'

cat > src/profile/Avatar.tsx <<'TS'
export function Avatar({ user }: { user: { name: string; avatar: { url: string } | null } }) {
  if (!user.avatar) return <span className="avatar-initials">{user.name.slice(0, 2)}</span>;
  return <img src={user.avatar.url} alt={user.name} />;
}
TS
git_at 10 "Fix null avatar crash on profile page (#377)"

gh_fixture prs-merged.json <<'JSON'
[{"number": 377, "title": "Fix null avatar crash on profile page", "state": "MERGED",
  "url": "https://github.com/example/shop/pull/377", "mergedAt": "{{ago:10d}}",
  "body": "Avatar.tsx renders initials when `user.avatar` is null instead of reading `.url` off null.\n\nFixes Sentry WEB-4490 (https://acme.sentry.io/issues/4490/). Ships in web@1.4.0.",
  "labels": [{"name": "bug"}]},
 {"number": 380, "title": "Checkout: loading skeleton for the order summary", "state": "MERGED",
  "url": "https://github.com/example/shop/pull/380", "mergedAt": "{{ago:5d}}",
  "body": "Shows a skeleton while the order summary loads on /checkout. No logic change.",
  "labels": [{"name": "ui"}]}]
JSON
gh_fixture issues.json <<'JSON'
[{"number": 290, "title": "Dark mode for the settings page", "state": "OPEN", "url": "https://github.com/example/shop/issues/290",
  "body": "Settings ignores the system dark mode.", "labels": [{"name": "feature"}]},
 {"number": 270, "title": "Profile page crashes for users without a photo", "state": "CLOSED", "url": "https://github.com/example/shop/issues/270",
  "body": "Reported by support. Closed by #377.", "labels": [{"name": "bug"}]}]
JSON
gh_fixture project-items.json <<'JSON'
{"items": [{"id": "PVTI_290", "status": "Backlog", "title": "Dark mode for the settings page",
            "content": {"type": "Issue", "number": 290, "title": "Dark mode for the settings page", "url": "https://github.com/example/shop/issues/290", "body": "Settings ignores the system dark mode."}}],
 "totalCount": 1}
JSON
: > .gh-log
