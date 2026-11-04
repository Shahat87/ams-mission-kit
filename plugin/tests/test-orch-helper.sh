#!/usr/bin/env bash
# Tests scripts/super-board-orch.sh — the orchestrator's fixed-purpose helper (issue #69).
# Deterministic, offline: gh is a stub on PATH, the board is a temp dir. Runs in well under 2s.
#
#   bash tests/test-orch-helper.sh
set -uo pipefail
cd "$(dirname "$0")"

PACK="$(cd .. && pwd)"
HELPER="$PACK/scripts/super-board-orch.sh"
THROTTLE="$PACK/scripts/super-board-throttle.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

PASS=0
ok() { PASS=$((PASS + 1)); printf '  ✅ %s\n' "$1"; }
fail() { echo "FAIL: $1" >&2; exit 1; }

[ -f "$HELPER" ] || fail "scripts/super-board-orch.sh is missing"
[ -f "$THROTTLE" ] || fail "scripts/super-board-throttle.sh is missing (reconcile calls it)"

# gh stub: a healthy quota, so throttle never "hits" and never reaches the network.
STUB="$(mktemp -d)"
cat > "$STUB/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' '{"resources":{"graphql":{"remaining":4500}},"resources":{"core":{"remaining":4500}}}'
exit 0
STUB
chmod +x "$STUB/gh"
export PATH="$STUB:$PATH"

orc() { ( cd "$T" && bash "$HELPER" "$@" ); }   # the helper always runs from the project root
LOCK="$T/.claude/super-board/inflight/workflow-wave.lock"

mkdir -p "$T/.claude/super-board/inflight" "$T/.claude/super-board/configs" "$T/.claude/workflows"

echo "── lock-acquire / lock-release (the wave marker, run-workflow.md precondition 5a)"
orc lock-acquire pslug >/dev/null; rc=$?
[ "$rc" -eq 0 ] || fail "lock-acquire failed (rc $rc)"
[ -f "$LOCK" ] || fail "lock-acquire created no lock"
grep -q '^SLUG=pslug$' "$LOCK" || fail "the lock does not record the slug"
grep -qE '^STARTED=[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$' "$LOCK" || fail "the lock does not record a UTC start time"
ok "lock-acquire writes SLUG and a UTC STARTED"

orc lock-acquire pslug >/dev/null 2>&1; rc=$?
[ "$rc" -eq 3 ] || fail "a second lock-acquire must exit 3, got $rc"
BEFORE="$(cat "$LOCK")"
orc lock-acquire other >/dev/null 2>&1; rc=$?
[ "$rc" -eq 3 ] || fail "a lock-acquire for another slug must exit 3, got $rc"
[ "$(cat "$LOCK")" = "$BEFORE" ] || fail "a refused lock-acquire changed the lock"
ok "lock exclusivity: a held lock refuses every new acquire (exit 3)"

printf 'sess-orch\n' > "$T/.claude/super-board/inflight/orchestrator-session"
orc lock-release wrongslug "$(sed -n 's/^STARTED=//p' "$LOCK")" >/dev/null 2>&1; rc=$?
[ "$rc" -eq 1 ] && [ -f "$LOCK" ] || fail "a wrong-slug release must exit 1 and keep the lock (rc $rc)"
orc lock-release pslug 1999-01-01T00:00:00Z >/dev/null 2>&1; rc=$?
[ "$rc" -eq 1 ] && [ -f "$LOCK" ] || fail "a wrong-started release must exit 1 and keep the lock (rc $rc)"
[ -f "$T/.claude/super-board/inflight/orchestrator-session" ] || fail "a refused release must keep the session binding"
ok "a release with a mismatched slug or start time keeps the lock and the session binding"

orc lock-release pslug "$(sed -n 's/^STARTED=//p' "$LOCK")" >/dev/null; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$LOCK" ] || fail "the matching release must remove the lock (rc $rc)"
[ ! -e "$T/.claude/super-board/inflight/orchestrator-session" ] || fail "the matching release must delete the session binding"
orc lock-release pslug 1999-01-01T00:00:00Z >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] || fail "releasing with no lock present is a no-op (rc $rc)"
ok "the matching release removes lock and session binding; releasing nothing is a no-op"

printf 'sess-crashed\n' > "$T/.claude/super-board/inflight/orchestrator-session"
orc lock-release >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$T/.claude/super-board/inflight/orchestrator-session" ] \
  || fail "a bare release with no lock must clear a leftover session binding (rc $rc)"
printf 'sess-crashed\n' > "$T/.claude/super-board/inflight/orchestrator-session"
orc lock-release anything 2026-01-01T00:00:00Z >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && [ ! -e "$T/.claude/super-board/inflight/orchestrator-session" ] \
  || fail "a release with no lock must clear a leftover session binding whatever the arguments (rc $rc)"
ok "a release with no lock clears the crashed run's leftover session binding"

echo
echo "── usage errors (bad slugs, bad numbers, unknown subcommands exit 64)"
for bad in 'lock-acquire bad/slug' 'lock-acquire "a b"' 'lock-release pslug' \
           'lock-release bad/slug 2026-01-01T00:00:00Z' 'lock-release pslug "a;b"' 'frobnicate' '' \
           'reconcile --config c.json --wave nope --date-slug d' 'reconcile --config c.json --wave 12345678 --date-slug d' \
           'reconcile --wave 1 --date-slug d' 'reconcile --config c.json --wave 1' \
           'db-check "bad env"' 'check-wave --wat'; do
  # shellcheck disable=SC2086
  orc $bad >/dev/null 2>&1; rc=$?
  [ "$rc" -eq 64 ] || fail "expected exit 64 for: $bad (got $rc)"
done
ok "a bad slug, number, subcommand or missing argument exits 64"

echo
echo "── db-check (the command comes from the protected config, never from an argument)"
cat > "$T/.claude/super-board/configs/p.json" <<'JSON'
{"migrations":{"allowed_envs":["test","staging","qa"],
  "commands":{"test":"npm run db:migrate -- --env test","staging":"supabase db push","qa":"npm run db:migrate -- --env qa"},
  "checks":{"test":"echo db-check-ok","staging":"false"}}}
JSON
OUT="$(orc db-check --config .claude/super-board/configs/p.json test extra ignored 'echo PWNED'; rc=$?; printf 'rc=%s' "$rc")"
case "$OUT" in
  *"db-check-ok"*"rc=0"*) ;;
  *) fail "db-check should run the configured check and ignore extra arguments, got: $OUT" ;;
esac
case "$OUT" in *PWNED*) fail "db-check must never run a command taken from an argument" ;; esac
ok "db-check runs migrations.checks from the config and ignores extra arguments"

orc db-check --config .claude/super-board/configs/p.json live >/dev/null 2>&1; rc=$?
[ "$rc" -eq 64 ] || fail "an env outside migrations.allowed_envs must exit 64, got $rc"
orc db-check --config .claude/super-board/configs/p.json staging >/dev/null 2>&1; rc=$?
[ "$rc" -ne 0 ] || fail "a failing configured check must fail db-check (rc 0)"
orc db-check --config .claude/super-board/configs/p.json qa >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] || fail "a command with no configured check says so once and goes on (rc $rc)"
orc db-check --config .claude/super-board/configs/nope.json test >/dev/null 2>&1; rc=$?
[ "$rc" -eq 66 ] || fail "a missing config under configs/ must exit 66, got $rc"
printf 'p\n' > "$T/.claude/super-board/active"
orc db-check test >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] || fail "db-check without --config must resolve .claude/super-board/active (rc $rc)"
ok "db-check: allowed_envs enforced, check failures propagate, active pointer resolves"

for bad_cfg in /tmp/sb-evil.json docs/elsewhere.json .claude/super-board/active \
               .claude/super-board/configs/../configs/p.json; do
  orc db-check --config "$bad_cfg" test >/dev/null 2>&1; rc=$?
  [ "$rc" -eq 64 ] || fail "--config outside .claude/super-board/configs must exit 64: $bad_cfg (got $rc)"
done
orc db-check --config "$T/.claude/super-board/configs/p.json" test >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] || fail "the project-anchored configs path stays allowed (rc $rc)"
printf 'p extra\n' > "$T/.claude/super-board/active"
orc db-check test >/dev/null 2>&1; rc=$?
[ "$rc" -eq 64 ] || fail "an active file that is not one slug must exit 64, got $rc"
printf '../p\n' > "$T/.claude/super-board/active"
orc db-check test >/dev/null 2>&1; rc=$?
[ "$rc" -eq 64 ] || fail "an active file holding a path must exit 64, got $rc"
printf 'p\n' > "$T/.claude/super-board/active"
ok "db-check: --config only under .claude/super-board/configs/, the active pointer must be one slug"

echo
echo "── reconcile (cards JSON on stdin → manifest rows + throttle)"
printf '{"max_workers":3}\n' > "$T/.claude/super-board/configs/p.json"
rm -f "$T/.claude/super-board/active"
CARDS='{"cards":[{"number":5,"lanesRun":"build:pass → qa:pass","finalStatus":"pass","column":"Done","detail":"PR #9 | merged"},
  {"number":7,"lanesRun":"none","finalStatus":"failed","column":"Ready","detail":"no lane ran"},
  {"number":9,"lanesRun":"build|pass\r\nqa:pass","finalStatus":"pa|ss","column":"Do|ne\r","detail":"multi\nline | row"}]}'
OUT="$(printf '%s' "$CARDS" | orc reconcile --config .claude/super-board/configs/p.json --wave 3 --date-slug 2026-01-01-pslug)"; rc=$?
[ "$rc" -eq 0 ] || fail "reconcile failed (rc $rc): $OUT"
printf '%s' "$OUT" | grep -q '"hit":false' || fail "reconcile must print the throttle JSON line, got: $OUT"
M="$T/docs/super-board/runs/2026-01-01-pslug.md"
[ -f "$M" ] || fail "reconcile wrote no manifest at docs/super-board/runs"
[ "$(grep -c '^| #' "$M")" -eq 3 ] || fail "the manifest needs one row per card: $(cat "$M")"
grep -qF '| #5 | build:pass → qa:pass | pass | Done | PR #9 \| merged |' "$M" || fail "row for the merged card is wrong: $(cat "$M")"
grep -qF '| #7 | none | failed | Ready | no lane ran |' "$M" || fail "row for the failed card is wrong: $(cat "$M")"
grep -qF '| #9 | build\|pass  qa:pass | pa\|ss | Do\|ne  | multi line \| row |' "$M" \
  || fail "every field must escape | and fold CR/LF: $(cat "$M")"
printf '%s' "$CARDS" | orc reconcile --config .claude/super-board/configs/p.json --wave 3 --date-slug 2026-01-01-pslug >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] && [ "$(grep -c '^| #' "$M")" -eq 6 ] || fail "a second reconcile appends, never rewrites (rc $rc)"
ok "reconcile appends one manifest row per card (every field escaped) and calls throttle"

printf '%s' '{"nope":1}' | orc reconcile --config .claude/super-board/configs/p.json --wave 3 --date-slug d >/dev/null 2>&1; rc=$?
[ "$rc" -eq 64 ] || fail "stdin that is not the cards JSON must exit 64, got $rc"
printf '%s' "$CARDS" | orc reconcile --config /nope/missing.json --wave 3 --date-slug d >/dev/null 2>&1; rc=$?
[ "$rc" -eq 66 ] || fail "a missing config must exit 66, got $rc"
ok "reconcile: bad stdin exits 64, a missing config exits 66"

echo
echo "── check-wave (the wave checks, run-workflow.md preconditions 4 and 5b)"
cp "$PACK/workflows/super-board-wave.js" "$T/.claude/workflows/super-board-wave.js"
orc check-wave >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] || fail "check-wave must pass on the shipped wave script (rc $rc)"
printf '\nthis is not ( valid javascript {\n' >> "$T/.claude/workflows/super-board-wave.js"
orc check-wave >/dev/null 2>&1; rc=$?
[ "$rc" -eq 1 ] || fail "check-wave must fail on a wave script that does not parse (rc $rc)"
cp "$PACK/workflows/super-board-wave.js" "$T/.claude/workflows/super-board-wave.js"
orc check-wave >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] || fail "check-wave must pass again on the repaired script (rc $rc)"
ok "check-wave: node --check on the wave script gates the launch"

printf '%s\n' "$$" > "$T/.claude/super-board/codex-wave.pid"
orc check-wave >/dev/null 2>&1; rc=$?
[ "$rc" -eq 1 ] || fail "check-wave must fail while the codex wave pid is alive (rc $rc)"
rm -f "$T/.claude/super-board/codex-wave.pid"
orc check-wave >/dev/null 2>&1; rc=$?
[ "$rc" -eq 0 ] || fail "check-wave must pass once the codex wave pid is gone (rc $rc)"
ok "check-wave: a live codex-wave pid fails the launch check"

if command -v pgrep >/dev/null 2>&1; then
  bash -c 'exec -a super-board-run.sh sleep 5' &
  FAKE=$!
  sleep 0.3
  orc check-wave >/dev/null 2>&1; rc=$?
  kill "$FAKE" 2>/dev/null; wait "$FAKE" 2>/dev/null
  [ "$rc" -eq 1 ] || fail "check-wave must fail while a legacy super-board-run.sh process is active (rc $rc)"
  ok "check-wave: a live legacy dispatcher fails the launch check"
  bash -c 'exec -a super-board-codex-lane.sh sleep 5' &
  FAKE=$!
  sleep 0.3
  orc check-wave >/dev/null 2>&1; rc=$?
  kill "$FAKE" 2>/dev/null; wait "$FAKE" 2>/dev/null
  [ "$rc" -eq 1 ] || fail "check-wave must fail while a repo-local super-board-codex-lane.sh process remains (rc $rc)"
  bash -c 'exec -a super-board-codex-wave.sh sleep 5' &
  FAKE=$!
  sleep 0.3
  orc check-wave >/dev/null 2>&1; rc=$?
  kill "$FAKE" 2>/dev/null; wait "$FAKE" 2>/dev/null
  [ "$rc" -eq 1 ] || fail "check-wave must fail while a repo-local super-board-codex-wave.sh process remains (rc $rc)"
  ok "check-wave: a live repo-local codex wave/lane process fails the launch check"
else
  echo "  ⚠️  pgrep not available here — the legacy-process check cannot run; skipped"
fi

echo
printf '\n%s passed\n' "$PASS"
[ "$PASS" -gt 0 ]
