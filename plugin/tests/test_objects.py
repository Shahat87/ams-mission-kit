#!/usr/bin/env python3
"""Tests for scripts/super-board-objects.py (Plan 1c Task 3). Run: python3 tests/test_objects.py"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("sb_objects", PLUGIN / "scripts" / "super-board-objects.py")
objs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(objs)

MODIFY_PATCH = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n hello\n+world\n"
EVIL_PATCH = 'diff --git a/evil.py b/evil.py\nnew file mode 100644\n--- /dev/null\n+++ b/evil.py\n@@ -0,0 +1 @@\n+print("evil")\n'
BAD_PATCH = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n nothello\n+world\n"
NONCE = "ab" * 32


def plain(cwd, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r.stdout.strip()


def snapshot(root, skip_git=True):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_git and ".git" in dirnames:
            dirnames.remove(".git")
        for f in filenames:
            p = Path(dirpath) / f
            st = p.stat()
            out[p.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sb-objtest-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        self.origin = self.tmp / "origin.git"
        self.repo = self.tmp / "work"
        plain(self.tmp, "init", "-q", "--bare", "-b", "staging", str(self.origin))
        plain(self.tmp, "init", "-q", "-b", "staging", str(self.repo))
        plain(self.repo, "config", "user.name", "Test User")
        plain(self.repo, "config", "user.email", "test@example.invalid")
        plain(self.repo, "config", "commit.gpgsign", "false")
        plain(self.repo, "remote", "add", "origin", str(self.origin))
        (self.repo / "a.txt").write_bytes(b"hello\n")
        plain(self.repo, "add", "a.txt")
        plain(self.repo, "commit", "-q", "-m", "init")
        plain(self.repo, "push", "-q", "origin", "staging")
        plain(self.repo, "fetch", "-q", "origin")
        self.parent = plain(self.repo, "rev-parse", "HEAD")
        self.identity = ("Test User", "test@example.invalid")
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        gh = self.stub / "gh"
        gh.write_bytes(b"#!/bin/sh\nexit 0\n")                           # `gh auth git-credential` prints nothing
        gh.chmod(gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.ghdir = self.tmp / "ghdir"
        self.ghdir.mkdir()
        env = {"SB_SCRATCH_ROOT": str(self.scratch), "SB_ROBOT_GH_CONFIG_DIR": str(self.ghdir),
               "PATH": self.stub.as_posix() + os.pathsep + os.environ.get("PATH", "")}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        # The wrapper's --robot and --robot-read env allow https only and GIT_ALLOW_PROTOCOL beats repo config, so a push or a
        # live-base read against the local bare origin needs a test-only copy of the wrapper that allows file. The production
        # wrapper is not touched.
        text = objs.WRAPPER.read_bytes().decode("utf-8")
        self.assertIn("export GIT_ALLOW_PROTOCOL=https", text)
        copy = self.tmp / "git-file-ok.sh"
        copy.write_bytes(text.replace("export GIT_ALLOW_PROTOCOL=https", "export GIT_ALLOW_PROTOCOL=file").encode("utf-8"))
        w = mock.patch.object(objs, "WRAPPER", copy)
        w.start()
        self.addCleanup(w.stop)

    def patch(self, text, name="p.patch"):
        path = self.tmp / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def repo_state(self):
        """Hashes of .git/index, .git/config and every file under .git/hooks/: a build must change none of them."""
        git = self.repo / ".git"
        out = {}
        for rel in ("index", "config"):
            f = git / rel
            out[rel] = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
        for f in sorted((git / "hooks").rglob("*")):
            if f.is_file():
                out["hooks/" + f.relative_to(git / "hooks").as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
        return out

    def build(self, patch_text=MODIFY_PATCH, message="lane: change", tool="fake"):
        before = self.repo_state()
        try:
            return objs.build_commit(self.repo, self.parent, self.patch(patch_text), message,
                                     objs.lane_trailers(tool, NONCE), self.identity)
        finally:
            self.assertEqual(self.repo_state(), before, "a build touched .git/index, .git/config or .git/hooks")

    def origin_ref(self, branch):
        r = subprocess.run(["git", "--git-dir", str(self.origin), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
                           capture_output=True, text=True)
        return r.stdout.strip()


class BuildTests(Base):
    def test_tree_is_parent_plus_patch_and_worktree_untouched(self):
        before = snapshot(self.repo)
        sha = self.build()
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(plain(self.repo, "rev-parse", sha + "^"), self.parent)
        self.assertEqual(plain(self.repo, "diff", "--name-status", self.parent, sha), "M\ta.txt")
        self.assertEqual(plain(self.repo, "show", sha + ":a.txt"), "hello\nworld")
        body = plain(self.repo, "log", "-1", "--format=%B", sha)
        self.assertIn("lane: change", body)
        self.assertIn("Mc-Lane-Tool: fake", body)
        self.assertIn("Mc-Lane-Nonce-Sha256: " + NONCE, body)
        self.assertEqual(plain(self.repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>", sha),
                         "Test User <test@example.invalid>|Test User <test@example.invalid>")
        self.assertEqual(plain(self.repo, "status", "--porcelain"), "")
        self.assertEqual(os.listdir(self.scratch), [])

    def test_patch_adding_evil_py_leaves_no_file_anywhere(self):
        sha = self.build(EVIL_PATCH)
        self.assertIn("evil.py", plain(self.repo, "ls-tree", "-r", "--name-only", sha).split("\n"))
        for dirpath, dirnames, filenames in os.walk(self.repo):
            if Path(dirpath) == self.repo / ".git" / "objects":
                dirnames[:] = []
                continue
            if Path(dirpath) == self.repo / ".git":
                dirnames[:] = [d for d in dirnames if d != "objects"]
            self.assertNotIn("evil.py", filenames + dirnames, dirpath)
        self.assertEqual(os.listdir(self.scratch), [])

    def test_bad_patch_raises_and_cleans_up(self):
        before = snapshot(self.repo)
        with self.assertRaises(objs.ObjectsError) as cm:
            self.build(BAD_PATCH)
        self.assertEqual(cm.exception.code, objs.EXIT_INTERNAL)
        self.assertEqual(os.listdir(self.scratch), [])
        self.assertEqual(snapshot(self.repo), before)
        self.assertEqual(self.origin_ref("feature"), "")

    def test_missing_patch_is_66(self):
        with self.assertRaises(objs.ObjectsError) as cm:
            objs.build_commit(self.repo, self.parent, self.tmp / "nope.patch", "m", "t", self.identity)
        self.assertEqual(cm.exception.code, objs.EXIT_MISSING)
        self.assertEqual(os.listdir(self.scratch), [])

    def test_scratch_inside_repo_refused_before_anything_is_created(self):
        with mock.patch.dict(os.environ, {"SB_SCRATCH_ROOT": str(self.repo / "inside")}):
            with self.assertRaises(objs.ObjectsError) as cm:
                self.build()
        self.assertEqual(cm.exception.code, objs.EXIT_USAGE)
        self.assertFalse((self.repo / "inside").exists())

    def test_scratch_inside_the_main_checkout_or_another_worktree_refused(self):
        wt = self.tmp / "wt2"
        plain(self.repo, "worktree", "add", "-q", str(wt), "-b", "wtb")
        cases = [(self.repo, self.repo / "deep" / "scratch"),             # under the main checkout
                 (wt, self.repo / "scratch2"),                            # repo is a worktree; root is in the MAIN checkout
                 (self.repo, wt / "scratch3"),                            # root is in another worktree
                 (wt, wt)]                                               # root is the worktree itself
        for repo, root in cases:
            with self.subTest(repo=repo.name, root=str(root)):
                with mock.patch.dict(os.environ, {"SB_SCRATCH_ROOT": str(root)}):
                    with self.assertRaises(objs.ObjectsError) as cm:
                        objs.build_commit(repo, self.parent, self.patch(MODIFY_PATCH), "m", objs.lane_trailers("t", NONCE), self.identity)
                self.assertEqual(cm.exception.code, objs.EXIT_USAGE)
                if root != wt:
                    self.assertFalse(root.exists())

    def test_scratch_outside_every_checkout_is_fine_even_when_a_worktree_exists(self):
        plain(self.repo, "worktree", "add", "-q", str(self.tmp / "wt3"), "-b", "wtc")
        self.assertRegex(self.build(), "^[0-9a-f]{40}")


class PatchShapes(Base):
    """What git actually does with awkward patches. Each one becomes git objects only (no file on disk anywhere), or is refused."""

    def ls(self, sha):
        return {line.split("\t")[1]: line.split()[0] for line in plain(self.repo, "ls-tree", "-r", sha).splitlines()}

    def assert_nothing_on_disk(self, *names):
        for n in names:
            self.assertFalse((self.repo / n).exists() or (self.repo / n).is_symlink(), n)
        self.assertFalse((self.repo / ".git" / "hooks" / "x").exists())
        self.assertFalse((self.tmp / "x").exists())

    def test_rename_is_objects_only(self):
        before = snapshot(self.repo)
        tree = self.ls(self.build("diff --git a/a.txt b/b.txt\nsimilarity index 100%\nrename from a.txt\nrename to b.txt\n"))
        self.assertEqual(tree, {"b.txt": "100644"})
        self.assertEqual(snapshot(self.repo), before)
        self.assertTrue((self.repo / "a.txt").is_file())
        self.assertFalse((self.repo / "b.txt").exists())

    def test_symlink_is_objects_only(self):
        patch = ("diff --git a/ln b/ln\nnew file mode 120000\n--- /dev/null\n+++ b/ln\n@@ -0,0 +1 @@\n+../../etc/passwd\n"
                 "\\ No newline at end of file\n")
        tree = self.ls(self.build(patch))
        self.assertEqual(tree.get("ln"), "120000")
        self.assert_nothing_on_disk("ln")

    def test_mode_change_is_objects_only(self):
        tree = self.ls(self.build("diff --git a/a.txt b/a.txt\nold mode 100644\nnew mode 100755\n"))
        self.assertEqual(tree, {"a.txt": "100755"})
        self.assertTrue((self.repo / "a.txt").is_file())

    def test_gitmodules_and_a_gitlink_are_objects_only(self):
        mods = ('diff --git a/.gitmodules b/.gitmodules\nnew file mode 100644\n--- /dev/null\n+++ b/.gitmodules\n@@ -0,0 +1,3 @@\n'
                '+[submodule "s"]\n+\tpath = s\n+\turl = file:///nowhere\n')
        link = "diff --git a/s b/s\nnew file mode 160000\n--- /dev/null\n+++ b/s\n@@ -0,0 +1 @@\n+Subproject commit " + "1" * 40 + "\n"
        tree = self.ls(self.build(mods + link))
        self.assertEqual((tree.get(".gitmodules"), tree.get("s")), ("100644", "160000"))
        self.assert_nothing_on_disk(".gitmodules", "s")
        self.assertFalse((self.repo / ".git" / "modules").exists())

    def test_paths_outside_the_tree_are_refused_by_git_and_nothing_is_written(self):
        for name, path in (("dotdot", "../x"), ("githooks", ".git/hooks/x")):
            patch = f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+x\n"
            with self.subTest(name):
                with self.assertRaises(objs.ObjectsError) as cm:
                    self.build(patch)
                self.assertEqual(cm.exception.code, objs.EXIT_INTERNAL)
                self.assertIn("invalid path", cm.exception.msg)
                self.assert_nothing_on_disk()
                self.assertEqual(os.listdir(self.scratch), [])


class PushTests(Base):
    def push(self, sha, branch, protected=("origin/Release",), base="staging"):
        objs.push_commit(self.repo, sha, branch, base, list(protected))

    def test_refusals_are_64(self):
        sha = self.build()
        for branch in ("staging", "main", "-x", "a..b", "/x", "a//b", "bad name", "a;b", "refs/heads/staging", "origin/staging",
                       "Staging", "STAGING", "Main", "master", "origin/main", "refs/remotes/origin/main", "HEAD", "head",
                       "refs/heads/x", "refs/tags/x", "release", "Origin/RELEASE"):
            with self.subTest(branch=branch):
                with self.assertRaises(objs.ObjectsError) as cm:
                    self.push(sha, branch)
                self.assertEqual(cm.exception.code, objs.EXIT_USAGE)
        self.assertEqual(self.origin_ref("main"), "")

    def test_base_and_protected_are_normalised_and_the_env_list_counts(self):
        sha = self.build()
        for base, protected, branch in (("refs/heads/staging", [], "staging"), ("origin/Staging", [], "staging"),
                                        ("staging", ["refs/remotes/origin/Prod"], "prod"), ("develop", [], "Develop"),
                                        ("staging", [], "main")):
            with self.subTest(base=base, protected=protected, branch=branch):
                with self.assertRaises(objs.ObjectsError) as cm:
                    objs.push_commit(self.repo, sha, branch, base, protected)
                self.assertEqual(cm.exception.code, objs.EXIT_USAGE)
        with mock.patch.dict(os.environ, {"SB_PROTECTED_BRANCHES": "Hotfix/Z"}):
            with self.assertRaises(objs.ObjectsError) as cm:
                self.push(sha, "hotfix/z")
        self.assertEqual(cm.exception.code, objs.EXIT_USAGE)
        self.assertEqual(self.origin_ref("hotfix/z"), "")

    def test_the_protected_list_and_base_are_handed_to_the_wrapper(self):
        sha = self.build()
        seen = {}

        def fake(args, *, robot=False, env=None, check=True):
            seen.update(env=env, robot=robot, args=list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(objs, "_git", fake), mock.patch.object(objs, "write_taint_marker"):
            objs.push_commit(self.repo, sha, "feature/y", "staging", ["origin/Release"])
        listed = set(seen["env"]["SB_PROTECTED_BRANCHES"].split(","))
        self.assertTrue({"main", "master", "staging", "release"} <= listed, listed)
        self.assertTrue(seen["robot"])
        self.assertEqual(seen["args"][-1], f"{sha}:refs/heads/feature/y")

    def test_happy_path_and_non_fast_forward(self):
        sha = self.build()
        self.push(sha, "feature/x")
        self.assertEqual(self.origin_ref("feature/x"), sha)
        other = self.build(EVIL_PATCH)               # diverges from sha: same parent, different tree
        with self.assertRaises(objs.ObjectsError) as cm:
            self.push(other, "feature/x")
        self.assertEqual(cm.exception.code, objs.EXIT_PUSH)
        self.assertEqual(self.origin_ref("feature/x"), sha)
        self.assertIn("non-fast-forward", cm.exception.msg)

    def test_only_a_stale_tip_rejection_is_labelled_non_fast_forward(self):
        sha = self.build()
        cases = (("! [rejected]        a -> b (non-fast-forward)", True),
                 ("! [rejected]        a -> b (fetch first)", True),
                 ("! [remote rejected] a -> b (protected branch hook declined)", False),
                 ("! [remote rejected] a -> b (pre-receive hook declined)", False),
                 ("fatal: unable to access: could not resolve host", False))
        for stderr, stale in cases:
            def fake(args, *, robot=False, env=None, check=True, _e=stderr):
                return subprocess.CompletedProcess(args, 1, "", _e)
            with self.subTest(stderr=stderr), mock.patch.object(objs, "_git", fake), mock.patch.object(objs, "write_taint_marker"):
                with self.assertRaises(objs.ObjectsError) as cm:
                    objs.push_commit(self.repo, sha, "feature/y", "staging", [])
                self.assertEqual(cm.exception.code, objs.EXIT_PUSH)
                self.assertEqual("(non-fast-forward)" in cm.exception.msg, stale, cm.exception.msg)
                self.assertIn(stderr[:30], cm.exception.msg)

    def test_the_marker_is_written_before_the_push_and_stays_when_the_push_fails(self):
        import inspect
        self.assertEqual(list(inspect.signature(objs.push_commit).parameters),
                         ["repo", "sha", "branch", "base_branch", "protected", "pr", "nonce_sha"])
        sha = self.build()
        marker = self.repo / ".claude" / "super-board" / "inflight" / "sandbox-taint-feature__m.json"
        order = []
        real_write = objs.write_taint_marker

        def spy(*a, **k):
            order.append("marker")
            return real_write(*a, **k)

        real_git = objs._git

        def git_spy(args, **k):
            if k.get("robot"):
                order.append("push")
                self.assertTrue(marker.is_file(), "the marker must exist before the push runs")
            return real_git(args, **k)

        with mock.patch.object(objs, "write_taint_marker", spy), mock.patch.object(objs, "_git", git_spy):
            objs.push_commit(self.repo, sha, "feature/m", "staging", [], pr=5, nonce_sha=NONCE)
        self.assertEqual(order, ["marker", "push"])
        data = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual((data["pr"], data["nonce_sha"], data["shas"]), (5, NONCE, [sha]))
        other = self.build(EVIL_PATCH)
        with self.assertRaises(objs.ObjectsError) as cm:                           # non-fast-forward: the push fails
            objs.push_commit(self.repo, other, "feature/m", "staging", [], pr=5, nonce_sha=NONCE)
        self.assertEqual(cm.exception.code, objs.EXIT_PUSH)
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["shas"], [sha, other])   # kept, and extended

    def test_an_unreadable_marker_stops_the_push_and_is_left_alone(self):
        sha = self.build()
        marker = self.repo / ".claude" / "super-board" / "inflight" / "sandbox-taint-feature__u.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"{not json")
        with self.assertRaises(objs.ObjectsError) as cm:
            objs.push_commit(self.repo, sha, "feature/u", "staging", [])
        self.assertEqual(cm.exception.code, objs.EXIT_INTERNAL)
        self.assertEqual(marker.read_bytes(), b"{not json")
        self.assertEqual(self.origin_ref("feature/u"), "")


class TaintTests(Base):
    def marker(self, name):
        return self.repo / ".claude" / "super-board" / "inflight" / name

    def test_a_tag_named_like_the_base_does_not_change_the_result(self):
        sha = self.build()
        plain(self.repo, "branch", "card/amb", sha)
        plain(self.repo, "tag", "origin/staging", sha)         # short name origin/staging would now resolve to the tag
        self.assertTrue(objs.is_tainted(self.repo, "card/amb", "staging"))
        objs.write_taint_marker(self.repo, "card/amb2", 1, NONCE, sha)
        with self.assertRaises(objs.ObjectsError) as cm:       # the tag would make sha look merged
            objs.clear_taint_marker(self.repo, "card/amb2", "staging")
        self.assertEqual(cm.exception.code, objs.EXIT_NOT_MERGED)

    def test_a_base_with_capitals_works(self):
        plain(self.repo, "push", "-q", "origin", f"{self.parent}:refs/heads/Develop")
        plain(self.repo, "fetch", "-q", "origin")
        sha = self.build()
        plain(self.repo, "branch", "card/dev", sha)
        plain(self.repo, "branch", "card/devclean", self.parent)
        self.assertTrue(objs.is_tainted(self.repo, "card/dev", "Develop"))
        self.assertFalse(objs.is_tainted(self.repo, "card/devclean", "Develop"))    # needs the live Develop tip to be confirmed
        objs.write_taint_marker(self.repo, "card/dev", 1, NONCE, self.parent)
        self.assertTrue(objs.clear_taint_marker(self.repo, "card/dev", "origin/Develop"))

    def test_an_unreadable_marker_is_internal_error_and_never_overwritten(self):
        path = self.marker("sandbox-taint-card__bad.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        for junk in (b"{not json", b"[1, 2]", b"\xff\xfe"):
            path.write_bytes(junk)
            with self.subTest(junk=junk):
                with self.assertRaises(objs.ObjectsError) as cm:
                    objs.write_taint_marker(self.repo, "card/bad", 1, NONCE, self.parent)
                self.assertEqual(cm.exception.code, objs.EXIT_INTERNAL)
                self.assertEqual(path.read_bytes(), junk)
                with self.assertRaises(objs.ObjectsError) as cm:
                    objs.clear_taint_marker(self.repo, "card/bad", "staging")
                self.assertEqual(cm.exception.code, objs.EXIT_INTERNAL)
                self.assertEqual(path.read_bytes(), junk)

    def advance_origin(self, sha, branch="staging"):
        """Move origin's BRANCH to SHA WITHOUT updating the local origin/BRANCH (a stale remote-tracking ref)."""
        plain(self.repo, "push", "-q", "origin", f"{sha}:refs/heads/tmp-carrier")
        subprocess.run(["git", "--git-dir", str(self.origin), "update-ref", f"refs/heads/{branch}", sha], check=True)

    def commit_with(self, message, parent=None):
        parent = parent or self.parent
        tree = plain(self.repo, "rev-parse", parent + "^{tree}")
        return plain(self.repo, "commit-tree", tree, "-p", parent, "-m", message)

    def test_trailer_marker_and_merged(self):
        sha = self.build()
        self.assertTrue(objs.is_tainted(self.repo, sha, "staging"))              # trailer
        self.assertFalse(objs.is_tainted(self.repo, self.parent, "staging"))     # nothing ahead of base
        plain(self.repo, "branch", "card/4", self.parent)
        self.assertFalse(objs.is_tainted(self.repo, "card/4", "staging"))
        objs.write_taint_marker(self.repo, "card/4", 7, NONCE, sha)
        self.assertTrue(self.marker("sandbox-taint-card__4.json").is_file())
        self.assertTrue(objs.is_tainted(self.repo, "card/4", "staging"))         # marker alone
        plain(self.repo, "branch", "-f", "card/4", sha)
        with self.assertRaises(objs.ObjectsError) as cm:                          # not merged yet
            objs.clear_taint_marker(self.repo, "card/4", "staging")
        self.assertEqual(cm.exception.code, objs.EXIT_NOT_MERGED)
        self.assertTrue(self.marker("sandbox-taint-card__4.json").is_file())
        plain(self.repo, "push", "-q", "origin", f"{sha}:refs/heads/staging")
        plain(self.repo, "fetch", "-q", "origin")
        self.assertTrue(objs.clear_taint_marker(self.repo, "card/4", "refs/heads/staging"))
        self.assertFalse(self.marker("sandbox-taint-card__4.json").exists())
        self.assertFalse(objs.is_tainted(self.repo, "card/4", "staging"))        # merged, marker gone

    def test_marker_content_and_appending_shas(self):
        s1, s2 = self.build(), self.build(EVIL_PATCH)
        path = objs.write_taint_marker(self.repo, "card/5", 9, NONCE, s1)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["branch"], data["pr"], data["nonce_sha"], data["shas"]), ("card/5", 9, NONCE, [s1]))
        self.assertIn("created", data)
        self.assertTrue(path.name.startswith("sandbox-taint-"))
        objs.write_taint_marker(self.repo, "card/5", 9, NONCE, s2)
        objs.write_taint_marker(self.repo, "card/5", 9, NONCE, s2)               # same sha twice: recorded once
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["shas"], [s1, s2])
        with self.assertRaises(objs.ObjectsError) as cm:
            objs.write_taint_marker(self.repo, "card/5", 9, NONCE, "abc")
        self.assertEqual(cm.exception.code, objs.EXIT_USAGE)

    def test_clear_needs_every_recorded_sha_in_the_live_base(self):
        s1, s2 = self.build(), self.build(EVIL_PATCH)
        name = "sandbox-taint-card__6.json"
        objs.write_taint_marker(self.repo, "card/6", 1, NONCE, s1)
        objs.write_taint_marker(self.repo, "card/6", 1, NONCE, s2)
        plain(self.repo, "push", "-q", "origin", f"{s1}:refs/heads/staging")    # only the first sha reaches the base
        plain(self.repo, "fetch", "-q", "origin")
        with self.assertRaises(objs.ObjectsError) as cm:
            objs.clear_taint_marker(self.repo, "card/6", "staging")
        self.assertEqual(cm.exception.code, objs.EXIT_NOT_MERGED)
        self.assertTrue(self.marker(name).is_file())
        merge = plain(self.repo, "commit-tree", plain(self.repo, "rev-parse", s2 + "^{tree}"), "-p", s1, "-p", s2, "-m", "merge")
        plain(self.repo, "push", "-q", "origin", f"{merge}:refs/heads/staging")
        plain(self.repo, "fetch", "-q", "origin")
        self.assertTrue(objs.clear_taint_marker(self.repo, "card/6", "staging"))
        self.assertFalse(self.marker(name).exists())
        self.assertFalse(objs.clear_taint_marker(self.repo, "card/6", "staging"))   # nothing left to clear

    def test_a_second_write_keeps_the_pr_and_collects_every_nonce(self):
        s1, s2, s3 = self.build(), self.build(EVIL_PATCH), self.commit_with("third")
        n2 = "cd" * 32
        path = objs.write_taint_marker(self.repo, "card/11", 7, NONCE, s1)
        objs.write_taint_marker(self.repo, "card/11", None, None, s2)                 # a commit-onto style write: no pr, no nonce
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["pr"], data["nonce_shas"], data["shas"]), (7, [NONCE], [s1, s2]))
        objs.write_taint_marker(self.repo, "card/11", None, n2, s3)                   # the next verify, another nonce
        objs.write_taint_marker(self.repo, "card/11", None, n2, s3)                   # a repeat adds nothing
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["pr"], data["nonce_shas"], data["shas"]), (7, [NONCE, n2], [s1, s2, s3]))
        self.assertEqual(data["nonce_sha"], NONCE)                                    # the old key stays readable
        objs.write_taint_marker(self.repo, "card/11", 8, None, s3)                    # a new non-None pr does replace it
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["pr"], 8)

    def test_a_marker_in_the_old_single_nonce_format_is_read_and_kept(self):
        path = self.marker("sandbox-taint-card__12.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"branch": "card/12", "pr": 3, "nonce_sha": NONCE, "shas": [self.parent], "created": "x"}))
        sha = self.build()
        objs.write_taint_marker(self.repo, "card/12", None, "cd" * 32, sha)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["pr"], data["nonce_shas"], data["shas"]), (3, [NONCE, "cd" * 32], [self.parent, sha]))

    def test_the_live_base_check_reads_through_robot_read_only(self):
        calls = []
        real = objs._git

        def spy(args, **k):
            calls.append((list(args)[2], bool(k.get("robot")), bool(k.get("robot_read"))))
            return real(args, **k)

        self.advance_origin(self.build())
        with mock.patch.object(objs, "_git", spy):
            self.assertTrue(objs._live_base_ok(self.repo, "staging"))
        remote_calls = [c for c in calls if c[0] in ("ls-remote", "fetch")]
        self.assertEqual([c[0] for c in remote_calls], ["ls-remote", "fetch"])
        self.assertTrue(all(c[2] and not c[1] for c in remote_calls), remote_calls)

    def test_clear_has_no_caller_supplied_sha(self):
        import inspect
        self.assertEqual(list(inspect.signature(objs.clear_taint_marker).parameters), ["repo", "branch", "base_branch"])

    def test_a_stale_local_base_is_refreshed_from_the_live_remote(self):
        sha = self.build()
        objs.write_taint_marker(self.repo, "card/8", 1, NONCE, sha)
        self.advance_origin(sha)                                                  # origin/staging is stale locally
        self.assertNotEqual(plain(self.repo, "rev-parse", "origin/staging"), sha)
        self.assertTrue(objs.clear_taint_marker(self.repo, "card/8", "staging"))
        self.assertEqual(plain(self.repo, "rev-parse", "origin/staging"), sha)    # the wrapper fetched it

    def test_is_tainted_judges_against_the_live_base(self):
        sha = self.build()
        plain(self.repo, "branch", "card/9", sha)
        self.assertTrue(objs.is_tainted(self.repo, "card/9", "staging"))
        self.advance_origin(sha)                                                  # now the live base contains it
        self.assertFalse(objs.is_tainted(self.repo, "card/9", "staging"))

    def test_an_unreachable_origin_fails_closed(self):
        plain(self.repo, "branch", "card/10", self.parent)
        objs.write_taint_marker(self.repo, "card/10", 1, NONCE, self.parent)
        gone = self.tmp / "origin-gone.git"
        self.origin.rename(gone)
        self.addCleanup(lambda: gone.rename(self.origin) if gone.exists() else None)
        self.assertTrue(objs.is_tainted(self.repo, self.parent, "staging"))      # clean ref, but the live base is unknown
        with self.assertRaises(objs.ObjectsError) as cm:
            objs.clear_taint_marker(self.repo, "card/10", "staging")
        self.assertEqual(cm.exception.code, objs.EXIT_USAGE)
        self.assertTrue(self.marker("sandbox-taint-card__10.json").is_file())

    def test_trailer_matching_is_case_and_space_tolerant(self):
        for i, line in enumerate(("Mc-Lane-Tool: x", "mc-lane-tool: x", "MC-LANE-TOOL : x", "  mc-lane-tool :x", "Mc-Lane-Tool:")):
            with self.subTest(line=line):
                sha = self.commit_with(f"subject\n\n{line}")
                plain(self.repo, "branch", "-f", f"t{i}", sha)
                self.assertTrue(objs.is_tainted(self.repo, f"t{i}", "staging"))
        sha = self.commit_with("subject\n\nMc-Lane-Toolbox: x\nnot mc-lane-tool: x")
        plain(self.repo, "branch", "-f", "plainb", sha)
        self.assertFalse(objs.is_tainted(self.repo, "plainb", "staging"))

    def test_a_ref_starting_with_a_dash_is_refused(self):
        with self.assertRaises(objs.ObjectsError) as cm:
            objs.is_tainted(self.repo, "--output=x", "staging")
        self.assertEqual(cm.exception.code, objs.EXIT_USAGE)


class TrailerTests(unittest.TestCase):
    def test_equals_lane_delegate_inline_format(self):
        src = (PLUGIN / "scripts" / "super-board-lane-delegate.py").read_text(encoding="utf-8")
        m = re.search(r'trailers = f"(Mc-Lane-Tool: )\{[^}]*\}" \+ NL \+ f"(Mc-Lane-Nonce-Sha256: )\{[^}]*\}"', src)
        if m:
            expected = m.group(1) + "codex" + "\n" + m.group(2) + NONCE
        else:                                    # Task 4 replaced the inline string with the helper
            self.assertIn("lane_trailers(", src)
            expected = "Mc-Lane-Tool: codex\nMc-Lane-Nonce-Sha256: " + NONCE
        self.assertEqual(objs.lane_trailers("codex", NONCE), expected)

    def test_control_chars_cannot_inject_a_trailer(self):
        lines = objs.lane_trailers("x\nMc-Lane-Tool: y", NONCE).splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(sum(1 for ln in lines if ln.startswith("Mc-Lane-Tool:")), 1)

    def test_nonce_must_be_64_lowercase_hex(self):
        for bad in ("", "abc123", "AB" * 32, "ab" * 31, "ab" * 33, "zz" * 32, NONCE + "\n", None):
            with self.subTest(bad=bad):
                with self.assertRaises(objs.ObjectsError) as cm:
                    objs.lane_trailers("codex", bad)
                self.assertEqual(cm.exception.code, objs.EXIT_USAGE)


class InterpreterTests(unittest.TestCase):
    def env(self, **kw):
        base = {k: v for k, v in os.environ.items() if k not in ("SB_BASH", "SB_OBJECTS_TEST")}
        base.update(kw)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_sb_bash_needs_the_test_flag(self):
        with self.env(SB_BASH="/opt/evil/bash"):
            self.assertNotEqual(objs._bash(), "/opt/evil/bash")
        with self.env(SB_BASH="/opt/evil/bash", SB_OBJECTS_TEST="1"):
            self.assertEqual(objs._bash(), "/opt/evil/bash")
        with self.env(SB_BASH="/opt/evil/bash", SB_OBJECTS_TEST="0"):
            self.assertNotEqual(objs._bash(), "/opt/evil/bash")

    def test_the_path_fallback_refuses_wsl_launchers(self):
        for bad in ("C:\\Windows\\System32\\bash.exe", "C:/Windows/Sysnative/bash.exe",
                    "C:\\Users\\u\\AppData\\Local\\Microsoft\\WindowsApps\\bash.exe"):
            with self.subTest(bad):
                with self.env(), mock.patch.object(objs.os, "name", "posix"), mock.patch.object(objs.shutil, "which", lambda n: bad):
                    with self.assertRaises(objs.ObjectsError) as cm:
                        objs._bash()
                self.assertEqual(cm.exception.code, objs.EXIT_MISSING)
        with self.env(), mock.patch.object(objs.os, "name", "posix"), mock.patch.object(objs.shutil, "which", lambda n: "/usr/bin/bash"):
            self.assertEqual(objs._bash(), "/usr/bin/bash")


if __name__ == "__main__":
    unittest.main()
