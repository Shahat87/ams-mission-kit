#!/usr/bin/env bash
# Tests super-board-merge-gate.sh — the merge mutex and the freshness gate.
# Scenarios 1-7 stop at or before the first `gh pr view`. Scenarios 8-13 run past
# it against a local bare `origin` and a `gh` stub on PATH, to pin the head guard.
#
# The gate exists because `mergeable: CLEAN` was trusted twice on 2026-08-20 and
# was wrong both times: the text did not conflict, but a shared interface had
# grown on the base since the branch was tested, so the merge would have turned
# the base branch red. These tests pin the two properties that prevent it — one
# merge at a time, and the proof happens under the lock.
set -euo pipefail
cd "$(dirname "$0")"
GATE="../scripts/super-board-merge-gate.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }

setup() {  # $1 = verify_commands JSON array
  TMP=$(mktemp -d)
  export SB_GITHUB_HALT_FILE="$TMP/halt.json" SB_GITHUB_RETRY_DELAY=0
  mkdir -p "$TMP/bin"
  printf '#!/usr/bin/env bash\nexit 1\n' > "$TMP/bin/gh"
  chmod +x "$TMP/bin/gh"
  export PATH="$TMP/bin:$PATH"
  mkdir -p "$TMP/.claude/super-board/inflight"
  printf '{"base_branch":"staging","repo":{"path":"%s","remote":"https://github.com/x/y.git"},"verify_commands":%s}' \
    "$TMP" "${1:-[\"git diff --check\"]}" > "$TMP/c.json"
  LOCK="$TMP/.claude/super-board/inflight/merge.lock"
}
teardown() { rm -rf "$TMP"; }

# 1 — a held, fresh lock makes a second caller wait and then give up with 4.
#     Exit 4 is distinct on purpose: "someone else is merging" is not the same
#     answer as "this branch is broken", and the caller routes them differently.
setup
mkdir -p "$LOCK"; date +%s > "$LOCK/at"
RC=0; "$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --stale-after 9999 >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 4 ] || fail "a contended fresh lock should exit 4, got $RC"
[ -d "$LOCK" ] || fail "the loser must not delete the winner's lock"
teardown

# 2 — a lock older than --stale-after is reaped. A process that died holding it
#     would otherwise wedge every future merge on the board.
setup
mkdir -p "$LOCK"; echo $(( $(date +%s) - 99999 )) > "$LOCK/at"
OUT=$("$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --stale-after 60 2>&1 || true)
echo "$OUT" | grep -q "reaping a lock abandoned" || fail "an abandoned lock should be reaped, got: $OUT"
teardown

# 3 — THE REGRESSION THIS FILE EXISTS FOR. --lock-timeout and --stale-after must
#     be independent. A caller willing to wait 20 minutes must NOT thereby decide
#     that a 20-minute-old lock is dead: that reaps the very merge it is queued
#     behind. With them collapsed into one number (the first implementation), a
#     lock aged just past the wait window was stolen.
setup
mkdir -p "$LOCK"; echo $(( $(date +%s) - 30 )) > "$LOCK/at"
OUT=$("$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --stale-after 9999 2>&1 || true)
echo "$OUT" | grep -q "reaping" && fail "a 30s-old lock must survive a 6s waiter with stale-after 9999"
echo "$OUT" | grep -q "could not take the merge lock" || fail "the waiter should time out politely, got: $OUT"
teardown

# 4 — the default stale-after has an absolute floor, not just a multiple of the
#     wait. An impatient caller must not be able to reap by being impatient: with
#     only a 3x multiple, `--lock-timeout 6` outlived its own 18s threshold inside
#     the polling loop and stole a live lock.
setup
mkdir -p "$LOCK"; echo $(( $(date +%s) - 60 )) > "$LOCK/at"
OUT=$("$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 2>&1 || true)
echo "$OUT" | grep -q "reaping" && fail "a 60s-old lock must survive an impatient waiter (floor is 1800s)"
echo "$OUT" | grep -q "could not take the merge lock" || fail "the impatient waiter should time out, got: $OUT"
teardown

# 5 — missing verification is a setup halt, not permission to merge.
for commands in '[]' '["", " ", "\t\n"]' '["\u0000"]' '["git\u0000 diff --check"]'; do
  setup "$commands"
  RC=0; OUT=$("$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 2>&1) || RC=$?
  [ "$RC" -eq 9 ] || fail "unusable verification must exit 9, got $RC: $OUT"
  echo "$OUT" | grep -q 'verification-readiness' || fail "must name the setup problem"
  echo "$OUT" | grep -q 'verify_commands' || fail "must name the setting to repair"
  [ ! -d "$LOCK" ] || fail "readiness failure must not acquire the merge lock"
  [ ! -f "$TMP/halt.json" ] || fail "readiness failure must not attempt GitHub reads"
  teardown
done
setup
jq 'del(.verify_commands)' "$TMP/c.json" > "$TMP/missing.json"
RC=0; "$GATE" --config "$TMP/missing.json" --pr 1 >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 9 ] || fail "missing verification must exit 9, got $RC"
teardown

# 6 — the lock is released on every exit path, including the failures above.
#     A gate that strands its own lock wedges the board it was written to protect.
setup
OUT=$("$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 2>&1 || true)
[ ! -d "$LOCK" ] || fail "the lock must be released on exit, even when the run fails"
teardown

# 7 — usage errors are distinct from operational ones, so a caller never reads a
#     typo as "the branch is broken".
setup
RC=0; "$GATE" --config "$TMP/c.json" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "a missing --pr should exit 64, got $RC"
RC=0; "$GATE" --config /nope/nope.json --pr 1 >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 66 ] || fail "a missing config should exit 66, got $RC"
teardown

# ── Head guard (8-11). A real git origin with a `staging` base and a `feat`
#    branch; `gh` is a stub that reports feat's head and logs every call.
#    STUB_OID       — headRefOid `gh pr view` reports
#    STUB_OID_AFTER — headRefOid reported once a merge has been attempted
#    STUB_MERGE_RC  — exit code of `gh pr merge`
head_setup() {
  setup '["git diff --check"]'
  ORIGIN="$TMP/origin.git"; git init -q --bare "$ORIGIN"
  git -C "$TMP" init -q -b staging 2>/dev/null || { git -C "$TMP" init -q; git -C "$TMP" checkout -q -b staging; }
  git -C "$TMP" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
  git -C "$TMP" remote add origin "$ORIGIN"; git -C "$TMP" push -q origin staging
  git -C "$TMP" checkout -q -b feat
  git -C "$TMP" -c user.email=t@t -c user.name=t commit -q --allow-empty -m feat
  git -C "$TMP" push -q origin feat; git -C "$TMP" checkout -q staging
  SHA=$(git -C "$TMP" rev-parse feat)
  mkdir -p "$TMP/bin"; export GH_LOG="$TMP/gh.log"; : > "$GH_LOG"
  cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$1 $2" in
  "pr view")
    case "$*" in *state,mergeCommit*)
      [ "${STUB_STATE_FAIL:-0}" = 0 ] || { grep -q '^pr merge' "$GH_LOG" && exit 1; }
      state="${STUB_STATE:-OPEN}"; oid="$STUB_OID"
      [ -z "${STUB_STATE_LATE:-}" ] || [ "$(grep -c 'state,mergeCommit' "$GH_LOG")" -lt 2 ] || state="$STUB_STATE_LATE"
      base="${STUB_BASE-staging}"; [ -z "${STUB_BASE_LATE:-}" ] || [ "$(grep -c 'state,mergeCommit' "$GH_LOG")" -lt 2 ] || base="$STUB_BASE_LATE"
      if grep -q '^pr merge' "$GH_LOG"; then state="${STUB_STATE_AFTER:-OPEN}"; oid="${STUB_OID_AFTER:-$STUB_OID}"; fi
      jq -n --arg state "$state" --arg head "$oid" --arg base "$base" '{state:$state,headRefOid:$head,mergeCommit:(if $state=="MERGED" then {oid:$head} else null end)} + (if $base == "" then {} else {baseRefName:$base} end)'; exit 0 ;;
    esac
    case "$*" in *labels*)
      if [ -n "${STUB_META_AFTER:-}" ] && [ -f "$STUB_VERIFIED" ]; then printf '%s\n' "$STUB_META_AFTER"; exit 0; fi
      if [ -n "${STUB_META:-}" ]; then printf '%s\n' "$STUB_META"; else echo '{"files":[],"labels":[],"body":"","additions":0,"deletions":0,"changedFiles":0}'; fi
      exit 0 ;; esac
    oid="$STUB_OID"; grep -q '^pr merge' "$GH_LOG" && oid="${STUB_OID_AFTER:-$STUB_OID}"
    echo "feat $oid" ;;
  "pr merge") exit "${STUB_MERGE_RC:-0}" ;;
  "api user") case "$*" in *" -q "*) echo "${STUB_ACTOR:-robot}" ;; *) jq -n --arg l "${STUB_ACTOR:-robot}" '{login:$l}' ;; esac ;;
  "pr diff")
    [ "${STUB_DIFF_FAIL:-0}" = 0 ] || exit 1
    if [ -n "${STUB_DIFF:-}" ]; then printf '%b' "$STUB_DIFF"; exit 0; fi
    python3 - <<'DIFF'
import json, os
m = json.loads(os.environ.get('STUB_META') or '{"files":[],"additions":0,"deletions":0}')
for i, f in enumerate(m.get('files', [])):
    p = f['path']; print(f'diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}')
    added = m.get('additions', 0) if i == 0 else 0
    removed = m.get('deletions', 0) if i == 0 else 0
    if added or removed:
        print(f'@@ -1,{removed} +1,{added} @@')
        print(''.join('-old\n' for _ in range(removed)) + ''.join('+new\n' for _ in range(added)), end='')
DIFF
    ;;
  "api graphql")
    jq -n --arg head "$STUB_OID" '[{data:{repository:{defaultBranchRef:{name:"main"},pullRequest:{headRefOid:$head,state:"OPEN",baseRefName:"main",body:"",closingIssuesReferences:{pageInfo:{hasNextPage:false,endCursor:null},nodes:[{number:42,repository:{nameWithOwner:"x/y"}}]}}}}}]' ;;
  "api --paginate")
    [ "${STUB_APPROVAL:-0}" = 1 ] || { echo '[[]]'; exit 0; }
    case "$*" in
      */issues/42/comments*) cat "$STUB_COMMENTS" ;;
      */issues/1/comments*) echo '[[]]' ;;
      *) exit 1 ;;
    esac ;;
  "api repos/x/y/collaborators/owner/permission")
    [ "${STUB_PERMISSION_FAIL:-0}" = 0 ] || exit 1
    echo '{"permission":"admin"}' ;;
  *) exit 1 ;;
esac
STUB
  chmod +x "$TMP/bin/gh"
}
gate() { SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$TMP/halt.json" PATH="$TMP/bin:$PATH" "${GATE_OVERRIDE:-$GATE}" --config "$TMP/c.json" --pr 1 --lock-timeout 6 "$@"; }

# 8 — head moved after review: the reviewed sha no longer matches the PR head.
#     Exit 6, and no merge is attempted on evidence gathered for another commit.
head_setup
RC=0; STUB_OID="$SHA" gate --expect-head deadbeef >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 6 ] || fail "a moved head should exit 6, got $RC"
grep -q '^pr merge' "$GH_LOG" && fail "no merge may be attempted when the head moved"
teardown

# 8b — layer B: the PR must target the configured base_branch (staging here).
#      A PR aimed at main would otherwise be verified against staging and merged.
head_setup
RC=0; OUT=$(STUB_OID="$SHA" STUB_BASE=main gate --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 3 ] || fail "a PR on the wrong base must exit 3, got $RC: $OUT"
echo "$OUT" | grep -q "configured base_branch" || fail "must say why, got: $OUT"
grep -q '^pr merge' "$GH_LOG" && fail "no merge may be attempted on the wrong base"
RC=0; OUT=$(STUB_OID="$SHA" STUB_BASE="" gate --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 3 ] || fail "an unreadable base must refuse (3), got $RC: $OUT"
teardown

# 8d — layer B: the base is re-read right before the merge (TOCTOU). The second
#      state read (the pre-merge one; the first is on entry) reports a retargeted PR.
head_setup
RC=0; OUT=$(STUB_OID="$SHA" STUB_BASE_LATE=main gate --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 3 ] || fail "a base retargeted before merge must exit 3, got $RC: $OUT"
grep -q '^pr merge' "$GH_LOG" && fail "no merge may be attempted after the base moved"
teardown

# 8e — layer B: the PR must still be OPEN right before the merge (closed during verify).
head_setup
RC=0; OUT=$(STUB_OID="$SHA" STUB_STATE_LATE=CLOSED gate --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 3 ] || fail "a PR closed during verification must exit 3, got $RC: $OUT"
grep -q '^pr merge' "$GH_LOG" && fail "no merge may be attempted on a PR closed during verification"
teardown

# 8c — layer B: notifications.bot_identity pins the acting login (case-insensitive);
#      "auto" and empty mean unset and never call `gh api user`.
head_setup
jq '.notifications = {bot_identity: "robot"}' "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"
RC=0; OUT=$(STUB_OID="$SHA" STUB_ACTOR=someone-else gate --expect-head "$SHA" --dry-run 2>&1) || RC=$?
[ "$RC" -eq 11 ] || fail "a different acting identity must halt with 11, got $RC: $OUT"
echo "$OUT" | grep -q "acting identity someone-else != bot_identity robot" || fail "must name both identities, got: $OUT"
RC=0; STUB_OID="$SHA" STUB_ACTOR=ROBOT gate --expect-head "$SHA" --dry-run >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "a matching identity (any case) should proceed, got $RC"
for unset in auto ""; do
  jq --arg v "$unset" '.notifications.bot_identity = $v' "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"
  : > "$GH_LOG"
  RC=0; STUB_OID="$SHA" STUB_ACTOR=anyone gate --expect-head "$SHA" --dry-run >/dev/null 2>&1 || RC=$?
  [ "$RC" -eq 0 ] || fail "bot_identity '$unset' must not gate, got $RC"
  grep -q '^api user' "$GH_LOG" && fail "bot_identity '$unset' must not look up the acting login"
done
teardown

# 9 — head unchanged: merge pins the reviewed commit with --match-head-commit.
head_setup
RC=0; STUB_OID="$SHA" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "a matching head should merge (exit 0), got $RC"
grep -q -- "--match-head-commit $SHA" "$GH_LOG" || fail "merge must pass --match-head-commit $SHA, got: $(cat "$GH_LOG")"
teardown

# 9b — the squash message: --subject and --body-file reach `gh pr merge` (writing standard).
head_setup
printf -- '- 413 shows the size message\nCloses #1\n' > "$TMP/msg.md"
RC=0; STUB_OID="$SHA" gate --expect-head "$SHA" --subject "🐛 [fix] receipts: show a size error" --body-file "$TMP/msg.md" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "a merge with a subject should exit 0, got $RC"
grep -q -- "--subject 🐛 \[fix\] receipts: show a size error" "$GH_LOG" || fail "merge must pass --subject, got: $(cat "$GH_LOG")"
grep -q -- "--body-file $TMP/msg.md" "$GH_LOG" || fail "merge must pass --body-file, got: $(cat "$GH_LOG")"
RC=0; STUB_OID="$SHA" gate --expect-head "$SHA" --body-file /nope.md >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 66 ] || fail "an unreadable --body-file should exit 66, got $RC"
teardown

# 10 — a push lands during verification: GitHub refuses the pinned merge and the
#      head has moved, so this is void evidence (6), not branch protection (3).
head_setup
RC=0; STUB_OID="$SHA" STUB_OID_AFTER=cafef00dcafef00dcafef00dcafef00dcafef00d STUB_MERGE_RC=1 gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 6 ] || fail "a head that moved mid-gate should exit 6, got $RC"
teardown

# 11 — same refusal with the head unchanged is still GitHub saying no (3).
head_setup
RC=0; STUB_OID="$SHA" STUB_MERGE_RC=1 gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 79 ] || fail "unconfirmed merge must pause for reconciliation, got $RC"
teardown

# A lost merge response is reconciled, never blindly repeated.
head_setup
RC=0; STUB_OID="$SHA" STUB_STATE_AFTER=MERGED STUB_MERGE_RC=1 gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "GitHub-confirmed lost merge response must report merged"
[ "$(grep -c '^pr merge' "$GH_LOG")" -eq 1 ] || fail "merge mutation must only run once"
teardown
head_setup
RC=0; STUB_OID="$SHA" STUB_STATE_FAIL=1 STUB_MERGE_RC=1 gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 79 ] || fail "unreadable merge outcome must halt"
[ "$(grep -c '^pr merge' "$GH_LOG")" -eq 1 ] || fail "unknown merge outcome must not replay mutation"
teardown

# 12 — post-merge cleanup: when the cleanup-wt hook is installed the gate runs it with
#      --post-merge --base <base> after a merge, and its failure never fails the
#      merge that already happened.
head_setup
mkdir -p "$TMP/.claude/hooks"
printf 'import sys\nopen("%s/cleanup.args","w").write(" ".join(sys.argv[1:]))\nsys.exit(1)\n' "$TMP" \
  > "$TMP/.claude/hooks/cleanup-wt.py"
RC=0; STUB_OID="$SHA" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "a failing cleanup must not fail the merge, got $RC"
grep -q -- "--post-merge --base staging" "$TMP/cleanup.args" 2>/dev/null \
  || fail "cleanup-wt should run with --post-merge --base staging, got: $(cat "$TMP/cleanup.args" 2>/dev/null)"
teardown

# 13 — a refused merge never cleans up. (Scenario 9 is the no-cleanup-wt path.)
head_setup
mkdir -p "$TMP/.claude/hooks"
printf 'open("%s/cleanup.ran","w")\n' "$TMP" > "$TMP/.claude/hooks/cleanup-wt.py"
RC=0; STUB_OID="$SHA" STUB_MERGE_RC=1 gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 79 ] || fail "an unconfirmed merge should halt, got $RC"
[ ! -f "$TMP/cleanup.ran" ] || fail "cleanup must not run when the merge was refused"
teardown

# ── Merge policy and migrations (14-25). Same origin and stub; STUB_META is the
#    `gh pr view --json labels,files,…` answer, STUB_DIFF the `gh pr diff` text.
cfgset() { jq "$1" "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"; }
meta() {  # $1 labels csv, $2 files csv, $3 body, $4 size
  jq -cn --arg l "$1" --arg f "$2" --arg b "${3:-}" --argjson n "${4:-1}" \
    '{labels: ($l | split(",") | map(select(. != "") | {name: .})),
      files:  ($f | split(",") | map(select(. != "") | {path: .})),
      additions: $n, deletions: 0, body: $b} | .changedFiles = (.files | length)'
}

# Required policy evidence: an unavailable diff must halt before migrations or merge.
head_setup
cfgset ".migrations = {allowed_envs: [\"staging\"], target_env: \"staging\", commands: {staging: \"touch $TMP/ran\"}}"
RC=0; SB_GITHUB_RETRY_DELAY=0 STUB_DIFF_FAIL=1 STUB_OID="$SHA" STUB_META="$(meta "" supabase/migrations/1.sql)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 79 ] || fail "failed required diff must halt run (79), got $RC"
[ ! -f "$TMP/ran" ] || fail "no migration may run without policy evidence"
grep -q '^pr merge' "$GH_LOG" && fail "no merge may run without policy evidence"
[ "$(grep -c '^pr diff' "$GH_LOG")" -eq 3 ] || fail "exactly three failed read attempts"
teardown

# 14 — a money label: a human merges. Exit 7, the category is named, no merge.
head_setup
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" 2>/dev/null) || RC=$?
[ "$RC" -eq 7 ] || fail "a money label should exit 7, got $RC"
echo "$OUT" | grep -q "^human-gate: money — label money" || fail "exit 7 must name the category, got: $OUT"
grep -q '^pr merge' "$GH_LOG" && fail "no merge may be attempted on a human-gated PR"
teardown

# 15 — an auth path, by glob; merge_policy.default human; a diff over auto_max_lines.
head_setup
RC=0; STUB_OID="$SHA" STUB_META="$(meta "" src/auth/session.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "a path under **/auth/** should exit 7, got $RC"
cfgset '.merge_policy = {default: "human"}'
RC=0; STUB_OID="$SHA" STUB_META="$(meta "" src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "merge_policy.default human should exit 7, got $RC"
cfgset '.merge_policy = {default: "auto", auto_max_lines: 100}'
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$(meta "" src/x.ts "" 400)" gate --expect-head "$SHA" 2>/dev/null) || RC=$?
[ "$RC" -eq 7 ] || fail "400 lines over auto_max_lines 100 should exit 7, got $RC"
echo "$OUT" | grep -q "human-gate: size" || fail "the size gate must say so, got: $OUT"
teardown

# 15b — the size cap defaults to 400 changed lines: 401 gates ("big PR"), 400
#       merges, and auto_max_lines 0 turns the cap off.
head_setup
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$(meta "" src/x.ts "" 401)" gate --expect-head "$SHA" 2>/dev/null) || RC=$?
[ "$RC" -eq 7 ] || fail "401 lines over the default cap 400 should exit 7, got $RC"
echo "$OUT" | grep -q "human-gate: size — big PR — please review" || fail "the size gate must say big PR, got: $OUT"
RC=0; STUB_OID="$SHA" STUB_META="$(meta "" src/x.ts "" 400)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "400 lines is at the default cap and should merge, got $RC"
cfgset '.merge_policy = {auto_max_lines: 0}'
RC=0; STUB_OID="$SHA" STUB_META="$(meta "" src/x.ts "" 5000)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "auto_max_lines 0 should turn the size cap off, got $RC"
teardown

# 15c — lockfiles, snapshots and generated files do not count toward the cap;
#       migration SQL is left out and reported separately; size_exclude is
#       configurable ([] counts everything).
head_setup
BIG=$(jq -cn '{labels: [], body: "", additions: 2300, deletions: 0, files: [
  {path: "src/x.ts", additions: 300, deletions: 0},
  {path: "package-lock.json", additions: 900, deletions: 0},
  {path: "src/__snapshots__/x.test.ts.snap", additions: 500, deletions: 0},
  {path: "src/api/__generated__/types.ts", additions: 400, deletions: 0},
  {path: "db/migrations/001_add.sql", additions: 200, deletions: 0}], changedFiles: 5}')
cfgset '.migrations = {allowed_envs: ["test"], target_env: "test", commands: {test: "true"}}'
RC=0; STUB_OID="$SHA" STUB_META="$BIG" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "300 counted lines (rest excluded) should merge, got $RC"
cfgset '.merge_policy = {auto_max_lines: 250}'
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$BIG" gate --expect-head "$SHA" 2>/dev/null) || RC=$?
[ "$RC" -eq 7 ] || fail "300 counted lines over cap 250 should exit 7, got $RC"
echo "$OUT" | grep -q "300 changed lines > auto_max_lines 250 (+200 migration lines counted separately)" \
  || fail "size gate should count 300 and report migration lines separately, got: $OUT"
cfgset '.merge_policy = {size_exclude: []}'
RC=0; STUB_OID="$SHA" STUB_META="$BIG" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "size_exclude [] should count the lockfile and exit 7, got $RC"
teardown

# 16 — a destructive keyword counts in ADDED and (layer B) REMOVED lines: removing a
#      guard or a DROP TABLE statement is as sensitive as adding one.
head_setup
RC=0; STUB_OID="$SHA" STUB_META="$(meta "" db/x.sql)" STUB_DIFF='diff --git a/src/x.ts b/src/x.ts\n--- a/src/x.ts\n+++ b/src/x.ts\n@@ -0,0 +1 @@\n+DROP TABLE users;\n' gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "an added DROP TABLE should exit 7 (schema), got $RC"
RC=0; STUB_OID="$SHA" STUB_META="$(meta "" db/x.sql | jq ' .additions=0 | .deletions=1')" STUB_DIFF='diff --git a/db/x.sql b/db/x.sql\n--- a/db/x.sql\n+++ b/db/x.sql\n@@ -1 +0,0 @@\n-DROP TABLE users;\n' gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "a removed DROP TABLE must gate (layer B), got $RC"
teardown

# 16b — a stale UI label cannot approve a policy gate.
head_setup
RC=0; STUB_OID="$SHA" STUB_META="$(meta money,needs-you:done src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "a needs-you:done label alone must not authorize a policy bypass, got $RC"
teardown

# 16e — layer B: with trusted_approvers ["owner"] the acting robot's own `done`
#       never approves; the trusted human's `done` does.
head_setup
export STUB_APPROVAL=1 STUB_COMMENTS="$TMP/comments.json"
echo '[[]]' > "$STUB_COMMENTS"
# No bot_identity here, and the robot IS a trusted approver: the actor exclusion is
# then the only thing that can reject the robot's own `done`.
jq '.merge_policy = {trusted_approvers: ["owner","robot"]}' "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"
OUT=$(STUB_OID="$SHA" STUB_ACTOR=robot STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" 2>/dev/null || true)
REQUEST=$(echo "$OUT" | sed -n 's/^approval-request: //p')
[ -n "$REQUEST" ] || fail "a hold must emit a pinned request"
done_by() {
  jq -n --argjson request "$REQUEST" --arg who "$1" '[[
    {id:1,body:("Reason tag: 🙋 needs you\napproval-request: " + ($request|tojson)),created_at:"2026-10-03T00:00:01Z",updated_at:"2026-10-03T00:00:01Z",user:{login:"owner",type:"User"}},
    {id:2,body:"done",created_at:"2026-10-03T00:00:02Z",updated_at:"2026-10-03T00:00:02Z",user:{login:$who,type:"User"}}
  ]]' > "$STUB_COMMENTS"
}
done_by robot
RC=0; STUB_OID="$SHA" STUB_ACTOR=robot STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "the acting robot's own done must not approve (7), got $RC"
done_by owner
RC=0; STUB_OID="$SHA" STUB_ACTOR=robot STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "a trusted human's done must approve (0), got $RC"
# bot_identity pinned (separate machine-user), trusted humans only: owner's done approves.
jq '.notifications = {bot_identity: "robot"} | .merge_policy = {trusted_approvers: ["owner"]}' "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"
RC=0; STUB_OID="$SHA" STUB_ACTOR=robot STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "pinned machine-user + trusted human's done must approve (0), got $RC"
# bot_identity pinned but no trusted_approvers: fail closed even for the owner.
jq 'del(.merge_policy.trusted_approvers)' "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"
RC=0; STUB_OID="$SHA" STUB_ACTOR=robot STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "bot_identity without trusted_approvers must hold (7), got $RC"
# A "[bot]" identity (GitHub App) skips ONLY the `api user` actor pin: it still counts
# as pinned for the fail-closed rule (no trusted_approvers -> hold), and with a trusted
# human's done it approves even though the actor login differs.
jq '.notifications.bot_identity = " Super-Board[BOT] "' "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"
: > "$GH_LOG"
RC=0; OUT=$(STUB_OID="$SHA" STUB_ACTOR=someone-else STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 7 ] || fail "a [bot] identity without trusted_approvers must still fail closed (7), got $RC: $OUT"
jq '.merge_policy = {trusted_approvers: ["owner"]}' "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"
: > "$GH_LOG"
RC=0; OUT=$(STUB_OID="$SHA" STUB_ACTOR=someone-else STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 0 ] || fail "[bot] + trusted owner's done must approve (0), got $RC: $OUT"
# bot_identity that is itself a trusted approver (any case/space) is a config error for the whole run: halt 11.
jq '.notifications.bot_identity = " Robot " | .merge_policy = {trusted_approvers: ["owner","robot"]}' "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"
: > "$GH_LOG"
RC=0; OUT=$(STUB_OID="$SHA" STUB_ACTOR=robot STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 11 ] || fail "bot_identity in trusted_approvers must halt with exit 11, got $RC: $OUT"
echo "$OUT" | grep -q "config: bot_identity Robot is also a trusted approver" || fail "the halt must give the reason, got: $OUT"
grep -q '^pr merge' "$GH_LOG" && fail "no merge on an identity config error"
unset STUB_APPROVAL STUB_COMMENTS
teardown

# 16c — a trusted done confirms the request emitted for the exact current head.
# The request exists before the human comment; processing never binds old done
# to whatever head happens to exist now.
head_setup
export STUB_APPROVAL=1 STUB_COMMENTS="$TMP/comments.json"
echo '[[]]' > "$STUB_COMMENTS"
OUT=$(STUB_OID="$SHA" STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" 2>/dev/null || true)
REQUEST=$(echo "$OUT" | sed -n 's/^approval-request: //p')
[ -n "$REQUEST" ] || fail "a hold must emit a pinned request"
make_approval() {
  jq -n --argjson request "$REQUEST" '[[
    {id:1,body:("Reason tag: 🙋 needs you\napproval-request: " + ($request|tojson)),created_at:"2026-10-03T00:00:01Z",updated_at:"2026-10-03T00:00:01Z",user:{login:"owner",type:"User"}},
    {id:2,body:"done",created_at:"2026-10-03T00:00:02Z",updated_at:"2026-10-03T00:00:02Z",user:{login:"owner",type:"User"}}
  ]]' > "$STUB_COMMENTS"
}
make_approval
RC=0; STUB_OID="$SHA" STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "current trusted approval must merge, got $RC"
: > "$GH_LOG"
# A later code version cannot reuse the same human's done.
REQUEST=$(echo "$REQUEST" | jq '.head = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"')
make_approval
RC=0; STUB_OID="$SHA" STUB_META="$(meta money,needs-you:done src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "old-head approval must hold, got $RC"
grep -q '^pr merge' "$GH_LOG" && fail "old approval must never reach merge"
# Neither unreadable authority nor a newer human block can authorize.
REQUEST=$(echo "$REQUEST" | jq --arg head "$SHA" '.head=$head')
make_approval
RC=0; STUB_PERMISSION_FAIL=1 STUB_OID="$SHA" STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 79 ] || fail "unreadable authority must halt, got $RC"
rm -f "$SB_GITHUB_HALT_FILE"  # next test starts a separate recovered run
jq '.[0] += [{id:3,body:"Reason tag: 🙋 new decision",created_at:"2026-10-03T00:00:03Z",updated_at:"2026-10-03T00:00:03Z",user:{login:"owner",type:"User"}}]' "$STUB_COMMENTS" > "$TMP/new.json"
mv "$TMP/new.json" "$STUB_COMMENTS"
RC=0; STUB_OID="$SHA" STUB_META="$(meta money,needs-you:done src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "new human block must revoke prior approval, got $RC"
# Verify takes time: a same-head body edit adds a new human step during it.
make_approval
export STUB_VERIFIED="$TMP/verified"
jq --arg cmd "touch $STUB_VERIFIED" '.verify_commands=[$cmd]' "$TMP/c.json" > "$TMP/new.json"
mv "$TMP/new.json" "$TMP/c.json"
RC=0; STUB_META_AFTER="$(meta money src/x.ts 'needs-you: run a new command')" STUB_OID="$SHA" STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 7 ] || fail "changed human steps during verify must revoke approval, got $RC"
grep -q '^pr merge' "$GH_LOG" && fail "stale approval after verification must not merge"
# Verify commands run without GitHub credentials (spec 6.5: gate hardening).
# A verify command that finds GH_TOKEN/GITHUB_TOKEN in its env, a working `gh auth status`, or a credential
# for github.com from `git credential fill` fails the gate (fixtures/verify_probe.py).
PROBE="python3 \"$PWD/fixtures/verify_probe.py\""
jq --arg cmd "$PROBE" '.verify_commands=[$cmd]' "$TMP/c.json" > "$TMP/probe.json"
mv "$TMP/probe.json" "$TMP/c.json"
RC=0; GH_TOKEN=ghp_should_not_leak GITHUB_TOKEN=ghp_should_not_leak STUB_OID="$SHA" STUB_META="$(meta money src/x.ts)"   gate --expect-head "$SHA" >/dev/null 2>"$TMP/probe.err" || RC=$?
grep -q "verified green" "$TMP/probe.err" || fail "verify must run without GitHub tokens (rc=$RC): $(tail -5 "$TMP/probe.err")"
unset STUB_APPROVAL STUB_COMMENTS STUB_VERIFIED
teardown

# 16d — head-bound approval also covers a declared human-only command; label
# changes cannot grant it, and a later human request during verification revokes it.
head_setup
export STUB_APPROVAL=1 STUB_COMMENTS="$TMP/comments.json"
echo '[[]]' > "$STUB_COMMENTS"
OUT=$(STUB_OID="$SHA" STUB_META="$(meta '' src/x.ts 'needs-you: confirm live setting')" gate --expect-head "$SHA" 2>/dev/null || true)
REQUEST=$(echo "$OUT" | sed -n 's/^approval-request: //p')
make_approval
RC=0; STUB_OID="$SHA" STUB_META="$(meta '' src/x.ts 'needs-you: confirm live setting')" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "trusted confirmation of current human step must merge, got $RC"
: > "$GH_LOG"
# verify runs with a scrubbed env (lane-delegate exec), so the path is written into the script.
cat > "$TMP/new-block.py" <<NEWBLOCK
import json, os
p="$STUB_COMMENTS"
x=json.load(open(p))
x[0].append({"id":3,"body":"Reason tag: 🙋 a new human decision","created_at":"2026-10-03T00:00:03Z","updated_at":"2026-10-03T00:00:03Z","user":{"login":"owner","type":"User"}})
with open(p,"w") as f: json.dump(x,f)
NEWBLOCK
jq --arg cmd "python3 $TMP/new-block.py" '.verify_commands=[$cmd]' "$TMP/c.json" > "$TMP/new.json"
mv "$TMP/new.json" "$TMP/c.json"
RC=0; STUB_OID="$SHA" STUB_META="$(meta '' src/x.ts 'needs-you: confirm live setting')" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 8 ] || fail "a newer human request during verification must hold, got $RC"
grep -q '^pr merge' "$GH_LOG" && fail "revoked human approval must not reach merge"
unset STUB_APPROVAL STUB_COMMENTS
teardown

# 16f — verify failures are blamed on the right thing (lane-delegate exit 20 + base check).
#       The PR adds broken.txt and the verify command rejects it: the same command passes on the base,
#       so the branch is at fault -> exit 2 and the "comes from this branch" line.
head_setup
git -C "$TMP" checkout -q feat; echo x > "$TMP/broken.txt"; git -C "$TMP" add broken.txt
git -C "$TMP" -c user.email=t@t -c user.name=t commit -q -m broken; git -C "$TMP" push -q origin feat
git -C "$TMP" checkout -q staging; SHA=$(git -C "$TMP" rev-parse feat)
cfgset '.verify_commands = ["test ! -f broken.txt"]'
RC=0; OUT=$(STUB_OID="$SHA" gate --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 2 ] || fail "a failure that only the PR causes must exit 2, got $RC: $OUT"
echo "$OUT" | grep -q "verify passes on staging in the same environment, so the failure comes from this branch" || fail "missing the base-passes line: $OUT"
echo "$OUT" | grep -q "send it back to Build for a rebase pass" || fail "missing the rebase message: $OUT"
grep -q '^pr merge' "$GH_LOG" && fail "a failing verify must not merge"
teardown

# 16g — the base fails too (environment problem): exit 9 with the config hint, never the rebase loop.
head_setup
cfgset '.verify_commands = ["exit 3"]'
RC=0; OUT=$(STUB_OID="$SHA" gate --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 9 ] || fail "a failure on the base too must exit 9, got $RC: $OUT"
echo "$OUT" | grep -q "verify fails on staging too in the scrubbed environment" || fail "missing the base-fails line: $OUT"
echo "$OUT" | grep -q "pilot.verify_env_keep" || fail "missing the config hint: $OUT"
echo "$OUT" | grep -q "rebase pass" && fail "an environment failure must not ask for a rebase: $OUT"
grep -q '^pr merge' "$GH_LOG" && fail "a failing verify must not merge"
teardown

# 16h — a missing lane-delegate is a setup problem (exit 9), not green and not a rebase.
head_setup
cp -r ../scripts "$TMP/gs"; rm "$TMP/gs/super-board-lane-delegate.py"
RC=0; OUT=$(STUB_OID="$SHA" SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$TMP/halt.json" PATH="$TMP/bin:$PATH"   "$TMP/gs/super-board-merge-gate.sh" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 9 ] || fail "a missing lane-delegate must exit 9, got $RC: $OUT"
echo "$OUT" | grep -q "lane-delegate missing" || fail "must say the delegate is missing: $OUT"
grep -q '^pr merge' "$GH_LOG" && fail "a missing delegate must not merge"
teardown

# 16i — provider docker (spec section 10, 15.9 F1): the repo is guarded, so the gate verifies on the Lenovo only (where lenovo)
#       and never puts the PR in a host working tree. A stub lane-delegate stands in for the runner and scripts the exit code
#       of each call (DELEGATE_RCS, one per call; "20 0" is a verify that fails on the PR head and passes on the base). The
#       verify command that only touches a marker file would leave it on this host if anything ran here.
install_delegate_stub() {  # $1 = space-separated exit codes, one per delegate call
  cp -r ../scripts "$TMP/gs"
  cat > "$TMP/gs/super-board-lane-delegate.py" <<'PYSTUB'
import os, sys
a = sys.argv[1:]
log = os.environ["DELEGATE_ARGS"]
open(log, "a").write(" ".join(a) + chr(10))
cnt = log + ".n"
n = int(open(cnt).read()) if os.path.exists(cnt) else 0
open(cnt, "w").write(str(n + 1))
rcs = os.environ.get("DELEGATE_RCS", "0").split()
rc = int(rcs[min(n, len(rcs) - 1)])
if rc == 20 and "--log" in a:
    open(a[a.index("--log") + 1], "w").write("FAILED: stub command" + chr(10))
sys.exit(rc)
PYSTUB
  DELEGATE_ARGS="$TMP/delegate.args"; : > "$DELEGATE_ARGS"; rm -f "$DELEGATE_ARGS.n"
  export DELEGATE_ARGS DELEGATE_RCS="$1"
}
docker_gate_setup() {  # $1 = delegate exit codes for the guarded path, $2 = sandbox provider
  head_setup
  git -C "$TMP" checkout -q feat; echo x > "$TMP/broken.txt"; git -C "$TMP" add broken.txt
  git -C "$TMP" -c user.email=t@t -c user.name=t commit -q -m broken; git -C "$TMP" push -q origin feat
  git -C "$TMP" checkout -q staging; SHA=$(git -C "$TMP" rev-parse feat)
  HOSTMARK="$TMP/ran-on-the-host"
  PROV="${2:-docker}"; WHERE=lenovo; [ "$PROV" = docker ] || WHERE=local
  cfgjq --arg mark "touch $HOSTMARK" --arg prov "$PROV" --arg where "$WHERE"     '.verify_commands = ["test ! -f broken.txt", $mark]
     | .pilot.sandbox = {provider: $prov, where: $where, runner_repo: "o/r", repos: ["x/y"],
         image: ("mc-lane-smoke@sha256:" + ("a" * 64)), proxy_image: ("mc-lane-proxy@sha256:" + ("b" * 64))}'
  DLOG="$TMP/docker.log"; : > "$DLOG"
  git -C "$TMP" fetch -q origin staging
  BASESHA=$(git -C "$TMP" rev-parse origin/staging)
  DGATE="$GATE"
  if [ "$PROV" = docker ]; then install_delegate_stub "$1"; DGATE="$TMP/gs/super-board-merge-gate.sh"; fi
}
dgate() {  # the gate: the stub delegate for a guarded repo, the real one (with the stub docker) for provider host
  GATE_OVERRIDE="$DGATE" MC_DOCKER_SMOKE=1 MCL_DOCKER_ARGV="[\"python3\", \"$PWD/fixtures/docker_stub.py\"]" DOCKER_STUB_LOG="$DLOG"     GH_TOKEN=ghp_should_not_leak GITHUB_TOKEN=ghp_should_not_leak     STUB_OID="$SHA" gate --expect-head "$SHA" "$@"
}
cfgjq() {  # jq on the config with extra args: cfgjq [--arg k v]... FILTER
  jq "$@" "$TMP/c.json" > "$TMP/c2.json" && mv "$TMP/c2.json" "$TMP/c.json"
}

# base passes: the PR is what fails -> exit 2. Two delegate calls: the PR head merged onto the base, then the base alone.
docker_gate_setup '20 0'
RC=0; OUT=$(dgate 2>&1) || RC=$?
[ "$RC" -eq 2 ] || fail "docker gate: a PR-only failure must exit 2, got $RC: $OUT"
echo "$OUT" | grep -q "in the verify container" || fail "docker gate must say it uses the container: $OUT"
echo "$OUT" | grep -q "verify passes on staging in the same environment, so the failure comes from this branch" || fail "docker gate: missing the base-passes line: $OUT"
[ "$(wc -l < "$DELEGATE_ARGS")" -eq 2 ] || fail "docker gate: the PR head and the base must each be verified once: $(cat "$DELEGATE_ARGS")"
sed -n 1p "$DELEGATE_ARGS" | grep -q -- "--gate-head $SHA --gate-base $BASESHA" || fail "docker gate: first call must carry head and base: $(cat "$DELEGATE_ARGS")"
sed -n 2p "$DELEGATE_ARGS" | grep -q -- "--gate-head $BASESHA --gate-base $BASESHA" || fail "docker gate: the base check must verify the base alone: $(cat "$DELEGATE_ARGS")"
grep -q 'ghp_should_not_leak' "$DELEGATE_ARGS" && fail "docker gate: a GitHub token reached the delegate argv"
grep -q '^pr merge' "$GH_LOG" && fail "a failing verify must not merge"
teardown

# base fails too (environment problem) -> exit 9 with the config hint
docker_gate_setup '20 20'
RC=0; OUT=$(dgate 2>&1) || RC=$?
[ "$RC" -eq 9 ] || fail "docker gate: a failure on the base too must exit 9, got $RC: $OUT"
echo "$OUT" | grep -q "verify fails on staging too in the scrubbed environment" || fail "docker gate: missing the base-fails line: $OUT"
echo "$OUT" | grep -q "pilot.verify_env_keep" || fail "docker gate: missing the config hint: $OUT"
[ -e "$HOSTMARK" ] && fail "docker gate: a verify command ran on the host (base check)"
grep -q '^pr merge' "$GH_LOG" && fail "a failing verify must not merge"
teardown

# green: no base run is needed and the gate carries on to the merge
docker_gate_setup '0'
RC=0; OUT=$(dgate 2>&1) || RC=$?
echo "$OUT" | grep -q "verified green" || fail "docker gate: a passing verify must reach 'verified green' (rc=$RC): $OUT"
[ "$(wc -l < "$DELEGATE_ARGS")" -eq 1 ] || fail "docker gate: a green verify runs once: $(cat "$DELEGATE_ARGS")"
teardown

# provider host: the old path, unchanged. The marker is made by the host clone, and docker is never called.
docker_gate_setup '' host
RC=0; OUT=$(dgate 2>&1) || RC=$?
[ "$RC" -eq 2 ] || fail "provider host: a PR-only failure must exit 2, got $RC: $OUT"
echo "$OUT" | grep -q "in a scrubbed clean clone" || fail "provider host keeps the scrubbed-clone message: $OUT"
[ -e "$HOSTMARK" ] || fail "provider host: the verify must run in the scrubbed host clone"
[ ! -s "$DLOG" ] || fail "provider host must not call docker: $(cat "$DLOG")"
teardown

# 16k — H2: provider docker never runs a migrate command on the host. The command is written by the PR's project; here it
#       would touch a marker. The gate exits 8 (needs-you) with the command listed and the marker stays absent.
docker_gate_setup '0'
cfgjq --arg mig "touch $TMP/ran-migration" '.migrations = {allowed_envs: ["staging"], target_env: "staging", commands: {staging: $mig}}'
RC=0; OUT=$(STUB_META="$(meta "" supabase/migrations/1.sql)" dgate 2>&1) || RC=$?
[ "$RC" -eq 8 ] || fail "docker gate: a migration must exit 8 (needs-you), got $RC: $OUT"
echo "$OUT" | grep -q "auto-migrations are disabled in docker mode; run them yourself" || fail "docker gate: missing the disabled message: $OUT"
echo "$OUT" | grep -q "^needs-you: touch $TMP/ran-migration" || fail "docker gate: the exact command must be listed: $OUT"
[ ! -e "$TMP/ran-migration" ] || fail "docker gate: the migrate command ran on the host"
grep -q '^pr merge' "$GH_LOG" && fail "docker gate: a PR with migrations must not merge"
teardown

# 16l — M1: git calls on the PR's tree run with hooks off. This is the UNGUARDED path (no sandbox section), the one that still
#       makes a scratch worktree. The repo config points core.hooksPath at a post-checkout hook that writes a marker; a plain
#       `git worktree add` would run it. The git log must show the worktree WAS added, or the test proves nothing.
shim_setup() {  # after head_setup: record every git and python3 argv, then run the real one
  REALGIT=$(command -v git); GIT_LOG="$TMP/git.log"; : > "$GIT_LOG"
  REALPY=$(command -v python3); PY_LOG="$TMP/py.log"; : > "$PY_LOG"
  mkdir -p "$TMP/shim"
  cat > "$TMP/shim/git" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$GIT_LOG"
case " \$* " in *" merge-tree "*)
  if [ -f "$TMP/mt-fail" ]; then
    case "\$(cat "$TMP/mt-fail")" in
      128) echo "fatal: stub merge-tree failure" >&2; exit 128 ;;
      empty1) echo "0123456789abcdef0123456789abcdef01234567"; exit 1 ;;
    esac
  fi ;;
esac
exec "$REALGIT" "\$@"
SHIM
  cat > "$TMP/shim/python3" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$PY_LOG"
exec "$REALPY" "\$@"
SHIM
  chmod +x "$TMP/shim/git" "$TMP/shim/python3"
}
head_setup
shim_setup
mkdir -p "$TMP/evilhooks"
printf '#!/bin/sh
echo ran > "%s/hook-ran"
' "$TMP" > "$TMP/evilhooks/post-checkout"
chmod +x "$TMP/evilhooks/post-checkout"
git -C "$TMP" config core.hooksPath "$TMP/evilhooks"
RC=0; OUT=$(STUB_OID="$SHA" SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$TMP/halt.json" PATH="$TMP/shim:$TMP/bin:$PATH" "$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --expect-head "$SHA" 2>&1) || RC=$?
echo "$OUT" | grep -q "verified green" || fail "hook test: the gate should reach 'verified green' (rc=$RC): $OUT"
grep -Eq '(^| )worktree add( |$)' "$GIT_LOG" || fail "hook test: the unguarded path must add a worktree, or this proves nothing: $(cat "$GIT_LOG")"
[ ! -e "$TMP/hook-ran" ] || fail "the gate ran a git hook on the PR's tree"
teardown

# 16m — a jq that cannot read the provider must NEVER read as "no sandbox": the gate exits 7 (a human merges) before it
#       verifies or migrates anything. The stub jq fails only for the pilot.sandbox read; with the old `|| true` the provider
#       came back empty and the migrate command was eval'd on the host with the database credentials.
docker_gate_setup '0'
cfgjq --arg mig "touch $TMP/ran-migration" '.migrations = {allowed_envs: ["staging"], target_env: "staging", commands: {staging: $mig}}'
REALJQ=$(command -v jq)
cat > "$TMP/bin/jq" <<STUB
#!/usr/bin/env bash
case "\$*" in *sandbox*) echo "jq: Invalid argument" >&2; exit 126 ;; esac
exec "$REALJQ" "\$@"
STUB
chmod +x "$TMP/bin/jq"
RC=0; OUT=$(STUB_META="$(meta "" supabase/migrations/1.sql)" dgate 2>&1) || RC=$?
[ "$RC" -eq 7 ] || fail "docker gate: an unreadable provider must exit 7, got $RC: $OUT"
echo "$OUT" | grep -q "could not read pilot.sandbox.provider" || fail "docker gate: missing the unreadable-provider message: $OUT"
[ ! -e "$TMP/ran-migration" ] || fail "docker gate: a broken jq let the migrate command run on the host"
grep -q '^pr merge' "$GH_LOG" && fail "docker gate: an unreadable provider must not merge"
teardown

# 16n — F1: a guarded repo (provider docker, where lenovo) never puts the PR in a host working tree. A recording `git` shim sits
#       first on PATH (and so also sees the calls super-board-git.sh makes); its log must hold merge-tree but no worktree add,
#       checkout, switch or bare merge. A stub lane-delegate stands in for the runner.
sgate() {  # the gate on the copied scripts (stub delegate) with the recording git shim first on PATH
  STUB_OID="$SHA" SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$TMP/halt.json" PATH="$TMP/shim:$TMP/bin:$PATH"     "$TMP/gs/super-board-merge-gate.sh" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --expect-head "$SHA" "$@"
}
tree_touching_git() {  # the log lines that would put PR content into a working tree
  awk '
    /(^| )(worktree add|checkout|checkout-index|switch|merge|restore|stash|reset --hard)( |$)/ { print; next }
    /(^| )read-tree( |.* )-[a-zA-Z]*u/ { print; next }
    /(^| )apply( |$)/ && !/--cached/ { print }
  ' "$GIT_LOG"
}
guarded_setup() {  # $1 = sandbox.where, $2 = delegate exit codes
  head_setup
  cfgjq --arg where "$1" '.pilot.sandbox = {provider: "docker", where: $where, runner_repo: "o/r", repos: ["x/y"]}'
  install_delegate_stub "$2"
  shim_setup
  git -C "$TMP" fetch -q origin staging
  BASESHA=$(git -C "$TMP" rev-parse origin/staging)
}

# clean merge, verify green
guarded_setup lenovo 0
RC=0; OUT=$(sgate --dry-run 2>&1) || RC=$?
[ "$RC" -eq 0 ] || fail "guarded gate: a clean green run must exit 0 on --dry-run, got $RC: $OUT"
echo "$OUT" | grep -q "would squash-merge" || fail "guarded gate: must reach the merge step: $OUT"
grep -q 'merge-tree' "$GIT_LOG" || fail "guarded gate: conflicts must be checked with merge-tree: $(cat "$GIT_LOG")"
[ -z "$(tree_touching_git)" ] || fail "guarded gate: a git call put the PR in a working tree: $(tree_touching_git)"
grep -q -- "--gate-head $SHA --gate-base $BASESHA" "$DELEGATE_ARGS" || fail "guarded gate: the delegate must get --gate-head <head> --gate-base <base>: $(cat "$DELEGATE_ARGS")"
grep -q -- "--dir $TMP " "$DELEGATE_ARGS" || fail "guarded gate: --dir must be the main checkout: $(cat "$DELEGATE_ARGS")"
teardown

# conflicting head: exit 5, the file is named, nothing is checked out
head_setup
git -C "$TMP" checkout -q feat; echo B > "$TMP/clash.txt"; git -C "$TMP" add clash.txt
git -C "$TMP" -c user.email=t@t -c user.name=t commit -q -m feat-clash; git -C "$TMP" push -q origin feat
git -C "$TMP" checkout -q staging; echo A > "$TMP/clash.txt"; git -C "$TMP" add clash.txt
git -C "$TMP" -c user.email=t@t -c user.name=t commit -q -m base-clash; git -C "$TMP" push -q origin staging
SHA=$(git -C "$TMP" rev-parse feat)
cfgjq '.pilot.sandbox = {provider: "docker", where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
install_delegate_stub 0
shim_setup
RC=0; OUT=$(sgate 2>&1) || RC=$?
[ "$RC" -eq 5 ] || fail "guarded gate: a conflict must exit 5, got $RC: $OUT"
echo "$OUT" | grep -q "conflicts: clash.txt" || fail "guarded gate: the conflict message must name the file: $OUT"
echo "$OUT" | grep -q "this is a rebase pass for the Builder lane, not a Blocked card" || fail "guarded gate: missing the rebase message: $OUT"
[ -z "$(tree_touching_git)" ] || fail "guarded gate: a conflict check put the PR in a working tree: $(tree_touching_git)"
[ ! -s "$DELEGATE_ARGS" ] || fail "guarded gate: a conflict must not reach the delegate: $(cat "$DELEGATE_ARGS")"
grep -q '^pr merge' "$GH_LOG" && fail "guarded gate: a conflict must not merge"
teardown

# where local: the Lenovo-only refusal, exit 9, no worktree
guarded_setup local 0
RC=0; OUT=$(sgate 2>&1) || RC=$?
[ "$RC" -eq 9 ] || fail "guarded gate: where local must exit 9, got $RC: $OUT"
echo "$OUT" | grep -q "guarded repo (pilot.sandbox.provider docker): the gate verifies on the Lenovo only (pilot.sandbox.where lenovo)" || fail "guarded gate: missing the Lenovo-only message: $OUT"
[ -z "$(tree_touching_git)" ] || fail "guarded gate: where local created a working tree: $(tree_touching_git)"
[ ! -s "$DELEGATE_ARGS" ] || fail "guarded gate: where local must not verify anywhere: $(cat "$DELEGATE_ARGS")"
grep -q '^pr merge' "$GH_LOG" && fail "guarded gate: where local must not merge"
teardown

# verify 20, base 0: exit 2; the base check verifies the base alone and creates no worktree
guarded_setup lenovo '20 0'
RC=0; OUT=$(sgate 2>&1) || RC=$?
[ "$RC" -eq 2 ] || fail "guarded gate: verify 20 then base 0 must exit 2, got $RC: $OUT"
[ "$(wc -l < "$DELEGATE_ARGS")" -eq 2 ] || fail "guarded gate: two delegate calls expected: $(cat "$DELEGATE_ARGS")"
sed -n 2p "$DELEGATE_ARGS" | grep -q -- "--gate-head $BASESHA --gate-base $BASESHA" || fail "guarded gate: the base check must pass BASE as head and base: $(cat "$DELEGATE_ARGS")"
[ -z "$(tree_touching_git)" ] || fail "guarded gate: the base check created a working tree: $(tree_touching_git)"
teardown

# verify 20, base 20: exit 9
guarded_setup lenovo '20 20'
RC=0; OUT=$(sgate 2>&1) || RC=$?
[ "$RC" -eq 9 ] || fail "guarded gate: verify 20 then base 20 must exit 9, got $RC: $OUT"
sed -n 2p "$DELEGATE_ARGS" | grep -q -- "--gate-head $BASESHA --gate-base $BASESHA" || fail "guarded gate: the base check must pass BASE as head and base: $(cat "$DELEGATE_ARGS")"
[ -z "$(tree_touching_git)" ] || fail "guarded gate: the base check created a working tree: $(tree_touching_git)"
teardown

# 16o — the unguarded path is untouched: with no sandbox section the gate still builds a scratch worktree.
head_setup
shim_setup
RC=0; OUT=$(STUB_OID="$SHA" SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$TMP/halt.json" PATH="$TMP/shim:$TMP/bin:$PATH"   "$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 0 ] || fail "unguarded gate: a clean run must merge, got $RC: $OUT"
grep -Eq '(^| )worktree add( |$)' "$GIT_LOG" || fail "unguarded gate: the old path must still use a scratch worktree: $(cat "$GIT_LOG")"
teardown

# 16p — tree_touching_git itself must flag every git call that writes a working tree, and none of the object-only ones.
TMP=$(mktemp -d); GIT_LOG="$TMP/git.log"
printf '%s\n' "-C r checkout-index -a" "-C r restore ." "-C r reset --hard abc" "-C r stash push" "-C r apply fix.patch" \
  "-C r read-tree -um a b" "-C r read-tree -u a" "-C r worktree add --detach d abc" "-C r switch x" "-C r merge origin/x" "-C r checkout abc" > "$GIT_LOG"
[ "$(tree_touching_git | wc -l)" -eq 11 ] || fail "tree_touching_git must flag all 11 tree-writing calls, flagged: $(tree_touching_git)"
printf '%s\n' "-C r merge-tree --write-tree a b" "-C r apply --cached fix.patch" "-C r read-tree --index-output=f a" "-C r diff --name-only" \
  "-C r fetch origin x" "-C r rev-parse origin/x^{commit}" > "$GIT_LOG"
[ -z "$(tree_touching_git)" ] || fail "tree_touching_git flagged an object-only call: $(tree_touching_git)"
rm -rf "$TMP"

# 16q — the provider is an ALLOWLIST: docker, host, none (a sandbox object without a provider) or absent. Anything else -- a
#       non-object sandbox, a case or whitespace variant, a non-string, an unknown tool -- is exit 7 (a human merges), before
#       any worktree is made and before the delegate is called.
refused_provider() {  # $1 = label, $2 = jq filter that sets pilot.sandbox
  head_setup
  cfgjq "$2"
  install_delegate_stub 0
  shim_setup
  RC=0; OUT=$(sgate 2>&1) || RC=$?
  [ "$RC" -eq 7 ] || fail "provider $1: must exit 7, got $RC: $OUT"
  echo "$OUT" | grep -q "pilot.sandbox.provider is not docker or host" || fail "provider $1: missing the policy message: $OUT"
  grep -Eq '(^| )worktree add( |$)' "$GIT_LOG" && fail "provider $1: made a worktree: $(cat "$GIT_LOG")"
  [ -z "$(tree_touching_git)" ] || fail "provider $1: a git call touched a working tree: $(tree_touching_git)"
  [ ! -s "$DELEGATE_ARGS" ] || fail "provider $1: the delegate was called: $(cat "$DELEGATE_ARGS")"
  grep -q '^pr merge' "$GH_LOG" && fail "provider $1: must not merge"
  teardown
}
refused_provider "string sandbox" '.pilot.sandbox = "docker"'
refused_provider "Docker" '.pilot.sandbox = {provider: "Docker", where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
refused_provider "docker+space" '.pilot.sandbox = {provider: "docker ", where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
refused_provider "docker+newline" '.pilot.sandbox = {provider: "docker\n", where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
refused_provider "true" '.pilot.sandbox = {provider: true, where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
refused_provider "podman" '.pilot.sandbox = {provider: "podman", where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
refused_provider "literal absent" '.pilot.sandbox = {provider: "absent", where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
refused_provider "literal none" '.pilot.sandbox = {provider: "none", where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
refused_provider "null provider" '.pilot.sandbox = {provider: null, where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
refused_provider "cyrillic o" '.pilot.sandbox = {provider: "dоcker", where: "lenovo", runner_repo: "o/r", repos: ["x/y"]}'
refused_provider "sandbox false" '.pilot.sandbox = false'
refused_provider "sandbox zero" '.pilot.sandbox = 0'
refused_provider "sandbox empty string" '.pilot.sandbox = ""'
# A sandbox object with no provider but with docker-only keys is a half-written guarded config, never the unguarded host path.
refused_provider "no provider, where+runner_repo" '.pilot.sandbox = {where: "lenovo", runner_repo: "o/r"}'
refused_provider "no provider, image" '.pilot.sandbox = {image: ("mc-lane-smoke@sha256:" + ("a" * 64))}'
refused_provider "no provider, gate_image" '.pilot.sandbox = {gate_image: ("mc-lane-gate@sha256:" + ("a" * 64))}'
refused_provider "no provider, proxy_image" '.pilot.sandbox = {proxy_image: ("mc-lane-proxy@sha256:" + ("b" * 64))}'

# 16q2 — a sandbox object with only unrelated keys (and null / a missing key) keeps the old unguarded path: a worktree is made.
for filter in '.pilot.sandbox = {note: "x"}' '.pilot.sandbox = null' 'del(.pilot.sandbox)'; do
  head_setup
  cfgjq "$filter"
  shim_setup
  RC=0; OUT=$(STUB_OID="$SHA" SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$TMP/halt.json" PATH="$TMP/shim:$TMP/bin:$PATH" "$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --expect-head "$SHA" 2>&1) || RC=$?
  [ "$RC" -eq 0 ] || fail "sandbox $filter: must take the old path and merge, got $RC: $OUT"
  grep -Eq '(^| )worktree add( |$)' "$GIT_LOG" || fail "sandbox $filter: the old path must use a scratch worktree: $(cat "$GIT_LOG")"
  teardown
done

# 16q3 — `none` (a sandbox object without a provider) never evals a migrate command: that is host-only.
head_setup
cfgjq --arg mig "touch $TMP/ran-migration" '.pilot.sandbox = {note: "x"} | .migrations = {allowed_envs: ["staging"], target_env: "staging", commands: {staging: $mig}}'
shim_setup
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$(meta "" supabase/migrations/1.sql)" SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$TMP/halt.json" PATH="$TMP/shim:$TMP/bin:$PATH" "$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 8 ] || fail "provider none: a migration must exit 8 (needs-you), got $RC: $OUT"
[ ! -e "$TMP/ran-migration" ] || fail "provider none: the migrate command ran on the host"
grep -q '^pr merge' "$GH_LOG" && fail "provider none: a PR with migrations must not merge"
teardown

# 16r — merge-tree diagnostics. A merge-tree that cannot run (exit 128: unrelated histories, a missing object) is exit 9 with its
#       stderr tail shown. A conflict exit (1) that names no path is also exit 9, never the rebase pass (5).
guarded_setup lenovo 0
echo 128 > "$TMP/mt-fail"
RC=0; OUT=$(sgate 2>&1) || RC=$?
[ "$RC" -eq 9 ] || fail "merge-tree exit 128 must exit 9, got $RC: $OUT"
echo "$OUT" | grep -q "stub merge-tree failure" || fail "merge-tree exit 128: its stderr tail must be shown: $OUT"
[ ! -s "$DELEGATE_ARGS" ] || fail "merge-tree exit 128 must not reach the delegate"
teardown
guarded_setup lenovo 0
echo empty1 > "$TMP/mt-fail"
RC=0; OUT=$(sgate 2>&1) || RC=$?
[ "$RC" -eq 9 ] || fail "merge-tree exit 1 with no conflicted path must exit 9, got $RC: $OUT"
echo "$OUT" | grep -q "rebase pass" && fail "merge-tree exit 1 with no conflicted path is not a rebase pass: $OUT"
teardown

# 16s — the REAL lane-delegate accepts the guarded argv (--dir, --gate-head, --gate-base) and no token rides in it. Only the remote
#       dispatch is stubbed: MC_LANE_GH_CMD points the delegate at a gh that logs a line and fails, so the run ends in exit 70.
head_setup
shim_setup
git -C "$TMP" fetch -q origin staging
BASESHA=$(git -C "$TMP" rev-parse origin/staging)
cfgjq '.pilot.sandbox = {provider: "docker", where: "lenovo", runner_repo: "o/r", repos: ["x/y"],
         image: ("mc-lane-smoke@sha256:" + ("a" * 64)), proxy_image: ("mc-lane-proxy@sha256:" + ("b" * 64))}
       | .pilot.tools = [{id: "fake", via: "fake", model: "fake-1", command: ["true"]}]'
GHLANE="$TMP/gh-lane.log"; : > "$GHLANE"
GHCMD=$(jq -cn --arg log "$GHLANE" '["sh", "-c", "echo gh-called >> \($log); exit 1", "gh"]')
RC=0; OUT=$(MC_LANE_GH_CMD="$GHCMD" GH_TOKEN=ghp_should_not_leak GITHUB_TOKEN=ghp_should_not_leak STUB_OID="$SHA" SB_GITHUB_RETRY_DELAY=0 \
  SB_GITHUB_HALT_FILE="$TMP/halt.json" PATH="$TMP/shim:$TMP/bin:$PATH" "$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --expect-head "$SHA" 2>&1) || RC=$?
echo "$OUT" | grep -q "unrecognized arguments" && fail "the real delegate refused the guarded argv: $OUT"
echo "$OUT" | grep -q "lane-delegate exit 70" || fail "the real delegate should get as far as the stubbed dispatch (exit 70): $OUT"
DLINE=$(grep 'super-board-lane-delegate.py exec' "$PY_LOG" | head -1)
[ -n "$DLINE" ] || fail "the gate never ran the real delegate: $(cat "$PY_LOG")"
case "$DLINE" in *"--gate-head $SHA --gate-base $BASESHA"*) : ;; *) fail "delegate argv lacks --gate-head/--gate-base: $DLINE" ;; esac
case "$DLINE" in *"--dir $TMP "*) : ;; *) fail "delegate argv lacks --dir with the main checkout: $DLINE" ;; esac
case "$DLINE" in *ghp_should_not_leak*) fail "a GitHub token reached the delegate argv: $DLINE" ;; esac
[ -s "$GHLANE" ] || fail "the delegate never reached the stubbed gh dispatch"
teardown

# 16t — the config race: the gate read provider docker, then the file flips to host before the REAL delegate reads it. The delegate
#       must refuse --gate-head/--gate-base (64), so the gate exits 9 and never merges a PR that was not verified. The python3
#       shim rewrites the provider just before it hands over to the real python3.
head_setup
shim_setup
cfgjq '.pilot.sandbox = {provider: "docker", where: "lenovo", runner_repo: "o/r", repos: ["x/y"],
         image: ("mc-lane-smoke@sha256:" + ("a" * 64)), proxy_image: ("mc-lane-proxy@sha256:" + ("b" * 64))}
       | .pilot.tools = [{id: "fake", via: "fake", model: "fake-1", command: ["true"]}]'
REALJQ=$(command -v jq)
cat > "$TMP/shim/python3" <<SHIM
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$PY_LOG"
case "\$*" in *lane-delegate.py*)
  "$REALJQ" '.pilot.sandbox.provider = "host"' "$TMP/c.json" > "$TMP/c-race.json" && mv "$TMP/c-race.json" "$TMP/c.json" ;;
esac
exec "$REALPY" "\$@"
SHIM
chmod +x "$TMP/shim/python3"
GHLANE="$TMP/gh-lane.log"; : > "$GHLANE"
GHCMD=$(jq -cn --arg log "$GHLANE" '["sh", "-c", "echo gh-called >> \($log); exit 1", "gh"]')
RC=0; OUT=$(MC_LANE_GH_CMD="$GHCMD" STUB_OID="$SHA" SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$TMP/halt.json" \
  PATH="$TMP/shim:$TMP/bin:$PATH" "$GATE" --config "$TMP/c.json" --pr 1 --lock-timeout 6 --expect-head "$SHA" 2>&1) || RC=$?
[ "$RC" -eq 9 ] || fail "config race: a provider flipped to host under the gate must exit 9, got $RC: $OUT"
echo "$OUT" | grep -q "lane-delegate exit 64" || fail "config race: the delegate must refuse with 64: $OUT"
echo "$OUT" | grep -q "need pilot.sandbox.provider docker" || fail "config race: missing the delegate message: $OUT"
grep -q '^pr merge' "$GH_LOG" && fail "config race: must not merge"
[ ! -s "$GHLANE" ] || fail "config race: nothing may be dispatched"
teardown

# 17 — custom categories replace the defaults: with always_human = {} a money
#      label merges.
head_setup
cfgset '.merge_policy = {always_human: {}}'
RC=0; STUB_OID="$SHA" STUB_META="$(meta money src/x.ts)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "always_human {} should let a money label merge, got $RC"
teardown

# 18 — a migration whose target env is allowed: the gate runs the configured
#      command for each allowed env, then merges.
head_setup
cfgset ".migrations = {allowed_envs: [\"test\",\"staging\"], target_env: \"staging\",
        commands: {test: \"touch $TMP/ran-test\", staging: \"touch $TMP/ran-staging\"}}"
RC=0; STUB_OID="$SHA" STUB_META="$(meta "" supabase/migrations/001_add.sql)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "an allowed migration should merge, got $RC"
[ -f "$TMP/ran-test" ] && [ -f "$TMP/ran-staging" ] || fail "both allowed migrate commands should have run"
teardown

# 19 — the target env is NOT allowed (live): 🙋 exit 8 with the exact command,
#      no merge. The allowed env still ran, so the human has one step left.
head_setup
cfgset ".migrations = {allowed_envs: [\"staging\"], target_env: \"live\",
        commands: {staging: \"touch $TMP/ran-staging\", live: \"npm run db:migrate:live\"}}"
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$(meta "" prisma/migrations/2026/migration.sql)" gate --expect-head "$SHA" 2>/dev/null) || RC=$?
[ "$RC" -eq 8 ] || fail "a migration for a disallowed env should exit 8, got $RC"
echo "$OUT" | grep -q "^needs-you: npm run db:migrate:live" || fail "exit 8 must print the exact command, got: $OUT"
[ -f "$TMP/ran-staging" ] || fail "the allowed env should still migrate"
grep -q '^pr merge' "$GH_LOG" && fail "no merge while a human step is open"
teardown

# 20 — a UI label cannot confirm that human-only steps ran.
head_setup
cfgset ".migrations = {allowed_envs: [\"staging\"], target_env: \"live\",
        commands: {staging: \"true\", live: \"npm run db:migrate:live\"}}"
RC=0; STUB_OID="$SHA" STUB_META="$(meta needs-you:done prisma/migrations/2026/migration.sql)" gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 8 ] || fail "a needs-you:done label alone must not confirm human steps, got $RC"
teardown

# 20b — layer B: a jq that cannot start while the gate reads its merge plan (seen
#       under load on Windows: exit 126/31) must never read as "no human steps".
#       Before the fix, process substitution swallowed it, NEEDS came out empty
#       and the PR merged past a needs-you step. Each plan read must fail closed.
for filter in '.needs_you[]' '[.needs_you[] | select(length>0)] | length' '.migrations | length' '.migrations | join(" ")' '.human[] | "human-gate: \(.category) — \(.why)"' '.run[] | [.env, .cmd] | @tsv'; do
  head_setup
  cfgset ".migrations = {allowed_envs: [\"staging\"], target_env: \"live\",
          commands: {staging: \"true\", live: \"npm run db:migrate:live\"}}"
  REAL_JQ=$(command -v jq)
  cat > "$TMP/bin/jq" <<SHIM
#!/usr/bin/env bash
for a in "\$@"; do [ "\$a" = "\$FAIL_JQ_FILTER" ] && exit 126; done
exec "$REAL_JQ" "\$@"
SHIM
  chmod +x "$TMP/bin/jq"
  RC=0; OUT=$(FAIL_JQ_FILTER="$filter" STUB_OID="$SHA" STUB_META="$(meta needs-you:done prisma/migrations/2026/migration.sql)" gate --expect-head "$SHA" 2>&1) || RC=$?
  [ "$RC" -eq 7 ] || fail "an unreadable plan ($filter) must hold for a human (7), got $RC: $OUT"
  grep -q '^pr merge' "$GH_LOG" && fail "an unreadable plan ($filter) must never merge"
  teardown
done

# 20c — fix 4: an empty or multi-line needs-you entry is flattened/dropped by the
#       policy, so the gate's count check does not fail closed (7) on it; the human
#       step still blocks with 8 and prints as one line.
head_setup
cfgset '.migrations = {allowed_envs: ["staging"], target_env: "staging", commands: {staging: "true"}, human_steps: ["", "echo a
echo b"]}'
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$(meta "" drizzle/0001.sql)" gate --expect-head "$SHA" 2>/dev/null) || RC=$?
[ "$RC" -eq 8 ] || fail "an empty/multi-line human step must still hold for a human (8), got $RC: $OUT"
echo "$OUT" | grep -qx "needs-you: echo a echo b" || fail "multi-line step must be one flattened line, got: $OUT"
[ "$(echo "$OUT" | grep -c '^needs-you:')" -eq 1 ] || fail "the empty entry must be dropped, got: $OUT"
teardown

# 21 — an allowed migrate command that FAILS is a needs-you, and needs-you:done
#      does not excuse a command that is failing right now.
head_setup
cfgset '.migrations = {allowed_envs: ["staging"], target_env: "staging", commands: {staging: "exit 3"}}'
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$(meta needs-you:done drizzle/0001.sql)" gate --expect-head "$SHA" 2>/dev/null) || RC=$?
[ "$RC" -eq 8 ] || fail "a failing migrate command should exit 8, got $RC"
echo "$OUT" | grep -q "staging: failed in the merge gate" || fail "the failure must be named, got: $OUT"
teardown

# 22 — a human-only step declared in the PR body, on a PR with no migrations.
head_setup
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$(meta "" src/x.ts 'Notes
needs-you: `npm run backfill -- --env live`')" gate --expect-head "$SHA" 2>/dev/null) || RC=$?
[ "$RC" -eq 8 ] || fail "a needs-you: line in the body should exit 8, got $RC"
echo "$OUT" | grep -q "^needs-you: npm run backfill -- --env live" || fail "the declared step must be printed, got: $OUT"
teardown

# 23 — metadata the gate cannot read fails safe to a human (7), never to merge.
head_setup
RC=0; STUB_OID="$SHA" STUB_META='not json' gate --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 79 ] || fail "unreadable PR metadata should halt (79), got $RC"
teardown

# 24 — dry run: migrations are reported, never executed.
head_setup
cfgset ".migrations = {allowed_envs: [\"staging\"], target_env: \"staging\", commands: {staging: \"touch $TMP/ran\"}}"
RC=0; OUT=$(STUB_OID="$SHA" STUB_META="$(meta "" supabase/migrations/1.sql)" gate --expect-head "$SHA" --dry-run 2>&1) || RC=$?
[ "$RC" -eq 0 ] || fail "a dry run should exit 0, got $RC"
[ ! -f "$TMP/ran" ] || fail "a dry run must not run a migrate command"
echo "$OUT" | grep -q "would migrate staging" || fail "a dry run should say what it would migrate, got: $OUT"
teardown

# 25 — the glob dialect: **/ spans zero or more directories; * stays in one segment.
P="../scripts/super-board-merge-policy.py"
pol() { printf '%s' "$1" > "$T25/m.json"; python3 "$P" --config "$T25/c.json" --meta "$T25/m.json" --diff /dev/null; }
T25=$(mktemp -d); echo '{"base_branch":"staging","migrations":{"globs":["**/migrations/*.sql"]}}' > "$T25/c.json"
pol "$(meta "" migrations/1.sql)"          | jq -e '.migrations == ["migrations/1.sql"]' >/dev/null || fail "**/ should match zero directories"
pol "$(meta "" a/b/migrations/1.sql)"      | jq -e '.migrations | length == 1' >/dev/null || fail "**/ should match nested directories"
pol "$(meta "" migrations/old/1.sql)"      | jq -e '.migrations | length == 0' >/dev/null || fail "* must not cross a /"
rm -rf "$T25"

# Dry-run also reports missing verification before querying or merging a PR.
head_setup
jq '.verify_commands = ["", " "]' "$TMP/c.json" > "$TMP/invalid.json"
RC=0; STUB_OID="$SHA" "$GATE" --config "$TMP/invalid.json" --pr 1 --dry-run >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 9 ] || fail "dry-run must expose missing verification as setup, got $RC"
[ ! -s "$GH_LOG" ] || fail "unready setup must not call GitHub"
teardown

# A real configured command still runs; a failed project check blocks the merge.
head_setup
# (fails on the PR head, whose last commit is "feat", but passes on the base, so the PR is what broke it)
jq '.verify_commands = ["", " ", "test \"$(git log -1 --format=%s)\" != feat"]' "$TMP/c.json" > "$TMP/failing.json"
RC=0; STUB_OID="$SHA" "$GATE" --config "$TMP/failing.json" --pr 1 --expect-head "$SHA" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 2 ] || fail "failed configured project check must exit 2, got $RC"
grep -q '^pr merge' "$GH_LOG" && fail "failed check must not merge"
teardown

echo "PASS: test-merge-gate.sh (merge, policy and approval scenarios)"
