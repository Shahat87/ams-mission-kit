# super-board guard hooks

Claude Code hook scripts. Python 3 standard library only (no jq, no node). Each reads the
hook JSON on stdin and prints a decision only when it blocks. Every guard judges the `Bash`
AND the `PowerShell` tool (a Windows session has both; a guard that only reads `Bash` is a
guard the shell next door walks around). The four that protect secrets, the robot identity and
the base branch -- `guard-secrets`, `guard-gh-identity`, `guard-merge`, `guard-protected-push`,
`guard-config-write`, plus `guard-delete-outside` and `guard-key-literals` -- fail CLOSED: any exception while judging
(malformed input included) exits 2 with a message on stderr, which Claude Code
treats as a block, because a session in `bypassPermissions` mode has nothing behind them. The
remaining guard (`guard-worktree-path`) exits 0 on its own errors so a broken guard never stops unrelated work. All of the
below are on by default except the protected-push guard (opt-in) and the dev-only skill-eval gate.

**Architectural note.** Hooks filter command TEXT. A shell has more ways to spell a command than any regex
list can close, so the guards below are defense in depth. The real control that keeps `main` safe is the
server-side ruleset ("main: admin only": update / deletion / non-fast-forward, bypass for the admin role only)
with the robot on the `write` role, and the `staging` ruleset (one approving review from someone other than the last
pusher). Those server-side rulesets are the control; the hooks here are the second layer behind them. Keep the
rulesets; do not read a green hook test as a security proof.

- `guard-worktree-path.py` — PreToolUse `Bash|PowerShell`: denies `git worktree add|move` whose target resolves outside `<repo>/.claude/worktrees/` (honours `cd X &&`, `git -C X`).
- `guard-secrets.py` — PreToolUse `Bash|PowerShell|Read|Grep`: denies reading, copying, writing or piping dotenv files (`cat`, `Get-Content`/`gc`/`type`, `Select-String`, `Copy-Item`, `Set-Content`, `Remove-Item`, `.e*` wildcards), SSH keys, `.npmrc`, `credentials(.json)`, service-account JSON and `.pem`, and dumping the environment: `printenv`, a bare `env` / `set` / `export -p` / `declare -x`, `/proc/*/environ`, `Get-ChildItem Env:`, `Get-Item Env:NAME`, `[Environment]::GetEnvironmentVariable(s)`, `$env:` reads of secret-looking names (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `AUTH`, …) and `echo` of such a variable. `.env.example` stays readable. Fails closed. One plain call to `super-board-env-check.sh KEY…` is allowed: it prints present / empty / missing per key and never a value (no pipe, chain or redirect may ride along).
- `guard-key-literals.py` — PreToolUse `Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell`: denies writing a live-looking API key into a file; PostToolUse `Edit|MultiEdit|Write|NotebookEdit`: flags one already on disk. Reports line numbers, never the value.
- `guard-delete-outside.py` — PreToolUse `Bash|PowerShell`: denies `rm`, `rmdir`, `unlink`, PowerShell `Remove-Item` / `ri` / `del` / `erase` / `rd`, `find … -delete` / `-exec rm` and `git clean` whose target resolves outside `$CLAUDE_PROJECT_DIR`, and always `/` or `~` itself (honours `cd X &&`, `git -C X`, `~`, `$HOME`). Inside `/tmp`, `/private/tmp`, `/var/folders` and `$TMPDIR` stays allowed. A path whose value is only known at run time (`$(…)`, a variable set earlier in the same command) is allowed.
- `guard-gh-identity.py` — PreToolUse `Bash|PowerShell|Read|Grep|Glob`: keeps the session on the robot GitHub identity (hardened). Active only in a project that has a robot identity, and that is not decided only by files the agent can edit: `GH_CONFIG_DIR` in the hook's own environment, `SB_GH_IDENTITY_GUARD=1` (environment, or settings `env` -- `super-board-setup.py fix` / `sync-guard-env` writes it whenever a board config sets `notifications.bot_identity`), a robot `GH_CONFIG_DIR` in settings, or a board config with `bot_identity` other than `auto`. An unreadable board config or settings file counts as configured, and then every `gh` is refused. Three layers: (1) deny rules on the whole command text for `gh auth …`, any mention of `GH_CONFIG_DIR` / `GH_TOKEN` / `GITHUB_TOKEN` / `GH_HOST`, git credential config, `cmdkey`, `gh` with a `--hostname` or `-H Authorization:` header, the bot's gh config dir and `hosts.yml` (Read/Grep/Glob: paths only); (2) refusal of commands that BUILD a name to reach them (`"GH_"+"TOKEN"`, `${!n}`, `eval`/`Invoke-Expression` with expansions, name stems) and of environment dumps (`printenv`, bare `env`/`set`, `export -p`, `Get-ChildItem Env:`, `[Environment]::GetEnvironmentVariable`); (3) a positive rule for `gh`: an ALLOWLIST of subcommands (`issue`, `api`, `project`, `run`, `workflow`, `label`, `search`, `pr create|view|list|comment|edit|ready|checks|diff`, `repo view`, `release view`; `gh pr edit` with a base change is refused); `auth`, `alias`, `extension`, `config`, `secret`, `ssh-key`, `gpg-key`, `codespace`, ... are denied, quote-insensitively (`gh 'alias' set`); no `--hostname`, no token variable in the session environment, and the session's `GH_CONFIG_DIR` equal to the robot config dir configured in `.claude/settings.local.json` / `settings.json` `env.GH_CONFIG_DIR` (or `SB_ROBOT_GH_CONFIG_DIR`). Fails closed. Known residual bypasses are listed in the script header. The deny rules read the whole command text, quoted strings included, so a `gh issue comment --body "…gh auth switch…"` or a commit message that names a token variable is denied too (for example on a card about this hook): write the text to a file and pass `--body-file <md>` (or `git commit -F <file>`). In a robot-identity project every `gh` is also refused when `GH_CONFIG_DIR` is unset in the session env (no fallback to the default gh profile): set `env.GH_CONFIG_DIR` in `.claude/settings.local.json` and restart.
- `guard-merge.py` — PreToolUse `Bash|PowerShell`: a PR is merged only by the merge gate. Denies `gh pr merge`, `gh api … /merge|/merges` (any method), GraphQL `mergePullRequest` / `enablePullRequestAutoMerge` (and a GraphQL body read from a file, which cannot be inspected), `gh pr create|edit --base main` / `-B main` / `--base=main` / `-Bmain`, and `gh api … -f|-F base=main` on a `/pulls` endpoint. "main" is `main`, `master` and `SB_MERGE_PROTECTED` (comma-separated), minus any branch a board config names as its `base_branch` (a repo whose base IS main opens its PRs against main). Also denies `gh api` writes with a `$`-containing argument (non-graphql), a graphql query held in a variable, `updatePullRequest` + `baseRefName`, a `--base` / `-B` / `base=` taken from a variable, a merge path built in a variable then passed to `gh api`, `gh @splat`, `iex` / `Invoke-Expression` with piped or non-literal input, and `powershell|pwsh -e|-ec|-enc|-EncodedCommand` (every guard here denies that form). Looks through `bash -c`, `pwsh -Command`, `cmd /c`, `eval`, `Invoke-Expression`, `Start-Process gh -FilePath/-ArgumentList`, `sudo`/`env`/`xargs`, `$( )`, `$x = gh …` and chains. A plain call of `.claude/bin/super-(board|review|qa)-*.sh|py` anchored at the project (relative, `$CLAUDE_PROJECT_DIR/…` or the absolute project path) is allowed on purpose: the merge gate runs its own `gh pr merge` inside the script process, where no hook looks. Fails closed. The merge-gate (and other `.claude/bin/super-*`) call is exempt only when the script and its `--config` resolve, from the effective cwd (payload cwd plus any `cd` / `Set-Location` / `pushd` earlier in the command), to `<project-dir>/.claude/bin/` and to an EXISTING file under `<project-dir>/.claude/super-board/configs/` (realpath, case-insensitive on Windows; `--config` may be absent); a worktree copy, a foreign path, a variable or a missing value is denied. `gh pr create` without an explicit base (`--base X`, `--base=X`, `-B X`) is denied with "pass --base <base_branch>" (read from the board config).
- `guard-config-write.py` — PreToolUse `Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell`: a session may not rewrite its own guards. Denies writes inside the project to `.claude/hooks/**`, `.claude/bin/**`, `.claude/workflows/**`, `.claude/super-board/configs/**`, `.claude/settings*.json` and `.claude/super-board/active` (the active-board pointer: the board's scripts write it in-process; a Write whose content is not a plain name `^[A-Za-z0-9._-]+$` gets its own reason, and is denied either way), and, wherever it runs (G-4, user scope: the home rules do not depend on `CLAUDE_PROJECT_DIR`), to `<home>/.mission-control/**` and `<home>/.claude/settings*.json` (`<home>` is `USERPROFILE` or `HOME`; also caught as `~`, `$HOME`, `${HOME}`, `$env:USERPROFILE`, `%USERPROFILE%` and the absolute Windows and MSYS spellings; a `cd ~` / `cd $HOME` / `Set-Location ~` makes later relative targets resolve against home): the file tools' paths, and in a shell command a redirect, `tee`, `cp`/`mv`/`install`/`ln`/`rsync`/`dd of=`, `sed -i`, `touch`/`truncate`/`rm`, `Set-Content`/`Add-Content`/`Out-File`/`New-Item`/`Copy-Item`/`Move-Item`/`Remove-Item` (a `cd .claude/hooks` counts for the rest of the command; nested shells are looked through; `.claude/worktrees/**` copies are exempt). `SB_ALLOW_CONFIG_WRITE=1` in the environment Claude Code was STARTED with turns it off for that session: that is how the owner or a sync PR updates these files outside the lanes (a `export` inside the session does not count). The board's own scripts (`super-board-setup.py fix`, `install.sh`) write these files in their own process, so onboarding and upgrades keep working; a lane that wants to edit a board config with the Write tool now needs the escape. Fails closed. GUARDRAIL: catches habit and mistakes; not a security boundary (spec §15.3).
- `mc-identity-check.sh` — SessionStart (shell, not Python): halts the board (exit 2, message on stdout and stderr) when `python3` is not runnable (the Microsoft Store stub: every guard hook would otherwise fail non-blockingly), and halts the board when `GH_TOKEN` / `GITHUB_TOKEN` is set or `gh api user` is not the `notifications.bot_identity` machine-user login of the board config (case-insensitive). Silent when no config names a robot login (`auto`, empty, or a GitHub App `…[bot]`). A mismatch exits 0 with SessionStart `additionalContext` (`MISSION CONTROL HALT …`, so the model sees it) and writes the marker `.claude/super-board/identity-halt` (expected vs actual login, time); `guard-gh-identity.py` denies every `gh` while the marker exists, `guard-config-write.py` stops the agent clearing it, and a matching login at a later SessionStart removes it.
- `guard-host-tests.py` — PreToolUse `Bash|PowerShell`: only in docker mode (`pilot.sandbox.provider` = `docker`). A card worktree `.claude/worktrees/issue-<number>` whose tool patch was applied by `lane-delegate verify` carries a marker `.claude/super-board/inflight/sandbox-<number>` in the main repo; while it exists this guard denies every test / build / install / server command on the host (`npm|pnpm|yarn|npx|bun` test run start exec x dlx install ci, `node`, `python`/`python3`/`py` (`-m pytest`, `-m unittest`, `manage.py`, `-c`), `pytest`, `vitest`, `jest`, `playwright`, `tsc`, `vite`, `next`, `make`, `tox`, `nox`, `poetry run`, `uv run`, `cargo test`, `go test`, `dotnet test`, a `.ps1` / `bash` / `sh` script that resolves into the worktree, an executable inside it, `git rebase --exec`, `find -exec`). The directory is the payload `cwd` plus every `cd` / `Set-Location` / `git -C` / `--prefix` / `--dir` target; one only known at run time counts as the marked worktree. git, gh, `python3 .claude/bin/super-board-*`, cat, ls, grep, rg, find, head, tail, `sed`, Get-Content and Select-String stay allowed. It fails closed (a command it cannot evaluate is denied) and tells the agent to use `python3 .claude/bin/super-board-lane-delegate.py exec --sandbox --config <cfg> --dir <worktree> -- <cmd>`.
- `guard-external-tools.py` (G-1, **user scope**) — PreToolUse `Bash|PowerShell|mcp__.*` and SessionStart: in a guarded repo (see `mc_guarded.py`), external coding CLIs (codex, grok, pi, ...) run only on the Lenovo, through `python3 .claude/bin/super-board-lane-delegate.py delegate`; a command that runs a covered tool, directly or through `wsl`, `cmd /c`, `pwsh -c`, `bash -c`, `Start-Process`, `env` and the like, is denied (exit 2), as is an MCP server whose configured command is a covered tool. At SessionStart it only warns about a configured covered MCP server. Fails closed. Installed to `~/.mission-control/hooks/` and wired into `~/.claude/settings.json` by `install.sh --guard-repo`, not into the project. GUARDRAIL: catches habit and mistakes; not a security boundary (spec §15.3).
- `guard-taint.py` (G-2, project scope) — PreToolUse `Bash|PowerShell`: in a guarded repo it keeps tool-written code off this host's disk. A branch, PR or sha recorded as tainted (`mc-delegate/*`, the `sandbox-taint-*` and `sandbox-delegate-*` markers in `.claude/super-board/inflight/`) cannot be pulled, merged, rebased, checked out, cherry-picked or applied here, and a tainted worktree refuses those commands whatever the arguments; `gh pr checkout` of a tainted PR is refused too. Runs no subprocess, not even git. Wired by the project snippet next to `guard-host-tests.py`. GUARDRAIL: catches habit and mistakes; not a security boundary (spec §15.3).
- `guard-orchestrator.py` (project scope) — PreToolUse `Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell`: while a default workflow-backend run holds its wave lock (`.claude/super-board/inflight/workflow-wave.lock`), the parent orchestrator cannot edit files or run product build / test / install commands — lanes do all product work. Enforces only when the lock exists, the payload `session_id` equals the first line of `.claude/super-board/inflight/orchestrator-session` (the hook writes that file itself when it allows `super-board-orch.sh lock-acquire` from a parent call — only when no lock exists yet and no other session is still named in it, so a leftover binding from a crashed run denies the acquire until one bare `lock-release` clears it, and the bound session is always the one whose acquire created the lock), the payload has no `agent_id` (subagents and workflow lanes carry one and are exempt), and `SB_ORCH_GUARD_OFF` is not 1 (the escape hatch in settings `env`). While it enforces: PowerShell and NotebookEdit are always denied; Write/Edit/MultiEdit are allowed only for the orchestrator's own run files — under `.claude/super-board/inflight/`, under the payload's `scratchpad_dir` (when the payload sets one) or under `docs/super-board/runs/` — every other path stays denied and `..` never survives normalization; a Bash command must be one simple allowlisted call (gh issue view|edit|comment, pr view|diff|checks|comment, project item-list|item-edit, `gh api` — REST and GraphQL, including writes: outside the fence, which targets product edits and builds, not GitHub calls; git reads, never with `--output`; `git worktree remove` without `--force`; `pgrep -f`, `kill -0`, `ps -p`; `jq -r` on a board config or `.claude/settings.json`; `date -u`; `bash .claude/bin/super-board-{wave-plan,preflight,usage,throttle,card,stop,orch,codex-lane,codex-wave,host}.sh`; `python3 .claude/bin/super-board-{roadmap,github-read,skills-ready,verification-ready}.py`, each Python helper also in its bare shebang form). Any unquoted shell operator, newline, command substitution, nested shell or wrapper is denied; the one exception is a quoted heredoc into `super-board-orch.sh reconcile` (the cards JSON is literal stdin data there). Every denial exits 2, ends with “do not retry in another form.” and appends one line (first token only, plus a sha256 of the full source) to `.claude/super-board/orchestrator-violations.jsonl`, which rotates at 1 MiB; an unwritable log never lifts a denial. A malformed payload fails closed. GUARDRAIL: catches habit and mistakes; not a security boundary (spec §15.3).
- `mc_guarded.py` — not a hook: the guarded-repo list (spec §15.4 D-5) that G-1 and G-2 share, `~/.mission-control/guarded-repos.json`. A repo is guarded when a remote or its git common dir matches an entry; a missing or invalid list means every repo is guarded. CLI: `mc_guarded.py add --repo owner/name [--root DIR]` and `mc_guarded.py check <path>`.
- `sb-python.sh` — not a hook: the launcher every project-scope Python hook goes through, `bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/sb-python.sh <hook>.py`. See "Interpreter pinning" below.
- `cleanup-wt.py` — SessionStart (`--auto`): when a base branch tip moved since the last session, removes worktrees and local branches already merged into it. The merge gate also runs it with `--post-merge` after every merge. Never deletes unmerged work (a dirty worktree on a merged branch is wip-committed and its branch kept), never touches bases or the worktree it runs in, and writes a recovery TSV to `<git-common-dir>/cleanup-wt/<timestamp>.tsv` before acting. Restore with `git branch <name> <sha>`. A manual dry run (`python3 .claude/hooks/cleanup-wt.py`, then `--apply`) still works.

## Enable

`install.sh` does this for you: it copies the scripts to `.claude/hooks/` in the target
project and merges the block below (kept in `settings-snippet.json`) into
`.claude/settings.json`, backing the file up first and never adding a command twice.
`./install.sh --no-hooks` skips both (and with it the merge gate's post-merge cleanup, which needs `cleanup-wt.py`). By hand, copy the scripts, including `sb-python.sh`, and merge this (run `install.sh` once to write the interpreter pin):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|PowerShell",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-worktree-path.py"
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-delete-outside.py"
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-merge.py"
          }
        ]
      },
      {
        "matcher": "Bash|PowerShell",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-host-tests.py"
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-taint.py"
          }
        ]
      },
      {
        "matcher": "Bash|PowerShell|Read|Grep",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-secrets.py"
          }
        ]
      },
      {
        "matcher": "Bash|PowerShell|Read|Grep|Glob",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-gh-identity.py"
          }
        ]
      },
      {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-key-literals.py"
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-config-write.py"
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-orchestrator.py"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-key-literals.py"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd \"$CLAUDE_PROJECT_DIR\" && bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh cleanup-wt.py --auto --fetch 2>/dev/null || true"
          },
          {
            "type": "command",
            "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/mc-identity-check.sh"
          }
        ]
      }
    ]
  }
}
```

## Interpreter pinning (G-3)

Every hook command calls Python through a pinned interpreter with `-I` (isolated: no `PYTHON*` variables, no user
site, no script directory or cwd on `sys.path`), never a bare `python3` that PATH can swap or that can be the Microsoft
Store stub.

- **Project scope uses a launcher.** The project's `.claude/settings.json` is committed to git, so it cannot hold
  a machine-specific interpreter path: every other checkout would break, and a hook command that cannot start is only a
  non-blocking error in Claude Code, so the guard would be silently off. The snippet therefore calls
  `bash "$CLAUDE_PROJECT_DIR"/.claude/hooks/sb-python.sh <hook>.py [args]`. The launcher validates the hook name, finds the
  interpreter, and runs `exec <python> -I -X utf8 <hooks dir>/<hook>.py` (`-I` drops `PYTHONUTF8`, and non-ASCII paths matter).
- **The pin.** Every hooks `install.sh` (also `--no-hooks --protect-main`) writes the interpreter it ran with to
  `${USERPROFILE:-$HOME}/.mission-control/python` (one absolute path, forward slashes; rewritten only when it changes) and
  prints `🐍 hooks run with <path> -I` (before it merges the settings; it tries `python3`, then `python`). The launcher uses
  that line when it is an absolute path to an executable file, else `python3`, then `python`, from PATH. A candidate that is
  the Microsoft Store stub is skipped and the next one tried (a pin naming the stub counts as no pin).
- **No usable Python means a deny.** If every candidate is the Microsoft Store stub (a path containing
  `\Microsoft\WindowsApps\`), or there is none, the launcher prints `super-board hooks: no usable Python (run install.sh
  to pin one); refusing` and exits 2, which makes a PreToolUse hook a deny: a guard that cannot start must not silently allow.
- **User scope uses the launcher copied beside the user-scope hooks.** `~/.claude/settings.json` gets
  `bash "<home>/.mission-control/hooks/sb-python.sh" <hook>.py [args]` (forward slashes), so a re-pin or a new Python needs
  no settings change. An earlier user-scope command for the same hook file (the old pinned-path form) is replaced, never
  duplicated; user hooks are never touched.
- An older install whose settings still carry `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<name>.py` (or the `python`
  variant, or the old `cleanup-wt.py --auto --fetch` line) is migrated on the next `install.sh`: the legacy command is removed
  (listed with `- ...` under `super-board-settings.py hooks --dry-run`) and the launcher one added. User hooks and
  commands of any other shape are never touched.

## Guarded repos: `install.sh --guard-repo owner/name`

```
./install.sh --guard-repo owner/name [--protect-main] [target-project-dir]
```

After the normal install it (1) records the repo in `~/.mission-control/guarded-repos.json` (with the target's git common
dir when its `origin` matches; a broken existing list stops the install with exit 70 and is not overwritten),
(2) copies `guard-external-tools.py`, `guard-config-write.py` and `mc_guarded.py` to `~/.mission-control/hooks/`,
(3) merges three commands into `~/.claude/settings.json` (never overwriting: it keeps every key, backs the file up, adds
each command once): `guard-external-tools.py` on PreToolUse `Bash|PowerShell|mcp__.*` and on SessionStart, and
`guard-config-write.py --user-scope` on PreToolUse `Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell`; and (4) prints
`🔒 guarded: owner/name (list: <path>); G-1 and config guard at user scope`. It refuses `--no-hooks` (exit 64) and a
value that is not `owner/name` (exit 64). It proves the guard took: after `add` it runs `mc_guarded.py check <target>`, and
when the target is not matched by `owner/name` (its remotes do not normalise to it) it prints `✗ --guard-repo: ...` and
exits 65, leaving the list entry (harmless; a remote may be added later). An invalid user `settings.json` is left
untouched, prints a `!` line on stderr, exits 70 and prints no `🔒` line. The user-scope merge is
`super-board-settings.py user-hooks`. `--user-scope` makes `guard-config-write.py` apply only the home rules
(`<home>/.mission-control/**`, `<home>/.claude/settings*.json`), because at user scope it runs in every project and the
project's own `.claude/` rules belong to the project-scope entry.

An install made before 3.1.3-ams.10 has the old `Bash`-only matcher entries; the merge adds the new
`Bash|PowerShell` ones beside them (same command, so it runs once more for `Bash` and is harmless).
Delete the old entries by hand if you like.

## Opt-in: block pushes to the base branch

`guard-protected-push.py` — PreToolUse `Bash|PowerShell`: denies a `git push` that would update or delete
`main`, `master`, any `base_branch` in `.claude/super-board/configs/*.json`, or a name in
`SB_PROTECTED_BRANCHES` (comma-separated). That covers force pushes (`-f`, `--force`,
`--force-with-lease`, `+ref`), explicit refspecs (`feat:main`, `HEAD:refs/heads/main`), `--delete`,
`--all` / `--mirror`, and a bare `git push` while the base is checked out. Force pushes to feature
branches stay allowed: the rebase pass needs them. `SB_ALLOW_PROTECTED_PUSH=1` turns it off for a
session. Fails closed (exit 2) when it cannot decide.

It is installed but not wired by default: a brand-new repo often pushes its first commits straight
to `main`. `super-board onboard` asks once ("Block direct/force pushes to main? Recommended for
existing apps; skip for brand-new repos"), and `./install.sh --protect-main` wires it. After a
`--no-hooks` install, onboard does not wire a script that is not there; it names
`./install.sh --no-hooks --protect-main <dir>`, which installs this one guard alone. Either way the
entry is `settings-protect-main.json`:

```
{ "matcher": "Bash|PowerShell", "hooks": [ { "type": "command",
  "command": "bash \"$CLAUDE_PROJECT_DIR\"/.claude/hooks/sb-python.sh guard-protected-push.py" } ] }
```

## Optional: static deny rules for merging and retargeting

`settings-deny-main.json` is a `permissions.deny` list for `Bash` and `PowerShell`: `gh pr merge`,
`gh api …/merge`, `gh api …mergePullRequest…` and `gh pr create|edit` with `--base main`, `--base=main`,
`-B main`, `-B=main`, `-Bmain`. It is NOT merged by `install.sh` (the settings merger copies `hooks` only,
and these rules cannot tell a repo whose base_branch is main from one whose base is staging), and in
`bypassPermissions` mode deny rules are the weaker control: `guard-merge.py` is the one that always runs.
Copy the `permissions` block into `.claude/settings.json` by hand for a project whose base branch is not main.

## Known residual bypasses

A regex guard cannot parse every shell, and the server-side ruleset is the control that does not depend on text. Left open, on purpose and in writing: a file name or variable name
assembled at run time that the concatenation rules do not recognise; an interpreter reading the environment or
a secret file on its own (`python -c "print(os.environ)"`); a script the agent writes itself and then runs
(writing into `.claude/bin` is blocked by `guard-config-write`, but a script written elsewhere is not read);
an interpreter writing a guarded file from code (`python -c "open('.claude/hooks/x','w')…"`); a command name
built at run time (`& $g auth token`); a request body piped to `gh api --input -` on a non-`/pulls` endpoint;
a git alias that expands to a push;
a robot `GH_CONFIG_DIR` that is not configured in settings (the identity guard cannot compare it then);
heredoc text that happens to contain a command line is checked as a command (that errs on the side of denying).
a graphql query held in a variable that does not start with `$`; a `gh` allowlist that is text, not identity (the
session's real identity is what `mc-identity-check.sh` and the ruleset check).
`SB_ALLOW_PROTECTED_PUSH=1` and `SB_ALLOW_CONFIG_WRITE=1` switch their guards off for a session when set at its start.

## For repos that author skills: `dev/gate-skill-evals.py`

A Stop hook for this pack and for a host repo, **never installed into a target project** (`install.sh`
copies `hooks/*.py` only). When a skill changed this session — uncommitted, untracked, or committed
but not pushed — and no `claude plugin eval` result is newer than the change, it blocks the stop and
names the command to run. A changed `SKILL.md` always counts; a changed reference, script or the
plugin's `workflows/<skill>*.js` counts only when the skill has an eval case. Cases are found per
skill (`<skill>/evals/`) or per plugin (`evals/<case>/case.yaml` whose `tags` or `name` has the
skill); results are the newest file under the matching `evals/results/`. A changed `SKILL.md` with
no case only warns. A running `plugin eval`, `stop_hook_active`, `SKILL_EVAL_GATE=off` and globs in
`.claude/eval-gate-ignore` all stand it down.

Why Stop and not pre-commit: the point is to make the agent run the eval while it still has the
context to read the result. A pre-commit gate fires later, in whoever commits (a human, `git-sync`,
another agent), and cannot run a paid eval itself, so it gets bypassed with `--no-verify`. The Stop
hook still covers commits made during the session, via the unpushed-commits check.

Wired in this pack's `.claude/settings.json`. For a host repo, add to its `.claude/settings.json`:

```
"Stop": [ { "hooks": [ { "type": "command", "timeout": 30,
  "command": "python3 \"$CLAUDE_PROJECT_DIR\"/_skills/vendor/super-board/hooks/dev/gate-skill-evals.py" } ] } ]
```

Adapted from `gate-skill-evals.sh` in ai-builder-starter-kit.

Tests: `tests/test-guard-hooks.sh` (all guards, the `sb-python.sh` launcher, `mc-identity-check.sh`, the wiring and the eval gate; Bash and PowerShell forms), `tests/test-install.sh`, `tests/test-settings.sh`, `tests/test_guard_host_tests.py` (the docker-mode test guard), `tests/test-cleanup-wt.sh`.

## Credit

- `cleanup-wt.py` — ported from an earlier internal `cleanup-wt` skill (Node), generalised.
- `guard-worktree-path.py` — ported from an earlier internal `.claude/hooks/` guard (JavaScript),
  ported to Python.
- `guard-secrets.py`, `guard-key-literals.py` — ported from `.agents/hooks/` in
  ai-builder-starter-kit
  (the kit has no top-level licence file, and this pack is MIT).
