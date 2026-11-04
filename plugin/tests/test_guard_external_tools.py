#!/usr/bin/env python3
"""hooks/guard-external-tools.py (G-1) and hooks/mc_guarded.py (D-5 helper).

GUARDRAIL tests: the hooks catch habit and mistakes, not attacks (spec section 15.3). Each hook runs as a subprocess,
`python -I <hook>`, with hook JSON on stdin and a temp HOME / USERPROFILE. Tests may run git; the hooks never do."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
HOOK = HOOKS / "guard-external-tools.py"
HELPER = HOOKS / "mc_guarded.py"
sys.path.insert(0, str(HOOKS))


def fwd(p):
    return str(p).replace("\\", "/")


def make_repo(root, remote):
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / ".git" / "config").write_text('[core]\n\tbare = false\n[remote "origin"]\n\turl = %s\n' % remote)


def make_linked(main, name, wt):
    """A linked worktree: `.git` is a file, the git dir has a commondir file."""
    gd = main / ".git" / "worktrees" / name
    gd.mkdir(parents=True)
    (gd / "HEAD").write_text("ref: refs/heads/%s\n" % name)
    (gd / "commondir").write_text("../..\n")
    (gd / "gitdir").write_text(fwd(wt / ".git") + "\n")
    wt.mkdir(parents=True)
    (wt / ".git").write_text("gitdir: %s\n" % fwd(gd))


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name).resolve()
        self.home = self.t / "home"
        (self.home / ".mission-control").mkdir(parents=True)
        self.guarded = self.t / "guarded"
        make_repo(self.guarded, "git@github.com:Org/Guarded.git")
        self.other = self.t / "other"
        make_repo(self.other, "https://github.com/someone/else.git")
        self.write_list(["Org/Guarded"])

    def tearDown(self):
        self.tmp.cleanup()

    @property
    def listfile(self):
        return self.home / ".mission-control" / "guarded-repos.json"

    def write_list(self, repos, version=1, common=None):
        entries = [{"repo": r, "common_dir": common, "added": "2026-10-28T00:00:00Z"} for r in repos]
        self.listfile.write_text(json.dumps({"version": version, "repos": entries}))

    def env(self, project=None):
        env = dict(os.environ)
        env["HOME"] = env["USERPROFILE"] = str(self.home)
        env.pop("CLAUDE_PROJECT_DIR", None)
        if project:
            env["CLAUDE_PROJECT_DIR"] = str(project)
        return env

    def run_hook(self, payload, project=None):
        return subprocess.run([sys.executable, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=self.env(project), timeout=60)

    def cmd(self, command, cwd=None, project=None, tool="Bash"):
        cwd = cwd or self.guarded
        return self.run_hook({"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command},
                              "cwd": str(cwd)}, project=project or cwd)

    def assertDenied(self, r, msg=""):
        self.assertEqual(r.returncode, 2, "%s: expected deny, got rc=%s out=%r err=%r" % (msg, r.returncode, r.stdout, r.stderr))
        self.assertIn("G-1", r.stderr, msg)

    def assertAllowed(self, r, msg=""):
        self.assertEqual(r.returncode, 0, "%s: expected allow, got rc=%s err=%r" % (msg, r.returncode, r.stderr))


class McGuardedTests(Env):
    def mcg(self):
        import mc_guarded
        return mc_guarded

    def test_remote_forms(self):
        m = self.mcg()
        cases = [("https://github.com/Org/Name.git", "org/name"), ("https://user@github.com/org/name/", "org/name"),
                 ("https://github.com/org/name", "org/name"), ("ssh://git@github.com/org/name.git", "org/name"),
                 ("ssh://git@github.com:22/org/name", "org/name"), ("git@github.com:Org/Name.git", "org/name"),
                 ("github.com:org/name", "org/name"), ("https://gitlab.com/org/name", None), ("git@github.com:org", None),
                 ("file:///x/y", None), ("https://github.com/org/name/extra", None), ("", None)]
        for url, want in cases:
            with self.subTest(url=url):
                self.assertEqual(m.normalize_remote(url, []), want)

    def test_insteadof_longest_prefix_wins(self):
        m = self.mcg()
        self.assertEqual(m.normalize_remote("gh:Org/Name", [("gh:", "git@github.com:")]), "org/name")
        rules = [("gh:", "https://example.com/"), ("gh:org/", "git@github.com:org/")]
        self.assertEqual(m.normalize_remote("gh:org/name", rules), "org/name")

    def test_linked_worktree_common_dir_matches(self):
        m = self.mcg()
        main = self.t / "main"
        make_repo(main, "https://example.com/not/github")
        wt = self.t / "wt1"
        make_linked(main, "wt1", wt)
        common = fwd(os.path.realpath(main / ".git"))
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}):
            facts = m.repo_facts(str(wt))
            self.assertEqual(facts[0].lower(), common.lower())
            self.assertFalse(m.is_guarded(str(wt)))
            self.write_list(["x/unrelated"], common=common)
            self.assertTrue(m.is_guarded(str(wt)))
            self.assertTrue(m.is_guarded(str(main)))

    def test_remote_match_and_home_insteadof(self):
        m = self.mcg()
        (self.home / ".gitconfig").write_text('[url "git@github.com:"]\n\tinsteadOf = gh:\n')
        r = self.t / "viaalias"
        make_repo(r, "gh:Org/Guarded")
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}):
            self.assertTrue(m.is_guarded(str(r)))
            self.assertTrue(m.is_guarded(str(self.guarded / "sub" / "deep")))
            self.assertFalse(m.is_guarded(str(self.other)))

    def test_broken_lists_mean_guarded(self):
        m = self.mcg()
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}):
            self.listfile.unlink()
            self.assertIsNone(m.load())
            self.assertTrue(m.is_guarded(str(self.other)))
            self.listfile.write_text("{not json")
            self.assertTrue(m.is_guarded(str(self.other)))
            self.write_list(["a/b"], version=2)
            self.assertIsNone(m.load())
            self.write_list(["bad slug"])
            self.assertIsNone(m.load())
            self.assertTrue(m.is_guarded(str(self.other)))

    def test_load_accepts_a_bom(self):
        m = self.mcg()
        body = json.dumps({"version": 1, "repos": [{"repo": "Org/Guarded", "common_dir": None, "added": "x"}]})
        self.listfile.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}):
            self.assertIsNotNone(m.load())
            self.assertFalse(m.is_guarded(str(self.other)))
            self.assertTrue(m.is_guarded(str(self.guarded)))

    def test_not_a_repo_with_good_list_is_unguarded(self):
        m = self.mcg()
        plain = self.t / "plain"
        plain.mkdir()
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)}):
            self.assertFalse(m.is_guarded(str(plain)))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-I", str(HELPER)] + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=self.env(), timeout=60)

    def test_add_cli_merges(self):
        self.listfile.unlink()
        r = self.cli("add", "--repo", "Org/Guarded", "--root", str(self.guarded))
        self.assertEqual(r.returncode, 0, r.stderr)
        common = fwd(os.path.realpath(self.guarded / ".git"))
        self.assertEqual(r.stdout.strip(), "guarded: Org/Guarded (common dir: %s)" % common)
        r = self.cli("add", "--repo", "acme/widgets")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "guarded: acme/widgets (common dir: none)")
        data = json.loads(self.listfile.read_text())
        self.assertEqual(data["version"], 1)
        self.assertEqual([e["repo"] for e in data["repos"]], ["Org/Guarded", "acme/widgets"])
        self.assertEqual(data["repos"][0]["common_dir"], common)
        self.assertIsNone(data["repos"][1]["common_dir"])
        self.assertTrue(data["repos"][1]["added"].endswith("Z"))
        # a repo already listed gets its common_dir updated when the root gives one
        main = self.t / "acmerepo"
        make_repo(main, "git@github.com:acme/widgets.git")
        self.assertEqual(self.cli("add", "--repo", "acme/widgets", "--root", str(main)).returncode, 0)
        data = json.loads(self.listfile.read_text())
        self.assertEqual(len(data["repos"]), 2)
        self.assertEqual(data["repos"][1]["common_dir"], fwd(os.path.realpath(main / ".git")))
        # a root whose remote does not match records no common dir
        r = self.cli("add", "--repo", "zed/zed", "--root", str(self.other))
        self.assertIn("(common dir: none)", r.stdout)

    def test_add_cli_errors(self):
        r = self.cli("add", "--repo", "not a slug")
        self.assertEqual(r.returncode, 64)
        self.listfile.write_text("{broken")
        r = self.cli("add", "--repo", "a/b")
        self.assertEqual(r.returncode, 70)
        self.assertTrue(r.stderr.strip())
        self.assertEqual(self.listfile.read_text(), "{broken")

    def test_check_cli(self):
        self.assertEqual(self.cli("check", str(self.guarded)).stdout.strip(), "guarded")
        self.assertEqual(self.cli("check", str(self.other)).stdout.strip(), "unguarded")


BLOCKED = [
    "pi", "grok", 'codex exec "x"', "node ~/.claude/skills/codex-delegate/scripts/relay.mjs", "npx @openai/codex exec x",
    "npx -y @mariozechner/pi-coding-agent@1.2", "wsl -e codex", "cmd /c codex.cmd", 'pwsh -c "codex exec x"',
    "Start-Process codex", 'bash -c "cd x && grok"', r"C:\tools\codex.exe", "aider --yes", "FOO=1 env nohup opencode",
    "bunx opencode-ai", "pnpm dlx @github/copilot", "npm exec -- codex", 'python3 -c "import os; os.system(\'pi\')"',
    "node -e \"require('child_process').exec('grok')\"", "Start-Process -FilePath pi -ArgumentList x", "& 'C:\\x\\cursor-agent.cmd'",
    "wsl -- bash -c 'codex exec x'", "sudo time kimi",
]

ALLOWED = [
    "python3 .claude/bin/super-board-lane-delegate.py delegate --tool codex --model m --mode write --brief b.txt",
    "git log --grep codex", "echo pi", "cat docs/codex.md", "ls ~/.codex", "grep -r opencode .", "piper --help",
]


class GuardExternalToolsTests(Env):
    def test_blocks_in_guarded_repo(self):
        for c in BLOCKED:
            with self.subTest(command=c):
                self.assertDenied(self.cmd(c), c)

    def test_powershell_tool_blocks_too(self):
        self.assertDenied(self.cmd("codex exec x", tool="PowerShell"))

    def test_reason_text(self):
        r = self.cmd("codex exec x")
        self.assertIn("Blocked (guardrail G-1): codex is an external coding tool and ", r.stderr)
        self.assertIn("is a guarded repo (~/.mission-control/guarded-repos.json). External tools run only on the Lenovo: "
                      "use python3 .claude/bin/super-board-lane-delegate.py delegate --tool codex ... (spec", r.stderr)
        self.assertNotIn("missing or invalid", r.stderr)

    def test_allows_in_unguarded_repo(self):
        for c in BLOCKED:
            with self.subTest(command=c):
                self.assertAllowed(self.cmd(c, cwd=self.other), c)

    def test_blocks_with_no_list_file(self):
        self.listfile.unlink()
        for c in ("codex exec x", "pi"):
            r = self.cmd(c, cwd=self.other)
            self.assertDenied(r, c)
            self.assertIn("The guarded-repo list is missing or invalid, so every repo counts as guarded.", r.stderr)
            self.assertTrue(r.stderr.strip().endswith(
                "The guarded-repo list is missing or invalid, so every repo counts as guarded."), r.stderr)

    def test_must_allow_cases_in_guarded_repo(self):
        for c in ALLOWED:
            with self.subTest(command=c):
                self.assertAllowed(self.cmd(c), c)

    def test_cd_style_targets(self):
        g = fwd(self.guarded)
        self.assertDenied(self.cmd("codex --cd %s exec x" % g, cwd=self.other), "--cd X")
        self.assertDenied(self.cmd("codex --cd=%s exec x" % g, cwd=self.other), "--cd=X")
        self.assertDenied(self.cmd("aider --cwd=%s" % g, cwd=self.other), "--cwd=X")
        self.assertDenied(self.cmd("codex -C %s exec x" % g, cwd=self.other), "-C X")
        self.assertDenied(self.cmd("pi --prefix %s" % g, cwd=self.other), "--prefix X")
        rel = os.path.relpath(self.guarded, self.other).replace("\\", "/") if os.name != "nt" or \
            self.guarded.drive == self.other.drive else g
        self.assertDenied(self.cmd("codex --cd %s exec x" % rel, cwd=self.other), "relative target")
        self.assertDenied(self.cmd('codex --cd "%s" exec x' % str(self.guarded), cwd=self.other), "native path form")

    def test_git_dash_c_is_not_a_hit(self):
        self.assertAllowed(self.cmd("git -C %s status" % fwd(self.guarded), cwd=self.other))

    def test_msys_path_form(self):
        if os.name != "nt":
            self.skipTest("MSYS path forms are Windows only")
        p = fwd(self.guarded)
        msys = "/%s%s" % (p[0].lower(), p[2:])
        self.assertDenied(self.cmd("codex --cd %s exec x" % msys, cwd=self.other), msys)

    def test_mcp_denied_for_covered_server(self):
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "cx": {"command": "npx", "args": ["@openai/codex", "mcp"]}, "plain": {"command": "node", "args": ["srv.js"]}}}))
        pl = lambda tool: {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}, "cwd": str(self.guarded)}
        self.assertDenied(self.run_hook(pl("mcp__cx__run"), project=self.guarded))
        self.assertAllowed(self.run_hook(pl("mcp__plain__run"), project=self.guarded))
        self.assertAllowed(self.run_hook(pl("mcp__nosuch__run"), project=self.guarded))
        self.assertAllowed(self.run_hook(pl("mcp__cx__run"), project=self.other))

    def test_mcp_from_project_scopes(self):
        (self.guarded / ".mcp.json").write_text(json.dumps({"mcpServers": {"pj": {"command": "codex", "args": ["mcp-server"]}}}))
        (self.home / ".claude.json").write_text(json.dumps({"projects": {fwd(self.guarded): {"mcpServers": {
            "pp": {"command": "uvx", "args": ["aider-chat"]}}}}}))
        pl = lambda tool: {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {}, "cwd": str(self.guarded)}
        self.assertDenied(self.run_hook(pl("mcp__pj__x"), project=self.guarded))
        self.assertDenied(self.run_hook(pl("mcp__pp__x"), project=self.guarded))

    def test_mcp_unreadable_config_allows(self):
        (self.home / ".claude.json").write_text("{broken")
        r = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "mcp__cx__run", "tool_input": {},
                           "cwd": str(self.guarded)}, project=self.guarded)
        self.assertAllowed(r)

    def test_session_start_warns_and_exits_zero(self):
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "cx": {"command": "npx", "args": ["@openai/codex", "mcp"]}}}))
        r = self.run_hook({"hook_event_name": "SessionStart", "cwd": str(self.guarded)}, project=self.guarded)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("cx", r.stdout)
        self.assertEqual(len([l for l in r.stdout.splitlines() if l.strip()]), 1)
        r = self.run_hook({"hook_event_name": "SessionStart", "cwd": str(self.other)}, project=self.other)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    HEREDOC_APOS = "git commit -F - <<'EOF'\nDon't do it\nEOF"

    def test_heredoc_with_apostrophe_is_allowed(self):
        self.assertAllowed(self.cmd(self.HEREDOC_APOS), "guarded")
        self.assertAllowed(self.cmd(self.HEREDOC_APOS, cwd=self.other), "unguarded")
        self.listfile.unlink()
        self.assertAllowed(self.cmd(self.HEREDOC_APOS, cwd=self.other), "no list")
        self.assertAllowed(self.cmd(self.HEREDOC_APOS), "no list, cwd guarded")

    def test_heredoc_body_is_not_commands_unless_fed_to_a_shell(self):
        self.assertAllowed(self.cmd("cat <<EOF\npi\nEOF"))
        self.assertAllowed(self.cmd("cat <<-'EOF'\n\tcodex exec x\n\tEOF\necho done"))
        self.assertDenied(self.cmd("bash <<'EOF'\ncodex exec x\nEOF"))
        self.assertDenied(self.cmd("sh <<EOF\ngrok\nEOF"))
        self.assertDenied(self.cmd("python3 <<'EOF'\nimport os; os.system('pi')\nEOF"))
        self.assertDenied(self.cmd("cat <<EOF\nx\nEOF\ncodex exec y"), "command after a heredoc")
        self.assertDenied(self.cmd("cat <<A <<B\nx\nA\ny\nB\ncodex"), "two heredocs")

    def test_unbalanced_quote_outside_heredoc(self):
        self.assertEqual(self.cmd('echo "it\'s').returncode, 2)
        self.assertAllowed(self.cmd('echo "it\'s', cwd=self.other))

    def test_powershell_rules_and_comments(self):
        self.listfile.unlink()          # no list: every repo counts as guarded
        self.assertAllowed(self.cmd('echo "C:\\path\\"', tool="PowerShell"))
        self.assertAllowed(self.cmd('Get-ChildItem "C:\\Users\\x\\" | Select -First 3', tool="PowerShell"))
        self.assertAllowed(self.cmd("git commit -m @'\nfix: don't crash\n'@", tool="PowerShell"))
        self.assertAllowed(self.cmd("cat x # don't"))
        self.assertAllowed(self.cmd("cat x # don't", tool="PowerShell"))
        self.assertDenied(self.cmd('echo "C:\\path\\"; codex exec x', tool="PowerShell"))
        self.assertDenied(self.cmd("echo 'it''s'; pi", tool="PowerShell"))
        self.assertDenied(self.cmd("git commit -m @'\nfix: x\n'@ ; codex exec x", tool="PowerShell"))
        self.assertDenied(self.cmd("echo a#b; pi"))

    def test_interpreter_heredocs(self):
        self.assertAllowed(self.cmd("python3 <<'EOF'\npi = math.pi\nEOF"))
        self.assertAllowed(self.cmd("python - <<'EOF'\n# it's a check\nprint(1)\nEOF"))
        self.assertDenied(self.cmd("python3 <<'EOF'\nimport subprocess; subprocess.run([\"codex\",\"exec\",\"x\"])\nEOF"))
        self.assertDenied(self.cmd("node <<'EOF'\nrequire('child_process').spawn('grok')\nEOF"))

    def test_arithmetic_shift_is_not_a_heredoc(self):
        self.assertDenied(self.cmd("echo $((x<<y))\ncodex exec"))

    def test_delegate_paths_count_only_in_command_positions(self):
        self.assertAllowed(self.cmd("grep -rn relay.mjs ."))
        self.assertAllowed(self.cmd("git add skills/codex-delegate/scripts/x.sh"))
        self.assertDenied(self.cmd("node ~/.claude/skills/codex-delegate/scripts/relay.mjs"))
        self.assertDenied(self.cmd("bash ~/.claude/skills/pi-delegate/scripts/run.sh"))
        self.assertDenied(self.cmd("~/.claude/skills/pi-delegate/scripts/run.sh"))
        self.assertDenied(self.cmd("npx ./skills/x-delegate/scripts/relay.mjs"))

    def test_hash_after_brace_is_not_a_comment(self):
        self.assertDenied(self.cmd("echo ${#x}; codex exec"))
        self.assertDenied(self.cmd("echo ${#arr[@]}; codex exec"))
        self.assertAllowed(self.cmd("echo ${#x}"))

    def test_package_names_in_process_lines(self):
        self.assertDenied(self.cmd("node -e \"require('child_process').execSync('npx @openai/codex exec')\""))
        self.assertAllowed(self.cmd("node -e \"console.log('@openai/codex')\""))

    def test_powershell_block_comments(self):
        self.assertDenied(self.cmd("<# x #> codex exec", tool="PowerShell"))
        self.assertDenied(self.cmd("<# it's\nmulti #> codex exec", tool="PowerShell"))
        self.assertAllowed(self.cmd("<# it's #> echo ok", tool="PowerShell"))

    def test_fails_closed(self):
        r = subprocess.run([sys.executable, "-I", str(HOOK)], input="{not json", capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=self.env(self.guarded), timeout=60)
        self.assertEqual(r.returncode, 2)
        r = self.run_hook({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}, "cwd": str(self.guarded)},
                          project=self.guarded)
        self.assertEqual(r.returncode, 2)
        r = self.cmd('echo "unterminated')
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
