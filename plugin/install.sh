#!/usr/bin/env bash
# super-board installer.
# Copies skills/, scripts/, workflows/ and the guard hooks into the target
# project's .claude/ tree, and merges the hook settings into
# .claude/settings.json (backed up first, never duplicated).
#
# Usage:
#   ./install.sh [--no-hooks] [--protect-main] [--guard-repo owner/name] [target-project-dir]
# Defaults to the current working directory. --no-hooks skips the guard hooks
# and leaves settings.json untouched. --protect-main also wires the opt-in
# guard that blocks direct and force pushes to main/master/base_branch; with
# --no-hooks it installs that one guard alone. The installer asks nothing:
# super-board onboard asks the questions (protect main is one of them).
# Project settings call the hooks through .claude/hooks/sb-python.sh, which runs them with the
# interpreter pinned in ${USERPROFILE:-$HOME}/.mission-control/python (written by every hooks
# install) under `-I`; the committed settings.json holds no machine path.
# --guard-repo owner/name (not with --no-hooks; exit 64) also records the repo in the guarded
# list ${USERPROFILE:-$HOME}/.mission-control/guarded-repos.json, copies the user-scope hooks (and the
# launcher) to ~/.mission-control/hooks/ and wires them into ~/.claude/settings.json (merged, backed up,
# never overwritten; an invalid file is left untouched, exit 70). It then checks that the target is really
# matched by owner/name; if not, exit 65 (the list entry stays).
#
# Output is grouped with emojis, one line per group of files (failures still name
# the file). Environment: SUPER_BOARD_QUIET_NEXT=1 prints only the "🔧 installing"
# group — get.sh sets it and prints its own header and summary around it.
#
# An older super-board already in the target (a different skills/super-board/VERSION,
# or one with no VERSION) is recorded in .claude/super-board/upgrade.json
# {from, to, added_skills}; onboard's 🔍 Checks finishes the upgrade and lists it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS=1
PROTECT=0
TARGET=""
GUARD_REPO=""
while [ "$#" -gt 0 ]; do
  arg="$1"; shift
  case "$arg" in
    --no-hooks) HOOKS=0 ;;
    --protect-main) PROTECT=1 ;;
    --guard-repo)
      [ "$#" -gt 0 ] || { echo "--guard-repo needs owner/name" >&2; exit 64; }
      GUARD_REPO="$1"; shift ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    -*) echo "unknown option: $arg" >&2; exit 64 ;;
    *) TARGET="$arg" ;;
  esac
done
if [ -n "$GUARD_REPO" ]; then
  if [ "$HOOKS" -eq 0 ]; then echo "--guard-repo cannot be combined with --no-hooks" >&2; exit 64; fi
  case "$GUARD_REPO" in
    */*/*|*[!A-Za-z0-9_./-]*|/*|*/|'') echo "--guard-repo wants owner/name, got: $GUARD_REPO" >&2; exit 64 ;;
    */*) : ;;
    *) echo "--guard-repo wants owner/name, got: $GUARD_REPO" >&2; exit 64 ;;
  esac
fi
TARGET="${TARGET:-$PWD}"

# The Python every step below uses: python3, else python, skipping the Microsoft Store stub and a python that
# does not run. Falls back to a bare python3 so a machine with none still fails the way it always did.
is_store_stub() {
  case "$1" in
    *[/\\][Mm][Ii][Cc][Rr][Oo][Ss][Oo][Ff][Tt][/\\][Ww][Ii][Nn][Dd][Oo][Ww][Ss][Aa][Pp][Pp][Ss][/\\]*) return 0 ;;
  esac
  return 1
}
RESOLVED_PY=""
for cand in python3 python; do
  py=$("$cand" -c 'import os,sys;print(os.path.realpath(sys.executable).replace(chr(92),"/"))' 2>/dev/null) || py=""
  py="${py%$'\r'}"
  if [ -n "$py" ] && ! is_store_stub "$py"; then RESOLVED_PY="$py"; break; fi
done
PYBIN="${RESOLVED_PY:-python3}"

# Primary: the board and its lanes. Secondary: standalone helpers the lanes
# can call (visual, git-sync, ui-refine-loop). Worktree cleanup (cleanup-wt) is a
# hook, not a skill: it ships with the guard hooks below.
PRIMARY_SKILLS="super-board super-build super-qa super-review super-collect"
SECONDARY_SKILLS="visual git-sync ui-refine-loop"

if [ ! -d "$TARGET" ]; then
  echo "target directory not found: $TARGET" >&2
  exit 64
fi

QUIET="${SUPER_BOARD_QUIET_NEXT:-}"
PACK_VERSION="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION" 2>/dev/null || echo '?')"

# Record an upgrade BEFORE the copy overwrites the old VERSION.
PREV_VERSION=""
if [ -d "$TARGET/.claude/skills/super-board" ]; then
  PREV_VERSION="$(tr -d '[:space:]' < "$TARGET/.claude/skills/super-board/VERSION" 2>/dev/null || echo 'older')"
  [ -n "$PREV_VERSION" ] || PREV_VERSION="older"
fi
ADDED_SKILLS=""
if [ -n "$PREV_VERSION" ]; then
  for skill in $PRIMARY_SKILLS $SECONDARY_SKILLS; do
    [ -e "$TARGET/.claude/skills/$skill" ] || ADDED_SKILLS="$ADDED_SKILLS $skill"
  done
fi

[ -n "$QUIET" ] || echo "🧩 super-board $PACK_VERSION → $TARGET"
echo "🔧 installing"

# Resolve all skill paths before replacing anything. A destination can contain
# the pack (or a linked source skill); clearing it would destroy our input.
# A destination inside its source would instead recursively copy into itself.
"$PYBIN" - "$REPO_ROOT" "$TARGET" $PRIMARY_SKILLS $SECONDARY_SKILLS <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2])
for skill in sys.argv[3:]:
    src = (root / "skills" / skill).resolve()
    dest = (target / ".claude" / "skills" / skill).resolve()
    if src == dest:
        continue
    if dest == root or dest in root.parents or dest in src.parents or src in dest.parents:
        sys.exit(f"    ✗ refusing to install {skill}: source and destination overlap ({dest})")
PY

mkdir -p "$TARGET/.claude/skills" "$TARGET/.claude/bin"

# A project may keep the real skill directories elsewhere and symlink them into
# .claude/skills — a layout some repos use to share one tree between .claude and
# another agent host. `cp -R src dest/` fails outright on such a target ("Not a
# directory"), so resolve the link and write to where it actually points. That
# keeps the project's chosen layout instead of replacing it with a plain copy.
for skill in $PRIMARY_SKILLS $SECONDARY_SKILLS; do
  if [ ! -d "$REPO_ROOT/skills/$skill" ]; then
    echo "    ✗ missing $skill in repo — skipping" >&2
    continue
  fi
  dest="$TARGET/.claude/skills/$skill"
  note=""
  if [ -L "$dest" ]; then
    # `cd -P` walks the link to its physical target in one step. Do NOT rebuild
    # the path from `readlink` output: `cd ""` SUCCEEDS in bash, so an empty
    # readlink silently leaves you in the parent, and the rm below then empties
    # .claude/skills instead of one skill. That is not hypothetical — it deleted
    # 190 entries on a real repo on 2026-08-20 before this comment existed.
    resolved=$(cd -P "$dest" 2>/dev/null && pwd) || resolved=""
    if [ -z "$resolved" ]; then
      echo "    ✗ $skill is a symlink pointing nowhere — skipping rather than replacing it" >&2
      continue
    fi
    dest="$resolved"
    note=" (via symlink → ${resolved#$TARGET/})"
  fi

  # Belt and braces: whatever the resolution produced, it must be a path ENDING
  # in this skill's own name. Anything else is a bug in the line above, and the
  # blast radius of being wrong here is the user's entire skills tree.
  case "$dest" in
    */"$skill") : ;;
    *) echo "    ✗ refusing to write $skill: resolved to '$dest', which is not a $skill directory" >&2
       continue ;;
  esac

  # A self-vendored project can expose this very source through one or more
  # links. There is nothing to copy in that case; clearing dest erases src too.
  if [ "$REPO_ROOT/skills/$skill" -ef "$dest" ]; then
    echo "   ✓ $skill already uses the source directory"
    continue
  fi

  # Replace the contents, not the directory itself, so a symlink stays a symlink.
  mkdir -p "$dest"
  rm -rf "${dest:?}/"* 2>/dev/null || true
  cp -R "$REPO_ROOT/skills/$skill/." "$dest/"
  [ -z "$note" ] || echo "   ✓ $skill$note"
done

# File destinations can be links into the pack too. -ef follows symlinks and
# also recognizes hard links, so cp never tries to overwrite its own input.
copy_file() {
  if [ ! "$1" -ef "$2" ]; then
    cp "$1" "$2"
  fi
}

# Hooks run through hooks/sb-python.sh with the interpreter pinned here (spec section 15.3 G-3). The user
# scope (--guard-repo) holds the pinned path directly; the committed project settings never do.
MC_HOME="${USERPROFILE:-$HOME}/.mission-control"
PIN=""
pin_python() {
  local py="$RESOLVED_PY" cur=""
  if [ -z "$py" ]; then
    echo "   ! no usable Python to pin (python3 is missing or the Microsoft Store stub): the guard hooks refuse to run until you install Python and re-run install.sh"
    return 0
  fi
  PIN="$py"
  if [ -f "$MC_HOME/python" ]; then IFS= read -r cur < "$MC_HOME/python" || true; fi
  cur="${cur%$'\r'}"
  if [ "$cur" != "$py" ]; then
    mkdir -p "$MC_HOME"
    printf '%s\n' "$py" > "$MC_HOME/python.tmp.$$" && mv -f "$MC_HOME/python.tmp.$$" "$MC_HOME/python"
  fi
  echo "   🐍 hooks run with $py -I"
}
install_launcher() {
  copy_file "$REPO_ROOT/hooks/sb-python.sh" "$TARGET/.claude/hooks/sb-python.sh"
  chmod +x "$TARGET/.claude/hooks/sb-python.sh"
}

for script in super-board-lane-delegate.py super-board-lane-paths.py super-board-lane-env.py super-board-lane-integrity.py super-board-lane-signatures.py super-board-lane-allowpath.py super-board-lane-docker.py super-board-lane-remote.py super-board-project-verifier.py super-board-skills-ready.py super-board-calibration.py super-board-verification-ready.py super-board-github-read.py super-board-events.py super-board-collect-state.py super-board-run.sh super-board-gh-guard.sh super-board-card.sh super-board-status.py super-board-roadmap.py super-board-autopilot-batch.py super-board-wave-plan.sh super-board-codex-lane.sh super-board-codex-wave.sh super-board-deps.sh super-board-preflight.sh super-board-merge-gate.sh super-board-merge-policy.py super-board-approval.py super-board-env-check.sh super-board-agents-md.py super-board-settings.py super-board-setup.py super-board-usage.sh super-board-throttle.sh super-board-host.sh super-board-pr-body.sh super-review-file-refactor.sh super-qa-file-bug.sh super-board-stop.sh super-board-git.sh super-board-objects.py super-board-orch.sh super-board-watchdog.py super-board-watchdog-install.sh; do
  if [ -f "$REPO_ROOT/scripts/$script" ]; then
    copy_file "$REPO_ROOT/scripts/$script" "$TARGET/.claude/bin/$script"
    chmod +x "$TARGET/.claude/bin/$script"
  fi
done

# lane-docker renders the tool proxy's squid.conf from this one template; installed it sits beside the script.
if [ -f "$REPO_ROOT/sandbox/proxy/squid.conf.tmpl" ]; then
  copy_file "$REPO_ROOT/sandbox/proxy/squid.conf.tmpl" "$TARGET/.claude/bin/super-board-squid.conf.tmpl"
fi

mkdir -p "$TARGET/.claude/workflows"
for wf in super-board-wave.js ui-refine-loop.js; do
  if [ -f "$REPO_ROOT/workflows/$wf" ]; then
    copy_file "$REPO_ROOT/workflows/$wf" "$TARGET/.claude/workflows/$wf"
  fi
done
echo "   ✓ skills, scripts and workflows → .claude/"

SETTINGS_PY="$REPO_ROOT/scripts/super-board-settings.py"
# Stdlib-only merge (scripts/super-board-settings.py): keeps every existing key
# and hook, adds each guard command once per event + matcher, backs the file up
# before writing, and leaves an invalid settings.json untouched (exit 2).
# Prints one summary word for the settings line; the helper's own lines (an
# invalid-JSON refusal) go to stderr untouched.
merge_settings() {
  local had=0 res rc=0
  [ -f "$TARGET/.claude/settings.json" ] && had=1
  res=$("$PYBIN" "$SETTINGS_PY" hooks "$TARGET/.claude/settings.json" "$@" 2>&1) || rc=$?
  if [ "$rc" -eq 2 ]; then printf '%s\n' "$res" >&2; echo "not valid JSON — left untouched"
  elif echo "$res" | grep -q "already present"; then echo "already present"
  elif [ "$had" -eq 1 ]; then echo "backup kept"
  else echo "new file"; fi
}

if [ "$HOOKS" -eq 1 ]; then
  mkdir -p "$TARGET/.claude/hooks"
  # hooks/*.py only: hooks/dev/ holds gates for repos that author skills, never installed.
  n=0
  for h in "$REPO_ROOT"/hooks/*.py; do
    [ -f "$h" ] || continue
    copy_file "$h" "$TARGET/.claude/hooks/$(basename "$h")"
    chmod +x "$TARGET/.claude/hooks/$(basename "$h")"
    n=$((n + 1))
  done
  # The one shell hook: SessionStart identity check (wired by settings-snippet.json).
  if [ -f "$REPO_ROOT/hooks/mc-identity-check.sh" ]; then
    copy_file "$REPO_ROOT/hooks/mc-identity-check.sh" "$TARGET/.claude/hooks/mc-identity-check.sh"
    chmod +x "$TARGET/.claude/hooks/mc-identity-check.sh"
  fi
  install_launcher
  pin_python
  SNIPPETS="$REPO_ROOT/hooks/settings-snippet.json"
  if [ "$PROTECT" -eq 1 ]; then SNIPPETS="$SNIPPETS $REPO_ROOT/hooks/settings-protect-main.json"; fi
  # shellcheck disable=SC2086
  echo "   🛡️  $n guard hooks → .claude/settings.json ($(merge_settings $SNIPPETS))"
elif [ "$PROTECT" -eq 1 ]; then
  # --no-hooks --protect-main: the one guard asked for, nothing else. This is the
  # command onboard names when its protect-main question finds no guard script.
  mkdir -p "$TARGET/.claude/hooks"
  copy_file "$REPO_ROOT/hooks/guard-protected-push.py" "$TARGET/.claude/hooks/guard-protected-push.py"
  chmod +x "$TARGET/.claude/hooks/guard-protected-push.py"
  install_launcher
  pin_python
  echo "   🛡️  --no-hooks: only the push guard → .claude/settings.json ($(merge_settings "$REPO_ROOT/hooks/settings-protect-main.json"))"
else
  echo "   ⏭️  guard hooks skipped (--no-hooks)"
fi

# AGENTS.md: a re-install refreshes the managed super-board block and touches
# nothing outside its markers. No block yet → onboard writes it (with consent).
if [ -f "$TARGET/AGENTS.md" ] && grep -q '^<!-- super-board:begin' "$TARGET/AGENTS.md"; then
  "$PYBIN" "$REPO_ROOT/scripts/super-board-agents-md.py" block --root "$TARGET" \
    --template "$REPO_ROOT/skills/super-board/references/agents-md-block.md" \
    --version "$PACK_VERSION" >/dev/null 2>&1 \
    && echo "   📜 AGENTS.md: super-board block refreshed" \
    || echo "   ! AGENTS.md: couldn't refresh the super-board block — onboard repairs it"
  # The refresh rewrote the Merge / Migrations rows from the template; put the config's answers back.
  "$PYBIN" "$REPO_ROOT/scripts/super-board-setup.py" sync-agents-md --root "$TARGET" >/dev/null 2>&1 || true
fi
if [ -f "$TARGET/CLAUDE.md" ] && ! grep -m1 -v '^[[:space:]]*$' "$TARGET/CLAUDE.md" | grep -qx '@AGENTS.md'; then
  echo "   ! CLAUDE.md holds its own rules — onboard offers a merge"
fi

# Upgrade record for onboard's 🔍 Checks (it migrates config, labels, columns).
if [ -n "$PREV_VERSION" ] && [ "$PREV_VERSION" != "$PACK_VERSION" ]; then
  mkdir -p "$TARGET/.claude/super-board"
  "$PYBIN" - "$TARGET/.claude/super-board/upgrade.json" "$PREV_VERSION" "$PACK_VERSION" $ADDED_SKILLS <<'PY'
import json, sys, time
path, frm, to, added = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
try:
    old = json.load(open(path))
except Exception:
    old = {}
rec = {"from": old.get("from", frm), "to": to, "added_skills": sorted(set(old.get("added_skills", []) + added)),
       "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
json.dump(rec, open(path, "w"), indent=2)
PY
  echo "   ⬆️  upgrade from $PREV_VERSION — onboard finishes it (config, labels, columns)"
fi

# --guard-repo: the guarded list (D-5) and the user-scope guards (G-1, G-4).
if [ -n "$GUARD_REPO" ]; then
  if [ -z "$PIN" ]; then
    echo "   ✗ --guard-repo needs a usable Python to pin (see the ! line above)" >&2
    exit 70
  fi
  rc=0
  res=$("$PIN" -I "$REPO_ROOT/hooks/mc_guarded.py" add --repo "$GUARD_REPO" --root "$TARGET" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$res" >&2
    exit "$rc"
  fi
  chk=$("$PIN" -I "$REPO_ROOT/hooks/mc_guarded.py" check "$TARGET" 2>&1) || true
  if [ "$chk" != "guarded" ]; then
    echo "   ✗ --guard-repo: $TARGET is not matched by $GUARD_REPO (its remotes do not normalise to it)" >&2
    exit 65
  fi
  mkdir -p "$MC_HOME/hooks"
  for h in guard-external-tools.py guard-config-write.py mc_guarded.py sb-python.sh; do
    copy_file "$REPO_ROOT/hooks/$h" "$MC_HOME/hooks/$h"
    chmod +x "$MC_HOME/hooks/$h"
  done
  HOME_FWD="${USERPROFILE:-$HOME}"; HOME_FWD="${HOME_FWD//\\//}"
  USER_SETTINGS="${USERPROFILE:-$HOME}/.claude/settings.json"
  rc=0
  res=$("$PYBIN" "$SETTINGS_PY" user-hooks "$USER_SETTINGS" --hooks-dir "$HOME_FWD/.mission-control/hooks" 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$res" >&2
    echo "   ! $USER_SETTINGS is not valid JSON or could not be merged — left untouched; wire the user-scope hooks by hand (see hooks/README.md)" >&2
    exit 70
  fi
  echo "   🔒 guarded: $GUARD_REPO (list: $MC_HOME/guarded-repos.json); G-1 and config guard at user scope"
fi

if [ -z "$QUIET" ]; then
  echo "🎉 super-board $PACK_VERSION is installed"
  echo "👉 next: open Claude Code here and run /super-board onboard"
fi
