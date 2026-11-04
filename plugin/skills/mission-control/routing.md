# mission-control routing

Pick the phase, announce `Phase: <phase> → <skill id>`, run the first fitting skill. Ids are exact
(bare = `~/.claude/skills/<name>`). † = user-invoked: read its `SKILL.md` and follow it (SKILL.md §3).
Lane workers inside super-board load super-board's own registry; add per-card skills with `Skills:`.
External lanes: read the delegate `SKILL.md` before dispatching; opencode never via `relay.mjs` (crashes here).
The operator's override in chat always wins.

| phase | triggers (Arabic + English) | skill | output |
|---|---|---|---|
| discuss | "عايز أعمل" · "فكرة" · "ناقشني" · "اسألني" · "I want to build" · "idea" · "grill me" | `brainstorming`, `mattpocock-skills:grill-with-docs` †, `mattpocock-skills:grilling`, `mattpocock-skills:research`, `mattpocock-skills:prototype` | agreed goal + decisions in mission log; `CONTEXT.md` / ADR when the skill writes them |
| plan | "خطة" · "قسّمها" · "تذاكر" · "plan" · "spec" · "tickets" · "break it down" | `writing-plans`, `mattpocock-skills:to-spec` †, `mattpocock-skills:to-tickets` †, `mattpocock-skills:wayfinder` † (huge / foggy work) | plan file, spec, tracer-bullet tickets with blocking edges |
| build (super-board) | "ابدأ" · "نفّذ" · "شغّل البورد" · "build it" · "start" · "run the board" | `mattpocock-skills:to-tickets` †, `dispatching-parallel-agents` (ad-hoc work off the board) | cards in `Ready` (`## Blocked by`, `Skills:`), then Skill tool `super-board:super-board`, args `run [--low\|--high\|--codex=<model>]`; draft PRs |
| debug | "فيه مشكلة" · "مش شغال" · "الـCI واقع" · "bug" · "broken" · "failing test" · "CI red" | `mattpocock-skills:diagnosing-bugs`, `systematic-debugging`, `pstack:fix-ci` | failing repro, root cause, fix card (label `bug`) for the board |
| review | "راجع" · "شوف الـPR" · "review" · "check this PR" · "second opinion" | `mattpocock-skills:code-review`, `requesting-code-review`, `pstack:blast-radius` | findings one line each + verdict; different-model reviewer per SKILL.md §4a |
| ship | "خلصنا؟" · "جاهز" · "اعمل PR" · "ship" · "done?" · "open the PR" · "conflict" | `verification-before-completion`, `mattpocock-skills:pr`, `pstack:fix-merge-conflicts` | evidence the checks passed, PR link + summary to the operator; merge = gate (pilot repo) or the operator |

| lane | when | delegate skill | model id | skill loading |
|---|---|---|---|---|
| `[Sonnet]` subagent | repo batches: code, ports, tests (default executor) | — | `sonnet` | Skill tool: brief starts `Invoke these skills first: <ids>`; † → `SKILL.md` path |
| `[Haiku]` subagent | narrow / mechanical: renames, moves, scans | — | `haiku` | same as Sonnet |
| `[Opus]` subagent | judgment, adversarial review of Sonnet / Haiku / Codex / Pi work | — | `opus` | same as Sonnet |
| `[Fable]` subagent | only when the operator names it | — | `fable` | same as Sonnet |
| `[Codex · <model>]` | second lane: code or review in a different model family | `codex-delegate` (recipe only; call codex exec direct, never relay.mjs) | `gpt-5.6-luna` default; `gpt-5.6-sol` / `gpt-6-astra` / `gpt-5.6-terra` only when the operator names one | paste `SKILL.md` path + "read it and follow it exactly"; brief says NON-INTERACTIVE; judge output text, not exit code |
| `[Pi · runinfra/<model>]` | standby when Codex / OpenCode credit runs out | `pi-delegate` (relay works) | `runinfra/glm-5-3-flash` default; `runinfra/nemotron-3-5-lightning-30b` mechanical; `runinfra/qwen3-8-27b` reasoning / review | paste `SKILL.md` path; key presence check only |
| `[OpenCode · <model>]` | second lane, the operator's choice | `opencode-delegate` (direct shell, skip relay.mjs on this machine) | the operator names it (e.g. `kimi-k2.7-code`) | paste `SKILL.md` path |
| `[Grok · <model>]` | extra second opinion | `grok-delegate` | CLI default; label with the model from `result.json` | paste `SKILL.md` path |
| `[Claude CLI · <model>]` | headless fresh-context Claude in another repo | `claude-delegate` | CLI default; label with the model actually run | pasted `SKILL.md` path, or skill id if installed there |
