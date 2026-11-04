#!/usr/bin/env bash
# tests/mission-control-hook.sh — hook prints CORE.md only when the marker exists.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOK="$ROOT/hooks/mission-control-start.sh"
export CLAUDE_PLUGIN_ROOT="$ROOT"
tmp="$(mktemp -d)"; fail=0

export CLAUDE_PROJECT_DIR="$tmp"
out="$(bash "$HOOK")"; rc=$?
{ [ $rc -eq 0 ] && [ -z "$out" ]; } || { echo "FAIL: no marker should print nothing, exit 0"; fail=1; }

touch "$tmp/.mission-control"
out="$(bash "$HOOK")"; rc=$?
{ [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "MISSION CONTROL ACTIVE"; } || { echo "FAIL: marker should print CORE.md"; fail=1; }
printf '%s' "$out" | grep -q "$ROOT/skills/mission-control/SKILL.md" || { echo "FAIL: absolute SKILL.md path missing"; fail=1; }

# (a) CLAUDE_PROJECT_DIR unset, cwd = temp dir without marker -> silent, exit 0
tmp2="$(mktemp -d)"
out="$(cd "$tmp2" && env -u CLAUDE_PROJECT_DIR bash "$HOOK")"; rc=$?
{ [ $rc -eq 0 ] && [ -z "$out" ]; } || { echo "FAIL: unset CLAUDE_PROJECT_DIR without marker should print nothing, exit 0"; fail=1; }

# (b) .mission-control is a directory, not a file -> silent, exit 0
tmp3="$(mktemp -d)"; mkdir "$tmp3/.mission-control"
out="$(CLAUDE_PROJECT_DIR="$tmp3" bash "$HOOK")"; rc=$?
{ [ $rc -eq 0 ] && [ -z "$out" ]; } || { echo "FAIL: .mission-control directory should print nothing, exit 0"; fail=1; }

# Windows form: backslash paths with a space (production CLAUDE_PROJECT_DIR / CLAUDE_PLUGIN_ROOT).
if command -v cygpath >/dev/null 2>&1; then
  tmp4="$(mktemp -d)/A B"; mkdir -p "$tmp4"
  out="$(CLAUDE_PROJECT_DIR="$(cygpath -w "$tmp4")" CLAUDE_PLUGIN_ROOT="$(cygpath -w "$ROOT")" bash "$HOOK")"; rc=$?
  { [ $rc -eq 0 ] && [ -z "$out" ]; } || { echo "FAIL: backslash path without marker should print nothing"; fail=1; }
  touch "$tmp4/.mission-control"
  out="$(CLAUDE_PROJECT_DIR="$(cygpath -w "$tmp4")" CLAUDE_PLUGIN_ROOT="$(cygpath -w "$ROOT")" bash "$HOOK")"; rc=$?
  { [ $rc -eq 0 ] && printf '%s' "$out" | grep -q "MISSION CONTROL ACTIVE"; } || { echo "FAIL: backslash path with marker should print CORE.md"; fail=1; }
  rm -rf "$(dirname "$tmp4")"
fi

rm -rf "$tmp" "$tmp2" "$tmp3"; [ $fail -eq 0 ] && echo "PASS"; exit $fail
