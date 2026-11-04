#!/usr/bin/env bash
# super-board-watchdog-install.sh — schedule the external watchdog (#68).
#
# Usage:
#   super-board-watchdog-install.sh --config <repo>/.claude/super-board/configs/<slug>.json
#                                    [--print|--apply|--remove]
#
# --print (the default) prints the exact schtasks.exe /Create line and runs
#   nothing. The printed form is cmd.exe-ready (inner quotes escaped as \").
# --apply registers the task with schtasks.exe. Refuses (exit 78) while
#   watchdog.enabled is false in the config: the operator opts in deliberately.
# --remove prints the exact schtasks.exe /Delete line; like --print it never
#   runs schtasks — run the printed line yourself (or hand it to cmd.exe).
#
# The task runs every poll_minutes minutes as the CURRENT user (no /RU):
#   <bash.exe> -lc 'cd <repo> && <pinned python> -I .claude/bin/super-board-watchdog.py --config <cfg>'
# The interpreter is the one pinned in ${USERPROFILE:-$HOME}/.mission-control/python
# (written by install.sh), else python3/python resolved the same way install.sh
# resolves it (skipping the Microsoft Store stub). Run install.sh first so
# .claude/bin/super-board-watchdog.py exists in the target repo.
#
# Exit codes: 0 ok · 64 bad --config path/slug, missing jq/python/schtasks, or
# a repo/config/pinned-python path containing a quote · 78 --apply while
# watchdog.enabled is false. Never reads tokens, .env or hosts.yml, and never
# runs gh: this script only reads the config with jq.
set -euo pipefail

MODE="print"
CONFIG=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --print)  MODE="print" ;;
    --apply)  MODE="apply" ;;
    --remove) MODE="remove" ;;
    --config)
      [ "$#" -gt 1 ] || { echo "--config needs a path" >&2; exit 64; }
      CONFIG="$2"; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
  shift
done
[ -n "$CONFIG" ] || { echo "--config <path> is required" >&2; exit 64; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 64; }
[ -f "$CONFIG" ] || { echo "config not found: $CONFIG" >&2; exit 64; }

CONFIG_DIR=$(cd "$(dirname "$CONFIG")" && pwd) || exit 64
CONFIG_ABS="$CONFIG_DIR/$(basename "$CONFIG")"
SLUG=$(basename "$CONFIG_ABS")
SLUG="${SLUG%.json}"
case "$SLUG" in
  .|..) echo "invalid slug: $SLUG" >&2; exit 64 ;;
esac
case "$SLUG" in
  *[!A-Za-z0-9._-]*) echo "invalid slug: $SLUG (use letters, digits, dot, dash, underscore)" >&2; exit 64 ;;
esac
# The config must live where the watchdog expects the repo root.
[ "$(basename "$CONFIG_DIR")" = "configs" ] || { echo "the config must live at <repo>/.claude/super-board/configs/" >&2; exit 64; }
SB_DIR=$(dirname "$CONFIG_DIR")
CLAUDE_DIR=$(dirname "$SB_DIR")
REPO=$(dirname "$CLAUDE_DIR")
[ "$(basename "$SB_DIR")" = "super-board" ] && [ "$(basename "$CLAUDE_DIR")" = ".claude" ] \
  || { echo "the config must live at <repo>/.claude/super-board/configs/" >&2; exit 64; }
[ -f "$REPO/.claude/bin/super-board-watchdog.py" ] \
  || { echo "super-board-watchdog.py is not installed in $REPO/.claude/bin — run install.sh first" >&2; exit 64; }

# A single or double quote in the repo or config path would break out of the
# `bash -lc '...'` quoting inside the /TR value and inject into the scheduled
# command. The paths are operator-local, not untrusted GitHub text, so the
# blast radius is self-injection only — but the right answer is still: refuse.
# Checked on the raw paths, before any native tool has to open them.
for QUOTED_PATH in "$REPO" "$CONFIG_ABS"; do
  case "$QUOTED_PATH" in
    *"'"*|*'"'*)
      echo "refusing a path containing a quote (it would break the bash -lc quoting): $QUOTED_PATH" >&2
      exit 64 ;;
  esac
done

ENABLED=$(jq -r '.watchdog.enabled // false' "$CONFIG_ABS")
POLL=$(jq -r '.watchdog.poll_minutes // 10' "$CONFIG_ABS")
case "$POLL" in
  ""|*[!0-9]*|0) echo "watchdog.poll_minutes must be a positive integer, got: $POLL" >&2; exit 64 ;;
esac

TO_MIX() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
REPO_M=$(TO_MIX "$REPO")
CFG_M=$(TO_MIX "$CONFIG_ABS")

# The pinned interpreter, exactly the install.sh pattern (install.sh:190-205):
# ${USERPROFILE:-$HOME}/.mission-control/python wins, else resolve python3/python
# skipping the Microsoft Store stub and interpreters that do not run.
MC_HOME="${USERPROFILE:-$HOME}/.mission-control"
PIN=""
if [ -f "$MC_HOME/python" ]; then
  PIN=$(tr -d '\r' < "$MC_HOME/python" || true)
fi
if [ -z "$PIN" ] || [ ! -e "$PIN" ]; then
  is_store_stub() {
    case "$1" in
      *[/\\][Mm][Ii][Cc][Rr][Oo][Ss][Oo][Ff][Tt][/\\][Ww][Ii][Nn][Dd][Oo][Ww][Ss][Aa][Pp][Pp][Ss][/\\]*) return 0 ;;
    esac
    return 1
  }
  PIN=""
  for cand in python3 python; do
    py=$("$cand" -c 'import os,sys;print(os.path.realpath(sys.executable).replace(chr(92),"/"))' 2>/dev/null) || py=""
    py="${py%$'\r'}"
    if [ -n "$py" ] && ! is_store_stub "$py"; then PIN="$py"; break; fi
  done
fi
[ -n "$PIN" ] || { echo "no usable Python to pin (python3 is missing or the Microsoft Store stub)" >&2; exit 64; }
case "$PIN" in
  *"'"*|*'"'*)
    echo "refusing a pinned-python path containing a quote (it would break the bash -lc quoting): $PIN" >&2
    exit 64 ;;
esac
PIN_M=$(TO_MIX "$PIN")
BASH_P=$(command -v bash) || { echo "bash not found" >&2; exit 64; }
BASH_P=$(TO_MIX "$BASH_P")

TN="super-board-watchdog-$SLUG"
# One argv value for /TR: "bash" -lc 'cd "<repo>" && "<python>" -I .claude/bin/super-board-watchdog.py --config "<cfg>"'
SQ="'"
TR="\"${BASH_P}\" -lc ${SQ}cd \"${REPO_M}\" && \"${PIN_M}\" -I .claude/bin/super-board-watchdog.py --config \"${CFG_M}\"${SQ}"

case "$MODE" in
  print)
    TR_ESC=${TR//\"/\\\"}
    echo "schtasks.exe /Create /F /SC MINUTE /MO ${POLL} /TN ${TN} /TR \"${TR_ESC}\""
    ;;
  remove)
    echo "schtasks.exe /Delete /TN ${TN} /F"
    ;;
  apply)
    [ "$ENABLED" = "true" ] || { echo "watchdog.enabled is false in ${CONFIG_ABS} — set it true first" >&2; exit 78; }
    SCHTASKS=$(command -v schtasks || command -v schtasks.exe || true)
    [ -n "$SCHTASKS" ] || { echo "schtasks not found on PATH" >&2; exit 64; }
    # MSYS2_ARG_CONV_EXCL keeps Git Bash from rewriting the /flags into paths.
    MSYS2_ARG_CONV_EXCL="*" MSYS_NO_PATHCONV=1 "$SCHTASKS" /Create /F /SC MINUTE /MO "$POLL" /TN "$TN" /TR "$TR"
    echo "✓ scheduled ${TN} every ${POLL} min — the watchdog reports to .claude/super-board/watchdog/${SLUG}/"
    ;;
esac
