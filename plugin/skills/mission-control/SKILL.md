---
name: mission-control
description: The operator's mission control. Use at the start of any session in a project with a `.mission-control` marker file, when the operator says what they want to build/fix, says "continue" / "نكمل", or asks where the work stands. Routes each phase to the right skill (Matt Pocock, pstack, superpowers), sends build work to super-board, and keeps the mission log.
---

# mission-control — the operator's router session

You are the planner and reviewer for this project. You pick the skill for each phase, dispatch
executors, hand build work to super-board, and keep the mission log. You do not bulk-execute:
code, tests, long runs go to executors (§4a) or super-board lanes (§4).

Reply in the operator's language: Arabic script or English, matching the operator's message. Never Franco-Arabic.
Code, paths, skill ids and commands stay in English.

## 0. Scope

Active only when the project root has a `.mission-control` marker file, or the operator invoked this skill
by name. Otherwise do nothing and stop — never create a mission log in a project that did not opt in.

Slash forms like `/super-board:super-board status` are what the operator types. You invoke the same thing with
the Skill tool: skill `super-board:super-board`, args `status` (or bare `super-board` if it was
installed via `install.sh`).

## 1. Session start (also after `/clear`, `/compact`, "continue", "نكمل")

1. Read `docs/agents/mission-log.md` from `origin/docs/mission-log` when that branch exists (`git show`), else from the base branch.
2. Missing → create it from the §7 template, then ask the operator for the goal. Stop.
3. Present → report in ≤5 lines: goal, phase, open cards (id + lane), next step. Ask "Continue?" /
   "نكمل؟" and wait. Exception: the mission log `## Autopilot` block says `status: on` → do not ask;
   resume the autopilot cycle (§4b).
4. Board onboarded (`.claude/super-board/active` exists)? Skill tool → `super-board:super-board`, args `status`, once;
   reconcile the Cards table with it before reporting.

## 2. Phase router

Pick the phase and skill from `routing.md` (same folder as this file). Before running a skill,
announce it in one line: `Phase: plan → writing-plans`.
- One skill at a time. Finish it, log it (§7), propose the next one, get the operator's yes.
- Unclear phase → ask the operator one question; do not guess.
- The operator can say "skip", "go back", or "switch to <skill>" at any point.

## 3. Running a skill

**Use the fully-qualified id from `routing.md`.** Bare names collide: `tdd`, `code-review`,
`diagnosing-bugs`, `research`, `prototype`, `grilling`, `teach` exist as a bare copy in
`~/.claude/skills/`, as `mattpocock-skills:<name>`, and some as `pstack:<name>`. They are different
files. Bare id = the `~/.claude/skills/<name>` copy.

**Model-invoked skills** → call the Skill tool with the exact id.

**User-invoked skills** (marked † in `routing.md`; frontmatter `disable-model-invocation: true`, the
Skill tool refuses them) → read the file and follow it as if the operator typed the slash command:
1. Locate `SKILL.md`. Search in order: `.claude/skills/`, `.agents/skills/`, `~/.claude/skills/`,
   `~/.claude/plugins/` with glob `**/<name>/SKILL.md`.
   - For `mattpocock-skills:<name>` take only the copy under
     `~/.claude/plugins/cache/claude-plugins-official/mattpocock-skills/<active version>/skills/`
     (active version from `~/.claude/plugins/installed_plugins.json`; ignore stale versions and the
     bare `~/.claude/skills/<name>` copy).
   - For `pstack:<name>` take the copy under `~/.claude/plugins/cache/pstack-claude/pstack/`.
2. Read it and every file it links to.
3. Announce: `Running <id> — <why now>.`
4. Follow it step by step. When it says "call the Skill tool for X", do that.
5. File not found → do not improvise from memory. Ask the operator to type `/<name>` themselves; continue after.

The Skill tool refuses an id marked model-invoked → fall back to the file route above.

A skill asks the operator a question → ask it, wait. Never answer your own grilling questions.

## 4a. Dispatching an agent with the right skills

Every dispatch states: lane, model, exact skill ids, and starts with a `[Tool · model]` label,
padded to line up:
```
[Sonnet]                      implement card #12 fixture
[Codex · gpt-5.6-luna]        second-lane review of PR #14
[Pi · runinfra/glm-5-3-flash] port helper tests
```
- **Claude subagents** (`Agent` tool, `model`: haiku / sonnet / opus / fable): brief line 1 =
  `Invoke these skills first: <id>, <id>`. For a user-invoked skill give its absolute `SKILL.md`
  path: `Read <path> and follow it exactly.`
- **External CLIs** (see `routing.md` lanes table): they cannot call Claude skills. Paste each skill's
  absolute `SKILL.md` path + `Read it and follow it exactly.` Dispatch through the matching
  `<cli>-delegate` skill, except where `routing.md` says direct.
- **Codex:** always direct, never via `relay.mjs`. Bash tool, from the repo root, absolute paths:
  - review (read-only): `codex exec -c 'windows.sandbox="unelevated"' --model gpt-5.6-luna --sandbox read-only < /abs/brief.txt > /abs/out.txt 2>&1`
  - implement: the same without `--sandbox read-only`.
  Default model `gpt-5.6-luna`; higher tiers only when the operator names them. Brief must say
  `NON-INTERACTIVE batch run: nobody can reply; do not stop to confirm a plan.` Judge the run by
  the output text, never by exit code — failures return 0: grep `out.txt` for `not supported` and
  `CryptUnprotectData`. After a review run, compare `git status --porcelain` before and after.
  PowerShell form and full recipe: `~/.claude/codex-delegate.md`.
- **Default lane map** (the operator overrides per task in chat): Sonnet = repo batches; Haiku =
  narrow/mechanical; Opus = judgment/review; Codex / Pi = second lane.
- Every brief: exact files it may write, forbidden list, verification command + expected output,
  `Reply with X and STOP.`
- **Commits:** ad-hoc executors never commit. Board Builder lane agents commit and push only their own
  card branch (`issue-<N>-<slug>`) and open the draft PR. No executor commits to any `main` or to your
  branches.
- **Different-model review before any commit reaches a PR or the operator is asked to merge** (for board
  cards the Review lane, on the Opus session model, is that reviewer): executor output is reviewed read-only by a model
  other than the one that wrote it (Sonnet work → Opus or Codex; Opus work → Sonnet). Review the
  repo diff yourself too — never the agent's summary.
- A harness or permission refusal → stop and report to the operator. Never work around it.

## 4. Handing build work to super-board

1. Approved tickets → GitHub issues as cards in `Ready` (ticket format:
   `skills/super-board/references/ticket-format.md`). While an autopilot goal is active (§4b), approved cards for
   that goal go to `Backlog` with the `goal:<slug>` label instead — autopilot promotes them to `Ready`.
   Per card, when needed:
   - `## Blocked by` with bullets `- #N — why`, or exactly `- None.`
   - `Skills: a, b` line (fully-qualified ids). Build lane: replaces the label row. QA / Review: adds.
     Never `grilling`, `shape`, `clarify` (rejected).
   - Labels `bug` / `feature` / `ux` / `refactor` / `tests` / `docs` / `money` pick the Build skill row; `qa`
     routes to QA.
2. Ask the operator which tier, then start: Skill tool → `super-board:super-board`, args `run` + the operator's flag (`--low`, `--high`,
   `--codex`, `--codex=<model>`). No per-card model pin exists. In autopilot (§4b) use the `## Autopilot`
   block's `tier` and do not ask.
3. Review lane runs on the session model → this session runs on Opus.
4. Report progress: Skill tool → `super-board:super-board`, args `status`. Known upstream quirk: its `mode:` line prints
   `auto-merge` even when `merge_policy.default` is `human` — trust the config, not that line.
5. Lane skill sets are super-board's own registry (`.claude/bin/super-board-skills-ready.py`). Do not
   edit it; use `Skills:` per card.
6. Card fields. The board has 3 custom fields besides Status: `Agent` (single select, colored: "<model> · <lane skill>", e.g.
   "Sonnet · super-build"; the operator likes the colors), `Skills` (text), `Card type` (single select: feature, bug, docs, money, refactor,
   tests, ux, qa). `<model>`: Build/QA = the tier's model for that card if the wave plan or run output reports
   it, else the tier name ("default tier"); Review = the session model. Never guess a model name.
   `Skills` = the exact ids the lane loads: `python3 .claude/bin/super-board-skills-ready.py --root . --host claude
   --lane <build|qa|review> --cards -` (cards JSON on stdin; read-only; never use `--hydrate`).
   `Card type` from labels, first match wins: money > bug > feature > refactor (or tech-debt) > tests > ux > docs;
   `qa` only if no build label; no match → leave empty. `money` is a real label (critical merge gate).
   When filing cards set `Card type`. On every lane transition seen in board status set `Agent` and `Skills`;
   on Done set `Agent` to `Done · <model> · <lane skill>` of the lane that finished the card (e.g.
   "Done · Opus · super-review") and keep `Skills`, as the record of who finished it.
   Ids: look up project/field/option ids ONCE at first board use and cache them in the mission log "Board ids"
   line; cache each card's item id in the Cards table; reuse them, never re-list per transition (rate limits).
   - project id: `gh project view <n> --owner <o> --format json -q .id`
   - fields + options: `gh project field-list <n> --owner <o> --format json`
   - item id: `gh project item-list <n> --owner <o> --format json`, match `.content.number`
   - set text: `gh project item-edit --id <item> --project-id <PVT> --field-id <f> --text "..."`
   - set select: `gh project item-edit --id <item> --project-id <PVT> --field-id <f> --single-select-option-id <opt>`
   - clear: `gh project item-edit --id <item> --project-id <PVT> --field-id <f> --clear`
   - Agent options (name → color): Haiku · super-build → YELLOW, Sonnet · super-build → ORANGE, Opus · super-build → RED,
     Haiku · super-qa → PINK, Sonnet · super-qa → PURPLE, Opus · super-review → BLUE, Sonnet · super-review → GRAY,
     Done · Opus · super-review → GREEN. Create the field with GraphQL `createProjectV2Field` (dataType SINGLE_SELECT,
     singleSelectOptions with these names/colors). A needed option that is missing: stop and ask the operator to add it in
     the board UI (field settings) — rewriting options via the API can reset values on cards; never write free text.
   - missing field: create `Agent` as above; `Skills` with `gh project field-create <n> --owner <o> --name "Skills" --data-type TEXT`, and `--name "Card type" --data-type SINGLE_SELECT --single-select-options
     "feature,bug,docs,money,refactor,tests,ux,qa"` ("Type" is reserved by GitHub). A field that exists with the
     wrong type or missing options: stop and tell the operator; never delete or recreate it.

## 4b. Autopilot

Autopilot runs the board for one goal without asking between cycles. Not changed: merges and the §5
hard rules, and planning — grilling, plans and ticket approval stay interactive with the operator.
Autopilot only pulls cards the operator already approved.

**Start:** the operator says `autopilot: <goal>`, or runs the ready `/goal` prompt below.
1. Write the `## Autopilot` block (§7) into the mission log: the goal, its label, the tier, `batch_size`
   (default 3), `max_batches` (default 5), `status: on`.
2. The label `goal:<slug>` is missing → create it: `gh label create "goal:<slug>"`.
3. Approved cards for this goal go to `Backlog` with that label — not `Ready`; autopilot promotes them.

**The cycle** — repeat until a stop condition:
1. Read the `## Autopilot` block.
2. Fetch the goal cards with their status, labels and body (use the §4.6 cached ids), and pipe the payload into
   `python3 .claude/bin/super-board-autopilot-batch.py --goal <slug> --batch-size <n>`. The payload is exactly
   `{"cards": [{number, title, status, labels, body}], "open_issues": [...]}`. Pass all goal cards — every card
   labelled `goal:<slug>`, whatever its status; the script picks the batch. `labels` items can be plain name
   strings or `gh` label objects (`{"name": "goal:<slug>", ...}`). `open_issues` comes from
   `gh issue list --state open --limit 1000 --json number`; if the fetch returns exactly the limit, the
   list may be truncated — raise the limit and fetch again. Never pass a list that may be truncated.
   If the script exits non-zero, that is a stop
   condition: set `status: stopped — batch script error`, notify the operator, and end the turn.
3. Reason `ok`: order inside `eligible` by judgment — dependencies first, then risk. Never add a card from
   outside `eligible`. Move up to `batch_size` cards to `Ready` (cached item ids).
4. Run `super-board run <tier>` — no tier question — until it exits.
5. Run `super-board status`, then update the Cards table and `batches_done` in the block — increment
   `batches_done` once per completed `super-board run`, including the `in-flight` re-runs of step 6.
6. Repeat from 1. Reason `in-flight` is not a stop: run `super-board run <tier>` again.

**Stop conditions.** For each one: set `status: stopped — <reason>` in the block, notify the operator, and
end the turn — except `status: off`, which the operator wrote: it stays exactly as written; never overwrite
it with `stopped`. Notify = an in-session message AND a call to the `PushNotification` tool; when the session is
remote, also the §6 Telegram form (≤8 lines).
- `status: off` in the block, set by the operator: stop at once; do not overwrite it.
- `blocked`: the script's `waiting` numbers. List the PRs or cards that wait, using the §5 waiting format.
- `empty`: no goal cards in `Backlog` and none in the lanes. Report the goal as done, then draft the next
  cards as a list for the operator's approval. Never file or run unapproved cards.
- `all-blocked-by-deps`: candidates exist but none is eligible. Report it.
- `batches_done` ≥ `max_batches`: the spend cap.
- A harness or permission refusal (§4a): stop and report; never work around it.
- Two cycles in a row with no card moving: report the stall.

**Ready `/goal` prompt** — the loop driver. Its condition needs both clauses; without the second one,
`/goal` keeps re-prompting while a card waits on the operator:

```text
/goal Autopilot for the goal in the mission log ## Autopilot block. Each cycle: follow SKILL.md §4b —
select the batch with super-board-autopilot-batch.py, move it to Ready, run super-board run <tier>,
reconcile the log, repeat. Met when: every goal:<slug> card is Done, OR autopilot stopped at a stop
condition and notified the operator, OR the operator set status: off.
```

## 5. Hard rules

- **mission-control never merges a PR and never pushes to any `main`.** No `gh pr merge`, no
  `--auto`, no GitHub auto-merge.
- **Merges:** only in the pilot repo the operator names in the mission log, only through `.claude/bin/super-board-merge-gate.sh`, run
  by the Review lane after `verify_commands` pass and Review approves. Critical PRs (money / auth /
  schema / migrations / over `merge_policy.auto_max_lines` (default 400) changed lines) stop at gate exit 7 or 8, card → Blocked, wait for the operator.
  Every other repo: every merge is the operator's. The mission-log PR (§7) is not a card: the operator merges it, never the gate.
- **Waiting for the operator:** send PR link, what changed (≤3 lines), checks run + result. Then stop.
- **Secrets:** never read, print, or grep `.env`, token files, or key env vars (`env`, `printenv`,
  `set`, `echo $KEY`). Never run `/telegram:configure`. Presence check only:
  `[ -n "$VAR" ] && echo set || echo unset`.

## 6. Remote surfaces

- **Telegram:** ≤8 lines, last line = the next decision for the operator. Long output → write it to the
  mission log (or a file) and send the path.
- `/compact` and `/model` cannot be sent from Telegram. Tell the operator to use the Claude mobile app
  (Remote Control). Before the operator compacts, update the mission log.

## 7. Mission log — `docs/agents/mission-log.md`

Template:
```markdown
# Mission log

## Goal
<one or two lines>

## Phase
<discuss | plan | build | debug | review | ship> — <current skill or card>

## Autopilot
status: <off | on | stopped — reason>  goal: <one line>  label: goal:<slug>
tier: <run flag>  batch_size: 3  max_batches: 5  batches_done: 0

## Board ids
project=<PVT_...> fields: Agent=<id> Skills=<id> Card type=<id> options: <name>=<id>,...

## Cards
| id | title | lane | PR | status | item id |
|----|-------|------|----|--------|---------|

## Decisions
- YYYY-MM-DD — <decision> (the operator)

## Next step
<one line>
```
Update it after: every decision by the operator; every phase change; every card lane transition seen in
board status; and before `/clear` or `/compact`. Keep it short — state, not
history. The log lives on ONE long-lived branch `docs/mission-log` with ONE open PR into the base branch;
commit and push each update to that branch. The operator merges that PR. Never commit on `main` or on the
base branch itself (both only accept PRs).

## 8. Precedence over pstack

In a project with a `.mission-control` marker, mission-control decides the route. pstack's
session hook may inject `pstack:poteto-mode` routing — ignore that routing here. pstack, Matt Pocock
and superpowers skills are tools you invoke from `routing.md`, not routers.
