#!/usr/bin/env bash
# super-board-orch.sh — the fixed-purpose helper for the workflow-backend orchestrator (issue #69).
#
# The orchestrator guard (hooks/guard-orchestrator.py) denies the parent session every Bash command
# that is not one simple allowlisted call while the run's wave lock is held. The orchestrator's own
# compound steps therefore live HERE, where no hook inspects arguments, each behind one validated
# subcommand:
#
#   lock-acquire <slug>                    create .claude/super-board/inflight/workflow-wave.lock
#                                          (SLUG=…, STARTED=…, noclobber); exit 3 when it already exists
#   lock-release [SLUG] [STARTED]          remove the lock only when both match the lock's own lines;
#                                          a mismatch keeps the lock and exits 1. The run's session
#                                          binding (.claude/super-board/inflight/orchestrator-session)
#                                          is deleted with the lock, and a release with NO lock present
#                                          clears any leftover binding from a crashed run (no arguments
#                                          needed) — the fence then cannot outlive its lock
#   check-wave [--codex]                   the wave checks before dispatch (run-workflow.md
#                                          preconditions 4 and 5b): node --check on a wrapped copy of
#                                          .claude/workflows/super-board-wave.js (--codex: codex on PATH
#                                          plus bash -n on both Codex launchers), no legacy
#                                          super-board-run.sh dispatcher, no headless claude -p
#                                          super-board run, no repo-local
#                                          super-board-codex-wave.sh / super-board-codex-lane.sh
#                                          process, no live codex wave pid
#   db-check [--config <cfg>] <env> [xx…]  run migrations.checks[env] read straight from the protected
#                                          config (never from an argument); extra arguments are ignored;
#                                          an env outside migrations.allowed_envs exits 64. --config must
#                                          name a JSON file under .claude/super-board/configs/ (exit 64
#                                          otherwise) and the active pointer must hold exactly one slug
#   reconcile --config <cfg> --wave <wave> --date-slug <date-slug>
#                                          cards JSON on stdin: append one manifest row per card to
#                                          docs/super-board/runs/<date-slug>.md, then run
#                                          super-board-throttle.sh (its JSON lands on stdout)
#
# Slugs match [A-Za-z0-9._-]+ and numbers [0-9]{1,7}. GitHub text never becomes a command token.
# Exit codes: 0 ok · 1 a check failed / identity mismatch · 3 lock exists · 64 usage · 66 config missing.
set -euo pipefail

INFLIGHT=".claude/super-board/inflight"
LOCK="$INFLIGHT/workflow-wave.lock"
ACTIVE=".claude/super-board/active"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "usage: super-board-orch.sh lock-acquire SLUG | lock-release SLUG STARTED | check-wave [--codex]" \
       "| db-check [--config CFG] ENV [ignored…] | reconcile --config CFG --wave WAVE --date-slug DATE-SLUG" >&2
  exit 64
}

slug_ok() { [ -n "${1:-}" ] && printf '%s' "$1" | grep -qE '^[A-Za-z0-9._-]+$'; }
num_ok() { [ -n "${1:-}" ] && [ "${#1}" -le 7 ] && printf '%s' "$1" | grep -qE '^[0-9]+$'; }
started_ok() { [ -n "${1:-}" ] && printf '%s' "$1" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'; }

cfg_under_configs() {
  # True when $1 names a JSON file directly under .claude/super-board/configs/ — the same rule the
  # guard's jq row applies: relative, ./-prefixed, project-anchored or absolute; Windows and MSYS
  # spellings are normalised; any ".." is refused, and so is a subdirectory.
  local p="${1//\\//}" tail
  case "$p" in
    *..*) return 1 ;;
    .claude/super-board/configs/*) tail="${p#.claude/super-board/configs/}" ;;
    */.claude/super-board/configs/*) tail="${p##*/.claude/super-board/configs/}" ;;
    *) return 1 ;;
  esac
  printf '%s' "$tail" | grep -qE '^[A-Za-z0-9._-]+\.json$'
}

cmd_lock_acquire() {
  [ $# -ge 1 ] || usage
  slug_ok "$1" || { echo "lock-acquire: bad slug: $1" >&2; exit 64; }
  mkdir -p "$INFLIGHT"
  local started
  started="$(date -u +%FT%TZ)"
  if ( set -C; printf 'SLUG=%s\nSTARTED=%s\n' "$1" "$started" > "$LOCK" ) 2>/dev/null; then
    printf 'locked %s (slug %s, started %s)\n' "$LOCK" "$1" "$started"
  else
    echo "wave lock already exists (lost the race): $LOCK" >&2
    exit 3
  fi
}

cmd_lock_release() {
  if [ ! -f "$LOCK" ]; then
    # No lock: nothing to match. Clear any leftover orchestrator-session binding from a crashed run
    # so the next lock-acquire can bind again (the hook refuses to bind over a foreign leftover).
    # A bare release (no arguments) is the documented crash-cleanup form; arguments, when given,
    # must still be well-formed.
    if [ $# -ge 1 ]; then
      [ $# -ge 2 ] || usage
      slug_ok "$1" || { echo "lock-release: bad slug: $1" >&2; exit 64; }
      started_ok "$2" || { echo "lock-release: bad STARTED value: $2" >&2; exit 64; }
    fi
    rm -f "$INFLIGHT/orchestrator-session"
    echo "no wave lock to release (any leftover orchestrator-session binding is cleared)"
    exit 0
  fi
  [ $# -ge 2 ] || usage
  slug_ok "$1" || { echo "lock-release: bad slug: $1" >&2; exit 64; }
  started_ok "$2" || { echo "lock-release: bad STARTED value: $2" >&2; exit 64; }
  local got_slug got_started
  got_slug="$(sed -n 's/^SLUG=//p' "$LOCK" | head -1)"
  got_started="$(sed -n 's/^STARTED=//p' "$LOCK" | head -1)"
  if [ "$got_slug" = "$1" ] && [ "$got_started" = "$2" ]; then
    rm -f "$LOCK"
    rm -f "$INFLIGHT/orchestrator-session"
    printf 'released %s (slug %s, started %s)\n' "$LOCK" "$1" "$2"
  else
    echo "lock identity mismatch (lock has slug=$got_slug started=$got_started); keeping $LOCK" >&2
    exit 1
  fi
}

cmd_check_wave() {
  local codex=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --codex) codex=1; shift ;;
      *) usage ;;
    esac
  done
  local busy=0
  if [ "$codex" -eq 1 ]; then
    if ! command -v codex >/dev/null 2>&1; then
      echo "check-wave: codex is not on PATH" >&2
      busy=1
    fi
    local f
    for f in .claude/bin/super-board-codex-wave.sh .claude/bin/super-board-codex-lane.sh; do
      if [ ! -f "$f" ]; then
        echo "check-wave: missing $f" >&2
        busy=1
      elif ! bash -n "$f" 2>/dev/null; then
        echo "check-wave: $f does not parse" >&2
        busy=1
      fi
    done
  elif [ ! -f .claude/workflows/super-board-wave.js ]; then
    echo "check-wave: missing .claude/workflows/super-board-wave.js" >&2
    busy=1
  elif ! { echo '(async function(){'; sed 's/^export const meta/const meta/' .claude/workflows/super-board-wave.js; echo '})'; } | node --check 2>&1; then
    echo "check-wave: .claude/workflows/super-board-wave.js does not parse" >&2
    busy=1
  fi
  # No legacy run may be active (run-workflow.md precondition 5b). pgrep is best-effort: when it is
  # missing, say so once and let the lock carry the mutual exclusion.
  if command -v pgrep >/dev/null 2>&1; then
    if pgrep -f 'super-board-run\.sh' >/dev/null 2>&1; then
      echo "check-wave: a legacy super-board-run.sh dispatcher is active" >&2
      busy=1
    fi
    if pgrep -f 'claude -p .*super-board run' >/dev/null 2>&1; then
      echo "check-wave: a headless claude -p super-board run is active" >&2
      busy=1
    fi
    if pgrep -f 'super-board-codex-(wave|lane)\.sh' >/dev/null 2>&1; then
      echo "check-wave: a repo-local super-board-codex-wave.sh / super-board-codex-lane.sh process remains" >&2
      busy=1
    fi
  else
    echo "check-wave: pgrep not available — verify no legacy run is active by hand" >&2
  fi
  local pid
  if [ -f .claude/super-board/codex-wave.pid ]; then
    pid="$(head -1 .claude/super-board/codex-wave.pid | tr -d '[:space:]')"
    if printf '%s' "$pid" | grep -qE '^[0-9]{1,7}$' && kill -0 "$pid" 2>/dev/null; then
      echo "check-wave: a codex wave (pid $pid) is still running" >&2
      busy=1
    fi
  fi
  [ "$busy" -eq 0 ] || exit 1
  echo "check-wave: ok — the wave launcher parses and no legacy run is active"
}

cmd_db_check() {
  local env_name="" config=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --config) [ $# -ge 2 ] || usage; config="$2"; shift 2 ;;
      --config=*) config="${1#--config=}"; shift ;;
      *) if [ -z "$env_name" ]; then env_name="$1"; fi; shift ;;   # extra arguments are ignored, never run
    esac
  done
  slug_ok "$env_name" || { echo "db-check: bad env: ${env_name:-<none>}" >&2; exit 64; }
  if [ -n "$config" ]; then
    cfg_under_configs "$config" || { echo "db-check: --config must name a JSON file under .claude/super-board/configs/ (got: $config)" >&2; exit 64; }
  fi
  if [ -z "$config" ]; then
    [ -f "$ACTIVE" ] || { echo "db-check: no --config and no $ACTIVE" >&2; exit 66; }
    local active_slug
    active_slug="$(cat "$ACTIVE")"
    slug_ok "$active_slug" || { echo "db-check: $ACTIVE must hold exactly one board slug, got: $(printf '%s' "$active_slug" | head -1)" >&2; exit 64; }
    config=".claude/super-board/configs/$active_slug.json"
  fi
  [ -f "$config" ] || { echo "db-check: config not found: $config" >&2; exit 66; }
  local allowed cmd has_cmd
  allowed="$(jq -r --arg e "$env_name" 'if ((.migrations.allowed_envs // []) | index($e)) == null then "no" else "yes" end' "$config")"
  [ "$allowed" = "yes" ] || { echo "db-check: env $env_name is not in migrations.allowed_envs" >&2; exit 64; }
  cmd="$(jq -r --arg e "$env_name" '.migrations.checks[$e] // empty' "$config")"
  has_cmd="$(jq -r --arg e "$env_name" '.migrations.commands[$e] // empty' "$config")"
  if [ -z "$cmd" ]; then
    if [ -n "$has_cmd" ]; then
      echo "db-check: env $env_name has a migrate command but no configured check — say so once, then go on"
    else
      echo "db-check: env $env_name has no command and no check"
    fi
    exit 0
  fi
  bash -c "$cmd"
}

cmd_reconcile() {
  local config="" wave="" dateslug=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --config) [ $# -ge 2 ] || usage; config="$2"; shift 2 ;;
      --wave) [ $# -ge 2 ] || usage; wave="$2"; shift 2 ;;
      --date-slug) [ $# -ge 2 ] || usage; dateslug="$2"; shift 2 ;;
      *) usage ;;
    esac
  done
  [ -n "$config" ] && [ -n "$wave" ] && [ -n "$dateslug" ] || usage
  num_ok "$wave" || { echo "reconcile: bad wave number: $wave" >&2; exit 64; }
  slug_ok "$dateslug" || { echo "reconcile: bad date-slug: $dateslug" >&2; exit 64; }
  [ -f "$config" ] || { echo "reconcile: config not found: $config" >&2; exit 66; }
  local cards
  cards="$(cat)"
  printf '%s' "$cards" | jq -e '(.cards | type) == "array"' >/dev/null 2>&1 \
    || { echo "reconcile: stdin must be the wave result cards JSON (a \"cards\" array); got something else" >&2; exit 64; }
  mkdir -p docs/super-board/runs
  local manifest="docs/super-board/runs/$dateslug.md"
  printf '%s' "$cards" | jq -r 'def cell: gsub("\\|"; "\\|") | gsub("\n"; " ") | gsub("\r"; " ");
    .cards[] | "| #\(.number) | \((.lanesRun // "none") | cell) | \((.finalStatus // "?") | cell) | \((.column // "?") | cell) | \((.detail // "-") | cell) |"' >> "$manifest"
  local results rc=0
  results="$(mktemp)"
  printf '%s' "$cards" > "$results"
  bash "$SELF_DIR/super-board-throttle.sh" --config "$config" --results "$results" || rc=$?
  rm -f "$results"
  printf 'reconcile: %s row(s) → %s (wave %s)\n' "$(printf '%s' "$cards" | jq '.cards | length')" "$manifest" "$wave" >&2
  return "$rc"
}

case "${1:-}" in
  lock-acquire) shift; cmd_lock_acquire "$@" ;;
  lock-release) shift; cmd_lock_release "$@" ;;
  check-wave) shift; cmd_check_wave "$@" ;;
  db-check) shift; cmd_db_check "$@" ;;
  reconcile) shift; cmd_reconcile "$@" ;;
  *) usage ;;
esac
