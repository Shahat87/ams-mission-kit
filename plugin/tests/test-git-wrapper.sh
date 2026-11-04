#!/usr/bin/env bash
# Tests super-board-git.sh — the hardened git wrapper. Hostile global AND repo-local config (alias, clean/smudge filter,
# textconv driver, diff.external, fsmonitor, hooks in .git/hooks and a global hooksPath), a hostile .gitattributes and hostile
# GIT_* environment variables must never get to run a program through the wrapper; forbidden args, global options and
# subcommands exit 64; --robot accepts exactly one push shape and wires gh as the credential helper. No network: gh is a stub.
set -uo pipefail
KIT=$(cd "$(dirname "$0")/.." && pwd)
W="$KIT/scripts/super-board-git.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
N=0
ok() { N=$((N + 1)); }
fail() { echo "FAIL: $1" >&2; exit 1; }

# Clean env for setup and for the "bare" control: no inherited identity or git config switches.
unset GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM GIT_ALLOW_PROTOCOL SB_ROBOT_GH_CONFIG_DIR GH_CONFIG_DIR SB_PROTECTED_BRANCHES \
      GIT_EXTERNAL_DIFF GIT_CONFIG_COUNT GH_TOKEN
export HOME="$T/home" XDG_CONFIG_HOME="$T/home/.config"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid
mkdir -p "$HOME" "$T/hooks" "$T/bin"
PWNED="$T/PWNED"
# paths written into git config must be ones the native git can spawn (Git Bash /tmp/... is not): cygpath -m when present.
WP() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

for h in pre-commit post-commit post-checkout; do
  printf '#!/bin/sh\ntouch "%s"\n' "$PWNED" > "$T/hooks/$h"
  chmod +x "$T/hooks/$h"
done
printf '#!/bin/sh\ntouch "%s"\ncat "$1"\n' "$PWNED" > "$T/textconv.sh"
printf '#!/bin/sh\ntouch "%s"\nexit 0\n' "$PWNED" > "$T/ext.sh"
printf '#!/bin/sh\ntouch "%s"\nexit 0\n' "$PWNED" > "$T/fsm.sh"
chmod +x "$T/textconv.sh" "$T/ext.sh" "$T/fsm.sh"
cat > "$HOME/.gitconfig" <<EOF
[alias]
	st = !touch "$PWNED"
[filter "x"]
	clean = touch "$PWNED" && cat
	smudge = touch "$PWNED" && cat
[diff "y"]
	textconv = $(WP "$T/textconv.sh")
[core]
	hooksPath = $(WP "$T/hooks")
EOF

REPO="$T/repo"
mkdir -p "$REPO"
git -C "$REPO" init -q 2>/dev/null
# repo-local hostility: hooks in .git/hooks, a filter, a textconv driver, diff.external and fsmonitor in .git/config.
for h in pre-commit post-commit post-checkout; do
  printf '#!/bin/sh\ntouch "%s"\n' "$PWNED" > "$REPO/.git/hooks/$h"
  chmod +x "$REPO/.git/hooks/$h"
done
cat >> "$REPO/.git/config" <<EOF
[core]
	hooksPath = $(WP "$REPO/.git/hooks")
	fsmonitor = $(WP "$T/fsm.sh")
[filter "x"]
	clean = touch "$PWNED" && cat
	smudge = touch "$PWNED" && cat
[diff "y"]
	textconv = $(WP "$T/textconv.sh")
[diff]
	external = $(WP "$T/ext.sh")
EOF
printf '*.txt filter=x diff=y\n' > "$REPO/.gitattributes"
echo one > "$REPO/a.txt"

# 0 — control: plain git in this HOME and repo does run the hostile config (so the wrapper tests mean something).
(cd "$REPO" && git add a.txt .gitattributes >/dev/null 2>&1)
[ -e "$PWNED" ] || fail "control: hostile config did not fire under plain git (add); the test setup is wrong"
rm -f "$PWNED"
(cd "$REPO" && git commit -q -m one >/dev/null 2>&1)
[ -e "$PWNED" ] || fail "control: .git/hooks/pre-commit did not fire under plain git (commit); the test setup is wrong"
rm -f "$PWNED"
echo two >> "$REPO/a.txt"
(cd "$REPO" && git diff >/dev/null 2>&1)
[ -e "$PWNED" ] || fail "control: diff.external / textconv did not fire under plain git (diff); the test setup is wrong"
rm -f "$PWNED"
(cd "$REPO" && GIT_EXTERNAL_DIFF="$(WP "$T/ext.sh")" git -c diff.external= diff >/dev/null 2>&1)
[ -e "$PWNED" ] || fail "control: GIT_EXTERNAL_DIFF did not fire under plain git; the test setup is wrong"
rm -f "$PWNED"
(cd "$REPO" && git add a.txt >/dev/null 2>&1 && git commit -q -m two >/dev/null 2>&1)
rm -f "$PWNED"
(cd "$REPO" && git checkout -q -b side >/dev/null 2>&1 && git checkout -q - >/dev/null 2>&1)
[ -e "$PWNED" ] || fail "control: .git/hooks/post-checkout did not fire under plain git (checkout); the test setup is wrong"
rm -f "$PWNED"
echo three >> "$REPO/a.txt"      # a modified tracked file, so diff has something to run a driver on
ok

g() { (cd "$REPO" && bash "$W" "$@"); }
nopwn() { [ ! -e "$PWNED" ] || fail "$1: PWNED exists, the wrapper ran a config-driven program"; }
refused() {   # refused LABEL args... : exits 64, runs nothing
  local label="$1"; shift
  local rc=0; g "$@" >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 64 ] || fail "$label should exit 64, got $rc"
  nopwn "$label"; ok
}

# 1 — the allowed read/object subcommands run no hostile program, under every hostile setting above.
g -- status >/dev/null 2>&1; nopwn status; ok
g -- diff >/dev/null 2>&1; nopwn diff; ok
g -- diff --cached >/dev/null 2>&1; nopwn "diff --cached"; ok
g -- diff HEAD~1 HEAD >/dev/null 2>&1; nopwn "diff HEAD~1 HEAD"; ok
g -- show HEAD >/dev/null 2>&1; nopwn show; ok
g -- log -p >/dev/null 2>&1; nopwn "log -p"; ok
g -- ls-tree -r HEAD >/dev/null 2>&1; nopwn ls-tree; ok
g -- cat-file -p HEAD >/dev/null 2>&1; nopwn cat-file; ok
g -- for-each-ref >/dev/null 2>&1; nopwn for-each-ref; ok
g -- worktree list >/dev/null 2>&1; nopwn "worktree list"; ok
[ "$(g -- rev-list --count HEAD 2>/dev/null)" = 2 ] || fail "rev-list --count HEAD should be 2"
ok
# the objects-only pipeline, with a private index: read-tree, apply --cached, write-tree, commit-tree.
printf 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,3 @@\n one\n two\n+extra\n' > "$T/p.patch"
IDX="$T/private-index"
export_idx() { (cd "$REPO" && GIT_INDEX_FILE="$IDX" bash "$W" "$@"); }
export_idx -- read-tree HEAD >/dev/null 2>&1 || fail "read-tree through the wrapper failed"
export_idx -- apply --cached -p1 "$T/p.patch" >/dev/null 2>&1 || fail "apply --cached through the wrapper failed"
TREE=$(export_idx -- write-tree 2>/dev/null) || fail "write-tree through the wrapper failed"
COMMIT=$(export_idx -- commit-tree "$TREE" -p HEAD -m via-wrapper 2>/dev/null) || fail "commit-tree through the wrapper failed"
[ -n "$COMMIT" ] || fail "commit-tree printed no sha"
nopwn "object pipeline"; ok
[ "$(cd "$REPO" && git show "$COMMIT:a.txt" 2>/dev/null | wc -l | tr -d ' ')" = 3 ] || fail "the object pipeline did not produce the patched tree"
rm -f "$PWNED"; ok

# 1b — hostile environment variables are scrubbed: a plain-git control fired GIT_EXTERNAL_DIFF above.
(cd "$REPO" && GIT_EXTERNAL_DIFF="$(WP "$T/ext.sh")" bash "$W" -- diff >/dev/null 2>&1); nopwn "GIT_EXTERNAL_DIFF"; ok
(cd "$REPO" && GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0="touch $PWNED" \
   GIT_CONFIG_KEY_1=diff.external GIT_CONFIG_VALUE_1="$(WP "$T/ext.sh")" bash "$W" -- diff >/dev/null 2>&1)
nopwn "GIT_CONFIG_COUNT"; ok
(cd "$REPO" && GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.pager GIT_CONFIG_VALUE_0="touch $PWNED" bash "$W" -- log -p >/dev/null 2>&1)
nopwn "GIT_CONFIG_COUNT core.pager"; ok
(cd "$REPO" && GIT_DIR="$T/nowhere" bash "$W" -- rev-parse --git-dir 2>/dev/null | grep -q nowhere) && fail "GIT_DIR leaked through the wrapper"
ok

# 2 — usage, forbidden arguments, global options and subcommands exit 64.
RC=0; bash "$W" status >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "missing -- should exit 64, got $RC"; ok
RC=0; bash "$W" -- >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "no git args should exit 64, got $RC"; ok
for bad in "-c" "--config-env=core.pager=X" "--exec-path=/x" "--exec=x" "--upload-pack=x" "--receive-pack=x" "-O" "-Ox" \
           "--open-files-in-pager" "--output" "--output=$T/out"; do
  refused "forbidden arg $bad" -- log "$bad"
done
[ ! -e "$T/out" ] || fail "--output wrote a file"
refused "fetch -u" -- fetch -u origin
refused "fetch --upload-pack" -- fetch --upload-pack=x origin
refused "fetch --upload-p" -- fetch --upload-p x origin
refused "ls-remote --upload-pack" -- ls-remote --upload-pack=x origin
# only -C DIR is a global option
refused "--git-dir X diff" -- --git-dir "$T/nowhere" diff
refused "--git-dir=X diff" -- --git-dir="$T/nowhere" diff
refused "--work-tree X diff" -- --work-tree "$T" diff
refused "--work-tree=X diff" -- --work-tree="$T" diff
refused "--namespace X" -- --namespace x status
refused "--attr-source" -- --attr-source=HEAD status
refused "--super-prefix" -- --super-prefix=x/ status
refused "--list-cmds" -- --list-cmds=main
refused "-c as a global" -- -c core.pager=x status
refused "-C without a dir" -- -C
# subcommands outside the allowlist
for sub in add commit checkout switch restore reset push pull rebase merge config clone init gc credential st alias submodule \
           branch tag stash clean am bisect grep difftool mergetool; do
  refused "subcommand $sub" -- "$sub"
done
refused "worktree add" -- worktree add "$T/wt"
refused "worktree (bare)" -- worktree
refused "apply without --cached" -- apply "$T/p.patch"
nopwn "refused subcommands"
ok

# 3 — --robot: exactly one argv shape, validated before anything else (64); then a gh config dir (66); then gh path (66).
SHA=$(cd "$REPO" && git rev-parse HEAD)
SHA64=$(printf '%s' "$SHA" | sha256sum | cut -d' ' -f1)
cat > "$T/bin/gh" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SB_TEST_GH_LOG"
printf 'GH_CONFIG_DIR=%s\n' "${GH_CONFIG_DIR:-}" >> "$SB_TEST_GH_LOG"
printf 'GH_TOKEN=%s\n' "${GH_TOKEN:-}" >> "$SB_TEST_GH_LOG"
printf 'GH_HOST=%s\n' "${GH_HOST:-}" >> "$SB_TEST_GH_LOG"
exit 1
SH
chmod +x "$T/bin/gh"
export SB_TEST_GH_LOG="$T/gh.log"
ORIG_PATH="$PATH"
export PATH="$T/bin:$PATH"
robot_refused() {
  local label="$1"; shift
  local rc=0; (cd "$REPO" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- "$@") >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 64 ] || fail "--robot $label should exit 64, got $rc"
  [ ! -e "$SB_TEST_GH_LOG" ] || fail "--robot $label reached gh"
  ok
}
rm -f "$SB_TEST_GH_LOG"
robot_refused "push -f" -C "$REPO" push -f origin "$SHA:refs/heads/feat"
robot_refused "push --force" -C "$REPO" push --force origin "$SHA:refs/heads/feat"
robot_refused "push --delete" -C "$REPO" push --delete origin feat
robot_refused "plus refspec" -C "$REPO" push origin "+$SHA:refs/heads/feat"
robot_refused "sha:main" -C "$REPO" push origin "$SHA:refs/heads/main"
robot_refused "sha:master" -C "$REPO" push origin "$SHA:refs/heads/master"
robot_refused "sha:Staging" -C "$REPO" push origin "$SHA:refs/heads/Staging"
robot_refused "sha:origin/main" -C "$REPO" push origin "$SHA:refs/heads/origin/main"
robot_refused "sha:HEAD" -C "$REPO" push origin "$SHA:refs/heads/HEAD"
robot_refused "abbreviated sha" -C "$REPO" push origin "${SHA:0:12}:refs/heads/feat"
robot_refused "not a refs/heads target" -C "$REPO" push origin "$SHA:feat"
robot_refused "name with .." -C "$REPO" push origin "$SHA:refs/heads/a..b"
robot_refused "name with //" -C "$REPO" push origin "$SHA:refs/heads/a//b"
robot_refused "name starting with -" -C "$REPO" push origin "$SHA:refs/heads/-x"
robot_refused "name with a space" -C "$REPO" push origin "$SHA:refs/heads/a b"
robot_refused "two refspecs" -C "$REPO" push origin "$SHA:refs/heads/a" "$SHA:refs/heads/b"
robot_refused "option before origin" -C "$REPO" push --no-verify origin "$SHA:refs/heads/feat"
robot_refused "other remote" -C "$REPO" push elsewhere "$SHA:refs/heads/feat"
robot_refused "no -C" push origin "$SHA:refs/heads/feat"
robot_refused "credential fill" credential fill
robot_refused "fetch" -C "$REPO" fetch origin main
robot_refused "status" -C "$REPO" status
robot_refused "-c" -c core.pager=x -C "$REPO" push origin "$SHA:refs/heads/feat"
RC=0; (cd "$REPO" && SB_PROTECTED_BRANCHES="release,Hotfix/x" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO" push origin "$SHA:refs/heads/hotfix/x") >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "--robot push to a branch listed in SB_PROTECTED_BRANCHES should exit 64, got $RC"; ok
RC=0; (cd "$REPO" && SB_PROTECTED_BRANCHES="release" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO" push origin "$SHA:refs/heads/origin/RELEASE") >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "--robot push to origin/RELEASE (protected release) should exit 64, got $RC"; ok

RC=0; g --robot -- -C "$REPO" push origin "$SHA:refs/heads/feat" >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 66 ] || fail "--robot without a gh config dir should exit 66, got $RC"; ok
# gh found only in a directory whose name has a quote: refused (66), because the helper line is shell-quoted.
mkdir -p "$T/it's"; cp "$T/bin/gh" "$T/it's/gh"
RC=0; (cd "$REPO" && PATH="$T/it's:$ORIG_PATH" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO" push origin "$SHA:refs/heads/feat") >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 66 ] || fail "--robot with a gh path that contains a quote should exit 66, got $RC"
[ ! -e "$SB_TEST_GH_LOG" ] || fail "gh ran for a quoted path"; ok

# 4 — --robot push to a file:// remote: the shape passes, https-only protocol blocks it with a transport error (not 64/66).
git init -q --bare "$T/bare.git" 2>/dev/null
(cd "$REPO" && git remote add origin "file://$T/bare.git" >/dev/null 2>&1)
RC=0; ERR=$( (cd "$REPO" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO" push origin "$SHA:refs/heads/feat") 2>&1 >/dev/null) || RC=$?
[ "$RC" -ne 0 ] && [ "$RC" -ne 64 ] && [ "$RC" -ne 66 ] || fail "--robot push to file:// should fail with a transport error, got $RC: $ERR"
echo "$ERR" | grep -qi "not allowed\|transport" || fail "--robot push to file:// failed for another reason: $ERR"
[ -z "$(git -C "$T/bare.git" for-each-ref 2>/dev/null)" ] || fail "--robot push to file:// created a ref"
nopwn "robot push"; ok
# the same shape with a 64-hex sha passes validation too (it then fails in git, not at 64).
RC=0; (cd "$REPO" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO" push origin "$SHA64:refs/heads/feat") >/dev/null 2>&1 || RC=$?
[ "$RC" -ne 64 ] && [ "$RC" -ne 66 ] || fail "a 64-hex sha should pass the shape check, got $RC"; ok

# 5 — credential wiring and token scrub. The production wrapper refuses `credential`, so this drives a copy with only the
# shape check removed (everything after it is identical): git asks the stub gh for `auth git-credential`.
sed '/# BEGIN ROBOT SHAPE/,/# END ROBOT SHAPE/d' "$W" > "$T/wrapper-noshape.sh"
grep -q 'ROBOT SHAPE' "$T/wrapper-noshape.sh" && fail "could not strip the shape block"
rm -f "$SB_TEST_GH_LOG"
printf 'protocol=https\nhost=example.invalid\n\n' | GH_TOKEN=leak-token GH_HOST=leak.example SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" \
  bash "$T/wrapper-noshape.sh" --robot -- credential fill >/dev/null 2>&1 || true
[ -f "$SB_TEST_GH_LOG" ] || fail "--robot never invoked the stub gh"
grep -q 'auth git-credential get' "$SB_TEST_GH_LOG" || fail "stub gh not called with auth git-credential get"; ok
grep -q "GH_CONFIG_DIR=.*/robot-gh$" "$SB_TEST_GH_LOG" || fail "stub gh did not receive the robot GH_CONFIG_DIR"; ok
grep -q '^GH_TOKEN=$' "$SB_TEST_GH_LOG" || fail "GH_TOKEN leaked to gh"; ok
grep -q '^GH_HOST=$' "$SB_TEST_GH_LOG" || fail "GH_HOST leaked to gh"; ok

# 6 — without --robot no credential helper runs, and `credential` is refused outright.
rm -f "$SB_TEST_GH_LOG"
RC=0; printf 'protocol=https\nhost=example.invalid\n\n' | SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" -- credential fill >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "credential without --robot should exit 64, got $RC"
[ ! -e "$SB_TEST_GH_LOG" ] || fail "a credential helper ran without --robot"; ok
RC=0; printf 'protocol=https\nhost=example.invalid\n\n' | SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$T/wrapper-noshape.sh" -- credential fill >/dev/null 2>&1 || RC=$?
[ ! -e "$SB_TEST_GH_LOG" ] || fail "a credential helper ran without --robot (shape check removed)"; ok

# 7 — the wrapper's OVERRIDES list equals OVERRIDES in super-board-lane-delegate.py.
cat > "$T/cmp.py" <<'PY'
import ast, re, sys
sh = open(sys.argv[1], encoding="utf-8").read()
block = sh.split("# BEGIN OVERRIDES", 1)[1].split("# END OVERRIDES", 1)[0]
arr = re.search(r"^OVERRIDES=\((.*)\)\s*$", block, re.M).group(1)
wrapper = [s.replace("$NULLDEV", "@NULL") for s in re.findall(r'"([^"]*)"', arr)]
py = open(sys.argv[2], encoding="utf-8").read()
node = next(n for n in ast.parse(py).body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "OVERRIDES")
def lit(e):
    if isinstance(e, ast.Constant): return e.value
    return "".join("@NULL" if isinstance(v, ast.FormattedValue) else v.value for v in e.values)
ref = [lit(e) for e in node.value.elts]
if wrapper != ref:
    print("wrapper:", wrapper, "\nlane-delegate:", ref, file=sys.stderr)
    sys.exit(1)
PY
python3 "$T/cmp.py" "$W" "$KIT/scripts/super-board-lane-delegate.py" || fail "OVERRIDES drifted between the wrapper and super-board-lane-delegate.py"
ok

# 8 — drivers whose names are not plain words (x+y, k=v, "a b"): the config keys are read with -z and blanked through
# GIT_CONFIG_COUNT/KEY/VALUE verbatim. A space cannot appear in a .gitattributes value, so "a b" is only defined, the others are used.
cat >> "$REPO/.git/config" <<EOF
[filter "x+y"]
	clean = touch "$PWNED" && cat
	smudge = touch "$PWNED" && cat
[filter "a b"]
	clean = touch "$PWNED" && cat
[diff "k=v"]
	textconv = $(WP "$T/textconv.sh")
	command = $(WP "$T/ext.sh")
[merge "m w"]
	driver = touch "$PWNED"
EOF
printf 'b.dat filter=x+y diff=k=v\n' >> "$REPO/.gitattributes"
echo b1 > "$REPO/b.dat"
rm -f "$PWNED"
(cd "$REPO" && git add b.dat >/dev/null 2>&1)
[ -e "$PWNED" ] || fail "control: filter x+y did not fire under plain git; the test setup is wrong"
rm -f "$PWNED"
(cd "$REPO" && git -c core.hooksPath=/dev/null commit -q -m b >/dev/null 2>&1)
rm -f "$PWNED"
echo b2 >> "$REPO/b.dat"
(cd "$REPO" && git diff >/dev/null 2>&1)
[ -e "$PWNED" ] || fail "control: diff driver k=v did not fire under plain git; the test setup is wrong"
rm -f "$PWNED"
g -- diff >/dev/null 2>&1; nopwn "diff with odd driver names"; ok
g -- status >/dev/null 2>&1; nopwn "status with odd driver names"; ok
g -- show HEAD >/dev/null 2>&1; nopwn "show with odd driver names"; ok
g -- log -p >/dev/null 2>&1; nopwn "log -p with odd driver names"; ok

# 9 — remote.*.uploadpack and friends are neutralised: a plain-git fetch runs the program, the wrapper's does not.
printf '#!/bin/sh\ntouch "%s"\nexec git-upload-pack "$@"\n' "$PWNED" > "$T/up.sh"; chmod +x "$T/up.sh"
git init -q --bare "$T/up.git" 2>/dev/null
cat >> "$REPO/.git/config" <<EOF
[remote "upl"]
	url = file://$(WP "$T/up.git")
	uploadpack = $(WP "$T/up.sh")
[core]
	alternateRefsCommand = touch "$PWNED"
EOF
rm -f "$PWNED"
(cd "$REPO" && git fetch -q upl >/dev/null 2>&1)
[ -e "$PWNED" ] || fail "control: remote.upl.uploadpack did not fire under plain git; the test setup is wrong"
rm -f "$PWNED"
# git keeps the FIRST uploadpack it reads, so it cannot be blanked from the command line: the wrapper refuses instead.
refused "fetch with remote.upl.uploadpack" -- fetch upl
refused "ls-remote with remote.upl.uploadpack" -- ls-remote upl
g -- status >/dev/null 2>&1; nopwn "status with remote.upl.uploadpack"; ok
# core.alternateRefsCommand is blanked (last value wins): a fetch that does not use the uploadpack remote still runs clean.
g -- fetch origin >/dev/null 2>&1; nopwn "fetch origin with core.alternateRefsCommand"; ok

# 10 — gpg and signatures, diff drivers: refused with 64.
for bad in "-S" "-Sfoo" "--gpg-sign" "--gpg-sign=x" "--show-signature" "--format=%GG" "--format=%H %G?" "--pretty=%GS" "--pretty=format:%G?"; do
  refused "log $bad" -- log "$bad"
done
refused "show --show-signature" -- show --show-signature HEAD
refused "diff --ext-diff" -- diff --ext-diff
refused "diff --ext-diff=x" -- diff --ext-diff=x
refused "diff --textconv" -- diff --textconv
refused "show --textconv" -- show --textconv HEAD
refused "log --ext-diff" -- log -p --ext-diff
g -- log --format=%H >/dev/null 2>&1 || fail "a plain --format=%H should still be allowed"; ok

# 11 — SB_PROTECTED_BRANCHES: comma split, trimmed, normalised, case-insensitive, no globbing.
for list in "release, dev" "release,dev " " Dev" "refs/heads/dev,x" "origin/DEV" "refs/remotes/origin/dev"; do
  RC=0; (cd "$REPO" && SB_PROTECTED_BRANCHES="$list" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO" push origin "$SHA:refs/heads/dev") >/dev/null 2>&1 || RC=$?
  [ "$RC" -eq 64 ] || fail "SB_PROTECTED_BRANCHES='$list' should protect dev (64), got $RC"; ok
done
RC=0; (cd "$REPO" && SB_PROTECTED_BRANCHES="*" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO" push origin "$SHA:refs/heads/dev") >/dev/null 2>&1 || RC=$?
[ "$RC" -ne 64 ] || fail "a glob in SB_PROTECTED_BRANCHES must not protect everything"; ok

# 12 — robot hardening, seen through a stub git that records what the wrapper finally runs.
mkdir -p "$T/stubgit"
cat > "$T/stubgit/git" <<'SH'
#!/bin/sh
{
  printf 'ARGS:%s\n' "$*"
  printf 'PARAMS=%s\n' "${GIT_CONFIG_PARAMETERS-unset}"
  printf 'PAGER=%s\n' "${GIT_PAGER-unset}"
  printf 'HTTPS_PROXY=%s\n' "${HTTPS_PROXY-unset}"
  printf 'http_proxy=%s\n' "${http_proxy-unset}"
  printf 'ALL_PROXY=%s\n' "${ALL_PROXY-unset}"
  printf 'SSL_CERT_FILE=%s\n' "${SSL_CERT_FILE-unset}"
  printf 'GIT_SSL_NO_VERIFY=%s\n' "${GIT_SSL_NO_VERIFY-unset}"
  printf 'CURL_CA_BUNDLE=%s\n' "${CURL_CA_BUNDLE-unset}"
  printf -- '--\n'
} >> "$SB_TEST_GIT_LOG"
exit 0
SH
chmod +x "$T/stubgit/git"
export SB_TEST_GIT_LOG="$T/git.log"
rm -f "$SB_TEST_GIT_LOG"
(cd "$REPO" && PATH="$T/stubgit:$PATH" HTTPS_PROXY=http://evil.invalid http_proxy=http://evil.invalid ALL_PROXY=x SSL_CERT_FILE=/evil CURL_CA_BUNDLE=/evil \
   GIT_SSL_NO_VERIFY=1 GIT_PAGER="touch $PWNED" GIT_CONFIG_PARAMETERS="'core.pager'='touch $PWNED'" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" \
   bash "$W" --robot -- -C "$REPO" push origin "$SHA:refs/heads/feat") >/dev/null 2>&1
LAST=$(awk 'BEGIN{RS="--\n"} /ARGS:.* push /{r=$0} END{printf "%s", r}' "$SB_TEST_GIT_LOG")
[ -n "$LAST" ] || fail "the stub git never saw the push"
for want in "push.followTags=false" "push.recurseSubmodules=no" "http.sslVerify=true" "credential.helper=!"; do
  echo "$LAST" | grep -q "ARGS:.*$want" || fail "robot push is missing -c $want"
done
echo "$LAST" | grep -q '^PARAMS=unset$' || fail "GIT_CONFIG_PARAMETERS reached git on the robot push"
echo "$LAST" | grep -q '^PAGER=cat$' || fail "GIT_PAGER is not cat"
for v in HTTPS_PROXY http_proxy ALL_PROXY SSL_CERT_FILE GIT_SSL_NO_VERIFY CURL_CA_BUNDLE; do
  echo "$LAST" | grep -q "^$v=unset$" || fail "$v reached git on the robot push"
done
nopwn "stub robot push"; ok
# a blank command-scope http.* key breaks real https transport (schannel/OpenSSL) and loses to http.<url>.* repo keys, so the
# wrapper must NOT add one on --robot or --robot-read; repo-local http.* keys are refused instead (below).
rm -f "$SB_TEST_GIT_LOG"
(cd "$REPO" && PATH="$T/stubgit:$PATH" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO" push origin "$SHA:refs/heads/feat") >/dev/null 2>&1
(cd "$REPO" && PATH="$T/stubgit:$PATH" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot-read -- -C "$REPO" ls-remote origin refs/heads/feat) >/dev/null 2>&1
for sub in " push " " ls-remote "; do
  LAST=$(awk -v s="$sub" 'BEGIN{RS="--\n"} index($0, "ARGS:") && index($0, s){r=$0} END{printf "%s", r}' "$SB_TEST_GIT_LOG")
  [ -n "$LAST" ] || fail "the stub git never saw$sub"
  for key in http.sslCAInfo http.proxy http.sslCert http.sslKey; do
    if echo "$LAST" | grep -q -- "-c $key="; then fail "robot$sub carries a blank -c $key= (it breaks real https transport)"; fi
  done
done
ok
# the plain (non-robot) path also scrubs GIT_CONFIG_PARAMETERS and sets GIT_PAGER=cat for every git it runs.
rm -f "$SB_TEST_GIT_LOG"
(cd "$REPO" && PATH="$T/stubgit:$PATH" GIT_PAGER="touch $PWNED" GIT_CONFIG_PARAMETERS="'core.pager'='touch $PWNED'" bash "$W" -- log) >/dev/null 2>&1
[ -s "$SB_TEST_GIT_LOG" ] || fail "the stub git never ran"
[ "$(grep -c '^PARAMS=' "$SB_TEST_GIT_LOG")" = "$(grep -c '^PARAMS=unset$' "$SB_TEST_GIT_LOG")" ] || fail "GIT_CONFIG_PARAMETERS reached git"
[ "$(grep -c '^PAGER=' "$SB_TEST_GIT_LOG")" = "$(grep -c '^PAGER=cat$' "$SB_TEST_GIT_LOG")" ] || fail "GIT_PAGER is not cat"
ok

# url.*.insteadOf / pushInsteadOf in the repo config: the robot push is refused (64); without them it is not.
REPO2="$T/repo2"; mkdir -p "$REPO2"; git -C "$REPO2" init -q 2>/dev/null
git -C "$REPO2" remote add origin "https://example.invalid/x.git" 2>/dev/null
for key in insteadOf pushInsteadOf; do
  git -C "$REPO2" config "url.https://evil.invalid/.$key" "https://example.invalid/"
  RC=0; (cd "$REPO2" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO2" push origin "$SHA:refs/heads/feat") >/dev/null 2>&1 || RC=$?
  [ "$RC" -eq 64 ] || fail "--robot with url.*.$key should exit 64, got $RC"
  git -C "$REPO2" config --unset-all "url.https://evil.invalid/.$key"
  ok
done
git -C "$REPO2" config remote.origin.pushurl "https://evil.invalid/x.git"
RC=0; (cd "$REPO2" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO2" push origin "$SHA:refs/heads/feat") >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "--robot with remote.origin.pushurl should exit 64, got $RC"
git -C "$REPO2" config --unset-all remote.origin.pushurl
ok
git -C "$REPO2" config remote.origin.receivepack "touch $PWNED"
RC=0; (cd "$REPO2" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO2" push origin "$SHA:refs/heads/feat") >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "--robot with remote.origin.receivepack should exit 64, got $RC"
git -C "$REPO2" config --unset-all remote.origin.receivepack
ok
RC=0; (cd "$REPO2" && PATH="$T/stubgit:$PATH" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO2" push origin "$SHA:refs/heads/feat") >/dev/null 2>&1 || RC=$?
[ "$RC" -ne 64 ] || fail "--robot without insteadOf keys should not be refused with 64"; ok
# repo-local http.* transport keys and remote.origin.proxy would redirect or weaken the credentialed transport: refused (64)
# on --robot and --robot-read, before gh is reached. A blank -c cannot override them, so there is no override to rely on.
REPO5="$T/repo5"; mkdir -p "$REPO5"; git -C "$REPO5" init -q 2>/dev/null
git -C "$REPO5" remote add origin "https://example.invalid/x.git" 2>/dev/null
for kv in "http.proxy=http://evil.invalid" "http.sslCAInfo=/evil/ca.pem" "http.sslCert=/evil/c.pem" "http.sslKey=/evil/k.pem"           "http.https://example.invalid/.proxy=http://evil.invalid" "http.sslVerify=false" "remote.origin.proxy=http://evil.invalid"; do
  key="${kv%%=*}"; val="${kv#*=}"
  git -C "$REPO5" config "$key" "$val"
  rm -f "$SB_TEST_GH_LOG"
  RC=0; (cd "$REPO5" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO5" push origin "$SHA:refs/heads/feat") >/dev/null 2>&1 || RC=$?
  [ "$RC" -eq 64 ] || fail "--robot with repo $key should exit 64, got $RC"
  [ ! -e "$SB_TEST_GH_LOG" ] || fail "--robot with repo $key reached gh"
  RC=0; (cd "$REPO5" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot-read -- -C "$REPO5" ls-remote origin refs/heads/main) >/dev/null 2>&1 || RC=$?
  [ "$RC" -eq 64 ] || fail "--robot-read with repo $key should exit 64, got $RC"
  [ ! -e "$SB_TEST_GH_LOG" ] || fail "--robot-read with repo $key reached gh"
  git -C "$REPO5" config --unset-all "$key"
  ok
done
# an upper-case [HTTP] section pulled in through include.path is refused too.
printf '[HTTP]\n\tProxy = http://evil.invalid\n' > "$T/inc.cfg"
git -C "$REPO5" config include.path "$(WP "$T/inc.cfg")"
RC=0; (cd "$REPO5" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot-read -- -C "$REPO5" ls-remote origin refs/heads/main) >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "--robot-read with an included [HTTP] Proxy should exit 64, got $RC"
git -C "$REPO5" config --unset-all include.path
ok
# control on real git (a stub git would list no config keys at all): origin url/fetch keys alone are not refused.
RC=0; (cd "$REPO5" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot -- -C "$REPO5" push origin "$SHA:refs/heads/feat") >/dev/null 2>&1 || RC=$?
[ "$RC" -ne 64 ] || fail "--robot without http.* keys should not be refused with 64"
RC=0; (cd "$REPO5" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot-read -- -C "$REPO5" ls-remote origin refs/heads/main) >/dev/null 2>&1 || RC=$?
[ "$RC" -ne 64 ] || fail "--robot-read without http.* keys should not be refused with 64"; ok

# 13 — a caller's GIT_CONFIG_COUNT/KEY/VALUE cannot inject a filter: a plain-git control fires it, the wrapper does not.
REPO3="$T/repo3"; mkdir -p "$REPO3"; git -C "$REPO3" init -q 2>/dev/null
printf '* filter=y\n' > "$REPO3/.gitattributes"
echo one > "$REPO3/f.txt"
git -C "$REPO3" add .gitattributes f.txt >/dev/null 2>&1
git -C "$REPO3" commit -q -m one >/dev/null 2>&1
echo two >> "$REPO3/f.txt"
rm -f "$PWNED"
(cd "$REPO3" && GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=filter.y.clean GIT_CONFIG_VALUE_0="touch $PWNED" git diff >/dev/null 2>&1)
[ -e "$PWNED" ] || fail "control: an env-injected filter.y.clean did not fire under plain git; the test setup is wrong"
rm -f "$PWNED"
(cd "$REPO3" && GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=filter.y.clean GIT_CONFIG_VALUE_0="touch $PWNED" bash "$W" -- diff >/dev/null 2>&1)
nopwn "env-injected filter.y.clean"; ok

# 14 — --robot-read: exactly three read shapes (ls-remote origin refs/heads/NAME, fetch origin SHA, fetch origin refs/heads/NAME),
# the robot's credential helper, and nothing else. A protected name is fine to READ.
rm -f "$SB_TEST_GH_LOG"
robot_read_refused() {
  local label="$1"; shift
  local rc=0; (cd "$REPO" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot-read -- "$@") >/dev/null 2>&1 || rc=$?
  [ "$rc" -eq 64 ] || fail "--robot-read $label should exit 64, got $rc"
  [ ! -e "$SB_TEST_GH_LOG" ] || fail "--robot-read $label reached gh"
  ok
}
robot_read_refused "push" -C "$REPO" push origin "$SHA:refs/heads/feat"
robot_read_refused "push -f" -C "$REPO" push -f origin "$SHA:refs/heads/feat"
robot_read_refused "status" -C "$REPO" status
robot_read_refused "log" -C "$REPO" log
robot_read_refused "credential fill" credential fill
robot_read_refused "no -C" ls-remote origin refs/heads/main
robot_read_refused "ls-remote without a ref" -C "$REPO" ls-remote origin
robot_read_refused "ls-remote with a sha" -C "$REPO" ls-remote origin "$SHA"
robot_read_refused "ls-remote with an option" -C "$REPO" ls-remote --heads origin main
robot_read_refused "ls-remote with two refs" -C "$REPO" ls-remote origin refs/heads/a refs/heads/b
robot_read_refused "ls-remote bare name" -C "$REPO" ls-remote origin main
robot_read_refused "ls-remote other remote" -C "$REPO" ls-remote elsewhere refs/heads/main
robot_read_refused "ls-remote a url" -C "$REPO" ls-remote https://example.invalid/x.git refs/heads/main
robot_read_refused "fetch without a ref" -C "$REPO" fetch origin
robot_read_refused "fetch all" -C "$REPO" fetch --all
robot_read_refused "fetch with an option" -C "$REPO" fetch --tags origin "$SHA"
robot_read_refused "fetch a plus refspec" -C "$REPO" fetch origin "+refs/heads/main:refs/remotes/origin/main"
robot_read_refused "fetch a refspec with a destination" -C "$REPO" fetch origin "refs/heads/main:refs/heads/main"
robot_read_refused "fetch HEAD" -C "$REPO" fetch origin HEAD
robot_read_refused "fetch a bare name" -C "$REPO" fetch origin main
robot_read_refused "fetch an abbreviated sha" -C "$REPO" fetch origin "${SHA:0:12}"
robot_read_refused "fetch two refs" -C "$REPO" fetch origin refs/heads/a refs/heads/b
robot_read_refused "fetch other remote" -C "$REPO" fetch elsewhere "$SHA"
robot_read_refused "fetch name with .." -C "$REPO" fetch origin refs/heads/a..b
robot_read_refused "fetch name with //" -C "$REPO" fetch origin refs/heads/a//b
robot_read_refused "fetch name with a space" -C "$REPO" fetch origin "refs/heads/a b"
robot_read_refused "fetch name starting with -" -C "$REPO" fetch origin refs/heads/-x
robot_read_refused "fetch with -c" -c core.pager=x -C "$REPO" fetch origin "$SHA"
robot_read_refused "rev-parse" -C "$REPO" rev-parse HEAD
# the three shapes pass validation (a protected name too): they then fail in git on the https-only protocol, never at 64/66.
# (a clean repo with a file:// origin: $REPO carries remote.origin.uploadpack from the sections above, which is refused on reads)
REPO4="$T/repo4"; mkdir -p "$REPO4"; git -C "$REPO4" init -q 2>/dev/null
git -C "$REPO4" remote add origin "file://$T/bare.git" 2>/dev/null
for shape in "ls-remote origin refs/heads/main" "ls-remote origin refs/heads/issue-1-x" "fetch origin $SHA" "fetch origin $SHA64"              "fetch origin refs/heads/main" "fetch origin refs/heads/Hotfix/x"; do
  # shellcheck disable=SC2086
  RC=0; ERR=$( (cd "$REPO4" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot-read -- -C "$REPO4" $shape) 2>&1 >/dev/null) || RC=$?
  [ "$RC" -ne 64 ] && [ "$RC" -ne 66 ] || fail "--robot-read $shape should pass the shape check, got $RC: $ERR"
  ok
done
RC=0; (cd "$REPO4" && bash "$W" --robot-read -- -C "$REPO4" ls-remote origin refs/heads/main) >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 66 ] || fail "--robot-read without a gh config dir should exit 66, got $RC"; ok
# the credential helper is wired for --robot-read exactly as for --robot: the stub git sees it (and the scrubbed env) ...
rm -f "$SB_TEST_GIT_LOG"
(cd "$REPO" && PATH="$T/stubgit:$PATH" HTTPS_PROXY=http://evil.invalid GIT_SSL_NO_VERIFY=1 GIT_CONFIG_PARAMETERS="'core.pager'='touch $PWNED'" SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot-read -- -C "$REPO" ls-remote origin refs/heads/main) >/dev/null 2>&1
LAST=$(awk 'BEGIN{RS="--\n"} /ARGS:.* ls-remote /{r=$0} END{printf "%s", r}' "$SB_TEST_GIT_LOG")
[ -n "$LAST" ] || fail "the stub git never saw the --robot-read ls-remote"
for want in "http.sslVerify=true" "credential.helper=!"; do
  echo "$LAST" | grep -q "ARGS:.*$want" || fail "--robot-read is missing -c $want"
done
echo "$LAST" | grep -q '^PARAMS=unset$' || fail "GIT_CONFIG_PARAMETERS reached git on --robot-read"
for v in HTTPS_PROXY GIT_SSL_NO_VERIFY; do
  echo "$LAST" | grep -q "^$v=unset$" || fail "$v reached git on --robot-read"
done
nopwn "stub robot-read"; ok
# ... and the real helper chain asks the stub gh for `auth git-credential` with the robot's GH_CONFIG_DIR and no token.
rm -f "$SB_TEST_GH_LOG"
printf 'protocol=https\nhost=example.invalid\n\n' | GH_TOKEN=leak-token GH_HOST=leak.example SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" \
  bash "$T/wrapper-noshape.sh" --robot-read -- credential fill >/dev/null 2>&1 || true
[ -f "$SB_TEST_GH_LOG" ] || fail "--robot-read never invoked the stub gh"
grep -q 'auth git-credential get' "$SB_TEST_GH_LOG" || fail "--robot-read: stub gh not called with auth git-credential get"; ok
grep -q "GH_CONFIG_DIR=.*/robot-gh$" "$SB_TEST_GH_LOG" || fail "--robot-read: stub gh did not receive the robot GH_CONFIG_DIR"; ok
grep -q '^GH_TOKEN=$' "$SB_TEST_GH_LOG" || fail "--robot-read: GH_TOKEN leaked to gh"; ok
# a repo-defined url.*.insteadOf / remote.origin.pushurl would redirect the credentialed read: refused (64) like --robot.
git -C "$REPO2" config "url.https://evil.invalid/.insteadOf" "https://example.invalid/"
RC=0; (cd "$REPO2" && SB_ROBOT_GH_CONFIG_DIR="$T/robot-gh" bash "$W" --robot-read -- -C "$REPO2" ls-remote origin refs/heads/main) >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "--robot-read with url.*.insteadOf should exit 64, got $RC"
git -C "$REPO2" config --unset-all "url.https://evil.invalid/.insteadOf"
ok

echo "PASS: test-git-wrapper.sh ($N scenarios)"
