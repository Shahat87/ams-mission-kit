#!/usr/bin/env python3
"""hooks/guard-taint.py (G-2, as amended by F6).

GUARDRAIL tests: the hook catches habit and mistakes, not attacks (spec section 15.3). It runs as a subprocess,
`python -I <hook>`, hook JSON on stdin, temp HOME / USERPROFILE. The repos are written by hand (no git needed)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "guard-taint.py"

TAINT_SHA = "deadbeefcafe0123456789abcdef0123456789ab"
DELEGATE_SHA = "feedface00112233445566778899aabbccddeeff"


def fwd(p):
    return str(p).replace("\\", "/")


class TaintBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name).resolve()
        self.home = self.t / "home"
        (self.home / ".mission-control").mkdir(parents=True)
        self.main = self.t / "repo"
        git = self.main / ".git"
        git.mkdir(parents=True)
        (git / "HEAD").write_text("ref: refs/heads/main\n")
        self.config = ('[core]\n\tbare = false\n[remote "origin"]\n\turl = git@github.com:Org/Guarded.git\n'
                       '[branch "work"]\n\tremote = origin\n\tmerge = refs/heads/feat/tainted-1\n')
        (git / "config").write_text(self.config)
        self.inflight = self.main / ".claude" / "super-board" / "inflight"
        self.inflight.mkdir(parents=True)
        (self.inflight / "sandbox-taint-feat-tainted-1.json").write_text(json.dumps(
            {"branch": "feat/tainted-1", "pr": 42, "shas": [TAINT_SHA]}))
        (self.inflight / "sandbox-delegate-abc123").write_text(json.dumps(
            {"tool": "codex", "nonce_sha256": "a" * 64, "commit": DELEGATE_SHA, "pr": 77}))
        (self.inflight / "sandbox-5").write_text(json.dumps({"tool": "codex"}))
        self.card = self.linked("issue-5-thing", "card5", "feature/card5")      # marker sandbox-5
        self.clean = self.linked("issue-6-clean", "card6", "feature/card6")     # no marker, clean branch
        self.up = self.linked("issue-7-up", "card7", "work")                    # upstream is a tainted branch
        self.write_list(["Org/Guarded"])

    def tearDown(self):
        self.tmp.cleanup()

    def linked(self, folder, gitname, branch):
        wt = self.main / ".claude" / "worktrees" / folder
        gd = self.main / ".git" / "worktrees" / gitname
        gd.mkdir(parents=True)
        (gd / "HEAD").write_text("ref: refs/heads/%s\n" % branch)
        (gd / "commondir").write_text("../..\n")
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: %s\n" % fwd(gd))
        return wt

    def write_list(self, repos):
        entries = [{"repo": r, "common_dir": None, "added": "2026-10-28T00:00:00Z"} for r in repos]
        (self.home / ".mission-control" / "guarded-repos.json").write_text(json.dumps({"version": 1, "repos": entries}))

    def run_cmd(self, command, cwd=None, project=None, tool="Bash"):
        env = dict(os.environ)
        env["HOME"] = env["USERPROFILE"] = str(self.home)
        env["CLAUDE_PROJECT_DIR"] = str(project or self.main)
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command},
                   "cwd": str(cwd or self.main)}
        return subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=60)

    def assertDenied(self, r, msg=""):
        self.assertEqual(r.returncode, 2, "%s: expected deny, got rc=%s err=%r" % (msg, r.returncode, r.stderr))
        self.assertIn("Blocked (guardrail G-2): ", r.stderr, msg)
        self.assertIn("would put tool-written code on disk (tainted: ", r.stderr, msg)
        self.assertIn("super-board-git.sh show <sha>", r.stderr, msg)

    def assertAllowed(self, r, msg=""):
        self.assertEqual(r.returncode, 0, "%s: expected allow, got rc=%s err=%r" % (msg, r.returncode, r.stderr))


class RuleATests(TaintBase):
    def test_denies_whatever_the_arguments_in_marked_worktree(self):
        for c in ("git pull", "git merge origin/staging", "git rebase main", "git reset --hard", "git checkout main",
                  "git checkout -- file.txt", "git switch main", "git cherry-pick abcdef1", "git restore --source HEAD~1 f",
                  "git apply p.diff", "git read-tree -u HEAD", "git -c core.pager=cat pull", "env git pull",
                  'bash -c "git pull"', "cd . && git merge x"):
            with self.subTest(command=c):
                self.assertDenied(self.run_cmd(c, cwd=self.card), c)

    def test_allows_reads_in_marked_worktree(self):
        for c in ("git status", "git log --oneline", "git diff", "git add -A", "git commit -m x", "git reset --soft HEAD~1",
                  "git restore f", "python3 -m unittest"):
            with self.subTest(command=c):
                self.assertAllowed(self.run_cmd(c, cwd=self.card), c)

    def test_subdirectory_and_cd_and_dash_c(self):
        sub = self.card / "src"
        sub.mkdir()
        self.assertDenied(self.run_cmd("git pull", cwd=sub))
        self.assertDenied(self.run_cmd('cd "%s" && git pull' % fwd(self.card)))
        self.assertDenied(self.run_cmd('git -C "%s" pull' % fwd(self.card)))
        self.assertDenied(self.run_cmd('git -C "%s" pull' % str(self.card)))
        self.assertAllowed(self.run_cmd('cd "%s" && git status' % fwd(self.card)))

    def test_cd_out_of_marked_worktree_is_allowed(self):
        self.assertAllowed(self.run_cmd('cd "%s" && git pull' % fwd(self.main), cwd=self.card))

    def test_upstream_tainted_worktree(self):
        self.assertDenied(self.run_cmd("git pull", cwd=self.up))
        self.assertAllowed(self.run_cmd("git status", cwd=self.up))

    def test_clean_worktree_and_main_checkout_pull_allowed(self):
        self.assertAllowed(self.run_cmd("git pull", cwd=self.clean))
        self.assertAllowed(self.run_cmd("git pull"))
        self.assertAllowed(self.run_cmd("git merge origin/staging", cwd=self.clean))

    def test_tainted_branch_checked_out_in_worktree(self):
        wt = self.linked("issue-8-mc", "card8", "mc-delegate/zz")
        self.assertDenied(self.run_cmd("git pull", cwd=wt))
        wt2 = self.linked("issue-9-rec", "card9", "feat/tainted-1")
        self.assertDenied(self.run_cmd("git merge x", cwd=wt2))


class RuleBTests(TaintBase):
    def test_denies_naming_tainted_ref(self):
        for c in ("git checkout mc-delegate/abc", "git switch mc-delegate/abc", "git checkout origin/mc-delegate/abc",
                  "git worktree add ../x feat/tainted-1", "git worktree add -b n ../x origin/feat/tainted-1",
                  "git merge refs/remotes/origin/feat/tainted-1", "git rebase refs/heads/feat/tainted-1",
                  "git cherry-pick %s" % TAINT_SHA[:8], "git cherry-pick %s" % DELEGATE_SHA[:7],
                  "git pull origin feat/tainted-1", "git reset --hard %s" % TAINT_SHA[:10],
                  "git restore --source=feat/tainted-1 f", "git restore --source feat/tainted-1 f",
                  "git -C %s checkout mc-delegate/q" % fwd(self.main), "git checkout mc-delegate/abc~1"):
            with self.subTest(command=c):
                self.assertDenied(self.run_cmd(c), c)

    def test_allows_untainted_refs_and_short_prefixes(self):
        for c in ("git checkout main", "git checkout -b feature/new", "git cherry-pick %s" % TAINT_SHA[:6],
                  "git cherry-pick 0123456", "git worktree add ../x feature/card6", "git merge origin/staging",
                  "git reset --hard HEAD", "git log mc-delegate/abc", "git diff feat/tainted-1", "git status"):
            with self.subTest(command=c):
                self.assertAllowed(self.run_cmd(c), c)

    def test_gh_pr_checkout(self):
        self.assertDenied(self.run_cmd("gh pr checkout 42"))
        self.assertDenied(self.run_cmd("gh pr checkout 77"))
        self.assertDenied(self.run_cmd("gh pr checkout https://github.com/Org/Guarded/pull/42"))
        self.assertDenied(self.run_cmd("gh pr checkout feat/tainted-1"))
        self.assertDenied(self.run_cmd("gh --repo Org/Guarded pr checkout 42"))
        self.assertAllowed(self.run_cmd("gh pr checkout 43"))
        self.assertAllowed(self.run_cmd("gh pr view 42"))

    def test_powershell_tool(self):
        self.assertDenied(self.run_cmd("git checkout mc-delegate/abc", tool="PowerShell"))


class ScopeTests(TaintBase):
    def test_unguarded_repo_allows_everything(self):
        self.write_list(["someone/else"])
        for c, cwd in (("git pull", self.card), ("git checkout mc-delegate/abc", self.main), ("gh pr checkout 42", self.main)):
            self.assertAllowed(self.run_cmd(c, cwd=cwd), c)

    def test_missing_list_means_guarded(self):
        (self.home / ".mission-control" / "guarded-repos.json").unlink()
        self.assertDenied(self.run_cmd("git pull", cwd=self.card))

    def test_non_git_tools_pass(self):
        r = self.run_cmd("ls", cwd=self.card)
        self.assertAllowed(r)
        env = dict(os.environ)
        env["HOME"] = env["USERPROFILE"] = str(self.home)
        env["CLAUDE_PROJECT_DIR"] = str(self.main)
        r = subprocess.run([sys.executable, "-I", str(HOOK)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60,
                           input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {},
                                             "cwd": str(self.card)}))
        self.assertEqual(r.returncode, 0)

    def test_heredoc_with_apostrophe_is_allowed(self):
        c = "git commit -F - <<'EOF'\nDon't do it\nEOF"
        self.assertAllowed(self.run_cmd(c))
        self.assertAllowed(self.run_cmd(c, cwd=self.clean))
        (self.home / ".mission-control" / "guarded-repos.json").unlink()
        self.assertAllowed(self.run_cmd(c))

    def test_heredoc_body_dropped_unless_fed_to_a_shell(self):
        self.assertAllowed(self.run_cmd("cat <<EOF\ngit pull\nEOF", cwd=self.card))
        self.assertDenied(self.run_cmd("bash <<'EOF'\ngit pull\nEOF", cwd=self.card))

    def test_facts_are_lazy_and_corrupt_record_fails_closed_for_git(self):
        (self.inflight / "sandbox-taint-bad.json").write_text("{corrupt")
        self.assertAllowed(self.run_cmd("ls", cwd=self.card))
        self.assertAllowed(self.run_cmd("cat README.md | grep x"))
        r = self.run_cmd("git checkout x")
        self.assertEqual(r.returncode, 2)
        self.assertIn("fails closed", r.stderr)
        r = self.run_cmd("gh pr checkout 43")
        self.assertEqual(r.returncode, 2)

    def test_parse_failure_denies_only_with_git_or_gh(self):
        (self.home / ".mission-control" / "guarded-repos.json").unlink()
        self.assertAllowed(self.run_cmd('echo "unterminated', cwd=self.card))
        self.assertAllowed(self.run_cmd('Get-ChildItem "C:\\Users\\x\\" | Select -First 3', tool="PowerShell"))
        r = self.run_cmd('git pull "x', cwd=self.card)
        self.assertEqual(r.returncode, 2)
        self.assertIn("fails closed", r.stderr)
        self.assertEqual(self.run_cmd('gh.exe pr view "x').returncode, 2)

    def test_powershell_rules_and_comments(self):
        (self.home / ".mission-control" / "guarded-repos.json").unlink()
        self.assertAllowed(self.run_cmd('echo "C:\\path\\"', tool="PowerShell"))
        self.assertAllowed(self.run_cmd("git commit -m @'\nfix: don't crash\n'@", tool="PowerShell", cwd=self.card))
        self.assertAllowed(self.run_cmd("cat x # don't"))
        self.assertDenied(self.run_cmd("git commit -m @'\nx\n'@ ; git pull", tool="PowerShell", cwd=self.card))
        self.assertDenied(self.run_cmd("echo 'it''s'; git pull", tool="PowerShell", cwd=self.card))

    def test_interpreter_heredocs_are_skipped(self):
        self.assertAllowed(self.run_cmd("python - <<'EOF'\n# it's a check\nprint(1)\nEOF", cwd=self.card))
        self.assertAllowed(self.run_cmd("python3 <<'EOF'\ngit pull\nEOF", cwd=self.card))

    def test_arithmetic_shift_is_not_a_heredoc(self):
        self.assertDenied(self.run_cmd("echo $((x<<y))\ngit pull", cwd=self.card))

    def test_remote_prefixes_and_gh_options(self):
        self.assertDenied(self.run_cmd("git checkout refs/remotes/upstream/mc-delegate/x"))
        with open(self.main / ".git" / "config", "a") as fh:
            fh.write('[remote "fork"]\n\turl = https://example.com/x.git\n')
        self.assertDenied(self.run_cmd("git checkout fork/feat/tainted-1"))
        self.assertAllowed(self.run_cmd("git checkout stranger/feat/tainted-1"))
        self.assertDenied(self.run_cmd("git read-tree -u -m %s" % TAINT_SHA[:8]))
        self.assertDenied(self.run_cmd("gh pr checkout -R owner/repo 42"))
        self.assertDenied(self.run_cmd("gh pr checkout --repo owner/repo 42"))
        self.assertAllowed(self.run_cmd("gh pr checkout -R owner/repo 43"))
        self.assertAllowed(self.run_cmd("gh pr checkout -b mine 43"))

    def test_hash_after_brace_is_not_a_comment(self):
        self.assertDenied(self.run_cmd("echo ${#a}; git pull", cwd=self.card))
        self.assertDenied(self.run_cmd("echo ${#a[@]}; git pull", cwd=self.card))

    def test_powershell_block_comments(self):
        self.assertDenied(self.run_cmd("<# x #> git pull", cwd=self.card, tool="PowerShell"))
        self.assertAllowed(self.run_cmd("<# it's #> git status", cwd=self.card, tool="PowerShell"))

    def test_parse_failure_with_git_needs_taint_facts(self):
        (self.home / ".mission-control" / "guarded-repos.json").unlink()
        for f in list(self.inflight.iterdir()):
            f.unlink()
        c = "git commit -m $'it\x5c's done'"
        self.assertAllowed(self.run_cmd(c), "no taint facts")
        self.assertEqual(self.run_cmd('git checkout "mc-delegate/x').returncode, 2, "names mc-delegate")
        (self.inflight / "sandbox-taint-x.json").write_text(json.dumps({"branch": "b", "pr": 1, "shas": []}))
        r = self.run_cmd(c)
        self.assertEqual(r.returncode, 2)
        self.assertIn("fails closed", r.stderr)

    def test_fails_closed(self):
        env = dict(os.environ)
        env["HOME"] = env["USERPROFILE"] = str(self.home)
        env["CLAUDE_PROJECT_DIR"] = str(self.main)
        r = subprocess.run([sys.executable, "-I", str(HOOK)], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60,
                           input="{not json")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.run_cmd('git pull "unterminated', cwd=self.card).returncode, 2)


if __name__ == "__main__":
    unittest.main()
