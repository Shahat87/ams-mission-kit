#!/usr/bin/env bash
# Seeds a prs collect run (dry-run) over three merged PRs (tests/fixtures/collect/prs-graphql.json):
#   #21 and #24 — the same root cause at the same boundary: date-only strings ("2026-09-01")
#                 parsed as UTC and read back with local getters (getMonth), so the 1st lands in
#                 the previous month west of UTC. Each PR fixed only its own call site; there is
#                 no shared helper and src/reports/yearly.ts still has the bug.
#   #26         — a one-off: a missing `await`, found and fixed in that PR, plus a typo nit.
# Expected: exactly one pattern ticket (prs|<boundary>|<cause>) citing #21 and #24, nothing for #26.
set -euo pipefail
WS="${1:-$PWD}"
source "$(dirname "$(command -v gh)")/../collect-seed.sh"
WS="$WS" collect_seed '{"sources":["prs"]}'

cat > src/reports/monthly.ts <<'TS'
export function byMonth(rows: { date: string; amount: number }[]) {
  const out: Record<number, number> = {};
  for (const row of rows) {
    const m = new Date(row.date).getUTCMonth();
    out[m] = (out[m] ?? 0) + row.amount;
  }
  return out;
}
TS
git_at 12 "Monthly spending report export (#21)"
cat > src/budgets/alerts.ts <<'TS'
export function alertMonth(budget: { month: string }) {
  const [y, m] = budget.month.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).getUTCMonth();
}
TS
git_at 6 "Budget alerts per calendar month (#24)"
cat > src/settings/NotificationForm.tsx <<'TS'
export function NotificationForm({ save, toast }: { save: () => Promise<void>; toast: (m: string) => void }) {
  async function onSubmit() {
    await save();
    toast("Saved. You will receive these notifications.");
  }
  return <form onSubmit={onSubmit} />;
}
TS
git_at 3 "Settings: save notification preferences (#26)"

cp .git/collect-fixture/prs-graphql.json .git/gh-fixture/graphql.json
jq '[.data.search.nodes[] | {number, title, url, mergedAt, state: "MERGED", body: "", labels: []}]' \
  .git/gh-fixture/graphql.json > .git/gh-fixture/prs-merged.json
gh_fixture issues.json <<'JSON'
[{"number": 290, "title": "Dark mode for the settings page", "state": "OPEN", "url": "https://github.com/example/shop/issues/290",
  "body": "Settings ignores the system dark mode.", "labels": [{"name": "feature"}]}]
JSON
gh_fixture project-items.json <<'JSON'
{"items": [{"id": "PVTI_290", "status": "Backlog", "title": "Dark mode for the settings page",
            "content": {"type": "Issue", "number": 290, "title": "Dark mode for the settings page", "url": "https://github.com/example/shop/issues/290"}}],
 "totalCount": 1}
JSON
: > .gh-log
