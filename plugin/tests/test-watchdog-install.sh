#!/usr/bin/env bash
# Offline tests for super-board-watchdog-install.sh (issue #68).
#
# A stub `schtasks` on PATH records every invocation; the --print/--remove
# modes must never call it. No real scheduled task is ever touched, no gh is
# run, no network. Run directly: bash tests/test-watchdog-install.sh
set -uo pipefail
cd "$(dirname "$0")"
INST="../scripts/super-board-watchdog-install.sh"
FIX=fixtures/watchdog

fail() { echo "FAIL: $1" >&2; exit 1; }
command -v jq >/dev/null || fail "jq is required"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

# The stub schtasks: log the argv, exit 0. Named without .exe so Git Bash
# resolves it via `command -v schtasks` before any real schtasks.exe.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/schtasks" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SCHTASKS_LOG"
exit 0
EOF
chmod +x "$TMP/bin/schtasks"
export SCHTASKS_LOG="$TMP/schtasks.log"
export PATH="$TMP/bin:$PATH"

# Stage the target layout the installer expects (i.e. after install.sh ran):
#   <repo>/.claude/bin/super-board-watchdog.py
#   <repo>/.claude/super-board/configs/<slug>.json
REPO="$TMP/repo"
mkdir -p "$REPO/.claude/super-board/configs" "$REPO/.claude/bin"
cp "$FIX/wave-config.json" "$REPO/.claude/super-board/configs/wdtest.json"
printf '#!/usr/bin/env python3\n' > "$REPO/.claude/bin/super-board-watchdog.py"
CFG="$REPO/.claude/super-board/configs/wdtest.json"

# ── 1 ── the exact --print line ────────────────────────────────────────
OUT=$(bash "$INST" --config "$CFG" --print) || fail "--print should exit 0"
[ "$(printf '%s\n' "$OUT" | wc -l)" -eq 1 ] || fail "--print must print exactly one line, got: $OUT"
case "$OUT" in
  'schtasks.exe /Create /F /SC MINUTE /MO 10 /TN super-board-watchdog-wdtest /TR "'*)
    ;;
  *) fail "line must start with the /Create skeleton, got: $OUT" ;;
esac
printf '%s' "$OUT" | grep -q -- '-lc' || fail "the task must run through bash -lc"
printf '%s' "$OUT" | grep -q 'watchdog\.py --config' || fail "the task must call the watchdog script"
printf '%s' "$OUT" | grep -qF "$(win "$REPO")" || fail "the repo path must be in the command"
printf '%s' "$OUT" | grep -qF "$(win "$CFG")" || fail "the config path must be in the command"
printf '%s' "$OUT" | grep -q '\-I ' || fail "python must run isolated (-I)"
[[ "$OUT" == *\'\" ]] || fail "the /TR value must end the quoted bash command, got: $OUT"
[ ! -e "$SCHTASKS_LOG" ] || fail "--print must never invoke schtasks"
OUT2=$(bash "$INST" --config "$CFG") || fail "default mode should be --print"
[ "$OUT" = "$OUT2" ] || fail "--print must be deterministic"

# ── 2 ── exit 64 for a bad slug or path ────────────────────────────────
cp "$FIX/wave-config.json" "$REPO/.claude/super-board/configs/bad slug.json"
RC=0; bash "$INST" --config "$REPO/.claude/super-board/configs/bad slug.json" --print >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "a slug with a space should exit 64, got $RC"
RC=0; bash "$INST" --config "$TMP/missing.json" --print >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "a missing config should exit 64, got $RC"
# a config outside .claude/super-board/configs is a bad path too
cp "$FIX/wave-config.json" "$TMP/stray.json"
RC=0; bash "$INST" --config "$TMP/stray.json" --print >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "a config outside configs/ should exit 64, got $RC"
[ ! -e "$SCHTASKS_LOG" ] || fail "nothing so far may invoke schtasks"

# ── 3 ── exit 78 for --apply when enabled is false ─────────────────────
jq '.watchdog.enabled = false' "$FIX/wave-config.json" > "$REPO/.claude/super-board/configs/off.json"
RC=0; bash "$INST" --config "$REPO/.claude/super-board/configs/off.json" --apply >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 78 ] || fail "--apply with enabled false should exit 78, got $RC"
[ ! -e "$SCHTASKS_LOG" ] || fail "a refused --apply must not invoke schtasks"

# ── 4 ── --remove prints the /Delete line ──────────────────────────────
OUT=$(bash "$INST" --config "$CFG" --remove) || fail "--remove should exit 0"
[ "$(printf '%s\n' "$OUT" | wc -l)" -eq 1 ] || fail "--remove must print exactly one line, got: $OUT"
[ "$OUT" = "schtasks.exe /Delete /TN super-board-watchdog-wdtest /F" ] || fail "--remove printed: $OUT"

# ── 5 ── without --apply, schtasks is never invoked ────────────────────
rm -f "$SCHTASKS_LOG"
bash "$INST" --config "$CFG" --print >/dev/null || fail "--print (5) should exit 0"
bash "$INST" --config "$CFG" --remove >/dev/null || fail "--remove (5) should exit 0"
bash "$INST" --config "$CFG" >/dev/null || fail "default mode (5) should exit 0"
[ ! -e "$SCHTASKS_LOG" ] || fail "schtasks ran without --apply: $(cat "$SCHTASKS_LOG")"

# ── 6 ── a quote in an embedded path is refused (exit 64) ──────────────
# A single or double quote in the repo, config or pinned-python path would
# break out of the `bash -lc '...'` quoting inside the /TR value.
REPO_Q="$TMP/repo'q"
mkdir -p "$REPO_Q/.claude/super-board/configs" "$REPO_Q/.claude/bin"
cp "$FIX/wave-config.json" "$REPO_Q/.claude/super-board/configs/wdtest.json"
printf '#!/usr/bin/env python3\n' > "$REPO_Q/.claude/bin/super-board-watchdog.py"
RC=0; bash "$INST" --config "$REPO_Q/.claude/super-board/configs/wdtest.json" --print >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "a repo path containing a single quote should exit 64, got $RC"
[ ! -e "$SCHTASKS_LOG" ] || fail "a refused path must not invoke schtasks"

# the pinned-python path is checked the same way: a fake .mission-control pin
PINHOME="$TMP/pin'home"
PINDIR="$TMP/pin'ed"
mkdir -p "$PINHOME/.mission-control" "$PINDIR"
printf 'stub interpreter' > "$PINDIR/python"
printf '%s' "$PINDIR/python" > "$PINHOME/.mission-control/python"
RC=0; USERPROFILE="$PINHOME" HOME="$PINHOME" bash "$INST" --config "$CFG" --print >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "a pinned-python path containing a single quote should exit 64, got $RC"
[ ! -e "$SCHTASKS_LOG" ] || fail "a refused pin must not invoke schtasks"

echo "PASS: test-watchdog-install.sh (6 cases)"
