#!/usr/bin/env python3
"""hooks/guard-orchestrator.py (issue #69): while a default workflow-backend run holds its wave lock, the parent
orchestrator cannot edit files or run product build / test / install commands.

GUARDRAIL tests: the hook catches habit and mistakes, not attacks (spec section 15.3). It runs as a subprocess,
`python -I <hook>`, hook JSON on stdin, the project a temp dir. No model calls, no network.
The path-based fallback mode is out of scope (settled by the live probe: `agent_id` identifies the caller).
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "guard-orchestrator.py"
SESSION = "sess-orch-1"
TAIL = "do not retry in another form."
LOCK_REL = ".claude/super-board/inflight/workflow-wave.lock"
SESSION_REL = ".claude/super-board/inflight/orchestrator-session"


def fwd(p):
    return str(p).replace("\\", "/")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "repo"
        for d in (self.root / ".claude" / "super-board" / "inflight",
                  self.root / ".claude" / "super-board" / "configs",
                  self.root / ".claude" / "bin"):
            d.mkdir(parents=True)
        self.take_lock()

    def tearDown(self):
        self.tmp.cleanup()

    def take_lock(self, slug="p"):
        (self.root / LOCK_REL).write_text("SLUG=%s\nSTARTED=2026-01-01T00:00:00Z\n" % slug)
        (self.root / SESSION_REL).write_text(SESSION + "\n")

    def drop_lock(self):
        (self.root / LOCK_REL).unlink()
        (self.root / SESSION_REL).unlink(missing_ok=True)

    def run_hook(self, tool, tool_input, cwd=None, session_id=SESSION, agent_id=None,
                 agent_type=None, env_extra=None, raw=None, scratchpad=None):
        if raw is None:
            payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input,
                       "cwd": fwd(cwd or self.root), "session_id": session_id}
            if agent_id is not None:
                payload["agent_id"] = agent_id
                if agent_type is not None:
                    payload["agent_type"] = agent_type
            if scratchpad is not None:
                payload["scratchpad_dir"] = scratchpad
            raw = json.dumps(payload)
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = fwd(self.root)
        env.update(env_extra or {})
        p = subprocess.run([sys.executable, "-I", str(HOOK)], input=raw, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
        return p

    def bash(self, command, **kw):
        return self.run_hook("Bash", {"command": command}, **kw)

    def assertDenied(self, p, label, rule=None):
        self.assertEqual(p.returncode, 2, "%s: expected deny, got rc=%s out=%r" % (label, p.returncode, p.stdout))
        msg = p.stderr.strip()
        self.assertTrue(msg, "%s: a deny needs a message" % label)
        self.assertTrue(msg.endswith(TAIL), "%s: the message must end with the no-retry rule: %r" % (label, msg))
        self.assertLess(len(msg), 300, "%s: the message must stay under 300 chars: %r" % (label, msg))
        if rule is not None:
            rows = self.violations()
            self.assertTrue(rows, "%s: the deny must append one violation-log line" % label)
            self.assertEqual(rows[-1]["rule"], rule,
                             "%s: the violation log must classify this denial as %r, not %r"
                             % (label, rule, rows[-1]["rule"]))
        return p

    def assertAllowed(self, p, label):
        self.assertEqual(p.returncode, 0, "%s: expected allow, got rc=%s err=%r" % (label, p.returncode, p.stderr))
        self.assertEqual(p.stdout, "", "%s: an allow prints nothing" % label)

    def deny_bash(self, command, label=None, rule=None, **kw):
        return self.assertDenied(self.bash(command, **kw), label or command, rule=rule)

    def allow_bash(self, command, label=None, **kw):
        return self.assertAllowed(self.bash(command, **kw), label or command)

    def violations(self):
        path = self.root / ".claude" / "super-board" / "orchestrator-violations.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class GhRow(Base):
    def test_allowed_gh_reads(self):
        for c in ("gh issue view 5 --json number,title", "gh issue edit 5 --remove-assignee sb-bot",
                  "gh issue comment 5 --body-file card-5/note.md", "gh pr view 5", "gh pr diff 5 --name-only",
                  "gh pr checks 5", "gh pr comment 5 --body-file card-5/note.md",
                  "gh project item-list 7", "gh project item-edit --id item_5 --field-id f --project-id p",
                  "gh api repos/o/r/issues/5/comments", "gh api graphql -f query='query{rateLimit{remaining}}'",
                  "gh api -X DELETE repos/o/r/issues/5/assignees -f 'assignees[]=sb-bot'"):
            with self.subTest(command=c):
                self.allow_bash(c)

    def test_gh_near_misses_are_denied(self):
        for c in ("gh pr merge 5 --squash", "gh pr checkout 5", "gh repo clone o/r", "gh release create v1",
                  "gh auth status", "gh issue delete 5", "gh pr edit 5 --title x", "gh", "gh issue"):
            with self.subTest(command=c):
                self.deny_bash(c, rule="not-allowlisted")


class GitReadRow(Base):
    def test_allowed_git_reads(self):
        for c in ("git status", "git status --porcelain", "git log --oneline -3", "git rev-parse HEAD",
                  "git ls-remote --heads origin 'issue-5-*'", "git fetch origin", "git fetch --quiet origin",
                  "git worktree list", "git worktree list --porcelain",
                  "git -C .claude/worktrees/issue-5-build status --porcelain",
                  "git -C .claude/worktrees/issue-5-build status --porcelain --ignored"):
            with self.subTest(command=c):
                self.allow_bash(c)

    def test_git_read_near_misses_are_denied(self):
        for c in ("git push origin HEAD", "git commit -m x", "git pull", "git rebase main",
                  "git worktree add .claude/worktrees/x -b feat", "git -C .claude/worktrees/issue-5-build add -A",
                  "git -C .claude/worktrees/issue-5-build status --short", "git -c x=y status",
                  "git fetch --upload-pack=x origin", "git ls-remote --upload-pack=x origin",
                  "git log --output=x", "git log --output out.txt", "git fetch --output=f",
                  "git", "git worktree list --porcelain extra"):
            with self.subTest(command=c):
                self.deny_bash(c)


class GitRemoveRow(Base):
    def test_worktree_remove_without_force_is_allowed(self):
        self.allow_bash("git worktree remove .claude/worktrees/issue-5-build")
        self.allow_bash("git worktree remove .claude/worktrees/issue-5-build-qa")

    def test_worktree_remove_near_misses_are_denied(self):
        for c in ("git worktree remove --force .claude/worktrees/issue-5-build",
                  "git worktree remove -f .claude/worktrees/issue-5-build",
                  "git worktree remove ../elsewhere", "git worktree remove /tmp/x",
                  "git worktree remove .claude/worktrees/issue-5-build extra"):
            with self.subTest(command=c):
                self.deny_bash(c)


class ProcessRow(Base):
    def test_allowed_process_reads(self):
        for c in ("pgrep -f 'super-board-run.sh'", "pgrep -f 'claude -p .*super-board run'",
                  "kill -0 1234", "ps -p 1234 -o command=", "ps -p 42 -o pid="):
            with self.subTest(command=c):
                self.allow_bash(c)

    def test_process_near_misses_are_denied(self):
        for c in ("kill -9 1234", "kill -0 12a4", "kill -0", "kill 1234", "ps aux", "ps -p 1234 -o command",
                  "pgrep super-board-run.sh", "pgrep -l super-board-run.sh"):
            with self.subTest(command=c):
                self.deny_bash(c)


class ReadRow(Base):
    def test_allowed_reads(self):
        for c in ("jq -r '.repo.remote' .claude/super-board/configs/p.json",
                  "jq -r '.max_workers // 0' .claude/super-board/configs/p.json",
                  "jq -r '.hooks' .claude/settings.json",
                  "jq -r '.env.SB_ORCH_GUARD_OFF // \"unset\"' .claude/settings.json",
                  "jq -r '.hooks' " + fwd(self.root / ".claude" / "settings.json"),
                  "date -u"):
            with self.subTest(command=c):
                self.allow_bash(c)

    def test_read_near_misses_are_denied(self):
        for c in ("jq -r . other.json", "jq '.repo.remote' .claude/super-board/configs/p.json",
                  "jq -r '.repo.remote' .claude/super-board/active", "date", "date -u +%s",
                  "jq -r '.repo.remote' .claude/super-board/configs/../configs/p.json",
                  "jq -r '.hooks' .claude/settings.local.json", "jq '.hooks' .claude/settings.json",
                  "jq -r '.hooks' .claude/settings.json extra", "jq -r '.hooks' ../.claude/settings.json"):
            with self.subTest(command=c):
                self.deny_bash(c)


class HelperRow(Base):
    def test_allowed_helpers(self):
        for c in ("bash .claude/bin/super-board-wave-plan.sh --config .claude/super-board/configs/p.json",
                  "bash .claude/bin/super-board-preflight.sh --config c.json",
                  "bash .claude/bin/super-board-usage.sh check --config c.json",
                  "bash .claude/bin/super-board-throttle.sh --config c.json --results r.json",
                  "bash .claude/bin/super-board-card.sh --config c.json audit",
                  "bash .claude/bin/super-board-stop.sh p",
                  "bash .claude/bin/super-board-orch.sh check-wave",
                  "python3 .claude/bin/super-board-roadmap.py sync --config c.json --wave 3",
                  "python3 .claude/bin/super-board-github-read.py --kind issue -- issue view 5 --json body",
                  "python3 .claude/bin/super-board-skills-ready.py --root . --host claude",
                  "bash $CLAUDE_PROJECT_DIR/.claude/bin/super-board-card.sh --config c.json audit"):
            with self.subTest(command=c):
                self.allow_bash(c)

    def test_helper_near_misses_are_denied(self):
        for c in ("bash .claude/bin/super-board-run.sh", "bash .claude/bin/super-board-merge-gate.sh",
                  "python3 .claude/bin/super-board-lane-delegate.py exec --sandbox --config c --dir . -- npm test",
                  "sh .claude/bin/super-board-card.sh audit", "python .claude/bin/super-board-roadmap.py sync",
                  "bash ../.claude/bin/super-board-card.sh audit",
                  "bash .claude/bin/super-board-card.sh; npm test", "bash /tmp/.claude/bin/super-board-card.sh"):
            with self.subTest(command=c):
                self.deny_bash(c)


class EventsHelperRow(Base):
    # issue #66: the event dispatcher reads GitHub read-only and keeps its state under
    # .claude/super-board/events/, so each subcommand is an orchestrator helper.
    CFG = ".claude/super-board/configs/p.json"
    EID = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_events_subcommands_are_allowed(self):
        for c in ("python3 .claude/bin/super-board-events.py --config %s poll" % self.CFG,
                  "python3 .claude/bin/super-board-events.py --config %s next" % self.CFG,
                  "python3 .claude/bin/super-board-events.py --config %s claim %s" % (self.CFG, self.EID),
                  "python3 .claude/bin/super-board-events.py --config %s done %s --result advanced" % (self.CFG, self.EID),
                  "python3 .claude/bin/super-board-events.py --config %s fail %s --reason stale" % (self.CFG, self.EID),
                  "python3 .claude/bin/super-board-events.py --config %s status" % self.CFG,
                  "python3 .claude/bin/super-board-events.py --config %s next" % fwd(self.root / ".claude" / "super-board" / "configs" / "p.json"),
                  ".claude/bin/super-board-events.py --config %s next" % self.CFG):
            with self.subTest(command=c):
                self.allow_bash(c)

    def test_events_near_misses_are_denied(self):
        for c in ("python3 .claude/bin/super-board-events.py --config c.json poll && npm test",
                  "python3 .claude/bin/super-board-eventsx.py --config c.json next",
                  "python .claude/bin/super-board-events.py --config c.json next",
                  "sh .claude/bin/super-board-events.py next",
                  "bash ../.claude/bin/super-board-events.py next"):
            with self.subTest(command=c):
                self.deny_bash(c)


class Compound(Base):
    def test_unquoted_metacharacters_are_denied(self):
        for c in ("git status; npm test", "git status && npm test", "git log | jq .", "git log > out.txt",
                  "cat < in.txt", "git status &", "echo `git status`", "echo $(git status)",
                  "git status\nnpm test", "git status \n npm test", "git log 2>err.txt", "git log >&2",
                  "git -C .claude/worktrees/issue-5-build status --porcelain && npm test"):
            with self.subTest(command=c):
                self.deny_bash(c, rule="compound")

    def test_metacharacters_inside_quotes_stay_inert_when_the_command_is_allowlisted(self):
        self.allow_bash("gh issue comment 5 --body \"a ; b && c | d\"")
        self.allow_bash("gh issue comment 5 --body 'run `npm test` and $(deploy)'")

    def test_live_substitutions_inside_double_quotes_are_denied(self):
        self.deny_bash('gh issue comment 5 --body "$(date)"', rule="compound")
        self.deny_bash('gh issue comment 5 --body "run `npm test`"', rule="compound")

    def test_nested_shells_and_wrappers_are_denied(self):
        for c in ("bash -c 'git status'", "sh -c 'git status'", "bash -lc 'git status'", "eval git status",
                  "env git status", "xargs git status", "sudo git status", "cmd /c git status",
                  "wsl git status", "pwsh -c git status", "powershell -c git status"):
            with self.subTest(command=c):
                self.deny_bash(c, rule="compound")

    def test_a_command_that_cannot_be_tokenized_is_denied(self):
        self.deny_bash('git log --format="unterminated', rule="compound")


class Heredoc(Base):
    RECONCILE = "bash .claude/bin/super-board-orch.sh reconcile --config c.json --wave 1 --date-slug 2026-01-01-p"

    def test_a_quoted_heredoc_into_reconcile_is_allowed(self):
        cmd = self.RECONCILE + " <<'EOF'\n{\"cards\":[{\"number\":5}]}\nEOF"
        self.allow_bash(cmd)
        cmd = self.RECONCILE + ' <<"EOF"\n{"cards":[]}\nEOF'
        self.allow_bash(cmd)

    def test_heredocs_are_only_allowed_into_reconcile(self):
        for c in ("cat <<'EOF'\nhi\nEOF",
                  "gh api repos/o/r/issues/5/comments -f body=@- <<'EOF'\nhi\nEOF",
                  "bash .claude/bin/super-board-card.sh audit <<'EOF'\nhi\nEOF",
                  "bash -c 'x' <<'EOF'\nhi\nEOF",
                  "bash ../.claude/bin/super-board-orch.sh reconcile <<'EOF'\n{}\nEOF"):
            with self.subTest(command=c):
                self.deny_bash(c, rule="compound")

    def test_an_unquoted_delimiter_is_denied(self):
        self.deny_bash(self.RECONCILE + " <<EOF\n{}\nEOF", rule="compound")

    def test_junk_around_the_heredoc_is_denied(self):
        self.deny_bash(self.RECONCILE + " <<'EOF' &\n{}\nEOF", rule="compound")
        self.deny_bash(self.RECONCILE + " <<'EOF'\n{}\nEOF\necho more", rule="compound")
        self.deny_bash("git status; " + self.RECONCILE + " <<'EOF'\n{}\nEOF", rule="compound")
        self.deny_bash(self.RECONCILE + " <<'EOF'\nno terminator", rule="compound")
        self.allow_bash("git log --format='a<<b'", "a quoted << stays inert")


class FileToolsAndPowershell(Base):
    def test_file_tools_are_always_denied(self):
        for tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
            with self.subTest(tool=tool):
                p = self.run_hook(tool, {"file_path": fwd(self.root / "src" / "a.ts"), "content": "x"})
                self.assertDenied(p, tool, rule="file-tool")

    def test_powershell_is_always_denied(self):
        for c in ("Get-Content a.txt", "git status"):
            with self.subTest(command=c):
                p = self.run_hook("PowerShell", {"command": c})
                self.assertDenied(p, c, rule="powershell")


class Activation(Base):
    def test_no_lock_means_inert(self):
        self.drop_lock()
        self.allow_bash("npm test")
        p = self.run_hook("Write", {"file_path": fwd(self.root / "src" / "a.ts"), "content": "x"})
        self.assertAllowed(p, "Write without a lock")

    def test_the_lane_exemption(self):
        for agent_id, agent_type in (("lane-1", "general-purpose"), ("lane-2", "workflow-subagent"),
                                     ("lane-3", None)):
            with self.subTest(agent_id=agent_id):
                self.allow_bash("npm test", agent_id=agent_id, agent_type=agent_type)
                p = self.run_hook("Write", {"file_path": "x.ts", "content": "x"},
                                  agent_id=agent_id, agent_type=agent_type)
                self.assertAllowed(p, "lane file write")

    def test_a_session_mismatch_is_inert(self):
        (self.root / SESSION_REL).write_text("sess-other\n")
        self.allow_bash("npm test")
        self.allow_bash("git push origin HEAD")

    def test_the_env_escape(self):
        self.allow_bash("npm test", env_extra={"SB_ORCH_GUARD_OFF": "1"})
        p = self.run_hook("Write", {"file_path": "x.ts", "content": "x"}, env_extra={"SB_ORCH_GUARD_OFF": "1"})
        self.assertAllowed(p, "Write with the escape")
        self.deny_bash("npm test", env_extra={"SB_ORCH_GUARD_OFF": "0"})

    def test_only_the_value_1_escapes(self):
        self.deny_bash("npm test", env_extra={"SB_ORCH_GUARD_OFF": "true"})


class SessionBinding(Base):
    def test_the_hook_writes_the_session_file_when_it_allows_lock_acquire(self):
        self.drop_lock()
        self.allow_bash("bash .claude/bin/super-board-orch.sh lock-acquire p")
        self.assertEqual((self.root / SESSION_REL).read_text(), SESSION + "\n")

    def test_a_bound_session_is_fenced_once_the_lock_exists(self):
        self.drop_lock()
        self.allow_bash("bash .claude/bin/super-board-orch.sh lock-acquire p")
        self.take_lock()
        self.deny_bash("npm test", rule="not-allowlisted")
        self.allow_bash("bash .claude/bin/super-board-orch.sh check-wave")

    def test_a_lane_never_binds_the_session(self):
        self.drop_lock()
        self.allow_bash("bash .claude/bin/super-board-orch.sh lock-acquire p", agent_id="lane-1")
        self.assertFalse((self.root / SESSION_REL).exists(), "a lane must not write the session file")

    def test_a_foreign_session_does_not_take_over_the_binding(self):
        (self.root / SESSION_REL).write_text("sess-other\n")
        self.allow_bash("bash .claude/bin/super-board-orch.sh lock-acquire p", session_id="sess-new")
        self.assertEqual((self.root / SESSION_REL).read_text(), "sess-other\n")

    def test_lock_acquire_with_a_bad_slug_is_not_a_binding_call(self):
        self.drop_lock()
        self.allow_bash("bash .claude/bin/super-board-orch.sh lock-acquire 'bad slug'")
        self.assertFalse((self.root / SESSION_REL).exists())

    def test_a_stale_binding_from_a_crashed_run_blocks_a_new_acquire_until_it_is_cleared(self):
        # The TOCTOU closure: this acquire cannot win the lock while another session is still named,
        # so it is denied instead of being allowed to race it (and possibly run unfenced as the winner).
        self.drop_lock()
        (self.root / SESSION_REL).write_text("sess-crashed\n")
        self.deny_bash("bash .claude/bin/super-board-orch.sh lock-acquire p", rule="stale-session")
        self.assertEqual((self.root / SESSION_REL).read_text(), "sess-crashed\n",
                         "the denial must not overwrite the leftover binding")
        # one lock-release clears the leftover binding (the helper deletes it with the lock),
        # then the same acquire binds
        (self.root / SESSION_REL).unlink()
        self.allow_bash("bash .claude/bin/super-board-orch.sh lock-acquire p")
        self.assertEqual((self.root / SESSION_REL).read_text(), SESSION + "\n")

    def test_a_crashed_session_can_rebind_itself(self):
        self.drop_lock()
        (self.root / SESSION_REL).write_text(SESSION + "\n")
        self.allow_bash("bash .claude/bin/super-board-orch.sh lock-acquire p")
        self.assertEqual((self.root / SESSION_REL).read_text(), SESSION + "\n")


class DocFlow(Base):
    """The exact command shapes run-workflow.md and run.md tell the ORCHESTRATOR to run (the REDs of
    the #69 guard review): when the guard blocks one of these again, the documented workflow is dead."""

    def test_required_evidence_reads_run_in_both_documented_forms(self):
        # run-workflow.md:43 and :211, run.md:9 — the bare shebang form is documented; the fenced
        # python3 form must keep working too.
        for c in ("python3 .claude/bin/super-board-github-read.py --kind issue -- issue view 5 --json number,title,body",
                  ".claude/bin/super-board-github-read.py --kind issue -- issue view 5 --json number,title,body",
                  ".claude/bin/super-board-github-read.py --kind pr -- pr view 9 --json number,state"):
            with self.subTest(command=c):
                self.allow_bash(c)
        self.deny_bash("python .claude/bin/super-board-github-read.py --kind issue -- issue view 5",
                       rule="not-allowlisted")

    def test_precondition_1_verification_ready_is_allowed(self):
        # run-workflow.md:97 and run.md:66 — a resume after a crash (stale lock still held) enforces
        # from the first call, so this precondition must be allowlisted.
        self.allow_bash("python3 .claude/bin/super-board-verification-ready.py --config .claude/super-board/configs/p.json")
        self.allow_bash(".claude/bin/super-board-verification-ready.py --config .claude/super-board/configs/p.json")
        self.deny_bash("python .claude/bin/super-board-verification-ready.py --config c.json",
                       rule="not-allowlisted")

    def test_step_7_migration_check_runs_through_the_helper(self):
        # run-workflow.md:138 — the raw migrations.checks command is never allowlisted; db-check is.
        self.allow_bash("bash .claude/bin/super-board-orch.sh db-check test")
        self.allow_bash("bash .claude/bin/super-board-orch.sh db-check --config .claude/super-board/configs/p.json test")

    def test_the_usage_fallback_and_the_codex_launch_are_allowed(self):
        # run-workflow.md:166 — the Claude-hosted fallback dispatches codex lanes while fenced;
        # "Codex runs" launches super-board-codex-wave.sh as a background task.
        self.allow_bash("bash .claude/bin/super-board-codex-lane.sh --config .claude/super-board/configs/p.json"
                        " --card '{\"number\":5}' --lane build")
        self.allow_bash("bash .claude/bin/super-board-codex-wave.sh --config c.json --cards claimed.json"
                        " --codex --tier medium --output wave-result.json")
        self.allow_bash("bash .claude/bin/super-board-codex-wave.sh --config c.json --cards claimed.json"
                        " --tier high --output wave-result.json")

    def test_every_orchestrator_helper_is_allowlisted(self):
        # the walk over the orchestrator steps in run-workflow.md and run.md (not the lane lifecycles)
        for c in ("bash .claude/bin/super-board-orch.sh check-wave",
                  "python3 .claude/bin/super-board-verification-ready.py --config c.json",
                  "bash .claude/bin/super-board-usage.sh check --config c.json",
                  "bash .claude/bin/super-board-card.sh --config c.json audit",
                  "bash .claude/bin/super-board-wave-plan.sh --config c.json",
                  "python3 .claude/bin/super-board-github-read.py --kind issue -- issue view 5 --json body",
                  "python3 .claude/bin/super-board-skills-ready.py --root . --host claude",
                  "bash .claude/bin/super-board-codex-lane.sh --config c.json --card '{}' --lane qa",
                  "bash .claude/bin/super-board-codex-wave.sh --config c.json --cards c.json --output w.json",
                  "python3 .claude/bin/super-board-roadmap.py sync --config c.json --wave 3",
                  "python3 .claude/bin/super-board-events.py --config .claude/super-board/configs/p.json next",
                  "bash .claude/bin/super-board-stop.sh p",
                  "bash .claude/bin/super-board-host.sh"):
            with self.subTest(command=c):
                self.allow_bash(c)


    def test_the_orchestrator_can_write_its_own_run_files(self):
        # round-2 RED: the doc tells the orchestrator to create cards.json (step 2), claimed-cards.json
        # (step 3 / the Codex launch), fresh-meta.json (--resume) and the run manifest rows and pause/
        # halt notes (docs/super-board/runs/) — Write/Edit/MultiEdit must be allowed exactly there.
        for tool, path, extra in (
                ("Write", ".claude/super-board/inflight/cards.json", {"content": "[]"}),
                ("Write", fwd(self.root / ".claude" / "super-board" / "inflight" / "claimed-cards-w3.json"),
                 {"content": "[]"}),
                ("Edit", fwd(self.root / "docs" / "super-board" / "runs" / "2026-01-01-p.md"),
                 {"old_string": "", "new_string": "| #5 | | | | |"}),
                ("MultiEdit", fwd(self.root / "docs" / "super-board" / "runs" / "2026-01-01-p.md"),
                 {"edits": [{"old_string": "", "new_string": "x"}]})):
            with self.subTest(tool=tool, path=path):
                self.assertAllowed(self.run_hook(tool, dict(file_path=path, **extra)), tool + " " + path)

    def test_a_scratchpad_file_write_is_allowed(self):
        sp = fwd(self.root / ".." / "scratchpads" / "run-2026-01-01-p")
        for tool in ("Write", "Edit"):
            with self.subTest(tool=tool):
                p = self.run_hook(tool, {"file_path": sp + "/cards-w3.json", "content": "[]"}, scratchpad=sp)
                self.assertAllowed(p, "scratchpad " + tool)
        self.assertAllowed(self.run_hook("Write", {"file_path": sp + "/card-5/meta.json", "content": "{}"},
                                         scratchpad=sp + "/"), "scratchpad write with a trailing slash")

    def test_writes_outside_the_run_paths_are_denied(self):
        for path in ("plugin/foo.py", fwd(self.root / "src" / "x.ts"),
                     fwd(self.root / ".claude" / "super-board" / "configs" / "x.json"),
                     fwd(self.root / ".claude" / "hooks" / "guard-orchestrator.py"),
                     fwd(self.root / "docs" / "super-board" / "runs")):
            with self.subTest(path=path):
                self.assertDenied(self.run_hook("Write", {"file_path": path, "content": "x"}),
                                  "Write " + path, rule="file-tool")

    def test_the_dotdot_escape_is_denied(self):
        for path in (".claude/super-board/inflight/../../src/x",
                     fwd(self.root / ".claude" / "super-board" / "inflight" / ".." / ".." / "src" / "x.ts"),
                     "..\\.claude\\super-board\\inflight\\x"):
            with self.subTest(path=path):
                self.assertDenied(self.run_hook("Write", {"file_path": path, "content": "x"}),
                                  "Write .. escape", rule="file-tool")


class RunFileEdges(Base):
    """Edge cases of the run-file write allowance beyond the documented shapes."""

    def test_notebookedit_stays_denied_even_under_the_run_paths(self):
        self.assertDenied(self.run_hook("NotebookEdit",
                                        {"file_path": ".claude/super-board/inflight/x.ipynb", "new_source": "x"}),
                          "NotebookEdit", rule="file-tool")

    def test_no_scratchpad_dir_means_the_scratchpad_write_is_denied(self):
        path = fwd(self.root / "scratch" / "x.json")
        self.assertDenied(self.run_hook("Write", {"file_path": path, "content": "x"}),
                          "scratchpad write without scratchpad_dir", rule="file-tool")

    def test_an_unusable_scratchpad_dir_counts_as_unset(self):
        path = fwd(self.root / "elsewhere" / "x.json")
        for bad in ("", "   ", "/tmp/../elsewhere", 7):
            with self.subTest(scratchpad=bad):
                self.assertDenied(self.run_hook("Write", {"file_path": path, "content": "x"}, scratchpad=bad),
                                  "bad scratchpad_dir", rule="file-tool")

    def test_a_scratchpad_that_is_or_holds_the_project_counts_as_unset(self):
        self.assertDenied(self.run_hook("Write", {"file_path": fwd(self.root / "src" / "x.ts"), "content": "x"},
                                        scratchpad=fwd(self.root)), "scratchpad = project", rule="file-tool")
        self.assertDenied(self.run_hook("Write", {"file_path": fwd(self.root / "src" / "x.ts"), "content": "x"},
                                        scratchpad="C:/"), "scratchpad above the project", rule="file-tool")

    def test_a_relative_scratchpad_dir_is_project_anchored(self):
        self.assertAllowed(self.run_hook("Write", {"file_path": fwd(self.root / "runs-tmp" / "x.json"), "content": "x"},
                                         scratchpad="runs-tmp"), "relative scratchpad")
        self.assertDenied(self.run_hook("Write", {"file_path": fwd(self.root / "src" / "x.ts"), "content": "x"},
                                        scratchpad="runs-tmp"), "relative scratchpad does not open the project",
                          rule="file-tool")


class AuditLog(Base):
    def test_one_schema_line_per_violation(self):
        self.deny_bash("npm test")
        rows = self.violations()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(sorted(row), ["cwd", "head", "rule", "run", "session_id", "sha256", "tool", "ts", "v"])
        self.assertEqual(row["v"], 1)
        self.assertEqual(row["session_id"], SESSION)
        self.assertEqual(row["run"], {"slug": "p", "started": "2026-01-01T00:00:00Z"})
        self.assertEqual(row["tool"], "Bash")
        self.assertEqual(row["rule"], "not-allowlisted")
        self.assertEqual(row["head"], "npm")
        self.assertEqual(row["sha256"], hashlib.sha256(b"npm test").hexdigest())
        self.assertEqual(row["cwd"], fwd(self.root))
        self.assertLessEqual(len(row["head"]), 32)
        self.assertEqual(len(self.violations()), 1, "one line per violation")

    def test_a_file_tool_violation_hashes_the_path(self):
        path = fwd(self.root / "src" / "a.ts")
        self.assertDenied(self.run_hook("Write", {"file_path": path, "content": "x"}), "Write")
        row = self.violations()[0]
        self.assertEqual(row["rule"], "file-tool")
        self.assertEqual(row["sha256"], hashlib.sha256(path.encode("utf-8")).hexdigest())

    def test_the_log_rotates_at_1_mib(self):
        log = self.root / ".claude" / "super-board" / "orchestrator-violations.jsonl"
        log.write_text("x" * (1024 * 1024))
        self.deny_bash("npm test")
        self.assertEqual(len((self.root / ".claude" / "super-board" / "orchestrator-violations.jsonl.1").read_text()), 1024 * 1024)
        self.assertEqual(len(self.violations()), 1)

    def test_an_unwritable_log_still_denies(self):
        log = self.root / ".claude" / "super-board" / "orchestrator-violations.jsonl"
        log.unlink(missing_ok=True)
        log.mkdir()
        try:
            self.deny_bash("npm test")
        finally:
            log.rmdir()


class Malformed(Base):
    def test_garbage_payloads_fail_closed(self):
        for raw in ("not json", "[]", json.dumps({}), json.dumps({"tool_name": "Bash"}),
                    json.dumps({"tool_name": "Read", "tool_input": {}}),
                    json.dumps({"tool_name": "Bash", "tool_input": {}}),
                    json.dumps({"tool_name": "Bash", "tool_input": {"command": 7}})):
            with self.subTest(raw=raw[:40]):
                self.assertDenied(self.run_hook(None, None, raw=raw), raw[:40])

    def test_a_file_tool_without_a_path_is_still_a_denial(self):
        self.assertDenied(self.run_hook("Write", {"content": "x"}), "Write without a path")


class PathSpellings(Base):
    def test_backslash_and_msys_spellings_decide_the_same_way(self):
        self.allow_bash("git -C .claude\\worktrees\\issue-5-build status --porcelain")
        self.allow_bash(r"bash .claude\bin\super-board-card.sh --config c.json audit")
        self.deny_bash("git -C .claude\\worktrees\\issue-5-build status --short")
        self.deny_bash("git -C .claude/worktrees/../worktrees/issue-5-build status --porcelain")

    def test_the_same_verdict_for_every_cwd_spelling(self):
        for cwd in (str(self.root), str(self.root).replace("\\", "/"), fwd(self.root)):
            with self.subTest(cwd=cwd):
                self.deny_bash("npm test", cwd=cwd)
                self.allow_bash("git status", cwd=cwd)
        self.assertEqual(self.violations()[-1]["cwd"], fwd(self.root))

    def test_an_absolute_project_helper_path_is_allowed(self):
        self.allow_bash("bash %s/.claude/bin/super-board-card.sh --config c.json audit" % fwd(self.root))

    def test_the_log_cwd_is_normalised(self):
        self.deny_bash("npm test", cwd=fwd(self.root) + "/")
        self.assertEqual(self.violations()[-1]["cwd"], fwd(self.root))


if __name__ == "__main__":
    unittest.main()
