#!/usr/bin/env bash
# Tests super-board-preflight.sh — the check that runs before any card goes
# Ready → Building. `gh` is a stub on PATH that serves fixtures and logs calls.
#
# The rule it pins: two agents must never build the same thing, and nothing that
# is already on the base branch gets built twice. Same files alone is not a
# duplicate — it is a sequencing problem, and must not hold the card.
set -euo pipefail
cd "$(dirname "$0")"
PRE="$(pwd)/../scripts/super-board-preflight.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }

TMP=$(mktemp -d); export SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$TMP/halt.json"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/fx"; export GH_LOG="$TMP/gh.log" FX="$TMP/fx"
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  "api rate_limit"*)          echo '{"resources":{"graphql":{"remaining":5000,"reset":0},"core":{"remaining":5000,"reset":0}}}' ;;
  "issue view "*)             cat "$FX/issue-$3.json" ;;
  "pr list"*"--state merged"*) cat "$FX/merged.json" ;;
  "pr list"*"--state open"*)   cat "$FX/open.json" ;;
  "issue list"*"--state closed"*) cat "$FX/closed.json" ;;
  "issue list"*"--state open"*)   cat "$FX/open-issues.json" ;;
  "api graphql"*)             [ -f "$FX/gql.json" ] && cat "$FX/gql.json" || exit 1 ;;
  *) exit 1 ;;
esac
STUB
chmod +x "$TMP/bin/gh"

AC=$'## Acceptance Criteria\n- [ ] it works'
issue() { jq -n --argjson n "$1" --arg t "$2" --arg b "$3" '{number:$n,title:$t,body:$b}' > "$FX/issue-$1.json"; }
issue 20 "Export invoices as CSV"            "$AC"
issue 21 "Dark mode toggle in settings"      "$AC"
issue 22 "Rate limit the login endpoint"     $'Touches `src/auth/login.ts`.\n'"$AC"
issue 23 "Weekly digest email"               "$AC"
issue 24 "Something about the dashboard"     $'No criteria here.'
issue 25 "Retry failed webhooks"             "$AC"$'\nfingerprint: err|sentry|991'

cat > "$FX/merged.json" <<'J'
[ {"number":31,"title":"CSV export for invoices","body":"## Issue\nResolves #20 — Export invoices as CSV","url":"u31"},
  {"number":32,"title":"Webhook backoff","body":"fingerprint: err|sentry|991","url":"u32"} ]
J
cat > "$FX/open.json" <<'J'
[ {"number":40,"title":"Dark mode toggle in settings page","body":"wip","url":"u40","headRefName":"example-org/dark-mode",
   "files":[{"path":"src/settings/theme.ts"}]},
  {"number":41,"title":"Session timeout banner","body":"Resolves #50","url":"u41","headRefName":"issue-50-session-timeout",
   "files":[{"path":"src/auth/login.ts"},{"path":"src/ui/banner.tsx"}]},
  {"number":42,"title":"Weekly digest email","body":"Resolves #23","url":"u42","headRefName":"issue-23-weekly-digest",
   "files":[{"path":"src/mail/digest.ts"}]} ]
J
echo '[]' > "$FX/closed.json"
# Issues still open on the board's repo (the ones the open PRs close are the only ones read from it).
echo '[{"number":50},{"number":23}]' > "$FX/open-issues.json"

run() { PATH="$TMP/bin:$PATH" "$PRE" --repo o/r "$@"; }
OUT=$(run --issues 20,21,22,23,24,25)
get() { echo "$OUT" | jq -e ".[\"$1\"] | $2" >/dev/null; }

# 1 — duplicate merged PR → hold. #31 merged with "Resolves #20": building #20
#     again would ship the same feature twice.
get 20 '.verdict == "hold" and .tag == "done"' || fail "#20 is already merged in #31: $(echo "$OUT" | jq -c '.["20"]')"
get 20 '.evidence | any(test("#31"))'          || fail "#20's hold must name PR #31"

# 2 — open PR, same feature, different branch → hold. Another agent (or a human)
#     is already building dark mode on example-org/dark-mode.
get 21 '.verdict == "hold" and .tag == "in-progress"' || fail "#21 is in flight in #40: $(echo "$OUT" | jq -c '.["21"]')"
get 21 '.evidence | any(test("#40"))'                 || fail "#21's hold must name PR #40"

# 3 — same files only → sequence, never hold. #41 is a different feature that
#     touches login.ts; #22 goes behind the issue #41 closes (#50), so the
#     wave-start sweep frees it the moment #50 closes.
get 22 '.verdict == "sequence" and .tag == "overlap"' || fail "#22 overlaps #41 only: $(echo "$OUT" | jq -c '.["22"]')"
get 22 '.blockedBy == [50] and .conflictWith == [41]' || fail "#22 must sit behind #50 / PR #41: $(echo "$OUT" | jq -c '.["22"]')"

# 4 — clean → proceed. #23's own branch PR (#42, issue-23-*) is the card coming
#     back for a rebuild, not a duplicate of itself.
get 23 '.verdict == "proceed" and .tag == "clean"' || fail "#23 is clean (own PR ignored): $(echo "$OUT" | jq -c '.["23"]')"

# 5 — no acceptance criteria → hold as unclear (❓), never built on a guess.
get 24 '.verdict == "hold" and .tag == "unclear"' || fail "#24 has no ACs: $(echo "$OUT" | jq -c '.["24"]')"

# 6 — a merged PR with the same fingerprint is "already done" even with a
#     different title. That is what fingerprints are for.
get 25 '.verdict == "hold" and .tag == "done"' || fail "#25 shares #32's fingerprint: $(echo "$OUT" | jq -c '.["25"]')"

# 7 — batching is cheap: three list calls total for six issues, plus one view each.
[ "$(grep -c '^pr list\|^issue list' "$GH_LOG")" -eq 4 ] || fail "lists must be fetched once per batch (merged, open, closed, and open issues): $(cat "$GH_LOG")"
[ "$(grep -c '^issue view' "$GH_LOG")" -eq 6 ] || fail "one issue view per card"
# W41: blockedBy keeps only open issues, read as ONE open-issue list (a ref that is a PR, deleted or
# transferred is simply absent; a per-issue view on it would halt the run).
[ "$(grep -c -- '^issue list.*--state open' "$GH_LOG")" -eq 1 ] || fail "one open-issue list per batch: $(cat "$GH_LOG")"

# 8 — another card already in Building with the same title → hold; a lower-
#     numbered Ready peer in the same wave wins, a higher-numbered one does not.
issue 26 "Bulk archive old projects" "$AC"
echo '[{"number":19,"title":"Bulk archive old projects","status":"Ready"},
       {"number":27,"title":"Retry failed webhooks","status":"Ready"}]' > "$TMP/cards.json"
OUT8=$(run --issues 26 --inflight "$TMP/cards.json")
echo "$OUT8" | jq -e '.["26"].verdict == "hold" and .["26"].tag == "in-progress"' >/dev/null \
  || fail "#26 duplicates lower Ready peer #19: $OUT8"
echo '[{"number":27,"title":"Bulk archive old projects","status":"Ready"}]' > "$TMP/cards.json"
OUT8b=$(run --issues 26 --inflight "$TMP/cards.json")
echo "$OUT8b" | jq -e '.["26"].verdict == "proceed"' >/dev/null \
  || fail "the lower-numbered peer (#26) must proceed: $OUT8b"

# 9 — a blind pre-flight never says proceed: gh failing exits 69.
RC=0; PATH="$TMP/bin:$PATH" "$PRE" --repo o/r --issues 99 >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 79 ] || fail "an unreadable issue should halt (79), got $RC"
RC=0; "$PRE" --repo o/r >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "missing --issues should exit 64, got $RC"

# 10 — two batch peers name the same file and neither has an open PR: the lower
#      number proceeds, only the higher one is sequenced behind it. Both sequenced
#      against each other parked both forever (#180/#181, 2026-10-03).
rm -f "$SB_GITHUB_HALT_FILE"   # scenario 9 halted reads on purpose
issue 60 "Corpus loader caching"  $'Touches `src/corpus.ts`.\n'"$AC"
issue 61 "Corpus search ranking"  $'Touches `src/corpus.ts`.\n'"$AC"
issue 62 "Unrelated footer copy"  $'Touches `src/footer.tsx`.\n'"$AC"
OUT10=$(run --issues 61,60,62)
echo "$OUT10" | jq -e '.["60"].verdict == "proceed"' >/dev/null || fail "lower peer #60 must proceed: $OUT10"
echo "$OUT10" | jq -e '.["61"].verdict == "sequence" and .["61"].tag == "overlap" and .["61"].blockedBy == [60]' >/dev/null \
  || fail "#61 must sit behind #60: $OUT10"
echo "$OUT10" | jq -e '.["62"].verdict == "proceed" and (.["60"] | has("_files") | not)' >/dev/null \
  || fail "#62 is clean and no internal _files leaks: $OUT10"

# 11 — payloads far beyond the OS argv limit (Windows ~32K per command line, Linux 128K per
#      argument) must still go through: 100 merged + 100 open PRs with ~3K bodies each is
#      ~600K of JSON. They once rode on jq's command line (--argjson) and died with exit 126
#      "Argument list too long" (pilot wave 8).
BIG=$(head -c 3000 /dev/zero | tr '\000' 'x')
jq -n --arg b "$BIG" '[range(100) | {number: (1000 + .), title: "Unrelated big change \(.)", body: $b, url: "u"}]' > "$FX/merged.json"
jq -n --arg b "$BIG" '[range(100) | {number: (2000 + .), title: "Unrelated open change \(.)", body: $b, url: "u",
                                     headRefName: "other-\(.)", files: [{path: "src/other/f\(.).ts"}]}]' > "$FX/open.json"
[ "$(wc -c < "$FX/merged.json")" -gt 200000 ] || fail "fixture must exceed 200 KB"
RC=0; OUT11=$(run --issues 23 2>"$TMP/err11") || RC=$?
[ "$RC" -eq 0 ] || fail "a >200 KB payload must not break the pre-flight (exit $RC): $(head -c 300 "$TMP/err11")"
echo "$OUT11" | jq -e '.["23"].verdict == "proceed"' >/dev/null || fail "#23 must still proceed on a large board: $OUT11"

# 12 — the worker guard's quota merge (sourced by the pre-flight) takes GitHub's GraphQL
#      rateLimit reply by file, not --argjson: a >200 KB reply must still be merged, and an
#      unparseable one must leave the REST payload untouched.
jq -n --arg b "$BIG" '{data: {rateLimit: {limit: 5000, remaining: 77, used: 4923, resetAt: "2026-10-21T10:00:00Z"}}, pad: [range(100) | $b]}' > "$FX/gql.json"
[ "$(wc -c < "$FX/gql.json")" -gt 200000 ] || fail "gql fixture must exceed 200 KB"
REST='{"resources":{"graphql":{"remaining":5000,"reset":0},"core":{"remaining":5000,"reset":0}}}'
merge() { PATH="$TMP/bin:$PATH" bash -c '. "$1"; sb_gh_quota_merge "$2"' _ "$(dirname "$PRE")/super-board-gh-guard.sh" "$REST"; }
MERGED12=$(merge) || fail "quota merge must not fail on a large GraphQL reply"
echo "$MERGED12" | jq -e '.resources.graphql.remaining == 77 and .resources.core.remaining == 5000' >/dev/null   || fail "the GraphQL rateLimit must replace the REST graphql bucket: $MERGED12"
echo 'not json' > "$FX/gql.json"
[ "$(merge)" = "$REST" ] || fail "an unparseable GraphQL reply must leave the REST payload unchanged"
rm -f "$FX/gql.json"

# 13 — an empty in-flight cards file is a broken input, not "no cards": --slurpfile of an empty
#      file binds [] and $cards_f[0] silently became null.
: > "$TMP/empty-inflight.json"
RC=0; run --issues 20 --inflight "$TMP/empty-inflight.json" >/dev/null 2>"$TMP/err13" || RC=$?
[ "$RC" -ne 0 ] || fail "an empty --inflight file must fail, not pass pre-flight"
grep -q "cards: expected exactly one JSON value" "$TMP/err13" || fail "empty inflight must say why: $(head -c 300 "$TMP/err13")"

# 14 — --files names one card's paths, so it applies to a single --issues only. On a batch it
#      once gave every card the same list: false peer overlaps sequenced clean cards (2026-10-30).
#      A batch ignores it with a warning and parses each body; one issue still uses it.
RC=0; OUT14=$(run --issues 60,62 --files src/corpus.ts 2>"$TMP/err14") || RC=$?
[ "$RC" -eq 0 ] || fail "--files on a batch must not fail the batch, got $RC"
grep -q -- "--files ignored" "$TMP/err14" || fail "--files on a batch must warn: $(head -c 300 "$TMP/err14")"
echo "$OUT14" | jq -e '.["60"].verdict == "proceed" and .["62"].verdict == "proceed"' >/dev/null   || fail "one --files list must not overlap batch peers: $OUT14"
OUT14b=$(run --issues 62 --files src/other/f7.ts 2>/dev/null)
echo "$OUT14b" | jq -e '.["62"].verdict == "sequence" and .["62"].tag == "overlap"' >/dev/null   || fail "a single issue still uses --files (open PR #2007 touches src/other/f7.ts): $OUT14b"

# 15 — bare file names count (W23, card g03 `parse_kwd`): a card body naming `kwd.py` with no slash
#      overlaps a lower-numbered batch peer naming it, so the higher one is sequenced behind it.
#      `e.g.`, `i.e.` and `v1.2` are not files. Every card reports its `files`, sorted and unique.
issue 70 "Parse keyword lists"   $'Edit kwd.py and `tests/test_kwd.py`. e.g. tidy, i.e. quickly, bump to v1.2, see example.com.
'"$AC"
issue 71 "Rank search terms"     $'Also edits kwd.py.
'"$AC"
issue 72 "Footer copy tweak"     $'Only footer.html and foo.dev.ts and aa.co.uk.json and foo.co.ts (see e.g. the notes, v1.2, www.x.com, bbc.co.uk, example.com.au, shop.co.il).
'"$AC"
OUT15=$(run --issues 71,70,72)
echo "$OUT15" | jq -e '.["70"].verdict == "proceed"' >/dev/null || fail "lower peer #70 must proceed: $OUT15"
echo "$OUT15" | jq -e '.["71"].verdict == "sequence" and .["71"].tag == "overlap" and .["71"].blockedBy == [70]' >/dev/null   || fail "#71 names bare kwd.py like #70 and must sit behind it: $OUT15"
echo "$OUT15" | jq -e '.["70"].files == ["kwd.py","tests/test_kwd.py"]' >/dev/null || fail "#70 files must be sorted, unique, and exclude e.g./i.e./v1.2: $OUT15"
echo "$OUT15" | jq -e '.["71"].files == ["kwd.py"] and .["72"].files == ["aa.co.uk.json","foo.co.ts","foo.dev.ts","footer.html"]' >/dev/null || fail "#71/#72 files wrong: $OUT15"
echo "$OUT15" | jq -e 'all(.[]; (.files | type) == "array") and all(.[]; has("_files") | not)' >/dev/null || fail "every card needs a files array and no _files: $OUT15"

# 16-20 — W28 (eval run 20261101T141106Z, cards g03/g06): "merged on GitHub" is not "on the base branch".
#      A merged PR delivers a card only if its merge commit is an ancestor of origin/<base_branch>
#      and the PR targeted that base. A real tiny git repo supplies the ancestry.
REPO_DIR="$TMP/repo"; mkdir -p "$REPO_DIR"
g() { git -C "$REPO_DIR" -c user.email=t@t -c user.name=t -c commit.gpgsign=false "$@"; }
g init -q -b main; g commit -q --allow-empty -m A; SHA_A=$(g rev-parse HEAD)
g checkout -q -b side; g commit -q --allow-empty -m B; SHA_B=$(g rev-parse HEAD); g checkout -q main
# A local bare "origin": the pre-flight fetches origin/<base> first, and the tests make no network call.
git init -q --bare -b main "$TMP/origin.git"; g remote add origin "$TMP/origin.git"; g push -q origin main
echo '{"base_branch":"main"}' > "$TMP/cfg-main.json"
issue 80 "Reconcile ledger totals"        "$AC"
issue 81 "Quarterly ledger reconciliation report" "$AC"
issue 82 "Archive stale sessions"         "$AC"
issue 83 "Rotate signing keys"            "$AC"
jq -n --arg a "$SHA_A" --arg b "$SHA_B" '[
  {number:90,title:"Totals fix",body:"Resolves #80",url:"u90",baseRefName:"main",mergeCommit:{oid:$b}},
  {number:91,title:"Key rotation",body:"Resolves #83",url:"u91",baseRefName:"main",mergeCommit:{oid:$a}},
  {number:92,title:"Session archive",body:"Resolves #82",url:"u92",baseRefName:"release",mergeCommit:{oid:$a}},
  {number:93,title:"Quarterly ledger reconciliation report",body:"x",url:"u93",baseRefName:"main",mergeCommit:{oid:$b}} ]' > "$FX/merged.json"
echo '[]' > "$FX/open.json"
inrepo() { ( cd "$REPO_DIR" && run "$@" ); }
OUT16=$(inrepo --issues 80,81,82,83 --config "$TMP/cfg-main.json" 2>"$TMP/err16")
echo "$OUT16" | jq -e '.["80"].tag != "done"' >/dev/null || fail "(a) merge commit not on origin/main must not deliver #80: $OUT16"
echo "$OUT16" | jq -e '.["83"].verdict == "hold" and .["83"].tag == "done"' >/dev/null || fail "(b) merge commit on origin/main delivers #83: $OUT16"
echo "$OUT16" | jq -e '.["82"].tag != "done"' >/dev/null || fail "(c) a PR merged into another base must not deliver #82: $OUT16"
echo "$OUT16" | jq -e '.["81"].tag != "done"' >/dev/null || fail "(d) title similarity from a non-ancestor merged PR must be ignored: $OUT16"
grep -q "ancestry check skipped" "$TMP/err16" && fail "(a-d) the check ran, so no skip warning: $(cat "$TMP/err16")"
# (e) today's behaviour plus one warning: no --config / no git repo / no origin/<base>.
OUT20=$(inrepo --issues 80,81,82,83 2>"$TMP/err20")
echo "$OUT20" | jq -e '.["80"].tag == "done" and .["82"].tag == "done" and .["81"].tag == "done"' >/dev/null || fail "(e) without --config the old behaviour holds: $OUT20"
[ "$(grep -c 'ancestry check skipped' "$TMP/err20")" -eq 1 ] && grep -q 'ancestry check skipped: no --config' "$TMP/err20" || fail "(e) one skip warning naming no --config: $(cat "$TMP/err20")"
mkdir -p "$TMP/nogit"
OUT20b=$( cd "$TMP/nogit" && GIT_CEILING_DIRECTORIES="$TMP" run --issues 80 --config "$TMP/cfg-main.json" 2>"$TMP/err20b" )
echo "$OUT20b" | jq -e '.["80"].tag == "done"' >/dev/null || fail "(e) outside a git repo the old behaviour holds: $OUT20b"
grep -q 'ancestry check skipped: not a git repository' "$TMP/err20b" || fail "(e) not-a-repo warning: $(cat "$TMP/err20b")"
echo '{"base_branch":"nobranch"}' > "$TMP/cfg-nobranch.json"
OUT20c=$(inrepo --issues 80 --config "$TMP/cfg-nobranch.json" 2>"$TMP/err20c")
echo "$OUT20c" | jq -e '.["80"].tag == "done"' >/dev/null || fail "(e) a missing base ref keeps the old behaviour: $OUT20c"
grep -qF 'ancestry check skipped: origin/nobranch not found' "$TMP/err20c" || fail "(e) missing-ref warning: $(cat "$TMP/err20c")"
# base_branch defaults to main when the config omits it (as card.sh does).
echo '{}' > "$TMP/cfg-empty.json"
OUT20d=$(inrepo --issues 80 --config "$TMP/cfg-empty.json" 2>/dev/null)
echo "$OUT20d" | jq -e '.["80"].tag != "done"' >/dev/null || fail "an omitted base_branch defaults to main: $OUT20d"

# (f) a merge commit the clone does not have (git exit 128) is not delivered, and the PR is named on stderr.
issue 84 "Prune orphaned uploads" "$AC"
GHOST=1111111111111111111111111111111111111111
jq -n --arg o "$GHOST" '[{number:94,title:"Upload pruning",body:"Resolves #84",url:"u94",baseRefName:"main",mergeCommit:{oid:$o}}]' > "$FX/merged.json"
OUT21=$(inrepo --issues 84 --config "$TMP/cfg-main.json" 2>"$TMP/err21")
echo "$OUT21" | jq -e '.["84"].tag != "done"' >/dev/null || fail "(f) an unknown merge commit must not deliver #84: $OUT21"
grep -qF "merged PR #94: merge commit $GHOST not in the clone; not counted as delivered" "$TMP/err21" || fail "(f) the unknown merge commit must be named on stderr: $(cat "$TMP/err21")"
# (g) an unreadable --config is a caller error: exit 66, no output.
RC=0; inrepo --issues 84 --config "$TMP/no-such-config.json" >/dev/null 2>"$TMP/err22" || RC=$?
[ "$RC" -eq 66 ] || fail "(g) an unreadable --config must exit 66, got $RC: $(head -c 300 "$TMP/err22")"

# (h) the fetch fails (origin gone) but origin/<base> already exists: warn, and still run the check. A stale ref
#     answers ancestry correctly, so a non-ancestor PR stays excluded and an ancestor PR still delivers.
jq -n --arg a "$SHA_A" --arg b "$SHA_B" '[
  {number:95,title:"Totals fix again",body:"Resolves #80",url:"u95",baseRefName:"main",mergeCommit:{oid:$b}},
  {number:96,title:"Key rotation again",body:"Resolves #83",url:"u96",baseRefName:"main",mergeCommit:{oid:$a}} ]' > "$FX/merged.json"
mv "$TMP/origin.git" "$TMP/origin-gone.git"
OUT23=$(inrepo --issues 80,83 --config "$TMP/cfg-main.json" 2>"$TMP/err23")
mv "$TMP/origin-gone.git" "$TMP/origin.git"
echo "$OUT23" | jq -e '.["80"].tag != "done" and .["83"].tag == "done"' >/dev/null || fail "(h) a failed fetch must still check ancestry against the existing origin/main: $OUT23"
grep -qF 'ancestry check: git fetch origin main failed; using the existing origin/main' "$TMP/err23" || fail "(h) the failed fetch must be warned: $(cat "$TMP/err23")"
grep -q 'ancestry check skipped' "$TMP/err23" && fail "(h) the check must not be skipped: $(cat "$TMP/err23")"

# (i) a non-GNU `timeout` first on PATH (Windows ships C:/Windows/System32/timeout.exe, which exits 1 with
#     "Invalid syntax" for `timeout 60 cmd`) must not stop the fetch: origin/main moves to a new commit C, and a PR
#     merged at C delivers #85 only if the fetch really ran.
issue 85 "Compact audit log" "$AC"
git clone -q "$TMP/origin.git" "$TMP/other" 2>/dev/null
git -C "$TMP/other" -c user.email=t@t -c user.name=t -c commit.gpgsign=false commit -q --allow-empty -m C
git -C "$TMP/other" push -q origin HEAD:main; SHA_C=$(git -C "$TMP/other" rev-parse HEAD)
jq -n --arg c "$SHA_C" '[{number:97,title:"Audit log compaction",body:"Resolves #85",url:"u97",baseRefName:"main",mergeCommit:{oid:$c}}]' > "$FX/merged.json"
mkdir -p "$TMP/fakebin"
cat > "$TMP/fakebin/timeout" <<'FAKE'
#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "Windows timeout utility"; exit 0; fi
echo "ERROR: Invalid syntax." >&2; exit 1
FAKE
chmod +x "$TMP/fakebin/timeout"
OUT24=$( cd "$REPO_DIR" && PATH="$TMP/fakebin:$PATH" run --issues 85 --config "$TMP/cfg-main.json" 2>"$TMP/err24" )
echo "$OUT24" | jq -e '.["85"].tag == "done"' >/dev/null || fail "(i) a non-GNU timeout must not stop the fetch (PR merged at the new origin/main commit delivers #85): $OUT24 $(cat "$TMP/err24")"

# 25 - W34 path match: two names meet if equal ignoring case, or one is a bare name equal to the other's
#      last segment. Two names that both carry a slash must be equal (src/a.py and lib/a.py do not meet).
echo '[]' > "$FX/merged.json"; echo '[]' > "$FX/open.json"; echo '[]' > "$FX/closed.json"
issue 90 "Alpha widget parser"      $'Edit kwd.py.\n'"$AC"
issue 91 "Bravo exporter rewrite"   $'Edit src/kwd.py.\n'"$AC"
issue 92 "Charlie cache layer"      $'Edit KWD.py.\n'"$AC"
issue 93 "Delta queue worker"       $'Edit kwd.py.\n'"$AC"
issue 94 "Echo retry policy"        $'Edit src/a.py.\n'"$AC"
issue 95 "Foxtrot logging sink"     $'Edit lib/a.py.\n'"$AC"
issue 96 "Golf template engine"     $'Edit tests/test_kwd.py.\n'"$AC"
issue 97 "Hotel session store"      $'Edit test_kwd.py.\n'"$AC"
OUT25=$(run --issues 90,91 )
echo "$OUT25" | jq -e '.["91"].verdict == "sequence" and .["91"].blockedBy == [90] and (.["91"].evidence[0] | test("shared|touches"))' >/dev/null || fail "(a) kwd.py vs src/kwd.py must meet: $OUT25"
echo "$OUT25" | jq -e '.["91"].evidence[0] == "#90 touches src/kwd.py"' >/dev/null || fail "(a) the evidence lists the later card's names: $OUT25"
OUT25=$(run --issues 92,93)
echo "$OUT25" | jq -e '.["93"].verdict == "sequence" and .["93"].blockedBy == [92]' >/dev/null || fail "(b) KWD.py vs kwd.py must meet: $OUT25"
OUT25=$(run --issues 94,95)
echo "$OUT25" | jq -e '.["94"].verdict == "proceed" and .["95"].verdict == "proceed"' >/dev/null || fail "(c) src/a.py vs lib/a.py must NOT meet: $OUT25"
OUT25=$(run --issues 96,97)
echo "$OUT25" | jq -e '.["97"].verdict == "sequence" and .["97"].blockedBy == [96]' >/dev/null || fail "(d) tests/test_kwd.py vs test_kwd.py must meet: $OUT25"
# open PR side: a PR touching src/kwd.py meets an issue naming bare kwd.py, and KWD.py meets kwd.py.
echo '[{"number":150,"title":"Unrelated lunar rover","body":"wip","url":"u","headRefName":"x/lunar","files":[{"path":"src/kwd.py"},{"path":"lib/a.py"}]}]' > "$FX/open.json"
OUT25=$(run --issues 93,92,94)
echo "$OUT25" | jq -e '.["93"].verdict == "sequence" and .["93"].tag == "overlap" and .["93"].conflictWith == [150]' >/dev/null || fail "(e) open PR src/kwd.py vs bare kwd.py must meet: $OUT25"
echo "$OUT25" | jq -e '.["92"].verdict == "sequence" and .["92"].tag == "overlap"' >/dev/null || fail "(e) open PR src/kwd.py vs KWD.py must meet: $OUT25"
echo "$OUT25" | jq -e '.["94"].verdict == "proceed"' >/dev/null || fail "(e) src/a.py vs open PR lib/a.py must NOT meet: $OUT25"

# 26 - W34 bare names: only an allowlisted extension is a file, so identifiers are not (self.name, item.price,
#      Decimal.round); kwd.py and config.yaml are. Stems may be 1 char or start with a digit or _. The
#      extensionless Dockerfile, Makefile, Procfile, Gemfile, Rakefile, Jenkinsfile, Vagrantfile count (whole
#      word, case-sensitive). Dotfiles stay unmatched. v1.2, 1.5 and e.g. are never files.
echo '[]' > "$FX/open.json"
issue 98 "India identifier prose" $'Call self.name and item.price then Decimal.round in kwd.py and config.yaml. Bump v1.2, 1.5, e.g. this, see .env and .gitignore.\n'"$AC"
issue 99 "Juliet odd stems" $'Touch __init__.py, _utils.py, 2fa.py, a.py, src/x.ts, README.MD.\n'"$AC"
issue 100 "Kilo build files" $'Edit Dockerfile, Makefile, Procfile, Gemfile, Rakefile, Jenkinsfile and Vagrantfile; not dockerfile, MAKEFILE or MyDockerfile.\n'"$AC"
OUT26=$(run --issues 98,99,100)
echo "$OUT26" | jq -e '.["98"].files == ["config.yaml","kwd.py"]' >/dev/null || fail "(f) identifiers, versions, e.g. and dotfiles are not files: $(echo "$OUT26" | jq -c '.["98"].files')"
echo "$OUT26" | jq -e '.["99"].files == ["2fa.py","README.MD","__init__.py","_utils.py","a.py","src/x.ts"]' >/dev/null || fail "(g) wider stems: $(echo "$OUT26" | jq -c '.["99"].files')"
echo "$OUT26" | jq -e '.["100"].files == ["Dockerfile","Gemfile","Jenkinsfile","Makefile","Procfile","Rakefile","Vagrantfile"]' >/dev/null || fail "(h) extensionless names, case-sensitive and whole-word: $(echo "$OUT26" | jq -c '.["100"].files')"

# 27 - W34 review: JS frameworks in brand casing are not files (Node.js, Vue.js, D3.js, Chart.js; also when a hyphen
#      follows, Node.js-based). Lowercase node.js, chart.js, react.js, express.js, three.js, next.js ARE files, as are
#      App.js, app.js, my-App.js and nodeapp.js. A miss lets cards collide; a false match only delays one wave.
issue 101 "Lima framework prose" $'Built on Node.js-based, Vue.js, Next.js, Nuxt.js, Express.js, D3.js, Three.js and Chart.js; edit App.js, app.js, my-App.js, nodeapp.js, node.js, chart.js, react.js, express.js, three.js, next.js.\n'"$AC"
OUT27=$(run --issues 101)
echo "$OUT27" | jq -e '.["101"].files == ["App.js","app.js","chart.js","express.js","my-App.js","next.js","node.js","nodeapp.js","react.js","three.js"]' >/dev/null || fail "(a) brand-cased framework names are not files, everything else is: $(echo "$OUT27" | jq -c '.["101"].files')"

# 28 - W34 review: extensionless build files count under a directory prefix and with a suffix; never inside a longer word.
#      The bare name is also reported (a miss lets cards collide), so deploy/Dockerfile also yields Dockerfile.
issue 102 "Mike deploy files" $'Edit deploy/Dockerfile, infra/Makefile, Dockerfile.prod and docker/Dockerfile.dev; not MyDockerfileX.\n'"$AC"
OUT28=$(run --issues 102)
echo "$OUT28" | jq -e '.["102"].files == ["Dockerfile","Dockerfile.dev","Dockerfile.prod","Makefile","deploy/Dockerfile","docker/Dockerfile.dev","infra/Makefile"]' >/dev/null || fail "(a) build files in subdirectories and with suffixes: $(echo "$OUT28" | jq -c '.["102"].files')"

# 29 - W34 review round 2: adjacent build names, hyphen suffixes and a ~ prefix.
issue 103 "November adjacent names" $'Edit Dockerfile/Makefile today.\n'"$AC"
issue 104 "Oscar reversed names"    $'Edit Makefile/Dockerfile today.\n'"$AC"
issue 105 "Papa hyphen suffixes"    $'Edit Dockerfile-prod, Dockerfile-dev and ~/Dockerfile.\n'"$AC"
OUT29=$(run --issues 103,104,105)
echo "$OUT29" | jq -e '.["103"].files | (index("Dockerfile") != null and index("Makefile") != null)' >/dev/null || fail "(a) Dockerfile/Makefile yields both names: $(echo "$OUT29" | jq -c '.["103"].files')"
echo "$OUT29" | jq -e '.["104"].files | (index("Dockerfile") != null and index("Makefile") != null)' >/dev/null || fail "(b) Makefile/Dockerfile yields both names: $(echo "$OUT29" | jq -c '.["104"].files')"
echo "$OUT29" | jq -e '.["105"].files | (index("Dockerfile-prod") != null and index("Dockerfile-dev") != null)' >/dev/null || fail "(c) hyphen suffixes: $(echo "$OUT29" | jq -c '.["105"].files')"
echo "$OUT29" | jq -e '.["105"].files | (index("~/Dockerfile") != null)' >/dev/null || fail "(d) ~/Dockerfile: $(echo "$OUT29" | jq -c '.["105"].files')"

# 30 - W41: blockedBy keeps only issues that are still OPEN. An open PR overlaps the card and closes #301,
#      but #301 was closed (not planned) while the PR stayed open: sequencing behind it would be swept,
#      re-held, and swept again forever. With every closing issue closed, blockedBy is [] (the
#      "sequence, blockedBy empty -> proceed with a conflict note" row). An open closing issue still gates.
echo '[]' > "$FX/merged.json"; echo '[]' > "$FX/closed.json"
issue 300 "Lima overlap target" $'Edit src/w41/shared.ts.
'"$AC"
echo '[{"number":160,"title":"Unrelated tango work","body":"Resolves #301","url":"u","headRefName":"x/tango","files":[{"path":"src/w41/shared.ts"}]}]' > "$FX/open.json"
echo '[]' > "$FX/open-issues.json"
OUT30=$(run --issues 300)
echo "$OUT30" | jq -e '.["300"].verdict == "sequence" and .["300"].tag == "overlap" and .["300"].blockedBy == [] and .["300"].conflictWith == [160]' >/dev/null || fail "(a) a closed closing issue must not be in blockedBy: $OUT30"
echo '[{"number":301}]' > "$FX/open-issues.json"
OUT30=$(run --issues 300)
echo "$OUT30" | jq -e '.["300"].blockedBy == [301] and .["300"].conflictWith == [160]' >/dev/null || fail "(b) an open closing issue still gates: $OUT30"
echo '[{"number":160,"title":"Unrelated tango work","body":"Resolves #301 and fixes #302","url":"u","headRefName":"x/tango","files":[{"path":"src/w41/shared.ts"}]}]' > "$FX/open.json"
echo '[{"number":302}]' > "$FX/open-issues.json"
OUT30=$(run --issues 300)
echo "$OUT30" | jq -e '.["300"].blockedBy == [302]' >/dev/null || fail "(c) only the open one of two closing issues stays: $OUT30"
# (d) a closing ref that is not an issue (a PR, or deleted/transferred) must neither halt the run nor block.
echo '[{"number":160,"title":"Unrelated tango work","body":"Resolves #999","url":"u","headRefName":"x/tango","files":[{"path":"src/w41/shared.ts"}]}]' > "$FX/open.json"
echo '[{"number":302}]' > "$FX/open-issues.json"
OUT30=$(run --issues 300)
[ ! -e "$SB_GITHUB_HALT_FILE" ] || fail "(d) a not-an-issue ref halted the run: $(cat "$SB_GITHUB_HALT_FILE")"
[ "$(grep -c '^issue view 999' "$GH_LOG")" -eq 0 ] || fail "(d) closing refs must not be read one by one"
echo "$OUT30" | jq -e '.["300"].blockedBy == []' >/dev/null || fail "(d) a ref that is not an open issue is not a blocker: $OUT30"

# 31 - W41 batch peer rule: a card that is "sequence, blockedBy empty" (its only closing issue is closed)
#      builds anyway, so it must count as a peer. Cards 1, 2, 3 all name src/kwd.py; open PR 90 closes #7,
#      CLOSED. Card 1 builds with a conflict note; 2 and 3 go behind card 1, not beside it.
echo '[{"number":90,"title":"Quebec unrelated","body":"Closes #7","url":"u","headRefName":"x/quebec","files":[{"path":"src/kwd.py"}]}]' > "$FX/open.json"
echo '[]' > "$FX/open-issues.json"
issue 1 "Romeo parser rework"   $'Edit src/kwd.py.
'"$AC"
issue 2 "Sierra cache layer"    $'Edit src/kwd.py.
'"$AC"
issue 3 "Tango queue worker"    $'Edit src/kwd.py.
'"$AC"
OUT31=$(run --issues 1,2,3)
echo "$OUT31" | jq -e '.["1"] | .verdict == "sequence" and .blockedBy == [] and .conflictWith == [90]' >/dev/null || fail "(a) card 1 builds with a conflict note: $OUT31"
echo "$OUT31" | jq -e '.["2"] | .verdict == "sequence" and .blockedBy == [1]' >/dev/null || fail "(b) card 2 goes behind card 1: $OUT31"
echo "$OUT31" | jq -e '.["3"] | .verdict == "sequence" and .blockedBy == [1]' >/dev/null || fail "(c) card 3 goes behind card 1: $OUT31"

echo "PASS: test-preflight.sh (31 scenarios)"
