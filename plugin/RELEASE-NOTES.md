# Release notes

## v3.1.3-ams.31

- ⚠️ **Upgrade notes: re-run the installer so `.claude/bin/super-board-autopilot-batch.py` lands.**
- ✨ **Mission-control autopilot: one session runs the board for one goal, with no "Continue?" or tier prompt between cycles — each cycle pipes the goal's cards and the repo's open issues into the batch script, moves up to `batch_size` eligible `Backlog` cards to `Ready`, runs `super-board run <tier>`, reconciles the mission log, and repeats. It runs only when the operator starts it (`autopilot: <goal>`, or the shipped `/goal` prompt) and is otherwise off; merge rules are unchanged, and auto-merge stays in the pilot repo only.** (PR #76)
  - New file `plugin/scripts/super-board-autopilot-batch.py` (added to the installer list and `BIN_SCRIPTS`, so it lands at `.claude/bin/super-board-autopilot-batch.py`): a pure function — stdin JSON in, JSON out, no network, no side effects. It matches the goal label `goal:<slug>` case-insensitively (plain name strings and `gh` label objects both work), parses the card body's `## Blocked by` section against the repo's open issue numbers, orders the eligible cards by issue number, returns one of the reasons `ok` / `empty` / `in-flight` / `all-blocked-by-deps` / `blocked`, and exits 2 on any malformed input.
  - The cycle: fetch the goal cards with their status, labels and body, pipe the payload into the batch script, move up to `batch_size` eligible `Backlog` cards to `Ready`, run `super-board run <tier>` (no tier question), then update the Cards table and `batches_done` in the mission log. Reason `in-flight` is not a stop: the session re-runs the board. Autopilot only pulls cards the operator already approved; planning, grilling and ticket approval stay interactive with the operator.
  - Settings and defaults: the mission-log template (§7) gains an `## Autopilot` block — `status`, `goal`, `label`, `tier`, `batch_size` (default 3), `max_batches` (default 5), `batches_done`. `status: on` resumes the cycle after a break (§1, `CORE.md`); approved cards for the goal go to `Backlog` with the `goal:<slug>` label instead of `Ready` (§4.1); the tier is read from the block (§4.2).
  - Stop conditions — each sets `status: stopped — <reason>` in the block, notifies the operator (an in-session message and a `PushNotification`, plus Telegram when the session is remote) and ends the turn: a goal card is `Blocked` (a card super-board itself moves to `Blocked`, for example by pre-flight sequencing, also stops autopilot as `blocked`, which errs on the safe side); the goal is done (the session drafts the next cards for approval and files nothing); every candidate is blocked by its dependencies; `batches_done` reaches `max_batches`; a harness or permission refusal; two cycles in a row with no card moving; a batch script error; the operator sets `status: off` — the one value never overwritten. The shipped `/goal` prompt drives the same loop, and its "met" condition holds at every stop, so `/goal` no longer re-prompts while a card waits on the operator.
  - §5 wording fix: `>200 changed lines` becomes `over merge_policy.auto_max_lines` (default 400), which matches the gate. No other §5 change, and no merge rule changed.
  - Tests: new `test_autopilot_batch.py` (30 unit tests).

## v3.1.3-ams.30

- ⚠️ **Upgrade notes: re-run the installer so `.claude/hooks/guard-orchestrator.py` and the new `.claude/bin` scripts land. `--no-hooks` installs can no longer run `super-board run`.**
- ✨ **Event triggers: a trusted GitHub event starts exactly one lane on exactly one card, through the normal wave and every gate, inside a hard start budget. Off by default; set `events.enabled: true` and run `super-board run --events`.** (PR #73)
  - Sources (deterministic only, no model filter): a card's status changing to Ready with a trusted actor, and an issue comment, PR comment or PR review comment whose first line is exactly `/super-board go` from a trusted author. `events.sources` turns each source on or off. The lane comes only from the card's status (Ready gives build, or qa for a `qa` card; QA gives qa; Review gives review); nothing in a comment can choose the lane.
  - Trust: the actor must be in `events.initiators`, must not be `bot_identity` or a `[bot]` login (checked at enqueue; a failing event is dropped, never queued), and must have live `write` or higher permission (checked at claim, the latest moment before any start). If `bot_identity` is `auto`, events are disabled.
  - Budget, fail closed: 3 starts per hour and 10 per day by default; the pilot is off for event starts; the budget is checked at `next` and at `claim` and recorded before launch; a missing or corrupt ledger dispatches nothing; overflow events stay queued and are reported once.
  - How it runs: no scheduled task — the Monitor loop inside the session calls `super-board-events.py poll` (up to 4 reads per tick through the guarded read helper, with a watermark overlap and dedup), and each event is dispatched under the normal wave lock with `cards:[one]` and `singleLane: true`, which stops each card after its first lane (folded and qa-label paths included). Without the flag the prompts are byte-identical to before.
  - Safety: no comment body is ever stored or passed on; `guard-config-write` protects `events/` from session writes, so a lane cannot plant an event; the orchestrator guard allowlists `super-board-events.py`.
  - Tests: new `test_events.py` (74 cases, Windows and Linux) and `test-wave-single-lane.sh`; the fold and exit-checkpoint prompt hashes are unchanged.
- ✨ **Orchestrator guard: during a default workflow-backend run the parent session can no longer edit product files or run builds and tests — a PreToolUse hook enforces it, not the prompt. On by default; `super-board run` refuses to start without the guard hook, and `SB_ORCH_GUARD_OFF=1` is the escape.** (PR #72)
  - The hook payload carries `agent_id` only for subagents and workflow lanes, so the guard enforces only when all four hold: `agent_id` is missing, the wave lock exists, the session matches the bound run, and `SB_ORCH_GUARD_OFF` is not `1`. Lanes stay free. It is a guardrail, not a security boundary.
  - File writes (Write, Edit, MultiEdit) are allowed only under `.claude/super-board/inflight/`, the orchestrator's own scratchpad and `docs/super-board/runs/` (run files and the manifest); every other path is denied. NotebookEdit and PowerShell are always denied.
  - Bash: one allowlisted simple command, no pipes, chaining, redirects or substitution — every documented `.claude/bin/super-board-*` helper (both the `python3 x.py` and bare `x.py` forms), git reads (never `--output`), `gh issue|pr|project`, `jq -r` on a board config or `.claude/settings.json`, process reads. `gh api` (REST and GraphQL writes) is outside the fence.
  - New `super-board-orch.sh` takes over the compound steps the fence now forbids: `lock-acquire` / `lock-release` (also clears the session binding), `check-wave`, `db-check` (command read from protected config only), `reconcile`.
  - Install gate: the run halts at step 0 when the hook file is missing, `settings.json` does not wire the hook, or `SB_ORCH_GUARD_OFF` is `1`.
  - Audit: each denial is one JSONL line in the gitignored `.claude/super-board/orchestrator-violations.jsonl` (rule, first token, a hash, the cwd — never the full command text), rotating at 1 MiB; if the log cannot be written the call is still denied, and every deny message ends with "do not retry in another form."
  - Tests: new `test_guard_orchestrator.py` (61 tests, including DocFlow tests that fail if any documented orchestrator step is blocked again) and `test-orch-helper.sh` (13 checks); the install, run-gates, settings and get tests are extended.
- ✨ **Watchdog: an external deterministic checker reports stuck cards and stalled runs, once per incident, with no model calls and no GitHub writes. Off by default; set `watchdog.enabled: true`, and the operator runs `super-board-watchdog-install.sh --apply` — the release installs nothing.** (PR #71)
  - `super-board-watchdog.py`: a pure `evaluate()` with a thin I/O layer; reads GitHub only through `gh api graphql` (at most 5 pages, with a rate-limit floor) and not the GitHub read helper, because that helper can write `github-halt.json`; comments from authors who are not trusted are dropped before `deps.sh --from` sees them, so a stranger's `blocked-by: -` cannot silence it or reset its clocks. It never stops or restarts anything.
  - Incident kinds: `card-stall`, `card-loop`, `run-stall`, `unknown-state`, `operator-queue`, `watchdog-blind`. When only the operator can act, it sends one operator-queue report and then stays silent. State and the incident log live under the gitignored `.claude/super-board/watchdog/<slug>/`; notifications are a best-effort Windows toast built from a fixed template, the text passed through an environment variable, never as an argument.
  - `super-board-watchdog-install.sh` prints the `schtasks.exe` command by default; `--apply` exits 78 unless `watchdog.enabled` is true, and exits 64 on a bad slug or paths that contain quotes. A new `watchdog` block in `config-schema.json`; docs in `references/watchdog.md`; `install.sh` copies both scripts.
  - Cost: 0 model calls per hour, at most about 300 GitHub points/hour (6 polls × 5 pages), about 6% of the hourly limit.
  - Tests: new `test_watchdog.py` (20 cases) and `test-watchdog-install.sh` (6 cases); greps confirm no model call and no `gh` write verb in either script.
- ✨ **Exit checkpoint: before its final board move, a build, qa or folded build+qa lane rereads its PR's review threads and handles the unresolved ones that start with its own prefix. `lanes.exit_checkpoint` is on by default, with a cap of 2 (`lanes.exit_checkpoint_max`, default 2; `0` = off).** (PR #70)
  - Prompt-only (no mailbox, no lease, no new `agent()` call): `EXIT_CHECKPOINT(lane)` is added to the build and qa prompts and the folded prompt, before `BOARD_IO`; preflight gets none, and review is unchanged because Reviewer Gate 1 already acts as its checkpoint. The lane handles only `[builder]` / `[qa]`-prefixed threads, using the rebuild steps; unprefixed and human threads are left to Gate 1 and are never resolved by the lane.
  - `checkpointMax` is clamped to 0–3, and junk falls back to 2; the cap counts total reads. Worst case is 2 extra guarded GitHub reads per lane, inside a lane that is already running.
  - `run.md`: Builder step 8b, Tester step 5c (before the Pass/Fail move), and an `### Exit checkpoint` section; `run-workflow.md` lists the launch args and the key-to-arg mapping.
  - Tests: new `test-wave-exit-checkpoint.sh` (prefixes per lane, review and preflight get none, both off-switches restore the old prompts byte for byte, the `agent()` call list is unchanged, the clamp).
- ✨ **The eval session manager runs on Haiku, and every wave `agent()` call pins a model, so no lane inherits the session's.** (PR #53)
  - `evals/golden/run_eval.py` launches `claude -p --model claude-haiku-5-5` (the full id, like the judge's, so baselines do not drift); `--session-model` overrides it.
  - New `plugin/tests/test-wave-model-pins.sh` runs the real `super-board-wave.js` under node with a stubbed `agent()` and asserts every call gets a non-empty model — pilot, preflight, classify, build, qa and review; tiers low, medium and high; classified, unclassified and late-entry cards; pilot answers that are good, null, a throw, garbage or empty.
  - The audit found all 4 `agent()` sites already pin a model, so `super-board-wave.js` is unchanged. One sentence each in `plugin/README.md` and `evals/golden/README.md`.
- ✨ **OpenCode and Pi delegate lanes: `super-board-codex-lane.sh --tool codex|opencode|pi` (default `codex`), opt-in and unsandboxed, with exit 77 = tool unavailable.** (PR #54, with #59 and #60 making both lanes launch for real on Windows)
  - Gates: build and qa only (review always exits 78 before launch); `usage_fallback` must equal the tool name; because OpenCode and Pi have no filesystem sandbox, they are refused unless the config sets `usage_fallback_unsandboxed: true` (strict boolean).
  - Lane commands: `opencode run --format json --agent build --model <opencode.model | opencode-go/kimi-k2.7-code> --auto` and `pi --mode json --model <pi.model | runinfra/glm-5-3-flash> --no-approve`, the prompt on stdin.
  - Exit 77 releases the card for a Claude lane — it is not marked failed or halted. OpenCode and Pi: 77 only on a startup failure (an anchored, tool-specific credit or auth error on the tool's own stderr, prompt echo removed, or an OpenCode stdout error event before any work); Codex: 77 only when the binary is missing or fails to launch. Halts (9/10/11/79) never become 77.
  - The result is taken only from the final assistant message (for OpenCode the latest `messageID` across all part events); the whole message must be one JSON object with `status`, anything else is `failed`.
  - Env: an allowlist of `super-board-lane-env.ALLOW` plus `HOME`, `APPDATA`, `XDG`, `GH_CONFIG_DIR`, an explicit list of `SUPER_BOARD_*` names and the tool's own key; names matching `SECRET_NAME` are dropped. Codex's environment is unchanged.
  - Windows launch fixes (PR #59): OpenCode's `--auto` is dropped (opencode 1.2.10 has no such flag) and permissions go through `OPENCODE_CONFIG_CONTENT` as `{"permission":{"edit":"allow","bash":"allow","webfetch":"deny"}}`; OpenCode and Pi resolve their binary once at the guard (unresolved exits 77) and launch through the full path, never `shell=True`; a model must match `^[A-Za-z0-9._/:-]+$` or the lane exits 64; temp-dir cleanup is best-effort and never replaces the lane's exit code.
  - Windows launch and stop fixes (PR #60): the Codex lane runs the shim's `node_modules/@openai/codex/bin/codex.js` under `node` directly, because cmd.exe would mangle the `-c` JSON through the `.cmd` shim (bare-name fallback with exit 77 when `node` or `codex.js` is missing); stopping an interrupted lane kills the whole process tree with `taskkill /T /F`, falls back to a direct kill with a `worker pid N may still be alive` detail, and the deadlocking `child.wait()` in the signal handler is gone.
  - Tests: new `test-delegate-lane.sh`; new argv, bare-name fallback, tree-kill and liveness cases; the real Windows smoke showed Pi starting and a missing key exiting 77 with no `WinError 2`.
- ✨ **`lanes.fold_build_qa` (off by default): a `bug` card runs one build+qa lane — one preamble, one worktree, Builder then Tester, one report — with Review as a separate agent after it.** (PR #55)
  - Passed to the wave as `args.foldBuildQa`. Review runs only when the folded lane returns `advanced` with `column=Review`. `qa` cards are never folded (already single-lane), nor are `feature` cards or cards labelled `bug`+`qa`.
  - The folded prompt checks taint first and fails closed: a non-zero check, a fetch failure, a `sandbox-<N>` marker or a check that cannot be completed all count as tainted, and a tainted card returns `bounced` to Ready with `tainted, rerun unfolded` and no mutation.
  - The folded lane runs on the Builder's model; a `models.qa` pin does not apply to it (documented). With the switch off, prompts are byte-identical to before, guarded by a hash snapshot of the build, qa and review prompts.
  - PR #57 fixes the two test interactions left red on main after #53, #54 and #55 merged together: the folded prompt's worktree line now carries the tainted-card qualifier the `test-move-docs.sh` gap check requires, and the `test-wave-model-pins.sh` stub returns `Review` for `build+qa`.
  - Tests: new `test-wave-fold.sh`.
- 🐛 **Pre-flight peer overlap: cards that share files never build in the same wave.** (PR #46, hardened by #58)
  - `super-board-preflight.sh`: every card in the output carries `files`; the `paths` regex also matches bare file names, including multi-dot ones (`foo.dev.ts`, `aa.co.uk.json`), excluding `e.g.`, `i.e.`, `v1.2`, domains and second-level domains.
  - `super-board-wave.js`: a new deterministic pass runs after all pre-flight batches return and before any Builder starts. Among the cards that will build, in number order, a card whose files meet a lower building card's files is deferred — verdict `skipped`, detail `preflight:deferred behind #N (shared …)` — stays in Ready, and builds next wave. A card with no `files` is never deferred.
  - PR #58 hardening, fail closed: `files` is required in the pre-flight verdict schema and the agent is told to always echo it (`[]` when the script found none); a building card whose verdict has no `files` array is held as unknown, and every later building card with files defers behind it. One path rule: two names meet if they are equal ignoring case, or one has no slash and equals the other's last segment (`kwd.py` meets `src/kwd.py`; `src/a.py` does not meet `lib/a.py`). Bare names need an allowlisted extension (1-character and digit/`_` stems count, so `__init__.py` and `2fa.py` match); Dockerfile, Makefile, Procfile, Gemfile, Rakefile, Jenkinsfile and Vagrantfile count, with a directory prefix, a `.`/`-` suffix, or joined; JS framework names in brand casing (`Node.js`, `Vue.js`, `Chart.js`) do not, while every other `.js` name does.
  - Known and accepted: product names like `Node.js` can count, names are compared as written (`test_kwd.py` does not meet `tests/test_kwd.py`), and the worst case for any mismatch is one card delayed by one wave.
  - Tests: new cases in `test-preflight.sh` (scenarios 25–29 for #58) and `test-wave-preflight.sh`; the three wave stubs now echo `files: []`.
- 🐛 **Pre-flight: a merged PR counts as delivered only if its merge commit is an ancestor of the base branch.** (PR #49)
  - `super-board-preflight.sh` gains an optional `--config <path>` (base branch read as `.base_branch // "main"`, the same way as `card.sh`); a merged PR must have `baseRefName` equal to the base branch and pass `git merge-base --is-ancestor <oid> refs/remotes/origin/<base>`; a merge commit the clone does not have is not counted, and stderr names it.
  - The script fetches `origin/<base>` once and the fetch never prompts (`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, BatchMode ssh, `timeout 60` only when it is GNU coreutils — Windows' `timeout.exe` broke the fetch). A failed fetch still runs the check against the existing ref; the check is skipped, with today's behaviour and a stderr warning, only without `--config`, a git repo, or `origin/<base>`.
  - Callers pass `--config`: `super-board-wave.js` (quoted), `run.md` (with a shell-quote note), `super-board-codex-lane.sh` (via `shlex.quote`).
  - `run-workflow.md` and `run.md`: every claim reference names `notifications.bot_identity` (the config key), no rule changed.
  - Tests: `test-preflight.sh` at 24 scenarios (ancestry cases (a)–(i) on a local bare origin); new assertions in `test-wave-preflight.sh`, `test-move-docs.sh` and `test-codex-lane.sh`.
- 🐛 **Lanes branch from a fetched `origin/<base>`, never the local base, and a 🛑 block's `blocked-by` line is never prose.** (PR #51)
  - `run.md` (Builder + Tester), `super-qa/SKILL.md`, `super-build/SKILL.md` and the qa prompts in `super-board-wave.js` and `super-board-codex-lane.sh` now say: run `git -C <main repo> fetch --quiet origin <base>` first, create the worktree/branch from `origin/<base>`, never branch from or read the local base, and if the fetch fails, Block and stop — never fall back. A QA lane that cut its worktree from the local base had audited stale code and blocked the card.
  - `block-template.md`: a 🛑 block whose reason is a harness or tool refusal ends `blocked-by: -`; a block that waits on other cards keeps its issue numbers. The line is never prose (the deps parser rejects prose).
  - The legacy standalone super-build path still names the local base (`references/standalone.md`, `worker-preamble.md`, `super-build-dispatch.sh`); it is not used by super-board waves and is deferred as a follow-up.
  - Tests: positive and negative doc/prompt assertions in `test-move-docs.sh`.
- 🐛 **Retro review of #45, #47 and #55: safe stranded-worktree rule, no admin key on the QA board path, sensitive bug cards never fold, and the 403 retry resumes only on success.** (PR #61)
  - A worktree is removed only when it is clean (`git status --porcelain --ignored` empty) and neither locked nor detached. An unsafe worktree keeps the card in Building, with one `[orchestrator] [report] ⚠ unsafe worktree` comment, and later sweeps skip that card; the operator preserves the work first (`git branch` for a detached HEAD, `git worktree unlock` for a locked one), then removes the worktree. `run.md`, `run-workflow.md` and the README agree.
  - `super-qa`'s board path no longer re-issues an admin password when a session drops; it re-reads `.e2e.user` / `.e2e.pass` from `.claude/settings.local.json`. The admin-key path is for standalone use only.
  - A bug card carrying any default merge-policy always-human label (`money`, `payments`, `billing`, `pricing`, `auth`, `security`, `schema`, `destructive`) never folds, so its QA keeps a separate tester; matching is by label only. The folded QA part tests the PR head in the Part 1 worktree, skipping the lifecycle's separate `-qa` checkout.
  - After a 403 or secondary rate limit, the lane resumes only if both the re-check and the retried call succeed; otherwise it stops the lane and comments with the quota. The same wording in `run.md` and `rate-limit-etiquette.md`.
  - The stale `humanApprovesMerge` wave arg is gone from `skill-map.json` and the visual pages. Tests: `test-wave-fold.sh` gains a case where all eight sensitive labels run build, qa and review as separate lanes with fold on; that case fails on main.
- 🐛 **Taint: one fail-closed rule that lists the card's branches, closing the marker and closed-PR gaps.** (PR #62)
  - The old rule treated a card with no PR branch as untainted, so a card carrying the `inflight/sandbox-<N>` marker but no PR branch counted as clean, and an `issue-<N>-<slug>` branch whose PR was closed counted as "no PR branch" — a lane could check out a tainted branch.
  - The new rule, in `run.md` → Tainted cards and the folded prompt: list branches with `git ls-remote --heads origin 'issue-<N>' 'issue-<N>-*'`. Tainted (fail closed) if the lookup exits non-zero, the marker exists, any fetch or check fails or prints `tainted`, or the check cannot be completed for any reason. Untainted only if the marker is absent and either no branch is listed or every listed branch prints `clean`.
  - A tainted card with no open PR goes to Blocked with a ❓ comment naming the branch — no checkout, no diff, no merge — in `run.md` and in the folded prompt.
  - Tests: `test-wave-fold.sh` pins the branch lookup, the "untainted only when" sentence and the no-PR Blocked path.
- 🐛 **The loop sweeps before stopping, and pre-flight never sequences a card behind a closed issue.** (PR #63)
  - `run-workflow.md` (steps 2 and 8) and `run.md`: before stopping because nothing moved or the board is drained, the orchestrator runs the wave-start sweep; if the sweep frees any card — its status reads back as `Ready` — the loop continues. A card with `blocked-by: -` is never sweepable.
  - `super-board-preflight.sh`: `blockedBy` keeps only the issues that are still open (one `issue list --state open` read through the GitHub read helper); a closed, deleted, transferred or non-issue ref is not an open blocker, and a failed list read still exits. A card the sweep has freed can no longer be held again on the blocker that freed it.
  - A card that proceeds with a conflict note (`sequence`, empty `blockedBy`) counts as building for the batch peer rule, so later cards that touch the same files are sequenced behind it; a card held or sequenced by pre-flight counts as no progress for the zero-progress halt gate.
  - Known limit: the open-issue read is capped at 1000 issues; a blocker beyond the cap counts as closed, so the card proceeds with a conflict note and the merge gate rebases it.
  - Tests: new `test-loop-sweep-before-stop.sh`; `test-preflight.sh` scenarios 30 (a closing issue that is closed, open, mixed, or not an issue) and 31 (three cards on one file behind a PR whose issue is closed).
- 🐛 **The qa lane prompt names the exact `[qa] [report] ✅/❌` marker.** (PR #64)
  - A Tester had posted `[tester] [qa] ✅ pass, handoff to Review` instead of the required PR and issue comments, and the grader scored the merged card `fail:no_report`. The qa lane prompt now says the QA report is one PR comment and one issue comment whose first line starts exactly `[qa] [report] ✅` (pass) or `[qa] [report] ❌` (fail), and that a handoff or status comment does not replace it. The folded build+qa prompt's PART 2 names the same marker; the build and review prompts are unchanged.
  - Tests: `test-wave-review-memory.sh` asserts the qa prompt names both markers and the build and review prompts do not; `test-wave-fold.sh` asserts the folded prompt names both (its switch-off qa hashes were re-taken because the qa prompt changed on purpose).
- 📝 **Docs: the stale `humanApprovesMerge` wave arg is dropped, and one 403 wording for lanes.** (PR #45)
  - `run-workflow.md`: `humanApprovesMerge` is removed from the wave `args` line, and the stale sentence about serializing Review lanes when it is false is deleted — the wave does neither.
  - `run.md` and `rate-limit-etiquette.md` §5: one lane rule — a 60 s sleep, one re-check at threshold 500, then resume, and never sleep in a loop. The orchestrator's "sleep until reset" text is unchanged.
- 📝 **Docs (audit #18): safe worktree-removal wording, lane triggers that actually fire, and no dotenv on the QA board path.** (PR #47)
  - `run.md`: a leftover build worktree is removed only when it is clean, registered, not locked and not on a detached HEAD, matching `run-workflow.md` and the legacy reclaim — a detached HEAD's commits may have no other ref.
  - The super-build, super-qa and super-review SKILL.md integration sections now also fire when the prompt names `references/run.md` or says it is a super-board workflow wave; before, they fired only on `SUPER_BOARD_RUN=1` (set by no script) or the literal "super-board run".
  - `super-qa/SKILL.md`: the board path reads no dotenv files or service-role key; it uses `.e2e.user` / `.e2e.pass` from `.claude/settings.local.json`, and if those are absent it tests public routes and says so in the QA report. The env-file bootstrap stays in `references/standalone.md`, marked standalone-only.
- 🧪 **Internal eval and lessons changes (PRs #50, #52, #56, #65).**
  - Golden card g07 now waits on g02 (#50) and g06 (#56): its QA rounding audit pinned a negative-amount format and a NaN error that g02 and g06 define differently, so the merge gate failed them after g07 merged first. The `deps` entries and the `## Blocked by` bodies match.
  - `evals/golden/baseline.json` (#52) is run `20261101T170141Z` on eval-base `73e2c07` (8/9 cards, $10.42, 64 min), with `cost_per_rep_usd`, `eval_base` and `plugin` added and the README fixture bullet rewritten to match.
  - `tasks/lessons.md` (#65) gains one entry: build fixture overlays from eval-base, not staging, because staging still holds the previous run's merges (it wasted a ~$20 baseline run).

## v3.1.3-ams.29

- ✨ **Each wave lane runs on a model picked from a per-lane Claude ladder by card complexity. No lane builds on Opus by default.** (PR #37, with #39 and #40 changing the end state)
  - `super-board-wave.js`: new `LANE_LADDERS`, one table per lane (build, qa, review), indexed by the card's complexity from the classify router. A card that was never classified (it entered past Ready) takes the tier's medium cell. The Reviewer now gets an explicit model instead of inheriting the session's. Each lane's history entry records `model` and `modelReason`, and the wave summary returns them per card as `models` and `modelReasons` (`<lane>=<value>` pairs).
  - Ladder on main (low / medium / high card complexity):

    | tier | build | qa | review |
    |---|---|---|---|
    | `low` (`--low`) | haiku / sonnet / sonnet | haiku / haiku / sonnet | sonnet / sonnet / sonnet |
    | `medium` (default) | sonnet / sonnet / sonnet | sonnet / sonnet / sonnet | sonnet / sonnet / opus |
    | `high` (`--high`) | sonnet / sonnet / sonnet | sonnet / sonnet / sonnet | sonnet / sonnet / opus |

  - `pilot.models` (new config key, passed to the wave as `args.models`): the operator's per-lane pins, `{build?, qa?, review?}`. Each value is a model string (`haiku`, `sonnet`, `opus` or `fable`) or `{low?, medium?, high?}` by card complexity. A pin wins over the ladder. `fable` runs only from a pin. An unknown lane, complexity or model string stops the wave at start.
  - `model_tier` in the config (and `--low` / `--high` on `run`) still picks the tier; an explicit flag wins over config. `config-schema.json`, `run-workflow.md` and `SKILL.md` describe the new ladder.
  - `README.md`: the Models table shows the per-lane Claude ladder instead of one model per card grade.
  - Tests: new `test-wave-model-ladder.sh`.
- ✨ **Review runs on Sonnet, and on Opus only for high-complexity cards (medium and high tiers).** (PR #39)
  - The Reviewer was Opus on every card of the medium and high tiers (the comment in `super-board-wave.js` puts an all-Opus Reviewer at about $6 a wave). It is now sonnet / sonnet / opus by low / medium / high complexity on those tiers; the `low` tier reviews on Sonnet for every card. `config-schema.json`, `run-workflow.md` and `SKILL.md` are updated to match.
  - Tests: `test-wave-model-ladder.sh` and `test-wave-pilot.sh` assertions updated.
- ✨ **The medium (default) tier runs QA on Sonnet for every card, low-complexity included. The `--low` tier keeps Haiku.** (PR #40)
  - Medium-tier QA was haiku on a low-complexity card; it is now sonnet. The `low` tier is unchanged: haiku / haiku / sonnet. `LANE_LADDERS.medium.qa` in `super-board-wave.js` is now sonnet / sonnet / sonnet (the table comment matches); `config-schema.json` says the same, and `test-wave-model-ladder.sh` now expects `sonnet` for low-complexity QA on the medium tier.
- ✨ **Opt-in Opus pilot: one Opus agent per card proposes a model for each lane, and the wave enforces the ladder rules on what it proposes.** (PR #38)
  - `pilot.enabled` (new config key, default `false`): when it is `true`, the wave gets `args.pilot: true` (omitted otherwise). The wave then runs one Opus pilot agent per card that reaches a lane, before that card's first lane, returning `{model, reason}` per lane. The cost is one Opus call per such card, on every tier. When the key is absent or `false`, the wave behaves as before.
  - Rules the wave applies to the answer: a pin from `pilot.models` wins; only `haiku`, `sonnet` or `opus` is accepted (`fable` only from a pin); `opus` never builds unless pinned; the pilot may raise the Reviewer's model but not lower it below its ladder cell (only a pin lowers review); a missing or rejected answer keeps the ladder cell. `modelReason` reads `pilot: <reason>`, `pin`, or `ladder … (pilot rejected: <why>)`.
  - A pilot failure logs one line, uses the ladder and never halts the wave. Claude models only.
  - Tests: new `test-wave-pilot.sh`.
- 🐛 **`super-board-github-read.py`: a `gh` usage error is a caller error (exit 64), not a halt.** (PR #36)
  - New `gh_usage_error()` matches `gh` refusing the command line itself, in stderr only: `not supported with`, `unknown flag`, `unknown shorthand flag`, `unknown command`, `accepts at most`, `accepts N arg`, `requires at least`, `flag needs an argument`, `cannot be used together` and similar. An example from the docstring is `--slurp` with `--jq`. The read exits 64 with the first stderr line and the run keeps going.
  - Not matched when stderr carries an HTTP status or a network error (`HTTP 4xx/5xx`, `error connecting`, `Could not resolve host`, `x509`, `tls handshake`, `timeout`), a rate limit, or an access error; those keep their old handling. With `SB_GITHUB_READ_STRICT=1` (`--issue-history`, `super-board-deps.sh`) it still halts.
  - Tests: new cases in `test_github_reads.py`.
- 📝 **The orchestrator never holds a Ready card on its content. Pre-flight gains a sixth question, `Unsafe?`, and a `hold — unsafe` outcome that moves the card to Blocked.** (PR #42)
  - `run-workflow.md`: the orchestrator does not hold, filter or pre-judge Ready cards on safety, scope or clarity. Every free Ready card goes to the wave, where Builder pre-flight and the Builder decide. A refusal ends in Blocked with the Block template, never left in Ready. Only pre-flight `skipped` (blind or no verdict) and `halted` cards stay in Ready; the summary reports them. The pre-flight question list now reads "too big? unsafe?".
  - `run.md`: pre-flight asks four script questions plus two agent questions. Question 6, `Unsafe?`, is agent judgement only: the acceptance criteria require passing untrusted input (anything a user, caller or request supplies) into `eval`, `exec` or a shell command, citing the AC line. A card that only mentions such a command is not unsafe.
  - New outcome `hold — unsafe`: the card moves to Blocked with a `🛡` comment naming the AC line, the risk and a safe alternative, Owner: the operator, `blocked-by: -`.

## v3.1.3-ams.28

- 🐛 **The PR-body check now covers the whole PR body, so a closing reference to another card in any block is refused, not only in `status`.**
  - `super-board-pr-body.sh`: the check runs on the body after the block is spliced in, before the dry-run, unchanged and PATCH paths, for every `--block`. Closing keywords (`close`, `closes`, `closed`, `fix`, `fixes`, `fixed`, `resolve`, `resolves`, `resolved`, any case, optional `:`) need a word boundary before them, so `prefix #5` is no longer a match. Caught forms: `#N`, `[#N]`, `owner/repo#N`, and `https://github.com/owner/repo/issues/N` or `/pull/N`. A reference is foreign when its number is not the card's, or its repo is not the PR's base repo. Exit 65 names the reference, the PR and the card; nothing is written. `Refs #N` does not close, so it is still checked in the `status` block only. If the check itself cannot run it exits 70 instead of writing. A branch with no `issue-<N>` number (`loop/issue-<N>` too) is not checked, as before.
  - This lifts the three known limits listed under ams.27.
  - Known limit: `Fixes #7, #221` — GitHub needs a keyword before each reference, so only `#7` is checked, matching what GitHub closes.
  - `run.md`: the exit-65 sentence says the whole body is checked.
  - Tests: new cases in `test-pr-body.sh` (other blocks, bracket, qualified and URL forms, another repo, the word boundary, a foreign reference already in the old body, `Refs` outside `status`); WSL Ubuntu run: 76 passed, 0 failed.

## v3.1.3-ams.27

- 🐛 **A PR's `status` block can no longer name another card, and the docker-provider merge gate works from a Linux checkout.**
  - `super-board-pr-body.sh` refuses (exit 65) a `status` block whose `Closes`, `Fixes`, `Resolves` or `Refs #N` names a different card from the PR branch's `issue-<N>` (`loop/issue-<N>` too). A branch with no issue number is not checked. Lanes of different cards share one scratchpad, and one lane's `status.md` had overwritten another's, so card #224's PR said `Closes #221`. Known limit: only the `status` block is checked (GitHub closes on a `Closes #N` in any block), the keyword has no word boundary (`prefix #5` reads as `fix #5`), and `Closes [#N]` or `owner/repo#N` forms are not caught.
  - `super-board-wave.js`: the lane prompt says to write scratch files (PR body blocks, comment drafts) only under `card-<N>/` in the scratch directory. `run.md` documents exit 65 and the per-card folder.
  - `super-board-git.sh` is mode 100755. `super-board-merge-gate.sh` runs it directly for `merge-tree`. On the docker-provider path (`pilot.sandbox.provider` docker), run from a Linux checkout, the old mode gave `Permission denied` and the gate exited 9 on every merge. Host-provider repos never call it. Installed copies were not affected, because `install.sh` runs `chmod +x` on every `.claude/bin` script.
  - Tests: new foreign-`Closes` cases in `test-pr-body.sh`, and a per-card scratch check in `test-wave-command-shape.sh`. `test-merge-gate.sh` (docker gate) and `test-o1-lifecycle.sh` failed on Linux for the mode alone, and pass with it (WSL Ubuntu run on main `5a3e0db`).
- 📝 **Docs: `hooks/README.md` says `guard-gh-identity.py` reads quoted text too.** A `--body` or commit message that names `gh auth …` or a token variable is denied. Write the text to a file and pass `--body-file` (or `git commit -F`).

## v3.1.3-ams.26

- 🐛 **Lanes run each `gh` or `git` write as its own Bash call, so the allowlist matches it instead of the auto-mode classifier judging (and refusing) a chained call. One lane's malformed GraphQL query no longer halts the whole run.**
  - `super-board-wave.js`: new `COMMAND_SHAPE` text in the lane prompts and the pre-flight prompt. Every GitHub or git write is its own Bash call: no `cd ... &&`, no `;`, `&&`, `||` or `|` chains, no `$(...)`. Use `gh --repo` and `git -C` instead of `cd`. Read the exit code from the tool result. Shell variables do not survive between calls, so pass literal values. A chained call does not match the allowlist, so the classifier judged and refused it.
  - `run-workflow.md` says the allowlist matches the start of a single command, and a chained call falls to the classifier.
  - `super-board-github-read.py`: a GraphQL query-shape error (`undefinedField`, `Cannot query field`, `Syntax Error` and similar) in non-strict mode exits 64 as a caller error with the first error line, and the run keeps going. It is matched only in stderr and the parsed `errors`, not in raw stdout. Access errors (401, 403, 404, bad credentials, missing scopes, unresolvable names) still halt with 79.
  - `--issue-history` and `super-board-deps.sh` (`SB_GITHUB_READ_STRICT=1`) stay strict: their commands are fixed, so a shape error there is a plugin bug and halts.
  - Tests: new `test-wave-command-shape.sh` (the text is in the lane and pre-flight prompts) and new cases in `test_github_reads.py` (caller error exits 64, access error halts, strict paths halt).
- 📝 **Docs: skills say "the operator" instead of a person's name, and the lane skills keep their standalone paths in a reference file.**
  - PR #20: `mission-control` (CORE.md, SKILL.md, routing.md), `block-template.md`, `writing-standard.md`, `run-workflow.md`, the super-build, super-review and ui-refine-loop skills, `families.json` and the README name "the operator". `run.md` now does too, in the duplicate-hold owner and the `Next:` line.
  - PR #21: the standalone (no-board) paths of super-build and super-qa moved from their SKILL.md to `references/standalone.md`; `skill-map.json` and a `super-qa-file-bug.sh` comment point at the new files.

## v3.1.3-ams.25

- 🐛 **A wave no longer halts before any card is built because of a hook false positive, a parked card, or a shared `--files` list.**
  - `guard-gh-identity.py`: the "eval with expansion" rule matches `eval` only as a command word. The word inside a path or name such as `my-eval-fixture` or `tools/eval.py` no longer denies the command. Every real `eval $x`, `a && eval "$b"`, `builtin eval`, `\eval` and quoted `'eval'` form is still denied.
  - `super-board-wave.js`: a Builder card that pre-flight did not clear (parked in Blocked, sequenced or skipped) is not classified, so its classify can no longer set the wave-wide halt. The shared lane prompt says a hook or tool denial is not a halt: retry once with a simpler command. `halted` is only for script exits 9, 10, 11 and 79.
  - `super-board-preflight.sh`: `--files` applies to a single `--issues` only. On a batch it is ignored with a warning and each card's paths come from its body, so one list no longer makes false peer overlaps. run.md and the pre-flight prompt say so.
  - Tests: new allow and deny eval cases in `test-guard-hooks.sh`, a parked-card scenario in `test-workflow-halt.sh`, scenario 14 in `test-preflight.sh` and scenario 12 in `test-wave-preflight.sh`.

## v3.1.3-ams.24

- 🐛 **`super-board-git.sh --robot` and `--robot-read` reach GitHub again, so `tainted` no longer fails closed on every card and QA can test.**
  - Since 3.1.3-ams.21 both modes passed `-c http.sslCAInfo= -c http.proxy= -c http.sslCert= -c http.sslKey=`. An empty `http.sslCAInfo` breaks TLS (schannel: `failed to open CA file ''`; OpenSSL: `error adding trust anchors from file:`), and an empty `http.sslCert` makes git ask for a client-cert password with no host (`refusing to work with credential missing host field`). Every robot read and every robot push failed, on Windows and on Linux. The tests used a stub git, so they never saw it.
  - The blanks were also not a real control: a repo `http.<url>.proxy` beats `-c http.proxy=`, and `remote.origin.proxy` beats `http.proxy`.
  - Now both modes refuse (exit 64) when the repo config has any `http.*` key (including `http.<url>.*` and included files) or `remote.origin.proxy` / `proxyAuthMethod`, the same way they already refuse `remote.origin.pushurl` and `url.*.insteadOf`. The empty `-c` overrides are gone. Global and system config were already excluded.

## v3.1.3-ams.23

- 🔒 **Lane isolation, waves 5 to 7 (issue #2): external coding tools run on the Lenovo only, guard hooks keep them and their work out of guarded repos, and tests prove no tool sha reaches a host tree.**
  - `lane-delegate delegate --tool codex|grok|pi --mode write|review` needs `pilot.sandbox.provider` docker with `where: lenovo` (exit 78 otherwise), a `base_branch`, and HEAD live on origin (exit 64, "push your branch first"). It dispatches a `lane=delegate` job.
  - Write mode validates the patch like `commit-onto`, builds the commit as objects only, writes taint markers, pushes to `mc-delegate/<nonce>`, and opens a draft PR into `base_branch` with the robot `gh`. Review mode returns the report framed as UNTRUSTED with a frame nonce known only to the Surface, folds look-alike and invisible characters, neutralises markers and caps it at 32 KB.
  - `delegate discard` refuses a merged PR. With no PR it deletes the ref only when the local marker exists, and drops the marker only after GitHub confirms a 404.
  - New hooks: `guard-external-tools.py` (user scope) denies external coding CLIs, their packages, wrappers and MCP servers in guarded repos. `guard-taint.py` (project scope) blocks pulling, merging or checking out tainted delegate work into a trusted checkout. `guard-config-write.py` protects `~/.mission-control`, the `~/.claude` settings and `super-board/active`.
  - `install.sh --guard-repo owner/name` writes the guarded-repos list and proves the match. `sb-python.sh` launches Python by absolute path with `-I -X utf8`, skipping the WindowsApps stub.
  - Tests: Docker smoke S19 (a detached child dies with teardown) and S20 (escape attempts from the run container all fail), with `MCL_SMOKE_ONLY` to run chosen scenarios. `test-o1-lifecycle.sh` records every git call through a shim across run, verify, commit-onto, exec, the merge gate and delegate, and fails if any tree-touching call uses a sha not on `origin/<base>`. A static check keeps checkout, switch and worktree add out of the guarded-path markers in the gate and in lane-delegate.

## v3.1.3-ams.22

- 🔒 **Lane isolation, waves 3 and 4 (issue #2): the merge gate and the lanes never put tool-written code on disk in a guarded repo.**
  - The merge gate, with `pilot.sandbox.provider` docker, finds conflicts with `merge-tree --write-tree` through `super-board-git.sh` (exit 5) and verifies the PR and the base check on the Lenovo only (`--gate-head` / `--gate-base`). It makes no worktree, checkout or merge on the host. `where` other than `lenovo` on a guarded repo is exit 9.
  - The gate reads `pilot.sandbox.provider` fail closed: only `docker`, `host`, a missing section, or an object with no provider and no docker keys pass. Anything else is a human merge (exit 7). lane-delegate refuses `--gate-head` / `--gate-base` unless the provider is docker, so a config changed under the gate cannot verify the wrong tree.
  - run.md has a new "Tainted cards" section, and the Build, QA and Review skills, the wave and Codex lane prompts and git-sync follow it. Each lane first checks taint (`super-board-objects.py tainted`, fail closed). On a tainted card the scope comes from the PR `headRefOid`, tests run through `exec --sandbox --ref`, commits go through `commit-onto`, the gate pins the reviewed sha, and gate exits 2 and 5 move the card to Blocked for the operator. Untainted cards work as before.
  - `config-schema.json` documents `pilot.sandbox` and `pilot.tools`. `test-move-docs.sh` fails when a lane doc or prompt diffs `HEAD` or checks out a card branch without the tainted-card rule.

## v3.1.3-ams.21

- 🔒 **Lane isolation, waves 1 and 2 (issue #2): tool-written code never lands in or runs from a host checkout of a guarded repo.**
  - `super-board-git.sh` is a hardened git wrapper. `--robot` pushes exactly one sha to a validated, non-protected branch; `--robot-read` allows only `ls-remote origin refs/heads/NAME` and `fetch` of one sha or branch. The host-tests hook denies both in hot contexts.
  - `super-board-objects.py` builds tool commits as objects only (temp index, `read-tree`, `apply --cached`, `write-tree`, `commit-tree`), so no patch is ever checked out on the host. Taint markers record which cards and delegate runs carried tool content.
  - `lane-delegate verify` and the new `commit-onto` build those commits and push them through the wrapper. The card branch must be `issue-<card>`, protected branches (main, master, staging, `base_branch`, merge and deploy targets) are refused, a moved branch tip is exit 64, and `commit-onto --expect-head` refuses a PR whose head moved.
  - Guarded repos (D-5) come from `%USERPROFILE%\.mission-control\guarded-repos.json`. A repo is guarded when any remote or push URL (after the user's `insteadOf` / `pushInsteadOf` rules, on any host) ends in a listed `owner/name`, or when its common git dir is listed. A missing or invalid file guards every repo.
  - `exec --sandbox` on a guarded repo with `where: local` exits 78: its tool code is tested on the Lenovo only. `where: lenovo` needs `--ref SHA --branch NAME`, checks live that the sha is on that origin branch (exit 64 otherwise or on a race), and dispatches the new `lane=exec` job. The command runs there as an argv list with no shell, and only `exec.log` and `exec.json` come back. `--ref` also works locally on an unguarded repo.
  - Until `install.sh --guard-repo` (a later wave) writes the list, local `exec --sandbox` exits 78 for every repo, by design.

## v3.1.3-ams.20

- ✨ **Done closes the issue, and a `released` label marks what reached the default branch.**
  - `super-board-card.sh move <issue> Done` now needs `--pr 50` or `--no-pr "reason"` (exactly one, Done only). With `--pr`, the PR must be MERGED into the config's `base_branch` and have a merge commit, or the move exits 64 and changes nothing.
  - After the Status is set, card.sh posts one `sb:done-closed` marker comment (never twice) and closes the issue as completed. If that fails after the move, it says so on stderr and exits 69.
  - A new config key, `protected_issues` (array of integers, default `[]`), makes `move` exit 64 and `release` skip those issues.
  - A new `release 60 [--dry-run]` verb runs after a person merges the release PR from `base_branch` into the default branch. It adds the `released` label (created when missing) to every closed issue whose marker sha is in that PR's commits. It refuses a PR with 250 or more commits, because GitHub caps that list.
  - A Done move also needs a config with `base_branch` (exit 66 without), the PR must belong to the issue (a closing reference, an `issue-N` branch, or `#N` as a whole token in its body or title), and the issue number must be a plain integer. `--pr` accepts `50`, `#50` and a PR URL. The marker check is exact, so a reopened issue that is done again with a PR gets its sha marker.
  - `audit` also lists open issues whose card is in Done as `Done missing: closed`; re-running the same Done move closes them. Release PRs into the default branch must be merge commits, not squash, or each release soon hits the 250-commit cap.
  - Drop paths close the issue as not planned first, then move to Done with `--no-pr "dropped: reason"`.
  - The review, lint and wave prompts and the docs carry the new flags.
  - `move --tool grok|pi|codex` names a delegate tool as the Agent (`Grok · super-build`, `Pi · super-build`) in place of `--model`. A missing Agent option is now an `error:` line on stderr and in the card comment that says never to add it through the API, because that wipes every card's Agent value.
  - A move into Building or Review of an issue with no milestone now warns on stderr (the Roadmap counts issues per milestone); the exit code is unchanged. Issues labelled `needs-grilling` and the config's `roadmap.issue` are exempt.

## v3.1.3-ams.19

- ✨ **Agent, Skills and the routing label are mandatory on a card that is worked.**
  - `super-board-card.sh move` into Building, QA, Review, Blocked or Done refuses (exit 64) without `--lane` and `--model`.
  - A move into a lane column also needs a `bug`, `feature` or `qa` label, or `--kind` to add one.
  - A missing Agent option on the board now posts one comment on the card instead of passing silently.
  - A new `audit` verb lists every card in a lane column that lacks Agent, Skills or a label. Each wave runs it and reports those cards, and the Roadmap issue shows them too.
  - `setup.py check` lists the Agent options the board still needs.
  - The AGENTS.md block tells every session to move cards only through the helper. The Card type field is still never written.

## v3.1.3-ams.18

- 🐛 **Onboard's upgrade step works on Windows.** `super-board-setup.py fix` started `bash` by name, and Windows found the WSL launcher in System32 before Git Bash, so `install.sh` failed with a broken `C:` path. It now walks PATH itself, skips the System32 and WindowsApps WSL launchers, and passes forward-slash paths to bash on Windows.

## v3.1.3-ams.17

- ✨ **A pinned 🗺️ Roadmap issue shows where the project stands.** Each GitHub milestone is a phase, in milestone order. The issue shows a Mermaid flowchart of the phases and a table with each phase's percent done and card counts by column, plus the cards in flight, the blocked ones and the next ones up. Onboard sets it up on a new install and on an upgrade, and proposes phases when the repo has none. After every wave the board refreshes it. A roadmap failure never halts a run, text outside the markers stays the owner's, and a lost config number never creates a second issue.

## v3.1.3-ams.16

- 🐛 **Lane agents are told the GitHub repository.** A Review agent once took the repo name from the working folder instead of the real repository, GitHub rejected the read and the run halted. The orchestrator now passes the config's `repo.remote` to the wave workflow as `repo` (owner/name), the workflow refuses to start without it, and every lane, pre-flight and classify prompt states it and forbids guessing it from a folder name.

## v3.1.3-ams.15

- ✨ **A reconciled Done card keeps a truthful Agent.** When the runner moves a closed issue to Done, it now also prefixes the card's current Agent value with "Done · " (for example "Done · Opus · super-build"), best effort: no Agent field, an empty value, a missing option or a write error is logged or skipped and never halts the run, and field options are never created.

## v3.1.3-ams.14

- ✨ **A card move fills the board's Agent and Skills fields.** `super-board-card.sh move <issue> <Status> --lane <build|qa|review> --model <Model>` now also sets `Agent` ("<Model> · super-<lane>", "Done · …" on Done) and `Skills` (the lane's skill ids) in one batched mutation, best effort: a missing field or option warns and never changes the exit code, and field options are never created or edited. The lane skills, the wave workflow and `run.md` pass the flags.
- 🧹 **No residue placeholders in synced code.** Angle-bracket placeholders named N, n, project or date in hooks, scripts, workflows and sandbox files are renamed (`<number>`, `<count>`, `<project-dir>`, `<yyyy-mm-dd>`; regex groups `(?P<num>…)`), because a product gate that greps for them rejected the synced pack. `tests/test-release.sh` now fails on any new hit.
- 🔧 **Re-install replaces widened hook entries.** `super-board-settings.py hooks` removes a plugin hook command left under an older matcher (for example `Bash` after the snippet moved to `Bash|PowerShell`) before adding the new entry, so a hook no longer runs twice. User hooks are never touched.

## v3.1.2 — 2026-10-11

`/super-collect` dedupes on the exact fingerprint and says when Sentry or PostHog is down.

- 🐛 **Exact-fingerprint dedupe.** `super-collect-file.sh` matched fingerprints as substrings,
  so `err|sentry|441` hit the open issue for `err|sentry|4411`. It now matches whole tokens.
- 🐛 **The dry run checks a bug ticket's format.** A plan could say `would-file` for a body
  `--yes` then refused with 66. The dry run now runs the QA filer's checks through the new
  `super-qa-file-bug.sh --check-body`, which validates and exits without calling `gh`.
- 🐛 **Unreachable is `unavailable`.** A DNS failure, refused connection or timeout crashed the
  Sentry and PostHog fetchers; they now print `{"status":"unavailable"}` and exit 2.
- 🐛 **The gh guard sources from zsh.** `super-board-gh-guard.sh` found its own path only
  through `BASH_SOURCE`.
- 📝 **A bare `--title`.** SKILL.md tells the agent to leave out the `🐛 [bug] …:` prefix the
  filers add, so it no longer appears twice.
- 🧪 **Offline tests and evals.** `tests/test-collect-baseline.sh` and
  `tests/test_collect_baseline.py` replay canned Sentry, PostHog and PR payloads in
  `run-safety.sh`; four paid `evals/collect-*` cases cover Sentry verdicts, a duplicate, a
  PostHog tracking gap and a PR pattern.

## v3.1.1 — 2026-10-04

`/super-board run` inside a Codex session runs on Codex instead of failing.

- ✨ **A Codex host means `--codex`.** With no Workflow tool, or when the new
  `scripts/super-board-host.sh` prints `codex`, a plain `run` takes the Codex ladder; `--low`,
  `--high` and `--codex=<model>` still apply. The check reads `CODEX_THREAD_ID` /
  `CODEX_SESSION_ID`, which codex-cli 0.160.0 sets in every shell it runs.
- 📝 **How Codex finds the skill.** Codex has no `/super-board` command: it reads skills from
  `.agents/skills/` and is invoked as `$super-board run`.

## v3.1.0 — 2026-10-04

The board spends about a twentieth of the GitHub budget it did, can run every lane on Codex,
and steps itself down when it hits the hourly limit anyway.

- ⚡ **About 20x fewer GraphQL points.** A new `scripts/super-board-card.sh` moves and reads
  cards with cached project and field ids — 1 point a move, where `gh project item-list`
  plus `field-list` cost ~300 on a 131-card board. Comments, labels and PR reads go over
  REST. Measured before: a 3-card wave spent ~2,850 of 5,000 points/hr.
- 📏 **An honest quota reading.** `gh api rate_limit` misreports the GraphQL bucket (REST
  said used=2 while GraphQL said used=930). `sb_gh_quota_merge` in `super-board-gh-guard.sh`
  takes GraphQL's own `rateLimit` numbers; the guard and the summary use it.
- 🤖 **Second account onboarding.** Onboard offers a robot GitHub account so the board gets
  its own 5,000 points/hr (`references/second-account.md`).
- ✨ **`run --codex[=<model>] [--low|--high]`.** Every lane runs on headless Codex
  (`super-board-codex-wave.sh` → `super-board-codex-lane.sh`), cards in parallel under
  `max_workers`. Ladders: Sol/Sol/Astra by default, Luna/Sol/Sol on `--low`, Astra on
  `--high`; a Luna router grades each Ready card. `--codex=<model>` or `codex.model` pins one.
  Review and merge on Codex need the explicit flag; the usage fallback
  (`usage_fallback: "codex"`, now required) builds and tests only.
- 🐢 **Throttle.** `super-board-throttle.sh` lowers `max_workers` after a wave that hit the
  hourly limit — unlimited → 3 → 2 → 1 — and writes it to the config.
- 🧭 **Preflight sequences overlapping peers.** Two Ready cards naming the same files, with
  no open PR, go one at a time: the lower number builds, the other waits behind it. Before,
  each was sequenced behind the other and neither was built.
- 🗄️ **Migration reach checks.** `migrations.checks[env]` is a read-only probe run before the
  first wave, so a broken database URL halts the run instead of a merge.
- 📝 **Models table** in the README; `--high` is now Opus for every card.

## v3.0.5 — 2026-10-03

Migrating a board no longer leaves its cards with no status.

- 🐛 **Every option rewrite is undone.** Rewriting the Status options gives every option a
  new id and clears the Status of every card. `board-migrate --prune-empty` rewrote them
  without restoring, so a live board's cards all fell to "No status". The restore now runs
  after any rewrite, whatever triggered it.
- 💾 **Snapshot on disk first.** Before the first rewrite, every card's status is saved to
  `.claude/super-board/backup/board-<number>-<ts>.json` (path in `status_backup`), so a run
  that dies mid-way can still be put back.
- 🔎 **Snapshot that cannot be empty by accident.** When `gh project item-list` surfaces no
  `status` key on any card, statuses are read over GraphQL instead; item reads now go past
  500 cards.
- 🧪 **Regressions.** Offline scenarios cover prune-empty on a board in use, the GraphQL
  fallback, and a dry run that writes nothing.

## v3.0.4 — 2026-10-03

The skill map reads from left to right, with nested skill families and clearer drill-down
views. The landing board and impeccable map follow the same visual flow.

- 🗺️ **Skill map redesign.** Nested families group related skills; main-step views reveal
  the work in order. Click highlights keep a selected path visible, bullet details make
  each node easier to scan, and the column browser opens related views side by side.
- 💄 **Landing board polish.** Column colours distinguish each lane in light and dark
  themes, with Done before Blocked. Skill-map links use the public Pages route.
- ➡️ **Impeccable map.** The main rail reads left to right through setup, planning,
  diagnosis, optional refinements and polish, with utilities in a separate lane.

## v3.0.3 — 2026-10-03

Refreshing a vendored installation now preserves skills and helpers that already point at
the source pack through symlinks.

- 🐛 **Safe local refresh.** `install.sh` recognizes the same source and destination before
  clearing skill contents or copying scripts, workflows, and hooks. Existing links stay intact.
- 🔒 **Overlapping paths refused.** A destination that contains the source pack or overlaps
  its source skill is rejected before any skill is replaced, preventing deletion or recursive copy.
- 🧪 **Installer regressions.** Disposable fixtures cover two layers of skill links, linked
  helper files, repeated refreshes, and both directions of source/destination nesting.

## v3.0.2 — 2026-10-02

A rewritten README, the repo at https://github.com/Shahat87/ams-mission-kit, and map
Source links that work away from a checkout.

- 📝 **README rewrite.** Kanban logo and pitch "Add tasks, walk away, get merged PRs with proof.",
  true counts (7 skills: 4 you type, 3 the board runs · 8 commands · 6 guard hooks), the skill map
  as the hero image, a link to the live site, install by one-liner or plugin, a three-step quick
  start and a commands cheat sheet. The old Setup notes moved to `docs/super-board/README.md`.
- 🌐 **GitHub Pages site.** `.github/workflows/pages.yml` publishes only the public pages: a
  landing page (`/`) with the live skill map, a one-card board walkthrough, the 7 skills and the
  install tabs; `/skill-map/`; `/onboarding/`, a step-by-step setup simulator for the 3.0.2
  install and 8-step onboard; `/impeccable/`; and `/writing-standard/`, a read-only reference
  built from the chosen formats. Pick-a-design pages stay out of the site.
- ♻️ **Skill groups.** `skills/families.json` now groups skills as "You type" (super-board,
  super-collect, ui-refine-loop, visual) and "The board runs" (super-build, super-qa,
  super-review). `super-board-readme-sync.py` keeps every "N skills (N you type, N the board
  runs)" count and the family titles in step.
- ✨ **`/visual render --source-base URL [--source-root DIR]`.** Node sources under the source root
  become links to the hosted file (`:12-20` → `#L12-L20`); others show as text. The skill-map
  pages are re-rendered against `https://github.com/Shahat87/ams-mission-kit/blob/main/`.
- 🧪 `test-readme-sync.sh` no longer needs a skills badge, and checks both counts and the family
  titles after a sync.

## v3.0.1 — 2026-10-02

Fixes the red Linux CI on v3.0.0 and stops diagram pages from publishing local machine paths.

- 🐛 **CI on Ubuntu.** `tests/test-setup.sh` hid `gh` with `PATH=/usr/bin:/bin`, but GitHub's
  ubuntu runners ship `gh` in `/usr/bin`. The test now runs the check on a curated PATH (git and
  the package manager only), so a missing `gh` is reported on every OS.
- 🐛 **Merge gate on bash 4+.** `super-board-merge-gate.sh` used `${#VERIFY[@]:-0}`, a bad
  substitution on bash 4+ (Linux), so every merge with a matching head failed. Now `${#VERIFY[@]}`.
- 🔒 **Repo-relative diagram sources.** `/visual` wrote absolute paths (home folder, worktrees,
  plugin cache) into map pages. Sources are now repo-relative (`~/…` outside the repo), the page
  stores the repo root relative to itself, and the skill-map pages are re-rendered.

## v3.0.0 — 2026-10-02

Onboarding rebuilt as an 8-step wizard, one board shape for every project, and labels that
route cards. **Breaking:** the Skipped column, the per-board `variant`, the goal question and
URL-only boards are gone; commits, tickets, PR bodies and comments follow the writing standard.

### Breaking

- **No Skipped column.** Every board is Backlog · Ready · Building · QA · Review · Blocked · Done.
  A card dropped on purpose is closed as not planned and moved to Done with a `🤷 dropped`
  comment. The run's done condition, the landed-work signal, status, lint and the block template
  no longer know Skipped.
- **Labels route cards, not `variant`.** Three labels: `qa` (Ready → QA → Review → Done, skips
  Building), `bug` and `feature` (built first). No label is built too, and the haiku classifier
  adds `feature` or `bug` — never `qa`. `super-board-wave-plan.sh` puts `lane` and `labels` on
  every card; `super-board-wave.js` and the legacy runner route on them. A config that still says
  `variant: "qa-only"` is refused (exit 65) until onboard upgrades it.
- **No goal question, no URL-only board.** Onboard always sets up a repo board. Testing a live
  site alone is `/super-qa <url>`.
- **Writing standard.** Commits `<emoji> [type] scope: subject` + short bullets; tickets Problem ·
  Context · Fix · Acceptance Criteria · Risk · Blocked by; PR bodies in marker blocks; comments
  `[role] [label] status` (references/writing-standard.md).

### Onboarding

**8 steps, each one question.** 🔍 Checks · 🔑 GitHub · 🗂️ Board · 🌿 Branch · 📜 AGENTS.md ·
🛡️ Policies · 📥 Bug sources · ✅ Review. Each opens with a numbered agenda strip and
`N of 8 · <emoji> <Name> — <why>`; the start screen lists the steps; a re-run shows the saved
answers and continues.

**Checks fixes, it doesn't ask.** `scripts/super-board-setup.py fix` installs or refreshes skills,
scripts, workflows, guard hooks and their settings entries, removes the super-refine / cleanup-wt
/ arch-loop folders, migrates config keys, runs `git init` and adds Matt Pocock's skills — backed
up first, listed under "Fixed for you". Only a system tool install (the exact brew / apt / dnf /
winget command) or a sign-in is asked, through Claude Code's permission prompt; it re-checks until
green.

**Board.** `board-rank` lists your GitHub Projects ranked by matching columns and recommends the
best (≥ 4 of 7); `board-migrate` adds the missing columns and the three labels and keeps every
card. A new board gets two names from package.json / README / the folder, or one you type.

**Branch.** No staging → "Create staging from main" (Recommended) runs `git push origin main:staging`.

**Policies** is two questions (safe defaults; N permission lines), details folded.
**Bug sources** adds "➕ Add another source" (below). **Review** is one compact table and
"Write everything?"; files change once, there. **Done** is three lines and a 🎉.

**🙋 Needs you, checklist first.** The comment opens with "🙋 Your turn on #N — title", the exact
commands as checkboxes and "Comment done here"; why and evidence fold under "Why, and what I
checked".

### super-collect custom sources

Onboard's "➕ Add another source" takes a link, app name, API URL, MCP server or CLI command.
`collect_custom.py classify` decides which, `ping` proves it is readable (read-only; commands that
change something are refused), `add` saves `{name, kind, target, auth_env?, map?}` to
`collect.custom[]`. `/super-collect` runs them (`list`; MCP reads go through the agent and
`normalize`), turns the output into candidates with fingerprint `custom|<name>|<key>`, and sends
them through the same verifier and filer (`--source custom`).

### Install output

`get.sh` and `install.sh` print grouped lines with emojis (🧩 🔍 📦 🔧 🧠 🎉 👉) instead of one line
per file; failures still name the file. `install.sh` records an older install in
`.claude/super-board/upgrade.json` for onboard.

### Upgrading from 2.x

> [!WARNING]
> `board-migrate` (step 2's board changes) is tested only against a stubbed `gh`, never a live
> GitHub project. Try it on a throwaway board first: copy your project, run
> `python3 .claude/bin/super-board-setup.py board-migrate --owner <o> --number <copy> --repo <r> --dry-run`,
> then without `--dry-run`, and check the columns, labels and card statuses before you run
> onboard on the real board.

1. Install over the old copy: `curl -fsSL https://raw.githubusercontent.com/Shahat87/ams-mission-kit/main/plugin/get.sh | bash`
   (or `./install.sh <project>`). It records the version you came from.
2. Run `/super-board onboard`. Step 1 upgrades with no question and lists it under
   "Upgraded for you" (backup in `.claude/super-board/backup/<ts>/`):
   - removes `.claude/skills/super-refine`, `cleanup-wt`, `arch-loop`;
   - config: drops `variant` (a "qa-only" board's cards get the `qa` label), sets the seven
     `columns`, turns a URL-only `target` into a repo target, replaces the old `collect` keys
     (`errors`, `feedback_paths`, `lookback_runs`) with `sources`, drops
     `refine.qa_hook_rounds`, adds `merge_policy` (from `human_approves_merge`), `migrations`,
     `timezone`, `worker_backend`, and sets `notifications.channel` to `session`;
   - board: adds missing columns, creates `qa` · `bug` · `feature`, maps old labels
     (build → feature, bug-fix → bug, qa-only → qa), moves Skipped cards to Done and removes the
     Skipped option. Card statuses the change clears are put back.
3. Your CLAUDE.md rules stay where they are; step 5 offers to move them into AGENTS.md.
4. A run started on an unmigrated `qa-only` config stops with exit 65 and says to run onboard.

### Tests

New `tests/test-setup.sh` (9: must-have fixes, upgrade detection and migration, idempotent config
migration, tool commands, board names, staging, board ranking and migration with a stateful `gh`
stub) and `tests/test_collect_custom.py` (17). Label routing in `test-wave-plan.sh`,
`test-wave-preflight.sh` (10) and `test-run-gates.sh`; grouped output in `test-install.sh` (13)
and `test-get.sh` (9); the 🙋 checklist in `test_writing_format.py` (14).

## v2.6.0 — 2026-10-02

Cleanup release: one renamed skill, one skill turned into a hook, super-collect rebuilt, arch-loop
removed, three new guards, two new evals and a sweep of stale docs.

### Skills

**ui-refine-loop, renamed from super-refine and rebuilt on Impeccable.** A standalone helper
(secondary) under `/ui-refine-loop`; the board no longer runs it (run.md Tester step 5b, the QA
lane prompt hook and `references/qa-hook.md` are gone; `refine.qa_hook_rounds` is ignored). It
reads the target's code, grills at most 10 questions in waves into a per-project taste file
(`docs/design/taste.md`, from a neutral default), then runs bounded rounds: an isolated design
reviewer and detector, a checker that ranks typed problems, and a fixer that routes each type to
an Impeccable command (polish last). Shots cover light and dark at 1440 and 390, with section
crops and before | after sheets. The score is design /20 + audit /20, not Nielsen. Default 5
rounds, stopping after two checks with no P0/P1. A finish reviewer grades each fix, and it ends in
a draft PR with a Before | After table (raw GitHub URLs). Impeccable v4.0.x (`node
scripts/detect.mjs`) and v4.4+ (`scripts/impeccable`) are both detected, parent dirs included; a
missing install now warns loudly instead of silently falling back to the rubric.

**cleanup-wt is a hook now, not a skill.** The script moved to `hooks/cleanup-wt.py` and ships
with the guard hooks. The merge gate still runs it with `--post-merge` after every merge, and the
`SessionStart` `--auto` sweep is now part of the default `hooks/settings-snippet.json` (it was an
opt-in block). It leaves the skill tables and is listed under Guards. An old install's
`.claude/skills/cleanup-wt/` can be deleted by hand.

**super-collect, rebuilt as source plug-ins.** One job: find problems and file them into the
project's Backlog. Sources `sentry` (REST), `posthog` (HogQL; exceptions, failure events, rage and
dead clicks, web vitals, surveys, tracking gaps), `github` (unboarded issues, adopted), `prs`
(recurring problems across merged PRs' comments, reviews and super-review reports, one GraphQL
search) and `architecture`; run one (`/super-collect sentry`) or all. `--since` picks the window
(default 14 days). Each candidate goes to one fresh verifier (real, still happening, already
fixed, duplicate); unclear ones file as `needs-triage`. The `intake`/`lookback` modes, feedback
folders and run-file lookback are gone; the config's `collect` block is replaced (sources,
sentry/posthog IDs and thresholds; secrets stay in `.env`), and onboard gains step 11b to set it
up and test each connection read-only. Tests: `tests/test-collect-fetchers.sh` (new, stubbed
HTTP), `tests/test-collect-file.sh` 30 → 47 cases.

**arch-loop folded into /super-collect architecture.** The skill is removed; architecture findings
are now read-only refactor tickets in Backlog instead of an implement loop.

### Guards

**No deleting outside the project (on by default).** `hooks/guard-delete-outside.py` denies `rm`,
`rmdir`, `unlink`, `find … -delete` / `-exec rm` and `git clean` aimed outside
`$CLAUDE_PROJECT_DIR`, and always `/` or `~`. Temp dirs stay deletable inside.

**Protected-branch push guard (opt-in).** `hooks/guard-protected-push.py` denies direct and force
pushes (`-f`, `--force`, `--force-with-lease`, `+ref`, `--delete`, `--all`/`--mirror`) to main,
master and the config's `base_branch`; force pushes to feature branches stay allowed.
`super-board onboard` asks once (step 9b) and `install.sh --protect-main` wires it
(`hooks/settings-protect-main.json`).

**Skill-eval gate (pack and host repo only).** `hooks/dev/gate-skill-evals.py`, a Stop hook adapted
from the starter kit's `gate-skill-evals.sh`: a skill changed this session (uncommitted or
unpushed) with no newer `claude plugin eval` result blocks the stop and names the command. A changed
reference or workflow counts only for a skill with an eval case; a `SKILL.md` with no case only
warns. Never installed into targets; wired in this pack's `.claude/settings.json`.

Tests: `tests/test-guard-hooks.sh` grows from 25 to 77 cases; `tests/test-install.sh` gains
scenario 10 (`--protect-main`) and checks that `hooks/dev/` is never installed.

### Evals

Two new `claude plugin eval` cases beside `review-remembers`, same offline `gh` stub and scaffold:

- **`ponytail-overengineering`** — a correct, tested PR wraps a one-line price formatter in a
  strategy class, registry, factory and JSON config. super-review must report an Over-engineering
  finding routed to Super Build and still call the PR merge-ready. 3/3 runs passed ($0.82).
- **`test-gap`** — AC 2 of a slugify PR (≤ 50 characters) has no test. super-qa's test-gap check
  must rank it High and write the test red-first or bounce to Build. 3/3 runs passed ($1.21).

The `gh` stub now serves a per-case `issue.json` for `gh issue view`.

### Stale docs swept

- **Telegram** is gone from every lane and reference: super-board sends no messages of its own.
  `notifications.channel` is now `"session"` and `chat_id` is reserved (config-schema, onboard
  step 10, run.md block-rate alert, run-workflow.md wave report, super-build, super-qa, super-review).
- **super-qa standalone loop** no longer calls `scripts/super-qa-dispatch.sh`, which never shipped:
  each iteration is a sub-agent launched in-session, with the same 0/2/3/4/5 statuses. Dropped the
  missing `docs/super-orchestrator/STAGING-ENV.md` link, the leftover app-specific spec list, and
  "phone-only via Telegram".
- **super-ux** (never a skill) → `ui-refine-loop` as a suggested owner in super-qa, its preamble and
  `super-qa-file-bug.sh --suggested-skill`.
- **super-board SKILL.md** said `run` is headless via `super-board-run.sh`; it is in-session by
  default. Removed every pointer to `docs/specs/2026-05-21-super-board-design.md`, which does not
  ship (SKILL.md, onboard, lint, status, stop, run, block-template, config-schema), and the
  `super-work-trader` line. run.md now says it holds the lane lifecycles plus the legacy runner.
- **super-build** dispatcher path is `.claude/skills/super-build/scripts/super-build-dispatch.sh`;
  removed the old example-board name. super-review is "the super-board reviewer", not the host repo's.
- **CLAUDE.md**: eight skills, not nine; product work goes to workflow lane agents, not only
  `claude -p`; the install contract matches what `install.sh` copies; script paths are `.claude/bin/`.
- Version 2.6.0 in `VERSION`, `skills/super-board/VERSION`, `plugin.json` and the README badge.

### Also

**Onboarding rebuilt (16 gaps).** Install and workflow-runtime checks run first (step 2), so a
missing `super-board-wave.js` halts before any question; a plugin install (`/super-board:super-board
onboard`) detects the missing bin scripts, workflows, hooks and Matt Pocock's skills and installs
them from the plugin's own copy. Every question leads with a `(Recommended)` option. Answers save
to `.claude/super-board/onboard-answers.json` as they come: a halt resumes where it stopped, and a
re-run offers keep all (check and repair) or edit which. Merge policy, protect main (asked once;
with `--no-hooks` it is never wired to a missing script) and migration envs share one Policies
screen with "Accept all recommended". The allowlist (merge gate, `gh pr merge`, migrate commands)
is offered as a diff up front. Collect sources are pinged before one review screen, and the config
is written once. URL-only boards skip git; option D is check and repair; the ticket format lives
once, in `docs/agents/issue-tracker.md`. `install.sh` ends with one line: run `/super-board onboard`.

**AGENTS.md as the source of truth.** Onboard asks to merge CLAUDE.md into AGENTS.md (backup
first; rule units, duplicates once, conflicts asked one by one quoting both lines, every unit
mapped to a line, diff and approve) and leaves CLAUDE.md as `@AGENTS.md` plus any Claude-only
rules. A managed super-board section (tables, terse, NEVER/DON'T in caps, which skill when) sits
between `<!-- super-board:begin -->` / `<!-- super-board:end -->`; re-install rewrites only that
block, and a CLAUDE.md that is no longer a pointer gets the merge offered again. Deterministic
parts in `scripts/super-board-agents-md.py` (detect, backup, block, pointer, units, coverage,
check, atomic writes).

**Merge policy.** New `merge_policy`: normal changes auto-merge; money, auth and destructive schema
(labels, path globs, added-line keywords, all configurable) and diffs over `auto_max_lines` always
go to a human. The merge gate decides (`scripts/super-board-merge-policy.py`) and exits 7; the card
goes to Blocked with 🙋, and the human merges it or comments `done` to approve.
`auto_max_lines` defaults to 400 changed lines (additions + deletions; `0` turns it off); lockfiles,
generated files and snapshots (`size_exclude`, configurable) and migration SQL (reported
separately) do not count. Over the cap → 🙋 "big PR — please review". "Schema" means destructive
only (DROP / TRUNCATE / RENAME, or the `schema` label); additive migrations follow the
`migrations` database rule, and a live database is always human.

**Small PRs by design.** Lint criterion 15 and the Builder pre-flight flag a ticket likely to exceed
~400 changed lines or spanning many areas as "too big": held with ❓, split via `/to-tickets` into
vertical slices. The Builder keeps each PR under the cap; growing past it mid-build, it stops and
proposes the split in a PR comment instead of pushing on.

**DB migrations at merge.** New `migrations` block: onboard asks which databases the robot may
migrate (test / staging / live, default test + staging). A PR touching the migration globs gets
the configured migrate command run against each allowed env inside the merge lock, after verify.
A target env that is not allowed, a failed command, or a declared human step (`needs-you:` in the
PR body, `migrations.human_steps`) exits 8: Blocked with 🙋 and the exact commands. A `done`
comment or the `needs-you:done` label puts the card in the planner's new `resume` list → Review →
the gate re-verifies and merges.

**🙋 Needs you** reason tag in `block-template.md`, for any step only a human may run. There is no
extra column: Blocked + 🙋 + the `needs-you` label.

**Helpers.** `scripts/super-board-env-check.sh` prints present / empty / missing per env key and
never a value; `guard-secrets.py` allows a plain call to it. `scripts/super-board-settings.py`
merges hooks and permission rules into settings.json (backup, atomic, `--dry-run` diff);
`install.sh` uses it, and `--no-hooks --protect-main` now installs only the push guard.

Tests: new `test-env-check.sh`, `test-agents-md.sh`, `test-settings.sh`; `test-merge-gate.sh`
13 → 26 scenarios, `test-deps.sh` 17 → 18, `test-wave-plan.sh` 17 → 18, `test-install.sh` 10 → 12,
`test-guard-hooks.sh` 77 → 82 cases.

## v2.5.0 — 2026-10-02

Review remembers, sharper lanes, five new skills, guard hooks, and a pack that installs and
documents itself.

### Review

**Review remembers.** A card rebuilt after a Review bounce used to meet a reviewer with no memory of
the last pass: settled points were re-litigated, and a builder could resolve a thread without
fixing the code. The Reviewer now loads `prior_report`, the newest PR comment carrying
`<!-- super-review:report -->` (one `gh pr view` call). Round 1 marks each prior finding `fixed`,
`not fixed` or `no longer applies` with the file:line that proves it; a resolved thread is not
proof. Any `not fixed` bounces the card again and the fresh pass is skipped; all clear → the normal
review runs as round 2 (run.md → Reviewer step 3b). Every Reviewer exit from step 3b posts a report
with stable finding ids (`R1`, `R2`, …) carried across rounds. No report means a first review,
unchanged. The Builder side only gains a note that a re-opened thread was resolved without a fix.
The wave's Review prompt carries the step and `STAGE_SCHEMA` gains an optional `priorFindings`
count. Test: `tests/test-wave-review-memory.sh` (also the wave script's syntax check: plain
`node --check` rejects a workflow body's top-level `return`).

**Eval.** `evals/review-remembers/` runs the behaviour end to end with `claude plugin eval`: an
offline `gh` stub serves a PR whose prior report lists R1 (really fixed) and R2 (thread resolved,
code unchanged). The reviewer must load the report, mark R1 fixed and R2 not fixed with file:line
proof, bounce, and never call `gh pr merge`. 3/3 runs passed on release. See `evals/README.md`.

**Fairer review.** The Reviewer reads the ACs and the raw diff and writes 2-4 hypotheses before it
reads the builder's summary, then checks the builder's claims the same way. Findings are classed
Gap / Bug / Verification miss / Scope drift / Over-engineering. Checks that held go under
`Verified`, coverage gaps under `Not verified`, and `Next` names the owner. Marker, R-ids and Prior
findings are unchanged.

**Merge pinned to the reviewed head.** The Reviewer records `headRefOid` when review passes and
passes it to `super-board-merge-gate.sh --expect-head`. The gate verifies that commit and merges
with `--match-head-commit`. A push after review, or during verify, exits 6: the evidence is void
and the card stays in Review. Tests 8-11 in `tests/test-merge-gate.sh`.

### Lanes

**Docs before outside-tool code.** When a ticket touches a third-party API or SDK, an upgrade, or
auth/billing, the Builder reads the current official docs for the installed version (context7,
else the vendor's site) first and cites them in a `Docs consulted` PR section. Unreachable docs are
named and the code they cover is marked unverified (run.md Builder step 3b, super-build).

**Simplest solution first.** Builders invoke `ponytail:ponytail` (full) before the first line of
code on every type label, or a five-line inline ladder when the plugin is absent
(decision-policy.md → "Simplest solution first"); it never cuts validation, security, data-loss
protection or accessibility. The Reviewer runs `ponytail:ponytail-review` on the merge-base diff;
an **Over-engineering** finding is Should fix, routed to Builder, and never blocks or bounces alone.

**Test-gap check in QA.** Before its test run the Tester maps every AC to unit / component / e2e
tests, marks edge cases with exact witness values, names weak tests and surviving mutants, and
ranks gaps (folded from post-tdd). High gaps are written red-first through `tdd`; one that needs
app code changed bounces to Builder. Medium/Low are listed and never block (run.md Tester step 4b).

**UI polish in QA.** A UI card (label `ui`, `design` or `frontend`, or a visual AC) whose AC tests
passed now gets ui-refine-loop in qa-hook mode, 3 rounds by default (run.md Tester step 5b,
`skills/ui-refine-loop/references/qa-hook.md`). AC tests re-run after it; red resets to the pre-hook
commit. It never moves the card, comments or blocks. The wave's QA prompt carries the condition.

**Tighter writing.** Lane comments follow five rules (run.md → Commenting cadence): outcome first,
evidence (command, sha, file:line) before prose, what was and was not verified, and a closing
`Next:` naming the owner, in 12 lines or fewer. The PR body opens with a status line pinned to the
head sha and gains `Not verified`. The Block template gains `Evidence`, `Checked` and `Owner`. The
wave report and halt note have fixed short formats; only cards that need a human get their own line.

### Run loop

**Stranded Building cards come back.** The planner reports unclaimed Building cards in `stranded`;
the orchestrator removes the leftover build worktree, keeps the branch, moves the card to Ready and
comments. The legacy dispatcher does the same once at start (`reclaim_stranded_building`), and
Builder step 2 continues on an existing branch. Tests: scenarios 16-17 in `test-wave-plan.sh`, six
checks in `test-run-gates.sh`.

**Usage guard before each wave.** New `scripts/super-board-usage.sh`. Claude Code exposes plan
usage only in the `rate_limits` JSON it pipes to the status line: `record` saves it from your
status-line command, and `check` compares the 5-hour and weekly windows against `usage_pause_pct`
(default 95). At or over it, no new wave launches, the running one finishes, a resume note is
posted and, if a wake tool exists, a re-check is scheduled at the reset. Pro/Max only, fresh only
while an interactive status line records, and an unknown reading never halts a run.
Test: `tests/test-usage.sh`.

**Post-merge cleanup.** After a merge the gate runs `cleanup-wt --post-merge --base <base>` when
`.claude/skills/cleanup-wt/` is installed. Local only; a cleanup failure never changes the exit.
Tests 12-13 in `tests/test-merge-gate.sh`.

**Config.** `config-schema.json` gains optional `refine` (ui-refine-loop settings) and `collect`
(`window_days`, `errors`, `feedback_paths`, `lookback_runs`) blocks.

### New skills

- **super-collect** (primary): files app errors, unboarded issues, feedback and repeat failures
  from past runs into Backlog, deduped, dry-run by default. Adapted from BuilderIO/skills (MIT).
  Test: `tests/test-collect-file.sh`.
- **ui-refine-loop** (primary): unattended critique → refine loop on one page or component, with its
  own worktree, dev server, before/after shots and `workflows/ui-refine-loop.js`. Adapted from an
  earlier internal project. Tests: `tests/test-ui-refine-loop-setup.sh`, `tests/test-ui-refine-loop-workflow.sh`.
- **visual** (secondary): one self-contained HTML page for a branch recap, a plan or a codebase
  map. Adapted from BuilderIO/skills (MIT), diagrams after tt-a1i/archify (MIT).
- **arch-loop** (secondary): architecture review loop, one verified commit per pass.
- **cleanup-wt** (secondary): removes merged worktrees and branches with a recovery TSV.
  Test: `tests/test-cleanup-wt.sh`.

### Guards, install and docs

**Guard hooks.** `hooks/guard-worktree-path.py`, `guard-secrets.py` and `guard-key-literals.py`
(Python stdlib). Test: `tests/test-guard-hooks.sh`.

**install.sh** installs all nine skills, both workflows and the guard hooks, and merges
`hooks/settings-snippet.json` into `.claude/settings.json`: existing keys kept, each command added
once, the file backed up first, invalid JSON left untouched. `--no-hooks` skips the hooks.
`tests/test-install.sh` grows to 9 scenarios. `plugin.json` lists the full pack.

**README.** Rewritten as a short front door; the long material moved to
`docs/super-board/README.md`. Every skill has a README. `scripts/super-board-readme-sync.py`
regenerates the skill tables and counts from SKILL.md frontmatter and `skills/families.json`
(`--check` exits 1 when stale), run by a git pre-commit hook (`hooks/pre-commit-readme.sh`) and a
Claude Code PostToolUse hook (`.claude/settings.json`). Test: `tests/test-readme-sync.sh`.

**Versions.** `skills/super-board/VERSION` was stale at 2.0.0; `VERSION`, `skills/super-board/VERSION`
and `plugin.json` all read 2.5.0.

## v2.4.0 — 2026-08-20

The `Blocked` column stops being a dead end, and nothing merges on GitHub's word.

Everything here came out of one real run in which five cards sat in `Blocked` long after their
blockers had merged, and two PRs that GitHub called mergeable would have turned the base branch red.

**Waves are sized by the dependency graph.** `super-board-wave-plan.sh` now dispatches every Ready
card whose `## Blocked by` issues have all closed, plus whatever is already in flight. `max_workers`
is demoted to an optional throttle — absent or `0` is unlimited, which is the new default. A fixed
cap sized waves badly: on a board where 23 of 32 Ready cards were waiting on an open blocker, it
spent its slots on cards that would hit their own preflight and park.

**The Blocked sweep.** The planner returns a `sweep` list — cards whose blockers have all closed —
and the orchestrator moves them back to `Ready` before launching, so they join that same wave. Cards
gated on a person (`blocked-by: -`) are never swept.

**New: `super-board-deps.sh`.** The dependency primitive. Reads the last `blocked-by:` line from a
card's Block comments, falling back to the body's `## Blocked by` section, and answers
`runnable` / `humanGated` / `parseable` per issue. It is deliberately fail-safe: three shapes that
look like "no blockers" but are not — a missing section, an empty one, and `- None — but #26 must
merge first` — are reported unreadable and flagged rather than guessed at. That third shape is
produced by following the `to-tickets` template, so it is common.

**New: `super-board-merge-gate.sh`.** Takes an atomic `mkdir` merge lock, merges the CURRENT base
into a scratch worktree, runs `config.verify_commands`, and only then squash-merges. Exit codes
route the caller: `2` and `5` mean a rebase pass for the Builder, `3` is a real human block, `4` is
just "someone else is merging". `mergeable: CLEAN` answers only "does the text conflict"; it says
nothing about whether the result compiles.

**The Review lane is no longer serialised.** The promise-chain mutex around the whole Review lane is
gone from `super-board-wave.js`. It guarded the right thing in the wrong place — reading the diff,
rerunning the suite and the truth-check never touch the base branch. The mutex now sits at the merge
step alone, where the race actually is, and is visible across processes and machines.

**Merge conflicts are a rebase pass, not a Blocked card.** `run.md` routes a conflicting or stale
branch back to `Ready` with `loop:rebase`. The Reviewer still never pushes to a branch it is judging
— it hands the work to the lane that is allowed to.

**Lint gains two criteria.** 13: the `## Blocked by` line must be machine-readable. 14: an
acceptance criterion proved against a fake must name the card that builds the real thing, or lint
offers to file it.

### Fixes

- `super-review-file-refactor.sh` and `super-qa-file-bug.sh` filed into hardcoded `Backlog` / `Bug`
  columns. A board without them got cards with **no Status at all** — invisible in the Kanban view,
  not merely misplaced. Both now fall back through the conventional aliases and say which they used.
- `install.sh` handles a `.claude/skills/<name>` that is a **symlink** to a real tree elsewhere,
  resolving it with `cd -P` and writing through it instead of failing with "Not a directory". It
  also refuses any destination not named after the skill, and `test-install.sh` plants decoy
  directories that must survive — because an earlier draft of this resolution deleted 190 of them
  (`cd ""` succeeds in bash, so an empty `readlink` silently resolved to the parent).
- `install.sh` never shipped `super-board-stop.sh`, so a fresh install had a broken `stop` verb.
- README claimed `bash 4+`. Every script here is bash-3.2 clean, which is what stock macOS has.
- README listed the `mattpocock/skills` pack under Credits rather than Requirements, though the lane
  skills reference twelve of them by name and fail without them.

### Tests

`test-deps.sh` (17) and `test-merge-gate.sh` (7) and `test-install.sh` (5) are new.
`test-wave-plan.sh` (15) was rewritten for the new wave contract. `test-file-refactor.sh` gained
five. All offline — no `gh`, no network.

## v2.3.0 — 2026-08-20

**Skills are assigned when the ticket is written, not re-derived at runtime.**
super-build defaulted every card to the same `tdd` + testing set regardless of
what the card was. A docs ticket got told to write a failing test first; a
refactor got no design vocabulary; and `implement` — whose description is
literally "implement a piece of work based on a spec or set of tickets" — never
fired once, because it is `disable-model-invocation: true` and nothing ever
pinned it.

Rule 4 now routes on the issue's **type label**:

| Type label | Loads, in order |
| --- | --- |
| `bug` | `diagnosing-bugs` → `tdd` |
| `feature` | `implement` → `tdd` → `codebase-design` |
| `ux` | `implement` → `tdd` |
| `refactor` / `tech-debt` | `codebase-design` → `tdd` |
| `tests` | `tdd` |
| `docs` | none — skip `tdd`, there is no behaviour to pin |
| *(none)* | `tdd` |

No new label vocabulary: these are the types `super-qa` already files via
`--kind`, plus `refactor` from `super-review`.

- **Order inside a row is load-bearing** — diagnose before writing the red test,
  implement against the spec before reaching for design vocabulary.
- **Always, on top of the row:** `verification-before-completion` before the
  final commit, and one `code-review` pass on the worker's own diff.
- **Test mechanics stay off the label.** `vitest` / `playwright-best-practices`
  are picked by walking the localisation ladder — a `ux` ticket whose defect
  reproduces in a pure function gets a unit test, not a browser spec.
- **Precedence:** an explicit `Skills:` line replaces the row outright rather
  than extending it. Multiple type labels → first matching row, top to bottom.
- `implement` was missing from the Skill map it is now routed to; added.

## v2.2.2 — 2026-08-20

Docs fix, no behaviour change. super-qa cited five `playwright-best-practices`
reference files by bare filename — `locators.md`, `fixtures-hooks.md`,
`test-data.md`, `assertions-waiting.md`, `page-object-model.md`. The skill
(`currents-dev/playwright-best-practices-skill`) ships them under `core/`.

A worker told to read a path that does not resolve reads nothing and falls back
to habit — which is the exact failure the citation exists to prevent. All five
now point at `core/<name>.md`.

The `vitest` citations were checked against the installed skill and are correct:
19 references, and all five named ones (`core-expect`, `features-mocking`,
`features-coverage`, `core-hooks`, `advanced-vi`) exist under `references/`.

## v2.2.1 — 2026-08-20

**`super-qa-file-bug.sh` existed only in prose.** The preamble told workers to
call it, documented its eleven flags, its dedupe policy, and four distinct exit
codes — and the file was never in the repo. Every QA finding that hit that line
died there. It is now written to the spec that was already on the page, and
tested.

- **Body guardrails.** Rejects a body missing any of Summary / Repro steps /
  Expected behavior / Actual behavior / Evidence / Suggested fix path /
  Acceptance criteria, and sweeps for unfilled `TBD`, `TODO:`, and
  `<placeholder>` leftovers outside fenced code — a HAR snippet has angle
  brackets, an unfilled template line has them too, and only one of those should
  block. `SUPER_QA_ALLOW_WEAK_BODY=1` bypasses, as documented.
- **Machinery, not evidence.** The agent writes the findings; the script
  prepends `## Board summary`, appends the hidden `super-qa-meta` block, and
  derives a fingerprint when none is passed. Derivation deliberately excludes
  the iteration number, so the same finding seen on iter 9 dedupes against the
  card filed on iter 3.
- **Project resolution per spec** — `SUPER_QA_PROJECT_OWNER` then the repo
  owner, `SUPER_QA_PROJECT_TITLE` then `Super Ultimate QA`. It halts rather than
  falling back to the repo's primary project, whose columns mean different
  things.
- **The exit-71 contract holds.** When the issue is filed but the board promote
  fails, the number still reaches stdout — the caller logs "manual move
  required" and carries on instead of losing the finding.
- `tests/test-file-bug.sh` — 33 assertions, `gh` stubbed, no network.

**Three doc contradictions the implementation surfaced**, all in
`super-qa/references/iteration-preamble.md`, all resolved toward
`super-qa/SKILL.md` as the more detailed spec:

- Destination column was `Ready` in the preamble and `Bug` in SKILL.md → `Bug`.
- Triage label was `super-qa` in the preamble's carved-exception paragraph and
  `source:qa` everywhere else → `source:qa`.
- The board was named after an earlier project ("… Admin project board (#2)"), a leftover from another
  project → the resolved Super Ultimate QA project.

## v2.2.0 — 2026-08-20

**The advisor panel is removed from super-build and super-qa.** It convened
`mattpocock-skills:grilling` inside a worker whose own preamble forbids asking
the user anything — and grilling's contract is "put each question to them and
wait." In practice the worker either stalled or answered its own questions,
which is inline reasoning wearing a costume.

- **New: the decision ladder.** Acceptance criteria → repo precedent → smallest
  blast radius → human gate. Walk it, stop at the first rung that answers the
  question. Replaces "poll five roles, take the majority, tie → smallest blast
  radius" with the tiebreak that was doing the work anyway.
- **`grilling`, `shape`, and `clarify` are now worker-forbidden** and documented
  as `super-board lint`-only. Reaching for one inside a lane is itself a human
  gate — it means the ticket should have been caught upstream, while a human was
  still at the keyboard.
- **`code-review` survives, with a fixed point.** It runs once against the
  worker's own diff before the final commit, with `git merge-base HEAD
  origin/<base>` supplied so it never prompts. Standards findings are fixed in
  place; a Spec finding that contradicts the issue's AC is a human gate.
- **Commit trailer: `--- decision-vote ---` → `--- decision ---`.** Records the
  question, the choice, and which rung settled it. Only rung-3 decisions need
  one; AC and precedent are their own record.
- **Human gates gained two entries** — public-contract breaks, and "you wanted
  to grill the ticket."

**super-review had no skills at all — now it has two.** The reviewer was running
on its own prompt while every other lane loaded a process stack.

- **`code-review` in the Review lane**, both axes. Standards against the repo's
  documented rules plus the Fowler smell baseline; Spec against the originating
  issue's acceptance criteria. The merge-base is always passed as the fixed point
  so it never stops to ask for one. A Spec finding that contradicts the AC is a
  Blocker.
- **`codebase-design` as review vocabulary.** The reviewer now reads the diff for
  shape — module, interface, depth, seam, adapter, leverage, locality — and applies
  the deletion test to anything shallow the diff adds. Scoped to the diff, never the
  whole codebase.
- **New finding class: deepening opportunity.** It never blocks a merge. A green PR
  does not get held for architecture taste.
- **New: `scripts/super-review-file-refactor.sh`.** Files shape problems as
  `refactor` cards in **Backlog** (not Ready — an unrefined card must not feed the
  build lane), deduped by fingerprint, labelled `source:review` and
  `strength:<strong|worth-exploring|speculative>`. It degrades to warnings on every
  failure past issue-create: a board hiccup must never strand a mergeable PR in
  Review. Covered by `tests/test-file-refactor.sh` (17 assertions, gh stubbed).
- **`improve-codebase-architecture` is documented as lane-forbidden.** It scans the
  whole codebase rather than the diff, writes an HTML report and shells out to
  `open`, then asks which candidate to explore and hands off to `grilling`. It is a
  desk tool — point it at the cards this lane files.

Files touched: `super-build/references/decision-policy.md` (rewritten),
`super-build/references/worker-preamble.md`, `super-build/SKILL.md`,
`super-qa/references/iteration-preamble.md`, `super-review/SKILL.md`,
`scripts/super-review-file-refactor.sh` (new),
`tests/test-file-refactor.sh` (new), `install.sh`, `README.md`.

## v2.1.1 — 2026-08-07

Docs only, no behaviour change. The README explained itself in paragraphs where
a diagram reads faster in a terminal.

- **New "At a glance" table** up top — what it does, what you get, how you
  start, what holds state. Previously you read five paragraphs first.
- **"How it works" leads with the lane pipeline** as an ASCII diagram
  (`Backlog → Ready → Building → QA → Review → Done`, with the bounce-back edge
  and the orchestrator underneath), then the skill table.
- **The testing section is two diagrams instead of two screens of prose** — the
  three layers, then the localisation ladder as a box with "found here" at the
  top and "write it HERE" at the bottom.
- **Backends collapsed into a comparison table** (`workflow` vs `claude-p`).
- **Safety controls became a spawn-gate decision tree** — the six guards drawn
  as the checks a worker passes before it starts, rather than a numbered list.
- **Config keys annotated beside the JSON, not inside it.** The JSON block stays
  valid so it survives copy-paste; annotations sit in a plain block underneath.
- **Variants drawn as their lane strips**, so `full` vs `qa-only` is visible
  instead of described.
- Skill structure and the pattern/conductor note became code blocks.

Prose blocks over 200 characters: 8 → 2. Code fences: 8 → 24.

## v2.1.0 — 2026-08-07

Workers now decide *which layer* a test belongs at, instead of defaulting to the
layer the bug was observed at.

### Testing is three layers, not one skill

`mattpocock-skills:tdd` says how to write a test worth having. It is
deliberately agnostic about the *kind* of test — which meant Testers, who find
every bug through a browser by construction, left every regression as a
Playwright spec.

Three layers now, answering different questions:

| Layer | Question | Skill |
| --- | --- | --- |
| Discipline | How do I write a test worth having? | `mattpocock-skills:tdd` |
| Placement | Which layer does this defect live at? | the localisation ladder |
| Mechanics | How do I express it in this repo? | `vitest` / `playwright-best-practices` |

### The localisation ladder

Walk down, stop at the first rung that still reproduces the failure:

1. Call the module directly with the same inputs → **unit** (Vitest)
2. Wire the real collaborators together → **integration** (Vitest)
3. Drive a real browser → **e2e** (Playwright)
4. Only fails against a live third party → not a test; file a mock/contract gap

Write the test at the rung you **stopped on**, not the rung you **found it on**.
Where a bug was observed says nothing about where it lives. The reason is
locality of failure: an e2e pinning a pure-logic defect is slower, flakier, and
goes red pointing at a page instead of a function, so the next person debugs the
wrong file.

### Two gates before a test counts as done

- **Red for the right reason.** Run against the *unfixed* code and read the
  message — it must name the defect. Red from a timeout or a missing selector
  pins the harness, not the bug.
- **Refactor-survivable.** Rewrite the implementation with behaviour unchanged;
  it must still pass.

### New optional skills

`vitest` (`antfu/skills@vitest`) and `testing-strategy`
(`anthropics/knowledge-work-plugins@testing-strategy`). `testing-strategy`
informs *coverage* — what a component type is worth testing and what to skip —
not placement. Contract testing stays out of the default set: Pact solves
consumer/provider drift across independently deployed services, which a
single-app repo does not have.

### Fixed

- `super-qa`'s frontmatter advertised "builds and runs Playwright path specs",
  framing every regression as e2e regardless of what the references said.
  Crawling is still browser-driven; the regression test it leaves behind is not.

## v2.0.0 — 2026-08-06

Two breaking changes ship together: the worker skill vocabulary moves to the
Matt Pocock stack, and "forward progress" is redefined in terms of landed work.

### BREAKING — workers run on mattpocock/skills

`superpowers:*`, the `gstack` CLI and `gsd-*` are no longer dependencies.
Boards that pin skills in an issue's `Skills:` line must update the names:

| Was | Now |
| --- | --- |
| `superpowers:using-superpowers` | `mattpocock-skills:ask-matt` |
| `superpowers:test-driven-development` | `mattpocock-skills:tdd` |
| `superpowers:systematic-debugging` | `mattpocock-skills:diagnosing-bugs` |
| `superpowers:writing-plans` | `mattpocock-skills:to-spec` |
| `superpowers:brainstorming` | `mattpocock-skills:grilling` |
| `gsd-discuss-phase` | `mattpocock-skills:grilling` |
| `gstack:shape` | `shape` *(kept — no equivalent)* |
| `gstack:clarify` | `clarify` *(kept — no equivalent)* |
| `superpowers:verification-before-completion` | `verification-before-completion` *(kept — no equivalent)* |

The advisor panel no longer shells out to `gstack vote`. It grills the decision
with `mattpocock-skills:grilling`, polls eng via `mattpocock-skills:code-review`,
role-plays the remaining seats inline, and records the result under a
`--- decision-vote ---` commit trailer (was `--- gstack-vote ---`).
`references/gstack-voting.md` is replaced by `references/decision-policy.md`,
which carries the full skill map.

### BREAKING — halt gate measures landed work, not lane occupancy (#8)

The old gate required "no card progressed for 3 ticks" **AND** "no lane active".
Lanes are almost always occupied, so the second clause made the gate
unreachable: one run went 293 ticks over ~6h with zero commits to `main` and
never self-halted. Progress is now a change in the set of cards in
`Done`/`Skipped`, checked every dispatch cycle regardless of lane state.

- New config `no_progress_cycles` (default `6`) — halt after this many cycles
  with nothing landed.
- New config `max_dispatches` (default `0` = unlimited) — hard spend ceiling.
- Both halts print a runaway summary: dispatch count, reap count, cards landed,
  the most re-dispatched issues, and where to find the worker logs.

### Fix: closed issues are no longer re-dispatched (#10)

The loop selected cards by Status column and never checked issue state, so
workers were dispatched at already-CLOSED issues — roughly half of one run's 30
dispatches. Now every dispatch path checks issue state first, and a closed issue
sitting in a non-terminal column is reconciled to `Done` instead of being handed
another worker. Status option IDs for that reconcile are resolved **by name at
runtime** and fail loud on an unknown name, so a column edit can't silently
stale them out.

### Fix: Reviewer merge protocol — Done now means merged (#9)

Builder opens draft PRs and GitHub never auto-merges a draft, so reviewed work
sat on branches: 0 commits to `main` across a 6h window while two complete
builds waited. The Reviewer lifecycle now mandates `gh pr ready` → merge →
**verify the merge commit is an ancestor of the base branch** → only then close
the issue and move the card to Done. A merge-blocked PR routes to `Blocked`
rather than back to `Review`, where it would be re-reviewed indefinitely.
`run-workflow.md` documents the `human_approves_merge` × allowlist matrix that
silently produced the zero-merge night.

### Fix: worker output is captured again (#13)

Workers were spawned with `>/dev/null`, so a 13-minute worker left no trace in
the run log. Output now lands in
`docs/super-board/runs/<date>-<slug>-workers/worker-<lane>-<issue>.log`.
The MSYS-vs-Windows PID namespace trap is documented in the script header —
`kill -0` in Git Bash and PowerShell's `Get-Process` disagree about the same
process, which is what made a live worker look dead during that incident.

### Tests

`tests/test-run-gates.sh` — 28 assertions, deterministic, stubbed `gh`, no
network. Covers the landed-work signal, the halt gate's independence from lane
state, the dispatch ceiling, the closed-issue guard at both the selection and
dispatch choke points, and by-name status-option resolution. `super-board-run.sh`
is now sourceable via `SB_LIB_ONLY=1` for gate testing.

## v1.7.1 — 2026-06-11

### Fix: tolerate JSON-string `args` at wave launch

The Workflow tool can deliver `args` as a JSON-encoded string. The wave script
now normalizes (`JSON.parse` when given a string) before validating, instead of
dying at the args guard. Found live on the magnetgate board.

## v1.7.0 — 2026-06-11

### Model-tier flags for `super-board run`

`super-board run` now takes a model ladder: `--low` (haiku/sonnet/opus by card
complexity), default medium (sonnet/opus/session model), `--high` (opus floor,
session model above). Config key `model_tier` sets the default; the flag wins.
The classify router stays on haiku except on `--high` runs (sonnet). Haiku
never does lane work outside an explicit `--low` run.

## v1.6.0 — 2026-06-10

### Workflow backend is now the default; claude-p is explicit opt-in

`"worker_backend"` now defaults to `"workflow"` — `/super-board run <slug>` drains the board in-session via the `super-board-wave` dynamic workflow unless the config explicitly sets `"claude-p"`. The legacy dispatcher (`super-board-run.sh`) refuses to run (exit 78) for any config that doesn't opt in, so a stale habit or old script can't silently spawn headless `claude -p` workers.

### Hardened mutual exclusion, claims, and crash recovery (PR #3 review findings)

- **Reaper no longer eats the wave lock** — `reap_finished_locks()` skips non-numeric basenames in `inflight/`, so `workflow-wave.lock` survives coexistence instead of being deleted within one tick.
- **Per-tick mutex re-check** — the legacy dispatcher re-checks `workflow-wave.lock` every tick (exit 74), closing the TOCTOU window left by the startup-only check; the workflow side now locks first (atomic noclobber), then looks for a legacy run.
- **Claims are verified** — after `--add-assignee`, the orchestrator re-reads assignees and proceeds only if it's the sole assignee (adding never fails on a contested card, so the add alone is not a mutex).
- **Crash-recovery sweep** — on start, the workflow backend strips leaked bot assignees so a crashed orchestrator can't silently stop the board from draining.
- **Allowlist completeness** — added `gh issue view` / `gh pr view|diff|checks` (the classify and Reviewer prompts require them); documented that auto-merge boards are attended-only unless `gh pr merge` is consciously allowlisted.
- **Loud variant validation** — the wave planner exits 65 on an unknown `variant` instead of silently dropping the QA column.

## v1.5.0 — 2026-06-10

### Dynamic-workflow worker backend

New `worker_backend` config key selects how cards get worked: `"claude-p"` (default, unchanged — headless workers via `super-board-run.sh`) or `"workflow"` (opt-in — waves drained in-session via the `workflows/super-board-wave.js` dynamic workflow).

- **In-session waves** — `workflows/super-board-wave.js` runs a classify → build → qa → review pipeline per card. Lane lifecycles, branch/PR model, and Block templates are unchanged from `references/run.md`; only the dispatcher differs. See `skills/super-board/references/run-workflow.md`.
- **Backlog-aware wave selection** — `scripts/super-board-wave-plan.sh` picks one card per non-empty column downstream-first (Review → QA → Ready), then fills the remaining `max_workers` slots from the most backlogged column. Extra Review slots are unlocked only when `human_approves_merge: true`.
- **Review-lane mutex** — on auto-merge boards the workflow serializes Review-lane agents, so concurrent merges can't race.
- **Backend mutual exclusion** — the workflow backend writes `.claude/super-board/inflight/workflow-wave.lock`; the legacy dispatcher refuses to start while it exists (exit 74).
- **Tests** — 6-scenario suite at `tests/test-wave-plan.sh` pins the wave planner's selection logic against fixtures, no `gh` calls.

Why: replaces `nohup claude -p` dispatch ahead of the June 15 Agent SDK billing split. The legacy `claude-p` backend remains the default — nothing changes unless you opt in.

## v1.4.0 — 2026-05-27

### Pure-Python `super-board status` renderer (~50× faster)

The status snapshot now renders via `.claude/bin/super-board-status.py` instead of being assembled token-by-token by the model. Same locked 80-column kanban template; ~1.3s instead of ~1min per invocation.

Pure Python 3 stdlib + `gh` CLI. No bash, no jq. Works on macOS, Linux, and Windows.

Highlights:

- Handles both user-owned and organization-owned GitHub Projects (`repositoryOwner { ... on ProjectV2Owner }`).
- Paginates project items via cursor + endCursor, with a 2000-card ceiling and a truncation warning past that.
- Defensive input handling: slug-arg sanitization rejects `..` and other path-traversal sentinels; issue-title control-char strip prevents hostile titles from emitting escape sequences into the kanban frame.
- Lane-handoff fix: clean Build → QA → Review handoffs no longer leave phantom in-flight entries from the prior lane.
- Cross-platform CI (`.github/workflows/cross-platform.yml`): smoke matrix on ubuntu/macos/windows × py3.10/3.12, plus 22 parser fixture tests that pin the regexes against real dispatcher log lines.

Agents that invoke the `super-board` skill will now prefer the script and print its stdout verbatim. The locked template spec in `references/status.md` is retained as fallback / change-control documentation.

Contributed by @LucariusWest (#2).

## v1.3.0 — 2026-05-24

### New verb: `super-board stop`

Graceful shutdown of an in-flight run. One command, no manual `pkill` choreography, full context preserved on the board so the next `super-board run` resumes cleanly.

What it does, in order:

1. Inventories in-flight workers from `.claude/super-board/inflight/<issue-N>` lock files.
2. For each one, posts a `🛑 super-board · stopped mid-flight` comment on the issue **and** its PR, including lane, worker PID, UTC timestamp, last pushed commit (the "resume point"), and the literal resume command.
3. Releases the GitHub assignee mutex on each claimed issue + clears `loop:in-build`/`loop:in-qa`/`loop:in-review` descriptive labels.
4. SIGTERM → 1s → SIGKILL the worker PIDs.
5. Sweeps any untracked `claude -p .*super-board` orphan workers (defense against crashed-dispatcher leftovers).
6. Kills the dispatcher loop (`super-board-run.sh`).
7. Removes in-flight lock files. Leaves worktrees, branches, and PRs in place.

**Resume = run.** There is no separate `super-board resume` verb on purpose. The board is the state — cards sit in whichever column they were in when stopped, branches and PRs persist, and `super-board run <slug>` re-claims the same cards on its next tick. Each previously-in-flight card costs one extra lane cycle on resume.

What stop does NOT do (deliberate):

- Doesn't wait for workers to reach a clean stopping point — `claude -p` has no SIGTERM handler that flushes a partial commit. Any uncommitted edits in worker worktrees are discarded; the last **pushed** commit is the resume floor.
- Doesn't touch worktrees — the next worker re-checks-out the same branch faster.
- Doesn't touch branches or PRs.

### Lock file format upgrade (backwards-compatible)

The dispatcher now writes lock files as bash-assignment style:

```
PID=12345
LANE=qa
STARTED=2026-05-24T18:42:11Z
```

This lets `super-board stop` recover the lane name + dispatch time without an extra `gh` call. A new `read_lock` helper handles both v1.3.0+ and legacy single-line-PID formats, so an upgrade mid-run is safe — existing locks keep working until the dispatcher rewrites them on the next dispatch.

### Routing

`SKILL.md` now lists five verbs. `references/stop.md` is the full contract. New routing rows: `stop`, `pause`, `kill`, and `resume`/`pick up where I left off` (all route to `stop.md`, since resume is just `run` again).

## v1.2.0 — 2026-05-24

First public release.

### Worker-storm fixes (post-incident #381, originally landed in an earlier internal repo 2026-05-22)

- **PID tracking + per-lane lockfile.** The dispatcher tracks `BUILD_PID`/`QA_PID`/`REVIEW_PID` and refuses to dispatch into a lane whose worker is still alive. Closes the 10–30s `claude -p` cold-start race that produced 7 racing workers on the very first run.
- **In-flight lockfiles** at `.claude/super-board/inflight/<issue-N>` containing the worker PID. `top_card_in_column` skips any issue with a live lock even before the assignee write propagates. Reaped each tick via PID liveness check.
- **Atomic assignee claim BEFORE worker spawn.** `try_claim_assignee` runs in the dispatcher and only proceeds to `nohup claude -p` if it wins the assignee write.
- **Orphan scan on startup.** Refuses to start if any `claude -p .*super-board run` worker is already alive from a prior crashed dispatcher run.

### Rate-limit fixes

- **Tick interval bumped 30s → 120s.** ProjectsV2 GraphQL query is ~103 points regardless of board size; 120s keeps usage at ~3.1k/hr, comfortably under the 5k/hr GraphQL budget.
- **Rate-limit guard** sleeps until reset when GraphQL remaining drops below 200.
- **Per-tick project-items cache** — one `gh project item-list` per tick, not per column lookup. ~7× quota cut.
- **Worker rate-limit etiquette** — sub-agent gh-call budgets, local `git blame` preference, `gh-quota-on-exit:` line required on every PR handoff comment.

### QA evidence

- **Mandatory inline screenshot embeds** on every QA exit (pass and fail) at standard viewports (1920×1080, 1024×768, 375×667). Screenshots committed to the issue branch BEFORE the GitHub comment is posted, so they render in-page.
- **`docs/super-board/runs/**/*.{png,jpg,webp,html,log,patch,diff,zip,trace}` gitignored** by default. Keep `.md` and `.json` summaries tracked for audit trail; drop the heavy artifacts. Users adopting on existing repos: `git rm --cached docs/super-board/runs/**/*.png` etc. to untrack what's already in.

### Documentation fixes

- **Card-locking semantics corrected.** The original spec said the GitHub assignee write was the lock. In practice it doesn't hold up — assigning yourself something you already have is a no-op on a solo account, and GH issues accept multiple assignees, so it never blocked a second worker. The real lock is the local `.claude/super-board/inflight/<N>` lockfile + per-lane PID tracking. Docs updated throughout.

### Other

- **Multi-attempt card-move guard.** Workers must call `sb_gh_guard_check` (or equivalent retry-with-backoff) around the column-move mutation and write a `move-mutation-result: ok|err|skipped` line in the PR handoff comment. Lets the dispatcher log retries and budget for them instead of silently re-dispatching every 10 min.
- **CI-budget bypass (💳).** If remote CI jobs `failed_to_start` due to Actions budget AND local-evidence is strong (truth gate passed, Tester clean, all threads clean), the Reviewer can squash-merge on local evidence with a `🛡 → ✅ CI-budget bypass` comment citing the failed run ID, Tester pass-count, and truth-gate score. Only for `💳` — never for `🛡` truth-fail, `🔐` missing creds, or `🧑` human-only decisions.
