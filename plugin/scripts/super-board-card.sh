#!/usr/bin/env bash
# super-board-card.sh — read and move project cards for a few GraphQL points.
#
# Why this exists: on 2026-10-04 a 3-card wave spent ~2,850 of the 5,000/hr
# GraphQL points (~950 a card) and the board stalled an hour after every wave.
# Almost all of it was board lookups, not work. Measured with
# `rateLimit(dryRun:true){cost}` against gh 2.101:
#   gh project item-list          101 points per 100-item page (fieldValues(100)
#                                 x labels/users/reviewers/pullRequests(10) each),
#                                 so a 131-card board costs ~203 per call
#   gh project field-list         101 — the same query with items(first:100),
#                                 fetched only to be thrown away
#   gh project view               2   (owner lookup + project)
#   gh issue/pr view, pr list     1 each
# Lanes moved a card as item-list + field-list + item-edit: ~305 points a move.
# This script moves one for 1 point (cached ids) or 2 (first sight of an issue),
# and lists the whole board for ~1 point per 100 cards.
#
# Usage:
#   super-board-card.sh [--config <cfg.json>] [--owner O --number N] [--repo owner/name] <verb>
#     move <issue> <Status> [--lane build|qa|review|lint] [--model <Model> | --tool grok|pi|codex] [--host claude|codex]
#                           [--kind bug|feature|qa] [--pr 50 | --no-pr "reason"]
#                             set the card's Status column (Ready, QA, Review, Blocked, Done, …).
#                             A move into Building, QA, Review, Blocked or Done REQUIRES --lane and
#                             --model (exit 64, nothing mutated, otherwise): Agent and Skills are
#                             mandatory. A move into Building, QA, Review or Blocked also needs a
#                             type label (bug, feature, qa, docs, ux, tests, refactor, tech-debt or
#                             money, any case) on the card; with none, --kind (bug|feature|qa) adds
#                             that label first (`gh issue edit --add-label`), and without --kind
#                             it exits 64 with nothing mutated. Backlog and Ready need neither.
#                             --lane/--model fill the board's Agent and Skills fields,
#                             best effort (a failure there warns on stderr and never changes the
#                             exit code): Agent = "<Model> · super-<lane>" ("Done · <Model> ·
#                             super-<lane>" when the Status is Done), Skills = the lane's resolved skill
#                             ids. <Model> is the worker's own family name: --model is normalised
#                             case-insensitively when it contains haiku, sonnet, opus or codex
#                             ("Claude Opus 5.5" -> Opus); any other value is kept as given.
#                             --lane lint is for the lint verb, only into Blocked or Done (any other
#                             Status exits 64): Agent = "<Model> · super-lint", Skills = the fixed text
#                             "super-board" (no resolver); Blocked still needs a type label or --kind.
#                             --tool grok|pi|codex (any case) sets the model to Grok, Pi or Codex exactly (not with
#                             --model; anything else exits 64). --model also maps a value containing grok to Grok
#                             and exactly "pi" to Pi. --model/--tool need --lane; --host (default claude) picks the skills inventory. Field options are
#                             never created or edited; a missing Agent option posts ONE issue comment
#                             (marker <!-- sb:agent-option-missing:Opus · super-qa --> for that value, never twice; the
#                             issue's comments are read only in that case).
#                             Field reads never halt the run.
#                             A move into Building or Review of an issue with no milestone prints
#                             "warning: #12 has no milestone; set one on the issue" on stderr (exit unchanged); the label
#                             needs-grilling and the config's roadmap.issue are exempt.
#                             A move into Done ALSO needs exactly one of (exit 64, nothing changed, otherwise):
#                               --pr 50           the PR must be MERGED, have a merge commit, target the
#                                                 config's base_branch and reference the issue (a closing
#                                                 reference, an issue-12 branch, or #12 as a whole token in its
#                                                 body or title), else exit 64 "PR #50 is not merged into
#                                                 staging; not moving #12" or "PR #50 does not reference #12;
#                                                 not moving it". "#50" and a PR URL are accepted too.
#                             A Done move needs --config (exit 66 without, before any read); base_branch defaults to main.
#                               --no-pr "duplicate of #9"   a Done move with no PR (lint, duplicate); the reason
#                                                 is required.
#                             Both flags are Done-only. After the Status is set, card.sh posts ONE marker comment
#                             (<!-- sb:done-closed:SHA --> or :none, never twice) and closes the issue with
#                             `gh issue close --reason completed` when it is open. A failure after the Status
#                             changed prints "moved #12 → Done, but closing it failed: …" and exits 69. The card
#                             stays in the Done column. A Done move that exited 69 shows up in `audit` as
#                             "Done missing: closed"; re-running the same Done move closes it (idempotent: the
#                             exact marker is never posted twice). Issues in the config's protected_issues (integers,
#                             default []) are never moved: exit 64 before any read.
#     release 60 [--dry-run]  run after a person merges the release PR (base_branch into the default branch).
#                             Labels `released` (created when missing) every closed issue whose
#                             sb:done-closed marker sha is among that PR's commits; protected issues and `none`
#                             markers are skipped. Needs base_branch in the config (exit 66 without it); a PR that
#                             is not merged base_branch -> default branch exits 64; 250 or more commits exit 69
#                             (GitHub caps that list). --dry-run prints "would release #12" and changes nothing.
#     audit [--json]        read-only, one paginated read. Lists every card in Building, QA, Review
#                             or Blocked that misses Agent, Skills or a type label, and every card in Done whose
#                             issue is still open (`#12 Done missing: closed`) (open Issues only):
#                             `#12 Review missing: agent,skills,label`, then `audit: 3 card(s)
#                             missing fields` or `audit: clean`. --json prints
#                             [{"number":N,"status":"Review","missing":["agent","skills","label"]}].
#                             Exit 0 whatever it finds; 69 read failed; 79 halted.
#     status <issue>          print the card's current Status name (1 point, never cached)
#     item-id <issue>         print the card's project item id (cached)
#     ids                     print {projectId, fieldId, options:{name:id}} (cached)
#     items                   the whole board in `gh project item-list --format json` shape
#                             ({items:[{id,status,labels,title,content:{type,number,title,url,repository}}],totalCount})
#
# Owner/number/repo come from --config (project.owner, project.number,
# repo.remote), else $BUILD_LOOP_OWNER / $BUILD_LOOP_PROJECT and the origin remote.
#
# Cache: <main checkout>/.claude/super-board/cache/board-<owner>-<number>.json,
# shared by every worktree. Option ids are reminted whenever someone edits the
# column list, so a failed move drops the cache, re-reads it once, and retries
# once — a stale id costs one extra point, never a wrong move.
#
# Exit: 0 ok · 64 usage · 66 config · 69 GitHub read/mutation failed ·
#       79 reads halted (super-board-github-read.py) · 3 issue not on this board /
#       Status option missing.
set -euo pipefail
# Fixed read commands: a malformed payload is GitHub's, so it halts (#32).
export SB_GITHUB_READ_STRICT=1

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONFIG="${SB_CONFIG:-}"; OWNER="${BUILD_LOOP_OWNER:-}"; NUMBER="${BUILD_LOOP_PROJECT:-}"; REPO=""
BASE=""; PROTECTED='[]'; ROADMAP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --owner)  OWNER="$2"; shift 2 ;;
    --number) NUMBER="$2"; shift 2 ;;
    --repo)   REPO="$2"; shift 2 ;;
    -h|--help) sed -n '2,95p' "$0"; exit 0 ;;
    --) shift; break ;;
    -*) echo "unknown flag: $1" >&2; exit 64 ;;
    *) break ;;
  esac
done
VERB="${1:-}"; [ -n "$VERB" ] || { echo "usage: super-board-card.sh <move|status|item-id|ids|items|audit|release> …" >&2; exit 64; }
shift

if [ -n "$CONFIG" ]; then
  [ -r "$CONFIG" ] || { echo "config not found: $CONFIG" >&2; exit 66; }
  CJ=$(cat "$CONFIG")
  [ -n "$OWNER" ]  || OWNER=$(echo "$CJ" | jq -r '.project.owner // ""')
  [ -n "$NUMBER" ] || NUMBER=$(echo "$CJ" | jq -r '.project.number // ""')
  [ -n "$REPO" ]   || REPO=$(echo "$CJ" | jq -r '.repo.remote // ""')
  BASE=$(echo "$CJ" | jq -r '.base_branch // ""')
  PROTECTED=$(echo "$CJ" | jq -c '.protected_issues // []')
  ROADMAP=$(echo "$CJ" | jq -r '(.roadmap.issue // 0) | if type == "number" then floor else 0 end' 2>/dev/null || echo 0)
  echo "$PROTECTED" | jq -e 'type == "array" and all(.[]; type == "number")' >/dev/null 2>&1 \
    || { echo "config protected_issues must be an array of integers" >&2; exit 66; }
fi
[ -n "$REPO" ] || REPO=$(git remote get-url origin 2>/dev/null || true)
REPO=$(echo "$REPO" | sed -E 's#^(https?://github\.com/|git@github\.com:)##; s#\.git$##')
[ -n "$OWNER" ] && [ -n "$NUMBER" ] || { echo "project owner/number unknown: pass --config or --owner/--number" >&2; exit 66; }

# Reads go through the kit's bounded retry helper when it is installed, so a
# board read shares the run's halt file; plain gh otherwise.
gh_read() {
  if [ -f "$HERE/super-board-github-read.py" ]; then
    python3 "$HERE/super-board-github-read.py" --kind json -- "$@"
  else
    gh "$@"
  fi
}
no_errors() { jq -e 'if type == "array" then all(.[]; (.errors // null) == null) else (.errors // null) == null end' >/dev/null; }

# ── cache ───────────────────────────────────────────────────────────────
cache_dir() {
  if [ -n "${SB_CARD_CACHE_DIR:-}" ]; then echo "$SB_CARD_CACHE_DIR"; return; fi
  local common
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -n "$common" ]; then echo "$(dirname "$common")/.claude/super-board/cache"
  else echo "$PWD/.claude/super-board/cache"; fi
}
CACHE_DIR=$(cache_dir)
CACHE="$CACHE_DIR/board-${OWNER}-${NUMBER}.json"
cache_get() { [ -s "$CACHE" ] && jq -r "($1) // empty" "$CACHE" 2>/dev/null || true; }
cache_put() {  # $1 = jq filter applied to the current cache, with --arg/--argjson in "$@"
  local filter="$1"; shift
  mkdir -p "$CACHE_DIR"
  [ -f "$CACHE_DIR/.gitignore" ] || echo '*' > "$CACHE_DIR/.gitignore"
  local cur tmp
  cur=$(cat "$CACHE" 2>/dev/null || echo '{}'); echo "$cur" | jq -e . >/dev/null 2>&1 || cur='{}'
  tmp=$(mktemp "$CACHE_DIR/.board.XXXXXX")
  echo "$cur" | jq "$@" "$filter" > "$tmp" && mv "$tmp" "$CACHE" || rm -f "$tmp"
}
cache_drop() { rm -f "$CACHE"; }

# ── reads ───────────────────────────────────────────────────────────────
load_ids() {  # fills PROJECT_ID FIELD_ID OPTIONS_JSON — 0 points when cached, else 1
  PROJECT_ID=$(cache_get .projectId); FIELD_ID=$(cache_get .fieldId)
  OPTIONS_JSON=$(cache_get '.options // empty | tojson')
  [ -n "$PROJECT_ID" ] && [ -n "$FIELD_ID" ] && [ -n "$OPTIONS_JSON" ] && return 0
  local out
  out=$(gh_read api graphql -f query='query($owner:String!,$number:Int!){
      repositoryOwner(login:$owner){ ... on ProjectV2Owner { projectV2(number:$number){ id
        field(name:"Status"){ ... on ProjectV2SingleSelectField { id options { id name } } } } } } }' \
      -F owner="$OWNER" -F number="$NUMBER") || return $?
  echo "$out" | no_errors || { echo "Status field read returned errors" >&2; return 69; }
  PROJECT_ID=$(echo "$out" | jq -r '.data.repositoryOwner.projectV2.id // empty')
  FIELD_ID=$(echo "$out" | jq -r '.data.repositoryOwner.projectV2.field.id // empty')
  OPTIONS_JSON=$(echo "$out" | jq -c '[.data.repositoryOwner.projectV2.field.options[]? | {(.name): .id}] | add // {}')
  [ -n "$PROJECT_ID" ] && [ -n "$FIELD_ID" ] || { echo "project $OWNER/$NUMBER or its Status field not found" >&2; return 69; }
  cache_put '.projectId = $p | .fieldId = $f | .options = $o' --arg p "$PROJECT_ID" --arg f "$FIELD_ID" --argjson o "$OPTIONS_JSON"
}

# One targeted query (1 point): this issue's item on this board, and its Status.
read_card() {  # $1 = issue; sets ITEM_ID STATUS_NAME
  [ -n "$REPO" ] || { echo "repo unknown: pass --repo or a config with repo.remote" >&2; return 66; }
  local out
  out=$(gh_read api graphql -f query='query($owner:String!,$name:String!,$issue:Int!){
      repository(owner:$owner,name:$name){ issueOrPullRequest(number:$issue){
        ... on Issue { projectItems(first:20){ nodes{ id project{ number owner{ ... on User{login} ... on Organization{login} } }
          fieldValueByName(name:"Status"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } } } } }
        ... on PullRequest { projectItems(first:20){ nodes{ id project{ number owner{ ... on User{login} ... on Organization{login} } }
          fieldValueByName(name:"Status"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } } } } } } } }' \
      -F owner="${REPO%%/*}" -F name="${REPO##*/}" -F issue="$1") || return $?
  echo "$out" | no_errors || { echo "card read for #$1 returned errors" >&2; return 69; }
  local node
  node=$(echo "$out" | jq -c --argjson n "$NUMBER" --arg o "$OWNER" '
    [.data.repository.issueOrPullRequest.projectItems.nodes[]?
     | select(.project.number == $n and ((.project.owner.login // "") | ascii_downcase) == ($o | ascii_downcase))][0] // empty')
  [ -n "$node" ] || { echo "#$1 is not on project $OWNER/$NUMBER" >&2; return 3; }
  ITEM_ID=$(echo "$node" | jq -r .id)
  STATUS_NAME=$(echo "$node" | jq -r '.fieldValueByName.name // ""')
  cache_put '.items[$k] = $v' --arg k "$1" --arg v "$ITEM_ID"
}

item_id() {  # $1 = issue
  ITEM_ID=$(cache_get ".items[\"$1\"]")
  [ -n "$ITEM_ID" ] || read_card "$1"
}

set_status() {  # $1 = item id, $2 = option id — one mutation
  local out
  out=$(gh api graphql -f query='mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){
      updateProjectV2ItemFieldValue(input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){ projectV2Item{ id } } }' \
      -f p="$PROJECT_ID" -f i="$1" -f f="$FIELD_ID" -f o="$2" 2>&1) || { echo "$out" >&2; return 69; }
  echo "$out" | no_errors || { echo "$out" >&2; return 69; }
}

# ── card fields (Agent, Skills) ─────────────────────────────────────────
# Best effort, only when move got --lane/--model. Never creates or edits field
# options: updateProjectV2Field reissues every option id and wipes the values.
load_fields() {  # fills FIELDS_JSON {name:{id,options?}} — 0 points when cached, else 1
  FIELDS_JSON=$(cache_get '.fields // empty | tojson')
  [ -n "$FIELDS_JSON" ] && return 0
  local out
  out=$(gh api graphql -f query='query($owner:String!,$number:Int!){
      repositoryOwner(login:$owner){ ... on ProjectV2Owner { projectV2(number:$number){
        fields(first:50){ nodes{
          ... on ProjectV2Field { id name }
          ... on ProjectV2SingleSelectField { id name options { id name } } } } } } } }' \
      -F owner="$OWNER" -F number="$NUMBER") || return 1
  echo "$out" | no_errors || return 1
  FIELDS_JSON=$(echo "$out" | jq -c '[.data.repositoryOwner.projectV2.fields.nodes[]?
      | select(.name == "Agent" or .name == "Skills")
      | {(.name): ({id: .id} + (if .options then {options: ([.options[] | {(.name): .id}] | add // {})} else {} end))}] | add // {}') || return 1
  cache_put '.fields = $f' --argjson f "$FIELDS_JSON"
}

META_NODE=""
read_meta() {  # $1 issue; fills META_NODE {body,labels} once per run — 1 point
  [ -z "$META_NODE" ] || return 0
  local out
  out=$(gh api graphql -f query='query($owner:String!,$name:String!,$issue:Int!){
      repository(owner:$owner,name:$name){ issueOrPullRequest(number:$issue){
        ... on Issue { body milestone{ title } labels(first:30){ nodes{ name } } }
        ... on PullRequest { body milestone{ title } labels(first:30){ nodes{ name } } } } } }' \
      -F owner="${REPO%%/*}" -F name="${REPO##*/}" -F issue="$1") || return 1
  echo "$out" | no_errors || return 1
  META_NODE=$(echo "$out" | jq -c '.data.repository.issueOrPullRequest // empty') || return 1
  [ -n "$META_NODE" ] || return 1
}

warn_agent_option_missing() {  # $1 issue, $2 Agent value — one comment per value, never twice
  local marker="<!-- sb:agent-option-missing:$2 -->"
  # Comments are read only here, when the option is missing: all of them, so an old marker is never missed.
  local cj
  if ! cj=$(gh api "repos/$REPO/issues/$1/comments" --paginate 2>/dev/null) || ! echo "$cj" | jq -es . >/dev/null 2>&1; then
    echo "warning: could not read #$1's comments; missing-option comment not posted (no duplicate risk taken)" >&2; return 0
  fi
  if echo "$cj" | jq -es --arg m "$marker" '[.[][]? | .body // ""] | any(contains($m))' 2>/dev/null | grep -qx true; then return 0; fi
  gh issue comment "$1" --repo "$REPO" --body "$marker
⚠️ [board] Agent '$2' is not an option on the board; add it in the board UI (Agent field → Edit). Never add it through the API: updateProjectV2Field rebuilds the options and wipes the Agent value on every card." >/dev/null \
    || echo "warning: could not post the missing-option comment on #$1" >&2
}

fill_card_fields() {  # $1 issue, $2 Status moved to, $3 lane, $4 model — returns 1 on any failure
  local issue="$1" status="$2" lane="$3" model="$4" out node labels body skills agent cstatus
  [ -n "$lane" ] || return 0
  # Lanes run inside .claude/worktrees/issue-*; the skills resolve from the main checkout.
  local ROOT; ROOT=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  if [ -n "$ROOT" ]; then ROOT=$(dirname "$ROOT"); else ROOT=.; fi
  load_fields || { echo "warning: could not read the board's fields; Agent/Skills left alone" >&2; return 1; }
  [ "$FIELDS_JSON" != "{}" ] || { cache_put 'del(.fields)'; echo "note: board has no Agent/Skills fields; skipped" >&2; return 0; }
  local q='mutation($p:ID!,$i:ID!' args=() sets='' k=0 fid oid
  if [ "$lane" = lint ]; then
    skills="super-board"   # lint has no lane skill set: fixed text, no resolver, no card-meta read
  else
  read_meta "$issue" || return 1
  node="$META_NODE"
  labels=$(echo "$node" | jq -c '[.labels.nodes[]?.name]')
  body=$(echo "$node" | jq -r '.body // ""')
  # The skills resolver wants the status the lane is entered from.
  case "$lane" in build) cstatus=Ready ;; qa) cstatus=QA ;; *) cstatus=Review ;; esac
  skills=$(jq -n --argjson n "$issue" --arg s "$cstatus" --argjson l "$labels" --arg b "$body" '[{number:$n,status:$s,labels:$l,body:$b}]' \
    | python3 "$HERE/super-board-skills-ready.py" --root "$ROOT" --host "${HOST:-claude}" --lane "$lane" --cards - 2>/dev/null \
    | jq -r '[.entries[]? | select(.status == "resolved") | (.name | sub("^[^:]*:"; ""))
              | select(. != "super-board")] | reduce .[] as $n ([]; if index($n) then . else . + [$n] end) | join(", ")' 2>/dev/null || true)
  fi
  fid=$(echo "$FIELDS_JSON" | jq -r '.Skills.id // empty')
  if [ -z "$fid" ]; then cache_put 'del(.fields)'; echo "note: board has no 'Skills' field; skipped" >&2
  elif [ -z "$skills" ]; then echo "warning: no skill ids resolved for lane $lane; Skills left unset" >&2
  else
    q="$q,\$f$k:ID!,\$t$k:String!"; args+=(-f "f$k=$fid" -f "t$k=$skills")
    sets="$sets a$k:updateProjectV2ItemFieldValue(input:{projectId:\$p,itemId:\$i,fieldId:\$f$k,value:{text:\$t$k}}){ clientMutationId }"
    k=$((k + 1))
  fi
  if [ -n "$model" ]; then
    agent="$model · super-$lane"; [ "$status" != Done ] || agent="Done · $agent"
    fid=$(echo "$FIELDS_JSON" | jq -r '.Agent.id // empty')
    oid=$(echo "$FIELDS_JSON" | jq -r --arg o "$agent" '.Agent.options[$o] // empty')
    if [ -z "$fid" ]; then cache_put 'del(.fields)'; echo "note: board has no 'Agent' field; skipped" >&2
    elif [ -z "$oid" ]; then cache_put 'del(.fields)'; echo "warning: 'Agent' has no option '$agent'; add it in the board UI (field settings). Left unset." >&2
      echo "error: Agent option '$agent' is missing on the board; add it in the board UI (Agent field → Edit). Never add it through the API: updateProjectV2Field rebuilds the options and wipes the Agent value on every card." >&2
      warn_agent_option_missing "$issue" "$agent"
    else
      q="$q,\$f$k:ID!,\$o$k:String!"; args+=(-f "f$k=$fid" -f "o$k=$oid")
      sets="$sets a$k:updateProjectV2ItemFieldValue(input:{projectId:\$p,itemId:\$i,fieldId:\$f$k,value:{singleSelectOptionId:\$o$k}}){ clientMutationId }"
      k=$((k + 1))
    fi
  fi
  [ "$k" -gt 0 ] || return 0
  out=$(gh api graphql -f query="$q){$sets }" -f p="$PROJECT_ID" -f i="$ITEM_ID" "${args[@]}" 2>&1) || { echo "$out" >&2; cache_put 'del(.fields)'; return 1; }
  echo "$out" | no_errors || { echo "$out" >&2; cache_put 'del(.fields)'; return 1; }
}

is_protected() {  # $1 = issue; true when the config's protected_issues holds it
  echo "$PROTECTED" | jq -e --arg n "$1" 'any(.[]; tostring == $n)' >/dev/null
}

PR_BASE=""; MERGE_SHA=""
verify_pr_merged() {  # $1 issue, $2 PR; sets PR_BASE MERGE_SHA — 64 unless MERGED with a merge commit on the base branch
  local out state base
  out=$(gh pr view "$2" --repo "$REPO" --json state,mergeCommit,baseRefName,closingIssuesReferences,headRefName,body,title 2>/dev/null) \
    && echo "$out" | jq -e . >/dev/null 2>&1 || { echo "could not read PR #$2; not moving #$1" >&2; return 69; }
  state=$(echo "$out" | jq -r '.state // ""'); MERGE_SHA=$(echo "$out" | jq -r '.mergeCommit.oid // ""')
  PR_BASE=$(echo "$out" | jq -r '.baseRefName // ""')
  if [ "$state" != MERGED ] || [ -z "$MERGE_SHA" ] || { [ -n "$BASE" ] && [ "$PR_BASE" != "$BASE" ]; }; then
    echo "PR #$2 is not merged into ${BASE:-${PR_BASE:-its base branch}}; not moving #$1" >&2; return 64
  fi
  # The PR must belong to the issue: a closing reference, an issue-N branch, or #N as a whole token in the body/title.
  # (gh prints closingIssuesReferences as an array; a nodes wrapper is accepted too.)
  if ! echo "$out" | jq -e --arg n "$1" '
      ([(.closingIssuesReferences | if type == "array" then . else (.nodes // []) end)[]?.number] | any(tostring == $n))
      or ((.headRefName // "") | test("(^|/)issue-" + $n + "(-|$)"))
      or (((.body // "") + "\n" + (.title // "")) | test("(^|[^0-9A-Za-z_/])#" + $n + "([^0-9A-Za-z_]|$)"))' >/dev/null 2>&1; then
    echo "PR #$2 does not reference #$1; not moving it" >&2; return 64
  fi
}

# After the Status is Done: one marker comment (never twice), then close the issue if it is open.
# Any failure here is loud: 69, with the Status already changed; the next run retries.
done_close() {  # $1 issue
  local issue="$1" cj st body why marker
  if [ -n "$PR" ]; then marker="<!-- sb:done-closed:$MERGE_SHA -->"; else marker="<!-- sb:done-closed:none -->"; fi
  why="could not read its comments"
  if cj=$(gh api "repos/$REPO/issues/$issue/comments" --paginate 2>/dev/null) && echo "$cj" | jq -es . >/dev/null 2>&1; then
    why=""
    # Exact marker only: an old "none" marker must not hide the sha marker of a later Done with a PR.
    if ! echo "$cj" | jq -es --arg m "$marker" '[.[][]? | .body // ""] | any(contains($m))' 2>/dev/null | grep -qx true; then
      if [ -n "$PR" ]; then body="$marker
✅ Done: merged in #$PR (${MERGE_SHA:0:7}) into $PR_BASE."
      else body="$marker
✅ Done without a PR: $NOPR"; fi
      gh issue comment "$issue" --repo "$REPO" --body "$body" >/dev/null 2>&1 || why="could not post the merge comment"
    fi
  fi
  if [ -z "$why" ]; then
    why="could not read its state"
    if st=$(gh issue view "$issue" --repo "$REPO" --json state 2>/dev/null) && st=$(echo "$st" | jq -r '.state // ""') && [ -n "$st" ]; then
      why=""
      if [ "$st" = OPEN ]; then
        gh issue close "$issue" --repo "$REPO" --reason completed >/dev/null 2>&1 || why="could not close the issue"
      fi
    fi
  fi
  [ -z "$why" ] || { echo "moved #$issue → Done, but closing it failed: $why" >&2; return 69; }
}

move() {  # $1 = issue, $2 = Status name
  local issue="$1" name="$2" opt attempt route="" labelled=0 need_label=0
  if [ "$name" = Done ]; then
    [ -n "$REPO" ] || { echo "repo unknown: pass --repo or a config with repo.remote" >&2; return 66; }
    # Done means merged on the base branch: prove it before any mutation.
    if [ -n "$PR" ]; then verify_pr_merged "$issue" "$PR" || return $?; fi
  fi
  # Building, QA, Review and Blocked need a type label. Read it before any mutation.
  case "$name" in Building|QA|Review|Blocked) need_label=1 ;; esac
  if [ "$need_label" = 1 ]; then
    [ -n "$REPO" ] || { echo "repo unknown: pass --repo or a config with repo.remote" >&2; return 66; }
    read_meta "$issue" || { echo "could not read #$issue's labels; not moving it" >&2; return 69; }
    route=$(echo "$META_NODE" | jq -r '[.labels.nodes[]?.name | ascii_downcase | select(IN("bug","feature","qa","docs","ux","tests","refactor","tech-debt","money"))][0] // ""')
    if [ -z "$route" ] && [ -z "$KIND" ]; then
      echo "#$issue has no type label (bug, feature, qa, docs, ux, tests, refactor, tech-debt or money); pass --kind" >&2; return 64
    fi
  fi
  for attempt in 1 2; do
    load_ids || return $?
    opt=$(echo "$OPTIONS_JSON" | jq -r --arg n "$name" '.[$n] // empty')
    if [ -z "$opt" ]; then
      [ "$attempt" = 1 ] && { cache_drop; continue; }
      echo "Status option '$name' is not on project $OWNER/$NUMBER" >&2; return 3
    fi
    item_id "$issue" || return $?
    if [ "$need_label" = 1 ] && [ -z "$route" ] && [ "$labelled" = 0 ]; then
      gh issue edit "$issue" --repo "$REPO" --add-label "$KIND" >/dev/null \
        || { echo "could not add label '$KIND' to #$issue; not moving it" >&2; return 69; }
      labelled=1; echo "note: #$issue had no type label; added '$KIND'" >&2
      # The cached labels feed fill_card_fields (Skills resolve per label): keep them current.
      META_NODE=$(echo "$META_NODE" | jq -c --arg k "$KIND" '.labels.nodes = ((.labels.nodes // []) + [{name: $k}])')
    fi
    if set_status "$ITEM_ID" "$opt"; then
      echo "moved #$issue → $name"
      case "$name" in Building|Review)
        if [ "$issue" != "$ROADMAP" ] && echo "$META_NODE" | jq -e '.milestone == null and ([.labels.nodes[]?.name | ascii_downcase] | index("needs-grilling") | not)' >/dev/null 2>&1; then
          echo "warning: #$issue has no milestone; set one on the issue (the Roadmap counts issues per milestone)" >&2
        fi ;;
      esac
      if [ -n "$LANE$MODEL" ]; then
        fill_card_fields "$issue" "$name" "$LANE" "$MODEL" || echo "warning: #$issue moved, but its Agent/Skills fields were not all written" >&2
      fi
      if [ "$name" = Done ]; then done_close "$issue" || return $?; fi
      return 0
    fi
    [ "$attempt" = 1 ] && { echo "move failed; re-reading board ids once" >&2; cache_drop; }
  done
  return 69
}

release() {  # $1 = PR (release PR: base_branch into the default branch), $2 = 1 for --dry-run
  local pr="$1" dry="$2" view def shas n cand cj marks m matches=() failed=() count=0
  [ -n "$REPO" ] || { echo "repo unknown: pass --repo or a config with repo.remote" >&2; return 66; }
  [ -n "$BASE" ] || { echo "release needs base_branch in the config" >&2; return 66; }
  view=$(gh pr view "$pr" --repo "$REPO" --json state,baseRefName,headRefName,mergeCommit 2>/dev/null) \
    && echo "$view" | jq -e . >/dev/null 2>&1 || { echo "could not read PR #$pr" >&2; return 69; }
  def=$(gh repo view "$REPO" --json defaultBranchRef 2>/dev/null | jq -r '.defaultBranchRef.name // ""') || def=""
  [ -n "$def" ] || { echo "could not read the default branch of $REPO" >&2; return 69; }
  if [ "$(echo "$view" | jq -r '.state // ""')" != MERGED ] || [ "$(echo "$view" | jq -r '.baseRefName // ""')" != "$def" ] \
     || [ "$(echo "$view" | jq -r '.headRefName // ""')" != "$BASE" ]; then
    echo "PR #$pr is not a merged release PR from $BASE into $def; changing nothing" >&2; return 64
  fi
  shas=$(gh api "repos/$REPO/pulls/$pr/commits" --paginate --jq '.[].sha' 2>/dev/null | tr -d '\r') || { echo "could not read PR #$pr's commits" >&2; return 69; }
  n=$(printf '%s\n' "$shas" | grep -c . || true)
  [ "$n" -lt 250 ] || { echo "PR #$pr has $n commits; GitHub caps that list at 250, so nothing was labelled; release PRs into the default branch must be merge commits, not squash" >&2; return 69; }
  cand=$(gh issue list --repo "$REPO" --state closed --search "-label:released" --limit 1000 --json number 2>/dev/null \
    | jq -r '.[].number' | tr -d '\r') || { echo "could not list closed issues" >&2; return 69; }
  for n in $cand; do
    if is_protected "$n"; then continue; fi
    if ! cj=$(gh api "repos/$REPO/issues/$n/comments" --paginate 2>/dev/null) || ! echo "$cj" | jq -es . >/dev/null 2>&1; then
      failed+=("#$n (could not read comments)"); continue
    fi
    marks=$(echo "$cj" | jq -es -r '[.[][]? | .body // "" | scan("<!-- sb:done-closed:([0-9a-f]+) -->") | .[0]] | .[]' 2>/dev/null | tr -d '\r' || true)
    for m in $marks; do
      if printf '%s\n' "$shas" | grep -qxF "$m"; then matches+=("$n"); break; fi
    done
  done
  if [ "$dry" != 1 ] && ! gh label list --repo "$REPO" --json name --limit 500 2>/dev/null | jq -e 'any(.[]; .name == "released")' >/dev/null 2>&1; then
    gh label create released --repo "$REPO" --color 0E8A16 --description "Shipped to the default branch" >/dev/null 2>&1 \
      || { echo "could not create the released label; nothing labelled" >&2; return 69; }
  fi
  for n in ${matches[@]+"${matches[@]}"}; do
    if [ "$dry" = 1 ]; then echo "would release #$n"; count=$((count + 1)); continue; fi
    if gh issue edit "$n" --repo "$REPO" --add-label released >/dev/null 2>&1; then echo "released #$n"; count=$((count + 1))
    else failed+=("#$n (label add failed)"); fi
  done
  echo "released: $count issue(s) from PR #$pr"
  [ ${#failed[@]} -eq 0 ] || { echo "release failed for: ${failed[*]}" >&2; return 69; }
}

items() {  # whole board, gh item-list shape, ~1 point per 100 cards
  local pages
  pages=$(gh_read api graphql --paginate --slurp -f query='query($owner:String!,$number:Int!,$endCursor:String){
      repositoryOwner(login:$owner){ ... on ProjectV2Owner { projectV2(number:$number){
        items(first:100, after:$endCursor){ totalCount pageInfo{ hasNextPage endCursor }
          nodes{ id fieldValueByName(name:"Status"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } }
            content{ __typename
              ... on Issue { number title url repository{ nameWithOwner } labels(first:30){ nodes{ name } } }
              ... on PullRequest { number title url repository{ nameWithOwner } labels(first:30){ nodes{ name } } }
              ... on DraftIssue { title } } } } } } } }' \
      -F owner="$OWNER" -F number="$NUMBER") || return $?
  echo "$pages" | no_errors || { echo "board read returned errors" >&2; return 69; }
  echo "$pages" | jq -e '
    [ .[].data.repositoryOwner.projectV2.items ] as $p
    | ($p[0].totalCount) as $total
    | [ $p[].nodes[] | select(.content != null)
        | { id,
            title: .content.title,
            content: ({ type: .content.__typename, title: .content.title }
                      + (if .content.__typename == "DraftIssue" then {} else
                          { number: .content.number, url: .content.url,
                            repository: .content.repository.nameWithOwner } end)) }
          + (if .fieldValueByName.name then { status: .fieldValueByName.name } else {} end)
          + (if ((.content.labels.nodes // []) | length) > 0
             then { labels: [ .content.labels.nodes[].name ] } else {} end) ] as $items
    | if ($items | length) == ([ $p[].nodes[] ] | length) and ([ $p[].nodes[] ] | length) == $total
      then { items: $items, totalCount: $total }
      else error("board read incomplete: \([ $p[].nodes[] ] | length) of \($total) items") end' \
    || { echo "board read incomplete" >&2; return 69; }
}

audit() {  # $1 = 1 for --json. One paginated read; lists open Issues in a lane column missing Agent, Skills or a type label.
  local pages res rc=0
  pages=$(gh_read api graphql --paginate --slurp -f query='query($owner:String!,$number:Int!,$endCursor:String){
      repositoryOwner(login:$owner){ ... on ProjectV2Owner { projectV2(number:$number){
        items(first:100, after:$endCursor){ totalCount pageInfo{ hasNextPage endCursor }
          nodes{ id status: fieldValueByName(name:"Status"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } }
            agent: fieldValueByName(name:"Agent"){ ... on ProjectV2ItemFieldSingleSelectValue{ name } ... on ProjectV2ItemFieldTextValue{ text } }
            skills: fieldValueByName(name:"Skills"){ ... on ProjectV2ItemFieldTextValue{ text } }
            content{ __typename
              ... on Issue { number state repository{ nameWithOwner } labels(first:30){ nodes{ name } } }
              ... on PullRequest { number labels(first:30){ nodes{ name } } } } } } } } } }' \
      -F owner="$OWNER" -F number="$NUMBER") || rc=$?
  if [ "$rc" -ne 0 ]; then
    if [ "$rc" = 79 ]; then return 79; fi
    echo "audit: board read failed" >&2; return 69
  fi
  echo "$pages" | no_errors || { echo "audit: board read returned errors" >&2; return 69; }
  res=$(echo "$pages" | jq -ce --arg repo "$REPO" '
    [ .[].data.repositoryOwner.projectV2.items ] as $p
    | ($p[0].totalCount) as $total
    | [ $p[].nodes[] ] as $all
    | if ($all | length) != $total then error("board read incomplete: \($all | length) of \($total) items") else
      [ $all[] | select(.content != null and .content.__typename == "Issue" and .content.number != null and (.content.state // "") != "CLOSED"
                and ((.content.repository.nameWithOwner // "") | ascii_downcase) == ($repo | ascii_downcase))
        | { number: .content.number, status: (.status.name // ""),
            agent: ((.agent.name // .agent.text // "") | gsub("\\s"; "")),
            skills: ((.skills.text // "") | gsub("\\s"; "")),
            routed: ([ (.content.labels.nodes // [])[].name | ascii_downcase | select(IN("bug","feature","qa","docs","ux","tests","refactor","tech-debt","money")) ] | length > 0) }
        | select(.status | IN("Building", "QA", "Review", "Blocked", "Done"))
        | { number, status, missing: (if .status == "Done" then ["closed"] else
                                      [ (if .agent == "" then "agent" else empty end),
                                        (if .skills == "" then "skills" else empty end),
                                        (if .routed then empty else "label" end) ] end) }
        | select(.missing | length > 0) ] | sort_by(.number) end') \
    || { echo "audit: board read incomplete" >&2; return 69; }
  if [ "$1" = 1 ]; then echo "$res"; return 0; fi
  echo "$res" | jq -r '.[] | "#\(.number) \(.status) missing: \(.missing | join(","))"' | tr -d '\r'
  echo "$res" | jq -r 'if length == 0 then "audit: clean" else "audit: \(length) card(s) missing fields" end' | tr -d '\r'
}

case "$VERB" in
  move)
    LANE=""; MODEL=""; TOOL=""; HOST=claude; KIND=""; PR=""; NOPR=""; NOPR_SET=0; MARGS=()
    while [ $# -gt 0 ]; do
      case "$1" in
        --lane)  [ $# -ge 2 ] || { echo "--lane needs a value" >&2; exit 64; }; LANE="$2"; shift 2 ;;
        --model) [ $# -ge 2 ] || { echo "--model needs a value" >&2; exit 64; }; MODEL="$2"; shift 2 ;;
        --tool)  [ $# -ge 2 ] || { echo "--tool needs a value" >&2; exit 64; }; TOOL=$(echo "$2" | tr '[:upper:]' '[:lower:]'); shift 2 ;;
        --host)  [ $# -ge 2 ] || { echo "--host needs a value" >&2; exit 64; }; HOST="$2"; shift 2 ;;
        --kind)  [ $# -ge 2 ] || { echo "--kind needs a value" >&2; exit 64; }; KIND=$(echo "$2" | tr '[:upper:]' '[:lower:]'); shift 2 ;;
        --pr)    [ $# -ge 2 ] || { echo "--pr needs a value" >&2; exit 64; }; PR="$2"; shift 2 ;;
        --no-pr) [ $# -ge 2 ] || { echo "--no-pr needs a reason" >&2; exit 64; }; NOPR="$2"; NOPR_SET=1; shift 2 ;;
        *) MARGS+=("$1"); shift ;;
      esac
    done
    case "$KIND" in ""|bug|feature|qa) ;; *) echo "--kind must be bug, feature or qa" >&2; exit 64 ;; esac
    case "$HOST" in claude|codex) ;; *) echo "--host must be claude or codex" >&2; exit 64 ;; esac
    if [ -n "$TOOL" ]; then
      [ -z "$MODEL" ] || { echo "pass --model or --tool, not both" >&2; exit 64; }
      case "$TOOL" in grok) MODEL=Grok ;; pi) MODEL=Pi ;; codex) MODEL=Codex ;; *) echo "--tool must be grok, pi or codex" >&2; exit 64 ;; esac
      [ -n "$LANE" ] || { echo "--tool needs --lane" >&2; exit 64; }
    fi
    [ -z "$MODEL" ] || [ -n "$LANE" ] || { echo "--model needs --lane" >&2; exit 64; }
    case "$LANE" in ""|build|qa|review|lint) ;; *) echo "--lane must be build, qa, review or lint" >&2; exit 64 ;; esac
    [ ${#MARGS[@]} -eq 2 ] || { echo "usage: move <issue> <Status> [--lane build|qa|review|lint] [--model <Model>] [--tool grok|pi|codex] [--kind bug|feature|qa] [--pr 50 | --no-pr \"reason\"]" >&2; exit 64; }
    [[ "${MARGS[0]}" =~ ^[1-9][0-9]*$ ]] || { echo "issue must be a plain number like 12, got: ${MARGS[0]}" >&2; exit 64; }
    if is_protected "${MARGS[0]}"; then echo "#${MARGS[0]} is protected (config protected_issues); not changing it" >&2; exit 64; fi
    if [ "$LANE" = lint ]; then
      case "${MARGS[1]}" in Blocked|Done) ;; *) echo "--lane lint only moves a card into Blocked or Done" >&2; exit 64 ;; esac
    fi
    # Family word, any spelling: "Claude Opus 5.5" and "opus" both become Opus; anything else stays as given.
    if [ -n "$MODEL" ]; then
      case "$(echo "$MODEL" | tr '[:upper:]' '[:lower:]')" in
        *haiku*) MODEL=Haiku ;; *sonnet*) MODEL=Sonnet ;; *opus*) MODEL=Opus ;; *codex*) MODEL=Codex ;; *grok*) MODEL=Grok ;; pi) MODEL=Pi ;;
      esac
    fi
    case "${MARGS[1]}" in
      Building|QA|Review|Blocked|Done)
        [ -n "$LANE" ] && [ -n "$MODEL" ] || { echo "move into ${MARGS[1]} needs --lane and --model (Agent/Skills are mandatory)" >&2; exit 64; } ;;
    esac
    # Done means merged on the base branch (--pr) or an explicit no-PR reason (--no-pr); never on any other Status.
    # Normalise --pr first (#50 or a PR URL become 50), so "--pr '#'" is caught as empty below.
    PR="${PR#\#}"
    case "$PR" in */pull/[0-9]*) PR="${PR##*/pull/}"; PR="${PR%%[!0-9]*}" ;; esac
    if [ "${MARGS[1]}" = Done ]; then
      if [ -n "$PR" ] && [ "$NOPR_SET" = 1 ]; then echo "move into Done takes --pr or --no-pr, not both" >&2; exit 64; fi
      if [ -z "$PR" ] && [ "$NOPR_SET" = 0 ]; then echo "move into Done needs --pr (the merged PR) or --no-pr with a reason" >&2; exit 64; fi
      if [ "$NOPR_SET" = 1 ] && [ -z "$(echo "$NOPR" | tr -d '[:space:]')" ]; then echo "--no-pr needs a non-empty reason" >&2; exit 64; fi
      case "$PR" in ""|*[!0-9]*) [ -z "$PR" ] || { echo "--pr needs a PR number (50, #50 or a PR URL)" >&2; exit 64; } ;; esac
      # A Done move proves the merge against base_branch, so it needs a config (checked before any read).
      # base_branch falls back to the schema default "main", as setup.py does.
      [ -n "$CONFIG" ] || { echo "move into Done needs --config (base_branch, protected_issues)" >&2; exit 66; }
      [ -n "$BASE" ] || BASE=main
    elif [ -n "$PR" ] || [ "$NOPR_SET" = 1 ]; then
      echo "--pr and --no-pr are only for a move into Done" >&2; exit 64
    fi
    move "${MARGS[0]}" "${MARGS[1]}" ;;
  release)
    RDRY=0; RPR=""
    for a in "$@"; do
      case "$a" in --dry-run) RDRY=1 ;; -*) echo "usage: release 60 [--dry-run]" >&2; exit 64 ;; *) [ -z "$RPR" ] && RPR="$a" || { echo "usage: release 60 [--dry-run]" >&2; exit 64; } ;; esac
    done
    case "$RPR" in ""|*[!0-9]*) echo "usage: release 60 [--dry-run]" >&2; exit 64 ;; esac
    release "$RPR" "$RDRY" ;;
  audit)
    AJSON=0
    case "${1:-}" in "") ;; --json) AJSON=1; shift ;; *) echo "usage: audit [--json]" >&2; exit 64 ;; esac
    [ $# -eq 0 ] || { echo "usage: audit [--json]" >&2; exit 64; }
    audit "$AJSON" ;;
  status)  [ $# -eq 1 ] || { echo "usage: status <issue>" >&2; exit 64; }; read_card "$1"; echo "$STATUS_NAME" ;;
  item-id) [ $# -eq 1 ] || { echo "usage: item-id <issue>" >&2; exit 64; }; item_id "$1"; echo "$ITEM_ID" ;;
  ids)     load_ids; jq -n --arg p "$PROJECT_ID" --arg f "$FIELD_ID" --argjson o "$OPTIONS_JSON" '{projectId:$p, fieldId:$f, options:$o}' ;;
  items)   items ;;
  *) echo "unknown verb: $VERB" >&2; exit 64 ;;
esac
