# super-board run — workflow backend contract

The DEFAULT backend (v1.6.0+): used when the active config sets
`"worker_backend": "workflow"` or omits the key. The legacy bash dispatcher
(`.claude/bin/super-board-run.sh`, see `run.md`) runs only on explicit
`"worker_backend": "claude-p"`; this file ONLY changes who dispatches
workers. Lane lifecycles, branch/PR model, comment cadence, Block
templates, halt gates, and done conditions are all inherited from `run.md`
unchanged.

## Codex runs

`/super-board run --codex` uses headless Codex for every lane of every wave,
overriding `worker_backend`. Use it when Claude's remaining usage is low or to try another model.
`--codex --low` / `--codex --high` pick the Codex ladder the same way the tier flags
pick Claude's (flag, else config `model_tier`, else medium); pass it as `--tier`.
A cheap router (`gpt-6-luna`) grades each Ready card low/medium/high, and the lane
launcher's `CODEX_LADDERS` table maps tier × grade to `-m <model>`; a card entering past
Ready takes its tier's medium cell. `--codex=<model>`, else optional `codex.model`,
pins one model for every card and skips the router.
Keep this run choice in each wave and resume command; skip only the Claude usage guard.

**Inside Codex, plain `run` is `--codex`.** Your tool list has no Workflow tool, or
`bash .claude/bin/super-board-host.sh` prints `codex` → run as `--codex` (tier flags and
`--codex=<model>` still apply); it counts as explicit `--codex`, Review included.
Say so once, in the first wave report: `🔁 codex host — running as --codex`. Never improvise a Claude workflow wave in Codex.

After the same plan and claim steps, launch this command as a background task:
`bash .claude/bin/super-board-codex-wave.sh --config <config-path> --cards <claimed-cards.json> --codex[=<model>] --tier <low|medium|high> --output <wave-result.json>`
(`<claimed-cards.json>` is the file written at Claim with the Write tool under
`.claude/super-board/inflight/` or the scratchpad — see precondition 0 for the fence's writable paths).
The helper starts one `super-board-codex-lane.sh` per card/lane, cards in parallel,
each advancing Build → QA → Review only after a successful handoff (QA labels skip Build).
Concurrency uses positive `max_workers`, otherwise `max(1, min(16, cpus - 2))`.
Pre-flight/classification and truth-check run inline; there are no Claude sub-agents.
Each lane emits `{status,column,detail}`; the wave returns the same `cards` summary
as the workflow, so reconcile, manifest, board/comments and throttle steps stay the same.
Review is allowed only with explicit `--codex`; merges still use the unchanged
merge gate with `--expect-head <reviewed-SHA>` and verify landing per `run.md`.
Concurrent reviews share that gate's merge mutex. Network and repo/git-dir writes
use the lane launcher's sandbox settings, including an external submodule git dir.

## Required GitHub evidence and a paused run

Required reads use `.claude/bin/super-board-github-read.py --kind <shape> -- <gh args>`.
Three total attempts means the first attempt plus two retries of that same read;
unrelated successful calls do not reset its failures. Invalid GraphQL queries,
authentication, and permission errors stop immediately. Do not retry mutations.
A payload that misses an agent-facing `--kind` shape (`READ_SHAPES` in
`workflows/super-board-wave.js`) exits 64 for that call only, printing the valid
form; it never halts the run. Fixed-command scripts set `SB_GITHUB_READ_STRICT=1`.

Exit **79**, a workflow result with `halted: true`, or a failed `--check` ends this
run. Check the helper's `--check` before claims, board/comment writes, each new
lane/wave, and all migration/merge steps. The marker is shared through the main
checkout's `.claude/super-board/github-halt.json`, including linked worktrees.
Cancel active workflow agents using local workflow controls (Codex: stop the background wave task); do not launch more
lanes. Already-running agents stop at their next checkpoint; this is not an
instantaneous process-wide kill. Preserve worktrees, claims, card statuses, and
approval evidence. Do not move cards to Done/Blocked or release claims using an
unavailable GitHub API. Report the local saved reason and what remains in flight.

After fixing access or the invalid query, explicitly resume with the helper's
`--resume`. It rechecks the failed read before clearing the marker and archives
the original reason. Failed recovery leaves the pause intact. When the caller's
query was corrected or PR metadata changed, supply its current read explicitly:
`--resume --kind <same-shape> [--meta <fresh-meta.json>] -- <corrected gh read args>`
(write `<fresh-meta.json>` with the Write tool under `.claude/super-board/inflight/` or the
scratchpad — see precondition 0).
The replacement must pass the same evidence validator; the broken saved query
is retained in the archived record. This is a read-only recovery check. An uncertain write
has no automatic recovery probe: inspect the remote outcome first, then archive
the halt record explicitly; never rerun a migration/write as a health check. Then follow normal
run preflight/reconcile, inspect preserved worktrees, and start a fresh wave.
Do not schedule automatic retries of the stopped run.

## Orchestrator delegation contract (NON-NEGOTIABLE, adapted)

The interactive session that runs this backend is the orchestrator. It:
- polls the board, claims assignees, launches workflow waves, reconciles
  results, posts notifications, and reports to the user between waves;
- does NOT do product work, patch lane skills mid-run, or hold per-card
  build context. Workflow lane agents or explicitly selected Codex workers do all product work.
- does NOT hold, filter or pre-judge Ready cards on their content (safety, scope, clarity).
  Every free Ready card goes to the wave, where Builder pre-flight and the Builder decide. A
  refusal ends in **Blocked** with the Block template, never left in Ready. Only pre-flight
  `skipped` (pre-flight blind or no verdict) and `halted` cards stay in Ready; the summary reports them.

## Preconditions (before the first wave)

Run the same preconditions as `run.md` §Preconditions, minus PID checks: (on a same-session resume the fence is already up, so `gh auth` validity is shown by the step-1 `gh api graphql` rate read, not `gh auth status`)
0. Orchestrator-guard install gate — before taking the lock, verify ALL THREE of these with the Read
   tool and `jq -r` on `.claude/settings.json` (both forms work even when the fence is already up in
   a same-session resume: in a fresh run the guard is not active yet, and while it enforces the
   settings.json `jq -r` probe is allowlisted read-only), and halt
   with a clear report if any fails:
   - the hook file exists: Read `.claude/hooks/guard-orchestrator.py`;
   - settings.json wires it: `jq -r '.hooks' .claude/settings.json` prints a command that contains
     `guard-orchestrator.py` (install.sh copies the helper to `.claude/bin/` and merges the wiring);
   - the escape hatch is not set: `jq -r '.env.SB_ORCH_GUARD_OFF // "unset"' .claude/settings.json`
     does not print `1`.
   A `--no-hooks` or partially wired install must not start a run: without the fence the
   orchestrator would do lane work with nothing stopping it. Halt: "🛑 orchestrator guard not
   installed (<what is missing>) — run ./install.sh, then restart the run."
   While the wave lock is held, the guard denies the orchestrator every file edit and every Bash
   command that is not one simple allowlisted call, and a denial is final: the message ends "do not
   retry in another form." — never re-express a denied step as a compound command, your own script,
   or a PowerShell call; use the helper subcommand (5a, 5b, 5, 7, lock release below) or wait for
   the lock. `gh api` (REST and GraphQL, including writes) is outside the fence; the fence targets
   product edits and builds, not GitHub calls. The fence leaves the orchestrator exactly three
   writable places for its own run files: the Write/Edit tools may create or update a file under
   `.claude/super-board/inflight/`, under the payload's `scratchpad_dir` when the payload sets one,
   or under `docs/super-board/runs/` (the run manifest, pause and halt notes included) — every
   other path stays denied, a path with `..` is denied after normalization, and NotebookEdit is
   denied everywhere. Lane agents carry an `agent_id` and are exempt; `SB_ORCH_GUARD_OFF=1` in settings
   `env` is the owner's escape hatch, never the run's. The hook binds this session to the run when
   it allows its `lock-acquire`, and `lock-release` deletes that binding file with the lock; after
   a crashed run a leftover binding with no lock blocks a new bind (the denial says so) — one bare
   `bash .claude/bin/super-board-orch.sh lock-release` clears it, then lock-acquire again.
1. Config exists and validates against `config-schema.json`. Run
   `python3 .claude/bin/super-board-verification-ready.py --config <config>`; exit 9
   stops before dispatch until the owning project's verification commands are configured.
   Preserve work; repair setup and restart explicitly, never bounce to Build.
2. Production-merge guard: refuse `base_branch: main` + `human_approves_merge:
   false` when deploy markers exist (same rule as super-board-run.sh).
3. Worktree check: if legacy `.worktrees/issue-*` exists, halt for inspection/move
   as described in run.md. Scan only board worktrees under `.claude/worktrees/`;
   when their named branch is gone use `git worktree remove` without force. Preserve
   detached, dirty, locked, unregistered, and unrelated folders.
4. For Claude waves, the wave script parses (catches a broken script before burning tokens):
   `bash .claude/bin/super-board-orch.sh check-wave` — it wraps `node --check` over a copy of
   `.claude/workflows/super-board-wave.js` and fails closed when it does not parse. With `--codex`,
   run it as `bash .claude/bin/super-board-orch.sh check-wave --codex`: it requires `codex` on PATH
   and `bash -n` on both Codex launcher scripts.
5. Wave marker FIRST, then the legacy check (lock-before-look closes the
   TOCTOU window where both backends pass each other's checks at once):
   a. Atomically create `.claude/super-board/inflight/workflow-wave.lock`:
      `bash .claude/bin/super-board-orch.sh lock-acquire <slug>` (exit 3 when the lock already
      exists; it records the config slug and a UTC start time, and the orchestrator guard binds
      this session to the run at that moment). Before replacing an existing marker, check
      `/workflows` and verify the PID in `.claude/super-board/codex-wave.pid` (plain numeric PID)
      with `kill -0` plus its process command. Only if neither backend has an active wave and no
      repo-local Codex lane remains is the marker stale from a crashed run — replace it: read the
      stale lock's SLUG and STARTED lines (the Read tool), then
      `bash .claude/bin/super-board-orch.sh lock-release <that-slug> <that-started>` and
      lock-acquire again (the release also deletes the crashed run's `orchestrator-session`
      binding, so the new acquire binds this session). Remove the lock when the run ends or stops
      (`lock-release` with this run's own values; it deletes the session binding with it). The
      legacy dispatcher refuses to start (and halts mid-run) while it exists.
   b. THEN verify no legacy run is active (the lock is held, so this runs through the helper):
      `bash .claude/bin/super-board-orch.sh check-wave` re-checks the wave script AND fails when a
      legacy `super-board-run.sh` dispatcher (it idles between dispatches with zero workers alive)
      or a headless `claude -p .*super-board run` is alive, when `/workflows` shows a running
      super-board-wave, when a repo-local `super-board-codex-wave.sh` / `super-board-codex-lane.sh`
      process remains, or when the codex-wave pid is still live. If it fails with a legacy run
      detected, `lock-release` the lock just created and stop — the legacy run won.
6. Crash-recovery sweep (the workflow backend's equivalent of the legacy
   reaper): with no wave running, strip `notifications.bot_identity` (from the config) from any
   Review/QA/Ready/Building card that still carries it
   (`gh issue edit <n> --remove-assignee <notifications.bot_identity>`). A crashed
   orchestrator releases nothing — leaked assignees make the planner skip
   those cards forever and the board silently stops draining.
7. Database reach: for every env in `migrations.allowed_envs` with a command, run its read-only
   check through the helper (the lock is held; the raw `migrations.checks[env]` command — e.g.
   `supabase migration list --db-url …` — is not allowlisted, and its URL resolves from the
   project's `.env`, never the session):
   `bash .claude/bin/super-board-orch.sh db-check <env>` (add `--config <config-path>` when the run
   is not on the active config). A failure halts the run with the check's error — a broken URL
   caught here costs nothing; caught at merge time it strands a wave of reviewed PRs. An env with
   a command and no configured check: db-check says so once, then go on.

## The wave loop

Repeat until a done condition or halt gate fires:

1. **Rate guard** — the fenced orchestrator reads the quota itself:
   `gh api graphql -f query='query{rateLimit{remaining}}'` (`gh api rate_limit` misreports GraphQL);
   if GraphQL remaining < 200, wait for reset (same thresholds as run.md). `sb_gh_guard_check`
   (super-board-gh-guard.sh) is for the dispatcher and lanes only — it is not an orchestrator
   command. A failing `gh` call outranks the reading:
   right after a reset the REST reading has shown 4999 while calls still failed.
   With GraphQL dry, the orchestrator's own writes go over REST, a separate
   quota: `gh api repos/<o>/<r>/issues/<n>/comments -f body=…` for a comment,
   `gh api -X DELETE repos/<o>/<r>/issues/<n>/assignees -f 'assignees[]=<bot>'`
   to release a claim. Board moves have no REST route; they wait for the reset.
   **Usage guard** (Claude waves only; skip for `--codex`, explicit or host-switched) — `bash .claude/bin/super-board-usage.sh check --config <config-path>`
   (Claude 5-hour and weekly plan usage; threshold `usage_pause_pct`, default 95):
   - exit 0 → launch.
   - exit 10 with `usage_fallback: "codex"` (or `"opencode"` / `"pi"`, run as
     `super-board-codex-lane.sh --tool <tool>`; Build/QA only, they never review) → resolve the selected remaining
     chain for `--host codex` before fallback claims; missing helpers halt with
     `skills-readiness:` and preserve state. Build/QA may use
     `super-board-codex-lane.sh` WITHOUT `--codex`; Review remains refused.
     Fallback never grants merge ownership; use explicit `run --codex` for all lanes.
     Plan as usual, take the `build` and `qa` cards (`review` cards wait), and for ONE
     card at a time claim it, run `bash .claude/bin/super-board-codex-lane.sh --config
     <config-path> --card '<card JSON>' --lane <build|qa>`, read its JSON exit like a lane
     result, then reconcile (step 5). Re-run the usage check between cards; on exit 0
     return to normal waves. The report line says `🔁 codex fallback`. Without
     `usage_fallback: "codex"` the launcher refuses (exit 78) unless `--codex` is given.
     `opencode` / `pi` also need `usage_fallback_unsandboxed: true` (exit 78 otherwise):
     they have no filesystem sandbox, so their tool calls can write anywhere the user
     can; only the working directory confines them.
     Exit 77 from a delegate lane means that tool is unavailable (binary missing, a
     credit/quota/auth error at startup, or a fallback delegate exiting 0 with no output
     before doing any work): it is out for the rest of the run. Run that lane on the ladder's Claude model instead and write
     "`<tool>` unavailable (<reason>), ran on <model>" in the card's lane comment.
   - exit 10 otherwise (pause) → launch nothing new. A wave already running finishes —
     stopping it mid-lane loses work. Then release claims as usual, post the
     resume note (one line to the user and, with Edit, to the run manifest under
     `docs/super-board/runs/` — an allowed orchestrator path:
     `⏸ paused at <window> <used>% — resets <local time>; remaining: <N> cards; resume: /super-board run <slug>`),
     remove the wave lock, and wait for the reset. If a wake tool is available
     (`ScheduleWakeup`, or a `/loop` re-entry), schedule
     `min(3600, resets_at - now + 60)` seconds and chain until the reset; on
     wake re-run the check — trust the reading, not the clock — and continue
     only on exit 0. No wake tool → stop with the note; the user resumes.
   - exit 3 (unknown) → launch, and say once per run why the guard is blind
     (its `reason`). Do not halt a run on a missing signal.

   The signal is the `rate_limits` block Claude Code pipes to the status line —
   the only scriptable source of plan usage (`/usage` is interactive; `ccusage`
   reports tokens and cost, not % of plan). It reaches the script only if the
   user's status-line command records it; add one line after it reads stdin:
   `printf '%s' "$input" | bash <repo>/.claude/bin/super-board-usage.sh record`.
   Pro/Max only (API-key sessions have no `rate_limits`); it refreshes on this
   session's own turns. Claude Code's built-in pause at 100% remains the
   backstop — the guard exists so a wave does not start that the limit would
   cut off mid-lane. The legacy `claude-p` dispatcher does not run this guard.
2. **Plan the wave** — first run the field audit, read-only, one GraphQL read:
   `bash .claude/bin/super-board-card.sh --config <config-path> audit`. Each `#N <Status> missing: …` line goes
   into the wave report as one line per card under the human's queue; a card with no type label
   (`bug`, `feature`, `qa`, `docs`, `ux`, `tests`, `refactor`, `tech-debt` or `money`) shows up as `missing: …label`.
   The planner does not skip such a card: a lane that moves it adds the label with `--kind` on its first
   move (the wave prompt names the kind), and the audit only reports it. An audit failure is one report line (`audit failed: <reason>`)
   and never a halt, except exit 79, which is the read halt below. Then
   `bash .claude/bin/super-board-wave-plan.sh --config <config-path>` →
   Exit 9 is a verification-readiness halt: preserve work and claims, repair the
   owning project's `verify_commands`, then restart explicitly. No GitHub recovery probe,
   rebase, Build bounce, or automatic retry.
   **Skill readiness before any wave writes.** Hydrate every selected issue body
   read-only with `.claude/bin/super-board-github-read.py --kind issue -- issue
   view <N> --json number,title,body`. Keep number, status, labels and literal body
   in a JSON array file written with the Write tool under `.claude/super-board/inflight/`
   (e.g. `.claude/super-board/inflight/cards-<wave>.json`) or the run's scratchpad — under the
   fence those are the only places a file tool may write. Run `python3 .claude/bin/super-board-skills-ready.py
   --root <repo-root> --host <claude|codex> --cards <cards.json>` for the actual
   worker host before sweep/resume/refresh/flag/stranded actions or Claim.
   Exit 10 names missing helpers: halt, preserve all state, repair the exact
   visible skill files and restart explicitly. Exit 64 means invalid input.
   GitHub hydration failure follows the existing exit-79 read policy.
   Empty selection checks no unused helpers. Repeat with the new host before
   usage fallback; Claude availability never proves Codex availability.
   Pass hydrated bodies into the wave. Workers recheck their lane before writes
   and activate conditional helpers only after `--require <exact-name>` passes.
   Untyped Ready cards conservatively check bug and feature chains before
   classification. Existing docs/ux/tests/refactor rows keep their chain; check
   any proposed new label chain before writing that label.

   The planner returns `cards`, `sweep`, `resume`, `refreshApproval`, `flag` and `stranded`. **Act on
   `sweep`, `resume`, `refreshApproval`, `flag` and `stranded` BEFORE launching** (run.md → "The wave-start
   sweep"): move every swept card to `Ready` with a comment naming what cleared
   it, move every `resume` card (verified human approval of the current pinned
   request) to `Review`; `needs-you:done` is display-only. Move `refreshApproval`
   cards to `Review` solely to review the changed head and post a fresh human request;
   clear their stale `needs-you:done` display labels. They have no approval to merge.
   Comment on flagged cards asking for their
   `## Blocked by` line to be fixed, and return every stranded Building card with a
   clean or no worktree to `Ready` (below). Swept, resumed, approval-refresh and stranded cards are NOT in this pass's
   `cards`; they join the next wave.

   **Stranded Building cards.** A card in Building with no assignee between
   waves has no live worker — a stopped or crashed wave left it mid-build. For
   each: find its branch (`git ls-remote --heads origin 'issue-<N>-*'`) and any
   leftover `.claude/worktrees/issue-<N>-build/`. Remove it with `git worktree remove`
   without force only when it is clean: `git status --porcelain --ignored` is empty and
   `git worktree list --porcelain` shows the entry as neither `locked` nor `detached` (a
   detached HEAD's commits may have no other ref). A dirty, locked, detached or unregistered
   worktree is unsafe: the card stays in Building, post one comment that starts with `[orchestrator] [report] ⚠ unsafe worktree` and names the worktree path and why it is unsafe, and do not redispatch it until its work
   is preserved. The stranded sweep skips a `Building` card whose most recent orchestrator comment starts with that prefix and whose worktree is still unsafe.
   The operator clears it by first preserving any work (for a detached HEAD, `git -C <wt> branch <name>`; for a locked worktree, `git worktree unlock <wt>`), then removing the worktree or leaving it clean on a branch; the next sweep then returns the card to Ready.
   Otherwise keep the branch and PR,
   move the card to `Ready`, and comment:
   `[orchestrator] [report] ↩️ back to Ready · stranded in Building` / `Did: found no live
   worker` / `✅ Done: card moved to Ready · branch <name> kept` / `Next: builder`
   (writing-standard.md § 4). The next Builder continues on the branch.
   **Builder pre-flight.** No card goes Ready → Building unchecked. The wave's first phase
   (`Pre-flight` in `super-board-wave.js`, before `runLane('build')`) runs one cheap sub-agent per
   batch of up to 5 Ready cards: already merged? already in progress (open PR or another card)?
   same files as an open PR? unclear? too big? unsafe? It runs `super-board-preflight.sh`, posts the Block template
   and moves held/sequenced cards to Blocked itself (run.md → "Builder pre-flight"). The summary
   reports those as `preflight:blocked`; `preflight:failed` means pre-flight was blind and the card
   stays in Ready for the next wave. Sequenced cards carry a `blocked-by:` line, so this same sweep
   frees them when the overlapping PR's issue closes.
   Wave width is every card the dependency graph says is free (no
   orchestrator content filter), in flight
   first (Review → QA → Ready), cut to `max_workers` when it is set (0 or
   absent = no cap). `max_workers` is the GitHub-limit valve: step 5 lowers it
   after a wave that hit the hourly limit. If `cards` is empty and
   Building/QA/Review counts are 0 and `sweep` is empty → done. If `sweep` freed any card, the board is
   not drained: those cards are Ready now, so loop to step 1 and the next wave builds them (a blocker that
   closed during the last wave frees its card only here). If empty but cards sit in Blocked
   only and the sweep freed nothing (every blocker still open, or `blocked-by: -`) → report and stop.
   This is unfinished work, not a completed-run calibration trigger.
3. **Claim** — for each card, `gh issue edit <n> --add-assignee <notifications.bot_identity>`,
   then VERIFY: re-read assignees (`gh issue view <n> --json assignees`) and
   proceed only if the list is exactly `[<notifications.bot_identity>]`. Adding an assignee
   does NOT fail when someone else already claimed (issues accept up to 10
   assignees), so the add alone is not a mutex — on any other assignee set,
   remove own assignee and skip the card (race lost). Skipped when
   `notifications.bot_identity` is unset in the config — accepted single-orchestrator risk: without it
   there is no cross-session claim at all, so never run two orchestrators
   (or /loop re-entries) against the same board without `notifications.bot_identity`.
   Write the claimed cards (the same hydrated JSON array) with the Write tool to
   `.claude/super-board/inflight/claimed-cards-<wave>.json` (or the scratchpad) — the Codex launch
   (step 4) passes that file as `--cards`.
4. **Launch** — With `--codex`, or no Workflow tool, or the host check printing `codex`, use
   "Codex runs" above; otherwise Workflow tool with
   `scriptPath: .claude/workflows/super-board-wave.js` and
   `args: { configPath, repo, cards, tier, models, pilot, foldBuildQa (only when true), exitCheckpoint / checkpointMax (only when not the default), singleLane: true (event dispatch only) }` (`cards` hydrated after the planner: each
   carries `lane` and `labels`, and a `qa` card skips the Builder). `repo` is the config's `repo.remote` as `owner/name`: read
   `jq -r '.repo.remote' <config>` and strip the scheme, host and `.git` by hand — while the lock is
   held the orchestrator guard denies pipes and `jq` flags beyond `-r`, so the fenced spelling is a
   plain `jq -r '<path>' <config>` call with the string work done by the model. Runs in the background; the
   orchestrator stays responsive.
   `tier` is the run's model ladder: `'low'` when the user invoked
   `super-board run --low`, `'high'` for `run --high`, omitted/`'medium'`
   otherwise (the default). A `model_tier` key in the config sets the default;
   an explicit flag wins over config. The ladder is per lane (build, qa, review)
   and indexed by card complexity; no lane builds on Opus by default, review
   runs on sonnet, and on opus for high-complexity cards only (sonnet on every card on `--low`), and a card that was never classified takes
   the tier's medium cell. `models` is config `pilot.models` passed through
   unchanged (`jq -r '.pilot.models // empty' <config>`; omit the arg when it
   prints nothing): the operator's pins, `{build?, qa?, review?}`, each a model
   string (`haiku|sonnet|opus|fable`) or `{low?, medium?, high?}`. A pin wins
   over the ladder; `fable` runs only from a pin; an unknown string stops the
   wave at start. Each lane's history entry records `model` and `modelReason`.
   `pilot` is config `pilot.enabled` (`jq -r '.pilot.enabled // false' <config>`);
   pass `pilot: true` only when it prints `true`, otherwise omit it and the wave
   behaves exactly as above. With it on, an Opus pilot agent proposes `{model, reason}`
   per lane for each card before that card's first lane runs (one Opus call per
   card that reaches a lane, because the pipeline stages have no barrier for a single
   per-wave call; paid on every tier). The wave enforces the rules: a pin wins, only
   `haiku|sonnet|opus` is accepted, `opus` never builds without a pin, the pilot may
   raise the Reviewer's model but never lower it below its ladder cell (the pilot reads
   card text; only a pin lowers review), and a missing or rejected answer
   keeps the ladder cell. `modelReason` reads `pilot: <reason>`, `pin`, or
   `ladder … (pilot rejected: <why>)`. A pilot failure logs one line, uses the
   ladder and never halts the wave. Claude models only; delegate tools are Phase 3.
   `exitCheckpoint` is config `lanes.exit_checkpoint` and `checkpointMax` is `lanes.exit_checkpoint_max`; pass each only when it differs from its default (`true`, `2`). `checkpointMax: 0` or `exitCheckpoint: false` drops the checkpoint from the build, qa and build+qa prompts (run.md → Exit checkpoint).
   `foldBuildQa` is config `lanes.fold_build_qa` (`jq -r '.lanes.fold_build_qa // false' <config>`); pass `foldBuildQa: true` only when it prints `true`. With it on, a `bug` card runs one `build+qa` lane (taint checked first; a tainted card bounces with "tainted, rerun unfolded"; not when the card carries a merge-policy always-human label) and Review stays separate; `qa` cards are already single-lane. The folded lane runs on the Builder's model; a `models.qa` pin does not apply to folded cards.
   `singleLane: true` (issue #66, event dispatch only — never in an ordinary wave) stops each card after its first lane: the entry-point lane for the card's column runs and the chain ends there, so one trusted event starts exactly one lane. Without the arg the chaining is byte-identical to the ordinary wave.
5. **Reconcile** (when the run completes) — read the returned `cards`
   summary. If `halted: true`, any card is `halted`, or the GitHub helper's `--check`
   fails, preserve claims and stop. For verification-readiness (exit 9), repair
   `verify_commands` and restart explicitly; do not invoke GitHub evidence recovery.
   For a GitHub read halt, follow "Required GitHub evidence" above — any `--meta <fresh-meta.json>`
   for the resume check is written with the Write tool under `.claude/super-board/inflight/` or
   the scratchpad. Otherwise,
   for EVERY card in the wave, release the assignee
   (`gh issue edit <n> --remove-assignee <notifications.bot_identity>`, idempotent).
   Then reconcile in one allowlisted call (the lock is held; the manifest append and the throttle
   read are compound steps the orchestrator guard fences):
   `bash .claude/bin/super-board-orch.sh reconcile --config <config-path> --wave <n> --date-slug <date>-<slug>`
   with the wave result JSON (the object with the `cards` array) as a QUOTED heredoc body
   (`<<'EOF' … EOF`) — the one compound form the guard allows. It appends one row per card to the run manifest
   `docs/super-board/runs/<date>-<slug>.md`:
   `| #N | <lanesRun> | <finalStatus> | <column> | <detail> |`, then runs
   `super-board-throttle.sh`, whose JSON line lands on stdout.
   `hit: true` (GraphQL at 0, or a lane failed on the limit) has already written
   `max_workers` one lower into the config — unlimited → 3 → 2 → 1, floor 1 — so the
   next wave and the next run start narrower. Raising it again is the owner's call.
   Then the roadmap: `python3 .claude/bin/super-board-roadmap.py sync --config <config-path> --wave <n>`
   (refreshes the pinned "🗺️ Roadmap" issue). Exit 0 or 3: say nothing. Exit 79 is the GitHub
   halt already handled above, like the `--check` failure. Any other exit: add one wave-report
   line `🗺️ roadmap not updated: <reason>` — never halt, never retry.
6. **Report** — one short block to the user per wave, in the session. Outcome
   first, one line per card that needs a human, then what happens next:

   ```
   Wave 3 · 5 cards → 2 merged · 1 bounced · 1 blocked · 1 failed · usage 5h 62%
     🛡 #15 Blocked 🔐 missing STRIPE_TEST_KEY — owner: the operator
     ❌ #18 failed: build lane returned no result — retried next wave
   Next: wave 4 (3 cards)   |   ⏸ paused until 14:05   |   ✅ board drained
   ```
   A throttle hit adds one line: `🐢 GitHub hourly limit hit — max_workers 3 → 2 (config saved)`.
   A roadmap failure adds one line: `🗺️ roadmap not updated: <reason>`.
   Merged and bounced cards are counts, not lines — they need nobody. Every
   `human-gate`/`blocked` card gets its line and its owner: that is the
   human's queue. Never report a card as done that is not on the base branch.
7. **Halt gates** — stop with a report if: 3 consecutive waves made zero
   progress (every card bounced, failed or was held or sequenced by pre-flight); block-rate exceeds
   `block_rate_alert_pct` of initial Ready; or the user says stop. The halt
   note is four lines: `🛑 halted — <gate>` · the evidence (counts, the cards
   that kept bouncing) · what is left in flight or claimed · `Resume:
   /super-board run <slug> [--codex[=<model>]] [--low|--high]` (retain the run's choice) and who must act first.
   Append the halt note to the run manifest `docs/super-board/runs/<date>-<slug>.md` with Edit (an
   allowed orchestrator path) and report it to the user in the session.
8. Loop to 1. Before stopping because nothing moved or the board is drained, run the wave-start sweep; if it frees any card, loop to step 1. A card counts as freed only when its status re-reads as `Ready` after the sweep's move; if the move fails, stop and report. Pre-flight never sequences a card behind a closed issue (`super-board-preflight.sh` keeps only open issues in `blockedBy`), so a swept card cannot be re-held on the blocker that freed it. A Blocked card whose `blocked-by:` issues have all closed is
   sweepable work; one with `blocked-by: -`, or a blocker still open, is not. For unattended cadence, the user may wrap this loop in /loop;
   the orchestrator must still stop at halt gates.

After normal completion, confirm all selected waves and lane tasks have ended,
finish final reconciliation, and release only the `workflow-wave.lock` created
by this host for this run: `bash .claude/bin/super-board-orch.sh lock-release <slug> <started>`
(the helper matches the lock's own saved slug/start identity and keeps the lock on any mismatch).
Never remove
a marker owned by another or uncertain run; preserve it and skip calibration.
The read-only adapter never removes markers. Then run the optional adapter in [`calibration.md`](calibration.md).
This is a separate post-run task, after all lane workers and pending selected work
are accounted for. Do not calibrate per wave, on a halt/partial delivery, or from
Done counts or process exit alone. When enabled and eligible, delegate the installed
skill automatically using the helper's scoped brief; check its actual diff before
accepting automatic edits. An unavailable skill/host or an ineligible receipt is
reported as skipped and does not change the completed product work.

## Event dispatch (`run --events`)

When the config enables `events` (`config-schema.json` → `events`; full contract in
`references/events.md`), the run also serves **trusted event triggers**: a card move onto a
routable column, or a comment whose first line is exactly `events.command`, starts exactly one
lane on one card through the normal wave — the wave from step 4 with `singleLane: true` and
that one card. The flow, in place of steps 1–3 (same lock and reconcile protocol):

1. `python3 .claude/bin/super-board-events.py --config <config-path> poll` — read GitHub
   (≤4 read-only calls), decide candidates into the queue. Exit 79 = a read halted; leave
   everything and report ("Required GitHub evidence" above).
2. `python3 .claude/bin/super-board-events.py --config <config-path> next` — prints the oldest
   dispatchable event's JSON; exit 3 = nothing dispatchable (disabled, empty, expired, or over
   the starts budget) — go back to the ordinary wave loop.
3. `bash .claude/bin/super-board-orch.sh lock-acquire <slug>` — same wave lock as any wave.
4. `python3 .claude/bin/super-board-events.py --config <config-path> claim <id>` — live
   permission + budget re-check; records the start; exit 5 = refused (report, do not retry).
5. Launch the wave (step 4) with `cards: [<the event's card hydrated>]` and `singleLane: true`.
6. `done <id> --result "<lane>: <column>"` or `fail <id> --reason "<detail>"`, then
   `lock-release <slug> <started>` (step 5's reconcile still applies to the card).

Trust: the actor must be in `events.initiators`, hold a **live** admin/maintain/write
permission (checked at claim time), and never be `notifications.bot_identity` (which must not
be `auto` — auto disables events). The bounce guard drops new events while a wave runs. State
lives under gitignored `.claude/super-board/events/<slug>/`.

## Stop / resume

- Stop: `x` on the run in `/workflows` (or TaskStop), then release assignees
  for in-flight cards and post "stopped mid-flight" comments (same protocol
  as `references/stop.md`; the lock is still held, so its branch and PR lookups use the fenced forms
  `git ls-remote --heads origin 'issue-N-*'` and `gh api 'repos/OWNER/REPO/pulls?head=OWNER:BRANCH'`,
  not `git branch -a` / `gh pr list`). Remove `.claude/super-board/inflight/workflow-wave.lock` with
  `bash .claude/bin/super-board-orch.sh lock-release <slug> <started>`.
- Codex stop: with GitHub available, run `bash .claude/bin/super-board-stop.sh <slug>`; it validates the wave PID and allows SIGTERM to cancel child lanes before releasing claims. Apply the same claim/comment/lock protocol to ALL selected claimed cards, including queued cards. During a GitHub halt, send SIGTERM only to the verified wave PID, wait for child cancellation, and preserve claims without GitHub writes.
- Resume: just run again — board state is the only state. A workflow stopped
  mid-wave can also be resumed in-session via `resumeFromRunId` (completed
  lane agents return cached results).
- Cards stranded in `Building` (wave stopped after the Builder moved
  Ready → Building): the next run's crash-recovery sweep releases their claim
  and the planner reports them in `stranded` (wave loop step 2). A card with a
  clean or no worktree returns to `Ready` with no manual drag; one with an unsafe
  worktree stays in `Building`. The operator clears it by first preserving any work (for a detached HEAD, `git -C <wt> branch <name>`; for a locked worktree, `git worktree unlock <wt>`), then removing the worktree or leaving it clean on a branch; the next sweep then returns the card to Ready.

## Mid-run permission prompts

Lane agents inherit the session allowlist and run in acceptEdits. Add these
to your project's `.claude/settings.json` → `permissions.allow` so waves
don't stall on prompts:

    "Bash(gh issue view:*)", "Bash(gh issue edit:*)", "Bash(gh issue comment:*)",
    "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)", "Bash(gh pr comment:*)",
    "Bash(gh pr create:*)", "Bash(gh pr ready:*)", "Bash(gh project item-edit:*)",
    "Bash(gh project item-list:*)", "Bash(gh api:*)", "Bash(git worktree:*)",
    "Bash(.claude/bin/super-board-card.sh:*)",
    "Bash(git checkout:*)", "Bash(git add:*)", "Bash(git commit:*)",
    "Bash(git push:*)", "Bash(git pull:*)", "Bash(git fetch:*)", "Bash(git blame:*)",
    "Bash(pgrep:*)",
    "Bash(bash .claude/bin/super-board-wave-plan.sh:*)",
    "Bash(bash .claude/bin/super-board-preflight.sh:*)",
    "Bash(python3 .claude/bin/super-board-skills-ready.py:*)",
    "Bash(python3 .claude/bin/super-board-github-read.py:*)",
    "Bash(python3 .claude/bin/super-board-verification-ready.py:*)",
    "Bash(bash .claude/bin/super-board-usage.sh:*)",
    "Bash(bash .claude/bin/super-board-throttle.sh:*)",
    "Bash(bash .claude/bin/super-board-orch.sh:*)",
    "Bash(python3 .claude/bin/super-board-roadmap.py:*)",
    "Bash(bash .claude/bin/super-board-codex-lane.sh:*)",
    "Bash(bash .claude/bin/super-board-codex-wave.sh:*)",
    "Bash(bash .claude/bin/super-board-host.sh:*)",
    "Bash(bash .claude/bin/super-board-pr-body.sh:*)", "Bash(gh pr edit:*)",
    plus your project's test runners (e.g. "Bash(npm test:*)", "Bash(npx playwright:*)").

The allowlist matches the start of a single command. A chained call (`cd … && gh …`) does not match and falls to the auto-mode classifier, so lanes run each GitHub or git write as its own call. These lines serve the lanes; during a run the orchestrator guard (precondition 0) fences the parent session to its own allowlist, which has no plain `mkdir` or `node --check` row — both happen inside the helpers (`check-wave` wraps `node --check`, `reconcile` creates the manifest directory), so the parent session needs no such entries.

Merging is NOT in the base list. On a board whose `merge_policy.default` is
`"auto"`, `super-board onboard` offers the merge lines as a diff to approve
up front (onboard → Permissions), so the first overnight run does not stall:

    "Bash(bash .claude/bin/super-board-merge-gate.sh:*)", "Bash(gh pr merge:*)",
    plus one line per configured migrate command in `migrations.commands`
    for the envs in `migrations.allowed_envs`.

Without them every Reviewer merge pauses for one interactive approval, so the
backend is **attended-only**. With them, the gate still enforces
`merge_policy` (money / auth / destructive schema / over `auto_max_lines` → human, exit 7) and `migrations`
(live DB → 🙋 needs you, exit 8), so the allowlist removes the prompt, not
the policy. Pair auto-merge with a non-production `base_branch`.

> **Read this before starting an overnight wave (issue #9).** The two settings
> interact, and the failure is silent. A board configured
> `"human_approves_merge": false` and `merge_policy.default: "auto"`, processing
> otherwise ungated PRs **without** `"Bash(gh pr merge:*)"` allowed, will build,
> test and review cards all night and land **nothing** —
> each Reviewer stalls on an approval prompt nobody is there to click, and the
> card returns to Review to be re-reviewed on the next wave.
>
> Pick one deliberately:
>
> | Intent | Configuration | Allowlist `gh pr merge` | Reviewer's terminal state |
> | --- | --- | --- | --- |
> | Attended — approve each merge command | `human_approves_merge: false`, `merge_policy.default: "auto"` | no | pauses on a tool permission prompt |
> | Unattended auto-merge | `human_approves_merge: false`, `merge_policy.default: "auto"` | **yes** | card → Done after confirmed merge; configured human gates → 🙋 Blocked |
> | Every PR requires human approval through the gate | `human_approves_merge: false`, `merge_policy.default: "human"` | needed only if the agent will merge after approval | gate exits 7 → 🙋 Blocked until a human merges or approves the pinned request |
> | Legacy human-only switch | `human_approves_merge: true` | no | Reviewer stops before the gate; card stays in Review |
>
> Gate-based rows also need the merge-gate command allowed. A tool permission prompt
> is separate from the gate's human-approval request; allowing a command does not
> bypass `merge_policy`.
>
> Row 2 is the only combination that lands code with nobody watching, and it
> requires a non-production `base_branch` — the run script's production-merge
> guard refuses to start otherwise. Whichever row you pick, the Reviewer still
> owes the full merge protocol in `run.md` → "Merge protocol": mark the draft PR
> ready, merge, **verify the merge commit is an ancestor of the base branch**,
> and only then move the card to Done. A merge-blocked PR goes to Blocked, not
> back to Review.

### Release

The robot never merges into the default branch. After a person merges the release PR (from `base_branch` into the
default branch), run `.claude/bin/super-board-card.sh --config <cfg> release <P>`, or add `--dry-run` first to see
what it would label. It adds the `released` label to every closed issue whose merge marker sha is in that PR's
commits. Issues in `protected_issues` are skipped. Release PRs into the default branch must use a merge commit, not
squash: a squash leaves staging's history out of main, so each later release lists the earlier commits again and soon
hits the 250-commit cap (exit 69).

A Done move that exits 69 (the status moved but the comment or close failed) shows up in
`.claude/bin/super-board-card.sh --config <cfg> audit` as `#N Done missing: closed`. Re-running the same Done move
closes it; it is idempotent and never posts the marker twice.

### Skill-readiness reconciliation

Exit 10 from a skill check or lane result with detail `skills-readiness:` is a
readiness halt. Stop new dispatch, stop only this runner's active workers,
and preserve claims, inflight records, worktrees, card state and approval
evidence. Repair the exact host-visible skills and restart explicitly. Do not
release claims, bounce a card or enter a retry loop on this halt. Existing
preselection crash recovery remains the discovery step; once missing skills
are known, recovery and wave writes stop.
