#!/usr/bin/env bash
# sb-python.sh <hook-file.py> [args...]: the portable launcher for super-board's Python hooks (spec §15.3 G-3).
#
# Project settings.json is committed to git, so it cannot hold a machine-specific interpreter path. It calls this
# launcher instead; the launcher finds the interpreter that install.sh pinned and runs the hook with `-I`
# (isolated: no PYTHON* env vars, no user site, no script dir or cwd on sys.path).
#
# Interpreter, in order:
#   1. the first line of ${USERPROFILE:-$HOME}/.mission-control/python, when it is an absolute path to an executable file;
#   2. `command -v python3`, then `command -v python`.
# The Microsoft Store stub (a path containing \Microsoft\WindowsApps\ or /Microsoft/WindowsApps/) is never run: it is
# skipped and the next candidate tried (on some machines python3 is the stub and python is real; a pin naming the stub
# counts as no pin). The hook runs as `python -I -X utf8` (-I drops PYTHONUTF8, and non-ASCII paths matter). With
# only the stub, or no Python at all, the launcher prints a message and exits 2: a PreToolUse hook that exits 2 is a
# deny, so a guard that cannot start fails closed instead of silently allowing.
# Only bash builtins run before the final exec, so a broken PATH cannot break the refusal.
set -u

hook="${1:-}"
# $1 must be exactly <letters, digits, _ or -> + ".py"; anything else (a path, "..", no extension) is refused.
name="${hook%.py}"
if [ -z "$hook" ] || [ "$name.py" != "$hook" ] || [ -z "$name" ] || [ -n "${name//[A-Za-z0-9_-]/}" ]; then
  echo "super-board hooks: bad hook name '${hook}' (want <name>.py, letters, digits, - and _ only); refusing" >&2
  exit 2
fi

is_store_stub() {
  case "$1" in
    *[/\\][Mm][Ii][Cc][Rr][Oo][Ss][Oo][Ff][Tt][/\\][Ww][Ii][Nn][Dd][Oo][Ww][Ss][Aa][Pp][Pp][Ss][/\\]*) return 0 ;;
  esac
  return 1
}

# A candidate is taken only when it is not the Store stub; a stub is skipped and the next candidate tried.
py=""
try_py() {
  [ -z "$py" ] || return 0
  [ -n "$1" ] || return 0
  if is_store_stub "$1"; then return 0; fi
  py="$1"
}
pin="${USERPROFILE:-${HOME:-}}/.mission-control/python"
if [ -f "$pin" ]; then
  IFS= read -r line < "$pin" || true
  line="${line%$'\r'}"
  case "$line" in
    /*|[A-Za-z]:[/\\]*) if [ -f "$line" ] && [ -x "$line" ]; then try_py "$line"; fi ;;
  esac
fi
try_py "$(command -v python3 2>/dev/null || true)"
try_py "$(command -v python 2>/dev/null || true)"

if [ -z "$py" ]; then
  echo "super-board hooks: no usable Python (run install.sh to pin one); refusing" >&2
  exit 2
fi

self="${BASH_SOURCE[0]:-$0}"
dir="${self%/*}"
[ "$dir" != "$self" ] || dir="."
exec "$py" -I -X utf8 "$dir/$hook" "${@:2}"
