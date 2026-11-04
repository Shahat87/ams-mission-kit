#!/usr/bin/env bash
# Tests super-board-card.sh against a fake gh. No network.
#
# The point of the helper is cost: a card move used to be item-list (~203
# points on a 131-card board) + field-list (101) + item-edit. These tests pin
# the call count — a warm move is ONE mutation, a cold one is two 1-point reads
# plus the mutation — and that a stale option id is recovered, not mis-moved.
set -euo pipefail
cd "$(dirname "$0")"
CARD="$PWD/../scripts/super-board-card.sh"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }
pass=0; ok() { pass=$((pass + 1)); echo "  ✅ $1"; }

mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'EOF'
#!/usr/bin/env bash
# Fake gh: logs one line per call, answers by query shape.
q=""; for a in "$@"; do case "$a" in query=*) q="${a#query=}" ;; esac; done
kind=other
case "$q" in
  *mutation*) kind=mutation ;;
  *'name:"Agent"'*) kind=audit ;;
  *'body milestone'*) kind=enrich ;;
  *'fields(first:50)'*) kind=fields ;;
  *'field(name:"Status")'*) kind=ids ;;
  *issueOrPullRequest*) kind=card ;;
  *'items(first:100'*) kind=items ;;
esac
[ "${1:-}" = issue ] && kind="issue-${2:-}"
[ "${1:-}" = api ] && case "${2:-}" in repos/*/issues/*/comments) kind=comments ;; repos/*/pulls/*/commits) kind=commits ;; esac
[ "${1:-}" = pr ] && kind="pr-${2:-}"
[ "${1:-}" = repo ] && kind="repo-${2:-}"
[ "${1:-}" = label ] && kind="label-${2:-}"
{ printf '%s ' "$kind" "$@" | tr '\n' ' '; echo; } >> "$FAKE_LOG"
case "$kind" in
  issue-edit|issue-comment|label-create) exit 0 ;;
  issue-close) if [ -f "$FAKE_DIR/fail-close" ]; then echo "HTTP 502" >&2; exit 1; fi; exit 0 ;;
  issue-view) cat "$FAKE_DIR/issue-state.json" 2>/dev/null || echo '{"state":"OPEN"}' ;;
  issue-list) cat "$FAKE_DIR/issues-closed.json" ;;
  pr-view) cat "$FAKE_DIR/pr-$3.json" || exit 1 ;;
  repo-view) echo '{"defaultBranchRef":{"name":"main"}}' ;;
  label-list) cat "$FAKE_DIR/labels.json" 2>/dev/null || echo '[]' ;;
  commits) cat "$FAKE_DIR/commits-$(echo "$2" | sed -E 's#.*/pulls/([0-9]+)/commits#\1#').txt" ;;  # what `--jq .[].sha` prints
  comments) if [ -f "$FAKE_DIR/fail-comments" ]; then echo "HTTP 500" >&2; exit 1; fi
    n=$(echo "$2" | sed -E 's#.*/issues/([0-9]+)/comments#\1#')
    cat "$FAKE_DIR/comments-$n.json" 2>/dev/null || cat "$FAKE_DIR/comments.json" 2>/dev/null || echo '[]' ;;
  audit) cat "$FAKE_DIR/audit.json" ;;
  mutation)
    if [ -f "$FAKE_DIR/fail-mutation-once" ]; then rm -f "$FAKE_DIR/fail-mutation-once"
      echo 'GraphQL: The single select option Id does not belong to the field' >&2; exit 1; fi
    if [ -f "$FAKE_DIR/fail-field-mutation" ] && [[ "$q" == *a0:* ]]; then
      echo 'GraphQL: field write refused' >&2; exit 1; fi
    echo '{"data":{"updateProjectV2ItemFieldValue":{"projectV2Item":{"id":"PVTI_232"}}}}' ;;
  ids) echo '{"data":{"repositoryOwner":{"projectV2":{"id":"PVT_1","field":{"id":"PVTSSF_1","options":[{"id":"opt_ready","name":"Ready"},{"id":"opt_qa","name":"QA"}]}}}}}' ;;
  card)
    case "$*" in
      *issue=999*) echo '{"data":{"repository":{"issueOrPullRequest":{"projectItems":{"nodes":[{"id":"PVTI_other","project":{"number":7,"owner":{"login":"acme"}},"fieldValueByName":null}]}}}}}' ;;
      *) echo '{"data":{"repository":{"issueOrPullRequest":{"projectItems":{"nodes":[{"id":"PVTI_other","project":{"number":7,"owner":{"login":"acme"}},"fieldValueByName":null},{"id":"PVTI_232","project":{"number":15,"owner":{"login":"Acme"}},"fieldValueByName":{"name":"Building"}}]}}}}}' ;;
    esac ;;
  items) cat "$FAKE_DIR/items.json" ;;
  fields) if [ -f "$FAKE_DIR/fail-fields" ]; then echo "GraphQL: fields read refused" >&2; exit 1; fi; cat "$FAKE_DIR/fields.json" ;;
  enrich) cat "$FAKE_DIR/enrich.json" ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
EOF
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH" FAKE_LOG="$TMP/log" FAKE_DIR="$TMP" SB_CARD_CACHE_DIR="$TMP/cache" \
       SB_GITHUB_HALT_FILE="$TMP/halt.json" SB_GITHUB_RETRY_DELAY=0
card() { "$CARD" --owner acme --number 15 --repo acme/app "$@"; }
calls() { grep -c "^$1 " "$FAKE_LOG" 2>/dev/null || true; }
reset_log() { : > "$FAKE_LOG"; }

# 1 — cold move: ids read + card read + one mutation, with the right option.
reset_log
OUT=$(card move 232 Ready)
[ "$OUT" = "moved #232 → Ready" ] || fail "cold move output: $OUT"
[ "$(wc -l < "$FAKE_LOG" | tr -d ' ')" = 3 ] || fail "cold move should be 3 calls: $(cat "$FAKE_LOG")"
grep '^mutation' "$FAKE_LOG" | grep -q 'o=opt_ready' || fail "mutation used wrong option: $(cat "$FAKE_LOG")"
grep '^mutation' "$FAKE_LOG" | grep -q 'i=PVTI_232' || fail "picked the other project's item: $(cat "$FAKE_LOG")"
ok "cold move = 2 reads + 1 mutation, this board's item"

# 2 — warm move: ONE call, the mutation.
reset_log
card move 232 Ready >/dev/null
[ "$(wc -l < "$FAKE_LOG" | tr -d ' ')" = 1 ] && [ "$(calls mutation)" = 1 ] || fail "warm move should be 1 mutation: $(cat "$FAKE_LOG")"
ok "warm move = 1 mutation"

# 3 — the cache is shared state, kept out of git.
[ -f "$TMP/cache/board-acme-15.json" ] || fail "cache file missing"
[ "$(cat "$TMP/cache/.gitignore")" = '*' ] || fail "cache dir not self-ignored"
jq -e '.items["232"] == "PVTI_232" and .options.QA == "opt_qa"' "$TMP/cache/board-acme-15.json" >/dev/null || fail "cache content wrong"
ok "cache holds ids, ignores itself"

# 4 — a reminted option id: first mutation fails, ids re-read, retried once.
jq '.options.Ready = "opt_stale"' "$TMP/cache/board-acme-15.json" > "$TMP/c" && mv "$TMP/c" "$TMP/cache/board-acme-15.json"
touch "$TMP/fail-mutation-once"; reset_log
OUT=$(card move 232 Ready 2>/dev/null) || fail "stale-id move should recover"
[ "$(calls mutation)" = 2 ] && [ "$(calls ids)" = 1 ] || fail "stale recovery calls: $(cat "$FAKE_LOG")"
tail -1 "$FAKE_LOG" | grep -q 'o=opt_ready' || fail "retry still used the stale id"
ok "stale option id → one re-read, one retry"

# 5 — a column the board does not have: re-read once, then exit 3, no mutation.
reset_log; RC=0; card move 232 Shipping >/dev/null 2>&1 || RC=$?
[ "$RC" = 3 ] && [ "$(calls mutation)" = 0 ] || fail "unknown Status should exit 3 without mutating (rc=$RC)"
ok "unknown Status refused"

# 6 — status reads live (never cached) and prints the name.
reset_log
[ "$(card status 232)" = Building ] && [ "$(calls card)" = 1 ] || fail "status read wrong"
ok "status = 1 live read"

# 7 — an issue on another board only: exit 3.
RC=0; card item-id 999 >/dev/null 2>&1 || RC=$?
[ "$RC" = 3 ] || fail "issue off this board should exit 3 (rc=$RC)"
ok "issue not on this board → 3"

# 8 — items: gh item-list shape, the fields the wave planner reads.
cat > "$TMP/items.json" <<'EOF'
[{"data":{"repositoryOwner":{"projectV2":{"items":{"totalCount":3,"pageInfo":{"hasNextPage":true,"endCursor":"c1"},"nodes":[
  {"id":"I1","fieldValueByName":{"name":"Ready"},"content":{"__typename":"Issue","number":12,"title":"Twelve","url":"u12","repository":{"nameWithOwner":"acme/app"},"labels":{"nodes":[{"name":"qa"}]}}},
  {"id":"I2","fieldValueByName":null,"content":{"__typename":"DraftIssue","title":"Draft"}}]}}}}},
 {"data":{"repositoryOwner":{"projectV2":{"items":{"totalCount":3,"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[
  {"id":"I3","fieldValueByName":{"name":"Review"},"content":{"__typename":"PullRequest","number":40,"title":"Forty","url":"u40","repository":{"nameWithOwner":"acme/app"},"labels":{"nodes":[]}}}]}}}}}]
EOF
OUT=$(card items)
echo "$OUT" | jq -e '.totalCount == 3 and (.items | length) == 3' >/dev/null || fail "items count: $OUT"
echo "$OUT" | jq -e '.items[0] == {id:"I1",title:"Twelve",status:"Ready",labels:["qa"],content:{type:"Issue",title:"Twelve",number:12,url:"u12",repository:"acme/app"}}' >/dev/null || fail "issue item shape: $OUT"
echo "$OUT" | jq -e '.items[1] | has("status") | not' >/dev/null || fail "no-status item must omit status: $OUT"
echo "$OUT" | jq -e '.items[2].content.type == "PullRequest" and (.items[2] | has("labels") | not)' >/dev/null || fail "PR item shape: $OUT"
ok "items in gh item-list shape"

# 9 — a short read is refused, never planned on.
jq '.[1].data.repositoryOwner.projectV2.items.nodes = []' "$TMP/items.json" > "$TMP/i2" && mv "$TMP/i2" "$TMP/items.json"
RC=0; card items >/dev/null 2>&1 || RC=$?
[ "$RC" != 0 ] || fail "incomplete board read must fail"
ok "incomplete board read refused"
# The real resolver depends on which skills this machine has installed, so the
# field tests run a copy of the helper beside a stub resolver that prints the
# entries seen on AMS card #313 (build lane): plugin prefixes, a fallback, a
# skipped one, a duplicate and the always-present super-board.
mkdir -p "$TMP/scripts"; cp "$CARD" "$TMP/scripts/super-board-card.sh"
cat > "$TMP/scripts/super-board-skills-ready.py" <<'EOF'
import json, os, sys
open(os.environ["FAKE_DIR"] + "/resolver.args", "w").write(" ".join(sys.argv[1:]))
cards = json.load(sys.stdin)
open(os.environ["FAKE_DIR"] + "/resolver.cards", "w").write(json.dumps(cards))
assert cards[0]["number"] == 232 and cards[0]["status"] in ("Ready", "QA", "Review"), cards
e = lambda n, s: {"name": n, "requirement": "required", "status": s}
print(json.dumps({"ready": True, "entries": [e("super-board", "resolved"), e("super-build", "resolved"),
  e("mattpocock-skills:diagnosing-bugs", "resolved"), e("mattpocock-skills:tdd", "resolved"),
  e("ponytail:ponytail", "fallback"), e("mattpocock-skills:code-review", "resolved"),
  e("verification-before-completion", "resolved"), e("mattpocock-skills:tdd", "resolved"), e("some:conditional", "skipped")]}))
EOF
CARD="$TMP/scripts/super-board-card.sh"

# 10 — card fields. Fixture: a board with the Agent and Skills fields.
cat > "$TMP/fields.json" <<'EOF'
{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[
  {"id":"PVTSSF_1","name":"Status","options":[{"id":"opt_ready","name":"Ready"}]},
  {"id":"F_agent","name":"Agent","options":[{"id":"ag_sonnet_build","name":"Sonnet · super-build"},{"id":"ag_done_sonnet_build","name":"Done · Sonnet · super-build"}]},
  {"id":"F_skills","name":"Skills"}]}}}}}
EOF
cat > "$TMP/enrich.json" <<'EOF'
{"data":{"repository":{"issueOrPullRequest":{"body":"no skills line","labels":{"nodes":[{"name":"bug"}]}}}}}
EOF
fresh_fields() { jq 'del(.fields)' "$TMP/cache/board-acme-15.json" > "$TMP/c" && mv "$TMP/c" "$TMP/cache/board-acme-15.json"; }
fieldmut() { grep '^mutation' "$FAKE_LOG" | grep 'a0:' || true; }

fresh_fields; reset_log
OUT=$(card move 232 QA --lane build --model Sonnet 2>&1) || fail "move with fields exited non-zero: $OUT"
[ "$(echo "$OUT" | head -1)" = "moved #232 → QA" ] || fail "output: $OUT"
[ "$(calls mutation)" = 2 ] || fail "status move + ONE batched field mutation expected: $(cat "$FAKE_LOG")"
M=$(fieldmut)
echo "$M" | grep -q 'i=PVTI_232' || fail "fields written to the wrong item: $M"
echo "$M" | grep -qF 'f0=F_skills -f t0=super-build, diagnosing-bugs, tdd, code-review, verification-before-completion -f f1=' || fail "Skills must be the #313 resolved names, prefixes stripped: $M"
echo "$M" | grep -q 'f1=F_agent -f o1=ag_sonnet_build' || fail "Agent 'Sonnet · super-build' not set: $M"
echo "$M" | grep -q 'Card type' && fail "Card type must never be written: $M"
ok "move --lane/--model: Agent + Skills in one batched mutation"

echo '{"base_branch":"staging"}' > "$TMP/cfg-early.json"
# 11 — Done writes the Done · option; the warm field cache costs no fields read.
jq '.options.Done = "opt_done"' "$TMP/cache/board-acme-15.json" > "$TMP/c" && mv "$TMP/c" "$TMP/cache/board-acme-15.json"
reset_log
card --config "$TMP/cfg-early.json" move 232 Done --lane build --model Sonnet --no-pr "test" >/dev/null 2>&1 || fail "Done move failed"
[ "$(calls fields)" = 0 ] || fail "warm field cache must not re-read fields: $(cat "$FAKE_LOG")"
M=$(fieldmut)
echo "$M" | grep -q 'o[0-9]=ag_done_sonnet_build' || fail "Done must use 'Done · Sonnet · super-build': $M"
ok "Done → 'Done · <Model> · super-<lane>'"

# 12 — no flags = the old behaviour: Status only, no extra reads.
reset_log
card move 232 Ready >/dev/null
[ "$(wc -l < "$FAKE_LOG" | tr -d ' ')" = 1 ] || fail "no-flag move must stay 1 call: $(cat "$FAKE_LOG")"
ok "no flags → Status only"

# 13 — a missing Agent option warns, never fails the move, still writes Skills.
reset_log
OUT=$(card move 232 Ready --lane qa --model Haiku 2>&1) || fail "missing option must not fail the move: $OUT"
echo "$OUT" | grep -q "warning: 'Agent' has no option 'Haiku · super-qa'" || fail "no warning for the missing option: $OUT"
M=$(fieldmut)
echo "$M" | grep -q 'f0=F_skills' && ! echo "$M" | grep -q 'F_agent' || fail "Skills-only write expected: $M"
ok "missing Agent option → warning, move exits 0, nothing created"

# 14 — a refused field write warns and the move still exits 0; an old board with no fields is a note.
fresh_fields; touch "$TMP/fail-field-mutation"; reset_log
RC=0; OUT=$(card move 232 QA --lane build --model Sonnet 2>&1) || RC=$?
rm -f "$TMP/fail-field-mutation"
[ "$RC" = 0 ] && echo "$OUT" | grep -q "warning: #232 moved, but its Agent/Skills fields were not all written" || fail "field failure must warn, exit 0 (rc=$RC): $OUT"
echo '{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[{"id":"PVTSSF_1","name":"Status","options":[]}]}}}}}' > "$TMP/fields.json"
fresh_fields; reset_log
OUT=$(card move 232 QA --lane build --model Sonnet 2>&1) || fail "old board must not fail: $OUT"
echo "$OUT" | grep -q "note: board has no Agent/Skills fields" && [ -z "$(fieldmut)" ] || fail "old board: $OUT"
ok "failed field write / board without the fields → warning, exit 0"

# 15 — a bad --lane is a usage error.
RC=0; card move 232 QA --lane deploy >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] || fail "bad --lane should exit 64 (rc=$RC)"
ok "bad --lane → 64"

# 16 — the field cache is dropped when it holds nothing usable, so a field or
# option the owner adds later is picked up on the next move.
cache_has_fields() { jq -e 'has("fields")' "$TMP/cache/board-acme-15.json" >/dev/null; }
reset_log; card move 232 QA --lane build --model Sonnet >/dev/null 2>&1   # fields.json is still the empty board
! cache_has_fields || fail "an empty field list must not stay cached"
cat > "$TMP/fields.json" <<'EOF'
{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[
  {"id":"F_agent","name":"Agent","options":[{"id":"ag_sonnet_build","name":"Sonnet · super-build"}]},
  {"id":"F_skills","name":"Skills"}]}}}}}
EOF
card move 232 QA --lane build --model Haiku >/dev/null 2>&1
! cache_has_fields || fail "a missing Agent option must drop the field cache"
echo '{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[{"id":"F_skills","name":"Skills"}]}}}}}' > "$TMP/fields.json"
card move 232 QA --lane build --model Sonnet >/dev/null 2>&1
! cache_has_fields || fail "a missing Agent field must drop the field cache"
ok "empty / missing-option / missing-field → field cache dropped"

# 17 — a failed field read warns, never writes the halt file, and the move exits 0.
rm -f "$TMP/halt.json"; fresh_fields; touch "$TMP/fail-fields"
RC=0; OUT=$(card move 232 QA --lane build --model Sonnet 2>&1) || RC=$?
rm -f "$TMP/fail-fields"
[ "$RC" = 0 ] && echo "$OUT" | grep -q "warning: could not read the board's fields" || fail "field read failure must warn, exit 0 (rc=$RC): $OUT"
[ ! -e "$TMP/halt.json" ] || fail "a field read must never write the halt file"
ok "field read failure → warning, no halt file, exit 0"

# 18 — usage: --model needs --lane; --host is claude|codex and reaches the resolver.
RC=0; card move 232 QA --model Sonnet >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] || fail "--model without --lane should exit 64 (rc=$RC)"
RC=0; card move 232 QA --lane build --host gemini >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] || fail "bad --host should exit 64 (rc=$RC)"
echo '{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[{"id":"F_skills","name":"Skills"}]}}}}}' > "$TMP/fields.json"; fresh_fields
card move 232 QA --lane build --model Sonnet --host codex >/dev/null 2>&1
grep -q -- '--host codex' "$TMP/resolver.args" || fail "--host codex not passed to the resolver: $(cat "$TMP/resolver.args")"
ok "--model needs --lane, --host validated and passed through"

# 19 — the resolver's --root is the main checkout even when move runs in a linked worktree.
R="$TMP/repo"; git init -q "$R"; git -C "$R" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init
git -C "$R" worktree add -q "$R/.claude/worktrees/issue-1" -b issue-1
fresh_fields
( cd "$R/.claude/worktrees/issue-1" && "$CARD" --owner acme --number 15 --repo acme/app move 232 QA --lane build --model Sonnet >/dev/null 2>&1 )
ROOT_SEEN=$(sed -E 's/.*--root ([^ ]+) .*/\1/' "$TMP/resolver.args")
[ "$(cd "$ROOT_SEEN" && pwd -P)" = "$(cd "$R" && pwd -P)" ] || fail "resolver root should be the main checkout $R, got $ROOT_SEEN"
ok "resolver root = main checkout from a worktree"

# ── ams.19: Agent, Skills and the routing label are mandatory ───────────
cat > "$TMP/fields.json" <<'EOF'
{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[
  {"id":"F_agent","name":"Agent","options":[{"id":"ag_sonnet_build","name":"Sonnet · super-build"}]},
  {"id":"F_skills","name":"Skills"}]}}}}}
EOF
LABELLED='{"data":{"repository":{"issueOrPullRequest":{"body":"b","labels":{"nodes":[{"name":"Bug"}]}}}}}'
UNLABELLED='{"data":{"repository":{"issueOrPullRequest":{"body":"b","labels":{"nodes":[{"name":"wontfix"}]}}}}}'
REFACTOR='{"data":{"repository":{"issueOrPullRequest":{"body":"b","labels":{"nodes":[{"name":"Refactor"}]}}}}}'
echo "$LABELLED" > "$TMP/enrich.json"
mutations() { grep -c '^mutation' "$FAKE_LOG" 2>/dev/null || true; }

# 20 — a move into Building/QA/Review/Blocked/Done needs --lane and --model: exit 64, nothing called.
for S in Building QA Review Blocked Done; do
  reset_log; RC=0; OUT=$(card move 232 "$S" 2>&1) || RC=$?
  [ "$RC" = 64 ] || fail "move into $S without --lane/--model should exit 64 (rc=$RC)"
  echo "$OUT" | grep -qF "move into $S needs --lane and --model (Agent/Skills are mandatory)" || fail "message for $S: $OUT"
  [ ! -s "$FAKE_LOG" ] || fail "move into $S without flags must make no call: $(cat "$FAKE_LOG")"
done
reset_log; RC=0; card move 232 Review --lane review >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && [ ! -s "$FAKE_LOG" ] || fail "Review with --lane but no --model must exit 64 untouched (rc=$RC)"
ok "mandatory --lane/--model: 64, no call, no mutation"

# 21 — no routing label and no --kind: exit 64, no mutation.
echo "$UNLABELLED" > "$TMP/enrich.json"; reset_log; RC=0
OUT=$(card move 232 Review --lane review --model Opus 2>&1) || RC=$?
[ "$RC" = 64 ] && [ "$(mutations)" = 0 ] && [ "$(calls issue-edit)" = 0 ] || fail "unlabelled without --kind: rc=$RC $(cat "$FAKE_LOG")"
echo "$OUT" | grep -qF "#232 has no type label (bug, feature, qa, docs, ux, tests, refactor, tech-debt or money); pass --kind" || fail "message: $OUT"
RC=0; card move 232 Review --lane review --model Opus --kind epic >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] || fail "bad --kind should exit 64 (rc=$RC)"
ok "no label, no --kind → 64, no mutation"

# 22 — --kind on an unlabelled card: the label is added BEFORE the Status mutation.
fresh_fields; reset_log; rm -f "$TMP/resolver.cards"
card move 232 QA --lane build --model Sonnet --kind BUG >/dev/null 2>&1 || fail "--kind move failed: $(cat "$FAKE_LOG")"
[ "$(calls issue-edit)" = 1 ] || fail "expected one label edit: $(cat "$FAKE_LOG")"
grep '^issue-edit' "$FAKE_LOG" | grep -q -- '--add-label bug' || fail "label should be lowercase bug: $(cat "$FAKE_LOG")"
E=$(grep -n '^issue-edit' "$FAKE_LOG" | head -1 | cut -d: -f1); M1=$(grep -n '^mutation' "$FAKE_LOG" | head -1 | cut -d: -f1)
[ "$E" -lt "$M1" ] || fail "label edit must come before the Status mutation: $(cat "$FAKE_LOG")"
jq -e '.[0].labels | index("bug") != null' "$TMP/resolver.cards" >/dev/null || fail "Skills must resolve with the label --kind just added: $(cat "$TMP/resolver.cards")"
ok "--kind bug on an unlabelled card → issue edit --add-label, then the move; Skills resolve with the new label"

# 23 — a labelled card (any case) moves as before; --kind is ignored; Ready/Backlog need no lane.
echo "$LABELLED" > "$TMP/enrich.json"; reset_log
OUT=$(card move 232 QA --lane build --model Sonnet --kind feature 2>&1) || fail "labelled move failed: $OUT"
echo "$OUT" | grep -q '^moved #232 → QA' && [ "$(calls issue-edit)" = 0 ] || fail "labelled card must not be re-labelled: $OUT"
[ "$(calls comments)" = 0 ] || fail "a move must not read comments unless the Agent option is missing: $(cat "$FAKE_LOG")"
reset_log; card move 232 Ready >/dev/null 2>&1 || fail "Ready without --lane must still work"
RC=0; card move 232 Backlog >/dev/null 2>&1 || RC=$?
[ "$RC" != 64 ] || fail "Backlog without --lane must not be a usage error"
ok "labelled card moves as today; Ready/Backlog need no --lane"

# 24 — a missing Agent option posts ONE comment per value; a marker already on the issue posts none.
#      Comments are read (REST, all pages) only on this path.
echo '[]' > "$TMP/comments.json"; reset_log
card move 232 QA --lane build --model Haiku >/dev/null 2>&1 || fail "missing Agent option must not fail the move"
[ "$(calls comments)" = 1 ] && grep '^comments' "$FAKE_LOG" | grep -q -- '--paginate' || fail "comments must be read once, paginated: $(cat "$FAKE_LOG")"
[ "$(calls issue-comment)" = 1 ] || fail "expected one comment: $(cat "$FAKE_LOG")"
grep '^issue-comment' "$FAKE_LOG" | grep -qF '<!-- sb:agent-option-missing:Haiku · super-build -->' || fail "marker missing: $(cat "$FAKE_LOG")"
grep '^issue-comment' "$FAKE_LOG" | grep -qF "Agent 'Haiku · super-build' is not an option on the board; add it in the board UI (Agent field → Edit)." || fail "comment text: $(cat "$FAKE_LOG")"
# the marker sits on page 2 of the comments: --paginate prints concatenated arrays
cat > "$TMP/comments.json" <<'EOF'
[{"body":"unrelated"}][{"body":"<!-- sb:agent-option-missing:Haiku · super-build -->\n⚠️ [board] Agent 'Haiku · super-build' is not an option on the board; add it in the board UI (Agent field → Edit)."}]
EOF
reset_log; RC=0
OUT=$(card move 232 QA --lane build --model Haiku 2>&1) || RC=$?
[ "$RC" = 0 ] && [ "$(calls issue-comment)" = 0 ] || fail "an existing marker must suppress the comment (rc=$RC): $(cat "$FAKE_LOG")"
echo "$OUT" | grep -q "warning: 'Agent' has no option 'Haiku · super-build'" || fail "stderr warning must stay: $OUT"
# a marker for another value does not suppress this one
echo '[{"body":"<!-- sb:agent-option-missing:Opus · super-build -->"}]' > "$TMP/comments.json"; reset_log
card move 232 QA --lane build --model Haiku >/dev/null 2>&1
[ "$(calls issue-comment)" = 1 ] || fail "a marker for another value must not suppress: $(cat "$FAKE_LOG")"
# a failed comments read posts nothing and still exits 0
touch "$TMP/fail-comments"; reset_log; RC=0
OUT=$(card move 232 QA --lane build --model Haiku 2>&1) || RC=$?
rm -f "$TMP/fail-comments"
[ "$RC" = 0 ] && [ "$(calls issue-comment)" = 0 ] && echo "$OUT" | grep -q "could not read #232's comments" || fail "comments read failure: rc=$RC $OUT"
ok "missing Agent option → one marker comment (all comment pages read), none the second time, warning stays, exit 0"

# 24b — a type label beyond bug/feature/qa (refactor) is already a type: moves without --kind, no label edit.
echo "$REFACTOR" > "$TMP/enrich.json"; reset_log; RC=0
OUT=$(card move 232 QA --lane qa --model Opus 2>&1) || RC=$?
[ "$RC" = 0 ] && echo "$OUT" | grep -q '^moved #232 → QA' && [ "$(calls issue-edit)" = 0 ] || fail "refactor card must move without --kind (rc=$RC): $OUT"
echo "$LABELLED" > "$TMP/enrich.json"
ok "refactor-labelled card moves without --kind"

# 25 — audit: one paginated read; Backlog/Ready ignored; PRs and closed issues ignored; the right missing lists.
cat > "$TMP/audit.json" <<'EOF'
[{"data":{"repositoryOwner":{"projectV2":{"items":{"totalCount":13,"pageInfo":{"hasNextPage":true,"endCursor":"c1"},"nodes":[
  {"id":"A1","status":{"name":"Backlog"},"agent":null,"skills":null,"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":1,"state":"OPEN","labels":{"nodes":[]}}},
  {"id":"A2","status":{"name":"Ready"},"agent":null,"skills":null,"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":2,"state":"OPEN","labels":{"nodes":[]}}},
  {"id":"A3","status":{"name":"Building"},"agent":{"name":"Sonnet · super-build"},"skills":{"text":"super-build, tdd"},"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":3,"state":"OPEN","labels":{"nodes":[{"name":"bug"}]}}},
  {"id":"A4","status":{"name":"Review"},"agent":null,"skills":null,"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":4,"state":"OPEN","labels":{"nodes":[]}}},
  {"id":"A8","status":{"name":"Review"},"agent":null,"skills":null,"content":{"__typename":"PullRequest","number":8,"labels":{"nodes":[]}}}]}}}}},
 {"data":{"repositoryOwner":{"projectV2":{"items":{"totalCount":13,"pageInfo":{"hasNextPage":false,"endCursor":null},"nodes":[
  {"id":"A5","status":{"name":"QA"},"agent":{"name":"Haiku · super-qa"},"skills":{"text":"  "},"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":5,"state":"OPEN","labels":{"nodes":[{"name":"QA"}]}}},
  {"id":"A6","status":{"name":"Blocked"},"agent":{"name":"Opus · super-review"},"skills":{"text":"super-review"},"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":6,"state":"OPEN","labels":{"nodes":[{"name":"wontfix"}]}}},
  {"id":"A7","status":{"name":"Building"},"agent":null,"skills":null,"content":{"__typename":"DraftIssue"}},
  {"id":"A9","status":{"name":"QA"},"agent":null,"skills":null,"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":9,"state":"CLOSED","labels":{"nodes":[]}}},
  {"id":"A11","status":{"name":"Review"},"agent":null,"skills":null,"content":{"__typename":"Issue","repository":{"nameWithOwner":"other/elsewhere"},"number":11,"state":"OPEN","labels":{"nodes":[]}}},
  {"id":"A12","status":{"name":"Done"},"agent":null,"skills":null,"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":12,"state":"OPEN","labels":{"nodes":[]}}},
  {"id":"A13","status":{"name":"Done"},"agent":null,"skills":null,"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":13,"state":"CLOSED","labels":{"nodes":[]}}},
  {"id":"A10","status":{"name":"Review"},"agent":{"name":"Opus · super-review"},"skills":{"text":"super-review"},"content":{"__typename":"Issue","repository":{"nameWithOwner":"Acme/App"},"number":10,"state":"OPEN","labels":{"nodes":[{"name":"Refactor"}]}}}]}}}}}]
EOF
reset_log
OUT=$(card audit) || fail "audit exit non-zero"
[ "$(calls audit)" = 1 ] && [ "$(wc -l < "$FAKE_LOG" | tr -d ' ')" = 1 ] || fail "audit must be one read: $(cat "$FAKE_LOG")"
grep '^audit' "$FAKE_LOG" | grep -q '__typename' && grep '^audit' "$FAKE_LOG" | grep -q 'state' || fail "audit query must read __typename and state"
EXPECT=$'#4 Review missing: agent,skills,label\n#5 QA missing: skills\n#6 Blocked missing: label\n#12 Done missing: closed\naudit: 4 card(s) missing fields'
[ "$OUT" = "$EXPECT" ] || fail "audit text (a PR item, a closed issue and a refactor-labelled card must not show up):
$OUT"
echo "$OUT" | grep -q '#8\|#9\|#10\|#13' && fail "PR / closed issue / refactor card listed: $OUT"
echo "$OUT" | grep -q '#11' && fail "a foreign-repo issue in Review must not be listed: $OUT"
grep '^audit' "$FAKE_LOG" | grep -q 'nameWithOwner' || fail "audit query must read the repository"
OUT=$(card audit --json)
echo "$OUT" | jq -e '. == [{"number":4,"status":"Review","missing":["agent","skills","label"]},{"number":5,"status":"QA","missing":["skills"]},{"number":6,"status":"Blocked","missing":["label"]},{"number":12,"status":"Done","missing":["closed"]}]' >/dev/null || fail "audit json: $OUT"
jq '[.[0].data.repositoryOwner.projectV2.items.nodes[0,1,2]] as $n | [{data:{repositoryOwner:{projectV2:{items:{totalCount:3,pageInfo:{hasNextPage:false,endCursor:null},nodes:$n}}}}}]' "$TMP/audit.json" > "$TMP/a2" && mv "$TMP/a2" "$TMP/audit.json"
OUT=$(card audit); [ "$OUT" = "audit: clean" ] || fail "clean board: $OUT"
[ "$(card audit --json)" = "[]" ] || fail "clean json"
RC=0; card audit --nope >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] || fail "audit bad flag should exit 64 (rc=$RC)"
ok "audit: text + --json, open Issues in lanes only, type labels beyond bug/feature/qa, clean board"

# 26 — --model is normalised to a family word; anything else is kept as given.
cat > "$TMP/fields.json" <<'EOF'
{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[
  {"id":"F_agent","name":"Agent","options":[{"id":"ag_opus_review","name":"Opus · super-review"},{"id":"ag_opus_lint","name":"Opus · super-lint"},{"id":"ag_done_opus_lint","name":"Done · Opus · super-lint"}]},
  {"id":"F_skills","name":"Skills"}]}}}}}
EOF
echo '[]' > "$TMP/comments.json"
jq '.options.Review = "opt_review"' "$TMP/cache/board-acme-15.json" > "$TMP/c" && mv "$TMP/c" "$TMP/cache/board-acme-15.json"
for V in "claude opus 5.5" "opus" "Claude Opus 5.5" "OPUS"; do
  fresh_fields; reset_log
  card move 232 Review --lane review --model "$V" >/dev/null 2>&1 || fail "move with --model '$V' failed"
  fieldmut | grep -q 'o[0-9]=ag_opus_review' || fail "--model '$V' must become Opus · super-review: $(fieldmut)"
done
fresh_fields; reset_log
OUT=$(card move 232 QA --lane qa --model Grok 2>&1) || fail "--model Grok failed: $OUT"
echo "$OUT" | grep -qF "warning: 'Agent' has no option 'Grok · super-qa'" || fail "--model Grok must stay Grok: $OUT"
ok "--model: 'claude opus 5.5' / 'opus' -> Opus, 'Grok' kept as given"

# 27 — --lane lint: Blocked and Done only; Agent '<Model> · super-lint', Skills fixed 'super-board', no resolver.
fresh_fields; reset_log; rm -f "$TMP/resolver.args"
jq '.options.Blocked = "opt_blocked" | .options.Done = "opt_done"' "$TMP/cache/board-acme-15.json" > "$TMP/c" && mv "$TMP/c" "$TMP/cache/board-acme-15.json"
echo "$LABELLED" > "$TMP/enrich.json"
card move 232 Blocked --lane lint --model "claude opus 5.5" >/dev/null 2>&1 || fail "lint into Blocked failed: $(cat "$FAKE_LOG")"
M=$(fieldmut)
echo "$M" | grep -q 'f0=F_skills -f t0=super-board -f' || fail "lint must write Skills 'super-board': $M"
echo "$M" | grep -q 'o[0-9]=ag_opus_lint' || fail "lint Agent should be 'Opus · super-lint': $M"
[ ! -e "$TMP/resolver.args" ] || fail "the skills resolver must not run for lint"
reset_log; card --config "$TMP/cfg-early.json" move 232 Done --lane lint --model Opus --no-pr "out of scope" >/dev/null 2>&1 || fail "lint into Done failed"
fieldmut | grep -q 'o[0-9]=ag_done_opus_lint' || fail "lint Done Agent should be 'Done · Opus · super-lint': $(fieldmut)"
for S in QA Review Building Ready Backlog; do
  reset_log; RC=0; card move 232 "$S" --lane lint --model Opus >/dev/null 2>&1 || RC=$?
  [ "$RC" = 64 ] && [ ! -s "$FAKE_LOG" ] || fail "lint into $S must exit 64 untouched (rc=$RC)"
done
echo "$UNLABELLED" > "$TMP/enrich.json"; reset_log; RC=0
card move 232 Blocked --lane lint --model Opus >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && [ "$(mutations)" = 0 ] || fail "lint into Blocked still needs a type label or --kind (rc=$RC)"
echo "$LABELLED" > "$TMP/enrich.json"
ok "--lane lint: Blocked/Done only, Skills 'super-board', no resolver; other Status → 64; Blocked keeps the label rule"

# ── ams.20: Done closes the issue; release labels what reached the default branch ──
SHA_A=a1b2c3d4e5f60718293a4b5c6d7e8f9012345678; SHA_B=b1b2c3d4e5f60718293a4b5c6d7e8f9012345678
echo '{"base_branch":"staging","protected_issues":[2]}' > "$TMP/cfg.json"
cardc() { card --config "$TMP/cfg.json" "$@"; }
# pr_json NUM STATE MERGE-COMMIT-JSON BASE HEAD [CLOSING-NUMBER-OR-none] [BODY]: by default the PR closes #232.
pr_json() {
  local closing='[{"number":232}]'; [ "${6:-232}" = none ] && closing='[]'
  jq -n --arg st "$2" --argjson mc "$3" --arg b "$4" --arg h "$5" --argjson c "$closing" --arg body "${7:-}"     '{state:$st,mergeCommit:$mc,baseRefName:$b,headRefName:$h,closingIssuesReferences:$c,body:$body,title:"a PR"}' > "$TMP/pr-$1.json"
}
pr_json 50 MERGED "{\"oid\":\"$SHA_A\"}" staging feat
pr_json 51 OPEN null staging feat
pr_json 52 MERGED "{\"oid\":\"$SHA_A\"}" main feat
pr_json 53 MERGED "{\"oid\":\"$SHA_A\"}" staging feat none "unrelated work"
pr_json 54 MERGED "{\"oid\":\"$SHA_A\"}" staging issue-232-x none
pr_json 55 MERGED "{\"oid\":\"$SHA_A\"}" staging feat none "Fixes #232"
pr_json 56 MERGED "{\"oid\":\"$SHA_A\"}" staging issue-2320-x none "mentions #2320 and #23"
echo '{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[{"id":"F_skills","name":"Skills"}]}}}}}' > "$TMP/fields.json"
echo "$LABELLED" > "$TMP/enrich.json"
dmove() { cardc move 232 Done --lane review --model Opus "$@"; }
done_reset() { fresh_fields; reset_log; rm -f "$TMP/fail-close" "$TMP/issue-state.json" "$TMP"/comments-*.json; echo '[]' > "$TMP/comments.json"; }
status_mutated() { grep '^mutation' "$FAKE_LOG" | grep -q 'o=opt_done'; }
quiet() { [ ! -s "$FAKE_LOG" ]; }

# 28 — Done needs exactly one of --pr / --no-pr, and only Done takes them: 64, nothing called.
done_reset; RC=0; dmove >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "Done with neither flag: rc=$RC $(cat "$FAKE_LOG")"
RC=0; dmove --pr 50 --no-pr "x" >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "Done with both flags: rc=$RC"
RC=0; dmove --no-pr "   " >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "empty --no-pr reason: rc=$RC"
RC=0; cardc move 232 QA --lane qa --model Opus --pr 50 >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "--pr on QA: rc=$RC"
RC=0; cardc move 232 QA --lane qa --model Opus --no-pr "x" >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "--no-pr on QA: rc=$RC"
ok "Done needs exactly one of --pr/--no-pr; QA takes neither: 64, no call"

# 29 — Done --pr 50 (MERGED into the config's base): status set, ONE marker comment with the sha, issue closed as completed.
done_reset; RC=0; OUT=$(dmove --pr 50 2>&1) || RC=$?
[ "$RC" = 0 ] && echo "$OUT" | grep -q '^moved #232 → Done' || fail "Done --pr 50: rc=$RC $OUT"
status_mutated || fail "Done status not set: $(cat "$FAKE_LOG")"
P=$(grep -n '^pr-view' "$FAKE_LOG" | head -1 | cut -d: -f1); M1=$(grep -n '^mutation' "$FAKE_LOG" | head -1 | cut -d: -f1)
[ -n "$P" ] && [ "$P" -lt "$M1" ] || fail "the PR must be read before any mutation: $(cat "$FAKE_LOG")"
[ "$(calls issue-comment)" = 1 ] || fail "expected one marker comment: $(cat "$FAKE_LOG")"
grep '^issue-comment' "$FAKE_LOG" | grep -qF "<!-- sb:done-closed:$SHA_A -->" || fail "marker lacks the merge sha: $(cat "$FAKE_LOG")"
grep '^issue-comment' "$FAKE_LOG" | grep -qF "✅ Done: merged in #50 (a1b2c3d) into staging." || fail "comment text: $(cat "$FAKE_LOG")"
[ "$(calls issue-close)" = 1 ] && grep '^issue-close' "$FAKE_LOG" | grep -q -- '--reason completed' || fail "issue not closed as completed: $(cat "$FAKE_LOG")"
ok "Done --pr 50 → status, one marker comment, issue closed"

# 30 — a PR that is OPEN, or merged into another base: 64, no status mutation, no close.
for P in 51 52; do
  done_reset; RC=0; OUT=$(dmove --pr "$P" 2>&1) || RC=$?
  [ "$RC" = 64 ] && [ "$(mutations)" = 0 ] && [ "$(calls issue-close)" = 0 ] && [ "$(calls issue-comment)" = 0 ] || fail "Done --pr $P: rc=$RC $(cat "$FAKE_LOG")"
  echo "$OUT" | grep -qF "PR #$P is not merged into staging; not moving #232" || fail "message for PR $P: $OUT"
done
ok "PR open / merged into another base → 64, nothing changed"

# 31 — --no-pr: no PR read, marker 'none' plus the reason, closed.
done_reset; RC=0; dmove --no-pr "duplicate of #9" >/dev/null 2>&1 || RC=$?
[ "$RC" = 0 ] && [ "$(calls pr-view)" = 0 ] && [ "$(calls issue-close)" = 1 ] || fail "Done --no-pr: rc=$RC $(cat "$FAKE_LOG")"
grep '^issue-comment' "$FAKE_LOG" | grep -qF '<!-- sb:done-closed:none -->' && grep '^issue-comment' "$FAKE_LOG" | grep -qF '✅ Done without a PR: duplicate of #9' || fail "none marker / reason: $(cat "$FAKE_LOG")"
ok "Done --no-pr → 'none' marker with the reason, closed"

# 32 — a marker already on the issue: no second comment (the issue is still closed).
done_reset; echo '[{"body":"<!-- sb:done-closed:none -->\nold"}]' > "$TMP/comments.json"
dmove --no-pr "again" >/dev/null 2>&1 || fail "Done with an existing marker failed"
[ "$(calls issue-comment)" = 0 ] && [ "$(calls issue-close)" = 1 ] || fail "existing marker must suppress the comment: $(cat "$FAKE_LOG")"
ok "existing sb:done-closed marker → no second comment"

# 33 — an already CLOSED issue is not closed again.
done_reset; echo '{"state":"CLOSED"}' > "$TMP/issue-state.json"
dmove --no-pr "x" >/dev/null 2>&1 || fail "Done on a closed issue failed"
[ "$(calls issue-close)" = 0 ] && [ "$(calls issue-comment)" = 1 ] || fail "closed issue: no close call expected: $(cat "$FAKE_LOG")"
ok "already closed issue → no close call"

# 34 — a protected issue is never changed: 64 and no gh call at all.
for S in "Done --lane review --model Opus --pr 50" "Ready"; do
  done_reset; RC=0; OUT=$(cardc move 2 $S 2>&1) || RC=$?
  [ "$RC" = 64 ] && quiet || fail "protected #2 ($S): rc=$RC $(cat "$FAKE_LOG")"
  echo "$OUT" | grep -qF "#2 is protected (config protected_issues); not changing it" || fail "protected message: $OUT"
done
ok "protected issue → 64, no gh call"

# 35 — the close fails after the status moved: loud, exit 69.
done_reset; touch "$TMP/fail-close"; RC=0; OUT=$(dmove --pr 50 2>&1) || RC=$?
rm -f "$TMP/fail-close"
[ "$RC" = 69 ] && status_mutated || fail "close failure must exit 69 after the status moved (rc=$RC): $OUT"
echo "$OUT" | grep -qF "moved #232 → Done, but closing it failed: could not close the issue" || fail "stderr message: $OUT"
ok "close failure after the move → exit 69, message on stderr"

# 41 — Done without base_branch falls back to main (a staging PR is refused, 64); without any config: 66, nothing called.
echo '{"protected_issues":[2]}' > "$TMP/cfg-nobase.json"
done_reset; RC=0; OUT=$(card --config "$TMP/cfg-nobase.json" move 232 Done --lane review --model Opus --pr 50 2>&1) || RC=$?
[ "$RC" = 64 ] && echo "$OUT" | grep -qF "is not merged into main" || fail "Done without base_branch should fall back to main: rc=$RC $OUT $(cat "$FAKE_LOG")"
done_reset; RC=0; card move 232 Done --lane review --model Opus --no-pr "x" >/dev/null 2>&1 || RC=$?
[ "$RC" = 66 ] && quiet || fail "Done without any config: rc=$RC"
ok "Done: no base_branch -> main; no config -> 66, nothing called"

# 42 — the PR must belong to the issue.
done_reset; RC=0; OUT=$(dmove --pr 53 2>&1) || RC=$?
[ "$RC" = 64 ] && [ "$(mutations)" = 0 ] && [ "$(calls issue-close)" = 0 ] && echo "$OUT" | grep -qF "PR #53 does not reference #232; not moving it" || fail "unrelated PR: rc=$RC $OUT"
done_reset; RC=0; OUT=$(dmove --pr 56 2>&1) || RC=$?
[ "$RC" = 64 ] && [ "$(mutations)" = 0 ] || fail "#2320 / #23 must not match issue 232: rc=$RC $OUT"
for P in 50 54 55; do   # closing reference / issue-232-x branch / "Fixes #232" in the body
  done_reset; RC=0; OUT=$(dmove --pr "$P" 2>&1) || RC=$?
  [ "$RC" = 0 ] && status_mutated || fail "PR $P references #232 but was refused: rc=$RC $OUT"
done
ok "PR must reference the issue: closing ref, issue-N branch or #N token pass; unrelated and #2320 do not"

# 43 — the issue must be a plain integer.
done_reset; RC=0; cardc move 02 Done --lane review --model Opus --pr 50 >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "move 02: rc=$RC"
RC=0; cardc move 2x Ready >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "move 2x: rc=$RC"
ok "non-integer issue number -> 64, nothing called"

# 44 — an old 'none' marker does not hide the sha marker of a later Done with a PR.
done_reset; echo '[{"body":"<!-- sb:done-closed:none -->\nold"}]' > "$TMP/comments.json"
dmove --pr 50 >/dev/null 2>&1 || fail "Done --pr after a none marker failed"
[ "$(calls issue-comment)" = 1 ] && grep '^issue-comment' "$FAKE_LOG" | grep -qF "<!-- sb:done-closed:$SHA_A -->" || fail "the sha marker must be posted: $(cat "$FAKE_LOG")"
done_reset; echo "[{\"body\":\"<!-- sb:done-closed:$SHA_A -->\nold\"}]" > "$TMP/comments.json"
dmove --pr 50 >/dev/null 2>&1 || fail "Done --pr with the same sha marker failed"
[ "$(calls issue-comment)" = 0 ] || fail "the same marker must not be posted twice: $(cat "$FAKE_LOG")"
ok "marker check is exact"

# 45 — --pr accepts #50 and a PR URL.
for V in '#50' 'https://github.com/o/r/pull/50'; do
  done_reset; RC=0; OUT=$(dmove --pr "$V" 2>&1) || RC=$?
  [ "$RC" = 0 ] && grep '^pr-view' "$FAKE_LOG" | grep -q ' view 50 ' && status_mutated || fail "--pr '$V': rc=$RC $OUT $(cat "$FAKE_LOG")"
done
done_reset; RC=0; dmove --pr abc >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "--pr abc: rc=$RC"
done_reset; RC=0; dmove --pr '#' >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "--pr '#' must not pass as an empty PR: rc=$RC $(cat "$FAKE_LOG")"
ok "--pr accepts #50 and a PR URL; junk and a bare # -> 64"

# 36 — release: label the closed issues whose marker sha is among the PR's commits.
done_reset
printf '%s\n%s\n' "$SHA_A" "c1b2c3d4e5f60718293a4b5c6d7e8f9012345678" > "$TMP/commits-60.txt"
pr_json 60 MERGED "{\"oid\":\"$SHA_A\"}" main staging
pr_json 61 MERGED "{\"oid\":\"$SHA_A\"}" main feature
echo '[{"number":5},{"number":6},{"number":7},{"number":2},{"number":8}]' > "$TMP/issues-closed.json"
echo "[{\"body\":\"<!-- sb:done-closed:$SHA_A -->\\nx\"}]" > "$TMP/comments-5.json"
echo "[{\"body\":\"<!-- sb:done-closed:$SHA_B -->\\nx\"}]" > "$TMP/comments-6.json"
echo '[{"body":"<!-- sb:done-closed:none -->\nx"}]' > "$TMP/comments-7.json"
echo "[{\"body\":\"<!-- sb:done-closed:$SHA_A -->\\nx\"}]" > "$TMP/comments-2.json"
echo '[{"body":"no marker"}]' > "$TMP/comments-8.json"
RC=0; OUT=$(cardc release 60 2>&1) || RC=$?
[ "$RC" = 0 ] || fail "release 60: rc=$RC $OUT"
[ "$(calls issue-edit)" = 1 ] && grep '^issue-edit' "$FAKE_LOG" | grep -q ' 5 .*--add-label released' || fail "only #5 should be labelled: $(cat "$FAKE_LOG")"
echo "$OUT" | grep -qx 'released #5' && echo "$OUT" | grep -qx 'released: 1 issue(s) from PR #60' || fail "release output: $OUT"
grep -q 'issues/2/comments' "$FAKE_LOG" && fail "a protected issue's comments must not even be read"
[ "$(calls label-create)" = 1 ] && grep '^label-create' "$FAKE_LOG" | grep -q -- '--color 0E8A16' || fail "label must be created when missing: $(cat "$FAKE_LOG")"
reset_log; echo '[{"name":"released"}]' > "$TMP/labels.json"; cardc release 60 >/dev/null 2>&1 || fail "release with the label present"
[ "$(calls label-create)" = 0 ] || fail "an existing label must not be created again"
rm -f "$TMP/labels.json"
ok "release 60 → #5 only (not B, none, protected #2, no marker); label created when missing"

# 37 — release --dry-run mutates nothing.
reset_log; RC=0; OUT=$(cardc release 60 --dry-run 2>&1) || RC=$?
[ "$RC" = 0 ] && echo "$OUT" | grep -qx 'would release #5' || fail "dry-run output: rc=$RC $OUT"
[ "$(calls issue-edit)" = 0 ] && [ "$(calls label-create)" = 0 ] || fail "dry-run must not mutate: $(cat "$FAKE_LOG")"
ok "release --dry-run → 'would release', no label create, no edit"

# 38 — a PR whose head is not base_branch: 64, nothing changed; no base_branch in the config: 66.
reset_log; RC=0; cardc release 61 >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && [ "$(calls issue-edit)" = 0 ] && [ "$(calls label-create)" = 0 ] || fail "release 61 (head != base_branch): rc=$RC"
RC=0; card release 60 >/dev/null 2>&1 || RC=$?
[ "$RC" = 66 ] || fail "release without base_branch should exit 66 (rc=$RC)"
ok "release: head != base_branch → 64; no base_branch → 66"

# 39 — GitHub caps the commit list at 250: exit 69, nothing labelled.
seq 1 250 | sed 's/^/a/' > "$TMP/commits-62.txt"; pr_json 62 MERGED "{\"oid\":\"$SHA_A\"}" main staging
reset_log; RC=0; OUT=$(cardc release 62 2>&1) || RC=$?
[ "$RC" = 69 ] && [ "$(calls issue-edit)" = 0 ] && [ "$(calls label-create)" = 0 ] || fail "250 commits: rc=$RC $(cat "$FAKE_LOG")"
echo "$OUT" | grep -q 'caps that list at 250' || fail "250-commit message: $OUT"
ok "release with 250 commits → 69, nothing labelled"

# 40 — --tool grok|pi|codex names the delegate tool as the Agent; a missing option is an error line, never created.
cat > "$TMP/fields.json" <<'EOF'
{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[
  {"id":"F_agent","name":"Agent","options":[{"id":"ag_grok_build","name":"Grok · super-build"},{"id":"ag_pi_build","name":"Pi · super-build"}]},
  {"id":"F_skills","name":"Skills"}]}}}}}
EOF
echo '[]' > "$TMP/comments.json"; rm -f "$TMP"/comments-*.json
fresh_fields; reset_log
card move 232 QA --lane build --tool GROK >/dev/null 2>&1 || fail "--tool GROK move failed: $(cat "$FAKE_LOG")"
fieldmut | grep -q 'o[0-9]=ag_grok_build' || fail "--tool grok must write 'Grok · super-build': $(fieldmut)"
fresh_fields; reset_log
card move 232 QA --lane build --tool pi >/dev/null 2>&1 || fail "--tool pi move failed"
fieldmut | grep -q 'o[0-9]=ag_pi_build' || fail "--tool pi must write 'Pi · super-build': $(fieldmut)"
fresh_fields; reset_log
card move 232 QA --lane build --model pi >/dev/null 2>&1 || fail "--model pi move failed"
fieldmut | grep -q 'o[0-9]=ag_pi_build' || fail "--model pi must become Pi: $(fieldmut)"
fresh_fields; reset_log
card move 232 QA --lane build --model "Grok Code Fast" >/dev/null 2>&1 || fail "--model grok move failed"
fieldmut | grep -q 'o[0-9]=ag_grok_build' || fail "--model containing grok must become Grok: $(fieldmut)"
fresh_fields; reset_log; OUT=$(card move 232 QA --lane build --model "pip" 2>&1) || fail "--model pip failed"
echo "$OUT" | grep -qF "has no option 'pip · super-build'" || fail "'pip' must not be matched as Pi: $OUT"
reset_log; RC=0; OUT=$(card move 232 QA --lane build --tool foo 2>&1) || RC=$?
[ "$RC" = 64 ] && quiet && echo "$OUT" | grep -qF -- "--tool must be grok, pi or codex" || fail "--tool foo: rc=$RC $OUT"
RC=0; OUT=$(card move 232 QA --lane build --tool grok --model Opus 2>&1) || RC=$?
[ "$RC" = 64 ] && quiet && echo "$OUT" | grep -qF "pass --model or --tool, not both" || fail "--tool with --model: rc=$RC $OUT"
RC=0; card move 232 QA --tool grok >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "--tool without --lane: rc=$RC"
RC=0; card move 232 QA --lane build >/dev/null 2>&1 || RC=$?
[ "$RC" = 64 ] && quiet || fail "--tool/--model still required for QA: rc=$RC"
fresh_fields; reset_log; echo '[]' > "$TMP/comments.json"
OUT=$(card move 232 QA --lane build --tool codex 2>&1) || fail "--tool codex with no Agent option must not fail the move: $OUT"
echo "$OUT" | grep -qF "error: Agent option 'Codex · super-build' is missing on the board; add it in the board UI (Agent field → Edit). Never add it through the API" || fail "missing-option error line: $OUT"
grep '^issue-comment' "$FAKE_LOG" | grep -qF 'Never add it through the API' || fail "comment must carry the never-through-the-API sentence: $(cat "$FAKE_LOG")"
ok "--tool grok|pi|codex, --model grok / exact pi, usage errors, missing Agent option → error line + comment"

# ── ams.20: a Building/Review move of an issue with no milestone warns ──
jq '.options.Building = "opt_building"' "$TMP/cache/board-acme-15.json" > "$TMP/c" && mv "$TMP/c" "$TMP/cache/board-acme-15.json"
echo '{"data":{"repositoryOwner":{"projectV2":{"fields":{"nodes":[{"id":"F_skills","name":"Skills"}]}}}}}' > "$TMP/fields.json"
MSWARN="warning: #232 has no milestone; set one on the issue (the Roadmap counts issues per milestone)"
meta_json() { jq -n --argjson m "$1" --argjson l "$2" '{data:{repository:{issueOrPullRequest:{body:"b",milestone:$m,labels:{nodes:$l}}}}}' > "$TMP/enrich.json"; }
echo '{"roadmap":{"issue":232}}' > "$TMP/cfg-road.json"
# 47a — no milestone: warning on stderr, exit 0, Status mutated.
meta_json null '[{"name":"bug"}]'; fresh_fields; reset_log; RC=0
OUT=$(card move 232 Building --lane build --model Sonnet 2>&1) || RC=$?
[ "$RC" = 0 ] && echo "$OUT" | grep -qxF "$MSWARN" && grep '^mutation' "$FAKE_LOG" | grep -q 'o=opt_building' || fail "no-milestone Building: rc=$RC $OUT"
# 47b — with a milestone: no warning.
meta_json '{"title":"v1"}' '[{"name":"bug"}]'; fresh_fields; reset_log
OUT=$(card move 232 Building --lane build --model Sonnet 2>&1) || fail "milestone Building failed: $OUT"
echo "$OUT" | grep -q 'no milestone' && fail "milestone present, must not warn: $OUT"
# 47c — needs-grilling label (any case): exempt.
meta_json null '[{"name":"bug"},{"name":"Needs-Grilling"}]'; fresh_fields; reset_log
OUT=$(card move 232 Building --lane build --model Sonnet 2>&1) || fail "needs-grilling Building failed: $OUT"
echo "$OUT" | grep -q 'no milestone' && fail "needs-grilling must not warn: $OUT"
# 47d — the issue is the config's roadmap.issue: exempt.
meta_json null '[{"name":"bug"}]'; fresh_fields; reset_log
OUT=$(card --config "$TMP/cfg-road.json" move 232 Building --lane build --model Sonnet 2>&1) || fail "roadmap Building failed: $OUT"
echo "$OUT" | grep -q 'no milestone' && fail "roadmap.issue must not warn: $OUT"
# 47e — QA does not warn.
meta_json null '[{"name":"bug"}]'; fresh_fields; reset_log
OUT=$(card move 232 QA --lane qa --model Sonnet 2>&1) || fail "QA move failed: $OUT"
echo "$OUT" | grep -q 'no milestone' && fail "QA must not warn: $OUT"
ok "no milestone: Building/Review warn (exit 0, mutated); milestone, needs-grilling, roadmap.issue and QA stay quiet"

echo "PASS: $pass card-helper checks"
