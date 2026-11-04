#!/usr/bin/env bash
# Tests scripts/super-board-pr-body.sh — one marker block rewritten, the rest
# untouched, nothing written when the PR head moved. `gh` is a stub on PATH.
#
#   bash tests/test-pr-body.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/super-board-pr-body.sh"
WORK="$(mktemp -d)"
export SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$WORK/halt.json"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  ❌ %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; }
is()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }
has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "contains: $3" "$2" ;; esac; }
hasnt() { case "$2" in *"$3"*) bad "$1" "no '$3'" "found it" ;; *) ok "$1" ;; esac; }

# gh stub (REST): GET pulls/N serves $WORK/body.md and $HEAD_OID; PATCH pulls/N --input - stores the new body.
cat > "$WORK/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
  "api -X PATCH repos/{owner}/{repo}/pulls/"*" --input -") jq -j .body > "$BODY" ;;
  "api repos/{owner}/{repo}/pulls/"*)
    jq -n --arg h "$HEAD_OID" --arg r "$HEAD_REF" --arg repo "$BASE_REPO" --rawfile b "$BODY" '{head: {sha: $h, ref: $r}, base: {repo: {full_name: $repo}}, body: $b}' | jq '{headRefOid: .head.sha, headRefName: .head.ref, baseRepo: .base.repo.full_name, body: (.body // "")}' ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
STUB
chmod +x "$WORK/gh"
export PATH="$WORK:$PATH" GH_LOG="$WORK/gh.log" BODY="$WORK/body.md" HEAD_OID="3f9c2a1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" HEAD_REF="issue-7-fix-x" BASE_REPO="acme/app"

cat > "$BODY" <<'MD'
<!-- sb:status -->
> [!NOTE]
> ⏳ In QA · ✅ 0/2 AC · Closes #7 · head `3f9c2a1`
<!-- /sb:status -->

<!-- sb:problem -->
## Problem
- **Where:** `/x`
<!-- /sb:problem -->

<!-- sb:history -->
## Iteration history
| Lane | Done | Time | Details |
|---|---|---|---|
| 🔨 builder | ✅ | Oct 2, 10:02 EDT | draft |
<!-- /sb:history -->
MD

printf '> [!TIP]\n> ✅ Merged · `9c41e07`\n' > "$WORK/status.md"
printf '## Risk\n🟢 **Low** · one constant.\n' > "$WORK/risk.md"
printf '| 🔍 qa | ✅ v1 | Oct 2, 11:55 EDT | runs/issue-7-qa-v1/ |\n' > "$WORK/row.md"

run() { : > "$GH_LOG"; "$SCRIPT" "$@" 2>"$WORK/err"; }

echo "── replace one block, keep the rest"
is  "replace reports updated" "updated" "$(run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/status.md")"
has "new status written"      "$(cat "$BODY")" "✅ Merged"
hasnt "old status gone"       "$(cat "$BODY")" "In QA"
has "problem block untouched" "$(cat "$BODY")" '- **Where:** `/x`'

echo "── idempotent"
is  "same content twice is a no-op" "unchanged" "$(run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/status.md")"
hasnt "no edit call on a no-op"     "$(cat "$GH_LOG")" "PATCH"

echo "── missing block inserted in order"
run --pr 7 --block risk --expect-head 3f9c2a1 --body-file "$WORK/risk.md" >/dev/null
has "risk inserted" "$(cat "$BODY")" "<!-- sb:risk -->"
H=$(grep -n 'sb:history -->' "$BODY" | head -1 | cut -d: -f1); R=$(grep -n '<!-- sb:risk -->' "$BODY" | cut -d: -f1)
[ "$R" -gt "$H" ] && ok "risk lands after history" || bad "risk lands after history" "line > $H" "$R"
printf '## Solution\n- one change\n' > "$WORK/sol.md"
run --pr 7 --block solution --expect-head 3f9c2a1 --body-file "$WORK/sol.md" >/dev/null
P=$(grep -n '<!-- /sb:problem -->' "$BODY" | cut -d: -f1); S=$(grep -n '<!-- sb:solution -->' "$BODY" | cut -d: -f1)
H=$(grep -n '<!-- sb:history -->' "$BODY" | cut -d: -f1)
[ "$S" -gt "$P" ] && [ "$S" -lt "$H" ] && ok "solution lands between problem and history" || bad "solution between problem and history" "$P < x < $H" "$S"

echo "── append a history row, once"
run --pr 7 --block history --expect-head 3f9c2a1 --append-file "$WORK/row.md" >/dev/null
has "row appended" "$(cat "$BODY")" "runs/issue-7-qa-v1/"
is  "re-append is a no-op" "unchanged" "$(run --pr 7 --block history --expect-head 3f9c2a1 --append-file "$WORK/row.md")"
is  "row appears once" "1" "$(grep -c 'runs/issue-7-qa-v1/' "$BODY")"

echo "── refuses when the head moved"
cp "$BODY" "$WORK/before.md"
RC=0; run --pr 7 --block status --expect-head deadbee --body-file "$WORK/risk.md" >/dev/null || RC=$?
is  "moved head exits 6" "6" "$RC"
hasnt "no edit on a moved head" "$(cat "$GH_LOG")" "PATCH"
is  "body unchanged" "$(cat "$WORK/before.md")" "$(cat "$BODY")"

echo "── status block must close the PR's own card"
# Golden eval 20261031T005927Z: two parallel QA lanes wrote the same scratch
# status.md; card #224's PR ended up saying "Closes #221".
cp "$BODY" "$WORK/before.md"
printf '> [!NOTE]\n> ⏳ Draft · QA passed · Closes #221 · head `716eb21`\n' > "$WORK/other.md"
RC=0; run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/other.md" >/dev/null || RC=$?
is  "foreign Closes # exits 65" "65" "$RC"
has "refusal names both issues" "$(cat "$WORK/err")" "#221"
has "refusal names the card"    "$(cat "$WORK/err")" "#7"
hasnt "no edit on a foreign Closes #" "$(cat "$GH_LOG")" "PATCH"
is  "body unchanged on a foreign Closes #" "$(cat "$WORK/before.md")" "$(cat "$BODY")"
printf '> [!NOTE]\n> ⏳ Partial · Refs #221 · head `3f9c2a1`\n' > "$WORK/other.md"
RC=0; run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/other.md" >/dev/null || RC=$?
is  "foreign Refs # exits 65" "65" "$RC"
printf '> [!NOTE]\n> ⏳ In review · Resolves #221 · head `3f9c2a1`\n' > "$WORK/other.md"
RC=0; run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/other.md" >/dev/null || RC=$?
is  "foreign Resolves # exits 65" "65" "$RC"
printf '> [!NOTE]\n> ⏳ In review · Closes #70 · head `3f9c2a1`\n' > "$WORK/other.md"
RC=0; run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/other.md" >/dev/null || RC=$?
is  "#70 is not card #7" "65" "$RC"
printf '> [!NOTE]\n> ⏳ In review · Closes #7 · head `3f9c2a1`\n' > "$WORK/own.md"
is  "own Closes # is written" "updated" "$(run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/own.md")"
is  "branch without an issue number is not checked" "updated" \
  "$(HEAD_REF="refine/some-slug" run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/other.md")"

# GitHub closes on a closing keyword ANYWHERE in the PR description, so every block is checked.
cp "$BODY" "$WORK/before.md"
chk() { # name block text
  printf '%s
' "$3" > "$WORK/x.md"
  RC=0; run --pr 7 --block "$2" --expect-head 3f9c2a1 --body-file "$WORK/x.md" >/dev/null || RC=$?
  is "$1" "65" "$RC"
  hasnt "$1: no PATCH" "$(cat "$GH_LOG")" "PATCH"
  is  "$1: body unchanged" "$(cat "$WORK/before.md")" "$(cat "$BODY")"
}
chk "(a) Fixes #221 in problem exits 65" problem "Fixes #221"
hasnt "(a) no PATCH" "$(cat "$GH_LOG")" "PATCH"
is  "(a) body unchanged" "$(cat "$WORK/before.md")" "$(cat "$BODY")"
chk "(b) closed #221 in history exits 65" history "closed #221"
chk "(c) Closes [#221] exits 65" status "Closes [#221]"
chk "(d) Closes acme/app#221 exits 65" status "Closes acme/app#221"
chk "(e) Closes issue URL exits 65" status "Closes https://github.com/acme/app/issues/221"
chk "(f) Closes other/repo#7 exits 65" status "Closes other/repo#7"
has "(f) message names the reference" "$(cat "$WORK/err")" "other/repo#7"
chk "(k) Closes:#221 with no space exits 65" status "Closes:#221"
chk "(l) http www issue URL exits 65" status "Closes http://www.github.com/acme/app/issues/221"
BASE_REPO="" chk "(m) a qualified ref with no base repo read exits 65" status "Closes acme/app#7"
printf 'Closes acme/app#7
' > "$WORK/x.md"
is  "(g) Closes acme/app#7 is written" "updated" "$(run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/x.md")"
is  "(n) issue-0007 branch: own Closes #7 is written" "unchanged" "$(HEAD_REF="issue-0007-fix-x" run --pr 7 --block status --expect-head 3f9c2a1 --body-file "$WORK/x.md")"
printf 'prefix #5 stays
' > "$WORK/x.md"
is  "(h) prefix #5 is not a closing word" "updated" "$(run --pr 7 --block solution --expect-head 3f9c2a1 --body-file "$WORK/x.md")"
printf 'Refs #221
' > "$WORK/x.md"
is  "(j) Refs #221 outside status is written" "updated" "$(run --pr 7 --block solution --expect-head 3f9c2a1 --body-file "$WORK/x.md")"
cp "$BODY" "$WORK/keep.md"
printf '
<!-- sb:ac -->
Fixes #221
<!-- /sb:ac -->
' >> "$BODY"
cp "$BODY" "$WORK/before.md"
chk "(i) foreign #221 already in the old ac block exits 65" risk "all clean"
is  "(i) body unchanged" "$(cat "$WORK/before.md")" "$(cat "$BODY")"
cp "$WORK/keep.md" "$BODY"

echo "── usage"
RC=0; run --pr 7 --block status --body-file "$WORK/risk.md" >/dev/null || RC=$?; is "expect-head required" 64 "$RC"
RC=0; run --pr 7 --block nope --expect-head x --body-file "$WORK/risk.md" >/dev/null || RC=$?; is "block is an enum" 64 "$RC"
RC=0; run --pr 7 --block ac --expect-head x >/dev/null || RC=$?; is "content file required" 64 "$RC"
OUT=$(run --pr 7 --block ac --dry-run --body-file "$WORK/risk.md"); has "dry run prints the body" "$OUT" "<!-- sb:ac -->"
hasnt "dry run edits nothing" "$(cat "$GH_LOG")" "PATCH"

echo "── skeleton"
SK=$("$SCRIPT" --skeleton)
for b in status problem solution ac history visual risk; do has "skeleton has $b" "$SK" "<!-- sb:$b -->"; done

echo "── time in config timezone"
echo '{"timezone":"UTC"}' > "$WORK/cfg.json"
has "time carries the zone" "$("$SCRIPT" --time --config "$WORK/cfg.json")" "UTC"

printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
