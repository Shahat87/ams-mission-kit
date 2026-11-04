#!/usr/bin/env bash
# W41: the run loop must not stop while a Blocked card's blockers have all closed.
# Eval run 20261102T143229Z: pre-flight sequenced two cards behind a Ready card, that card merged,
# and the orchestrator stopped ("re-run to free them") before the next wave-start sweep could
# return them to Ready. Two halves:
#   1. the planner counts a closed-blocker Blocked card as sweepable work even when the board
#      is otherwise only Done cards (script contract the docs rely on);
#   2. the docs say: sweep before stopping, and loop if the sweep freed anything.
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }
PLAN=scripts/super-board-wave-plan.sh
CFG=tests/fixtures/wave-config.json
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

card() { # number status
  printf '{"status":"%s","content":{"type":"Issue","number":%s,"title":"c%s","assignees":[],"labels":[]}}' "$2" "$1" "$1"
}
dep() { # number openBlockers(json) blockers(json) runnable
  printf '"%s":{"number":%s,"title":"c%s","state":"OPEN","blockers":%s,"openBlockers":%s,"parseable":true,"runnable":%s,"why":""}' "$1" "$1" "$1" "$3" "$2" "$4"
}

# Board: only Done cards plus Blocked #30 (blocker #9 closed), #31 (blocker #12 still open),
# #32 (blocked-by: -, human-gated).
echo "{\"items\":[$(card 20 Done),$(card 21 Done),$(card 30 Blocked),$(card 31 Blocked),$(card 32 Blocked)]}" > "$T/items.json"
echo "{$(dep 30 '[]' '[9]' true),$(dep 31 '[12]' '[12]' false),$(dep 32 '[]' '[]' false)}" > "$T/deps.json"
OUT=$("$PLAN" --config <(jq 'del(.max_workers)' "$CFG") --items "$T/items.json" --deps "$T/deps.json")

echo "$OUT" | jq -e '.cards == []' >/dev/null || fail "no card is dispatchable from Blocked/Done, got: $OUT"
echo "$OUT" | jq -e '[.sweep[].number] == [30]' >/dev/null \
  || fail "only #30 (closed blocker) is sweepable; #31 (open blocker) and #32 (blocked-by: -) are not, got: $(echo "$OUT" | jq -c .sweep)"

# Board with no sweepable card: the loop may stop.
echo "{\"items\":[$(card 20 Done),$(card 31 Blocked),$(card 32 Blocked)]}" > "$T/items2.json"
OUT2=$("$PLAN" --config <(jq 'del(.max_workers)' "$CFG") --items "$T/items2.json" --deps "$T/deps.json")
echo "$OUT2" | jq -e '.cards == [] and .sweep == []' >/dev/null || fail "open-blocker and human-gated cards must not be sweepable, got: $OUT2"

# Docs: the same sentence in run.md and in run-workflow.md step 8.
RW=skills/super-board/references/run-workflow.md
RUN=skills/super-board/references/run.md
RULE='Before stopping because nothing moved or the board is drained, run the wave-start sweep; if it frees any card, loop to step 1.'
for f in "$RW" "$RUN"; do
  [ -f "$f" ] || fail "$f missing"
  grep -qF -- "$RULE" "$f" || fail "$f lost the sweep-before-stop rule: $RULE"
done
# Step 8 itself carries the rule, and the stale "Blocked only -> report and stop" line is conditioned on the sweep.
awk '/^8\. Loop to 1/{p=1} p&&/Before stopping because nothing moved/{f=1} /^After normal completion/{p=0} END{exit f?0:1}' "$RW" \
  || fail "$RW step 8 must carry the sweep-before-stop rule"
grep -qF 'sweep freed nothing' "$RW" || fail "$RW step 2 must stop on Blocked-only only after a sweep that freed nothing"
# Pre-flight never sequences behind a closed issue, so a swept card cannot be re-held on the blocker that freed it.
for f in "$RW" "$RUN"; do
  grep -qF 'Pre-flight never sequences a card behind a closed issue (`super-board-preflight.sh` keeps only open issues in `blockedBy`), so a swept card cannot be re-held on the blocker that freed it.' "$f" || fail "$f lost the pre-flight closed-issue guard"
  grep -qF 'counts as freed only when its status re-reads as `Ready`' "$f" || fail "$f lost the freed definition"
  ! grep -qF 'never frees a card twice' "$f" || fail "$f still carries the old same-blockers prose guard"
done
# Halt-gate backstop: a wave whose cards were all held or sequenced by pre-flight made no progress.
# (run.md does not restate this gate; run-workflow.md step 7 is the only place.)
grep -qF 'every card bounced, failed or was held or sequenced by pre-flight' "$RW" || fail "$RW halt gate must count pre-flight holds and sequences as no progress"
! grep -qF 'every card bounced/failed' "$RW" || fail "$RW still has the old zero-progress wording"
echo "ok"
