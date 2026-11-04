#!/usr/bin/env python3
"""Integrity snapshot covers only what a lane must never change (spec §6.4)."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lane_integrity", ROOT / "scripts/super-board-lane-integrity.py")
I = importlib.util.module_from_spec(spec)
spec.loader.exec_module(I)


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.origin = t / "origin.git"
        git(t, "init", "--bare", "-q", str(self.origin))
        self.repo = t / "repo"
        git(t, "clone", "-q", str(self.origin), str(self.repo))
        git(self.repo, "config", "user.email", "t@t"); git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("a")
        git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "a"); git(self.repo, "push", "-q", "origin", "HEAD:main")
        git(self.repo, "checkout", "-qb", "issue-1-x"); git(self.repo, "push", "-q", "origin", "issue-1-x")
        self.config = t / "board.json"; self.config.write_text("{}")
        self.state = t / "state.json"; self.state.write_text("{}")
        self.home = t / "home"; self.home.mkdir()
        self.args = dict(repo=self.repo, worktree=self.repo, branch="issue-1-x", files=[self.config, self.state], home=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def snap(self):
        return I.snapshot(**self.args)

    def test_no_change_no_diff(self):
        self.assertEqual(I.compare(self.snap(), self.snap()), [])

    def test_board_config_change_detected(self):
        before = self.snap(); self.config.write_text('{"pilot":{}}')
        self.assertTrue(any("board.json" in d for d in I.compare(before, self.snap())))

    def test_hook_added_detected(self):
        before = self.snap(); (self.repo / ".git" / "hooks" / "pre-push").write_text("#!/bin/sh\n")
        self.assertTrue(any("hooks/pre-push" in d for d in I.compare(before, self.snap())))

    def test_selected_config_key_detected(self):
        before = self.snap(); git(self.repo, "config", "core.hooksPath", "/tmp/evil")
        self.assertTrue(any("core.hookspath" in d for d in I.compare(before, self.snap())))

    def test_sibling_upstream_write_is_ignored(self):
        before = self.snap(); git(self.repo, "config", "branch.issue-2-y.remote", "origin")
        self.assertEqual(I.compare(before, self.snap()), [])

    def test_lane_branch_head_and_remote_detected(self):
        before = self.snap()
        (self.repo / "b.txt").write_text("b"); git(self.repo, "add", "."); git(self.repo, "commit", "-qm", "b")
        git(self.repo, "push", "-q", "origin", "issue-1-x")
        diffs = I.compare(before, self.snap())
        self.assertTrue(any(d.startswith("HEAD") for d in diffs))
        self.assertTrue(any(d.startswith("remote refs/heads/issue-1-x") for d in diffs))

    def test_other_branch_push_ignored(self):
        before = self.snap()
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/issue-2-y")
        self.assertEqual(I.compare(before, self.snap()), [])

    def test_new_tag_detected(self):
        before = self.snap(); git(self.repo, "tag", "v9"); git(self.repo, "push", "-q", "origin", "v9")
        self.assertTrue(any(d.startswith("remote tags") for d in I.compare(before, self.snap())))

    def test_any_config_key_detected_fsmonitor(self):
        before = self.snap(); git(self.repo, "config", "core.fsmonitor", "touch /tmp/x")
        self.assertTrue(any("core.fsmonitor" in d for d in I.compare(before, self.snap())))

    def test_filter_driver_detected(self):
        before = self.snap(); git(self.repo, "config", "filter.x.clean", "evil")
        self.assertTrue(any("filter.x.clean" in d for d in I.compare(before, self.snap())))

    def test_branch_keys_ignored(self):
        before = self.snap(); git(self.repo, "config", "branch.issue-2-y.merge", "refs/heads/issue-2-y")
        self.assertEqual(I.compare(before, self.snap()), [])

    def test_info_attributes_detected(self):
        before = self.snap(); (self.repo / ".git" / "info").mkdir(exist_ok=True)
        (self.repo / ".git" / "info" / "attributes").write_text("* filter=x\n")
        self.assertTrue(any("info/attributes" in d for d in I.compare(before, self.snap())))

    def test_config_worktree_detected(self):
        before = self.snap(); (self.repo / ".git" / "config.worktree").write_text("[core]\n	fsmonitor = evil\n")
        self.assertTrue(any("config.worktree" in d for d in I.compare(before, self.snap())))

    def test_hookspath_dir_file_detected(self):
        hp = Path(self.tmp.name) / "hp"; hp.mkdir()
        git(self.repo, "config", "core.hooksPath", str(hp))
        before = self.snap(); (hp / "pre-push").write_text("#!/bin/sh\n")
        self.assertTrue(any("pre-push" in d for d in I.compare(before, self.snap())))

    def test_home_gitconfig_detected(self):
        before = self.snap(); (self.home / ".gitconfig").write_text("[core]\n	fsmonitor = evil\n")
        self.assertTrue(any(".gitconfig" in d for d in I.compare(before, self.snap())))


if __name__ == "__main__":
    unittest.main()
