# Event triggers — one trusted event starts exactly one lane (issue #66)

A **trusted event** — a project-card move onto a routable column, or a comment whose first
line is exactly the configured command — starts **exactly one lane on one card** through the
normal wave. The wave runs with `singleLane: true`, so a Ready card runs build (or the folded
build+qa), a QA card runs qa, a Review card runs review — and stops there. The board remains
the loop state: the next event (or the next ordinary wave) continues the card.

Nothing here replaces the ordinary wave. It is an additional, deterministic entry point for
"do this one thing now" moments that do not justify a poll-driven full wave.

## What runs where

```
.claude/bin/super-board-events.py --config .claude/super-board/configs/<slug>.json poll
.claude/bin/super-board-events.py --config .claude/super-board/configs/<slug>.json next
.claude/bin/super-board-events.py --config .claude/super-board/configs/<slug>.json claim <id>
.claude/bin/super-board-events.py --config .claude/super-board/configs/<slug>.json done <id> --result <text>
.claude/bin/super-board-events.py --config .claude/super-board/configs/<slug>.json fail <id> --reason <text>
.claude/bin/super-board-events.py --config .claude/super-board/configs/<slug>.json status
```

The script is stdlib-only. It never runs `gh` write verbs, `claude`, or `node`, and never
stores a comment body. Every GitHub read goes through `.claude/bin/super-board-github-read.py`
with `SB_GITHUB_READ_STRICT=1`; at most **4 calls per tick** (board + issue comments + PR
review comments + one batched correlation query). Any read failure exits **79** and leaves the
queue untouched (the run-level halt file is honored, like every other reader).

Exit codes: `0` ok · `3` nothing dispatchable (also: events disabled, over budget) · `5`
refused (unknown id, no wave lock, untrusted actor, over budget at claim) · `64` usage/config
error · `79` a GitHub read halted.

## Trust — all three, every time

1. **Offline, at poll (enqueue) time**: the actor is in `events.initiators`
   (case-insensitive), is not `notifications.bot_identity` (which must never be `"auto"` —
   auto disables events entirely and `next` exits 3 with `events disabled: bot_identity`),
   and is not a `[bot]` login. These are cheap name checks that need no API call, so they run
   when the event is enqueued: an untrusted actor's event is dropped with
   `reason: untrusted-actor` and is **never queued**.
2. **Live, at claim time — the freshest moment before any start**: the actor holds admin,
   maintain or write permission on the repo
   (`gh api repos/<owner>/<repo>/collaborators/<actor>/permission`, read-only; `role_name`
   counts too — GitHub reports `maintain` as `permission: write, role_name: maintain`).
   A queued event whose actor fails this live check never starts a lane: `claim` exits 5,
   the event moves to `dropped/` with the live permission in the reason, and **no start is
   recorded** in the budget ledger. If the live read itself fails (a GitHub outage), the
   claim halts with exit 79 and the event stays queued — nothing starts without it.
3. **Not the bot, at either moment**: the bot checks above are re-checked at claim, so an
   event queued before a config change still cannot start a lane for the bot.

An event failing any check is recorded in `dropped/` with the reason and never retried.

## Routing — the column picks the lane

| Event | Card column | Lane |
| --- | --- | --- |
| `ready_move` (move by an initiator on this project) | Ready | `build` |
| `ready_move` | Ready + `qa` label | `qa` |
| `ready_move` | QA | `qa` |
| `ready_move` | Review | `review` |
| `issue_comment` / `pr_comment` / `review_comment` (first line exactly `events.command`) | Ready | `build` (Ready+[qa] → `qa`) |
| same | QA | `qa` |
| same | Review | `review` |
| anything else (Building, Done, Blocked, no column) | — | dropped: not a lane start |

Correlation: a PR comment links to the card through the PR's **linked issues** — the project
item's PR on the board carries `closingIssuesReferences` in the board read; a PR that is not a
board item is resolved with one batched query (up to 8 per tick). Exactly **one** linked issue
on the project is required: zero or two → dropped.

The command match is exact: the comment's **first line** must equal `events.command`
byte-for-byte (CRLF tolerated). A quote (`> /super-board go`), different case, trailing
whitespace, or a match anywhere else in the body never triggers.

## Guardrails

- **Bounce guard**: while the wave lock (`workflow-wave.lock`) is held or the card has an
  inflight lock (`inflight/issue-<number>`), `poll` **drops** new events (decided, not
  retried) — a wave is already running.
- **One queued event per card**: the newest supersedes older queued ones (the loser lands in
  `dropped/` with `superseded by …`).
- **Dedup**: every decided event is recorded in `seen.jsonl` as `<source>:<node_id>` and never
  processed twice.
- **Watermark**: `poll` reads comments with a `since` cursor overlapping the last tick by
  120 s, so a tick boundary cannot swallow a comment. The cursor lives in `cursor.json`.
- **Budget**: at most `events.max_starts_per_hour` (3) starts in the last hour and
  `events.max_starts_per_day` (10) in the last day, counted from `starts.jsonl` (one line per
  claim). A missing or unreadable ledger blocks dispatch — fail closed. Over budget, `next`
  exits 3 and reports once per event (`budget_reported`).
- **TTL / orphans**: a queued event older than `events.queue_ttl_hours` (24 h) expires. A
  claim older than that is an **orphan**: it moves to `failed/` with
  `orphaned claim (never re-run)` and its start is not counted twice.
- **Locks**: `claim` refuses without the wave lock. Take it first:
  `bash .claude/bin/super-board-orch.sh lock-acquire <slug>`, and release it with
  `lock-release <slug> <started>` when the lane is done (the watchdog's zombie rules in
  `references/run.md` apply).

## State (gitignored)

```
.claude/super-board/events/<slug>/
├─ cursor.json      poll watermark + last poll + last error
├─ seen.jsonl       decided event keys, pruned after 7 days
├─ queue/           decided, dispatchable events (sha1-named JSON, one per card)
├─ claimed/         events dispatched into a lane
├─ done/  failed/  expired/  dropped/    terminal folders
└─ starts.jsonl     the budget ledger (one line per claim)
```

An event file carries identities and routing only — `id, key, source, actor,
actor_permission, issue, pr, lane, card_status, node_id, url, created_at, enqueued_at, state,
reason, budget_reported` — never a comment body.

## Dispatch flow (the session, when `next` returns an event)

1. `poll` (scheduled or manual) fills the queue.
2. `next` prints the oldest dispatchable event's JSON (or exits 3).
3. The session takes the wave lock (`super-board-orch.sh lock-acquire <slug>`).
4. `claim <id>` re-checks trust (live permission), records the start, moves the event to
   `claimed/`.
5. The session runs the wave **once, with `singleLane: true`** for this one card
   (`run-workflow.md` → "Run the wave"). No classify/preflight shortcuts are skipped — the
   wave's own guards apply because it is the normal wave.
6. On success: `done <id> --result "<lane>: <column>"`. On a refused/failed lane:
   `fail <id> --reason "<detail>"`. Release the wave lock.

If the session dies between 4 and 6, the orphan sweep files the claim under `failed/` after
the TTL — the event is never re-run; a fresh event (new comment or move) restarts the work.

## Enabling

Events are off unless the config has an `events` block with `enabled: true`
(`config-schema.json` → `events`), `repo.remote` parses to owner/name, and
`notifications.bot_identity` is a real account (never `auto`). Each source can be switched
off with `events.sources.<source>: false` (`ready_move`, `issue_comment`, `pr_comment`,
`review_comment`): a candidate from a disabled source is dropped at poll with
`reason: source-disabled` and is never queued. The orchestrator (or a
scheduled task) calls `poll` every `events.poll_seconds`; a poll is a no-op while events are
disabled. Onboard adds the state path to `.gitignore` (see `references/onboard.md`).
