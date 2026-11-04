#!/usr/bin/env bash
# Tests install.sh against plain, linked, and self-vendored target layouts.
#
# THIS FILE EXISTS BECAUSE INSTALL.SH ONCE DELETED 190 DIRECTORIES.
#
# On 2026-08-20 a symlink-aware install path resolved its destination by hand:
#
#   resolved=$(cd "$(dirname "$dest")" && cd "$(readlink "$dest")" && pwd)
#
# `cd ""` SUCCEEDS in bash. It is a no-op that returns 0 and leaves you where you
# were. So when `readlink` produced nothing, `resolved` became the PARENT —
# .claude/skills — and the `rm -rf "$dest"/*` that followed emptied the user's
# entire skills tree rather than one skill's contents.
#
# Every scenario below plants decoy directories beside the four skills and
# asserts they are still there afterwards. An installer is allowed to fail; it is
# never allowed to take the neighbours with it.
set -euo pipefail
cd "$(dirname "$0")"
INSTALL="../install.sh"
# install.sh pins the interpreter under ${USERPROFILE:-$HOME}/.mission-control: never the real one.
TEST_HOME=$(mktemp -d); export HOME="$TEST_HOME" USERPROFILE="$TEST_HOME"
trap 'rm -rf "$TEST_HOME"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }
plant_decoys() { for d in decoy-one decoy-two decoy-three; do mkdir -p "$1/.claude/skills/$d"; touch "$1/.claude/skills/$d/SKILL.md"; done; }
decoys_intact() { [ "$(ls "$1/.claude/skills" | grep -c '^decoy')" -eq 3 ]; }

SKILLS="super-board super-build super-qa super-review"
ALL_SKILLS="$SKILLS super-collect ui-refine-loop visual git-sync"

# 1 — a fresh project: plain directories, nothing to preserve. Every primary and
#     secondary skill lands, and both workflows.
T=$(mktemp -d)
"$INSTALL" "$T" >/dev/null 2>&1
for s in $ALL_SKILLS; do
  [ -f "$T/.claude/skills/$s/SKILL.md" ] || fail "plain layout: $s/SKILL.md missing"
done
for w in super-board-wave.js ui-refine-loop.js; do
  [ -f "$T/.claude/workflows/$w" ] || fail "workflow $w was not installed"
done
rm -rf "$T"

# 2 — a project that symlinks .claude/skills/<name> at a real tree elsewhere.
#     The symlink must survive, the content must land at the target, and the
#     neighbours must be untouched.
T=$(mktemp -d)
mkdir -p "$T/.claude/skills"
for s in $SKILLS; do
  mkdir -p "$T/.agents/skills/$s"
  ( cd "$T/.claude/skills" && ln -s "../../.agents/skills/$s" "$s" )
done
plant_decoys "$T"
"$INSTALL" "$T" >/dev/null 2>&1
for s in $SKILLS; do
  [ -L "$T/.claude/skills/$s" ] || fail "symlink layout: $s stopped being a symlink"
  [ -f "$T/.agents/skills/$s/SKILL.md" ] || fail "symlink layout: $s content did not reach the real path"
done
decoys_intact "$T" || fail "symlink layout: DECOYS DESTROYED — the 2026-08-20 regression is back"
rm -rf "$T"

# 3 — a symlink pointing nowhere. Skip it and say so; never guess, never delete.
T=$(mktemp -d)
mkdir -p "$T/.claude/skills"
( cd "$T/.claude/skills" && ln -s ../../nowhere/super-board super-board )
plant_decoys "$T"
OUT=$("$INSTALL" "$T" 2>&1 || true)
echo "$OUT" | grep -q "pointing nowhere" || fail "a dangling symlink should be reported, got: $OUT"
decoys_intact "$T" || fail "dangling symlink: DECOYS DESTROYED"
rm -rf "$T"

# 4 — the guard of last resort. Even if resolution goes wrong, a destination that
#     is not named after the skill is refused before anything is removed.
T=$(mktemp -d)
mkdir -p "$T/.claude/skills" "$T/elsewhere"
( cd "$T/.claude/skills" && ln -s ../../elsewhere super-board )
plant_decoys "$T"
OUT=$("$INSTALL" "$T" 2>&1 || true)
echo "$OUT" | grep -q "which is not a super-board directory" \
  || fail "a mis-resolved destination should be refused by name, got: $OUT"
[ "$(ls "$T/elsewhere" | wc -l | tr -d ' ')" -eq 0 ] || fail "the refused destination must not be written to"
decoys_intact "$T" || fail "mis-resolved destination: DECOYS DESTROYED"
rm -rf "$T"

# 4b — a vendored pack is already exposed through two layers of skill links.
#      Refreshing it must preserve the source and both links, on repeated runs.
T=$(mktemp -d)
PACK="$T/_skills/vendor/super-board"
mkdir -p "$PACK" "$T/.agents/skills" "$T/.claude/skills"
cp -R ../install.sh ../VERSION ../skills ../scripts ../workflows ../hooks "$PACK/"
for s in $ALL_SKILLS; do
  ln -s "../../_skills/vendor/super-board/skills/$s" "$T/.agents/skills/$s"
  ln -s "../../.agents/skills/$s" "$T/.claude/skills/$s"
done
plant_decoys "$T"
for run in 1 2; do
  OUT=$("$PACK/install.sh" --no-hooks "$T" 2>&1) || fail "self-vendored refresh failed: $OUT"
  diff -qr ../skills "$PACK/skills" >/dev/null || fail "self-vendored refresh changed the source skills"
  for s in $ALL_SKILLS; do
    [ -L "$T/.claude/skills/$s" ] && [ -L "$T/.agents/skills/$s" ] \
      || fail "self-vendored refresh replaced a link for $s"
    [ -f "$T/.claude/skills/$s/SKILL.md" ] || fail "self-vendored refresh lost $s"
  done
  decoys_intact "$T" || fail "self-vendored refresh: DECOYS DESTROYED"
done
rm -rf "$T"

# 4c — individual helper links also point into the vendored pack. Both the full
#      hook install and the push-guard-only path must tolerate the same files.
T=$(mktemp -d)
PACK="$T/_skills/vendor/super-board"
mkdir -p "$PACK" "$T/.claude/bin" "$T/.claude/workflows" "$T/.claude/hooks"
cp -R ../install.sh ../VERSION ../skills ../scripts ../workflows ../hooks "$PACK/"
for group in scripts workflows hooks; do
  case "$group" in scripts) dest=bin ;; *) dest="$group" ;; esac
  for src in "$PACK/$group/"*; do
    [ -f "$src" ] || continue
    ln -s "$src" "$T/.claude/$dest/$(basename "$src")"
  done
done
for flags in '--protect-main' '--no-hooks --protect-main'; do
  OUT=$("$PACK/install.sh" $flags "$T" 2>&1) || fail "linked helper refresh failed: $OUT"
  for group in scripts workflows hooks; do
    diff -qr "../$group" "$PACK/$group" >/dev/null || fail "linked helper refresh changed source $group"
  done
  for file in bin/super-board-run.sh workflows/super-board-wave.js hooks/guard-protected-push.py; do
    [ -L "$T/.claude/$file" ] || fail "linked helper refresh replaced $file"
    [ -s "$T/.claude/$file" ] || fail "linked helper refresh emptied $file"
  done
done
rm -rf "$T"

# 4d — a linked destination contains the pack itself. Refuse before clearing it.
T=$(mktemp -d)
PACK="$T/super-board/vendor"
mkdir -p "$PACK" "$T/.claude/skills"
cp -R ../install.sh ../VERSION ../skills ../scripts ../workflows ../hooks "$PACK/"
ln -s "$T/super-board" "$T/.claude/skills/super-board"
plant_decoys "$T"
if OUT=$("$PACK/install.sh" --no-hooks "$T" 2>&1); then
  fail "a destination containing the source pack was accepted"
fi
diff -qr ../skills "$PACK/skills" >/dev/null || fail "overlapping destination erased source skills"
diff -qr ../scripts "$PACK/scripts" >/dev/null || fail "overlapping destination erased source scripts"
echo "$OUT" | grep -q 'overlap' || fail "source overlap should be explained: $OUT"
decoys_intact "$T" || fail "overlapping destination: DECOYS DESTROYED"
rm -rf "$T"

# 4e — copying a skill into a directory within itself is unsafe too. It must
#      fail before partially copying files or changing the original source.
T=$(mktemp -d)
PACK="$T/pack"
mkdir -p "$PACK" "$T/.claude/skills"
cp -R ../install.sh ../VERSION ../skills ../scripts ../workflows ../hooks "$PACK/"
mkdir -p "$PACK/skills/super-board/nested/super-board"
ln -s "$PACK/skills/super-board/nested/super-board" "$T/.claude/skills/super-board"
cp -R "$PACK/skills" "$T/expected-skills"
plant_decoys "$T"
if OUT=$("$PACK/install.sh" --no-hooks "$T" 2>&1); then
  fail "a destination inside the source skill was accepted"
fi
diff -qr "$T/expected-skills" "$PACK/skills" >/dev/null || fail "nested destination changed source skills"
echo "$OUT" | grep -q 'overlap' || fail "nested source overlap should be explained: $OUT"
decoys_intact "$T" || fail "nested destination: DECOYS DESTROYED"
rm -rf "$T"

# 5 — every dispatcher script the skills reference is actually shipped. A verb
#     whose script was never installed fails only when a user reaches for it.
T=$(mktemp -d)
"$INSTALL" "$T" >/dev/null 2>&1
for s in super-board-run.sh super-board-stop.sh super-board-wave-plan.sh \
         super-board-codex-lane.sh super-board-codex-wave.sh super-board-card.sh super-board-throttle.sh super-board-host.sh \
         super-board-deps.sh super-board-preflight.sh super-board-merge-gate.sh super-board-usage.sh super-board-pr-body.sh \
         super-review-file-refactor.sh super-qa-file-bug.sh; do
  [ -x "$T/.claude/bin/$s" ] || fail "$s was not installed, or is not executable"
done
# the proxy's squid template is installed beside lane-docker, and lane-docker finds it there (no sandbox/ dir in a target)
[ -f "$T/.claude/bin/super-board-squid.conf.tmpl" ] || fail "super-board-squid.conf.tmpl was not installed into .claude/bin"
[ ! -e "$T/.claude/sandbox" ] || fail "the installed layout has no sandbox/ dir"
python3 - "$T/.claude/bin/super-board-lane-docker.py" <<'PY' || fail "an installed lane-docker could not render its squid.conf from .claude/bin"
import importlib.util, sys
spec = importlib.util.spec_from_file_location("installed_lane_docker", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert m.squid_template_path().name == "super-board-squid.conf.tmpl", m.squid_template_path()
assert "acl allow_hosts dstdomain a.example.com" in m.render_squid_conf(["a.example.com"])
PY
rm -rf "$T"

# 6 — guard hooks: scripts copied, settings merged into an existing file without
#     losing its keys or hooks, backed up first, and a second run adds nothing.
T=$(mktemp -d)
mkdir -p "$T/.claude"
cat > "$T/.claude/settings.json" <<'JSON'
{"permissions":{"allow":["Bash(ls:*)"]},"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"echo mine"}]}],"Stop":[{"hooks":[{"type":"command","command":"echo stop"}]}]}}
JSON
"$INSTALL" "$T" >/dev/null 2>&1
for h in guard-worktree-path.py guard-secrets.py guard-key-literals.py guard-delete-outside.py guard-protected-push.py guard-host-tests.py guard-orchestrator.py cleanup-wt.py; do
  [ -f "$T/.claude/hooks/$h" ] || fail "hook $h was not installed"
done
[ ! -e "$T/.claude/skills/cleanup-wt" ] || fail "cleanup-wt is a hook now, not a skill"
[ ! -e "$T/.claude/hooks/dev" ] && [ ! -e "$T/.claude/hooks/gate-skill-evals.py" ] || fail "hooks/dev/ (skill-eval gate) must never be installed into a target"
S="$T/.claude/settings.json"
jq -e '.permissions.allow == ["Bash(ls:*)"]' "$S" >/dev/null || fail "existing permissions were lost"
jq -e '[.hooks.PreToolUse[].hooks[].command] | index("echo mine") != null' "$S" >/dev/null || fail "existing PreToolUse hook was lost"
jq -e '.hooks.Stop[0].hooks[0].command == "echo stop"' "$S" >/dev/null || fail "existing Stop hook was lost"
jq -e '[.hooks.PreToolUse[] | select(.matcher == "Bash")] | length == 1' "$S" >/dev/null || fail "Bash matcher entry duplicated"
jq -e '[.hooks.SessionStart[].hooks[].command | select(contains("cleanup-wt.py --auto"))] | length == 1' "$S" >/dev/null \
  || fail "the cleanup-wt SessionStart hook is not wired by default"
jq -e '[.hooks[][].hooks[].command | select(contains("guard-protected-push"))] | length == 0' "$S" >/dev/null \
  || fail "the protected-push guard is opt-in: it must not be wired without --protect-main"
for h in guard-worktree-path guard-secrets guard-key-literals guard-delete-outside guard-host-tests guard-orchestrator; do
  jq -e --arg h "$h" '[.hooks[][].hooks[].command | select(contains($h))] | length > 0' "$S" >/dev/null || fail "$h not wired in settings"
done
ls "$T/.claude/" | grep -q '^settings.json.bak-' || fail "no backup written before changing settings.json"
BEFORE=$(cat "$S")
OUT=$("$INSTALL" "$T" 2>&1)
[ "$(cat "$S")" = "$BEFORE" ] || fail "a second install changed settings.json"
echo "$OUT" | grep -q "already present" || fail "a second install should report nothing to change, got: $OUT"
jq -e '[.hooks[] | [.[].hooks[].command] as $c | ($c | length) == ($c | unique | length)] | all' "$S" >/dev/null \
  || fail "a hook command appears twice in one event"
rm -rf "$T"

# 7 — --no-hooks: no hook scripts, no settings.json written.
T=$(mktemp -d)
"$INSTALL" --no-hooks "$T" >/dev/null 2>&1
[ ! -e "$T/.claude/hooks" ] || fail "--no-hooks still installed hooks"
[ ! -e "$T/.claude/settings.json" ] || fail "--no-hooks still wrote settings.json"
[ -f "$T/.claude/skills/super-board/SKILL.md" ] || fail "--no-hooks must still install the skills"
rm -rf "$T"

# 7b — --no-hooks --protect-main: exactly the one guard, wired; no other hook.
#      Onboard names this command when protect-main finds no guard script.
T=$(mktemp -d)
"$INSTALL" --no-hooks --protect-main "$T" >/dev/null 2>&1
[ "$(ls "$T/.claude/hooks" | tr '\n' ' ')" = "guard-protected-push.py sb-python.sh " ] || fail "--no-hooks --protect-main should install only the push guard and the launcher, got: $(ls "$T/.claude/hooks")"
jq -e '[.hooks[][].hooks[].command] == ["bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-protected-push.py"]' "$T/.claude/settings.json" >/dev/null \
  || fail "--no-hooks --protect-main should wire only the push guard"
rm -rf "$T"

# 8 — a settings.json that is not valid JSON is left exactly as it was.
T=$(mktemp -d)
mkdir -p "$T/.claude"; printf '{ not json' > "$T/.claude/settings.json"
OUT=$("$INSTALL" "$T" 2>&1 || true)
[ "$(cat "$T/.claude/settings.json")" = "{ not json" ] || fail "invalid settings.json was modified"
echo "$OUT" | grep -q "not valid JSON" || fail "invalid settings.json should be reported, got: $OUT"
rm -rf "$T"

# 10 — --protect-main wires the opt-in push guard, once, next to the defaults.
T=$(mktemp -d)
"$INSTALL" --protect-main "$T" >/dev/null 2>&1
S="$T/.claude/settings.json"
jq -e '[.hooks.PreToolUse[].hooks[].command | select(contains("guard-protected-push.py"))] | length == 1' "$S" >/dev/null \
  || fail "--protect-main did not wire guard-protected-push"
jq -e '[.hooks.PreToolUse[] | select(.matcher == "Bash|PowerShell")] | length == 1' "$S" >/dev/null || fail "--protect-main duplicated the Bash|PowerShell matcher"
OUT=$("$INSTALL" --protect-main "$T" 2>&1)
echo "$OUT" | grep -q "already present" || fail "a second --protect-main install should change nothing"
rm -rf "$T"

# 11 — AGENTS.md: a re-install refreshes only the managed block; a project without
#      a block is left alone; a rules-bearing CLAUDE.md gets the onboard hint; the
#      last line names onboard as the one next step.
T=$(mktemp -d)
printf '# AGENTS.md\n\n- keep me\n<!-- super-board:begin v0.0.1 -->\nold\n<!-- super-board:end -->\n- and me\n' > "$T/AGENTS.md"
printf '# Rules\n- pnpm\n' > "$T/CLAUDE.md"
OUT=$("$INSTALL" --no-hooks "$T" 2>&1)
grep -q '^- keep me$' "$T/AGENTS.md" && grep -q '^- and me$' "$T/AGENTS.md" || fail "install changed AGENTS.md outside the markers"
grep -q '^old$' "$T/AGENTS.md" && fail "install should refresh the managed block"
grep -q "super-board:begin v$(cat ../VERSION)" "$T/AGENTS.md" || fail "block should carry the pack version"
echo "$OUT" | grep -q "CLAUDE.md holds its own rules" || fail "a non-pointer CLAUDE.md should get the onboard hint"
[ "$(echo "$OUT" | tail -1)" = "👉 next: open Claude Code here and run /super-board onboard" ] || fail "last line should name onboard, got: $(echo "$OUT" | tail -1)"
echo "$OUT" | grep -q "📜 AGENTS.md: super-board block refreshed" || fail "the block refresh should be one grouped line, got: $OUT"
rm -rf "$T"
# SUPER_BOARD_QUIET_NEXT=1 (set by get.sh, which prints its own header and next step)
# prints only the 🔧 installing group: one line per group of files, no per-file list.
T=$(mktemp -d)
OUT=$(SUPER_BOARD_QUIET_NEXT=1 "$INSTALL" "$T" 2>&1)
[ "$(echo "$OUT" | head -1)" = "🔧 installing" ] || fail "quiet mode should start at the installing group, got: $OUT"
echo "$OUT" | grep -q "next:" && fail "quiet mode must not print the next step"
echo "$OUT" | grep -q "✓ skills, scripts and workflows → .claude/" || fail "quiet mode should print the grouped files line"
echo "$OUT" | grep -q "🛡️  14 guard hooks → .claude/settings.json (new file)" || fail "guard line should count hooks, got: $OUT"
[ "$(echo "$OUT" | wc -l | tr -d ' ')" -le 4 ] || fail "grouped output should be ≤ 4 lines here (files, guard hooks, interpreter pin), got: $OUT"
rm -rf "$T"

# 12 — upgrade record: an older super-board in the target is written to
#      .claude/super-board/upgrade.json (from, to, the skills that are new) for onboard.
T=$(mktemp -d); mkdir -p "$T/.claude/skills/super-board" "$T/.claude/skills/super-build"
echo "1.8.2" > "$T/.claude/skills/super-board/VERSION"
OUT=$("$INSTALL" --no-hooks "$T" 2>&1)
jq -e --arg v "$(cat ../VERSION)" '.from == "1.8.2" and .to == $v and (.added_skills | index("super-collect")) and (.added_skills | index("super-build") | not)' \
  "$T/.claude/super-board/upgrade.json" >/dev/null || fail "upgrade.json wrong: $(cat "$T/.claude/super-board/upgrade.json" 2>&1)"
echo "$OUT" | grep -q "⬆️  upgrade from 1.8.2" || fail "the upgrade should be one grouped line, got: $OUT"
"$INSTALL" --no-hooks "$T" >/dev/null 2>&1
jq -e '.from == "1.8.2"' "$T/.claude/super-board/upgrade.json" >/dev/null || fail "a second install must keep the original from-version"
rm -rf "$T"
T=$(mktemp -d); "$INSTALL" --no-hooks "$T" >/dev/null 2>&1
[ -e "$T/.claude/super-board/upgrade.json" ] && fail "a fresh install is not an upgrade"
rm -rf "$T"
T=$(mktemp -d); printf '# AGENTS.md\n- mine\n' > "$T/AGENTS.md"
"$INSTALL" --no-hooks "$T" >/dev/null 2>&1
[ "$(cat "$T/AGENTS.md")" = "$(printf '# AGENTS.md\n- mine')" ] || fail "install must not add a block uninvited"
for s in super-board-skills-ready.py super-board-host.sh super-board-codex-wave.sh super-board-codex-lane.sh super-board-env-check.sh super-board-merge-policy.py super-board-agents-md.py super-board-settings.py super-board-setup.py; do
  [ -x "$T/.claude/bin/$s" ] || fail "$s was not installed"
done
rm -rf "$T"

# 13 — every hooks install pins the interpreter and ships the launcher; project settings use the launcher.
inst() { HOME="$HM" USERPROFILE="$HM" "$INSTALL" "$@"; }
want_py() { python3 -c 'import os,sys;print(os.path.realpath(sys.executable).replace(chr(92),"/"))'; }
T=$(mktemp -d); HM=$(mktemp -d)
OUT=$(inst "$T" 2>&1)
[ -x "$T/.claude/hooks/sb-python.sh" ] || fail "sb-python.sh was not installed (executable) next to the hooks"
[ "$(head -1 "$HM/.mission-control/python" 2>/dev/null)" = "$(want_py)" ] || fail "the interpreter pin is missing or wrong: $(cat "$HM/.mission-control/python" 2>&1)"
echo "$OUT" | grep -qF "🐍 hooks run with $(want_py) -I" || fail "the pin line is missing, got: $OUT"
S="$T/.claude/settings.json"
jq -e '[.hooks[][].hooks[].command | select(startswith("python3") or startswith("python "))] | length == 0' "$S" >/dev/null || fail "project settings still call a bare python3"
jq -e '[.hooks[][].hooks[].command | select(contains(".py"))] | all(contains("sb-python.sh"))' "$S" >/dev/null || fail "every Python hook must go through the launcher"
jq -e '[.hooks.PreToolUse[] | select(.hooks[].command | contains("guard-host-tests.py")) | .hooks[].command | select(contains("guard-taint.py"))] | length == 1' "$S" >/dev/null || fail "guard-taint is not wired beside guard-host-tests"
jq -e '[.hooks.PreToolUse[] | select(.matcher == "Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell") | .hooks[].command | select(contains("guard-orchestrator.py"))] | length == 1' "$S" >/dev/null || fail "guard-orchestrator is not wired on the file-tool + shell matcher"
# B2: the pin is written before the settings merge (so a merge never points at a launcher with nothing pinned)
PL=$(echo "$OUT" | grep -n '🐍 hooks run with' | head -1 | cut -d: -f1); GL=$(echo "$OUT" | grep -n 'guard hooks → .claude/settings.json' | head -1 | cut -d: -f1)
[ -n "$PL" ] && [ -n "$GL" ] && [ "$PL" -lt "$GL" ] || fail "B2: the pin line must come before the guard-hooks line, got: $OUT"
printf '/stale/python\n' > "$HM/.mission-control/python"; inst "$T" >/dev/null 2>&1
[ "$(head -1 "$HM/.mission-control/python")" = "$(want_py)" ] || fail "a re-install must repair a stale pin"
# the launcher installed in the project really runs an installed hook
printf '%s' '{"tool_name":"Bash","tool_input":{"command":"echo hi"},"cwd":"'"$T"'"}' | HOME="$HM" USERPROFILE="$HM" CLAUDE_PROJECT_DIR="$T" bash "$T/.claude/hooks/sb-python.sh" guard-secrets.py || fail "the installed launcher could not run an installed hook"
# --no-hooks installs no pin; --no-hooks --protect-main installs the pin and the launcher beside the one guard
T2=$(mktemp -d); HM2=$(mktemp -d)
HOME="$HM2" USERPROFILE="$HM2" "$INSTALL" --no-hooks "$T2" >/dev/null 2>&1
[ ! -e "$HM2/.mission-control" ] || fail "--no-hooks must not write the pin"
HOME="$HM2" USERPROFILE="$HM2" "$INSTALL" --no-hooks --protect-main "$T2" >/dev/null 2>&1
[ -x "$T2/.claude/hooks/sb-python.sh" ] && [ -f "$HM2/.mission-control/python" ] || fail "--no-hooks --protect-main needs the launcher and the pin"
rm -rf "$T" "$T2" "$HM" "$HM2"

# 14 — --guard-repo: list entry, three user-scope commands, hook copies; existing user settings kept; idempotent.
T=$(mktemp -d); HM=$(mktemp -d)
git -C "$T" init -q; git -C "$T" remote add origin https://github.com/Owner/Name.git
mkdir -p "$HM/.claude"
echo '{"model":"x","permissions":{"allow":["Bash(ls:*)"]},"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"user.sh"}]}]}}' > "$HM/.claude/settings.json"
OUT=$(inst --guard-repo Owner/Name "$T" 2>&1) || fail "--guard-repo install failed: $OUT"
LST="$HM/.mission-control/guarded-repos.json"; US="$HM/.claude/settings.json"
jq -e '.version == 1 and (.repos | length) == 1 and .repos[0].repo == "Owner/Name" and (.repos[0].common_dir | type) == "string"' "$LST" >/dev/null || fail "guarded list wrong: $(cat "$LST" 2>&1)"
for h in guard-external-tools.py guard-config-write.py mc_guarded.py; do
  [ -f "$HM/.mission-control/hooks/$h" ] || fail "$h was not copied to ~/.mission-control/hooks"
done
UH="$HM/.mission-control/hooks"
[ -x "$UH/sb-python.sh" ] || fail "B4: sb-python.sh was not copied to ~/.mission-control/hooks"
jq -e '.model == "x" and .permissions.allow == ["Bash(ls:*)"]' "$US" >/dev/null || fail "user settings lost a key: $(cat "$US")"
jq -e '[.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[].command] == ["user.sh"]' "$US" >/dev/null || fail "user hook was lost"
jq -e --arg h "$UH" '[.hooks[][].hooks[].command | select(contains($h))] | sort == ([("bash \"" + $h + "/sb-python.sh\" guard-config-write.py --user-scope"), ("bash \"" + $h + "/sb-python.sh\" guard-external-tools.py"), ("bash \"" + $h + "/sb-python.sh\" guard-external-tools.py")] | sort)' "$US" >/dev/null \
  || fail "B4: user settings need the 3 launcher commands: $(cat "$US")"
jq -e '([.hooks.PreToolUse[] | select(.matcher=="Bash|PowerShell|mcp__.*")] | length) == 1 and ([.hooks.SessionStart[] | select(has("matcher") | not)] | length) == 1' "$US" >/dev/null || fail "G-1 matcher / SessionStart shape wrong"
echo "$OUT" | grep -qF "🔒 guarded: Owner/Name (list: " || fail "the guarded line is missing, got: $OUT"
echo "$OUT" | grep -qF "G-1 and config guard at user scope" || fail "the guarded line must name the user-scope guards"
cp "$US" "$HM/before.json"
inst --guard-repo Owner/Name "$T" >/dev/null 2>&1
cmp -s "$US" "$HM/before.json" || fail "a second --guard-repo run changed the user settings: $(diff "$US" "$HM/before.json")"
jq -e '(.repos | length) == 1' "$LST" >/dev/null || fail "a second --guard-repo run duplicated the list entry"
ls "$HM/.claude" | grep -q '^settings.json.bak-' || fail "user settings.json was not backed up before the merge"
# B4: a re-pin replaces an old pinned-path user-scope command instead of adding beside it
python3 - "$US" "$UH" <<'PY'
import json, sys
p, uh = sys.argv[1], sys.argv[2]
d = json.load(open(p))
for e in d["hooks"]["PreToolUse"]:
    for h in e["hooks"]:
        if "guard-config-write.py" in h["command"]:
            h["command"] = '"/old/python" -I "%s/guard-config-write.py"' % uh
json.dump(d, open(p, "w"))
PY
inst --guard-repo Owner/Name "$T" >/dev/null 2>&1
jq -e '[.hooks[][].hooks[].command | select(contains("/old/python"))] | length == 0' "$US" >/dev/null || fail "B4: the old pinned-path command was not replaced: $(cat "$US")"
jq -e '[.hooks[][].hooks[].command | select(contains("guard-config-write.py"))] | length == 1' "$US" >/dev/null || fail "B4: the config guard must appear once after a re-pin"
jq -e '[.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[].command] == ["user.sh"]' "$US" >/dev/null || fail "B4: the user hook must stay"
# B3: a slug that does not match the target's remotes (a typo) exits 65, prints no 🔒 line, and leaves the entry
HM6=$(mktemp -d)
RC=0; OUT=$(HOME="$HM6" USERPROFILE="$HM6" "$INSTALL" --guard-repo Owner/Nme "$T" 2>&1) || RC=$?
[ "$RC" -eq 65 ] || fail "B3: a typo slug must exit 65, got $RC: $OUT"
echo "$OUT" | grep -q '🔒' && fail "B3: no 🔒 line on a failed proof, got: $OUT"
echo "$OUT" | grep -qF "is not matched by Owner/Nme" || fail "B3: the failure must say what did not match, got: $OUT"
jq -e '.repos[0].repo == "Owner/Nme"' "$HM6/.mission-control/guarded-repos.json" >/dev/null || fail "B3: the entry stays in the list"
# a target that is not a git repo cannot be proven either
T3=$(mktemp -d); HM3=$(mktemp -d)
RC=0; HOME="$HM3" USERPROFILE="$HM3" "$INSTALL" --protect-main --guard-repo Owner/Other "$T3" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "B3: a target with no matching remote must exit 65, got $RC"
# --protect-main alongside --guard-repo works on a matching target
RC=0; HOME="$HM3" USERPROFILE="$HM3" "$INSTALL" --protect-main --guard-repo Owner/Name "$T" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "--protect-main --guard-repo failed, rc $RC"
# an invalid user settings.json is left byte-identical; the install still finishes
HM4=$(mktemp -d); mkdir -p "$HM4/.claude"; printf '{ not json' > "$HM4/.claude/settings.json"
RC=0; OUT=$(HOME="$HM4" USERPROFILE="$HM4" "$INSTALL" --guard-repo Owner/Name "$T" 2>&1) || RC=$?
[ "$RC" -eq 70 ] || fail "B3: an invalid user settings.json must exit 70, got $RC: $OUT"
[ "$(cat "$HM4/.claude/settings.json")" = "{ not json" ] || fail "invalid user settings.json was modified"
echo "$OUT" | grep -q '^ *!' || fail "an invalid user settings.json needs a ! line, got: $OUT"
echo "$OUT" | grep -q '🔒' && fail "B3: no 🔒 line when the user-scope merge failed, got: $OUT"
ERRO=$(HOME="$HM4" USERPROFILE="$HM4" "$INSTALL" --guard-repo Owner/Name "$T" 2>&1 >/dev/null) || true
echo "$ERRO" | grep -q '^ *!' || fail "B3: the ! line goes to stderr, got: $ERRO"
# round 2: once Python is resolved, every Python call uses it: a python3 on PATH that always fails (and a real
# python) must not break the install or the user-scope merge
FAKEBIN=$(mktemp -d); printf '#!/bin/sh\nexit 1\n' > "$FAKEBIN/python3"; chmod +x "$FAKEBIN/python3"
ln -s "$(command -v python3)" "$FAKEBIN/python"
HM7=$(mktemp -d)
RC=0; OUT=$(PATH="$FAKEBIN:$PATH" HOME="$HM7" USERPROFILE="$HM7" "$INSTALL" --guard-repo Owner/Name "$T" 2>&1) || RC=$?
[ "$RC" -eq 0 ] || fail "R2: a failing python3 on PATH with a real python must not break --guard-repo, rc $RC: $OUT"
jq -e '[.hooks[][].hooks[].command | select(contains("sb-python.sh"))] | length == 3' "$HM7/.claude/settings.json" >/dev/null || fail "R2: the user settings were not wired: $(cat "$HM7/.claude/settings.json" 2>&1)"
jq -e '[.hooks[][].hooks[].command | select(contains("sb-python.sh"))] | length > 0' "$T/.claude/settings.json" >/dev/null || fail "R2: the project settings were not merged"
rm -rf "$FAKEBIN" "$HM7"
# usage errors
RC=0; inst --no-hooks --guard-repo Owner/Name "$T" >/dev/null 2>&1 || RC=$?; [ "$RC" -eq 64 ] || fail "--guard-repo with --no-hooks must exit 64, got $RC"
RC=0; inst --guard-repo bad "$T" >/dev/null 2>&1 || RC=$?; [ "$RC" -eq 64 ] || fail "--guard-repo bad must exit 64, got $RC"
RC=0; inst "$T" --guard-repo >/dev/null 2>&1 || RC=$?; [ "$RC" -eq 64 ] || fail "--guard-repo with no value must exit 64, got $RC"
# a broken guarded list stops the install with mc_guarded's exit code and is not overwritten
HM5=$(mktemp -d); mkdir -p "$HM5/.mission-control"; printf '{ broken' > "$HM5/.mission-control/guarded-repos.json"
RC=0; HOME="$HM5" USERPROFILE="$HM5" "$INSTALL" --guard-repo Owner/Name "$T" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 70 ] && [ "$(cat "$HM5/.mission-control/guarded-repos.json")" = "{ broken" ] || fail "a broken list must stop the install with exit 70 and stay untouched (rc $RC)"
rm -rf "$T" "$T3" "$HM" "$HM3" "$HM4" "$HM5" "$HM6"

# 15 — an old-style project settings.json (bare python3 commands) is migrated to the launcher: the legacy command
#      is gone, the launcher one is present, and a user hook beside it stays.
T=$(mktemp -d); HM=$(mktemp -d); mkdir -p "$T/.claude"
cat > "$T/.claude/settings.json" <<'JSON'
{"hooks":{"PreToolUse":[{"matcher":"Bash|PowerShell","hooks":[{"type":"command","command":"python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/guard-merge.py"},{"type":"command","command":"user-hook.sh"}]}],"SessionStart":[{"hooks":[{"type":"command","command":"cd \"$CLAUDE_PROJECT_DIR\" && python3 .claude/hooks/cleanup-wt.py --auto --fetch 2>/dev/null || true"}]}]}}
JSON
inst "$T" >/dev/null 2>&1
S="$T/.claude/settings.json"
jq -e '[.hooks[][].hooks[].command | select(. == "python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/guard-merge.py")] | length == 0' "$S" >/dev/null || fail "the legacy guard-merge command survived"
jq -e '[.hooks[][].hooks[].command | select(contains("python3 .claude/hooks/cleanup-wt.py"))] | length == 0' "$S" >/dev/null || fail "the legacy cleanup-wt command survived"
jq -e '[.hooks[][].hooks[].command | select(. == "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-merge.py")] | length == 1' "$S" >/dev/null || fail "the launcher guard-merge command is missing"
jq -e '[.hooks[][].hooks[].command | select(. == "user-hook.sh")] | length == 1' "$S" >/dev/null || fail "the user hook beside the legacy command was lost"
rm -rf "$T" "$HM"

# 9 — the snippet the installer merges is the one hooks/README.md documents.
python3 - ../hooks/README.md ../hooks/settings-snippet.json <<'PY' || fail "hooks/README.md snippet and hooks/settings-snippet.json differ"
import json, re, sys
doc = re.search(r"```json\n(.*?)```", open(sys.argv[1]).read(), re.S).group(1)
sys.exit(0 if json.loads(doc) == json.load(open(sys.argv[2])) else 1)
PY

echo "PASS: test-install.sh (20 scenarios)"
