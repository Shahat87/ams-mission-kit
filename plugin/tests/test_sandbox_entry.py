#!/usr/bin/env python3
"""Unit tests for plugin/sandbox/entry.py (no Docker). Run: cd plugin && python3 tests/test_sandbox_entry.py
MCL_ENTRY_PATH overrides the module path (used to prove the tests go red against a broken copy)."""
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SANDBOX = HERE.parent / "sandbox"
ENTRY = Path(os.environ.get("MCL_ENTRY_PATH") or SANDBOX / "entry.py")
spec = importlib.util.spec_from_file_location("mc_lane_entry", ENTRY)
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)

POSIX = hasattr(os, "getuid")
HAVE_GIT = shutil.which("git") is not None
HAVE_SH = shutil.which("sh") is not None


def can_symlink():
    d = tempfile.mkdtemp()
    try:
        os.symlink("x", os.path.join(d, "l"))
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


CAN_LINK = can_symlink()


class Tmp(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="mclt-"))
        self.addCleanup(shutil.rmtree, str(self.d), True)

    def mk(self, *parts):
        p = self.d.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


class Helpers(Tmp):
    def test_parse_report_last_match_wins(self):
        t = '<mc-lane-report>{"a": 1}</mc-lane-report> noise <mc-lane-report>{"b": 2}</mc-lane-report>'
        self.assertEqual(E.parse_report(t), {"b": 2})

    def test_parse_report_strips_fences(self):
        self.assertEqual(E.parse_report('<mc-lane-report>\n```json\n{"x": [1]}\n```\n</mc-lane-report>'), {"x": [1]})
        self.assertEqual(E.parse_report('<mc-lane-report>```\n{"x": 1}\n```</mc-lane-report>'), {"x": 1})

    def test_parse_report_invalid_or_missing(self):
        self.assertIsNone(E.parse_report("nothing"))
        self.assertIsNone(E.parse_report("<mc-lane-report>not json</mc-lane-report>"))
        self.assertIsNone(E.parse_report("<mc-lane-report>[1,2]</mc-lane-report>"))   # not an object
        # last match invalid -> None, even though an earlier one was valid
        self.assertIsNone(E.parse_report('<mc-lane-report>{"a":1}</mc-lane-report><mc-lane-report>oops</mc-lane-report>'))

    def test_parse_report_oversize(self):
        big = json.dumps({"s": "x" * 70000})
        self.assertIsNone(E.parse_report("<mc-lane-report>%s</mc-lane-report>" % big))
        self.assertEqual(E.parse_report("<mc-lane-report>%s</mc-lane-report>" % big, max_bytes=100000)["s"][:2], "xx")

    def test_strip_ctrl(self):
        self.assertEqual(E.strip_ctrl("a\x1b[31mb\x00c\td\n\r\x07e\x7f"), "a[31mbc\td\ne")
        self.assertEqual(E.strip_ctrl("::add-mask::x\n"), "::add-mask::x\n")   # text kept; only control chars go

    def test_scrub_env(self):
        env = {"PATH": "/usr/bin", "HOME": "/h", "OPENAI_API_KEY": "k",
               "AMS_OPERATOR_EXPECTED": "1", "AMS_OPERATOR_DATA_ROOT": "/x", "SAP_LIVE_TESTS": "1",
               "ACTIONS_RUNTIME_TOKEN": "t", "RUNNER_TEMP": "/t", "GITHUB_TOKEN": "t", "GH_TOKEN": "t",
               "gh_config_dir": "/c", "LANE_REPO_TOKEN": "t", "MCL_TOOL": "codex", "MCL_HARD_S": "9", "EXTRA": "e"}
        out = E.scrub_env(env, extra_deny=["EXTRA"])
        self.assertEqual(out, {"PATH": "/usr/bin", "HOME": "/h", "OPENAI_API_KEY": "k"})

    def test_model_validation(self):
        for good in ("gpt-5.6-luna", "runinfra/glm-5-3-flash", "ok", "a:b_c"):
            self.assertTrue(E.MODEL_RE.match(good), good)
        for bad in ("", "-rf", "--sandbox", "a b", "a;b", "$(x)", "../x"[0:0] + "x" * 101):
            self.assertFalse(E.MODEL_RE.match(bad), bad)


class AuthFile(Tmp):
    def uid(self):
        return os.getuid() if POSIX else None

    def test_regular_file_accepted(self):
        p = self.mk("auth.json")
        p.write_text("{}")
        self.assertTrue(E.auth_file_ok(str(p), self.uid()))

    def test_oversize_rejected(self):
        p = self.mk("auth.json")
        p.write_bytes(b"x" * (64 * 1024 + 1))
        self.assertFalse(E.auth_file_ok(str(p), self.uid()))
        p.write_bytes(b"x" * (64 * 1024))
        self.assertTrue(E.auth_file_ok(str(p), self.uid()))

    @unittest.skipUnless(CAN_LINK, "symlinks unavailable")
    def test_symlink_rejected(self):
        real = self.mk("real.json")
        real.write_text("{}")
        link = self.mk("auth.json")
        os.symlink(str(real), str(link))
        self.assertFalse(E.auth_file_ok(str(link), self.uid()))

    @unittest.skipUnless(POSIX, "uid ownership is POSIX only")
    def test_wrong_uid_rejected(self):
        p = self.mk("auth.json")
        p.write_text("{}")
        self.assertFalse(E.auth_file_ok(str(p), os.getuid() + 1))

    def test_missing_rejected(self):
        self.assertFalse(E.auth_file_ok(str(self.d / "nope.json"), self.uid()))

    @unittest.skipUnless(CAN_LINK, "symlinks unavailable")
    def test_copy_auth_states(self):
        cred = self.mk("cred", "codex", "x").parent
        dest = str(self.d / "home" / ".codex" / "auth.json")
        self.assertEqual(E.copy_auth(str(self.d / "nocred"), "auth.json", dest, self.uid()), "absent")
        self.assertEqual(E.copy_auth(str(cred), "auth.json", dest, self.uid()), "bad")        # dir mounted, no file
        (cred / "auth.json").write_text('{"t": 1}')
        self.assertEqual(E.copy_auth(str(cred), "auth.json", dest, self.uid()), "copied")
        self.assertFalse(os.path.islink(dest))                                                # a copy, never a link into /cred
        self.assertEqual(Path(dest).read_text(), '{"t": 1}')
        Path(dest).write_text("refreshed")                                                    # the tool may refresh its copy
        self.assertEqual((cred / "auth.json").read_text(), '{"t": 1}')                        # the cred volume is untouched


class TarAndCopy(Tmp):
    def test_tar_only_regular_files(self):
        src = self.d / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_text("a")
        (src / "sub" / "b.txt").write_text("b")
        (src / "emptydir").mkdir()
        if CAN_LINK:
            os.symlink("a.txt", str(src / "link.txt"))
            os.symlink(str(src / "sub"), str(src / "dirlink"))
        if hasattr(os, "mkfifo"):
            os.mkfifo(str(src / "fifo"))
        names = E.build_tar(str(src), str(self.d / "o.tar"))
        self.assertEqual(sorted(names), ["a.txt", "sub/b.txt"])
        with tarfile.open(str(self.d / "o.tar")) as tf:
            self.assertEqual(sorted(tf.getnames()), ["a.txt", "sub/b.txt"])
            self.assertTrue(all(m.isreg() for m in tf.getmembers()))

    @unittest.skipUnless(CAN_LINK, "symlinks unavailable")
    def test_copy_tree_ignores_git_and_keeps_links(self):
        src = self.d / "src"
        (src / ".git").mkdir(parents=True)
        (src / ".git" / "HEAD").write_text("evil")
        (src / "pkg" / ".git").mkdir(parents=True)
        (src / "pkg" / "m.py").write_text("x")
        (src / "real.txt").write_text("r")
        os.symlink("real.txt", str(src / "ln"))
        os.symlink("/etc", str(src / "abs"))
        dst = self.d / "dst"
        E.copy_tree_nolinks(str(src), str(dst))
        self.assertFalse((dst / ".git").exists())
        self.assertFalse((dst / "pkg" / ".git").exists())
        self.assertEqual((dst / "pkg" / "m.py").read_text(), "x")
        self.assertTrue(os.path.islink(str(dst / "ln")))
        self.assertEqual(os.readlink(str(dst / "ln")), "real.txt")
        self.assertEqual(os.readlink(str(dst / "abs")), "/etc")      # recreated, never followed

    def test_empty_dir_except_git(self):
        r = self.d / "r"
        (r / ".git").mkdir(parents=True)
        (r / ".git" / "HEAD").write_text("ref")
        (r / "f").write_text("1")
        (r / "d" / "e").mkdir(parents=True)
        (r / "d" / "e" / "g").write_text("2")
        E.empty_dir_except(str(r))
        self.assertEqual(sorted(os.listdir(str(r))), [".git"])
        self.assertTrue((r / ".git" / "HEAD").exists())


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=str(cwd), check=True,
                   capture_output=True)


@unittest.skipUnless(HAVE_GIT, "git unavailable")
class Extract(Tmp):
    def test_extract_deletions_additions_and_bundle(self):
        pristine = self.d / "pristine"
        pristine.mkdir()
        git(pristine, "init", "-q")
        (pristine / "a.txt").write_text("alpha\n")
        (pristine / "keep.txt").write_text("keep\n")
        git(pristine, "add", "-A")
        git(pristine, "commit", "-qm", "start")
        start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(pristine), capture_output=True, text=True).stdout.strip()
        tool = self.d / "tool"                       # the tool's tree: a.txt deleted, c.txt added, keep.txt edited
        (tool / ".git").mkdir(parents=True)
        (tool / ".git" / "config").write_text("[core]\n\tfsmonitor = evil\n")
        (tool / "keep.txt").write_text("keep2\n")
        (tool / "c.txt").write_text("new\n")
        outsrc = self.d / "outsrc"
        (outsrc / "session").mkdir(parents=True)
        (outsrc / "raw.log").write_bytes(b"A" * (6 * 1024 * 1024) + b"TAILMARK")
        (outsrc / "report.json").write_text('{"ok": true}')
        (outsrc / "session" / "s1.jsonl").write_text("{}\n")
        (outsrc / "session" / "notes.txt").write_text("skip me")
        (outsrc / "result.json").write_text('{"kind": "ok"}')          # forged; must not travel
        if CAN_LINK:
            os.symlink(str(self.d / "tool" / "c.txt"), str(outsrc / "session" / "evil.jsonl"))
        x = self.d / "x"
        x.mkdir()
        rc = E.do_extract(str(tool), str(pristine), str(outsrc), str(x))
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(os.listdir(str(x))), ["bundle.tar"])
        with tarfile.open(str(x / "bundle.tar")) as tf:
            self.assertTrue(all(m.isreg() for m in tf.getmembers()))
            names = sorted(tf.getnames())
            self.assertEqual(names, ["facts.json", "lane.patch", "raw.log", "report.json", "session/001.jsonl"])
            patch = tf.extractfile("lane.patch").read()
            facts = json.loads(tf.extractfile("facts.json").read())
            raw = tf.extractfile("raw.log").read()
        self.assertIn(b"deleted file mode", patch)            # deletion preserved
        self.assertIn(b"a/a.txt", patch)
        self.assertIn(b"c.txt", patch)
        self.assertIn(b"+keep2", patch)
        self.assertNotIn(b"evil", patch)                      # tool's .git never copied
        self.assertEqual(facts["start_sha"], start)
        import hashlib
        self.assertEqual(facts["patch_sha256"], hashlib.sha256(patch).hexdigest())
        self.assertEqual(facts["patch_bytes"], len(patch))
        self.assertEqual(len(raw), 5_000_000)
        self.assertTrue(raw.endswith(b"TAILMARK"))

    def test_extract_symlink_becomes_mode_120000(self):
        if not CAN_LINK:
            self.skipTest("symlinks unavailable")
        pristine = self.d / "pristine"
        pristine.mkdir()
        git(pristine, "init", "-q")
        (pristine / "a.txt").write_text("a\n")
        git(pristine, "add", "-A")
        git(pristine, "commit", "-qm", "s")
        tool = self.d / "tool"
        tool.mkdir()
        (tool / "a.txt").write_text("a\n")
        os.symlink("/etc/passwd", str(tool / "link"))
        x = self.d / "x"
        x.mkdir()
        outsrc = self.d / "outsrc"
        outsrc.mkdir()
        self.assertEqual(E.do_extract(str(tool), str(pristine), str(outsrc), str(x)), 0)
        with tarfile.open(str(x / "bundle.tar")) as tf:
            self.assertIn(b"120000", tf.extractfile("lane.patch").read())


class StageAndPut(Tmp):
    def _seed(self):
        src = self.d / "src"
        src.mkdir()
        git(src, "init", "-q")
        (src / "a.txt").write_text("alpha\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "start")
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(src), capture_output=True, text=True).stdout.strip()
        seed = self.d / "seed.git"
        subprocess.run(["git", "init", "-q", "--bare", str(seed)], check=True)
        subprocess.run(["git", "-C", str(src), "push", "-q", str(seed), "HEAD:refs/mc/start"], check=True)
        return seed, sha

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_stage_clones_at_sha_without_a_remote(self):
        seed, sha = self._seed()
        dest = self.d / "work"
        self.assertEqual(E.do_stage(str(seed), str(dest), sha), 0)
        self.assertEqual((dest / "a.txt").read_text().replace("\r\n", "\n"), "alpha\n")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(dest), capture_output=True, text=True).stdout.strip()
        self.assertEqual(head, sha)
        cfg = subprocess.run(["git", "config", "--list"], cwd=str(dest), capture_output=True, text=True).stdout
        self.assertNotIn("remote.", cfg)

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_stage_refuses_wrong_sha_and_non_empty_dest(self):
        seed, sha = self._seed()
        self.assertNotEqual(E.do_stage(str(seed), str(self.d / "w1"), "1" * 40), 0)
        full = self.d / "w2"
        full.mkdir()
        (full / "x").write_text("x")
        self.assertNotEqual(E.do_stage(str(seed), str(full), sha), 0)
        self.assertEqual(E.do_stage(str(seed), str(self.d / "w3"), "nope"), E.EX_USAGE)

    def _merge_seed(self, conflict=False):
        """A seed holding the PR head (refs/mc/start) and the base (refs/mc/base), which both moved on from one commit."""
        src = self.d / "msrc"
        src.mkdir()
        rev = lambda: subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(src), capture_output=True, text=True).stdout.strip()
        git(src, "init", "-q")
        (src / "a.txt").write_text("alpha\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "common")
        common = rev()
        git(src, "checkout", "-q", "-b", "base")
        (src / ("a.txt" if conflict else "base.txt")).write_text("base side\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "base")
        base = rev()
        git(src, "checkout", "-q", "-b", "head", common)
        (src / ("a.txt" if conflict else "head.txt")).write_text("head side\n")
        git(src, "add", "-A")
        git(src, "commit", "-qm", "head")
        head = rev()
        seed = self.d / "mseed.git"
        subprocess.run(["git", "init", "-q", "--bare", str(seed)], check=True)
        subprocess.run(["git", "-C", str(src), "push", "-q", str(seed), "head:refs/mc/start", "base:refs/mc/base"], check=True)
        return seed, head, base

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_stage_merges_the_base_into_the_head(self):
        seed, head, base = self._merge_seed()
        dest = self.d / "merged"
        self.assertEqual(E.do_stage(str(seed), str(dest), head, merge_sha=base), 0)
        self.assertEqual((dest / "head.txt").read_text().replace("\r\n", "\n"), "head side\n")
        self.assertEqual((dest / "base.txt").read_text().replace("\r\n", "\n"), "base side\n")
        parents = subprocess.run(["git", "rev-list", "--parents", "-n", "1", "HEAD"], cwd=str(dest), capture_output=True,
                                 text=True).stdout.split()
        self.assertEqual(parents[1:], [head, base])                       # a real --no-ff merge commit
        who = subprocess.run(["git", "log", "-1", "--format=%an <%ae>"], cwd=str(dest), capture_output=True, text=True).stdout.strip()
        self.assertEqual(who, "mc-lane <mc-lane@invalid>")
        cfg = subprocess.run(["git", "config", "--list"], cwd=str(dest), capture_output=True, text=True).stdout
        self.assertNotIn("remote.", cfg)
        self.assertNotIn("user.", cfg)                                    # the identity was -c only, never written

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_stage_merge_conflict_is_exit_3(self):
        seed, head, base = self._merge_seed(conflict=True)
        self.assertEqual(E.do_stage(str(seed), str(self.d / "conflicted"), head, merge_sha=base), 3)

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_stage_merging_the_commit_itself_changes_nothing(self):
        seed, head, _base = self._merge_seed()
        dest = self.d / "same"
        self.assertEqual(E.do_stage(str(seed), str(dest), head, merge_sha=head), 0)
        now = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(dest), capture_output=True, text=True).stdout.strip()
        self.assertEqual(now, head)

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_stage_merge_refuses_a_bad_or_missing_base(self):
        seed, head, _base = self._merge_seed()
        self.assertEqual(E.do_stage(str(seed), str(self.d / "m1"), head, merge_sha="nope"), E.EX_USAGE)
        self.assertEqual(E.do_stage(str(seed), str(self.d / "m2"), head, merge_sha="1" * 40), 2)

    def _tar(self, members):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, data in members:
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
        buf.seek(0)
        return buf

    def test_put_writes_flat_regular_files(self):
        dest = self.d / "dst"
        self.assertEqual(E.do_put(str(dest), self._tar([("brief", b"hello"), ("lane.patch", b"p")])), 0)
        self.assertEqual((dest / "brief").read_bytes(), b"hello")
        self.assertEqual(sorted(os.listdir(str(dest))), ["brief", "lane.patch"])

    def test_put_refuses_unsafe_names(self):
        for name in ("../x", "/etc/x", "a/b", "a b", ""):
            dest = self.d / ("d" + str(abs(hash(name))))
            self.assertEqual(E.do_put(str(dest), self._tar([(name or "x", b"1")] if name else [("a/b", b"1")])),
                             E.EX_USAGE, name)


@unittest.skipUnless(HAVE_GIT, "git unavailable")
class Prepare(Tmp):
    """A13: entry.py only applies the patch. It never runs a verify command; the host does, with docker exec."""

    def _repo_and_patch(self):
        repo = self.d / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / "a.txt").write_text("a\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "s")
        (repo / "b.txt").write_text("b\n")
        git(repo, "add", "-A")
        patch = subprocess.run(["git", "diff", "--cached", "--binary"], cwd=str(repo), capture_output=True).stdout
        git(repo, "reset", "-q", "--hard")
        subprocess.run(["git", "clean", "-fdq"], cwd=str(repo), check=True)
        pf = self.d / "lane.patch"
        pf.write_bytes(patch)
        return repo, pf

    def test_applies_patch_to_index_and_stage_and_logs(self):
        repo, pf = self._repo_and_patch()
        x = self.d / "x"
        x.mkdir()
        self.assertEqual(E.do_prepare(str(repo), str(pf), str(x)), 0)
        self.assertEqual((repo / "b.txt").read_text(), "b\n")
        self.assertIn("b.txt", subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=str(repo),
                                              capture_output=True, text=True).stdout)
        self.assertEqual(json.loads((x / "prepare.json").read_text()), {"applied": True, "error": ""})
        self.assertEqual(sorted(os.listdir(str(x))), ["prepare.json"])               # no verify.json any more

    def test_bad_patch_exits_1_with_the_error(self):
        repo, pf = self._repo_and_patch()
        pf.write_bytes(b"not a patch at all\n")
        x = self.d / "x"
        x.mkdir()
        self.assertEqual(E.do_prepare(str(repo), str(pf), str(x)), 1)
        res = json.loads((x / "prepare.json").read_text())
        self.assertFalse(res["applied"])
        self.assertTrue(res["error"])

    def test_verify_subcommand_is_gone(self):
        self.assertFalse(hasattr(E, "do_verify"))
        with self.assertRaises(SystemExit):
            E.build_parser().parse_args(["verify"])
        a = E.build_parser().parse_args(["prepare"])
        self.assertEqual((a.repo, a.patch, a.x), ("/repo", "/in/lane.patch", "/x"))


class RunLane(Tmp):
    """run_lane with a python one-liner as the 'tool'; no Docker."""

    def setUp(self):
        super().setUp()
        for n in ("work", "out", "home", "tmp", "cred", "opt"):
            (self.d / n).mkdir()
        (self.d / "out" / "brief").write_text("do the thing")
        self.cfg = {n: str(self.d / n) for n in ("work", "out", "home", "tmp", "cred", "opt")}
        self.cfg["uid"] = os.getuid() if POSIX else 1000

    def tool(self, script, **extra):
        spec = {"argv": [sys.executable, "-c", script, "{model}"], "stdin_brief": True}
        spec.update(extra)
        (self.d / "opt" / "tools.json").write_text(json.dumps({"tools": {"t": spec}}))

    def env(self, **kw):
        e = {"PATH": os.environ.get("PATH", ""), "MCL_TOOL": "t", "MCL_MODEL": "m1"}
        if "SystemRoot" in os.environ:
            e["SystemRoot"] = os.environ["SystemRoot"]
        e.update(kw)
        return e

    def test_happy_path_writes_log_report_and_no_result_json(self):
        script = ("import sys,os\nb=sys.stdin.read()\nprint('got:'+b)\nprint('gh=' + str('GH_TOKEN' in os.environ)"
                  " + ' mcl=' + str('MCL_MODEL' in os.environ))\nprint('\\x1b[31mred')\n"
                  "print('<mc-lane-report>```json\\n{\"summary\": \"done\"}\\n```</mc-lane-report>')\nsys.exit(3)")
        self.tool(script)
        rc = E.run_lane(self.cfg, self.env(GH_TOKEN="secret", LANE_REPO_TOKEN="s2"), euid=1000)
        self.assertEqual(rc, 3)                                   # the tool's exit code
        log = (self.d / "out" / "raw.log").read_text()
        self.assertIn("got:do the thing", log)
        self.assertIn("gh=False mcl=False", log)
        self.assertNotIn("\x1b", log)
        self.assertEqual(json.loads((self.d / "out" / "report.json").read_text()), {"summary": "done"})
        self.assertFalse((self.d / "out" / "result.json").exists())          # A1
        self.assertLessEqual(set(os.listdir(str(self.d / "out"))), {"brief", "raw.log", "report.json", "session"})

    def test_refuses_uid_zero(self):
        self.tool("print(1)")
        self.assertEqual(E.run_lane(self.cfg, self.env(), euid=0), E.EX_CONFIG)
        self.assertFalse((self.d / "out" / "raw.log").exists())

    def test_bad_model_and_unknown_tool(self):
        self.tool("print(1)")
        self.assertEqual(E.run_lane(self.cfg, self.env(MCL_MODEL="--evil"), euid=1000), E.EX_USAGE)
        self.assertEqual(E.run_lane(self.cfg, self.env(MCL_TOOL="nope"), euid=1000), E.EX_USAGE)

    def test_hard_timeout_kills_and_returns_124(self):
        self.tool("import time\nprint('start', flush=True)\ntime.sleep(60)")
        rc = E.run_lane(self.cfg, self.env(MCL_HARD_S="1", MCL_IDLE_S="30"), euid=1000)
        self.assertEqual(rc, 124)
        self.assertIn("start", (self.d / "out" / "raw.log").read_text())

    def test_idle_timeout(self):
        self.tool("import time\nprint('start', flush=True)\ntime.sleep(60)")
        rc = E.run_lane(self.cfg, self.env(MCL_HARD_S="60", MCL_IDLE_S="1"), euid=1000)
        self.assertEqual(rc, 124)

    def test_profile_and_session_capture(self):
        prof = self.d / "opt" / "profiles" / "p"
        prof.mkdir(parents=True)
        (prof / "config.toml").write_text("x = 1")
        script = ("import os\nd=os.path.join(os.environ['HOME'],'.p','sessions','2026')\nos.makedirs(d)\n"
                  "open(os.path.join(d,'rollout-1.jsonl'),'w').write('{}\\n')\n"
                  "open(os.path.join(d,'other.txt'),'w').write('no')\nprint(open(os.path.join(os.environ['HOME'],'.p','config.toml')).read())")
        self.tool(script, profile={"src": "p", "dest": ".p"}, session_globs=[".p/sessions/**/*"])
        self.assertEqual(E.run_lane(self.cfg, self.env(), euid=1000), 0)
        self.assertIn("x = 1", (self.d / "out" / "raw.log").read_text())
        self.assertEqual(os.listdir(str(self.d / "out" / "session")), ["001-rollout-1.jsonl"])

    @unittest.skipUnless(CAN_LINK, "symlinks unavailable")
    def test_symlinked_auth_json_exits_77(self):
        real = self.d / "elsewhere.json"
        real.write_text("{}")
        (self.d / "cred" / "codex").mkdir()
        os.symlink(str(real), str(self.d / "cred" / "codex" / "auth.json"))
        self.tool("print('should not run')", auth_file={"cred_dir": "codex", "name": "auth.json", "dest": ".codex/auth.json"})
        self.assertEqual(E.run_lane(self.cfg, self.env(), euid=1000), E.EX_NOPERM)
        self.assertFalse((self.d / "out" / "raw.log").exists())

    def test_good_auth_json_is_copied_and_env_mode_skips(self):
        if not POSIX:
            self.skipTest("the owner check needs POSIX uids")
        (self.d / "cred" / "codex").mkdir()
        (self.d / "cred" / "codex" / "auth.json").write_text("{}")
        self.tool("import os\np=os.path.join(os.environ['HOME'],'.codex','auth.json')\nprint(os.path.isfile(p) and not os.path.islink(p))",
                  auth_file={"cred_dir": "codex", "name": "auth.json", "dest": ".codex/auth.json"})
        self.assertEqual(E.run_lane(self.cfg, self.env(), euid=self.cfg["uid"]), 0)
        self.assertIn("True", (self.d / "out" / "raw.log").read_text())
        shutil.rmtree(str(self.d / "cred" / "codex"))                     # env-auth mode: no cred dir, still runs
        self.assertEqual(E.run_lane(self.cfg, self.env(), euid=self.cfg["uid"]), 0)

    def test_missing_executable_is_127(self):
        (self.d / "opt" / "tools.json").write_text(json.dumps({"tools": {"t": {"argv": ["definitely-not-a-binary-xyz"]}}}))
        self.assertEqual(E.run_lane(self.cfg, self.env(), euid=1000), E.EX_NOTFOUND)


class CliParser(unittest.TestCase):
    def test_parser_knows_every_subcommand(self):
        p = E.build_parser()
        self.assertEqual(p.parse_args(["run"]).cmd, "run")
        self.assertEqual(p.parse_args(["stage", "--sha", "a" * 40]).seed, "/seed")
        self.assertEqual(p.parse_args(["put", "--dest", "/dst"]).dest, "/dst")


class StaticFiles(unittest.TestCase):
    def test_tools_json_shape_and_no_secrets(self):
        t = json.loads((SANDBOX / "tools.json").read_text())["tools"]
        self.assertEqual(sorted(t), ["codex", "grok", "pi"])             # `fake` exists only in the smoke image
        self.assertEqual(t["codex"]["argv"][:3], ["codex", "exec", "--json"])
        self.assertIn("-", t["codex"]["argv"][-1:])
        self.assertIn("--prompt-file", t["grok"]["argv"])
        self.assertFalse(t["grok"]["stdin_brief"])
        self.assertIn("--no-approve", t["pi"]["argv"])
        for name, s in t.items():
            self.assertTrue(all(isinstance(a, str) for a in s["argv"]), name)
            self.assertIn("unverified", s, name)

    def test_pi_models_has_no_key(self):
        m = json.loads((SANDBOX / "profiles" / "pi" / "models.json").read_text())
        p = m["providers"]["runinfra"]
        self.assertEqual(p["baseUrl"], "https://api.runinfra.ai")
        self.assertEqual(p["apiKey"], "RUNINFRA_GATEWAY_KEY")            # the env var NAME, not a value

    def test_squid_template(self):
        s = (SANDBOX / "proxy" / "squid.conf.tmpl").read_text()
        self.assertEqual(s.count("{allow_hosts}"), 1)
        r = s.replace("{allow_hosts}", "a.example.com b.example.com")
        self.assertNotIn("{", r)
        self.assertIn("acl allow_hosts dstdomain a.example.com b.example.com", r)
        self.assertIn("http_access deny all", r)
        self.assertLess(r.index("http_access deny private"), r.index("http_access allow allow_hosts"))
        self.assertLess(r.index("http_access deny ip_literal"), r.index("http_access allow allow_hosts"))
        self.assertIn("cache deny all", r)
        self.assertNotIn("/etc/squid", r)

    def test_dockerfile_invariants(self):
        d = (SANDBOX / "Dockerfile").read_text()
        for pin in ("@openai/codex@0.160.1", "@xai-official/grok@1.0.50", "@earendil-works/pi-coding-agent@1.0.4",
                    "node:22-bookworm-slim@sha256:c3de60bf", "python:3.14-slim-bookworm@sha256:48b13b00", "USER 1000:1000"):
            self.assertIn(pin, d)
        self.assertNotIn("| bash", d)
        self.assertNotIn("| sh", d)


if __name__ == "__main__":
    unittest.main(verbosity=1)
