#!/usr/bin/env bash
# Tests scripts/super-board-setup.py — onboard's 🔍 Checks, the upgrade path, config
# migration, board names, branch detection, and the GitHub board ranking / migration.
# Offline: `gh` is a stateful stub on PATH (board state in a JSON file, every call
# logged), install.sh comes from this checkout, helper skills are skipped.
#
#   bash tests/test-setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PACK="$PWD"
SETUP="$PACK/scripts/super-board-setup.py"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
fail() { echo "FAIL: $1" >&2; exit 1; }
export HOME="$WORK/home"; mkdir -p "$HOME"
export SUPER_BOARD_PACK="$PACK"
q() { jq -e "$@" >/dev/null; }

# 0 — the three version files agree (plugin.json, VERSION, skills/super-board/VERSION) and name this release.
V_PLUGIN=$(jq -r .version "$PACK/.claude-plugin/plugin.json"); V_PACK=$(tr -d '[:space:]' < "$PACK/VERSION"); V_SKILL=$(tr -d '[:space:]' < "$PACK/skills/super-board/VERSION")
[ "$V_PLUGIN" = "$V_PACK" ] && [ "$V_PACK" = "$V_SKILL" ] || fail "version files disagree: plugin.json=$V_PLUGIN VERSION=$V_PACK skills/super-board/VERSION=$V_SKILL"
[ "$V_PACK" = "3.1.3-ams.31" ] || fail "expected release 3.1.3-ams.31, got $V_PACK"

# 1 — a bare folder: check is red and lists what is missing; fix installs every
#     must-have with no question, runs git init, and the re-check is green.
T="$WORK/bare"; mkdir -p "$T"
OUT=$(python3 "$SETUP" check --root "$T" || true)
echo "$OUT" | q '.green == false and (.missing | index("skill super-board")) and .git == false' \
  || fail "bare folder should be red with super-board missing: $OUT"
OUT=$(python3 "$SETUP" fix --root "$T" --no-helpers || true)
echo "$OUT" | q '.fixed | index("super-board skills, scripts and board engine installed")' || fail "fix should install: $OUT"
echo "$OUT" | q '.fixed | index("git repo created (git init)")' || fail "fix should git init: $OUT"
echo "$OUT" | q '.missing == [] and .git == true and .upgraded == false' || fail "re-check after fix should have nothing missing: $OUT"
[ -x "$T/.claude/bin/super-board-setup.py" ] || fail "the setup script installs itself into .claude/bin"
TXT=$(python3 "$SETUP" check --root "$T" --text || true)
echo "$TXT" | grep -q "✓ super-board skills, scripts and board engine present" || fail "text report should list what is present: $TXT"

# 1b — missing safety helpers must trigger a repair even on a current installation.
for helper in super-board-approval.py super-board-github-read.py super-board-project-verifier.py super-board-lane-delegate.py; do
  rm "$T/.claude/bin/$helper"
  OUT=$(python3 "$SETUP" check --root "$T" || true)
  echo "$OUT" | q --arg helper "script $helper" '.missing | index($helper)' \
    || fail "missing $helper must be reported: $OUT"
  OUT=$(python3 "$SETUP" fix --root "$T" --no-helpers || true)
  [ -x "$T/.claude/bin/$helper" ] || fail "repair must restore $helper"
  echo "$OUT" | q '.missing == []' || fail "repair must leave no missing scripts: $OUT"
done

# 2 — upgrade: an older super-board (VERSION 1.8.2, removed skill folders, a qa-only
#     config with Skipped, old collect keys, telegram) is detected and upgraded with
#     no question, backed up first, and listed as "Upgraded for you".
T="$WORK/old"; mkdir -p "$T/.claude/skills/super-board" "$T/.claude/super-board/configs"
git -C "$T" init -q
echo "1.8.2" > "$T/.claude/skills/super-board/VERSION"
for d in super-refine cleanup-wt arch-loop; do mkdir -p "$T/.claude/skills/$d"; echo x > "$T/.claude/skills/$d/SKILL.md"; done
cat > "$T/.claude/super-board/configs/books.json" <<'JSON'
{"version":1,"variant":"qa-only","project":{"owner":"example-org","number":2},
 "columns":["Ready","QA","Review","Done","Blocked","Skipped"],
 "repo":{"path":".","remote":"https://github.com/example-org/books.git"},
 "collect":{"window_days":14,"errors":"auto","feedback_paths":["docs/feedback"],"lookback_runs":10},
 "refine":{"rounds":10,"qa_hook_rounds":3},"human_approves_merge":true,
 "notifications":{"channel":"telegram","chat_id":"auto","bot_identity":"example-org"}}
JSON
OUT=$(python3 "$SETUP" check --root "$T" || true)
echo "$OUT" | q '.upgrade == true and .from == "1.8.2" and .old_dirs == ["super-refine","cleanup-wt","arch-loop"]' \
  || fail "older install should be detected: $OUT"
OUT=$(python3 "$SETUP" fix --root "$T" --no-helpers || true)
echo "$OUT" | q '.upgraded == true and .from == "1.8.2"' || fail "fix should report the upgrade: $OUT"
echo "$OUT" | q '.fixed | any(test("^skills updated; added .*super-collect"))' || fail "upgrade should name the added skills: $OUT"
echo "$OUT" | q '.fixed | index("removed old folders: super-refine, cleanup-wt, arch-loop")' || fail "old folders line missing: $OUT"
echo "$OUT" | q '.fixed | any(test("^config moved to the new keys"))' || fail "config line missing: $OUT"
for d in super-refine cleanup-wt arch-loop; do [ ! -e "$T/.claude/skills/$d" ] || fail "$d should be removed"; done
B=$(echo "$OUT" | jq -r .backup)
[ -f "$T/$B/.claude/skills/arch-loop/SKILL.md" ] && [ -f "$T/$B/.claude/super-board/configs/books.json" ] \
  || fail "old folders and config must be backed up first (backup: $B)"
C="$T/.claude/super-board/configs/books.json"
q '(has("variant") | not) and ._qa_all == true' "$C" || fail "variant must go, qa-only remembered for the board: $(cat "$C")"
q '.columns == ["Backlog","Ready","Building","QA","Review","Blocked","Done"]' "$C" || fail "columns must be the v3 seven"
q '.collect.sources == ["sentry","github","prs","architecture"] and (.collect | has("lookback_runs") | not)' "$C" || fail "collect keys not migrated"
q '.merge_policy.default == "human" and .notifications.channel == "session" and (.refine | has("qa_hook_rounds") | not)' "$C" \
  || fail "merge rule / notifications / refine not migrated: $(cat "$C")"
[ ! -e "$T/.claude/super-board/upgrade.json" ] || fail "upgrade.json should be cleared once the upgrade is done"
TXT=$(python3 "$SETUP" check --root "$T" --text || true)
echo "$TXT" | grep -q "Upgraded for you" && fail "a finished upgrade must not be reported again"

# 3 — migrate-config is idempotent: a second pass changes nothing.
OUT=$(python3 "$SETUP" migrate-config "$C")
echo "$OUT" | q '.changes == [] and .written == false' || fail "second migration should be a no-op: $OUT"

# 4 — system tools are never installed silently: a missing one comes back in `needs`
#     with the exact command for this OS.
#     A curated PATH, not "/usr/bin:/bin": GitHub's ubuntu runners ship gh in /usr/bin.
NOBIN="$WORK/nobin"; mkdir -p "$NOBIN"
for t in git apt-get dnf; do p=$(command -v "$t" || true); [ -n "$p" ] && ln -sf "$p" "$NOBIN/$t"; done
OUT=$(PATH="$NOBIN" "$(command -v python3)" "$SETUP" check --root "$T" || true)
echo "$OUT" | q '.needs | map(.name) | index("gh")' || fail "missing gh should be in needs: $OUT"
case "$(uname)" in Darwin) echo "$OUT" | q '.needs[] | select(.name == "gh") | .command == "brew install gh"' || fail "mac command should be brew: $OUT" ;; esac
echo "$OUT" | q '.needs[] | select(.name == "node") | .command | test("skills@latest add mattpocock/skills")' \
  || fail "the node command should also add Matt Pocock's skills: $OUT"

# 5 — board names from package.json / README; an empty folder falls back to its name.
T="$WORK/ledgerly"; mkdir -p "$T"
echo '{"name":"ledgerly"}' > "$T/package.json"; printf '# Ledgerly — bookkeeping for freelancers\n' > "$T/README.md"
python3 "$SETUP" names --root "$T" | q '.names == ["Ledgerly board","Ledgerly build board"]' || fail "names from package.json/README wrong"
mkdir -p "$WORK/my-app"
python3 "$SETUP" names --root "$WORK/my-app" | q '.names[0] == "my-app board"' || fail "empty folder should use its name"

# 6 — branch: main + vercel.json, no staging → recommend creating staging from main.
T="$WORK/br"; mkdir -p "$T"; git -C "$T" init -q -b main; echo '{}' > "$T/vercel.json"
git -C "$T" add . && git -C "$T" -c user.email=t@t -c user.name=t commit -qm init
OUT=$(python3 "$SETUP" branch --root "$T")
echo "$OUT" | q '.create_staging == true and .deploy == "Vercel deploys main" and .create_command == "git push origin main:staging"' \
  || fail "no staging should recommend creating it: $OUT"
git -C "$T" branch staging
python3 "$SETUP" branch --root "$T" | q '.staging == "staging" and .create_staging == false' || fail "existing staging should be used"

# ── gh stub: state in $GH_STATE, calls in $GH_LOG ──────────────────────────────
mkdir -p "$WORK/bin"
cat > "$WORK/bin/gh" <<'PY'
#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
st = json.load(open(os.environ["GH_STATE"]))
open(os.environ["GH_LOG"], "a").write(" ".join(a) + "\n")
def save(): json.dump(st, open(os.environ["GH_STATE"], "w"))
def page(rows, cursor=None):
    start = int(cursor or 0); end = start + 100
    return {"nodes": rows[start:end], "totalCount": len(rows),
            "pageInfo": {"hasNextPage": end < len(rows), "endCursor": str(end)}}
def node(it):
    status = None if it.get("status") is None else {"name": it["status"], "optionId": next(o["id"] for o in st["options"]["2"] if o["name"] == it["status"])}
    n = it["content"]["number"]
    return {"id": it["id"], "updatedAt": it.get("updatedAt", "t0"), "type": "ISSUE", "status": status,
            "content": {"__typename": "Issue", "id": "ISS"+str(n), "number": n, "repository": {"nameWithOwner": "example-org/books"},
                        "labels": page([{"name": n} for n in it.get("labels", [])])}}
if a[:2] == ["project", "list"]:
    print(json.dumps({"projects": st["projects"]}))
elif a[:2] == ["project", "field-list"]:
    n = a[2]
    print(json.dumps({"fields": [{"id": "F" + n, "name": "Status", "type": "ProjectV2SingleSelectField",
                                  "options": [{"id": o["id"], "name": o["name"]} for o in st["options"].get(n, [])]}]}))
elif a[:2] == ["project", "view"]:
    print(json.dumps({"id": "P" + a[2], "url": "https://github.com/users/example-org/projects/" + a[2]}))
elif a[:2] == ["project", "item-list"]:
    items = st["items"]
    if st.get("hide_status"):  # a gh that does not surface the Status column
        items = [{k: v for k, v in it.items() if k != "status"} for it in items]
    else:  # like gh: no key on a card with no status
        items = [{k: v for k, v in it.items() if not (k == "status" and v is None)} for it in items]
    print(json.dumps({"items": items}))
elif a[:2] == ["project", "item-edit"]:
    item, opt = a[a.index("--id") + 1], a[a.index("--single-select-option-id") + 1]
    name = next(o["name"] for o in st["options"]["2"] if o["id"] == opt)
    for it in st["items"]:
        if it["id"] == item: it["status"], it["updatedAt"] = name, it.get("updatedAt", "t0") + "w"
    save()
elif a[:2] == ["api", "graphql"]:
    body = json.loads(sys.stdin.read())
    if body["query"].startswith("query"):
        query, v = body["query"], body["variables"]
        if "items(first:" in query:
            value = {"items": page([node(it) for it in st["items"]], v.get("after"))}
        elif "... on ProjectV2Item{" in query:
            value = node(next(it for it in st["items"] if it["id"] == v["id"]))
        elif "... on Issue{" in query:
            it = next(it for it in st["items"] if "ISS"+str(it["content"]["number"]) == v["id"])
            value = {"labels": page([{"name": n} for n in it.get("labels", [])], v.get("after"))}
        else:
            value = {"options": st["options"]["2"]}
        print(json.dumps({"data": {"node": value}}))
    else:
        st["gen"] = st.get("gen", 0) + 1
        new = [{"id": f"o{st['gen']}-{i}", "name": o["name"], "color": o["color"], "description": o["description"]}
               for i, o in enumerate(body["variables"]["opts"])]
        st["options"]["2"] = new
        names = {o["name"] for o in new}
        # Rewriting options can clear every card status; early scenarios only clear Ready.
        for it in st["items"]:
            if st.get("wipe_all") or it.get("status") == "Ready" or it.get("status") not in names: it["status"] = None
        save()
        print(json.dumps({"data": {"updateProjectV2Field": {"projectV2Field": {"options": [{"id": o["id"], "name": o["name"]} for o in new]}}}}))
elif a[0] == "api" and a[1].startswith("repos/"):
    print(json.dumps([[{"name": n} for n in st["labels"]]]))
elif a[:2] == ["label", "list"]:
    print(json.dumps([{"name": n} for n in st["labels"]]))
elif a[:2] == ["label", "create"]:
    st["labels"].append(a[2]); save()
elif a[:2] == ["issue", "edit"]:
    it = next(it for it in st["items"] if str(it["content"]["number"]) == a[2])
    labels = set(it.get("labels", []))
    if "--add-label" in a: labels.update(a[a.index("--add-label")+1].split(","))
    if "--remove-label" in a: labels.difference_update(a[a.index("--remove-label")+1].split(","))
    it["labels"] = sorted(labels); save()
else:
    sys.exit(f"unexpected gh call: {a}")
PY
chmod +x "$WORK/bin/gh"
export GH_STATE="$WORK/gh.json" GH_LOG="$WORK/gh.log"

# 7 — board-rank: open boards ranked by matching columns; the best is recommended only
#     with ≥ 4 of 7, and missing columns are named.
cat > "$GH_STATE" <<'JSON'
{"projects":[{"number":2,"title":"Old","url":"u2","items":{"totalCount":3}},
             {"number":4,"title":"Ledgerly Roadmap","url":"u4","items":{"totalCount":31}},
             {"number":7,"title":"Closed","url":"u7","closed":true,"items":{"totalCount":9}}],
 "options":{"2":[{"id":"a","name":"Todo"},{"id":"b","name":"Done"}],
            "4":[{"id":"1","name":"Backlog"},{"id":"2","name":"Ready"},{"id":"3","name":"Building"},
                 {"id":"4","name":"Review"},{"id":"5","name":"Blocked"},{"id":"6","name":"Done"}]},
 "items":[],"labels":[]}
JSON
OUT=$(PATH="$WORK/bin:$PATH" python3 "$SETUP" board-rank --owner example-org)
echo "$OUT" | q '.recommend == 4 and .boards[0].number == 4 and .boards[0].matches == 6 and .boards[0].missing == ["QA"]' \
  || fail "best board should be #4 with QA missing: $OUT"
echo "$OUT" | q '[.boards[].number] | index(7) | not' || fail "closed boards are not offered: $OUT"
jq '.projects = [.projects[0]]' "$GH_STATE" > "$WORK/s" && mv "$WORK/s" "$GH_STATE"
PATH="$WORK/bin:$PATH" python3 "$SETUP" board-rank --owner example-org | q '.recommend == null' \
  || fail "a board with 1 of 7 columns should not be recommended"

# 8 — board-migrate: adds missing columns, creates the three labels, maps old labels,
#     labels a qa-only board's cards qa, moves Skipped cards to Done, removes Skipped,
#     and restores every status the option rewrite cleared. No card is lost.
cat > "$GH_STATE" <<'JSON'
{"projects":[],"labels":["bug"],
 "options":{"2":[{"id":"r","name":"Ready","color":"BLUE","description":"go"},{"id":"q","name":"QA","color":"ORANGE","description":""},
                 {"id":"v","name":"Review","color":"PURPLE","description":""},{"id":"d","name":"Done","color":"GREEN","description":""},
                 {"id":"b","name":"Blocked","color":"RED","description":""},{"id":"s","name":"Skipped","color":"GRAY","description":""}]},
 "items":[{"id":"I1","status":"Skipped","content":{"type":"Issue","number":1}},
          {"id":"I2","status":"Ready","labels":["build"],"content":{"type":"Issue","number":2}},
          {"id":"I3","status":"Ready","content":{"type":"Issue","number":3}},
          {"id":"I4","status":"Review","labels":["qa"],"content":{"type":"Issue","number":4}}]}
JSON
: > "$GH_LOG"
CFG="$WORK/books.json"
echo '{"project":{"owner":"example-org","number":2},"repo":{"remote":"https://github.com/example-org/books.git"},"_qa_all":true}' > "$CFG"
OUT=$(PATH="$WORK/bin:$PATH" python3 "$SETUP" board-migrate --root "$WORK/board8" --config "$CFG")
echo "$OUT" | q '.added_columns == ["Backlog","Building"] and .labels_created == ["qa","feature"]' || fail "columns/labels wrong: $OUT"
echo "$OUT" | q '.labels_mapped == 1 and .qa_labelled == 1 and .skipped_moved == 1 and .skipped_removed == true' \
  || fail "mapping / qa-all / Skipped counts wrong: $OUT"
grep -q -- "issue edit 2 --repo example-org/books --add-label feature --remove-label build" "$GH_LOG" || fail "build should map to feature"
grep -q -- "issue edit 3 --repo example-org/books --add-label qa" "$GH_LOG" || fail "an unlabelled card on a qa-only board gets qa"
q '[.options["2"][].name] == ["Backlog","Ready","Building","QA","Review","Blocked","Done"]' "$GH_STATE" \
  || fail "options should be the v3 seven, Skipped gone: $(jq -c '.options["2"]' "$GH_STATE")"
q '[.items[] | {(.id): .status}] | add == {"I1":"Done","I2":"Ready","I3":"Ready","I4":"Review"}' "$GH_STATE" \
  || fail "every card must keep its column (Skipped → Done): $(jq -c .items "$GH_STATE")"
echo "$OUT" | q '.restored >= 2' || fail "cleared statuses must be restored: $OUT"
q '. == {"I1":"Skipped","I2":"Ready","I3":"Ready","I4":"Review"}' "$(echo "$OUT" | jq -r .status_backup)" \
  || fail "the pre-rewrite snapshot is saved to disk: $OUT"
q 'has("_qa_all") | not' "$CFG" || fail "the qa-all marker is dropped once applied"

# 9 — a brand-new board: --prune-empty swaps GitHub's default Todo / In Progress for the seven.
cat > "$GH_STATE" <<'JSON'
{"projects":[],"labels":["qa","bug","feature"],
 "options":{"2":[{"id":"t","name":"Todo","color":"GRAY","description":""},{"id":"p","name":"In Progress","color":"YELLOW","description":""},
                 {"id":"d","name":"Done","color":"GREEN","description":""}]},"items":[]}
JSON
OUT=$(PATH="$WORK/bin:$PATH" python3 "$SETUP" board-migrate --root "$WORK/board9" --owner example-org --number 2 --repo example-org/books --prune-empty)
q '[.options["2"][].name] == ["Backlog","Ready","Building","QA","Review","Blocked","Done"]' "$GH_STATE" \
  || fail "a new board should end with exactly the seven: $(jq -c '.options["2"]' "$GH_STATE")"
echo "$OUT" | q '.labels_created == []' || fail "existing labels are not created again: $OUT"

# 10 — --prune-empty on a live board that already has the seven: the option rewrite wipes
#      EVERY card's status (as GitHub does), and the restore must still run — it used to
#      run only for added columns or Skipped, so 114 cards fell to "No status".
cat > "$GH_STATE" <<'JSON'
{"projects":[],"labels":["qa","bug","feature"],"wipe_all":true,
 "options":{"2":[{"id":"k","name":"Backlog","color":"GRAY","description":""},{"id":"r","name":"Ready","color":"BLUE","description":""},
                 {"id":"u","name":"Building","color":"YELLOW","description":""},{"id":"q","name":"QA","color":"ORANGE","description":""},
                 {"id":"v","name":"Review","color":"PURPLE","description":""},{"id":"b","name":"Blocked","color":"RED","description":""},
                 {"id":"d","name":"Done","color":"GREEN","description":""},{"id":"t","name":"Todo","color":"GRAY","description":""}]},
 "items":[{"id":"I1","status":"Done","content":{"type":"Issue","number":1}},
          {"id":"I2","status":"Building","content":{"type":"Issue","number":2}},
          {"id":"I3","status":"Review","content":{"type":"Issue","number":3}},
          {"id":"I4","status":null,"content":{"type":"Issue","number":4}}]}
JSON
: > "$GH_LOG"
OUT=$(PATH="$WORK/bin:$PATH" python3 "$SETUP" board-migrate --owner example-org --number 2 --repo example-org/books --prune-empty --root "$WORK")
q '[.options["2"][].name] == ["Backlog","Ready","Building","QA","Review","Blocked","Done"]' "$GH_STATE" \
  || fail "the unused Todo should be pruned: $(jq -c '.options["2"]' "$GH_STATE")"
q '[.items[] | {(.id): .status}] | add == {"I1":"Done","I2":"Building","I3":"Review","I4":null}' "$GH_STATE" \
  || fail "prune-empty must put every wiped status back: $(jq -c .items "$GH_STATE")"
echo "$OUT" | q '.restored == 3' || fail "three cards should be restored: $OUT"
BK=$(echo "$OUT" | jq -r .status_backup)
case "$BK" in "$WORK/.claude/super-board/backup/board-2-"*.json) ;; *) fail "backup path wrong: $BK" ;; esac
q '. == {"I1":"Done","I2":"Building","I3":"Review","I4":null}' "$BK" || fail "backup must hold the pre-rewrite statuses: $(cat "$BK")"

# 11 — a gh that does not surface `status`: the snapshot falls back to GraphQL
#      fieldValueByName("Status"), so the restore still has something to restore.
jq '.items = [{"id":"I1","status":"Done","content":{"type":"Issue","number":1}},{"id":"I2","status":"Building","content":{"type":"Issue","number":2}}] | .hide_status = true
    | .options["2"] += [{"id":"t","name":"Todo","color":"GRAY","description":""}]' "$GH_STATE" > "$WORK/s" && mv "$WORK/s" "$GH_STATE"
OUT=$(PATH="$WORK/bin:$PATH" python3 "$SETUP" board-migrate --owner example-org --number 2 --repo example-org/books --prune-empty --root "$WORK")
q '[.items[] | {(.id): .status}] | add == {"I1":"Done","I2":"Building"}' "$GH_STATE" \
  || fail "statuses read over GraphQL must be restored: $(jq -c .items "$GH_STATE") $OUT"
echo "$OUT" | q '.restored == 2' || fail "two cards restored via the GraphQL snapshot: $OUT"

# 12 — --dry-run writes nothing: no option rewrite, no card move, no label, no backup file.
jq '.items = [{"id":"I1","status":"Skipped","content":{"type":"Issue","number":1}},
              {"id":"I2","status":"Ready","labels":["build"],"content":{"type":"Issue","number":2}}]
    | .hide_status = false | .labels = [] | del(.gen)
    | .options["2"] = [{"id":"r","name":"Ready","color":"BLUE","description":""},{"id":"s","name":"Skipped","color":"GRAY","description":""}]' \
  "$GH_STATE" > "$WORK/s" && mv "$WORK/s" "$GH_STATE"
cp "$GH_STATE" "$WORK/before.json"; : > "$GH_LOG"; rm -rf "$WORK/dry"; mkdir -p "$WORK/dry"
OUT=$(PATH="$WORK/bin:$PATH" python3 "$SETUP" board-migrate --owner example-org --number 2 --repo example-org/books --prune-empty --qa-all --dry-run --root "$WORK/dry")
cmp -s "$GH_STATE" "$WORK/before.json" || fail "dry-run changed board state: $(cat "$GH_STATE")"
! grep -Eq '^(project item-edit|label create|issue edit)' "$GH_LOG" || fail "dry-run made a write: $(cat "$GH_LOG")"
[ "$(grep -c '^api graphql' "$GH_LOG")" = 2 ] || fail "dry-run should only read options and the complete card snapshot over GraphQL: $(cat "$GH_LOG")"
[ ! -e "$WORK/dry/.claude" ] || fail "dry-run must not write a backup file"
echo "$OUT" | q '.status_backup == null and .restored == 0 and .added_columns != []' || fail "dry-run report wrong: $OUT"

# 13 — answers that follow from other answers (migrate-config derives them; no question in
#      non-interactive mode, the filled-in owner is printed as a change line).
#      merge_policy "human" => human_approves_merge true; a robot login with no trusted_approvers
#      => [repo owner] (an empty list holds every merge); never when the owner IS the robot.
D="$WORK/derive"; mkdir -p "$D"
CFG_JSON='{"version":3,"columns":["Backlog","Ready","Building","QA","Review","Blocked","Done"],"timezone":"UTC","worker_backend":"workflow",
 "project":{"owner":"example-org","number":1},"repo":{"path":".","remote":"https://github.com/example-org/ams.git"},
 "human_approves_merge":false,"merge_policy":{"default":"human","trusted_approvers":[]},
 "migrations":{"allowed_envs":[],"target_env":"staging"},
 "notifications":{"channel":"session","chat_id":null,"bot_identity":"ams-robot"}}'
echo "$CFG_JSON" > "$D/a.json"
OUT=$(python3 "$SETUP" migrate-config "$D/a.json" </dev/null)
echo "$OUT" | q '.changes | any(startswith("human_approves_merge: true"))' || fail "human policy should set human_approves_merge: $OUT"
echo "$OUT" | q '.changes | any(test("trusted_approvers: \\[\"example-org\"\\]"))' || fail "the owner default must be printed: $OUT"
q '.human_approves_merge == true and .merge_policy.trusted_approvers == ["example-org"] and .merge_policy.default == "human"' "$D/a.json" \
  || fail "derived answers not written: $(cat "$D/a.json")"
python3 "$SETUP" migrate-config "$D/a.json" </dev/null | q '.changes == [] and .written == false' || fail "derived answers must be idempotent"
echo "$CFG_JSON" | jq '.notifications.bot_identity = "example-org"' > "$D/b.json"
python3 "$SETUP" migrate-config "$D/b.json" </dev/null >/dev/null
q '.merge_policy.trusted_approvers == [] and .human_approves_merge == true' "$D/b.json" || fail "the owner is the robot: nothing to default (still holds, loudly), human flag still follows"
for ident in auto "super-board-bot[bot]" ""; do
  echo "$CFG_JSON" | jq --arg i "$ident" '.notifications.bot_identity = $i' > "$D/c.json"
  python3 "$SETUP" migrate-config "$D/c.json" </dev/null >/dev/null
  q '.merge_policy.trusted_approvers == []' "$D/c.json" || fail "no machine-user login ('$ident'): trusted_approvers stays empty"
done
echo "$CFG_JSON" | jq '.merge_policy.trusted_approvers = ["alice"]' > "$D/d.json"
python3 "$SETUP" migrate-config "$D/d.json" </dev/null >/dev/null
q '.merge_policy.trusted_approvers == ["alice"]' "$D/d.json" || fail "a configured trusted_approvers list is never overwritten"
echo "$CFG_JSON" > "$D/e.json"
python3 "$SETUP" migrate-config "$D/e.json" --approver Alice </dev/null >/dev/null
q '.merge_policy.trusted_approvers == ["Alice"]' "$D/e.json" || fail "--approver should override the owner default"
echo "$CFG_JSON" > "$D/f.json"
python3 "$SETUP" migrate-config "$D/f.json" --interactive </dev/null | q '.changes | any(test("example-org"))' || fail "--interactive without a terminal falls back to the owner default"
echo "$CFG_JSON" | jq '.merge_policy = {"default":"auto"} | .human_approves_merge = false | del(.notifications.bot_identity)' > "$D/g.json"
python3 "$SETUP" migrate-config "$D/g.json" </dev/null >/dev/null
q '.human_approves_merge == false and (.merge_policy | has("trusted_approvers") | not)' "$D/g.json" || fail "auto policy and no robot: nothing derived"

# 14 — the AGENTS.md Merge / Migrations rows follow merge_policy.default and migrations.allowed_envs.
T="$WORK/rows"; mkdir -p "$T"
python3 "$SETUP" fix --root "$T" --no-helpers >/dev/null || true
python3 "$T/.claude/bin/super-board-agents-md.py" block --root "$T" --create >/dev/null
cp "$T/AGENTS.md" "$WORK/agents.template.md"
grep -q 'auto for normal changes' "$T/AGENTS.md" && grep -q 'robot migrates allowed DBs only' "$T/AGENTS.md" || fail "the template rows are the default"
mkdir -p "$T/.claude/super-board/configs"
echo "$CFG_JSON" | jq '.human_approves_merge = true | .merge_policy.trusted_approvers = ["example-org"]' > "$T/.claude/super-board/configs/ams.json"
OUT=$(python3 "$SETUP" check --root "$T" || true)
echo "$OUT" | q '.agents_rows == ["Merge","Migrations"] and .green == false' || fail "rows out of line with the config should be red: $OUT"
TXT=$(python3 "$SETUP" check --root "$T" --text || true)
echo "$TXT" | grep -q 'AGENTS.md Merge row does not match the config' || fail "the text report should name the row: $TXT"
python3 "$SETUP" sync-agents-md --root "$T" --dry-run | q '.changed == ["Merge","Migrations"]' || fail "dry run should list the rows"
cmp -s "$T/AGENTS.md" "$WORK/agents.template.md" || fail "--dry-run must not write AGENTS.md"
OUT=$(python3 "$SETUP" fix --root "$T" --no-helpers || true)
echo "$OUT" | q '.fixed | any(startswith("AGENTS.md rows now follow the config: Merge, Migrations"))' || fail "fix should repair the rows: $OUT"
grep -q '^| Merge | every merge waits for the owner' "$T/AGENTS.md" || fail "human policy: Merge row must say every merge waits for the owner: $(grep '^| Merge' "$T/AGENTS.md")"
grep -q '^| Migrations | no robot migrations' "$T/AGENTS.md" || fail "allowed_envs []: Migrations row must say no robot migrations: $(grep '^| Migrations' "$T/AGENTS.md")"
[ "$(diff "$WORK/agents.template.md" "$T/AGENTS.md" | grep -c '^>')" = 2 ] || fail "only the two rows may change"
python3 "$SETUP" sync-agents-md --root "$T" | q '.changed == []' || fail "row sync must be idempotent"
bash "$PACK/install.sh" --no-hooks "$T" >/dev/null 2>&1 || fail "re-install failed"
grep -q '^| Merge | every merge waits for the owner' "$T/AGENTS.md" && grep -q '^| Migrations | no robot migrations' "$T/AGENTS.md" \
  || fail "install.sh refreshes the block from the template: it must put the config's rows back"
echo "$CFG_JSON" | jq '.human_approves_merge = false | .merge_policy = {"default":"auto","trusted_approvers":["example-org"]} | .migrations.allowed_envs = ["test"]' > "$T/.claude/super-board/configs/ams.json"
python3 "$SETUP" sync-agents-md --root "$T" | q '.changed == ["Merge","Migrations"]' || fail "going back to auto + a migratable DB should restore the template rows"
cmp -s "$T/AGENTS.md" "$WORK/agents.template.md" || fail "the template rows come back byte for byte"

# 15 — an organization owner is never filled in as the approver (an org cannot approve; a person must).
#      gh is a stub that answers `api users/<owner> --jq .type`.
ORGBIN="$WORK/orgbin"; mkdir -p "$ORGBIN"
printf '#!/bin/sh\n[ "$1" = api ] && { echo "${FAKE_OWNER_TYPE:-User}"; exit 0; }\nexit 1\n' > "$ORGBIN/gh"; chmod +x "$ORGBIN/gh"
echo "$CFG_JSON" > "$D/org.json"
OUT=$(PATH="$ORGBIN:$PATH" FAKE_OWNER_TYPE=Organization python3 "$SETUP" migrate-config "$D/org.json" </dev/null)
echo "$OUT" | q '.needs_approver == true and (.changes | any(startswith("merge_policy.trusted_approvers NOT filled")))' || fail "an org owner must require --approver: $OUT"
q '.merge_policy.trusted_approvers == []' "$D/org.json" || fail "the org must not be written as the approver: $(cat "$D/org.json")"
OUT=$(PATH="$ORGBIN:$PATH" FAKE_OWNER_TYPE=Organization python3 "$SETUP" migrate-config "$D/org.json" --approver Alice </dev/null)
echo "$OUT" | q '.needs_approver == false' || fail "--approver satisfies the requirement: $OUT"
q '.merge_policy.trusted_approvers == ["Alice"]' "$D/org.json" || fail "--approver Alice should be written"
echo "$CFG_JSON" > "$D/user.json"
PATH="$ORGBIN:$PATH" FAKE_OWNER_TYPE=User python3 "$SETUP" migrate-config "$D/user.json" </dev/null >/dev/null
q '.merge_policy.trusted_approvers == ["example-org"]' "$D/user.json" || fail "a personal owner is still filled in"

# 16 — a configured robot identity puts SB_GH_IDENTITY_GUARD=1 into settings env (check red, fix repairs,
#      idempotent, other keys kept); no robot identity, nothing written.
T="$WORK/guardenv"; mkdir -p "$T/.claude/super-board/configs"
python3 "$SETUP" fix --root "$T" --no-helpers >/dev/null || true
jq '.env = {"KEEP":"me"}' "$T/.claude/settings.json" > "$WORK/s.json" && mv "$WORK/s.json" "$T/.claude/settings.json"
echo "$CFG_JSON" | jq '.merge_policy.trusted_approvers = ["example-org"] | .human_approves_merge = true' > "$T/.claude/super-board/configs/ams.json"
OUT=$(python3 "$SETUP" check --root "$T" || true)
echo "$OUT" | q '.guard_env == true and .green == false' || fail "a robot identity without the settings env flag should be red: $OUT"
python3 "$SETUP" sync-guard-env --root "$T" --dry-run | q '.changed == true' || fail "dry run should report the change"
q '(.env | has("SB_GH_IDENTITY_GUARD") | not)' "$T/.claude/settings.json" || fail "--dry-run must not write settings"
OUT=$(python3 "$SETUP" fix --root "$T" --no-helpers || true)
echo "$OUT" | q '.fixed | any(startswith("settings.json env: SB_GH_IDENTITY_GUARD=1"))' || fail "fix should write the flag: $OUT"
q '.env.SB_GH_IDENTITY_GUARD == "1" and .env.KEEP == "me" and (.hooks | length > 0)' "$T/.claude/settings.json" || fail "flag written, other settings kept: $(cat "$T/.claude/settings.json")"
python3 "$SETUP" sync-guard-env --root "$T" | q '.changed == false' || fail "flag sync must be idempotent"
echo "$CFG_JSON" | jq '.notifications.bot_identity = "auto"' > "$T/.claude/super-board/configs/ams.json"
echo '{}' > "$T/.claude/settings.json"
python3 "$SETUP" sync-guard-env --root "$T" | q '.changed == false' || fail "no robot identity: nothing to write"
[ "$(jq -c . "$T/.claude/settings.json")" = "{}" ] || fail "settings must stay untouched without a robot identity"
echo "$CFG_JSON" > "$T/.claude/super-board/configs/ams.json"; echo '{ broken' > "$T/.claude/settings.json"
python3 "$SETUP" sync-guard-env --root "$T" | q '.changed == false' || fail "an unparseable settings.json is left alone"
[ "$(cat "$T/.claude/settings.json")" = "{ broken" ] || fail "unparseable settings.json must not be rewritten"

# 17 — board fields: Agent / Skills / Card type. check reports them red, fix and ensure-fields create
#      them (idempotent, correct options and colors), check then says the one manual view step.
#      A dedicated stub (state in $FS) so the earlier stub stays untouched.
FB="$WORK/fieldbin"; mkdir -p "$FB"
cat > "$FB/gh" <<'PY'
#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
FS = os.environ["FS_STATE"]
st = json.load(open(FS))
open(os.environ["FS_LOG"], "a").write(" ".join(a) + "\n")
def save(): json.dump(st, open(FS, "w"))
def fields():
    out = []
    for f in st["fields"]:
        row = {"id": f["id"], "name": f["name"], "type": f["type"]}
        if f["type"] == "ProjectV2SingleSelectField":
            row["options"] = [{"id": o["id"], "name": o["name"]} for o in f["options"]]
        out.append(row)
    return out
if a[:2] == ["project", "view"]:
    print(json.dumps({"id": "PVT_1", "url": "https://github.com/users/example-org/projects/" + a[2]}))
elif a[:2] == ["project", "field-list"]:
    print(json.dumps({"fields": fields()}))
elif a[:2] == ["project", "field-create"]:
    name, kind = a[a.index("--name") + 1], a[a.index("--data-type") + 1]
    assert kind == "TEXT", a
    st["fields"].append({"id": "F_" + name, "name": name, "type": "ProjectV2Field"}); save()
    print(json.dumps({"id": "F_" + name}))
elif a[:2] == ["api", "graphql"]:
    body = json.loads(sys.stdin.read()); q, v = body["query"], body["variables"]
    if "createProjectV2Field" in q:
        assert v["pid"] == "PVT_1"
        st["fields"].append({"id": "F_" + v["name"], "name": v["name"], "type": "ProjectV2SingleSelectField",
                             "options": [dict(o, id="o%d" % i) for i, o in enumerate(v["opts"])]}); save()
        print(json.dumps({"data": {"createProjectV2Field": {"projectV2Field": {"id": "F_" + v["name"], "name": v["name"]}}}}))
    elif "updateProjectV2Field" in q:
        f = next(f for f in st["fields"] if f["id"] == v["id"])
        st["gen"] = st.get("gen", 0) + 1
        f["options"] = [dict(o, id="n%d-%d" % (st["gen"], i)) for i, o in enumerate(v["opts"])]; save()
        print(json.dumps({"data": {"updateProjectV2Field": {"projectV2Field": {"options": []}}}}))
    else:
        f = next(f for f in st["fields"] if f["id"] == v["id"])
        print(json.dumps({"data": {"node": {"options": f["options"]}}}))
else:
    sys.exit("unexpected gh call: %s" % a)
PY
chmod +x "$FB/gh"
export FS_STATE="$WORK/fields.json" FS_LOG="$WORK/fields.log"
T="$WORK/fields"; mkdir -p "$T/.claude/super-board/configs"
python3 "$SETUP" fix --root "$T" --no-helpers >/dev/null || true
echo "$CFG_JSON" | jq '.project = {"owner":"example-org","number":3} | .notifications.bot_identity = "auto"' > "$T/.claude/super-board/configs/app.json"
echo '{"fields":[{"id":"F_Status","name":"Status","type":"ProjectV2SingleSelectField","options":[{"id":"s1","name":"Backlog","color":"GRAY","description":""}]}]}' > "$FS_STATE"; : > "$FS_LOG"
OUT=$(PATH="$FB:$PATH" python3 "$SETUP" check --root "$T" || true)
echo "$OUT" | q '.green == false and (.board_fields | to_entries[0].value.missing == ["Agent","Skills","Card type"]) and .manual_steps == []' \
  || fail "missing board fields must be red and name all three: $OUT"
TXT=$(PATH="$FB:$PATH" python3 "$SETUP" check --root "$T" --text || true)
echo "$TXT" | grep -q "✗ board fields missing on example-org/3: Agent, Skills, Card type" || fail "text check must list the missing fields in red: $TXT"
# a read-only check and a dry run write nothing
! grep -Eq 'field-create|createProjectV2Field|updateProjectV2Field' "$FS_LOG" || fail "check must not write: $(cat "$FS_LOG")"
OUT=$(PATH="$FB:$PATH" python3 "$SETUP" ensure-fields --config "$T/.claude/super-board/configs/app.json" --dry-run)
echo "$OUT" | q '.created == ["Agent","Skills","Card type"] and .dry_run == true' || fail "dry run should name what it would create: $OUT"
! grep -Eq 'field-create|createProjectV2Field' "$FS_LOG" || fail "--dry-run must not create fields: $(cat "$FS_LOG")"
OUT=$(PATH="$FB:$PATH" python3 "$SETUP" fix --root "$T" --no-helpers || true)
echo "$OUT" | q '.fixed | any(test("^board fields created on example-org/3: Agent, Skills, Card type"))' || fail "fix must create the fields: $OUT"
echo "$OUT" | q '.board_fields == {} and (.manual_steps | length == 1) and (.manual_steps[0] | test("View menu ▾ → Fields → tick Agent, Skills, Card type"))' \
  || fail "after fix: no board field left red, plus the one manual step: $OUT"
q '(.fields | map(.name)) == ["Status","Agent","Skills","Card type"]' "$FS_STATE" || fail "fields on the board: $(cat "$FS_STATE")"
# ams.19: a NEW Agent field gets the whole 32-name matrix; the original 8 keep their colors/descriptions.
q '(.fields[] | select(.name=="Agent") | .options) as $o
   | ($o | length) == 32
   and ([$o[] | .name] | index("Codex · super-lint") != null and index("Done · Haiku · super-qa") != null)
   and ([["Haiku · super-build","YELLOW","Build lane, Haiku"],["Sonnet · super-build","ORANGE","Build lane, Sonnet"],
         ["Opus · super-build","RED","Build lane, Opus"],["Haiku · super-qa","PINK","QA lane, Haiku"],
         ["Sonnet · super-qa","PURPLE","QA lane, Sonnet"],["Opus · super-review","BLUE","Review lane, Opus"],
         ["Sonnet · super-review","GRAY","Review lane, Sonnet"],["Done · Opus · super-review","GREEN","Finished by Review, Opus"]]
        - [$o[] | [.name,.color,.description]]) == []' "$FS_STATE"   || fail "Agent options/colors must be exact: $(jq -c '.fields[1]' "$FS_STATE")"
q '(.fields[] | select(.name=="Skills") | .type) == "ProjectV2Field"' "$FS_STATE" || fail "Skills is a text field"
q '(.fields[] | select(.name=="Card type") | .options | map(.name)) == ["feature","bug","docs","money","refactor","tests","ux","qa"]' "$FS_STATE" \
  || fail "Card type options"
# idempotent: a second run creates nothing and changes no state
cp "$FS_STATE" "$WORK/fields.before"; : > "$FS_LOG"
OUT=$(PATH="$FB:$PATH" python3 "$SETUP" ensure-fields --owner example-org --number 3)
echo "$OUT" | q '.created == [] and .options_missing == {} and .problems == []' || fail "second ensure-fields must be a no-op: $OUT"
cmp -s "$FS_STATE" "$WORK/fields.before" || fail "second ensure-fields changed the board"
! grep -Eq 'field-create|createProjectV2Field|updateProjectV2Field' "$FS_LOG" || fail "second run wrote: $(cat "$FS_LOG")"
# an existing field that lacks an option is reported red with the manual step and NEVER mutated: no
# updateProjectV2Field (it regenerates option ids and clears card values); cards keep their Agent / Card type values
jq '(.fields[] | select(.name=="Agent") | .options) |= map(select(.name != "Done · Opus · super-review")) | .items = [{"id":"I1","Agent":"o0","Card type":"o1"},{"id":"I2","Agent":"o2","Card type":"o3"}]' "$FS_STATE" > "$WORK/f2.json" && mv "$WORK/f2.json" "$FS_STATE"
cp "$FS_STATE" "$WORK/fields.before"; : > "$FS_LOG"
OUT=$(PATH="$FB:$PATH" python3 "$SETUP" check --root "$T" || true)
echo "$OUT" | q '.green == false and (.board_fields | to_entries[0].value.missing[0] | startswith("Agent (missing option(s): Done · Opus · super-review"))' || fail "a missing Agent option must be red: $OUT"
OUT=$(PATH="$FB:$PATH" python3 "$SETUP" ensure-fields --owner example-org --number 3)
echo "$OUT" | q '.created == [] and (.problems | any(. == "Agent: add option Done · Opus · super-review in the board UI: Agent field ▾ → Edit"))' || fail "the missing option must be a manual step: $OUT"
OUT=$(PATH="$FB:$PATH" python3 "$SETUP" fix --root "$T" --no-helpers || true)
echo "$OUT" | q '.notes | any(startswith("Agent: add option Done"))' || fail "fix must surface the manual step: $OUT"
! grep -Eq 'updateProjectV2Field|createProjectV2Field|field-create' "$FS_LOG" || fail "an existing field must never be mutated: $(cat "$FS_LOG")"
cmp -s "$FS_STATE" "$WORK/fields.before" || fail "ensure-fields/fix changed a board that only lacks an option"
q '.items == [{"id":"I1","Agent":"o0","Card type":"o1"},{"id":"I2","Agent":"o2","Card type":"o3"}]' "$FS_STATE" || fail "card values must survive"
# a field with the right name but the wrong type is reported, never touched
jq '(.fields[] | select(.name=="Skills")) |= (.type = "ProjectV2SingleSelectField" | .options = [])' "$FS_STATE" > "$WORK/f3.json" && mv "$WORK/f3.json" "$FS_STATE"
OUT=$(PATH="$FB:$PATH" python3 "$SETUP" ensure-fields --owner example-org --number 3)
echo "$OUT" | q '.problems | any(startswith("Skills exists with another type"))' || fail "wrong type must be a problem: $OUT"
# gh unreadable: not red, reported
printf '#!/bin/sh\necho boom >&2; exit 1\n' > "$WORK/gh-bad"; mkdir -p "$WORK/badbin"; mv "$WORK/gh-bad" "$WORK/badbin/gh"; chmod +x "$WORK/badbin/gh"
OUT=$(PATH="$WORK/badbin:$PATH" python3 "$SETUP" check --root "$T" || true)
echo "$OUT" | q '(.board_fields_unreadable | length) == 1 and .board_fields == {}' || fail "an unreadable board is reported, not guessed: $OUT"

# 18 — pr_rules (optional board config key): a well-formed value is accepted; every malformed shape is
#      reported by check (red, text names the file and the reason); fix never rewrites it.
T="$WORK/prr"; mkdir -p "$T/.claude/super-board/configs"
python3 "$SETUP" fix --root "$T" --no-helpers >/dev/null || true
PRC="$T/.claude/super-board/configs/app.json"
prr() {  # $1 pr_rules JSON ("absent" = no key)
  if [ "$1" = absent ]; then echo "$CFG_JSON" | jq 'del(.project) | del(.pr_rules)' > "$PRC"
  else echo "$CFG_JSON" | jq --argjson r "$1" 'del(.project) | .pr_rules = $r' > "$PRC"; fi
  python3 "$SETUP" check --root "$T" || true
}
GOOD='{"pre_pr_commands":["npm run lint","git diff --check"],"pr_body_notes":["When test counts change, add one line per test file: `Gate-Declare: <path> [passed|skipped|warnings] <+N|-N>`"]}'
for ok in absent "$GOOD" '{"pre_pr_commands":[]}' '{"pr_body_notes":["x"]}' '{}' null; do
  prr "$ok" | q '.pr_rules_errors == {}' || fail "pr_rules $ok must be accepted: $(prr "$ok" | jq -c .pr_rules_errors)"
done
for bad in '"npm test"' '["npm test"]' '{"pre_pr_commands":"npm test"}' '{"pre_pr_commands":[1]}' '{"pre_pr_commands":["ok"," "]}' \
           '{"pr_body_notes":[null]}' '{"pr_body_notes":[""]}' '{"pre_commands":["x"]}' '{"pre_pr_commands":["a\u0000b"]}'; do
  OUT=$(prr "$bad")
  echo "$OUT" | q '(.pr_rules_errors | length) == 1 and .green == false' || fail "pr_rules $bad must be red: $(echo "$OUT" | jq -c .pr_rules_errors)"
done
OUT=$(prr '{"pre_pr_commands":"npm test"}')
echo "$OUT" | q '.pr_rules_errors | to_entries[0].value == "pr_rules.pre_pr_commands must be an array of strings"' || fail "the reason must name the key: $OUT"
TXT=$(python3 "$SETUP" check --root "$T" --text || true)
echo "$TXT" | grep -q "✗ .claude/super-board/configs/app.json: pr_rules.pre_pr_commands must be an array of strings" || fail "text check names the file and reason: $TXT"
BEFORE=$(jq -c .pr_rules "$PRC"); python3 "$SETUP" fix --root "$T" --no-helpers >/dev/null || true
[ "$(jq -c .pr_rules "$PRC")" = "$BEFORE" ] || fail "fix must not rewrite pr_rules"
python3 "$SETUP" migrate-config "$PRC" --dry-run | q '.changes | all(test("pr_rules") | not)' || fail "migrate-config leaves pr_rules alone"

echo "PASS: test-setup.sh (18 scenarios)"
