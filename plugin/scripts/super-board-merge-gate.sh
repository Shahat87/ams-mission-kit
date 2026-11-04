#!/usr/bin/env bash
# super-board-merge-gate.sh — take the merge mutex, prove the branch still works
# against the base as it is RIGHT NOW, then merge. Release either way.
#
# WHY THIS EXISTS, MEASURED
#
# GitHub's `mergeable: MERGEABLE` / `mergeStateStatus: CLEAN` answers one
# question: does the text conflict? It says nothing about whether the branch
# still compiles against the base it is about to land on. On 2026-08-20 that gap
# was hit twice in one session on the same repo:
#
#   PR #89 read MERGEABLE/CLEAN and failed `tsc` against current `staging`,
#   because a PR that merged after #89 was tested had added a member to a shared
#   interface. Eleven test files that git merged without a single conflict marker
#   no longer typechecked. Merging on GitHub's word would have turned the base
#   branch red.
#
# Nothing textual conflicted. Nothing was wrong with either branch. The two facts
# were simply established at different times, and only one of them was rechecked.
#
# WHY THE CHECK IS INSIDE THE LOCK, NOT BEFORE IT
#
# A check that runs before the mutex proves the branch was good against a base
# that another lane may replace while this one waits its turn — which is exactly
# the stale-approval shape above, reintroduced one level down. Inside the lock,
# nothing can move the base between the proof and the merge.
#
# The cost is one verification run serialised per merge. The alternative, paid
# once, is a red base branch and every lane after it bouncing on someone else's
# failure.
#
# WHY A DIRECTORY LOCK RATHER THAN THE ORCHESTRATOR'S PROMISE CHAIN
#
# The previous mutex lived in the wave workflow as a JS promise chain, which
# serialised the WHOLE Review lane — reading the diff, rerunning the suite and
# the truth-check all queued behind one card, not just the merge. It also could
# not be seen by the `claude-p` backend or by a second orchestrator on another
# machine. `mkdir` is atomic on every POSIX filesystem, so the lock is visible to
# anything that can see the repo.
#
# CONFIG
#
#   verify_commands: ["cd server && npm run typecheck", "cd server && npm test"]
#
# Missing, empty, or whitespace-only commands halt with exit 9 before locking or
# reading GitHub. Configure the owning project's checks; this gate invents none.
#
# Usage:
#   super-board-merge-gate.sh --config <config.json> --pr <number>
#                             [--expect-head <sha>] [--subject <title>] [--body-file <md>]
#                             [--lock-timeout 1800] [--stale-after 5400] [--dry-run]
#
# `--expect-head` is the PR head commit the Reviewer actually reviewed and
# tested (its `headRefOid` when review passed). The gate pins everything to one
# commit: it verifies that commit, and merges with `--match-head-commit`, so a
# push that lands after review — or during verification — cannot ride in on
# evidence gathered for a different commit. Without the flag the gate pins the
# head it reads on entry and warns.
#
# `--subject` / `--body-file` set the squash commit message (writing-standard.md
# § 1): subject = the PR title (`🐛 [fix] receipts: …`), body = the commit bullets
# plus `Closes #N`. Without them GitHub writes its own message.
#
# `--lock-timeout` is how long THIS caller waits for its turn. `--stale-after` is
# how old a lock must be before it is presumed abandoned. They are deliberately
# two numbers: collapsing them means a caller that waits 20 minutes will reap a
# lock legitimately held for 20 minutes by a slow verification run — which is
# exactly the merge it was waiting politely for.
#
# Exit codes, all meaningful to the caller:
#   0  merged
#   2  verification failed — branch needs a rebase pass, NOT a Blocked card
#   3  PR is closed without a merge; no merge attempted. Also refused with 3,
#      before any verification or merge: the PR's baseRefName is not the
#      configured base_branch (checked on entry and again right before the merge).
#   4  could not take the lock within the timeout
#   11 identity mismatch: config notifications.bot_identity is a login (not
#      "auto"/empty) and `gh api user` is a different login. An ENVIRONMENT fault,
#      not a card fault: halt the run, leave card/approval/claims/worktree
#      untouched, fix the gh login (or the config), restart explicitly.
#   5  the base could not be merged in (real conflict) — needs a rebase pass
#   6  the PR head is not the commit that was reviewed — review evidence is void,
#      the card goes back to Review (not Blocked, not a rebase pass)
#   7  merge policy says a human merges this one (money / auth / destructive
#      schema, a diff over auto_max_lines — default 400, "big PR — please
#      review" — or merge_policy.default "human"). Stdout lists each
#      `human-gate: <category> — <evidence>`. Nothing ran, nothing merged; the
#      card → Blocked with the 🙋 template: the human reviews and merges it, or
#      comments "done" after its pinned request to approve — the next wave re-runs
#      the gate, which then skips the policy check and merges.
#   9  verification-readiness: repair config.verify_commands and restart; preserve
#      card/worktree/approval state. Never bounce to Build or retry a rebase.
#   79 required GitHub evidence unavailable: local run halt; preserve card and
#      approval state, stop dispatch, and explicitly restart after diagnosis.
#   8  🙋 needs you — the PR has migrations for a database the robot may not
#      touch (merge_policy → migrations.allowed_envs), an allowed migrate command
#      failed, or a human-only step is declared (`needs-you:` line in the PR body,
#      or migrations.human_steps). Stdout lists the exact commands as
#      `needs-you: <command>` lines. Card → Blocked with the 🙋 template; once the
#      current request has verified trusted-human approval, the gate re-runs and
#      merges.
#
# MERGE POLICY AND MIGRATIONS (config, all optional — defaults shown in
# references/config-schema.json)
#
#   merge_policy: { default: "auto"|"human", auto_max_lines: 400, size_exclude,
#                   always_human: { <category>: {labels, paths, keywords} } }
#   migrations:   { globs, allowed_envs, target_env, commands: {<env>: cmd},
#                   human_steps }
#
# Detection reads the PR's labels, changed paths (case-insensitive globs; the old
# path of a rename/copy counts too) and the diff's ADDED and REMOVED lines
# (keywords, case-insensitive). The policy is checked before verification — a
# card a human will merge does not need the gate's build proof spent on it —
# and migrations run after verification, inside the lock, right before the
# merge. `--dry-run` reports both and runs neither.
set -euo pipefail
# A caller must not be able to swap what this script runs through its environment: bash sources $BASH_ENV
# (non-interactive) and $ENV (sh mode) before anything else, and CDPATH changes what `cd` resolves. Unset them
# here. PATH is deliberately NOT pinned (a fixed PATH is not portable to Windows/Git Bash); guard-merge.py denies
# a PATH= / BASH_ENV= override in front of a gate call instead.
unset BASH_ENV ENV CDPATH
# Fixed read commands: a malformed payload is GitHub's, so it halts (#32).
export SB_GITHUB_READ_STRICT=1
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
GITHUB_READ="$HERE/super-board-github-read.py"

CONFIG=""; PR=""; LOCK_TIMEOUT=1800; STALE_AFTER=""; DRY=0; EXPECT_HEAD=""; SUBJECT=""; MSG_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config)        CONFIG="$2"; shift 2 ;;
    --pr)            PR="$2"; shift 2 ;;
    --expect-head)   EXPECT_HEAD="$2"; shift 2 ;;
    --subject)       SUBJECT="$2"; shift 2 ;;
    --body-file)     MSG_FILE="$2"; shift 2 ;;
    --lock-timeout)  LOCK_TIMEOUT="$2"; shift 2 ;;
    --stale-after)   STALE_AFTER="$2"; shift 2 ;;
    --dry-run)       DRY=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
[ -n "$CONFIG" ] && [ -e "$CONFIG" ] || { echo "config not found: ${CONFIG:-<unset>}" >&2; exit 66; }
[ -n "$PR" ] || { echo "--pr <number> is required" >&2; exit 64; }
[ -z "$MSG_FILE" ] || [ -r "$MSG_FILE" ] || { echo "--body-file not readable: $MSG_FILE" >&2; exit 66; }
# A lock is presumed abandoned only well past the point a caller stops waiting,
# so a slow-but-alive merge is never stolen from. The floor matters as much as
# the multiple: an impatient caller (say --lock-timeout 6 in a test) would
# otherwise outlive 3x its own wait and reap a lock that was never abandoned.
# Nothing this gate does legitimately exceeds half an hour, so half an hour is
# the earliest anything may be presumed dead.
STALE_FLOOR=1800
if [ -z "$STALE_AFTER" ]; then
  STALE_AFTER=$(( LOCK_TIMEOUT * 3 ))
  [ "$STALE_AFTER" -lt "$STALE_FLOOR" ] && STALE_AFTER="$STALE_FLOOR"
fi

CONFIG_JSON=$(cat "$CONFIG")
printf '%s\n' "$CONFIG_JSON" | python3 "$HERE/super-board-verification-ready.py" --config -
BASE=$(echo "$CONFIG_JSON" | jq -r '.base_branch // "main"')
REPO=$(echo "$CONFIG_JSON" | jq -r '.repo.remote // ""' | sed -E 's#^https?://github\.com/##; s#\.git$##')
REPO_PATH=$(echo "$CONFIG_JSON" | jq -r '.repo.path // "."')
export SB_REPO_PATH="$REPO_PATH"
# Read with a while-loop rather than `mapfile`: macOS ships bash 3.2 and every
# other script in this repo runs there, so this one does too.
VERIFY=()
while IFS= read -r line; do
  if [[ "$line" =~ [^[:space:]] ]]; then VERIFY+=("$line"); fi
done < <(echo "$CONFIG_JSON" | jq -r '.verify_commands[] | select(test("\\S"))')

# Defend the shell boundary too: extraction must leave a command to execute.
if [ "${#VERIFY[@]}" -eq 0 ]; then
  echo "verification-readiness: verify_commands yielded no usable shell commands; repair the project config and restart the board." >&2
  exit 9
fi
python3 "$GITHUB_READ" --check

LOCK_DIR="${REPO_PATH}/.claude/super-board/inflight/merge.lock"
mkdir -p "$(dirname "$LOCK_DIR")"

say() { echo "[merge-gate #${PR}] $*" >&2; }
# Every git call on the PR's own tree (the scratch worktrees) runs with hooks and fsmonitor off: a PR can carry husky /
# core.hooksPath hooks that would otherwise run as this user (Plan 1b review M1). NUL is the null device for a native Windows git.
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) NULDEV=NUL ;; *) NULDEV=/dev/null ;; esac
pr_git() { git -c "core.hooksPath=${NULDEV}" -c core.fsmonitor=false "$@"; }
# The merge plan is read through jq. A jq that cannot start (seen under load on
# Windows: "python3/jq: Invalid argument", exit 126/31) must NEVER read as "no
# human steps, no migrations": process substitution and `$(...)` inside a test
# swallow that failure, which merged past a needs-you step. Every plan field is
# read once, up front, and an unreadable plan is a human merge (exit 7).
plan_get() { printf '%s' "$PLAN" | jq -r "$1"; }
plan_fail() {
  echo "human-gate: policy — the merge plan could not be read"
  say "merge plan unreadable — a human merges this one"; exit 7
}
load_plan() {
  HUMAN=$(plan_get '.human[] | "human-gate: \(.category) — \(.why)"') || plan_fail
  NEEDS_N=$(plan_get '[.needs_you[] | select(length>0)] | length') || plan_fail
  MIG_N=$(plan_get '.migrations | length') || plan_fail
  NEEDS_RAW=$(plan_get '.needs_you[]') || plan_fail
  MIG_LIST=$(plan_get '.migrations | join(" ")') || plan_fail
  RUN_TSV=$(plan_get '.run[] | [.env, .cmd] | @tsv') || plan_fail
  case "${NEEDS_N}${MIG_N}" in ''|*[!0-9]*) plan_fail ;; esac
}

# ---- the mutex -------------------------------------------------------------
# mkdir is atomic: it succeeds for exactly one caller and fails for the rest.
# The pid/date inside is for a human reading a stuck lock, never for logic.
acquire() {
  local waited=0
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    # A lock older than the timeout belongs to a process that died holding it.
    # Reaping it is safe because the only thing it guards is a merge, and a dead
    # merge has already either landed or not.
    if [ -f "$LOCK_DIR/at" ]; then
      local age=$(( $(date +%s) - $(cat "$LOCK_DIR/at" 2>/dev/null || echo 0) ))
      if [ "$age" -gt "$STALE_AFTER" ]; then
        say "reaping a lock abandoned ${age}s ago (stale-after ${STALE_AFTER}s) by pid $(cat "$LOCK_DIR/pid" 2>/dev/null || echo '?')"
        rm -rf "$LOCK_DIR"; continue
      fi
    fi
    [ "$waited" -ge "$LOCK_TIMEOUT" ] && return 1
    sleep 5; waited=$(( waited + 5 ))
  done
  date +%s > "$LOCK_DIR/at"; echo "$$" > "$LOCK_DIR/pid"; echo "$PR" > "$LOCK_DIR/pr"
  return 0
}
release() { rm -rf "$LOCK_DIR"; }

acquire || { say "could not take the merge lock within ${LOCK_TIMEOUT}s"; exit 4; }
trap release EXIT

say "lock taken; verifying against ${BASE} as it is now"

# ---- the freshness check ---------------------------------------------------
# Done in a throwaway worktree so the branch under test is never mutated: the
# gate proves a merge, it does not perform one locally and it never pushes.
SCRATCH=$(mktemp -d)
cleanup() {
  pr_git -C "$REPO_PATH" worktree remove --force "$SCRATCH" 2>/dev/null || true
  [ -z "${BASE_SCRATCH:-}" ] || pr_git -C "$REPO_PATH" worktree remove --force "$BASE_SCRATCH" 2>/dev/null || true
  rm -rf "$SCRATCH" "${BASE_SCRATCH:-}" "${META:-}" "${DIFF:-}"; release
}
trap cleanup EXIT

# ---- the head guard --------------------------------------------------------
# Review evidence belongs to one commit. If the branch moved after the Reviewer
# passed it, the tests it reran and the diff it read describe something else.
pr_head() { python3 "$GITHUB_READ" --kind head -- pr view "$PR" ${REPO:+--repo "$REPO"} --json headRefName,headRefOid \
              -q '.headRefName + " " + .headRefOid'; }
HEAD=$(pr_head) || exit $?
read -r HEAD_REF HEAD_SHA <<<"$HEAD"
[ -n "${HEAD_SHA:-}" ] || { say "could not read the PR head from GitHub"; exit 1; }
pr_state() { python3 "$GITHUB_READ" --kind merge-state -- pr view "$PR" ${REPO:+--repo "$REPO"} --json state,mergeCommit,headRefOid,baseRefName; }
STATE=$(pr_state) || exit $?
if [ "$(echo "$STATE" | jq -r .state)" = MERGED ]; then
  say "already merged; no verification, migration, or merge is repeated"
  exit 0
fi
[ "$(echo "$STATE" | jq -r .state)" = OPEN ] || { say "PR is closed; refusing merge"; exit 3; }
# The verification, policy and migration plan below are all computed for $BASE.
# A PR aimed at another branch (or one GitHub did not name) must never ride on it.
PR_BASE=$(echo "$STATE" | jq -r '.baseRefName // ""')
if [ "$PR_BASE" != "$BASE" ]; then
  say "PR base is '${PR_BASE:-<unreadable>}', configured base_branch is '${BASE}'; refusing merge"
  exit 3
fi
# Acting identity: when a login is configured, only that account may merge.
# Normalised identically in super-board-approval.py: trim; case-insensitive
# "auto" = unset; a "[bot]" login is GitHub App mode (an App token cannot be read
# back with `gh api user`, so only the actor lookup is skipped; it still counts as
# pinned for the fail-closed rule in approval.py); a login that is also a trusted
# approver is a config error for the whole run (exit 11): a separate machine-user
# is required.
BOT_ID=$(echo "$CONFIG_JSON" | jq -r '(.notifications | objects | .bot_identity | strings) // ""' | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//')
BOT_LC=$(printf '%s' "$BOT_ID" | tr '[:upper:]' '[:lower:]')
[ "$BOT_LC" != auto ] || BOT_ID=""
APP_MODE=0; case "$BOT_LC" in *'[bot]') APP_MODE=1 ;; esac
if [ -n "$BOT_ID" ]; then
  TRUSTED_HIT=$(echo "$CONFIG_JSON" | jq -r --arg b "$BOT_LC" '[.merge_policy.trusted_approvers // [] | .[] | tostring | ascii_downcase | select(. == $b)] | length') || exit $?
  if [ "$TRUSTED_HIT" != 0 ]; then
    say "config: bot_identity ${BOT_ID} is also a trusted approver (halt: use a separate machine-user account for bot_identity, or remove it from merge_policy.trusted_approvers)"
    exit 11
  fi
  if [ "$APP_MODE" -eq 0 ]; then
    ACTOR=$(python3 "$GITHUB_READ" --kind scalar -- api user -q .login) || exit $?
    ACTOR=$(printf '%s' "$ACTOR" | tr -d '[:space:]')
    if [ "$(printf '%s' "$ACTOR" | tr '[:upper:]' '[:lower:]')" != "$BOT_LC" ]; then
      say "acting identity ${ACTOR} != bot_identity ${BOT_ID}; refusing merge (halt: fix the gh login or notifications.bot_identity)"
      exit 11
    fi
  fi
fi
if [ -n "$EXPECT_HEAD" ]; then
  case "$HEAD_SHA" in
    "$EXPECT_HEAD"*) : ;;
    *) say "head moved: reviewed ${EXPECT_HEAD}, PR head is now ${HEAD_SHA}"
       say "the review evidence is void — send the card back to Review"
       exit 6 ;;
  esac
else
  say "WARNING: no --expect-head; pinning the head read now (${HEAD_SHA}), not the reviewed one"
fi

# ---- merge policy + migration plan ----------------------------------------
# One read of the PR's labels, paths, size and body plus its diff, classified by
# super-board-merge-policy.py (beside this script). Unreadable → human: a policy
# the gate cannot evaluate must not default to "merge".
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Outside $SCRATCH: `git worktree add` below needs that directory empty.
META=$(mktemp); DIFF=$(mktemp)
python3 "$GITHUB_READ" --kind metadata -- pr view "$PR" ${REPO:+--repo "$REPO"} --json labels,files,additions,deletions,changedFiles,body > "$META"
python3 "$GITHUB_READ" --kind diff --meta "$META" -- pr diff "$PR" ${REPO:+--repo "$REPO"} > "$DIFF"
PLAN=$(python3 "$HERE/super-board-merge-policy.py" --config "$CONFIG" --meta "$META" --diff "$DIFF") || {
  echo "human-gate: policy — could not classify the PR (unreadable metadata)"
  say "merge policy could not be evaluated — a human merges this one"; exit 7; }

# The label is display-only. A request posted before `done` pins the exact head
# and policy/commands. Re-read trusted approval evidence; never mint it here.
APPROVED=false
approval_check() {
  APPROVAL=$(python3 "$HERE/super-board-approval.py" --repo "$REPO" --pr "$PR" \
    --head "$HEAD_SHA" --plan "$PLAN" --config "$CONFIG") || APPROVAL='{"approved":false}'
  python3 "$GITHUB_READ" --check || exit $?
  APPROVED=$(echo "$APPROVAL" | jq -r '.approved // false')
}
approval_request() {
  python3 "$HERE/super-board-approval.py" --request --repo "$REPO" --pr "$PR" \
    --head "$HEAD_SHA" --plan "$PLAN"
}
load_plan
if [ -n "$HUMAN" ] || [ "$NEEDS_N" -gt 0 ]; then
  approval_check
fi
if [ -n "$HUMAN" ] && [ "$APPROVED" != true ]; then
  echo "$HUMAN"
  while IFS= read -r n; do [ -n "$n" ] && echo "needs-you: $n"; done <<<"$NEEDS_RAW"
  approval_request
  say "merge policy: waiting for a trusted human to approve this exact head"; exit 7
fi

git -C "$REPO_PATH" fetch origin "$HEAD_REF" "$BASE" --quiet

# pilot.sandbox.provider docker (spec section 10, A3) marks a guarded repo for this gate (spec 15.9 F1): the PR's content never
# reaches a host working tree. No worktree, no merge, no checkout: conflicts are found on objects (merge-tree) and the verify
# runs on the Lenovo only. Any other provider keeps the scratch worktree and the scrubbed host clone below.
# The provider is read fail-CLOSED and checked against an ALLOWLIST. A jq that cannot run (seen under load on Windows) must never
# read as "no sandbox", because that is what lets a migrate command from the PR's project run on this host. The read gives one of:
# absent (no pilot.sandbox key, or null), none (a sandbox object without a provider and without docker-only keys), invalid (a non-object
# sandbox such as false or 0; no provider but where/runner_repo/image/gate_image/proxy_image; a provider that is not a string), or the
# provider string as written. Only docker, host, absent and none go on; anything else
# (Docker, "docker ", true, podman, ...) is a human merge (exit 7), because it would otherwise fall into the unguarded path.
SANDBOX_PROVIDER=$(jq -r '(.pilot // {}) as $p | if ($p | has("sandbox") | not) or $p.sandbox == null then "absent" else $p.sandbox as $s | if ($s | type) != "object" then "invalid" elif ($s | has("provider")) | not then (if ($s | has("where") or has("runner_repo") or has("image") or has("gate_image") or has("proxy_image")) then "invalid" else "none" end) elif ($s.provider | type) != "string" or ($s.provider | test("\n")) then "invalid" elif $s.provider == "docker" or $s.provider == "host" then $s.provider else "invalid" end end' "$CONFIG") || SANDBOX_PROVIDER=""
SANDBOX_WHERE=$(jq -r '(.pilot.sandbox // null) as $s | if ($s | type) == "object" then ($s.where // "") else "" end' "$CONFIG") || SANDBOX_PROVIDER=""
if [ -z "$SANDBOX_PROVIDER" ]; then
  echo "human-gate: policy — pilot.sandbox.provider could not be read"
  say "could not read pilot.sandbox.provider from ${CONFIG} — a human merges this one"; exit 7
fi
case "$SANDBOX_PROVIDER" in
  docker|host|absent|none) : ;;
  *)
    echo "human-gate: policy — pilot.sandbox.provider is not docker or host"
    say "pilot.sandbox.provider ${SANDBOX_PROVIDER@Q} is not docker or host — a human merges this one"
    exit 7 ;;
esac
# Migrations are eval'd on this host only when the provider is explicitly `host` or there is no sandbox section at all.
case "$SANDBOX_PROVIDER" in host|absent) MIGRATE_ON_HOST=1 ;; *) MIGRATE_ON_HOST=0 ;; esac

LANE_DELEGATE="$HERE/super-board-lane-delegate.py"
# Git Bash on Windows: hand the delegate the bash that is running this gate (an absolute path, outside any clone).
BASH_ARG=()
case "${BASH:-}" in /*|[A-Za-z]:*)
  BASH_ABS=$(cygpath -m "$BASH" 2>/dev/null || printf '%s' "$BASH"); BASH_ARG=(--bash "$BASH_ABS") ;;
esac

if [ "$SANDBOX_PROVIDER" = docker ]; then
# >>> guarded-path (objects only)
BASE_SHA=$(git -C "$REPO_PATH" rev-parse "origin/${BASE}^{commit}") || { say "could not resolve origin/${BASE}"; exit 9; }
MT_RC=0
MT_OUT=$("$HERE/super-board-git.sh" -- -C "$REPO_PATH" merge-tree --write-tree --name-only "$BASE_SHA" "$HEAD_SHA" 2>"$SCRATCH/.mt.err") || MT_RC=$?
# merge-tree exit 1 = conflicts. Any other non-zero (128: unrelated histories, or a head that is missing after the fetch)
# means it could not compare the two: that is exit 9, not a rebase pass.
case "$MT_RC" in
  0) : ;;
  1)
    # Line 1 is the merged tree sha; the conflicted paths follow, up to the first blank line.
    CONFLICTS=$(printf '%s\n' "$MT_OUT" | sed -n '2,$p' | sed '/^$/,$d' | tr '\n' ' ')
    if [ -n "${CONFLICTS// /}" ]; then
      say "the base does not merge in cleanly — conflicts: ${CONFLICTS}"
      say "this is a rebase pass for the Builder lane, not a Blocked card"
      exit 5
    fi
    say "merge-tree reported a conflict but named no path — treat as a verification-readiness problem"
    tail -10 "$SCRATCH/.mt.err" >&2 2>/dev/null || true
    exit 9 ;;
  *)
    say "merge-tree could not compare ${HEAD_SHA} with origin/${BASE} (git exit ${MT_RC}) — treat as a verification-readiness problem"
    tail -10 "$SCRATCH/.mt.err" >&2 2>/dev/null || true
    exit 9 ;;
esac
if [ "$SANDBOX_WHERE" != lenovo ]; then
  say "guarded repo (pilot.sandbox.provider docker): the gate verifies on the Lenovo only (pilot.sandbox.where lenovo)"
  exit 9
fi
[ -f "$LANE_DELEGATE" ] || { say "lane-delegate missing — reinstall super-board (setup fix)"; exit 9; }
say "verify: ${#VERIFY[@]} command(s) in the verify container (lenovo); nothing from the PR runs on this host"
# SCRATCH is a plain temp dir here, for logs only. --dir is the main checkout: lane-delegate reads its origin slug and HEAD
# and never clones or checks it out. The runner fetches the PR head and the base sha from GitHub and merges them itself.
VERIFY_LOG="$SCRATCH/.verify.log"
VRC=0
python3 "$LANE_DELEGATE" exec --config "$CONFIG" --dir "$REPO_PATH" --verify --gate --log "$VERIFY_LOG" \
  --gate-head "$HEAD_SHA" --gate-base "$BASE_SHA" ${BASH_ARG[@]+"${BASH_ARG[@]}"} 2>"$SCRATCH/.verify.err" || VRC=$?
cat "$SCRATCH/.verify.err" >&2 2>/dev/null || true
if [ "$VRC" -ne 0 ]; then
  say "FAILED: $(grep '^FAILED: ' "$VERIFY_LOG" 2>/dev/null | tail -1 | sed 's/^FAILED: //')"
  tail -30 "$VERIFY_LOG" >&2 2>/dev/null || true
  if [ "$VRC" -eq 20 ] && grep -q '^FAILED: ' "$VERIFY_LOG" 2>/dev/null; then
    # The base alone, on the Lenovo: head and base are the same sha, so nothing from the PR is in that tree.
    BRC=0
    python3 "$LANE_DELEGATE" exec --config "$CONFIG" --dir "$REPO_PATH" --verify --gate --log "$SCRATCH/.base.log" \
      --gate-head "$BASE_SHA" --gate-base "$BASE_SHA" ${BASH_ARG[@]+"${BASH_ARG[@]}"} 2>"$SCRATCH/.base.err" || BRC=$?
    case "$BRC" in
      0)
        say "verify passes on ${BASE} in the same environment, so the failure comes from this branch"
        say "the branch is stale against ${BASE} — send it back to Build for a rebase pass"
        exit 2 ;;
      20)
        say "verify fails on ${BASE} too in the scrubbed environment — not this PR's fault."
        say "Fix the project config: pilot.verify_env_keep (non-secret env names), pilot.verify_setup (e.g. git config user.email/user.name, installs), pilot.verify_timeout_min. See references/run.md → Lane protection layer."
        tail -30 "$SCRATCH/.base.log" >&2 2>/dev/null || true
        exit 9 ;;
      *)
        say "the base check on ${BASE} could not run (lane-delegate exit $BRC) — treat as a verification-readiness problem"
        tail -10 "$SCRATCH/.base.err" >&2 2>/dev/null || true
        exit 9 ;;
    esac
  fi
  say "verify could not run (lane-delegate exit $VRC) — treat as a verification-readiness problem"
  exit 9
fi
# <<< guarded-path
else
pr_git -C "$REPO_PATH" worktree add --detach "$SCRATCH" "$HEAD_SHA" --quiet 2>/dev/null || {
  say "could not create a scratch worktree for ${HEAD_REF}@${HEAD_SHA}"; exit 5; }

if ! pr_git -C "$SCRATCH" merge "origin/${BASE}" --no-edit --quiet 2>/dev/null; then
  CONFLICTS=$(pr_git -C "$SCRATCH" diff --name-only --diff-filter=U | tr '\n' ' ')
  say "the base does not merge in cleanly — conflicts: ${CONFLICTS}"
  say "this is a rebase pass for the Builder lane, not a Blocked card"
  exit 5
fi

VERIFY_LOG="$SCRATCH/.verify.log"
[ -f "$LANE_DELEGATE" ] || { say "lane-delegate missing — reinstall super-board (setup fix)"; exit 9; }
say "verify: ${#VERIFY[@]} command(s) in a scrubbed clean clone (no GitHub credentials)"
VRC=0
python3 "$LANE_DELEGATE" exec --config "$CONFIG" --dir "$SCRATCH" --verify --gate --log "$VERIFY_LOG"   ${BASH_ARG[@]+"${BASH_ARG[@]}"} 2>"$SCRATCH/.verify.err" || VRC=$?
cat "$SCRATCH/.verify.err" >&2 2>/dev/null || true
# lane-delegate exec exits: 0 ok · 20 a verify command failed · 64 usage · 70 internal error (no commands, timeout,
# git filters, --gate refusing a "real" profile). 20 alone does not blame the branch: the same commands must
# also pass on ${BASE} in the same scrubbed environment, else the environment (not the PR) is what fails.
if [ "$VRC" -ne 0 ]; then
  say "FAILED: $(grep '^FAILED: ' "$VERIFY_LOG" 2>/dev/null | tail -1 | sed 's/^FAILED: //')"
  tail -30 "$VERIFY_LOG" >&2 2>/dev/null || true
  if [ "$VRC" -eq 20 ] && grep -q '^FAILED: ' "$VERIFY_LOG" 2>/dev/null; then
    BASE_SCRATCH=$(mktemp -d)
    pr_git -C "$REPO_PATH" worktree add --detach "$BASE_SCRATCH" "origin/${BASE}" --quiet 2>/dev/null || {
      say "could not create a scratch worktree for origin/${BASE} to check the failure against"; exit 9; }
    BRC=0
    python3 "$LANE_DELEGATE" exec --config "$CONFIG" --dir "$BASE_SCRATCH" --verify --gate       --log "$BASE_SCRATCH/.verify.log" ${BASH_ARG[@]+"${BASH_ARG[@]}"} 2>"$BASE_SCRATCH/.verify.err" || BRC=$?
    case "$BRC" in
      0)
        say "verify passes on ${BASE} in the same environment, so the failure comes from this branch"
        say "the branch is stale against ${BASE} — send it back to Build for a rebase pass"
        exit 2 ;;
      20)
        say "verify fails on ${BASE} too in the scrubbed environment — not this PR's fault."
        say "Fix the project config: pilot.verify_env_keep (non-secret env names), pilot.verify_setup (e.g. git config user.email/user.name, installs), pilot.verify_timeout_min. See references/run.md → Lane protection layer."
        tail -30 "$BASE_SCRATCH/.verify.log" >&2 2>/dev/null || true
        exit 9 ;;
      *)
        say "the base check on ${BASE} could not run (lane-delegate exit $BRC) — treat as a verification-readiness problem"
        tail -10 "$BASE_SCRATCH/.verify.err" >&2 2>/dev/null || true
        exit 9 ;;
    esac
  fi
  say "verify could not run (lane-delegate exit $VRC) — treat as a verification-readiness problem"
  exit 9
fi
fi
say "verified green against ${BASE} (${#VERIFY[@]} command(s))"

python3 "$GITHUB_READ" --check

# ---- migrations (inside the lock, after the build proof) -------------------
# Run the configured migrate command for every allowed env, from the verified
# scratch tree. Collect, never stop early, so a human gets every command at once.
NEEDS=()
while IFS= read -r line; do [ -n "$line" ] && NEEDS+=("$line"); done \
  <<<"$NEEDS_RAW"
[ "${#NEEDS[@]}" -eq "$NEEDS_N" ] || plan_fail   # every declared human step must have been read
if [ "$MIG_N" -gt 0 ]; then
  say "migrations in this PR: ${MIG_LIST}"
  while IFS=$'\t' read -r env cmd; do
    [ -n "$env" ] || continue
    if [ "$MIGRATE_ON_HOST" -ne 1 ]; then
      # H2: the migrate command is written by the PR's project and would run on this host with the database credentials.
      # Unless the provider is explicitly `host` (or there is no sandbox section) nothing from the PR runs here, so it is a
      # human step; it is never eval'd.
      say "auto-migrations are disabled in docker mode; run them yourself (${env}: ${cmd})"
      NEEDS+=("${cmd}   # ${env}: auto-migrations are disabled in docker mode; run them yourself")
      continue
    fi
    if [ "$DRY" -eq 1 ]; then say "dry run: would migrate ${env}: ${cmd}"; continue; fi
    say "migrate ${env}: ${cmd}"
    python3 "$GITHUB_READ" --check
    if ! ( cd "$SCRATCH" && eval "$cmd" ) >"$SCRATCH/.migrate.log" 2>&1; then
      say "FAILED: migrate ${env}"; tail -20 "$SCRATCH/.migrate.log" >&2
      NEEDS+=("${cmd}   # ${env}: failed in the merge gate — fix, run it, then mark done")
    fi
  done <<<"$RUN_TSV"
fi
if [ "${#NEEDS[@]}" -gt 0 ]; then
  if [ "$APPROVED" = true ] && [ "$NEEDS_N" -eq "${#NEEDS[@]}" ]; then
    say "a trusted human confirmed the current head and human steps"
  else
    for n in "${NEEDS[@]}"; do echo "needs-you: $n"; done
    approval_request
    say "🙋 needs you before this merges — card → Blocked with the commands above"
    exit 8
  fi
fi

# An approval can be superseded while verification/migrations run. Re-read just
# before merging; GitHub independently pins the code with --match-head-commit.
if [ "$APPROVED" = true ]; then
  # Code remains pinned; body-declared human steps and config can change without
  # a commit. Reclassify them too instead of reusing the old approval scope.
  python3 "$GITHUB_READ" --kind metadata -- pr view "$PR" ${REPO:+--repo "$REPO"} --json labels,files,additions,deletions,changedFiles,body > "$META"
  PLAN=$(python3 "$HERE/super-board-merge-policy.py" --config "$CONFIG" --meta "$META" --diff "$DIFF") || {
    say "human approval scope could not be refreshed"; exit 7; }
  approval_check
  if [ "$APPROVED" != true ]; then
    approval_request
    say "human approval changed during verification; keep the card Blocked"
    if [ -n "$HUMAN" ]; then exit 7; else exit 8; fi
  fi
fi

# ---- the merge -------------------------------------------------------------
if [ "$DRY" -eq 1 ]; then
  say "dry run: would squash-merge PR #${PR}"
  exit 0
fi

# --match-head-commit makes GitHub refuse if anything was pushed after HEAD_SHA,
# including during the verification run above.
# The base can be retargeted while verification ran; re-read it at the last moment.
STATE=$(pr_state) || exit $?
if [ "$(echo "$STATE" | jq -r '.state')" != OPEN ]; then
  say "PR is no longer open (state $(echo "$STATE" | jq -r '.state')) after verification; refusing merge"
  exit 3
fi
if [ "$(echo "$STATE" | jq -r '.baseRefName // ""')" != "$BASE" ]; then
  say "PR base changed to '$(echo "$STATE" | jq -r '.baseRefName // "<unreadable>"')' during the gate; configured base_branch is '${BASE}'; refusing merge"
  exit 3
fi
python3 "$GITHUB_READ" --check
if gh pr merge "$PR" ${REPO:+--repo "$REPO"} --squash --delete-branch \
     ${SUBJECT:+--subject "$SUBJECT"} ${MSG_FILE:+--body-file "$MSG_FILE"} \
     --match-head-commit "$HEAD_SHA" 2>&1 | tail -3 >&2; then
  say "merged ${HEAD_SHA}"
  # Post-merge tidy-up: drop worktrees and local branches the merge just made
  # redundant. Optional (only when the cleanup-wt hook is installed), local-only, and it
  # can never fail the merge that already happened.
  if [ -f "$REPO_PATH/.claude/hooks/cleanup-wt.py" ]; then
    ( cd "$REPO_PATH" && python3 .claude/hooks/cleanup-wt.py --post-merge --base "$BASE" </dev/null ) >&2 || true
  fi
  exit 0
fi
STATE=$(pr_state) || exit $?
if [ "$(echo "$STATE" | jq -r .state)" = MERGED ]; then
  say "merge response was lost; GitHub confirms the merge commit"
  exit 0
fi
NOW_SHA=$(echo "$STATE" | jq -r .headRefOid)
if [ "$NOW_SHA" != "$HEAD_SHA" ]; then
  say "head moved during the gate: verified ${HEAD_SHA}, PR head is now ${NOW_SHA}"
  say "the review evidence is void — send the card back to Review"
  exit 6
fi
python3 "$GITHUB_READ" --halt "Merge response failed and GitHub has not confirmed a merge; reconcile PR #${PR} before restarting. No merge was retried."
