#!/usr/bin/env bash
# SessionStart: halt the board if the session is not the robot GitHub identity.
# Hardened over time and generalised: the expected login is the machine-user
# `notifications.bot_identity` of the board config(s) under .claude/super-board/configs/.
# Nothing to check (and nothing printed) when no config names a robot login: "auto", empty or a
# GitHub App ("...[bot]") identity is not something `gh api user` can verify.
# Prints nothing and exits 0 when fine. Never prints token values. A mismatch (wrong login, token env var,
# gh missing/unknown login) exits 0 with a SessionStart JSON `additionalContext` carrying the halt message (so the
# model sees it) AND writes the marker .claude/super-board/identity-halt (expected vs actual, time); guard-gh-identity.py
# denies every gh call while the marker exists. A matching login at a later SessionStart removes it. A python3 that
# does not run exits 2 with the halt message on stdout and stderr.
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || true

# Every guard hook is a `python3` script. If python3 does not run (the Microsoft Store stub on Windows
# answers 'command not found' / opens the Store), the hooks exit with an error Claude Code treats as
# non-blocking, i.e. the guards are silently off. Halt loudly instead: non-zero, message on both streams.
if ! python3 -c 1 >/dev/null 2>&1; then
  MSG="MISSION CONTROL HALT: python3 is not runnable here (a Microsoft Store stub?), so the guard hooks cannot run. Install Python 3 or fix PATH and restart. Do not run the board; tell the owner."
  echo "$MSG"
  echo "$MSG" >&2
  exit 2
fi

EXPECTED=""
for CFG in .claude/super-board/configs/*.json; do
  [ -f "$CFG" ] || continue
  if command -v jq >/dev/null 2>&1; then
    V="$(jq -r '.notifications.bot_identity // empty' "$CFG" 2>/dev/null)"
  else  # no jq: python3 (checked above) reads it
    V="$(python3 -c 'import json,sys
try:
    v=((json.load(open(sys.argv[1],encoding="utf-8")).get("notifications") or {}).get("bot_identity") or "")
    print(v if isinstance(v,str) else "")
except Exception:
    pass' "$CFG" 2>/dev/null)"
  fi
  V="$(printf '%s' "$V" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  case "$V" in ""|[Aa][Uu][Tt][Oo]|*"[bot]"|*"[BOT]") continue ;; esac
  EXPECTED="$V"
  break
done
[ -n "$EXPECTED" ] || exit 0

MARKER=.claude/super-board/identity-halt
halt() {
  MSG="MISSION CONTROL HALT: wrong GitHub identity ($1), expected $EXPECTED. Do not run the board; tell the owner."
  mkdir -p "$(dirname "$MARKER")" 2>/dev/null
  printf 'expected=%s\nactual=%s\ntime=%s\n' "$EXPECTED" "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$MARKER" 2>/dev/null
  if command -v jq >/dev/null 2>&1; then
    jq -cn --arg m "$MSG" '{hookSpecificOutput:{hookEventName:"SessionStart",additionalContext:$m}}'
  else
    python3 -c 'import json,sys; print(json.dumps({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":sys.argv[1]}}))' "$MSG"
  fi
  exit 0
}

if [ -n "${GH_TOKEN:-}" ] || [ -n "${GITHUB_TOKEN:-}" ]; then
  halt "token env var set"
fi

command -v gh >/dev/null 2>&1 || halt "gh not found"

TO=""
[ -x /usr/bin/timeout ] && TO="/usr/bin/timeout 10"
LOGIN="$($TO gh api user -q .login 2>/dev/null | tr -d '\r')"
[ -n "$LOGIN" ] || halt "unknown"
# GitHub logins are case-insensitive.
[ "$(printf '%s' "$LOGIN" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$EXPECTED" | tr '[:upper:]' '[:lower:]')" ] || halt "$LOGIN"
rm -f "$MARKER" 2>/dev/null
exit 0
