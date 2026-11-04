#!/usr/bin/env python3
"""hooks/guard-host-tests.py: in docker mode a card worktree holding a tool's patch (marker inflight/sandbox-<N>) never runs a
test / build / install / server command on the host. Driven the way Claude Code drives it: hook JSON on stdin."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "guard-host-tests.py"
SANDBOX_MSG = "python3 .claude/bin/super-board-lane-delegate.py exec --sandbox --config <cfg> --dir <worktree> -- <cmd>"


class Base(unittest.TestCase):
    provider = "docker"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "repo"
        self.wt = self.root / ".claude" / "worktrees" / "issue-3-add-thing"
        self.other = self.root / ".claude" / "worktrees" / "issue-4-other"
        for d in (self.wt, self.other, self.root / ".claude" / "bin", self.root / ".claude" / "super-board" / "configs",
                  self.root / ".claude" / "super-board" / "inflight"):
            d.mkdir(parents=True)
        self.config(self.provider)
        (self.root / ".claude" / "super-board" / "inflight" / "sandbox-3").write_text(
            json.dumps({"tool": "codex", "nonce_sha256": "a" * 64, "commit": "b" * 40}))

    def tearDown(self):
        self.tmp.cleanup()

    def config(self, provider):
        cfg = {"pilot": {"sandbox": {"provider": provider}}} if provider else {"pilot": {}}
        (self.root / ".claude" / "super-board" / "configs" / "p.json").write_text(json.dumps(cfg))

    def run_hook(self, command, cwd=None, tool="Bash", raw=None):
        payload = raw if raw is not None else json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool,
                                                          "tool_input": {"command": command}, "cwd": str(cwd or self.wt)})
        p = subprocess.run([sys.executable, str(HOOK)], input=payload, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None

    def deny(self, command, cwd=None, tool="Bash"):
        out = self.run_hook(command, cwd, tool)
        self.assertIsNotNone(out, f"expected deny: {command}")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(SANDBOX_MSG, out["permissionDecisionReason"])
        self.assertNotIn("fails closed", out["permissionDecisionReason"], f"denied only because the guard crashed: {command}")
        self.assertIn("marker .claude/super-board/inflight/sandbox-3", out["permissionDecisionReason"])
        return out

    def allow(self, command, cwd=None, tool="Bash"):
        out = self.run_hook(command, cwd, tool)
        self.assertIsNone(out, f"expected allow: {command}: {out}")


class DeniedRunners(Base):
    def test_runners_in_the_worktree_are_denied(self):
        for cmd in ("npm test", "npm run build", "npm start", "npm ci", "npm install", "pnpm test", "pnpm exec vitest", "yarn test",
                    "yarn install", "npx vitest run", "npx playwright test", "bun test", "bun x tsc", "bunx vite", "pnpm dlx x",
                    "node scripts/build.js", "node -e 1", "python3 -m pytest", "python -m unittest", "python3 manage.py test",
                    "python3 -c 'print(1)'", "py -m pytest -x", "pytest -q", "vitest run", "jest --ci", "playwright test",
                    "tsc -p .", "vite build", "next build", "make test", "tox", "nox -s tests", "poetry run pytest",
                    "uv run pytest", "cargo test", "go test ./...", "dotnet test",
                    "pwsh ./build.ps1", "powershell -File .\\build.ps1", "bash ./run-tests.sh", "sh scripts/test.sh",
                    "./gradlew test", "./node_modules/.bin/vitest", "source env.sh", "bash"):
            with self.subTest(cmd):
                self.deny(cmd)

    def test_the_same_commands_through_powershell_and_wrappers(self):
        for cmd in ("Set-Location . ; npm test", "& npm test", "npm test | Out-Null", "env CI=1 npm test", "time npm test",
                    "timeout 60 npm test", "sudo -u x npm test", "cmd /c npm test", 'cmd /c "npm test"',
                    'bash -c "npm test"', "sh -c 'cd . && pytest'", 'pwsh -Command "npm test"', 'powershell -c "pytest -q"',
                    'eval "npm test"', 'Invoke-Expression "npm test"', "echo hi && npm test", "ls; npm test",
                    "git status && python3 -m pytest", "Start-Process npm -ArgumentList test", "xargs npm test",
                    "find . -name x -exec npm test ;", "git rebase -x 'npm test' main", "git bisect run npm test",
                    "git submodule foreach npm test", "git -c core.pager='npm test' log", "rg --pre ./x foo"):
            with self.subTest(cmd):
                self.deny(cmd, tool="PowerShell" if cmd.startswith(("Set-", "&", "Invoke", "Start-")) else "Bash")

    def test_redirected_directories_count(self):
        # from the main repo root, a path that points into the marked worktree is as bad as standing in it
        for cmd in (f"cd {self.wt} && npm test", f"cd .claude/worktrees/issue-3-add-thing && pytest",
                    "Set-Location .claude/worktrees/issue-3-add-thing; npm test",
                    f"git -C {self.wt} bisect run make", f"npm --prefix {self.wt} test", "pnpm --dir .claude/worktrees/issue-3-add-thing test",
                    "make -C .claude/worktrees/issue-3-add-thing", "yarn --cwd .claude/worktrees/issue-3-add-thing test",
                    "cargo test --manifest-path .claude/worktrees/issue-3-add-thing/Cargo.toml",
                    "bash .claude/worktrees/issue-3-add-thing/scripts/test.sh", "python3 .claude/worktrees/issue-3-add-thing/x.py",
                    "./.claude/worktrees/issue-3-add-thing/run.sh", 'bash -c "cd .claude/worktrees/issue-3-add-thing && npm test"'):
            with self.subTest(cmd):
                self.deny(cmd, cwd=self.root)

    def test_a_directory_known_only_at_run_time_is_judged_by_any_marker(self):
        self.deny("cd $WT && npm test", cwd=self.root)
        self.deny("cd $env:WT; npm test", cwd=self.root, tool="PowerShell")
        self.deny('npm --prefix "$(pwd)/wt" test', cwd=self.root)
        self.allow("cd $WT && git status", cwd=self.root)


class Bypasses(Base):
    """Round 3: the decision inside a marked worktree is an ALLOWLIST. Every way round the old denylist is denied."""

    def test_names_that_are_not_plain_words_or_not_allowlisted(self):
        for cmd in ("./x.sh", ".\\x.ps1", '& ".\\x.ps1"', "& .\\x.ps1", "x=npm; $x test", "$(echo npm) test", "`echo npm` test",
                    "alias t=npm; t test", "perl -e 'system(\"npm test\")'", "awk 'BEGIN{system(\"npm test\")}'", "sed '1e npm test' a",
                    "sed -n '1e npm test' a", "sed -i s/a/b/ f", "sed -n -e 1p f", "tsx x.ts", "ts-node x.ts", "busybox sh -c x", "watch npm test",
                    "parallel npm ::: test", "Invoke-Item .\\x.ps1", "vim -c '!npm test'", 'n"p"m test', "n\\pm test", ". ./env.sh",
                    "ruby -e 1", "bash .claude/bin/evil.sh", "bash .claude/bin/super-board-card.sh;npm test", ".claude/bin/../../x.sh",
                    "bash .claude/bin/../../x.sh", "bash .claude/bin/super-board-*.sh", "bash ./.claude/bin/super-board-card.sh",
                    "bash -x .claude/bin/super-board-card.sh", "python3 .claude/bin/super-board-card.sh;npm test", ".claude/bin/super-board-card.sh && npm test",
                    "bash $OTHER/.claude/bin/super-board-card.sh", "bash /tmp/.claude/bin/super-board-card.sh", "python3 .claude/bin/evil.py", "env npm test", "Start-Process npm", "cmd /c npm test",
                    "find . -delete", "find . -fprint x", "rg --pre ./x foo", "rg --pre-glob '*' foo x", "diff --external=x a b",
                    "gh alias set t 'npm test'", "gh extension exec x", "git frob", "git worktree add ../x",
                    "git config core.hooksPath x", "git config --worktree --unset core.hooksPath", "git config alias.t '!npm test'",
                    "git config --edit", "git -c core.hooksPath=x commit -m y", "git -c core.fsmonitor=x status", "git -c alias.t=x t",
                    "git -c diff.external=x diff", "git -c foo.textconv=x diff", "git -c filter.a.clean=x add .", "git -c merge.a.driver=x merge b",
                    "git -c core.sshCommand=x fetch", "git -c core.editor=x commit", "git --config-env=core.hooksPath=V status",
                    "git diff --ext-diff", "git log --textconv", "git show --ext-diff HEAD", "git rebase --exec 'npm test' main",
                    "git rebase -x x main", "git bisect run x", "git submodule foreach x", "git difftool", "git mergetool",
                    "Invoke-Expression 'npm test'", "pwsh -EncodedCommand AAAA", "powershell -enc AAAA"):
            with self.subTest(cmd):
                self.deny(cmd, tool="PowerShell" if ".\\" in cmd or "Invoke" in cmd or "pwsh" in cmd or "powershell" in cmd or "Start-" in cmd else "Bash")

    def test_the_three_ways_the_skills_call_board_scripts_are_allowed(self):
        root = str(self.root).replace("\\", "/")
        for cmd in ("python3 .claude/bin/super-board-status.py --x", ".claude/bin/super-board-card.sh 3", "bash .claude/bin/super-board-pr-body.sh a",
                    "sh .claude/bin/super-board-env-check.sh K", '"$CLAUDE_PROJECT_DIR"/.claude/bin/super-board-card.sh 3',
                    "bash $CLAUDE_PROJECT_DIR/.claude/bin/super-board-card.sh", f"bash {root}/.claude/bin/super-board-run.sh",
                    f"{root}/.claude/bin/super-board-run.sh x", "python .claude/bin/super-board-status.py", f"py {root}/.claude/bin/super-board-status.py"):
            with self.subTest(cmd):
                self.allow(cmd)

    def test_the_git_wrapper_is_judged_as_a_git_command_line(self):
        sha = "a" * 40
        root = str(self.root).replace("\\", "/")
        for cmd in (f".claude/bin/super-board-git.sh -- checkout {sha}", ".claude/bin/super-board-git.sh -- worktree add x",
                    ".claude/bin/super-board-git.sh -- rebase --exec x", f"bash .claude/bin/super-board-git.sh -- checkout {sha}",
                    ".claude/bin/super-board-git.sh -- fetch --upload-pack=x", ".claude/bin/super-board-git.sh status",
                    f"{root}/.claude/bin/super-board-git.sh -- commit -m x", ".claude/bin/super-board-git.sh -- -C /tmp show x"):
            with self.subTest(cmd):
                self.deny(cmd)
        for cmd in (f".claude/bin/super-board-git.sh -- show {sha}", f"bash .claude/bin/super-board-git.sh -- diff {sha}",
                    ".claude/bin/super-board-git.sh -- status --porcelain", ".claude/bin/super-board-git.sh -- worktree list"):
            with self.subTest(cmd):
                self.allow(cmd)

    def test_robot_pushes_are_denied_in_a_hot_context(self):
        sha = "a" * 40
        for cmd in (f".claude/bin/super-board-git.sh --robot -- -C . push origin {sha}:refs/heads/feature-x",
                    f"bash .claude/bin/super-board-git.sh --robot -- -C . push origin {sha}:refs/heads/feature-x",
                    ".claude/bin/super-board-git.sh --robot -- -C . status"):
            with self.subTest(cmd):
                self.deny(cmd)

    def test_robot_reads_are_denied_in_a_hot_context(self):
        sha = "a" * 40
        for cmd in (".claude/bin/super-board-git.sh --robot-read -- -C . ls-remote origin refs/heads/main",
                    f"bash .claude/bin/super-board-git.sh --robot-read -- -C . fetch origin {sha}",
                    ".claude/bin/super-board-git.sh --robot-read -- -C . fetch origin refs/heads/feature-x"):
            with self.subTest(cmd):
                self.deny(cmd)

    def test_the_normal_lane_set_is_allowed_in_the_worktree_and_the_main_repo(self):
        for cmd in ("git status", "git add -A", "git commit -m 'x y'", "git push", "git log --oneline", "git diff", "gh pr view 3", "cat a",
                    "rg foo", "sed -n 1,5p a", "grep -n x a", "ls", "head a", "tail a", "wc -l a",
                    "python3 .claude/bin/super-board-lane-delegate.py exec --sandbox --config c.json --dir . -- npm test"):
            with self.subTest(cmd):
                self.allow(cmd)
                self.allow(cmd, cwd=self.root)
        self.allow("npm test", cwd=self.root)


class Round4(Base):
    """Every payload of the 2nd Opus re-review is denied; the normal lane set still passes. Conservative: whole classes."""
    WT = ".claude/worktrees/issue-5-build"

    def test_assignments_substitution_redirection_and_pipes_into_interpreters(self):
        for cmd in ("GIT_EXTERNAL_DIFF=x git diff", "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=x git status",
                    "GIT_SEQUENCE_EDITOR=x git rebase main", "RIPGREP_CONFIG_PATH=x rg foo", "BASH_ENV=x bash -c true", "PYTHONPATH=x python3 .claude/bin/super-board-status.py",
                    'echo "$(npm test)"', "echo `npm test`", 'cat "`npm test`"', "echo ${x}", "cat <(npm test)", "gc run.ps1 | iex",
                    "Get-Content x | Invoke-Expression", "gc x | Invoke-Command", "cat x | sh", "cat x | python3", "export X=1; git status",
                    "env X=1 git status", '$env:X="1"; git status', "set X=1", "git status & npm test", "git status &",
                    "echo x > .claude/bin/super-board-pwn.sh && bash .claude/bin/super-board-pwn.sh", "echo x >> a", "echo x 2> err.txt", "echo x &> f",
                    "cat a | tee b", "Out-File x", "Set-Content a b", "Add-Content a b", "New-Item a", "echo x >a", "git log > out.txt",
                    "bash .claude/bin/super-board-pwn.sh", "python3 .claude/bin/super-board-pwn.py", ".claude/bin/super-board-pwn.sh",
                    "bash .claude/bin/super-board.sh"):
            with self.subTest(cmd):
                self.deny(cmd, tool="PowerShell" if "iex" in cmd or "Invoke" in cmd or "Out-File" in cmd or "$env" in cmd or "Content" in cmd or "New-Item" in cmd else "Bash")

    def test_git_routes_that_run_a_command(self):
        for cmd in ("git fetch --upload-pack=x .", "git fetch --upload-pack x .", "git push --receive-pack=x .", "git pull --upload-pack=x",
                    "git clone -u x a b", "git fetch -u x", "git rebase -qx 'npm test' main", "git rebase -xq main", "git rebase -i main",
                    "git rebase --interactive main", "git rebase --exec x main", "git push --exec=x", "git grep -Ox foo", "git grep -O foo",
                    "git grep --open-files-in-pager=x foo", "git -C / status", "git -C ../../../.. status", "git -C /tmp log", "git -C $X status",
                    "git --exec-path=/x status"):
            with self.subTest(cmd):
                self.deny(cmd)
        for cmd in ("git -C . status", "git rebase main", "git rebase -q main", "git fetch origin", "git pull", "git push -u origin HEAD",
                    "git grep -n foo", "git commit -t msg.txt -m x"):
            with self.subTest(cmd):
                self.allow(cmd)

    def test_any_mention_of_a_marked_worktree_from_any_cwd(self):
        wt = self.WT
        for cmd in (f"python -m pytest {wt}", f"pytest -k foo {wt}", f"go test ./{wt}/...", f"node -r x {wt}/a.js", f"npm test --prefix ../{wt}",
                    f"npm test --prefix={wt}", f"make -C {wt}", f"CWD={wt} npm test", f"npm test --root=.\\{wt}", f"npm test {self.root.as_posix()}/{wt}",
                    "npm test .claude/worktrees/issue-5*", "npm test .claude/worktrees/ISSUE-~1", "npm test .claude/WORKTR~1/x",
                    "npm test .claude/work*/x", "npm test .c*/worktrees/x", "npm test ../issue-5-build", "cd .claude/worktrees/issue-5*; npm test",
                    "cd .claude/worktrees/ISSUE-~1; npm test"):
            with self.subTest(cmd):
                self.deny(cmd, cwd=self.root)

    def test_cd_tracking_gives_up_conservatively(self):
        wt = self.WT
        for cmd in (f"cd {wt}; cd /; cd -; npm test", f"pushd {wt}; pushd /; popd; npm test", "cd -; npm test", "cd; npm test", "cd ~2; npm test",
                    "cd ../*; npm test", "cd $X; npm test", "popd; npm test", "Pop-Location; npm test", "Set-Location $X; npm test",
                    "cd .claude/*/issue-3-add-thing; npm test"):
            with self.subTest(cmd):
                self.deny(cmd, cwd=self.root)
        self.deny("cd -; npm test", cwd=self.other)
        self.allow("cd /tmp; npm test", cwd=self.root)

    def test_the_normal_lane_set_still_passes(self):
        for cmd in ("git add -A", "git commit -m 'fix: x'", "git push origin HEAD", "git log --oneline -3", "git diff", "git status 2>/dev/null",
                    "git status 2>&1", "gh pr view 3", "gh pr comment 3 --body 'ok `npm test` passed'", "gh pr edit 3 --title x",
                    "python3 .claude/bin/super-board-status.py --x", "bash .claude/bin/super-board-pr-body.sh a", ".claude/bin/super-board-card.sh 3",
                    "cat a.ts", "rg -n foo 2>/dev/null", "sed -n 1,5p a", "ls >/dev/null",
                    "python3 .claude/bin/super-board-lane-delegate.py exec --sandbox --config c.json --dir . -- npm test"):
            with self.subTest(cmd):
                self.allow(cmd)
                self.allow(cmd, cwd=self.root)

    def test_the_hook_script_set_equals_the_installed_super_board_scripts(self):
        import importlib.util, re as _re
        spec = importlib.util.spec_from_file_location("guard", HOOK)
        g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(g)
        text = (HOOK.parents[1] / "install.sh").read_text(encoding="utf-8")
        listed = _re.search(r"for script in (.*?); do", text, _re.S).group(1).split()
        self.assertEqual(set(g.BIN_SCRIPTS), {n for n in listed if n.startswith("super-board-")})


class Round5(Base):
    """Final round: quote / escape / string-concatenation / symlink spellings of a worktree path are all caught."""

    def test_split_spellings_of_a_worktree_path(self):
        for cmd in ('node .claude/work""trees/iss""ue-5/x.js', "npm --prefix .claude/work''trees/iss''ue-5 test",
                    "node .claude/work\\trees/iss\\ue-5/x.js", 'pytest .claude/work"trees"/issue""-5', "pytest .claude/wo'rk'trees/x",
                    "go test ./.claude/work\"tre\"es/issue-5/...", "make -C .claude/wor''ktrees/issue-5"):
            with self.subTest(cmd):
                self.deny(cmd, cwd=self.root)

    def test_git_c_with_a_split_path_is_judged_by_the_subcommand(self):
        self.allow("git -C .claude/work''trees/iss''ue-5 status", cwd=self.root)
        self.deny("git -C .claude/work''trees/iss''ue-5 bisect run x", cwd=self.root)
        self.deny("git -C .claude/work''trees/iss''ue-5 -c core.pager=x log", cwd=self.root)

    def test_powershell_string_concatenation(self):
        for cmd in ("node ('.claude/wo'+'rktrees/iss'+'ue-5/x.js')", "npm --prefix ('.claude/wo'+'rktrees') test",
                    "pytest (Join-Path 'a' 'b') + 'x'", "node ('a'+'b')"):
            with self.subTest(cmd):
                self.deny(cmd, cwd=self.root, tool="PowerShell")

    def test_interpreter_with_a_command_line_script_is_hot(self):
        for cmd in ('python -c "exec(open(\'.claude/wo\'+\'rktrees/iss\'+\'ue-5/x.py\').read())"', "python3 -c 'import os'", "py -c 1",
                    "node -e 1", "node --eval 1", "node -p 1", "perl -e 1", "ruby -e 1", "deno eval 1", "bun -e 1", "pwsh -c 1",
                    "powershell -Command 1", "bash -c true && npm test"):
            with self.subTest(cmd):
                self.deny(cmd, cwd=self.root)

    def test_a_symlink_into_a_marked_worktree_is_marked(self):
        link = self.root / "lnk"
        try:
            os.symlink(self.wt, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symlink here")
        for cmd in ("cd lnk && npm test", "pushd lnk; npm test", "npm --prefix lnk test", "npm --prefix=lnk test", "make -C lnk"):
            with self.subTest(cmd):
                self.deny(cmd, cwd=self.root)
        self.deny("npm test", cwd=link)                       # a cwd that is the link
        self.allow("git -C lnk status", cwd=self.root)

    def test_normal_commands_still_pass_with_quotes_in_them(self):
        for cmd in ("git commit -m 'fix: it works'", 'gh pr comment 3 --body "done"', "git log --format='%h %s'", 'echo "a b"',
                    "gh pr edit 3 --title='x'"):
            with self.subTest(cmd):
                self.allow(cmd)
                self.allow(cmd, cwd=self.root)
        self.allow("npm test", cwd=self.root)


class AllowedCommands(Base):
    def test_read_only_commands_stay_allowed_in_the_worktree(self):
        for cmd in ("git status", "git diff HEAD~1", "git log --oneline -5", "git add -A && git commit -m x", "git push origin HEAD",
                    "gh pr create --fill", "gh issue view 3", "cat package.json", "ls -la", "grep -rn foo src", "rg foo", "find . -name '*.py'",
                    "head -20 README.md", "tail -f x.log", "sed -n 1,20p a.py", "Get-Content a.txt", "Select-String -Pattern x a.txt",
                    "python3 .claude/bin/super-board-lane-delegate.py exec --sandbox --config c.json --dir . -- npm test",
                    "python3 .claude/bin/super-board-card.sh x", "python .claude/bin/super-board-status.py", "py .claude/bin/super-board-status.py",
                    "git -C . rebase main", "git -c user.name=x commit -m y", "gh pr view 3", "cat src/a.ts", "rg -n foo",
                    "git config --get core.hooksPath", "git config --list", "sed -n 1,5p a.py", "diff a b", "echo hi", "pwd", "Get-Location",
                    "Test-Path a", "Get-Item a", "Resolve-Path a", "Get-ChildItem -Recurse", "popd", 'printf "%s" x',
                    "git add -A && git commit -m x && git push", "git log -p --stat", "git diff --stat HEAD~1"):
            with self.subTest(cmd):
                self.allow(cmd)

    def test_the_sandbox_exec_is_allowed_from_the_main_repo_too(self):
        self.allow(f"python3 .claude/bin/super-board-lane-delegate.py exec --sandbox --config c.json --dir {self.wt} -- npm test", cwd=self.root)

    def test_a_script_outside_any_marked_worktree_is_allowed(self):
        self.allow("python3 -m pytest", cwd=self.other)           # card 4 holds no tool patch (no marker)
        self.allow("npm test", cwd=self.root)                     # the main checkout
        self.allow(f"bash {self.root.as_posix()}/.claude/bin/super-board-run.sh")

    def test_other_tools_and_the_main_repo_are_inert(self):
        self.assertIsNone(self.run_hook("npm test", tool="Read"))
        self.allow("npm test", cwd=self.root)


class Activation(Base):
    def test_inert_when_the_provider_is_not_docker(self):
        for provider in ("host", None):
            with self.subTest(provider):
                self.config(provider)
                self.allow("npm test")

    def test_active_config_file_is_honoured(self):
        (self.root / ".claude" / "super-board" / "configs" / "q.json").write_text(json.dumps({"pilot": {"sandbox": {"provider": "host"}}}))
        (self.root / ".claude" / "super-board" / "active").write_text("q\n")
        self.allow("npm test")
        (self.root / ".claude" / "super-board" / "active").write_text("p\n")
        self.deny("npm test")

    def test_no_marker_no_block(self):
        (self.root / ".claude" / "super-board" / "inflight" / "sandbox-3").unlink()
        self.allow("npm test")

    def test_an_unreadable_config_fails_closed(self):
        (self.root / ".claude" / "super-board" / "configs" / "p.json").write_text("{not json")
        self.deny("npm test")


class FailClosed(Base):
    def test_garbage_payload_is_denied(self):
        for raw in ("not json", "[]", json.dumps({"tool_name": "Bash", "tool_input": {"command": 7}})):
            with self.subTest(raw):
                out = self.run_hook("", raw=raw)
                self.assertEqual(out["permissionDecision"], "deny")
                self.assertIn("fails closed", out["permissionDecisionReason"])

    def test_a_command_it_cannot_parse_is_denied(self):
        out = self.run_hook('echo "unterminated && npm test')
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("fails closed", out["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
