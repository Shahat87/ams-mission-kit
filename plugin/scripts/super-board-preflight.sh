#!/usr/bin/env bash
# super-board-preflight.sh — before a card goes Ready → Building, is it worth building?
#
# Four questions, asked of every candidate card before any Builder starts:
#
#   1. Already done?        a merged PR (on the base branch) closes it, or a merged PR / closed issue carries
#                           the same fingerprint or (near-)same title.
#   2. Already in progress? an open PR from ANOTHER branch closes it or has the same
#                           title, or another card in Building/QA/Review (or a lower-
#                           numbered Ready peer in this wave) has the same title.
#   3. File overlap?        an open PR touches files this card names (a path with a slash, a bare
#                           file name with a known extension like kwd.py, or an extensionless
#                           Dockerfile / Makefile / Procfile / Gemfile / Rakefile / Jenkinsfile /
#                           Vagrantfile; dotfiles such as .env are not matched; identifiers like
#                           self.name and domains like example.com are not files). Two names meet if
#                           they are equal ignoring case, or one has no slash and equals the other's
#                           last segment (kwd.py meets src/kwd.py; src/a.py and lib/a.py do not). Or a lower-numbered
#                           card in the same --issues batch names them. Not a duplicate —
#                           a sequencing problem. The card goes behind the issue that
#                           PR closes (blocked-by, so the wave-start sweep frees it when
#                           that issue closes), or, if the PR closes nothing, proceeds
#                           with an expected-conflict note so the merge gate rebases. Two
#                           batch peers: the lower number proceeds, the other is sequenced
#                           `blockedBy: [<lower>]` — never both against each other.
#   4. Unclear?             no `## Acceptance Criteria` section with at least one bullet.
#
# Outcomes: proceed · hold (dup done / dup in progress / unclear) · sequence.
#
# THIS SCRIPT IS THE MECHANICAL HALF. It catches what string matching can catch and
# lists near-misses (`candidates`) for the pre-flight sub-agent, which makes the
# semantic call: "same feature, different words" and "AC contradicts the code" are
# judgements, not regexes. The agent may upgrade proceed → hold on evidence; it may
# downgrade a mechanical hold only by naming why the match is a different feature.
#
# Cheap by construction: the three list calls run ONCE per invocation however many
# issues are checked (pass --issues 12,14,15 to batch a wave), plus one `gh issue
# view` per issue. The worker gh guard runs first when it is installed alongside.
#
# Usage:
#   super-board-preflight.sh --repo <owner/name> --issues <N[,M…]>
#                            [--inflight <cards.json>]   # [{number,title,status}] board cards
#                            [--config <config.json>]    # base_branch: a merged PR counts only if its merge commit is on origin/<base>
#                            [--files a/b.ts,c/d.ts]     # paths the card will touch (else: parsed from body)
#                            [--threshold 0.6]           # title Jaccard for a duplicate
#
# Stdout, one object keyed by issue number:
#   { "14": { "number": 14, "title": "…", "verdict": "proceed|hold|sequence",
#             "tag": "done|in-progress|unclear|overlap|clean", "reason": "…",
#             "evidence": ["#31 merged: Resolves #14"], "blockedBy": [31],
#             "conflictWith": [52], "files": ["src/a.ts"], "candidates": [{"ref":"#40","kind":"open-pr","sim":0.4,"title":"…"}] } }
#
# Exit 0 ok · 64 usage · 79 required GitHub evidence unavailable (pause the run;
# a blind pre-flight never says proceed).
set -euo pipefail
# Fixed read commands: a malformed payload is GitHub's, so it halts (#32).
export SB_GITHUB_READ_STRICT=1

REPO=""; ISSUES=""; INFLIGHT=""; FILES=""; THRESHOLD="0.6"; CONFIG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo)      REPO="$2"; shift 2 ;;
    --issues|--issue) ISSUES="$2"; shift 2 ;;
    --inflight)  INFLIGHT="$2"; shift 2 ;;
    --files)     FILES="$2"; shift 2 ;;
    --config)    CONFIG="$2"; shift 2 ;;
    --threshold) THRESHOLD="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
[ -n "$REPO" ] && [ -n "$ISSUES" ] || { echo "usage: --repo <owner/name> --issues <N[,M…]>" >&2; exit 64; }
echo "$ISSUES" | grep -Eq '^[0-9]+(,[0-9]+)*$' || { echo "--issues must be numbers: $ISSUES" >&2; exit 64; }
# --files names one card's paths. On a batch one list would give every card the same files
# and sequence clean peers behind each other, so a batch parses each body instead.
case "$ISSUES" in *,*) [ -z "$FILES" ] || { echo "--files ignored: it applies to a single --issues only; paths are parsed from each body" >&2; FILES=""; } ;; esac

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [ -f "$HERE/super-board-gh-guard.sh" ]; then
  # shellcheck source=/dev/null
  . "$HERE/super-board-gh-guard.sh"
  sb_gh_guard_check 200 || exit $?
fi

MERGED=$(python3 "$HERE/super-board-github-read.py" --kind prs-merged -- pr list --repo "$REPO" --state merged --limit 100 --json number,title,body,url,baseRefName,mergeCommit ) || exit $?
OPEN=$(python3 "$HERE/super-board-github-read.py" --kind prs-open -- pr list --repo "$REPO" --state open --limit 100 --json number,title,body,url,headRefName,files ) || exit $?
CLOSED=$(python3 "$HERE/super-board-github-read.py" --kind issues-closed -- issue list --repo "$REPO" --state closed --limit 100 --json number,title,body,stateReason ) || exit $?
CARDS="[]"
[ -n "$INFLIGHT" ] && CARDS=$(cat "$INFLIGHT")

SB_TMP=$(mktemp -d); trap 'rm -rf "$SB_TMP"' EXIT
TARGETS="[]"
for n in ${ISSUES//,/ }; do
  one=$(python3 "$HERE/super-board-github-read.py" --kind issue -- issue view "$n" --repo "$REPO" --json number,title,body ) || exit $?
  # The accumulated array grows with the batch and a body can be large: files, never argv.
  printf '%s' "$TARGETS" > "$SB_TMP/acc.json"
  [ -n "$one" ] || { echo "empty issue read for #$n" >&2; exit 65; }
  TARGETS=$(printf '%s' "$one" | jq -c --slurpfile acc "$SB_TMP/acc.json" '$acc[0] + [.]') || exit $?
  [ -n "$TARGETS" ] || { echo "empty target list after issue #$n" >&2; exit 65; }
done

# A merged PR delivers a card only if it targeted the configured base branch and its merge commit is on
# origin/<base> (a PR merged elsewhere, or onto a branch that was later reset, delivered nothing here).
# Same ref convention as the merge gate (super-board-merge-gate.sh): `git fetch origin <base>` once, then
# origin/<base_branch>. A merge commit this clone does not have makes `--is-ancestor` exit 128: that PR is
# not delivered, and is named on stderr. A failed fetch warns and uses the existing origin/<base> (a stale ref
# still answers ancestry). Where the check cannot run (no --config, not a git repo, origin/<base> absent) the
# old behaviour holds, with one stderr warning: no halt.
ANC_BASE=""; ANC_WHY=""
if [ -z "$CONFIG" ]; then ANC_WHY="no --config"
elif [ ! -r "$CONFIG" ]; then echo "config not found: $CONFIG" >&2; exit 66
else
  ANC_BASE=$(jq -r '.base_branch // "main"' "$CONFIG" | tr -d '\r') || exit $?
  if ! git rev-parse --git-dir >/dev/null 2>&1; then ANC_WHY="not a git repository"
  else
    # Never prompt (credentials, ssh): this runs unattended inside a wave. Hang bound: see below.
    # Only GNU timeout: Windows ships C:/Windows/System32/timeout.exe, which rejects `timeout 60 cmd` ("Invalid
    # syntax", exit 1) and would stop the fetch from ever running. Without it the http low-speed limit and the
    # default ssh options below bound the fetch; an ssh transfer that stalls mid-stream under a caller's own
    # GIT_SSH_COMMAND, with no GNU timeout, can still hang (accepted: same as the merge gate's fetch).
    # A caller's non-empty GIT_SSH_COMMAND and GIT_ASKPASS are used unchanged (so its ssh options, not ours,
    # bound that case); the defaults apply when they are unset or empty.
    TMO=(); timeout --version 2>/dev/null | head -1 | grep -q '^timeout (GNU coreutils)' && TMO=(timeout 60)
    SSHC="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4}"
    if ! GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never GIT_ASKPASS="${GIT_ASKPASS:-true}" SSH_ASKPASS_REQUIRE=never GIT_SSH_COMMAND="$SSHC" ${TMO[@]+"${TMO[@]}"} git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 fetch --quiet origin "$ANC_BASE" </dev/null >/dev/null 2>&1; then
      echo "ancestry check: git fetch origin $ANC_BASE failed; using the existing origin/$ANC_BASE" >&2
    fi
    git rev-parse --verify -q "refs/remotes/origin/$ANC_BASE" >/dev/null 2>&1 || ANC_WHY="origin/$ANC_BASE not found"
  fi
fi
if [ -n "$ANC_WHY" ]; then
  echo "ancestry check skipped: $ANC_WHY" >&2
else
  printf '%s' "$MERGED" > "$SB_TMP/merged-raw.json"
  KEEP=""
  while IFS=$'\t' read -r num pbase oid; do
    [ "$pbase" = "$ANC_BASE" ] && [ "$oid" != "-" ] || continue
    RC=0; git merge-base --is-ancestor "$oid" "refs/remotes/origin/$ANC_BASE" 2>/dev/null || RC=$?
    if [ "$RC" -eq 0 ]; then KEEP="$KEEP $num"
    elif [ "$RC" -eq 128 ]; then echo "merged PR #$num: merge commit $oid not in the clone; not counted as delivered" >&2; fi
  done < <(jq -r '.[] | [.number, (.baseRefName // "-"), (.mergeCommit.oid // "-")] | @tsv' "$SB_TMP/merged-raw.json" | tr -d '\r')
  MERGED=$(jq -c --arg keep "$KEEP" '($keep | split(" ") | map(select(length > 0) | tonumber)) as $k | map(select(.number as $n | $k | index($n)))' "$SB_TMP/merged-raw.json") || exit $?
fi

printf '%s' "$TARGETS" > "$SB_TMP/targets.json"
printf '%s' "$MERGED"  > "$SB_TMP/merged.json"
printf '%s' "$OPEN"    > "$SB_TMP/open.json"
printf '%s' "$CLOSED"  > "$SB_TMP/closed.json"
printf '%s' "$CARDS"   > "$SB_TMP/cards.json"

# blockedBy keeps only issues that are still OPEN. A PR can stay open after the issue it closes was
# closed (not planned); sequencing a card behind a closed issue is swept free and re-held forever.
# The open issues come from ONE list read, never an `issue view` per closing ref: a ref that is a PR,
# or a deleted or transferred issue, makes `issue view` fail "Could not resolve to an Issue", which the
# read helper treats as an access error and halts the run on. In a list such a ref is just absent, and
# absent means not an open blocker. A failed list read still stops (exit from the helper), never guesses.
if printf '%s' "$OPEN" | jq -e 'any(.[]; (.body // "") | test("(?i)\\b(close[sd]?|fix(e[sd])?|resolve[sd]?)[ :]+#[0-9]")) ' >/dev/null; then
  OPEN_ISSUES=$(python3 "$HERE/super-board-github-read.py" --kind array -- issue list --repo "$REPO" --state open --limit 1000 --json number ) || exit $?
else
  OPEN_ISSUES="[]"
fi
printf '%s' "$OPEN_ISSUES" > "$SB_TMP/open-issues.json"

# GitHub payloads go to jq by file (--slurpfile binds an array, hence the [0] below), never
# as --argjson: 100 PRs with bodies overran the Windows command-line limit (exit 126).
# Kept as --argjson/--arg: $th (a number) and $files (a short path list).
jq -n --slurpfile targets_f "$SB_TMP/targets.json" --slurpfile merged_f "$SB_TMP/merged.json" \
      --slurpfile open_f "$SB_TMP/open.json" --slurpfile closed_f "$SB_TMP/closed.json" \
      --slurpfile cards_f "$SB_TMP/cards.json" --slurpfile openiss_f "$SB_TMP/open-issues.json" --arg files "$FILES" \
      --argjson th "$THRESHOLD" '
  def one($v; $w): if ($v | length) != 1 then error("\($w): expected exactly one JSON value") else $v[0] end;
  one($targets_f; "targets") as $targets | one($merged_f; "merged") as $merged | one($open_f; "open") as $open
  | one($closed_f; "closed") as $closed | one($cards_f; "cards") as $cards | one($openiss_f; "open issues") as $openiss | ([ $openiss[].number ]) as $openn
  | def stop: ["the","and","for","with","from","into","when","that","this","add","fix","make","use","should","not","are","can","new"];
  def toks: ascii_downcase | [scan("[a-z0-9]+")] | map(select(length > 2)) | . - stop | unique;
  def sim($a; $b): ($a | toks) as $x | ($b | toks) as $y
    | if ($x | length) == 0 or ($y | length) == 0 then 0
      else ((($x - ($x - $y)) | length) / (($x + $y) | unique | length) * 100 | floor) / 100 end;
  # GitHub closing keywords only: a PR that merely mentions #N is not delivering it.
  def closes($n): (.body // "") | test("(?i)\\b(close[sd]?|fix(e[sd])?|resolve[sd]?)[ :]+#" + ($n | tostring) + "\\b");
  def closed_refs: [ (.body // "") | scan("(?i)\\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[ :]+#([0-9]+)")[0] | tonumber ] | unique;
  def fps: [ (.body // "") | scan("(?m)^[ \t]*fingerprint:[ \t]*([^\n]*[^ \t\n])")[0] ];
  # Two names meet if equal ignoring case, or one has no slash and equals the last segment of the other.
  def meets($a; $b): ($a | ascii_downcase) as $x | ($b | ascii_downcase) as $y
    | $x == $y
      or (($x | contains("/") | not) and $x == ($y | split("/") | last))
      or (($y | contains("/") | not) and $y == ($x | split("/") | last));
  # A path with a slash, or a bare file name: a stem (1+ chars, starting with a letter, digit or _), a dot and an
  # allowlisted extension, on a boundary (so e.g., i.e., v1.2, 1.5, self.name and item.price are not files, and
  # tests/test_kwd.py is not also test_kwd.py); or an extensionless build file, case-sensitive and never inside a longer word,
  # with an optional directory prefix (~/ too) and suffix (deploy/Dockerfile, Dockerfile.prod, Dockerfile-dev); the bare
  # name is also reported, so Dockerfile/Makefile yields Dockerfile, Makefile and the pair. A bare name that is a JS
  # framework in brand casing (Node.js, Vue.js, D3.js, Chart.js; also Node.js-based) is not a file; every other .js
  # name is (App.js, app.js, node.js, chart.js, my-App.js). A miss lets cards collide, a false match delays one wave.
  def paths: [ (.body // "")
               | scan("[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\\.[A-Za-z0-9]+"),
                 scan("(?<![A-Za-z0-9_./-])(?![A-Za-z0-9_-]+(?:\\.[A-Za-z0-9_-]+)*\\.(?:co|com|org|net|gov|edu|ac)\\.(?:uk|au|jp|nz|za|br|in|kw|eg|sa|ae|tr|il|mx|cn)(?![A-Za-z0-9_]|\\.[A-Za-z]))(?!(?:Node|Vue|Next|Nuxt|Express|D3|Three|React|Angular|Ember|Backbone|Svelte|Solid|Deno|Chart)\\.js(?![A-Za-z0-9_/]|\\.[A-Za-z]))[A-Za-z0-9_][A-Za-z0-9_-]*(?:\\.[A-Za-z0-9_-]+)*\\.(?!(?:com|net|org|io|ai|dev|app|co)(?![A-Za-z]))(?i:py|js|jsx|ts|tsx|mjs|cjs|json|md|mdx|yml|yaml|toml|ini|cfg|sh|bash|ps1|html|htm|css|scss|sass|less|sql|go|rs|rb|java|kt|swift|c|h|cc|cpp|hpp|cs|php|vue|svelte|txt|csv|xml|lock|gradle)(?![A-Za-z0-9_/]|\\.[A-Za-z])"),
                 scan("(?<![A-Za-z0-9_./~-])(?:[A-Za-z0-9_.~-]+/)*(?:Dockerfile|Makefile|Procfile|Gemfile|Rakefile|Jenkinsfile|Vagrantfile)(?:[.-][A-Za-z0-9_-]+)*(?![A-Za-z0-9_/-]|\\.[A-Za-z0-9])"),
                 scan("(?<![A-Za-z0-9_.-])(?:Dockerfile|Makefile|Procfile|Gemfile|Rakefile|Jenkinsfile|Vagrantfile)(?:[.-][A-Za-z0-9_-]+)*(?![A-Za-z0-9_-]|\\.[A-Za-z0-9])") ] | unique;
  # The AC section: lines after the LAST "## Acceptance Criteria" heading, up to the next "## ".
  def has_acs: ((.body // "") / "\n") as $l
    | [ $l | to_entries[] | select(.value | test("^##[ \t]+Acceptance Criteria")) | .key ] as $h
    | if ($h | length) == 0 then false
      else [ $l[($h | last) + 1:][] ] as $t
        | ([ $t | to_entries[] | select(.value | test("^##[ \t]")) | .key ] | first) as $e
        | (if $e == null then $t else $t[:$e] end) | any(.[]; test("^[ \t]*[-*][ \t]+\\S")) end;

  [ $targets[] | . as $i | $i.number as $n
    | ($i | fps) as $myfp
    | (if $files == "" then ($i | paths) else ($files / "," | unique) end) as $mine
    # Own branch = this card coming back (rebuild, rebase); never its own duplicate.
    | [ $open[] | select((.headRefName // "") | startswith("issue-\($n)-") | not) ] as $others

    | ( [ $merged[] | select(closes($n)) | "PR #\(.number) merged and closes #\($n)" ]
      + [ $merged[] | select(($myfp | length) > 0 and (fps - (fps - $myfp) | length) > 0)
          | "PR #\(.number) merged with the same fingerprint" ]
      + [ $merged[] | select(sim(.title; $i.title) >= $th) | "PR #\(.number) merged: \"\(.title)\"" ]
      + [ $closed[] | select(.number != $n and .stateReason != "NOT_PLANNED")
          | select(sim(.title; $i.title) >= $th or (($myfp | length) > 0 and (fps - (fps - $myfp) | length) > 0))
          | "issue #\(.number) closed: \"\(.title)\"" ] ) as $done

    | ( [ $others[] | select(closes($n)) | "open PR #\(.number) (\(.headRefName)) already closes #\($n)" ]
      + [ $others[] | select(closes($n) | not) | select(sim(.title; $i.title) >= $th)
          | "open PR #\(.number) covers it: \"\(.title)\"" ]
      + [ $cards[] | select(.number != $n)
          | select(((.status as $s | ["Building","QA","Review"] | index($s)) != null)
                   or (.status == "Ready" and .number < $n))
          | select(sim(.title; $i.title) >= $th)
          | "card #\(.number) in \(.status) covers it: \"\(.title)\"" ] ) as $busy

    | [ $others[] | select(closes($n) | not) | select(sim(.title; $i.title) < $th)
        | . as $p | [ ($p.files // [])[] | .path ] as $pf
        | [ $mine[] | select(. as $m | any($pf[]; meets($m; .))) ] as $hit
        | select(($hit | length) > 0)
        | { pr: $p.number, issues: ($p | closed_refs), files: $hit } ] as $overlap

    | ( [ $merged[] | { ref: "#\(.number)", kind: "merged-pr", title, sim: sim(.title; $i.title) } ]
        + [ $others[] | { ref: "#\(.number)", kind: "open-pr", title, sim: sim(.title; $i.title) } ]
        + [ $closed[] | select(.number != $n) | { ref: "#\(.number)", kind: "closed-issue", title, sim: sim(.title; $i.title) } ]
        + [ $cards[] | select(.number != $n) | { ref: "#\(.number)", kind: "card:\(.status)", title, sim: sim(.title; $i.title) } ]
        | map(select(.sim >= 0.25 and .sim < $th)) | sort_by(-.sim) | .[:5] ) as $near

    | { number: $n, title: $i.title, candidates: $near, blockedBy: [], conflictWith: [], _files: $mine }
      + ( if ($done | length) > 0 then
            { verdict: "hold", tag: "done", reason: "already delivered", evidence: $done }
          elif ($busy | length) > 0 then
            { verdict: "hold", tag: "in-progress", reason: "already being built elsewhere", evidence: $busy }
          elif ($i | has_acs | not) then
            { verdict: "hold", tag: "unclear", reason: "no `## Acceptance Criteria` bullets", evidence: ["issue #\($n) body"] }
          elif ($overlap | length) > 0 then
            { verdict: "sequence", tag: "overlap",
              reason: "open PRs touch the same files — build after them",
              evidence: [ $overlap[] | "open PR #\(.pr) touches \(.files | join(", "))" ],
              blockedBy: ([ $overlap[].issues[] ] | unique - [$n] | map(select(. as $x | $openn | index($x) != null))),
              conflictWith: [ $overlap[].pr ] }
          else
            { verdict: "proceed", tag: "clean", reason: "no duplicate, no overlap", evidence: [] }
          end ) ]
  # A "sequence" with an empty blockedBy (every closing issue of the overlapping PRs is closed) builds
  # too, so it is a peer exactly like a "proceed": later cards sharing its files go behind it.
  # Peer overlap: two cards checked together name the same files and neither has an open
  # PR yet. The lower-numbered one goes first; the other waits behind it. Sequencing both
  # against each other parks both forever (#180/#181 on 2026-10-03, corpus.ts).
  | sort_by(.number)
  | reduce .[] as $c ({out: [], go: []};
      ([ .go[] | select(. as $g | any($c._files[]; . as $m | any($g.files[]; meets($m; .)))) ] | first) as $first
      | ($c.verdict == "proceed" or ($c.verdict == "sequence" and ($c.blockedBy | length) == 0)) as $builds
      | if $builds and $first != null then
          .out += [ $c + { verdict: "sequence", tag: "overlap",
                           reason: "a lower-numbered card in this check touches the same files — build after it",
                           evidence: [ "#\($first.number) touches \([ $c._files[] | select(. as $m | any($first.files[]; meets($m; .))) ] | join(", "))" ],
                           blockedBy: [ $first.number ] } ]
        elif $builds then
          .out += [ $c ] | .go += [ { number: $c.number, files: $c._files } ]
        else .out += [ $c ] end)
  | .out | map(. + { files: ._files } | del(._files))
  | INDEX(.number | tostring)'
