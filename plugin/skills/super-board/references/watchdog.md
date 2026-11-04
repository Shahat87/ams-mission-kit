# The external watchdog — full contract (#68)

> The watchdog is **outside the loop**: a scheduled, model-free checker that
> watches one board while nothing is watching it, and reports to the operator —
> never to GitHub. It is read-only: it performs no GitHub mutations, calls no
> model, and never stops, kills or restarts anything. Safe to run alongside a
> headless `super-board run` or on a machine nobody is sitting at.

---

## Why it exists

The run loop already reports blockers in-session. What it cannot report is the
case where the loop itself is the problem: a wave that died with the machine, a
card looping on the same root cause forever, a session that stalled between
waves. The watchdog runs from the OS scheduler — every `poll_minutes` minutes —
and toasts the operator when the board looks stuck. The run loop never has to
know it exists.

## Install

`install.sh` copies both scripts to `<repo>/.claude/bin/`. Then, once per repo
(with the config you onboarded):

```bash
bash .claude/bin/super-board-watchdog-install.sh \
  --config <repo>/.claude/super-board/configs/<slug>.json --print   # show the command
bash .claude/bin/super-board-watchdog-install.sh \
  --config <repo>/.claude/super-board/configs/<slug>.json --apply   # register the task
```

- `--print` (the default) prints the exact `schtasks.exe /Create …` line and
  runs nothing; audit it before applying. The printed form is cmd.exe-ready.
- `--apply` registers the task with `schtasks.exe` and refuses (exit 78) while
  `watchdog.enabled` is false in the config — scheduling is an operator
  decision, never a default.
- `--remove` prints the exact `schtasks.exe /Delete /TN super-board-watchdog-<slug> /F`
  line (never runs it — remove is one line you can run yourself).
- Exit 64: bad `--config` path/slug, missing jq/python/schtasks, or a repo,
  config or pinned-python path containing `'` or `"` (a quote would break out
  of the `bash -lc '…'` quoting and inject into the scheduled command).

The task runs, as the current user, roughly:

```
<bash.exe> -lc 'cd <repo> && <pinned python> -I .claude/bin/super-board-watchdog.py --config <cfg>'
```

using the interpreter pinned in `${USERPROFILE:-$HOME}/.mission-control/python`
(the same pin `install.sh` writes; falls back to `python3`/`python`, skipping
the Microsoft Store stub). One poll is the only mode — the schedule lives in
the task, not in the process. Never reads tokens, `.env` or `hosts.yml`, and
never runs `gh` outside the single board read.

## The poll, step by step

1. **Overlap lock** — `mkdir .claude/super-board/watchdog/<slug>/lock`. A lock
   younger than 2× `poll_minutes` means a poll is already running: exit 0
   silently. An older lock is stale and gets replaced. Removed when done.
2. **Local reads** — the wave lock
   (`.claude/super-board/inflight/workflow-wave.lock`: exists, mtime, `STARTED`),
   whether the shared GitHub halt file exists, `super-board-usage.sh check`,
   and today's run manifest (`docs/super-board/runs/<date>-<slug>.md`) mtime.
3. **Halt check** — if the halt file exists (resolved the same way the run loop
   resolves it), **zero GitHub calls**: the poll evaluates a halt only, and
   reports one `operator-queue` incident whose waits say `github-halt` —
   constant key, reported once while the halt persists, state not pruned
   during it. It never writes the halt file.
4. **The one GitHub read** — a single paginated GraphQL query (≤ `max_pages`
   pages, 50 cards each): viewer, rate limit, items with `updatedAt` + Status
   column, and per issue labels, assignees, last 8 comments, and the linked
   PR's last commit. Paginating stops early at the `gh_points_floor` (the page
   already read is still evaluated). Any failure → the poll goes **blind**.
5. **Trust filter, then deps** — comments by anyone outside
   `merge_policy.bot_identity` + `trusted_approvers` + the API viewer +
   `watchdog.trusted_authors` are dropped **before anything is parsed**; the
   filtered payload goes to `super-board-deps.sh --from` (fully offline — one
   subprocess, no GitHub, no shell, its stderr never echoed into the record
   because it can quote issue text). If deps fails, the poll goes blind too.

## Incidents

One line each in `.claude/super-board/watchdog/<slug>/incidents.jsonl`, one
best-effort toast (details below), then silence until the state changes.

| Kind | Fires when | Evidence |
|---|---|---|
| `card-stall` | A card in Ready/Building/QA/Review has no activity newer than `card_stall_minutes` (Blocked cards are never stall-checked — they wait on people). Ready is only checked **while the wave lock exists**; a parked board is not a stall (`no-run` instead). | column, stale-since, minutes |
| `card-loop` | Trusted comments repeat the same `root-cause-hash:` line more than `rebuild_cap` times in one lane. | hash, first-seen |
| `run-stall` | The wave lock exists but nothing lives: no lock mtime, no manifest mtime, no card activity anywhere on the board for `run_stall_minutes`. | lock STARTED, last life sign |
| `unknown-state` | A Blocked card `super-board-deps.sh` cannot parse confidently. | column |
| `operator-queue` | Nothing is stalled, looping or stalled-run — but every evaluated open card has a wait reason and **no lane is active**. One evaluated card without a wait reason (a fresh Ready card during a run, say) vetoes the report: the board is not purely waiting on you yet. | every waiting card + reason |
| `watchdog-blind` | The watchdog itself could not see the board (GitHub read failed or deps failed). Exactly one toast, never pruned while blind. | — |

**Wait reasons** (why a card sits where it is): `human-gated`,
`needs-you`, `depends-on:#N` (lowest-numbered open blocker), `no-run`
(Ready while no wave lock exists), `github-halt` (the whole board, while the
halt file exists), `usage-pause`. During an open usage pause **only the stall
checks** (`card-stall`, `run-stall`) are suppressed, until `resets_at` +
`run_stall_minutes` — the reset should clear it, and only a stall that outlives
that is real. `card-loop` and `unknown-state` are still evaluated while
paused: a card looping on the same root cause is a run defect, not a usage
problem.

**Keys.** Every incident is keyed by the first 12 hex chars of a sha256 over
the kind plus the fields that define its state. Same state → same key →
reported once. New activity on a stalled card, a new root-cause hash, a new
waits fingerprint → a new key → reported again. After a successful full read,
keys no longer present are pruned from `state.json` — going blind never prunes
state, so a flapping network cannot spam toasts.

**What counts as card activity:** the newest of the project item's `updatedAt`,
the newest **trusted** comment, and the linked PR's last commit. The issue's
own `updatedAt` is deliberately ignored — a passer-by can bump it with a
stranger comment and hide a stuck card.

## The toast

Template, exactly:

```
super-board <slug>: <kind> #<card> (<column>, <minutes>m)
```

`run-stall` and `operator-queue` have no card; `watchdog-blind` is bare:
`super-board <slug>: watchdog-blind`. The text travels only in the
`SB_WATCHDOG_TEXT` environment variable — never as an argument. The default
notifier is a PowerShell `ShowBalloonTip`; `SB_WATCHDOG_NOTIFY="<command>"`
overrides it (the override runs with `SB_WATCHDOG_TEXT` set; if the override
command itself does not run or exits nonzero, that toast is recorded as
`"notify": "failed"` in incidents.jsonl and **never retried**). Everything else about the board — titles, bodies, untrusted
comments — never leaves the process.

## State on disk

All under gitignored `.claude/super-board/watchdog/<slug>/`:

- `state.json` — `{version, slug, last_poll, last_ok, blind_since, reported{}}`
- `incidents.jsonl` — append-only incident records
- `deps-payload.json` — transient; written for deps.sh and deleted after
- `lock/` — the overlap lock (owner pid + start time)

## Tests

`python plugin/tests/test_watchdog.py` (20 cases: pure `evaluate()` rules,
trust filtering, pagination and rate-floor caps, halt, blind, pause,
concurrency, notify-retry, injection, the transient deps payload and the
github-halt report) and
`bash plugin/tests/test-watchdog-install.sh` (6 cases: printed command shape,
exit 64/78 paths, remove line, schtasks never invoked in print mode, quoted
paths refused).
