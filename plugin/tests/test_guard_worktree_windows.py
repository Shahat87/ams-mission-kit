"""Windows path-normalisation tests for hooks/guard-worktree-path.py (uses its own check())."""
import importlib.util
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("gwp", os.path.join(HERE, "..", "hooks", "guard-worktree-path.py"))
gwp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gwp)


def make_run(top):
    """check() with a root_of that, like git, answers only for dirs inside the fake repo `top`."""
    inside = os.path.normcase(os.path.normpath(top))

    def root_of(d):
        nd = os.path.normcase(os.path.normpath(d))
        if nd == inside or nd.startswith(inside + os.sep):
            return gwp.repo_root(top if nd == inside else d.replace("\\", "/"))
        return None

    return lambda command, cwd: gwp.check(command, cwd, root_of, {})


@unittest.skipUnless(os.name == "nt", "Windows path semantics")
class WindowsGuard(unittest.TestCase):
    def setUp(self):
        self.run_ = make_run("C:/x/repo")

    def allow(self, cmd, cwd="C:/x/repo"):
        self.assertIsNone(self.run_(cmd, cwd), cmd)

    def deny(self, cmd, cwd="C:/x/repo"):
        self.assertIn("Blocked", self.run_(cmd, cwd) or "", cmd)

    def test_allowed(self):
        self.allow("git worktree add .claude/worktrees/issue-3-build -b b")
        self.allow("git -C C:/x/repo worktree add .claude/worktrees/a", "D:/elsewhere")
        self.allow("git -C /c/x/repo worktree add .claude/worktrees/a", "/")
        self.allow("git worktree add /c/x/repo/.claude/worktrees/z", "D:/elsewhere")
        self.allow("cd /c/x/repo && git worktree add .claude/worktrees/y", "/")
        self.allow("git worktree add .claude/worktrees/a", "c:/x/repo")
        self.allow("git worktree add .claude/worktrees/a", "C:/x/repo".replace("/", chr(92)))
        self.allow("git worktree add ../b", "C:/x/repo/.claude/worktrees/a")

    def test_denied(self):
        self.deny("git worktree add ../outside")
        self.deny("git worktree add .claude/worktrees-evil/a")
        self.deny("git worktree add .claude/worktreesX/a")
        self.deny("git worktree add .claude/worktrees/../../evil")
        self.deny("git worktree add C:/tmp/wt/x")
        self.deny("git worktree add /tmp/x")
        self.deny("git worktree add /cygdrive/c/x")
        self.deny("git worktree add //server/x")
        self.deny("git -C /c/x/repo worktree add .worktrees/x", "/")
        self.deny("git worktree add /c/../d/x/repo/.claude/worktrees/y")

    def test_msys_dotdot_maps_like_git_bash(self):
        self.assertEqual(gwp.msys("/c/../d/x"), "d:/x")
        self.assertEqual(gwp.msys("/.."), "/")
        self.assertEqual(gwp.msys("/c"), "c:/")

    def test_dotdot_repo_on_other_drive(self):
        self.run_ = make_run("D:/x/repo")
        self.allow("git worktree add /c/../d/x/repo/.claude/worktrees/y", "D:/x/repo")


if __name__ == "__main__":
    unittest.main()
