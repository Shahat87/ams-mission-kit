#!/usr/bin/env bash
# Baseline regression suite for super-collect-file.sh, driven by the ticket fixtures in
# tests/fixtures/collect/tickets/. Pins today's behaviour of: fingerprint dedupe (open hit →
# "Seen again" comment, closed-only → recurrence ticket), ticket-format rejection, the
# no-holding-column refusal (exit 65), and the PostHog tracking-gap ticket shape.
# Deterministic, no network: `gh` is a stub on PATH; the real QA / review filers run under it.
#
# `xfail` checks pin a behaviour SKILL.md promises that the code does not deliver yet: they
# print XFAIL and do not fail the suite; once the bug is fixed they print XPASS and fail, so
# the check gets promoted to a plain one.
#
#   bash tests/test-collect-baseline.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_ROOT/skills/super-collect/scripts/super-collect-file.sh"
TICKETS="$REPO_ROOT/tests/fixtures/collect/tickets"
WORK="$(mktemp -d)"
export SB_GITHUB_RETRY_DELAY=0 SB_GITHUB_HALT_FILE="$WORK/halt.json"
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0; KNOWN=0
ok()  { PASS=$((PASS + 1)); printf '  ✅ %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf '  ❌ %s\n     expected: %s\n     actual:   %s\n' "$1" "$2" "$3"; }
is()  { [ "$2" = "$3" ] && ok "$1" || bad "$1" "$2" "$3"; }
has() { case "$2" in *"$3"*) ok "$1" ;; *) bad "$1" "contains: $3" "$2" ;; esac; }
lacks() { case "$2" in *"$3"*) bad "$1" "no: $3" "found it" ;; *) ok "$1" ;; esac; }
xfail() { # xfail <name> <expected> <actual>: known bug, expected to differ today
  if [ "$2" = "$3" ]; then FAIL=$((FAIL + 1)); printf '  ❗ XPASS %s — bug fixed? make it a plain check\n' "$1"
  else KNOWN=$((KNOWN + 1)); printf '  ⚠️  XFAIL %s (want %s, got %s)\n' "$1" "$2" "$3"; fi
}

cat > "$WORK/config.json" <<'JSON'
{"version":1,"project":{"owner":"example","title":"Shop","number":1},
 "variant":"full","repo":{"path":".","remote":"https://github.com/example/shop.git"}}
JSON

# gh stub. Env switches:
#   STUB_HITS    — JSON array the paginated history read sees ([{number,state,body}])
#   STATUS_OPTS  — Status option names on the board (underscore = space)
cat > "$WORK/gh" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GH_LOG"
case "$1 ${2:-}" in
  "repo view")
    case "$*" in *nameWithOwner*) echo "example/shop";; *) echo "example";; esac ;;
  "api graphql")
    hits="${STUB_HITS:-[]}"
    printf '%s' "$hits" | jq '[{data:{repository:{issues:{totalCount:length,pageInfo:{hasNextPage:false,endCursor:null},nodes:.}}}}]' ;;
  "issue list")
    jq_expr=""; prev=""
    for a in "$@"; do [ "$prev" = "--jq" ] && jq_expr="$a"; prev="$a"; done
    hits="${STUB_HITS:-[]}"
    case "$*" in *"--state open"*) hits=$(echo "$hits" | jq '[.[] | select(.state == "OPEN")]');; esac
    if [ -n "$jq_expr" ]; then echo "$hits" | jq -r "$jq_expr"; else echo "$hits"; fi ;;
  "issue comment")
    prev=""
    for a in "$@"; do [ "$prev" = "--body" ] && printf '%s' "$a" > "$COMMENT_LOG"; prev="$a"; done ;;
  "issue create")
    prev=""
    for a in "$@"; do [ "$prev" = "--body-file" ] && [ -r "$a" ] && cat "$a" > "$BODY_LOG"; prev="$a"; done
    echo "https://github.com/example/shop/issues/412" ;;
  "project list")  echo '{"projects":[{"number":1,"title":"Shop"}]}' ;;
  "project item-add") echo "PVTI_stub" ;;
  "project view")  echo "PVT_stub" ;;
  "project field-list")
    OPTS=""
    for encoded in ${STATUS_OPTS:-Backlog Ready Done}; do
      name=$(echo "$encoded" | tr '_' ' ')
      OPTS="${OPTS}{\"id\":\"opt_${encoded}\",\"name\":\"${name}\"},"
    done
    echo "{\"fields\":[{\"id\":\"FLD_status\",\"name\":\"Status\",\"options\":[${OPTS%,}]}]}" ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$WORK/gh"
export PATH="$WORK:$PATH" GH_LOG="$WORK/gh.log" BODY_LOG="$WORK/body.filed" COMMENT_LOG="$WORK/comment.body"
export SUPER_BOARD_BIN="$REPO_ROOT/scripts"

run() { : > "$GH_LOG"; : > "$BODY_LOG"; : > "$COMMENT_LOG"; "$SCRIPT" --config "$WORK/config.json" "$@" 2>"$WORK/err"; }
code() { run "$@" >/dev/null; echo $?; }
WRITES='/^(issue (create|comment|edit)|label create|project (item-add|item-edit)) /'

BUG=(--type bug --source sentry --title "Checkout crashes when the last item is removed" --body-file "$TICKETS/bug-sentry-4411.md" --fingerprint "err|sentry|4411")
GAP=(--type feature --source posthog --title "Receipt upload: no success or failure event" --body-file "$TICKETS/feature-gap-receipt-upload.md" --fingerprint "gap|receipt-upload" --label analytics)
FIX=(--type fix --source prs --title "Date-only strings read with local getters" --body-file "$TICKETS/fix-prs-date-only-parsing.md" --fingerprint "prs|dates|date-only-local-getters")

echo "── the fixtures themselves are valid tickets"
for args in BUG GAP FIX; do
  eval "set -- \"\${${args}[@]}\""
  OUT=$(run "$@")
  has "$args fixture plans as would-file" "$OUT" "would-file|"
  is  "$args fixture files with --yes" "412" "$(run "$@" --yes)"
done

echo "── dedupe: open hit → 'Seen again' comment, never a second card"
OPEN='[{"number":301,"state":"OPEN","body":"…\n<!-- super-collect-fingerprint: err|sentry|4411 -->"}]'
is  "dry-run plans a duplicate" "duplicate|#301|bug|Checkout crashes when the last item is removed" "$(STUB_HITS="$OPEN" run "${BUG[@]}")"
OUT=$(STUB_HITS="$OPEN" run "${BUG[@]}" --yes)
is  "--yes returns the existing issue" "301" "$OUT"
has "comments on #301" "$(cat "$GH_LOG")" "issue comment 301"
is  "comment opens with 'Seen again' and the source" "Seen again by super-collect (sentry)." "$(head -1 "$COMMENT_LOG")"
has "comment carries the new evidence" "$(cat "$COMMENT_LOG")" "37 events, 14 users"
lacks "no issue create" "$(cat "$GH_LOG")" "issue create"
lacks "no labels or board moves on a duplicate" "$(awk '/^(label create|project item-(add|edit)) /' "$GH_LOG")" "project"
BOTH='[{"number":88,"state":"CLOSED","body":"err|sentry|4411"},{"number":301,"state":"OPEN","body":"err|sentry|4411"}]'
has "open beats closed when both exist" "$(STUB_HITS="$BOTH" run "${BUG[@]}")" "duplicate|#301"
has "dedupe paginates repo-wide all-state history" "$(cat "$GH_LOG")" "states:[OPEN,CLOSED]"
has "history read requests complete pages" "$(cat "$GH_LOG")" "--paginate --slurp"

echo "── dedupe: closed-only hit → recurrence, filed again naming the closed issue"
CLOSED='[{"number":88,"state":"CLOSED","body":"<!-- super-collect-fingerprint: prs|dates|date-only-local-getters -->"}]'
is  "dry-run plans a recurrence" "recurrence|#88|fix|Date-only strings read with local getters|Backlog" "$(STUB_HITS="$CLOSED" run "${FIX[@]}")"
is  "--yes files a new issue" "412" "$(STUB_HITS="$CLOSED" run "${FIX[@]}" --yes)"
has "body names the closed issue" "$(cat "$BODY_LOG")" "> Recurrence: this fingerprint was last filed as #88, which is closed."
has "body keeps the same fingerprint" "$(cat "$BODY_LOG")" "<!-- super-collect-fingerprint: prs|dates|date-only-local-getters -->"
lacks "no comment on the closed issue" "$(cat "$GH_LOG")" "issue comment"

echo "── ticket format: Problem · Context · Fix · Acceptance Criteria · Risk"
drop() { sed "/^## $1\$/,/^## /{/^## $1\$/d;/^## /!d;}" "$2"; }   # delete one section
drop Risk "$TICKETS/feature-gap-receipt-upload.md" > "$WORK/gap-no-risk.md"
is  "feature without Risk is refused" 66 "$(code --type feature --source posthog --title T --body-file "$WORK/gap-no-risk.md" --fingerprint "gap|receipt-upload")"
has "…and the error names the section" "$(cat "$WORK/err")" "missing required section(s): Risk"
drop Context "$TICKETS/feature-gap-receipt-upload.md" > "$WORK/gap-no-context.md"
is  "feature without Context is refused" 66 "$(code --type feature --source posthog --title T --body-file "$WORK/gap-no-context.md" --fingerprint "gap|receipt-upload")"
sed 's/^- \[ \] /- /' "$TICKETS/feature-gap-receipt-upload.md" > "$WORK/gap-no-checklist.md"
is  "Acceptance Criteria must be a checklist" 66 "$(code --type feature --source posthog --title T --body-file "$WORK/gap-no-checklist.md" --fingerprint "gap|receipt-upload")"
drop Evidence "$TICKETS/fix-prs-date-only-parsing.md" > "$WORK/fix-no-evidence.md"
is  "prs fix without Evidence is refused" 66 "$(code --type fix --source prs --title T --body-file "$WORK/fix-no-evidence.md" --fingerprint "prs|x|y")"
drop Fix "$TICKETS/fix-prs-date-only-parsing.md" > "$WORK/refactor-no-fix.md"
is  "refactor without Fix is refused" 66 "$(code --type refactor --source architecture --title T --body-file "$WORK/refactor-no-fix.md" --fingerprint "arch|dates|no-helper")"
drop Context "$TICKETS/bug-sentry-4411.md" > "$WORK/bug-no-context.md"
is  "bug without Context is refused on --yes (QA filer)" 66 "$(code "${BUG[@]/$TICKETS\/bug-sentry-4411.md/$WORK/bug-no-context.md}" --yes)"
lacks "…and nothing is created" "$(cat "$GH_LOG")" "issue create"
is  "bug without Context is refused on the dry-run too" \
  66 "$(code "${BUG[@]/$TICKETS\/bug-sentry-4411.md/$WORK/bug-no-context.md}")"
has "…and the dry-run error names the section" "$(cat "$WORK/err")" "missing required section(s): Context"
lacks "…and the dry-run writes nothing" "$(awk "$WRITES" "$GH_LOG")" "issue"
sed 's/^| Logs .*$//' "$TICKETS/bug-sentry-4411.md" > "$WORK/bug-11-rows.md"
is  "bug Evidence table needs all 12 rows" 66 "$(code "${BUG[@]/$TICKETS\/bug-sentry-4411.md/$WORK/bug-11-rows.md}" --yes)"

echo "── no holding column → refused with 65, nothing written"
for opts in "Ready QA Done" "Ready Building Review Done"; do
  is "board [$opts] refuses" 65 "$(STATUS_OPTS="$opts" code "${BUG[@]}" --yes)"
  is "board [$opts] made no writes" "" "$(awk "$WRITES" "$GH_LOG")"
done
has "refusal says why" "$(cat "$WORK/err")" "no holding column"
for col in Backlog Todo To_do Triage Inbox; do
  name=$(echo "$col" | tr '_' ' ')
  has "holding column '$name' is accepted" "$(STATUS_OPTS="Ready $col Done" run "${GAP[@]}")" "|$name"
done
has "Backlog wins over Todo" "$(STATUS_OPTS="Todo Backlog Ready" run "${GAP[@]}")" "|Backlog"

echo "── PostHog tracking gap → one analytics feature ticket"
is  "gap plan line" "would-file|feature|Receipt upload: no success or failure event|Backlog" "$(run "${GAP[@]}")"
is  "gap files" "412" "$(run "${GAP[@]}" --yes)"
has "routes via the QA filer as a feature" "$(cat "$GH_LOG")" "--label feature"
has "labels analytics"        "$(cat "$GH_LOG")" "--add-label analytics"
has "labels collect:posthog"  "$(cat "$GH_LOG")" "--add-label collect:posthog"
has "labels source:collect"   "$(cat "$GH_LOG")" "--add-label source:collect"
has "stamps gap fingerprint"  "$(cat "$BODY_LOG")" "<!-- super-collect-fingerprint: gap|receipt-upload -->"
has "stamps the source"       "$(cat "$BODY_LOG")" "<!-- super-collect-source: posthog -->"
has "lands in Backlog"        "$(cat "$GH_LOG")" "opt_Backlog"
lacks "never Ready"           "$(cat "$GH_LOG")" "opt_Ready"
is  "gap| needs a workflow key" 64 "$(code --type feature --source posthog --title T --body-file "$TICKETS/feature-gap-receipt-upload.md" --fingerprint "gap|")"
is  "gap| only for posthog"    64 "$(code --type feature --source prs --title T --body-file "$TICKETS/feature-gap-receipt-upload.md" --fingerprint "gap|receipt-upload")"

echo "── prs pattern ticket"
is  "prs| prefix required" 64 "$(code --type fix --source prs --title T --body-file "$TICKETS/fix-prs-date-only-parsing.md" --fingerprint "dates|x")"
is  "pattern files once" "412" "$(run "${FIX[@]}" --yes)"
has "files as tech-debt" "$(cat "$GH_LOG")" "--label tech-debt"
has "labels collect:prs" "$(cat "$GH_LOG")" "--add-label collect:prs"

echo "── dedupe matches the fingerprint exactly"
PREFIX='[{"number":301,"state":"OPEN","body":"<!-- super-collect-fingerprint: err|sentry|4411 -->"}]'
is  "Sentry 441 is not a duplicate of Sentry 4411" \
  "would-file" "$(STUB_HITS="$PREFIX" run --type bug --source sentry --title T --body-file "$TICKETS/bug-sentry-4411.md" --fingerprint "err|sentry|441" | cut -d'|' -f1)"

echo "── SKILL.md setup step 3: source the gh guard, then sb_gh_guard_check"
if command -v zsh >/dev/null; then
  RC=0; (cd "$WORK" && zsh -c "source '$REPO_ROOT/scripts/super-board-gh-guard.sh'; sb_gh_halt_check" >/dev/null 2>&1) || RC=$?
  is "gh guard works when sourced from zsh" 0 "$RC"
fi
RC=0; (cd "$WORK" && bash -c "source '$REPO_ROOT/scripts/super-board-gh-guard.sh'; sb_gh_halt_check" >/dev/null 2>&1) || RC=$?
is "gh guard works when sourced from bash" 0 "$RC"

echo
echo "passed: $PASS  failed: $FAIL  known bugs (xfail): $KNOWN"
[ "$FAIL" -eq 0 ]
