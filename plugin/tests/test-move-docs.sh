#!/usr/bin/env bash
# Doc regression guard for the card-move contract (ams.19): the docs must not tell a lane to
# omit --model, call the Agent matrix "not one of these", or have the planner skip unlabelled cards.
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }
for phrase in 'omit --model' 'not one of these'; do
  hits=$(grep -rnF -- "$phrase" skills workflows || true)
  [ -z "$hits" ] || fail "stale wording '$phrase' in:
$hits"
done
RW=skills/super-board/references/run-workflow.md
[ -f "$RW" ] || fail "$RW missing"
if grep -niE 'planner (skips|drops|ignores)[^.]*(unlabell?ed|no (type )?label|without a (type )?label)|(skips|skip) (an? |any |such )?(unlabell?ed|card with no (type )?label)' "$RW" | grep -viE 'does not|never|doesn.t'; then
  fail "$RW says the planner skips unlabelled cards"
fi
grep -qF 'The planner does not skip such a card' "$RW" || fail "$RW lost 'The planner does not skip such a card'"
RUN=skills/super-board/references/run.md
[ -f "$RUN" ] || fail "$RUN missing"
# W28: the config key is notifications.bot_identity; a bare `bot_identity` made the orchestrator look for a top-level key.
grep -qF 'notifications.bot_identity' "$RW" || fail "$RW must name notifications.bot_identity"
grep -qF "super-board-preflight.sh --repo <owner/name> --issues" "$RUN" && grep -qF "shell-quote the --config path" "$RUN" || fail "$RUN must show the pre-flight --config <cfg> with a shell-quote note"
! grep -E '<bot_identity>|bot_identity is unset' "$RW" "$RUN" || fail "run-workflow.md/run.md name a bare bot_identity value"
# ams.20: a Done move in super-review/SKILL.md must carry --pr (card.sh enforces it; the doc must match).
SR=skills/super-review/SKILL.md
BAD=$(grep -nE 'super-board-card\.sh[^`]* move [^ ]+ Done' "$SR" | grep -vE -- '--pr|--no-pr' || true)
[ -z "$BAD" ] || fail "$SR has a Done move command without --pr:
$BAD"
grep -qE 'move [^ ]+ Done[^`]*--pr' "$SR" || fail "$SR lost its Done move command with --pr"
# Any card.sh Done move command in skills/workflows must carry --pr or --no-pr (else it exits 64 and strands the card).
BAD=$(grep -rnE 'super-board-card\.sh.* move +[^ ]+ +Done' skills workflows | grep -vE -- '--pr|--no-pr' || true)
[ -z "$BAD" ] || fail "a Done move command carries neither --pr nor --no-pr:
$BAD"
# Plan 1c T7: on a tainted card no lane diffs HEAD or puts the card branch on disk (spec 15 O-2, F3, F4).
# A line that does so must say "untainted" or link "Tainted cards" on itself or on the next line.
RUN=skills/super-board/references/run.md
LANE_DOCS=$( { find skills/super-qa skills/super-review skills/super-build skills/super-board/references -name '*.md'; ls workflows/*.js skills/git-sync/SKILL.md skills/super-board/SKILL.md scripts/super-board-codex-lane.sh scripts/super-board-codex-wave.sh; } | sort -u)
# The Tainted cards section itself states the prohibitions this guard looks for, so its own lines are skipped.
TC_START=$(grep -nE '^#+ Tainted cards' "$RUN" | cut -d: -f1)
TC_END=$(awk -v s="$TC_START" 'NR>s && /^#+ /{print NR; exit}' "$RUN")
# Named exclusions: lines that match a regex but are not about a card branch.
#   git checkout --ours/--theirs : picks a side of one conflicted file (git-sync), not a branch checkout.
#   git push origin main:staging : onboarding creates the staging branch from main.
#   Bash(git push:*)             : a permission-allowlist pattern in run-workflow.md, not an instruction.
#   never check out, blame or run the branch : the fixed tainted-card line of the adversarial brief; it is the prohibition itself.
# skills/git-sync/README.md is user-facing description, not lane instructions, so only git-sync/SKILL.md is in LANE_DOCS.
GAP_IGNORE='git checkout --(ours|theirs)|git push origin main:staging|Bash\(git push:\*\)|never check out, blame or run the branch'
untainted_gap() { # file regex: print file:line for each match with no qualifier on the line, or on the next line when that line continues it
  local file=$1 rx=$2 n nxt
  { grep -nE -- "$rx" "$file" | grep -vE -- "$GAP_IGNORE" || true; } | cut -d: -f1 | while read -r n; do
    if [ "$file" = "$RUN" ] && [ "$n" -ge "$TC_START" ] && [ "$n" -lt "$TC_END" ]; then continue; fi
    sed -n "${n}p" "$file" | grep -qiE 'untainted|Tainted cards' && continue
    # The next line counts only as a continuation: not blank, a heading, a fence, a list item or a numbered step.
    nxt=$(sed -n "$((n + 1))p" "$file")
    if ! printf '%s
' "$nxt" | grep -qE '^[[:space:]]*$|^#|^[[:space:]]*```|^[[:space:]]*([-*+]|[0-9]+[.)])[[:space:]]'; then
      printf '%s
' "$nxt" | grep -qiE 'untainted|Tainted cards' && continue
    fi
    echo "$file:$n: $(sed -n "${n}p" "$file" | cut -c1-110)"
  done
}
HEAD_RX='merge-base HEAD|\.\.\.HEAD|diff[^`]*HEAD|merge-base diff|rev-parse HEAD'
DISK_RX='git (pull|rebase|checkout|switch)( |$)|worktree add|(Pull|[Cc]heckout)[^.]*issue-<N>|[Ww]orktree[^.]*issue-<N>-review[^.]* from |[Cc]heck(s|ed)? (it |the same branch )out|[Cc]heck(s)? out|checked out at|Worktree as above|[Cc]ommit \+ push|git push'
BAD=""
for f in $LANE_DOCS; do BAD="$BAD$(untainted_gap "$f" "$HEAD_RX")"$'
'; done
BAD=$(printf '%s' "$BAD" | sed '/^$/d')
[ -z "$BAD" ] || fail "a lane doc diffs HEAD with no tainted-card branch:
$BAD"
BAD=""
for f in $LANE_DOCS; do BAD="$BAD$(untainted_gap "$f" "$DISK_RX")"$'
'; done
BAD=$(printf '%s' "$BAD" | sed '/^$/d')
[ -z "$BAD" ] || fail "a lane doc tells the lane to pull, rebase, checkout or add a worktree of a card branch with no tainted-card branch:
$BAD"
grep -qE '^#+ Tainted cards' "$RUN" || fail "$RUN lost its 'Tainted cards' section"
TC=$(awk '/^#+ Tainted cards/{on=1; next} on && /^#+ /{exit} on' "$RUN")
for need in 'headRefOid' 'exec --sandbox --ref' 'commit-onto' '--expect-head' 'super-board-git.sh' 'exit `5`' 'exit `2`' 'objects.py tainted'; do
  printf '%s
' "$TC" | grep -qF -- "$need" || fail "$RUN 'Tainted cards' section lacks '$need'"
done
printf '%s
' "$TC" | grep -qE 'python3 [^ ]*super-board-lane-delegate\.py commit-onto' || fail "$RUN 'Tainted cards' section lacks a python3 commit-onto command"
grep -qF 'never this flow' <(awk '/^### The rebase pass/{on=1; next} on && /^##/{exit} on' "$RUN") || fail "$RUN rebase pass does not send tainted cards to 'Tainted cards'"
grep -qF 'Tainted cards' skills/git-sync/SKILL.md || fail "git-sync/SKILL.md does not forbid a tainted card branch"
# W29: a lane worktree is cut from a freshly fetched origin/<base>, never the local base; a 🛑 block still ends blocked-by: -.
for f in skills/super-qa/SKILL.md skills/super-board/references/run.md; do
  grep -qF 'fetch --quiet origin <base>' "$f" && grep -qF 'origin/<base>' "$f" || fail "$f must tell the qa lane to fetch origin <base> and branch from origin/<base>"
  grep -qF 'Never branch from or read the local `<base>`' "$f" || fail "$f lost 'Never branch from or read the local <base>'"
done
grep -qF '.claude/worktrees/issue-<N>-qa origin/<base>' skills/super-board/references/run.md || fail "run.md qa-card worktree command must create from origin/<base>"
grep -qF 'fetch --quiet origin <base_branch>' skills/super-board/references/run.md || fail "run.md Builder worktree must fetch origin <base_branch> first"
BT=skills/super-board/references/block-template.md
grep -qF 'a `🛑` block whose reason is a harness or tool refusal' "$BT" || fail "$BT must list a 🛑 harness or tool refusal block under blocked-by: -"
grep -qF 'a block that waits on other cards keeps its issue numbers' "$BT" || fail "$BT lost the waits-on-other-cards rule"
grep -qF 'issue numbers or `-`, never prose' "$BT" || fail "$BT lost the blocked-by line rule"
# W29: the old local-base wordings must not return in the lane prompts or specs.
for f in workflows/super-board-wave.js skills/super-build/SKILL.md skills/super-qa/SKILL.md skills/super-board/references/run.md; do
  for old in 'create the issue branch from the base branch' 'off `config.base_branch`' 'Pull latest of base'; do
    ! grep -qF -- "$old" "$f" || fail "$f still says '$old' (branch from a fetched origin/<base>)"
  done
done
! grep -qF 'create the issue branch from the base' scripts/super-board-codex-lane.sh || fail "super-board-codex-lane.sh qa prompt still cuts from the local base"
grep -qF 'fetch --quiet origin <base>' scripts/super-board-codex-lane.sh || fail "super-board-codex-lane.sh qa prompt must fetch origin <base>"
for f in skills/super-board/references/run.md skills/super-qa/SKILL.md; do
  grep -qF 'If the fetch fails, create nothing, Block with the error, and stop. Never fall back to the local `<base>`.' "$f" || fail "$f lost the fail-closed fetch clause"
done
# run.md:505 'reachable from the base branch' is the merge check, not a branch cut, so only the cut wordings are pinned.
! grep -qF 'from the base branch' workflows/super-board-wave.js skills/super-build/SKILL.md skills/super-qa/SKILL.md || fail "a lane prompt still cuts a branch 'from the base branch'"
grep -qF 'fetch --quiet origin <base>' workflows/super-board-wave.js || fail "super-board-wave.js qa prompt must fetch origin <base>"
grep -qF 'fetch --quiet origin <base_branch>' skills/super-build/SKILL.md || fail "super-build/SKILL.md must fetch origin <base_branch>"
grep -qF 'issue-<N>-build origin/<base_branch>' skills/super-board/references/run.md || fail "run.md Builder must create the worktree from origin/<base_branch>"
echo "PASS: test-move-docs.sh"
