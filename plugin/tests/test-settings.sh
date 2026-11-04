#!/usr/bin/env bash
# Tests super-board-settings.py allow — onboard's Permissions step: print the diff,
# write only after approval, keep every existing key, add each rule once.
set -euo pipefail
cd "$(dirname "$0")"
S="$PWD/../scripts/super-board-settings.py"
fail() { echo "FAIL: $1" >&2; exit 1; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
F="$T/.claude/settings.json"; mkdir -p "$T/.claude"
echo '{"permissions":{"allow":["Bash(ls:*)"]},"model":"x"}' > "$F"

# 1 — --dry-run prints the diff and writes nothing.
OUT=$(python3 "$S" allow "$F" 'Bash(gh pr merge:*)' 'Bash(ls:*)' --dry-run)
[ "$OUT" = "+ Bash(gh pr merge:*)" ] || fail "dry run should list only the new rule, got: $OUT"
jq -e '.permissions.allow == ["Bash(ls:*)"]' "$F" >/dev/null || fail "dry run wrote the file"

# 2 — a real run adds once, keeps other keys, backs up; a second run is a no-op.
python3 "$S" allow "$F" 'Bash(gh pr merge:*)' >/dev/null
jq -e '.permissions.allow == ["Bash(ls:*)","Bash(gh pr merge:*)"] and .model == "x"' "$F" >/dev/null || fail "merge lost or duplicated: $(cat "$F")"
ls "$T/.claude" | grep -q '^settings.json.bak-' || fail "no backup before writing"
python3 "$S" allow "$F" 'Bash(gh pr merge:*)' | grep -q "already present" || fail "second run should change nothing"

# 3 — invalid JSON is left exactly as it was (exit 2); no file → created.
printf '{ nope' > "$F"; RC=0; python3 "$S" allow "$F" 'X' >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 2 ] && [ "$(cat "$F")" = "{ nope" ] || fail "invalid settings must be untouched (rc $RC)"
python3 "$S" allow "$T/new/settings.json" 'X' >/dev/null && jq -e '.permissions.allow == ["X"]' "$T/new/settings.json" >/dev/null || fail "a missing file should be created"

# 4 — hooks: a plugin command under an old matcher is replaced by the widened one (once),
# a user hook under the same old matcher survives, and a second run is a no-op.
SNIP="$T/snip.json"; H="$T/hooks-settings.json"
echo '{"hooks":{"PreToolUse":[{"matcher":"Bash|PowerShell","hooks":[{"type":"command","command":"plugin-guard.sh"}]}]}}' > "$SNIP"
echo '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"plugin-guard.sh"},{"type":"command","command":"user-hook.sh"}]},{"matcher":"Edit","hooks":[{"type":"command","command":"plugin-guard.sh"}]}]}}' > "$H"
OUT=$(python3 "$S" hooks "$H" "$SNIP" --dry-run)
echo "$OUT" | grep -q '^- PreToolUse Bash: plugin-guard.sh$' || fail "dry run should list the stale entry, got: $OUT"
python3 "$S" hooks "$H" "$SNIP" >/dev/null
jq -e '[.hooks.PreToolUse[] | select(.matcher=="Bash|PowerShell") | .hooks[].command] == ["plugin-guard.sh"]' "$H" >/dev/null || fail "new matcher entry missing or duplicated: $(cat "$H")"
jq -e '[.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[].command] == ["user-hook.sh"]' "$H" >/dev/null || fail "user hook lost or stale plugin hook kept under Bash: $(cat "$H")"
jq -e '[.hooks.PreToolUse[] | select(.matcher != "Edit") | .hooks[].command | select(. == "plugin-guard.sh")] | length == 1' "$H" >/dev/null || fail "plugin command must appear exactly once: $(cat "$H")"
jq -e '[.hooks.PreToolUse[] | select(.matcher=="Edit") | .hooks[].command] == ["plugin-guard.sh"]' "$H" >/dev/null || fail "a disjoint matcher (Edit) must be kept: $(cat "$H")"
BEFORE=$(cat "$H")
python3 "$S" hooks "$H" "$SNIP" | grep -q "already present" || fail "second hooks run should change nothing"
[ "$(cat "$H")" = "$BEFORE" ] || fail "second hooks run rewrote the file"

# 5 — only a strict subset of the wanted tool set is stale: '*', '', '.*', wider sets and
# disjoint sets stay; a plugin-only Bash entry is removed (and the emptied entry dropped).
echo '{"hooks":{"PreToolUse":[{"matcher":"*","hooks":[{"type":"command","command":"plugin-guard.sh"}]},{"matcher":".*","hooks":[{"type":"command","command":"plugin-guard.sh"}]},{"hooks":[{"type":"command","command":"plugin-guard.sh"}]},{"matcher":"Bash|PowerShell|Edit","hooks":[{"type":"command","command":"plugin-guard.sh"}]},{"matcher":"Bash","hooks":[{"type":"command","command":"plugin-guard.sh"}]}]}}' > "$H"
python3 "$S" hooks "$H" "$SNIP" >/dev/null
jq -e '[.hooks.PreToolUse[] | .matcher // ""] | sort == ["","*",".*","Bash|PowerShell","Bash|PowerShell|Edit"]' "$H" >/dev/null || fail "kept/removed matchers wrong: $(cat "$H")"

# 6 — a malformed entry under an unrelated matcher does not stop the merge.
echo '{"hooks":{"PreToolUse":["oops",{"matcher":"Read","hooks":"nope"},{"matcher":"Read","hooks":["x",{"command":"u.sh"}]},{"matcher":"Bash","hooks":[7,{"type":"command","command":"plugin-guard.sh"}]}],"Stop":"junk"}}' > "$H"
RC=0; python3 "$S" hooks "$H" "$SNIP" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 0 ] || fail "malformed entries must not crash the merge (rc $RC)"
jq -e '[.hooks.PreToolUse[] | objects | select(.matcher=="Bash|PowerShell") | .hooks[].command] == ["plugin-guard.sh"]' "$H" >/dev/null || fail "merge did not happen: $(cat "$H")"

# 7 — legacy migration: a plugin-shaped python3/python command for a hook the snippet now launches through
# sb-python.sh is removed (any matcher, listed in --dry-run), so the old and the new form never both run.
# User hooks, other python hooks, non-plugin paths and unknown shapes stay.
L='bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/sb-python.sh'
LEG3='python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/guard-merge.py'
LEGP='python "$CLAUDE_PROJECT_DIR"/.claude/hooks/guard-merge.py'
LEGC='cd "$CLAUDE_PROJECT_DIR" && python3 .claude/hooks/cleanup-wt.py --auto --fetch 2>/dev/null || true'
jq -n --arg l "$L" '{hooks:{PreToolUse:[{matcher:"Bash|PowerShell",hooks:[{type:"command",command:($l+" guard-merge.py")}]}],
  SessionStart:[{hooks:[{type:"command",command:("cd \"$CLAUDE_PROJECT_DIR\" && "+$l+" cleanup-wt.py --auto --fetch 2>/dev/null || true")}]}]}}' > "$SNIP"
jq -n --arg a "$LEG3" --arg b "$LEGP" --arg c "$LEGC" '{model:"x",hooks:{PreToolUse:[
  {matcher:"Bash|PowerShell",hooks:[{type:"command",command:$a},{type:"command",command:"user-hook.sh"},{type:"command",command:"python3 /opt/other/guard-merge.py"},{type:"command",command:"python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/my-own-hook.py"}]},
  {matcher:"Bash",hooks:[{type:"command",command:$b}]},
  {matcher:"Edit",hooks:[{type:"command",command:"python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/guard-merge.py --extra"}]}],
  SessionStart:[{hooks:[{type:"command",command:$c},{type:"command",command:"echo hi"}]}]}}' > "$H"
OUT=$(python3 "$S" hooks "$H" "$SNIP" --dry-run)
echo "$OUT" | grep -qxF -- "- PreToolUse Bash|PowerShell: $LEG3" || fail "dry run should list the python3 legacy command, got: $OUT"
echo "$OUT" | grep -qxF -- "- PreToolUse Bash: $LEGP" || fail "dry run should list the python legacy command, got: $OUT"
echo "$OUT" | grep -qxF -- "- SessionStart *: $LEGC" || fail "dry run should list the legacy cleanup-wt command, got: $OUT"
echo "$OUT" | grep -qxF -- "+ PreToolUse Bash|PowerShell: $L guard-merge.py" || fail "dry run should list the new launcher command, got: $OUT"
cp "$H" "$T/before.json"; python3 "$S" hooks "$H" "$SNIP" --dry-run >/dev/null
cmp -s "$H" "$T/before.json" || fail "dry run wrote the file"
python3 "$S" hooks "$H" "$SNIP" >/dev/null
CMDS=$(jq -r '[.hooks[][].hooks[].command] | .[]' "$H")
for gone in "$LEG3" "$LEGP" "$LEGC"; do
  printf '%s\n' "$CMDS" | grep -qxF -- "$gone" && fail "legacy command survived: $gone"
done
[ "$(printf '%s\n' "$CMDS" | grep -cxF -- "$L guard-merge.py")" -eq 1 ] || fail "launcher command must appear exactly once: $CMDS"
for kept in 'user-hook.sh' 'python3 /opt/other/guard-merge.py' 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/my-own-hook.py' 'echo hi' 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/guard-merge.py --extra'; do
  printf '%s\n' "$CMDS" | grep -qxF -- "$kept" || fail "a non-legacy command was removed: $kept"
done
jq -e '.model == "x"' "$H" >/dev/null || fail "migration lost a key"
jq -e '[.hooks.PreToolUse[] | select(.matcher=="Bash")] | length == 0' "$H" >/dev/null || fail "the emptied Bash entry should be dropped: $(cat "$H")"
python3 "$S" hooks "$H" "$SNIP" | grep -q "already present" || fail "second migration run should change nothing"
# a legacy command is only removed for a hook the snippet launches through sb-python.sh
echo '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"python3 \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/guard-secrets.py"}]}]}}' > "$H"
python3 "$S" hooks "$H" "$SNIP" >/dev/null
jq -e '[.hooks[][].hooks[].command | select(contains("guard-secrets.py"))] | length == 1' "$H" >/dev/null || fail "a hook the snippet does not launch must keep its entry"

# 8 — user-hooks: the user-scope guard commands (through the user-scope launcher) merged into a given settings
# file: keeps every key, once per event+matcher, backs up, leaves invalid JSON untouched; an earlier command for
# the same hook under ~/.mission-control/hooks (the old pinned-path form) is replaced, not duplicated.
U="$T/user-settings.json"; D=/home/u/.mission-control/hooks
EXT='bash "/home/u/.mission-control/hooks/sb-python.sh" guard-external-tools.py'
CFG='bash "/home/u/.mission-control/hooks/sb-python.sh" guard-config-write.py --user-scope'
echo '{"model":"x","permissions":{"allow":["Bash(ls:*)"]},"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"user.sh"}]}]}}' > "$U"
python3 "$S" user-hooks "$U" --hooks-dir "$D" >/dev/null
jq -e '.model == "x" and .permissions.allow == ["Bash(ls:*)"]' "$U" >/dev/null || fail "user-hooks lost a key: $(cat "$U")"
jq -e '[.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[].command] == ["user.sh"]' "$U" >/dev/null || fail "user hook changed: $(cat "$U")"
jq -e --arg c "$EXT" '[.hooks.PreToolUse[] | select(.matcher=="Bash|PowerShell|mcp__.*") | .hooks[].command] == [$c]' "$U" >/dev/null || fail "G-1 PreToolUse entry wrong: $(cat "$U")"
jq -e --arg c "$EXT" '[.hooks.SessionStart[].hooks[].command] == [$c]' "$U" >/dev/null || fail "G-1 SessionStart entry wrong: $(cat "$U")"
jq -e --arg c "$CFG" '[.hooks.PreToolUse[] | select(.matcher=="Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell") | .hooks[].command] == [$c]' "$U" >/dev/null || fail "config guard entry wrong: $(cat "$U")"
ls "$T" | grep -q '^user-settings.json.bak-' || fail "user-hooks wrote without a backup"
cp "$U" "$T/u-before.json"
python3 "$S" user-hooks "$U" --hooks-dir "$D" | grep -q "already present" || fail "second user-hooks run should change nothing"
cmp -s "$U" "$T/u-before.json" || fail "second user-hooks run rewrote the file"
# re-pin dedupe: the old pinned-path commands are replaced, a user hook stays
echo '{"hooks":{"PreToolUse":[{"matcher":"Bash|PowerShell|mcp__.*","hooks":[{"type":"command","command":"\"/old/python\" -I \"/home/u/.mission-control/hooks/guard-external-tools.py\""},{"type":"command","command":"user.sh"}]},{"matcher":"Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell","hooks":[{"type":"command","command":"\"/old/python\" -I \"/home/u/.mission-control/hooks/guard-config-write.py\""}]}],"SessionStart":[{"hooks":[{"type":"command","command":"\"/old/python\" -I \"/home/u/.mission-control/hooks/guard-external-tools.py\""}]}]}}' > "$U"
OUT=$(python3 "$S" user-hooks "$U" --hooks-dir "$D" --dry-run)
echo "$OUT" | grep -qF -- '- PreToolUse Bash|PowerShell|mcp__.*: "/old/python" -I "/home/u/.mission-control/hooks/guard-external-tools.py"' || fail "dry run should list the old pinned-path command, got: $OUT"
python3 "$S" user-hooks "$U" --hooks-dir "$D" >/dev/null
CMDS=$(jq -r '[.hooks[][].hooks[].command] | .[]' "$U")
printf '%s\n' "$CMDS" | grep -q '/old/python' && fail "an old pinned-path command survived: $CMDS"
[ "$(printf '%s\n' "$CMDS" | grep -cxF -- "$EXT")" -eq 2 ] || fail "G-1 must appear exactly twice (PreToolUse, SessionStart): $CMDS"
[ "$(printf '%s\n' "$CMDS" | grep -cxF -- "$CFG")" -eq 1 ] || fail "the config guard must appear once: $CMDS"
printf '%s\n' "$CMDS" | grep -qxF user.sh || fail "the user hook was removed: $CMDS"
printf '{ nope' > "$U"; RC=0; python3 "$S" user-hooks "$U" --hooks-dir /h >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 2 ] && [ "$(cat "$U")" = "{ nope" ] || fail "invalid user settings must be untouched (rc $RC)"
RC=0; python3 "$S" user-hooks "$U" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "user-hooks without --hooks-dir is a usage error (rc $RC)"

# 9 — the shipped snippet wires the orchestrator guard once, on the file-tool + shell matcher (#69).
S2="$T/proj-settings.json"
echo '{"hooks":{"PreToolUse":[{"matcher":"Bash","hooks":[{"type":"command","command":"user.sh"}]}]}}' > "$S2"
python3 "$S" hooks "$S2" "$PWD/../hooks/settings-snippet.json" >/dev/null
jq -e '[.hooks.PreToolUse[] | select(.matcher=="Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell") | .hooks[].command | select(contains("guard-orchestrator.py"))] | length == 1' "$S2" >/dev/null \
  || fail "the snippet must wire guard-orchestrator.py once on the file-tool matcher: $(cat "$S2")"
jq -e '[.hooks[][].hooks[].command | select(contains("guard-orchestrator.py"))] | length == 1' "$S2" >/dev/null \
  || fail "guard-orchestrator.py must be wired exactly once overall: $(cat "$S2")"
jq -e '[.hooks.PreToolUse[] | select(.matcher=="Bash") | .hooks[].command] == ["user.sh"]' "$S2" >/dev/null \
  || fail "the snippet must not touch a foreign Bash matcher: $(cat "$S2")"

echo "PASS: test-settings.sh (9 scenarios)"
