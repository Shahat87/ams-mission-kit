#!/usr/bin/env python3
"""Offline contract for scripts/super-board-lane-remote.py and sandbox/lane.yml (Plan 1b Task 4).

gh is a stub script run as [python, stub.py] through MC_LANE_GH_CMD, so it works on Windows too.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/super-board-lane-remote.py"
LANE_YML = ROOT / "sandbox/lane.yml"

spec = importlib.util.spec_from_file_location("lane_remote", SCRIPT)
remote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remote)

NONCE = "a" * 32
OTHER_NONCE = "b" * 32
START = "1" * 40

STUB = r'''
import json, os, sys, pathlib
d = pathlib.Path(os.environ["MC_STUB_DIR"])
argv = sys.argv[1:]
stdin = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
with open(d / "calls.jsonl", "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv, "stdin": stdin.decode("utf-8", "replace")}) + "\n")
rules = json.loads((d / "rules.json").read_text(encoding="utf-8"))
state_path = d / "state.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {}
line = " ".join(argv)
for i, rule in enumerate(rules):
    if all(tok in line for tok in rule["match"]):
        seq = rule["seq"]
        n = state.get(str(i), 0)
        state[str(i)] = n + 1
        state_path.write_text(json.dumps(state))
        r = seq[min(n, len(seq) - 1)]
        if r.get("out_file"):
            sys.stdout.buffer.write(pathlib.Path(r["out_file"]).read_bytes())
        else:
            sys.stdout.write(r.get("out", ""))
        sys.stderr.write(r.get("err", ""))
        sys.exit(r.get("rc", 0))
sys.stderr.write("stub: no rule for " + line)
sys.exit(97)
'''

GH_OK = {"match": ["--version"], "seq": [{"out": "gh version 2.101.0 (2026-09-15)\n"}]}


def cfg(**over):
    base = {"runner_repo": "example-org/mc-lane-runner", "workflow": "lane.yml", "repos": ["Shahat87/ams-mission-kit"],
            "tools": ["codex", "pi-delegate"], "models": {"codex": ["gpt-5.6-luna"]}, "lanes": ["build", "qa", "review", "gate"],
            "tool_cfgs": [{"id": "codex", "auth": {"kind": "file"}, "egress": ["chatgpt.com"]},
                          {"id": "pi-delegate", "auth": {"kind": "env", "keys": ["RUNINFRA_GATEWAY_KEY"]},
                           "egress": ["api.runinfra.ai"]}],
            "payload": {"image": "mc-lane@sha256:" + "a" * 64, "verify_commands": ["true"]}}
    base.update(over)
    return base


def good_kwargs(**over):
    kw = dict(repo="Shahat87/ams-mission-kit", start_sha=START, card=12, lane="build", attempt=1, tool="codex",
              model="gpt-5.6-luna", nonce=NONCE, timeout_min=45, brief_gz_b64=remote.encode_brief(b"do the thing"),
              config_b64=remote.encode_config({"verify_commands": ["true"]}))
    kw.update(over)
    return kw


def make_zip(path, entries):
    """entries: (name, data, unix_mode_or_None)"""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            if mode is not None:
                info.create_system = 3
                info.external_attr = mode << 16
            zf.writestr(info, data)


def make_bundle(files, symlink=None, extra=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        if symlink:
            ti = tarfile.TarInfo(symlink)
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tf.addfile(ti)
        for name, data in (extra or {}).items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def write_artifact(directory, patch=b"diff --git a/x b/x\n", facts_over=None, verify=None, bundle=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    facts = {"nonce": NONCE, "start_sha": START, "patch_sha256": hashlib.sha256(patch).hexdigest(),
             "exit_code": 0, "timed_out": False}
    facts.update(facts_over or {})
    (directory / "facts.json").write_text(json.dumps(facts))
    (directory / "verify.json").write_text(json.dumps(verify if verify is not None else {
        "status": "ok", "commands": [{"cmd": "true", "exit": 0, "tail": "", "phase": "verify"}]}))
    (directory / "bundle.tar").write_bytes(bundle if bundle is not None else make_bundle({"lane.patch": patch, "raw.log": b"log"}))


class StubCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.stubdir = self.root / "stub"
        self.stubdir.mkdir()
        (self.stubdir / "gh.py").write_text(STUB)
        self.env_cmd = json.dumps([sys.executable, str(self.stubdir / "gh.py")])
        self.rules([GH_OK])
        self._saved = {k: os.environ.get(k) for k in ("MC_STUB_DIR", "MC_LANE_GH_CMD", "MC_LANE_POLL_S")}
        os.environ["MC_STUB_DIR"] = str(self.stubdir)
        os.environ["MC_LANE_GH_CMD"] = self.env_cmd
        os.environ["MC_LANE_POLL_S"] = "0.02"
        self.gh = remote.Gh()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.temp.cleanup()

    def rules(self, rules):
        (self.stubdir / "rules.json").write_text(json.dumps(rules))
        (self.stubdir / "state.json").unlink(missing_ok=True)

    def calls(self):
        p = self.stubdir / "calls.jsonl"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class BuildInputsTests(unittest.TestCase):
    def test_valid_inputs_are_strings_and_tool_is_normalised(self):
        inputs = remote.build_inputs(cfg(), **good_kwargs(tool="pi-delegate", model="runinfra/glm-5-3-flash"))
        self.assertEqual(inputs["tool"], "pi")
        self.assertTrue(all(isinstance(v, str) for v in inputs.values()))
        self.assertEqual(inputs["card"], "12")
        self.assertEqual(set(inputs), {"repo", "start_sha", "card", "lane", "attempt", "tool", "model", "nonce",
                                       "timeout_min", "brief_gz_b64", "config_b64"})

    def test_gate_needs_a_base_sha_and_nothing_else_may_carry_one(self):
        base = "2" * 40
        gate = remote.build_inputs(cfg(), **good_kwargs(lane="gate", base_sha=base))
        self.assertEqual((gate["lane"], gate["start_sha"], gate["base_sha"]), ("gate", START, base))
        plain = remote.build_inputs(cfg(), **good_kwargs())
        self.assertNotIn("base_sha", plain)
        for label, over in {"gate without base": dict(lane="gate"),
                            "gate base empty": dict(lane="gate", base_sha=""),
                            "gate base short": dict(lane="gate", base_sha="2" * 39),
                            "gate base uppercase": dict(lane="gate", base_sha="A" * 40),
                            "gate base shell": dict(lane="gate", base_sha="2" * 30 + "$(id)" + "2" * 5),
                            "build with a base": dict(lane="build", base_sha=base)}.items():
            with self.subTest(label):
                with self.assertRaises(remote.UsageError):
                    remote.build_inputs(cfg(), **good_kwargs(**over))

    def test_each_a4_rule_is_enforced(self):
        bad = {
            "nonce short": dict(nonce="a" * 31),
            "nonce uppercase": dict(nonce="A" * 32),
            "nonce shell": dict(nonce="a" * 20 + "$(id)" + "a" * 7),
            "sha short": dict(start_sha="1" * 39),
            "sha uppercase": dict(start_sha="A" * 40),
            "repo not allowlisted": dict(repo="evil/repo"),
            "tool not allowed": dict(tool="rm"),
            "lane not allowed": dict(lane="deploy"),
            "model not in tool list": dict(model="gpt-9"),
            "model chars": dict(model="x; curl evil", tool="pi-delegate"),
            "card text": dict(card="12; id"),
            "card zero": dict(card=0),
            "attempt text": dict(attempt="one"),
            "attempt big": dict(attempt=100),
            "timeout 4": dict(timeout_min=4),
            "timeout 121": dict(timeout_min=121),
            "timeout text": dict(timeout_min="soon"),
            "brief not base64": dict(brief_gz_b64="not base64!"),
            "brief empty": dict(brief_gz_b64=""),
            "config not base64": dict(config_b64="$(id)"),
        }
        for label, over in bad.items():
            with self.subTest(label):
                with self.assertRaises(remote.UsageError):
                    remote.build_inputs(cfg(), **good_kwargs(**over))

    def test_timeout_bounds_accepted(self):
        for t in (5, 120):
            remote.build_inputs(cfg(), **good_kwargs(timeout_min=t))

    def test_oversize_total_is_refused(self):
        big = "A" * 60000
        with self.assertRaises(remote.UsageError) as ctx:
            remote.build_inputs(cfg(), **good_kwargs(brief_gz_b64=big))
        self.assertEqual(ctx.exception.code, 64)

    def test_encode_brief_round_trips(self):
        import base64, gzip
        self.assertEqual(gzip.decompress(base64.b64decode(remote.encode_brief(b"hello"))), b"hello")


class DispatchTests(StubCase):
    def test_brief_never_on_argv_and_run_found_by_nonce(self):
        inputs = remote.build_inputs(cfg(), **good_kwargs())
        listing = lambda rows: {"out": json.dumps(rows)}
        wrong = {"databaseId": 5, "displayTitle": f"lane {OTHER_NONCE}", "createdAt": "2026-10-21T10:00:00Z"}
        right = {"databaseId": 77, "displayTitle": f"lane {NONCE}", "createdAt": "2026-10-21T10:00:05Z"}
        self.rules([GH_OK,
                    {"match": ["workflow run lane.yml"], "seq": [{"out": ""}]},
                    {"match": ["run", "list"], "seq": [listing([wrong]), listing([wrong, right])]}])
        run_id = remote.dispatch("example-org/mc-lane-runner", "lane.yml", inputs, self.gh, find_timeout_s=5, poll_s=0.01)
        self.assertEqual(run_id, 77)
        calls = self.calls()
        wf = next(c for c in calls if c["argv"][:2] == ["workflow", "run"])
        self.assertIn("--json", wf["argv"])
        self.assertEqual(json.loads(wf["stdin"]), inputs)
        for c in calls:
            joined = " ".join(c["argv"])
            self.assertNotIn(inputs["brief_gz_b64"], joined)
            self.assertNotIn(inputs["config_b64"], joined)
        lst = [c for c in calls if c["argv"][:2] == ["run", "list"]]
        self.assertEqual(len(lst), 2, "retried until the run showed up")
        self.assertIn("databaseId,displayTitle,createdAt", lst[0]["argv"])

    def test_run_not_found_is_internal(self):
        inputs = remote.build_inputs(cfg(), **good_kwargs())
        self.rules([GH_OK, {"match": ["workflow run lane.yml"], "seq": [{"out": ""}]},
                    {"match": ["run", "list"], "seq": [{"out": "[]"}]}])
        with self.assertRaises(remote.InternalError):
            remote.dispatch("example-org/mc-lane-runner", "lane.yml", inputs, self.gh, find_timeout_s=0.1, poll_s=0.01)

    def test_bad_runner_repo_or_workflow_is_usage(self):
        inputs = remote.build_inputs(cfg(), **good_kwargs())
        with self.assertRaises(remote.UsageError):
            remote.dispatch("not a repo", "lane.yml", inputs, self.gh)
        with self.assertRaises(remote.UsageError):
            remote.dispatch("a/b", "../lane.yml", inputs, self.gh)
        self.assertEqual(self.calls(), [])


class WaitTests(StubCase):
    def test_completed_returns(self):
        self.rules([{"match": ["run", "view"], "seq": [{"out": '{"status":"in_progress","conclusion":""}'},
                                                      {"out": '{"status":"completed","conclusion":"success"}'}]}])
        res = remote.wait("a/b", 9, time.time() + 30, self.gh, poll_s=0.01)
        self.assertEqual(res, {"state": "completed", "conclusion": "success"})
        self.assertFalse([c for c in self.calls() if c["argv"][:2] == ["run", "cancel"]])

    def test_deadline_cancels_the_run(self):
        self.rules([{"match": ["run", "view"], "seq": [{"out": '{"status":"in_progress","conclusion":""}'}]},
                    {"match": ["run", "cancel"], "seq": [{"out": ""}]}])
        res = remote.wait("a/b", 9, time.time() + 0.15, self.gh, poll_s=0.02)
        self.assertEqual(res["state"], "timeout")
        cancels = [c for c in self.calls() if c["argv"][:3] == ["run", "cancel", "9"]]
        self.assertEqual(len(cancels), 1)

    def test_repeated_gh_failure_is_internal(self):
        self.rules([{"match": ["run", "view"], "seq": [{"rc": 1, "err": "boom"}]}])
        with self.assertRaises(remote.InternalError):
            remote.wait("a/b", 9, time.time() + 30, self.gh, poll_s=0.01)


class SafeExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.zip = self.root / "a.zip"
        self.dest = self.root / "out"
        self.dest.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def refuses(self, entries, **kw):
        make_zip(self.zip, entries)
        with self.assertRaises(remote.IsolationError):
            remote.safe_extract(self.zip, self.dest, **kw)
        self.assertEqual(list(self.dest.rglob("*")), [], "nothing may be extracted")
        self.assertFalse((self.root / "x").exists())

    GOOD = ("facts.json", b"{}", None)

    def test_traversal(self):
        self.refuses([self.GOOD, ("../x", b"pwn", None)])

    def test_nested_traversal(self):
        self.refuses([self.GOOD, ("a/../../x", b"pwn", None)])

    def test_absolute(self):
        self.refuses([self.GOOD, ("/etc/x", b"pwn", None)])

    def test_drive_path(self):
        self.refuses([self.GOOD, ("C:/x", b"pwn", None)])

    def test_backslash_tricks(self):
        self.refuses([self.GOOD, ("..\\x", b"pwn", None)])
        self.refuses([self.GOOD, ("a\\..\\x", b"pwn", None)])

    def test_symlink_entry(self):
        self.refuses([self.GOOD, ("verify.json", b"/etc/passwd", 0o120777)])

    def test_unexpected_name(self):
        self.refuses([self.GOOD, ("evil.sh", b"x", None)])
        self.refuses([self.GOOD, ("sub/facts.json", b"x", None)])

    def test_directory_entry(self):
        self.refuses([self.GOOD, ("x/", b"", None)])

    def test_duplicate_entry(self):
        self.refuses([self.GOOD, ("facts.json", b"{}", None)])

    def test_oversize_file(self):
        self.refuses([("facts.json", b"x" * 200, None)], per_file_caps={"facts.json": 100})

    def test_oversize_total(self):
        self.refuses([("facts.json", b"x" * 80, None), ("verify.json", b"x" * 80, None)], total_cap=100)

    def test_not_a_zip(self):
        self.zip.write_bytes(b"this is not a zip")
        with self.assertRaises(remote.IsolationError):
            remote.safe_extract(self.zip, self.dest)

    def test_good_zip_extracts_the_three_names(self):
        make_zip(self.zip, [("bundle.tar", b"t", None), ("verify.json", b"{}", None), ("facts.json", b"{}", 0o100644)])
        names = remote.safe_extract(self.zip, self.dest)
        self.assertEqual(sorted(names), ["bundle.tar", "facts.json", "verify.json"])
        self.assertEqual((self.dest / "bundle.tar").read_bytes(), b"t")


class DownloadTests(StubCase):
    def listing(self, **over):
        art = {"id": 4242, "name": f"mc-lane-{NONCE}", "expired": False}
        art.update(over)
        return {"match": ["actions/runs/77/artifacts"], "seq": [{"out": json.dumps({"artifacts": [art]})}]}

    def zip_rule(self, path):
        return {"match": ["actions/artifacts/4242/zip"], "seq": [{"out_file": str(path)}]}

    def test_download_uses_api_zip_and_extracts(self):
        zpath = self.root / "good.zip"
        make_zip(zpath, [("bundle.tar", b"t", None), ("verify.json", b"{}", None), ("facts.json", b"{}", None)])
        self.rules([GH_OK, self.listing(), self.zip_rule(zpath)])
        dest = remote.download("example-org/mc-lane-runner", 77, NONCE, self.gh)
        try:
            self.assertEqual((dest / "bundle.tar").read_bytes(), b"t")
        finally:
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
        argvs = [" ".join(c["argv"]) for c in self.calls()]
        self.assertTrue(any("repos/example-org/mc-lane-runner/actions/artifacts/4242/zip" in a for a in argvs))
        self.assertFalse(any(a.startswith("run download") for a in argvs))

    def test_exec_download_takes_only_the_exec_names(self):
        import shutil
        good, bad = self.root / "good.zip", self.root / "bad.zip"
        make_zip(good, [("exec.log", b"out", None), ("exec.json", b'{"rc":0}', None)])
        make_zip(bad, [("exec.log", b"out", None), ("lane.patch", b"x", None)])
        self.rules([GH_OK, self.listing(), self.zip_rule(good)])
        dest = remote.download("example-org/mc-lane-runner", 77, NONCE, self.gh, lane="exec")
        try:
            self.assertEqual(sorted(p.name for p in dest.iterdir()), ["exec.json", "exec.log"])
        finally:
            shutil.rmtree(dest, ignore_errors=True)
        self.rules([GH_OK, self.listing(), self.zip_rule(bad)])
        with self.assertRaises(remote.IsolationError):
            remote.download("example-org/mc-lane-runner", 77, NONCE, self.gh, lane="exec")
        self.rules([GH_OK, self.listing(), self.zip_rule(good)])
        with self.assertRaises(remote.IsolationError):                  # without lane="exec" the exec names are unknown
            remote.download("example-org/mc-lane-runner", 77, NONCE, self.gh)

    def test_malicious_zip_is_refused_and_leaves_nothing(self):
        zpath = self.root / "bad.zip"
        make_zip(zpath, [("facts.json", b"{}", None), ("../x", b"pwn", None)])
        self.rules([GH_OK, self.listing(), self.zip_rule(zpath)])
        out = self.root / "dl"
        with self.assertRaises(remote.IsolationError):
            remote.download("example-org/mc-lane-runner", 77, NONCE, self.gh, out_dir=out)
        self.assertFalse(out.exists())

    def test_old_gh_is_refused_before_any_download(self):
        self.rules([{"match": ["--version"], "seq": [{"out": "gh version 2.63.0 (2025-01-01)\n"}]}, self.listing()])
        with self.assertRaises(remote.InternalError):
            remote.download("example-org/mc-lane-runner", 77, NONCE, self.gh)
        self.assertEqual([c["argv"] for c in self.calls()], [["--version"]])

    def test_gh_version_boundaries(self):
        self.assertEqual(remote.parse_gh_version("gh version 2.63.1 (x)"), (2, 63, 1))
        self.assertIsNone(remote.parse_gh_version("not gh"))
        for text, ok in (("2.63.1", True), ("2.63.0", False), ("2.100.0", True), ("1.99.9", False), ("3.0.0", True)):
            self.rules([{"match": ["--version"], "seq": [{"out": f"gh version {text} (d)\n"}]}])
            if ok:
                remote.check_gh_version(self.gh)
            else:
                with self.assertRaises(remote.InternalError):
                    remote.check_gh_version(self.gh)

    def test_missing_expired_or_ambiguous_artifact(self):
        for rule in (self.listing(name="other"), self.listing(expired=True)):
            self.rules([GH_OK, rule])
            with self.assertRaises(remote.InternalError):
                remote.download("example-org/mc-lane-runner", 77, NONCE, self.gh)


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name) / "art"

    def tearDown(self):
        self.temp.cleanup()

    def fresh(self, name):
        d = Path(self.temp.name) / name
        return d

    def test_good_artifact(self):
        write_artifact(self.dir)
        res = remote.validate(self.dir, NONCE, START)
        self.assertEqual(res["nonce"], NONCE)
        self.assertEqual(Path(res["patch_path"]).read_bytes(), b"diff --git a/x b/x\n")
        self.assertEqual(res["verify"]["status"], "ok")
        self.assertTrue(res["verify_ok"])

    def test_nonce_mismatch(self):
        write_artifact(self.dir, facts_over={"nonce": OTHER_NONCE})
        with self.assertRaises(remote.IsolationError):
            remote.validate(self.dir, NONCE, START)

    def test_start_sha_mismatch(self):
        write_artifact(self.dir, facts_over={"start_sha": "2" * 40})
        with self.assertRaises(remote.IsolationError):
            remote.validate(self.dir, NONCE, START)

    def test_patch_sha_mismatch(self):
        write_artifact(self.dir, facts_over={"patch_sha256": "0" * 64})
        with self.assertRaises(remote.IsolationError):
            remote.validate(self.dir, NONCE, START)

    def test_patch_swapped_after_hash(self):
        write_artifact(self.dir, bundle=make_bundle({"lane.patch": b"different patch"}))
        with self.assertRaises(remote.IsolationError):
            remote.validate(self.dir, NONCE, START)

    def test_verify_schema(self):
        ok_cmd = {"cmd": "t", "exit": 0, "tail": "", "phase": "verify"}
        for bad in ({}, {"status": "yes", "commands": []}, {"status": "ok"}, {"ok": True, "commands": []},
                    {"status": "ok", "commands": [dict(ok_cmd, cmd=1)]},
                    {"status": "ok", "commands": [dict(ok_cmd, exit=True)]},
                    {"status": "ok", "commands": [{"cmd": "t", "exit_code": 0}]},       # the old shape
                    {"status": "ok", "commands": [dict(ok_cmd, tail=5)]},
                    {"status": "ok", "commands": []},                                     # zero commands is never ok
                    {"status": "ok", "commands": [dict(ok_cmd, exit=1)]},                 # ok but a command failed
                    {"status": "ok", "commands": [dict(ok_cmd, phase="setup")]},          # no verify-phase command
                    []):
            with self.subTest(bad=bad):
                d = self.fresh("v%d" % abs(hash(json.dumps(bad))))
                write_artifact(d, verify=bad)
                with self.assertRaises(remote.IsolationError):
                    remote.validate(d, NONCE, START)

    def test_verify_failure_statuses_are_valid_not_errors(self):
        for status in ("verify_failed", "apply_failed", "timeout", "base_broken", "merge_conflict"):
            d = self.fresh("st-" + status)
            write_artifact(d, verify={"status": status, "commands": [{"cmd": "t", "exit": 1, "tail": "x"}]})
            res = remote.validate(d, NONCE, START)
            self.assertFalse(res["verify_ok"], status)

    def test_host_verify_schema_inherited_failures_are_status_inherited(self):
        passing = {"cmd": "lint", "exit": 0, "tail": "", "phase": "verify"}
        inherited = {"cmd": "pytest", "exit": 1, "tail": "x", "phase": "verify", "inherited": True}
        base = {"status": "verify_failed", "commands": [{"cmd": "pytest", "exit": 1, "tail": "x", "phase": "verify"}]}
        good = {"status": "inherited", "commands": [{"cmd": "make", "exit": 0, "tail": "", "phase": "setup"}, passing, inherited],
                "notes": ["inherited from base: pytest"], "base": base}
        d = self.fresh("inh-ok")
        write_artifact(d, verify=good)
        res = remote.validate(d, NONCE, START)
        self.assertTrue(res["verify_ok"])
        d = self.fresh("inh-only")
        write_artifact(d, verify={"status": "inherited", "commands": [inherited]})      # one command ran and it is inherited
        self.assertTrue(remote.validate(d, NONCE, START)["verify_ok"])
        for name, doc in (("ok-with-inherited", {"status": "ok", "commands": [passing, inherited]}),      # M6: never a plain ok
                          ("inherited-none-failed", {"status": "inherited", "commands": [passing]}),
                          ("inherited-new-failure", {"status": "inherited", "commands": [inherited, dict(inherited, cmd="x", inherited=False)]}),
                          ("inherited-no-verify-cmd", {"status": "inherited", "commands": [dict(inherited, phase="setup")]})):
            d = self.fresh(name)
            write_artifact(d, verify=doc)
            with self.assertRaises(remote.IsolationError, msg=name):
                remote.validate(d, NONCE, START)

    def test_host_verify_schema_refuses_unproven_failures(self):
        failing = {"cmd": "pytest", "exit": 1, "tail": "x", "phase": "verify"}
        passing = {"cmd": "lint", "exit": 0, "tail": "", "phase": "verify"}
        for bad in ({"status": "ok", "commands": [passing, dict(failing, inherited=False)]},
                    {"status": "ok", "commands": [passing, failing]},                           # no inherited key
                    {"status": "ok", "commands": [passing, dict(failing, phase="setup", inherited=True)]},   # setup never inherits
                    {"status": "ok", "commands": [passing, dict(failing, phase="prepare", inherited=True)]},
                    {"status": "ok", "commands": [passing, dict(failing, inherited="yes")]},
                    {"status": "ok", "commands": [dict(passing, phase="setup"), dict(failing, phase="setup", inherited=True)]},
                    {"status": "ok", "commands": [passing], "notes": "inherited"},
                    {"status": "ok", "commands": [passing], "notes": [1]},
                    {"status": "verify_failed", "commands": [failing], "base": []},
                    {"status": "verify_failed", "commands": [failing], "base": {"commands": [{"cmd": 1}]}},
                    {"status": "base_broken", "commands": [failing], "notes": {"a": 1}}):
            with self.subTest(bad=bad):
                d = self.fresh("hv%d" % abs(hash(json.dumps(bad))))
                write_artifact(d, verify=bad)
                with self.assertRaises(remote.IsolationError):
                    remote.validate(d, NONCE, START)

    def test_harness_files_are_a_list_of_strings(self):
        d = self.fresh("hf-ok")
        write_artifact(d, facts_over={"harness_files": ["pkg/conftest.py", "package.json"]})
        self.assertEqual(remote.validate(d, NONCE, START)["harness_files"], ["pkg/conftest.py", "package.json"])
        d = self.fresh("hf-missing")
        write_artifact(d)
        self.assertEqual(remote.validate(d, NONCE, START)["harness_files"], [])
        for bad in ("conftest.py", [1], {"a": "b"}, None):
            with self.subTest(bad=bad):
                d = self.fresh("hf%d" % abs(hash(json.dumps(bad))))
                write_artifact(d, facts_over={"harness_files": bad})
                with self.assertRaises(remote.IsolationError):
                    remote.validate(d, NONCE, START)

    def test_facts_shape(self):
        for over in ({"patch_sha256": 5}, {"exit_code": "0"}, {"timed_out": "no"}):
            with self.subTest(over=over):
                d = self.fresh("f%d" % abs(hash(json.dumps(over))))
                write_artifact(d, facts_over=over)
                with self.assertRaises(remote.IsolationError):
                    remote.validate(d, NONCE, START)

    def test_missing_files(self):
        write_artifact(self.dir)
        (self.dir / "verify.json").unlink()
        with self.assertRaises(remote.IsolationError):
            remote.validate(self.dir, NONCE, START)

    def test_bundle_with_symlink_or_unexpected_member(self):
        patch = b"p"
        write_artifact(self.fresh("s"), patch=patch, bundle=make_bundle({"lane.patch": patch}, symlink="session/x.jsonl"))
        with self.assertRaises(remote.IsolationError):
            remote.validate(self.fresh("s"), NONCE, START)
        write_artifact(self.fresh("u"), patch=patch, bundle=make_bundle({"lane.patch": patch}, extra={"../evil": b"x"}))
        with self.assertRaises(remote.IsolationError):
            remote.validate(self.fresh("u"), NONCE, START)
        write_artifact(self.fresh("n"), patch=patch, bundle=make_bundle({"raw.log": b"no patch here"}))
        with self.assertRaises(remote.IsolationError):
            remote.validate(self.fresh("n"), NONCE, START)

    def test_cli_exit_codes(self):
        write_artifact(self.dir)
        run = lambda *a: subprocess.run([sys.executable, str(SCRIPT), "validate", *a], capture_output=True, text=True)
        ok = run("--dir", str(self.dir), "--nonce", NONCE, "--start-sha", START)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        bad_dir = self.fresh("cli-bad")
        write_artifact(bad_dir, facts_over={"nonce": OTHER_NONCE})
        bad = run("--dir", str(bad_dir), "--nonce", NONCE, "--start-sha", START)
        self.assertEqual(bad.returncode, 4)
        usage = run("--dir", str(self.dir), "--nonce", "short", "--start-sha", START)
        self.assertEqual(usage.returncode, 64)
        self.assertEqual(subprocess.run([sys.executable, str(SCRIPT)], capture_output=True).returncode, 64)


class PayloadTests(unittest.TestCase):
    BOARD = {"verify_commands": ["pytest -q"], "pilot": {
        "verify_timeout_min": 20, "verify_setup": ["pip install -e ."],
        "sandbox": {"runner_repo": "example-org/mc-lane-runner", "repos": ["Shahat87/ams-mission-kit"],
                    "image": "mc-lane@sha256:" + "a" * 64, "proxy_image": "p@sha256:" + "b" * 64,
                    "limits": {"memory": "4g"}, "env_deny": ["SAP_LIVE_TESTS"], "verify_network": True},
        "tools": [{"id": "codex", "auth": {"kind": "file"}, "egress": ["chatgpt.com"]},
                  {"id": "pi-delegate", "auth": {"kind": "env", "keys": ["RUNINFRA_GATEWAY_KEY"]},
                   "egress": ["api.runinfra.ai"], "models": ["runinfra/glm-5-3-flash"]}]}}

    def test_payload_is_the_docker_modules_config(self):
        c = remote.remote_cfg_from_board(self.BOARD)
        p = remote.runner_payload(c, "pi-delegate")
        self.assertEqual(p["image"], "mc-lane@sha256:" + "a" * 64)
        self.assertEqual(p["verify_commands"], ["pytest -q"])
        self.assertEqual(p["verify_timeout_min"], 20)
        self.assertEqual([t["id"] for t in p["tools"]], ["pi"])            # normalised, and only the dispatched tool
        self.assertEqual(p["tools"][0]["egress"], ["api.runinfra.ai"])
        self.assertNotIn("repos", p)

    def test_tool_ids_are_compared_normalised(self):
        c = remote.remote_cfg_from_board(self.BOARD)
        c.update(repos=["Shahat87/ams-mission-kit"])
        for tool in ("pi", "pi-delegate"):
            inputs = remote.build_inputs(c, **good_kwargs(tool=tool, model="runinfra/glm-5-3-flash"))
            self.assertEqual(inputs["tool"], "pi")
        with self.assertRaises(remote.UsageError):
            remote.build_inputs(c, **good_kwargs(tool="pi", model="other/model"))

    def test_bundle_may_carry_extracts_own_facts_json(self):
        d = Path(tempfile.mkdtemp())
        try:
            patch = b"diff --git a/x b/x\n"
            write_artifact(d, patch=patch, bundle=make_bundle({"lane.patch": patch, "facts.json": b"{}"}))
            remote.validate(d, NONCE, START)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class CleanTests(unittest.TestCase):
    def test_control_characters_are_stripped(self):
        dirty = "ok\x1b[31m red\x00 ::add-mask::x\r\nline\x07\x9f end"
        out = remote.clean(dirty)
        self.assertFalse(re.search(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", out), repr(out))
        self.assertIn("ok", out)


# ---------------------------------------------------------------- lane.yml scanners

def run_block_dollar_lines(text):
    """Lines holding a GitHub expression inside the body of a `run:` step."""
    lines = text.splitlines()
    bad, i = [], 0
    while i < len(lines):
        m = re.match(r"^(\s*)(-\s+)?run:\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        key_col = len(m.group(1)) + (len(m.group(2)) if m.group(2) else 0)
        rest = m.group(3)
        if re.match(r"^[|>][+-]?\d*\s*(#.*)?$", rest):
            i += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > key_col):
                if "${{" in lines[i]:
                    bad.append((i + 1, lines[i].strip()))
                i += 1
        else:
            if "${{" in rest:
                bad.append((i + 1, lines[i].strip()))
            i += 1
    return bad


def unpinned_uses(text):
    bad = []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^\s*(?:-\s+)?uses:\s*(\S+)", line)
        if m and not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", m.group(1)):
            bad.append((n, line.strip()))
    return bad


def secret_problems(text):
    """Secrets only as `NAME: ${{ secrets.NAME }}` env lines; non-fetch secrets only in a tool-guarded step that
    never runs for lane=gate (the gate dispatch runs no tool, so no tool secret may be mapped)."""
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        if "secrets." in line and not re.match(r"^\s+[A-Z][A-Z0-9_]*: \$\{\{ secrets\.[A-Z][A-Z0-9_]* \}\}\s*$", line):
            problems.append((n, line.strip()))
    for step in re.split(r"\n      - ", text):
        names = re.findall(r"secrets\.([A-Z0-9_]+)", step)
        if names and names != ["LANE_REPO_TOKEN"] and not re.search(r"^\s+if: inputs\.tool == '[a-z]+' && inputs\.lane != 'gate'( && inputs\.lane != 'exec')?\s*$", step, re.M):
            problems.append((0, "secret outside a per-tool if: " + ",".join(names)))
    return problems


class ExecInputsTests(unittest.TestCase):
    def exec_cfg(self):
        return cfg(lanes=["build", "qa", "review", "gate", "exec"])

    def test_exec_is_a_default_lane(self):
        self.assertIn("exec", remote.DEFAULT_LANES)

    def test_a_valid_argv_is_carried_as_exact_compact_json(self):
        argv = ["pytest", "-q", "tests/a b.py"]
        inputs = remote.build_inputs(self.exec_cfg(), **good_kwargs(lane="exec", command=argv))
        self.assertEqual(inputs["command_json"], '["pytest","-q","tests/a b.py"]')
        self.assertEqual(inputs["lane"], "exec")
        self.assertEqual(remote.validate_exec_command(argv), inputs["command_json"])

    def test_only_exec_carries_command_json(self):
        self.assertNotIn("command_json", remote.build_inputs(self.exec_cfg(), **good_kwargs()))
        for lane in ("build", "gate"):
            kw = good_kwargs(lane=lane, command=["true"])
            if lane == "gate":
                kw["base_sha"] = "2" * 40
            with self.subTest(lane):
                with self.assertRaises(remote.UsageError):
                    remote.build_inputs(self.exec_cfg(), **kw)

    def test_exec_without_a_command_is_refused(self):
        with self.assertRaises(remote.UsageError):
            remote.build_inputs(self.exec_cfg(), **good_kwargs(lane="exec"))

    def test_bad_commands_are_refused(self):
        bad = {
            "shell string": "npm test",
            "empty list": [],
            "dollar paren": ["echo", "$(id)"],
            "backtick": ["echo", "`id`"],
            "non-ascii": ["echo", "caf" + chr(0xe9)],
            "control char": ["echo", "a" + chr(7)],
            "newline": ["echo", "a" + chr(10) + "b"],
            "del char": ["echo", "a" + chr(0x7f)],
            "empty element": ["echo", ""],
            "non-string element": ["echo", 1],
            "tuple": ("echo", "x"),
            "over 4096 bytes": ["echo", "x" * 4100],
        }
        for label, cmd in bad.items():
            with self.subTest(label):
                with self.assertRaises(remote.UsageError):
                    remote.build_inputs(self.exec_cfg(), **good_kwargs(lane="exec", command=cmd))
                with self.assertRaises(remote.UsageError):
                    remote.validate_exec_command(cmd)

    def test_a_shell_string_gets_the_argv_list_message(self):
        with self.assertRaises(remote.UsageError) as ctx:
            remote.validate_exec_command("npm test")
        self.assertIn("argv list, not a shell string", str(ctx.exception))

    def test_the_4096_byte_boundary(self):
        fits = ["x" * (4096 - len('["' + '"]'))]
        self.assertEqual(len(remote.validate_exec_command(fits).encode()), 4096)
        with self.assertRaises(remote.UsageError):
            remote.validate_exec_command(["x" * (4096 - len('["' + '"]') + 1)])


class CheckOnOriginTests(StubCase):
    SHA = "3" * 40
    TIP = "4" * 40

    def compare(self, status, tip=None):
        path = f"repos/o/n/compare/{self.SHA}...{tip or self.TIP}"
        return {"match": [path], "seq": [{"out": json.dumps({"status": status})}]}

    def check(self, tips, status="ahead"):
        seq = iter(tips)
        self.rules([GH_OK, self.compare(status, tips[0])])
        remote.check_on_origin("/repo", "o/n", "issue-1-x", self.SHA, self.gh, lambda: next(seq))

    def test_identical_and_ahead_pass(self):
        for status in ("identical", "ahead"):
            with self.subTest(status):
                self.check([self.TIP, self.TIP], status)

    def test_behind_and_diverged_fail(self):
        for status in ("behind", "diverged", "weird"):
            with self.subTest(status):
                with self.assertRaises(remote.UsageError) as ctx:
                    self.check([self.TIP, self.TIP], status)
                self.assertIn("sha is not on origin/issue-1-x", str(ctx.exception))

    def test_a_missing_tip_fails(self):
        with self.assertRaises(remote.UsageError) as ctx:
            remote.check_on_origin("/repo", "o/n", "b", self.SHA, self.gh, lambda: None)
        self.assertIn("branch not on origin", str(ctx.exception))
        self.assertEqual(self.calls(), [])

    def test_the_compare_api_path_and_never_the_commits_api(self):
        self.check([self.TIP, self.TIP])
        argvs = [c["argv"] for c in self.calls() if c["argv"][:1] == ["api"]]
        self.assertEqual(argvs, [["api", f"repos/o/n/compare/{self.SHA}...{self.TIP}"]])
        self.assertFalse(any("/commits/" in " ".join(a) for a in argvs))

    def test_a_tip_that_moves_between_reads_fails(self):
        with self.assertRaises(remote.UsageError) as ctx:
            self.check([self.TIP, "5" * 40])
        self.assertIn("branch moved during check; re-run", str(ctx.exception))

    def test_a_missing_second_read_fails(self):
        with self.assertRaises(remote.UsageError):
            self.check([self.TIP, None])

    def test_bad_inputs_are_refused_before_any_call(self):
        for args in (("/r", "bad slug", "b", self.SHA), ("/r", "o/n", "b", "xyz"), ("/r", "o/n", "../b", self.SHA)):
            with self.subTest(args):
                with self.assertRaises(remote.UsageError):
                    remote.check_on_origin(*args, self.gh, lambda: self.TIP)
        self.assertEqual(self.calls(), [])


class ReadBranchTipTests(StubCase):
    """F10: the tip comes from the slug's own repository through the gh API, never from the local origin remote."""
    TIP = "4" * 40

    def read(self, rule, branch="issue-1-x"):
        self.rules([GH_OK, rule])
        return remote.read_branch_tip("o/n", branch, self.gh)

    def test_the_tip_is_object_sha_of_the_ref_api_for_the_slug(self):
        rule = {"match": ["repos/o/n/git/ref/heads/issue-1-x"], "seq": [{"out": json.dumps({"object": {"sha": self.TIP}})}]}
        self.assertEqual(self.read(rule), self.TIP)
        self.assertEqual([c["argv"] for c in self.calls() if c["argv"][:1] == ["api"]], [["api", "repos/o/n/git/ref/heads/issue-1-x"]])

    def test_a_branch_with_a_slash_keeps_its_path(self):
        rule = {"match": ["repos/o/n/git/ref/heads/feat/x"], "seq": [{"out": json.dumps({"object": {"sha": self.TIP}})}]}
        self.assertEqual(self.read(rule, "feat/x"), self.TIP)

    def test_a_404_means_the_branch_is_not_on_origin(self):
        rule = {"match": ["git/ref/heads/"], "seq": [{"rc": 1, "err": "gh: Not Found (HTTP 404)"}]}
        self.assertIsNone(self.read(rule))

    def test_any_other_failure_is_a_usage_error(self):
        for err in ("gh: Server Error (HTTP 500)", "gh: Forbidden (HTTP 403)", "network down"):
            with self.subTest(err):
                with self.assertRaises(remote.UsageError):
                    self.read({"match": ["git/ref/heads/"], "seq": [{"rc": 1, "err": err}]})

    def test_a_gh_that_cannot_run_is_a_usage_error(self):
        class Boom:
            def json(self, args, **kw):
                raise PermissionError("denied")
        with self.assertRaises(remote.UsageError):
            remote.read_branch_tip("o/n", "issue-1-x", Boom())

    def test_dot_parts_in_the_slug_are_refused(self):
        for slug in ("../x", "./x", "o/..", "o/.", "../..", "./."):
            with self.subTest(slug):
                self.assertIsNone(remote.REPO_RE.fullmatch(slug))
                with self.assertRaises(remote.UsageError):
                    remote.read_branch_tip(slug, "b", self.gh)
        for slug in ("o/n", ".o/n", "o/.n", "o/n.", "o/..n", "a.b/c-d"):
            with self.subTest(slug):
                self.assertTrue(remote.REPO_RE.fullmatch(slug))
        self.assertEqual(self.calls(), [])

    def test_a_malformed_answer_is_a_usage_error(self):
        for out in ("[]", json.dumps({"object": {}}), json.dumps({"object": {"sha": "xyz"}}), json.dumps({"object": "x"}), "{}"):
            with self.subTest(out):
                with self.assertRaises(remote.UsageError):
                    self.read({"match": ["git/ref/heads/"], "seq": [{"out": out}]})

    def test_bad_inputs_are_refused_before_any_call(self):
        for slug, branch in (("bad slug", "b"), ("o/n", "../b"), ("o/n", "-x")):
            with self.subTest((slug, branch)):
                with self.assertRaises(remote.UsageError):
                    remote.read_branch_tip(slug, branch, self.gh)
        self.assertEqual(self.calls(), [])


class ExecDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.zip = self.root / "a.zip"
        self.dest = self.root / "out"
        self.dest.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_log_and_json_are_accepted_with_the_exec_names(self):
        make_zip(self.zip, [("exec.log", b"out", None), ("exec.json", b'{"rc":0}', None)])
        names = remote.safe_extract(self.zip, self.dest, allowed=remote.EXEC_ARTIFACT_NAMES)
        self.assertEqual(sorted(names), ["exec.json", "exec.log"])

    def test_extra_files_are_refused(self):
        for extra in ("lane.patch", "x.sh", "bundle.tar", "verify.json"):
            with self.subTest(extra):
                make_zip(self.zip, [("exec.log", b"out", None), ("exec.json", b"{}", None), (extra, b"x", None)])
                with self.assertRaises(remote.IsolationError):
                    remote.safe_extract(self.zip, self.dest, allowed=remote.EXEC_ARTIFACT_NAMES)
                self.assertEqual(list(self.dest.iterdir()), [])

    def test_the_caps(self):
        self.assertEqual(remote.EXEC_ARTIFACT_NAMES, {"exec.log": 5_000_000, "exec.json": 4096})
        make_zip(self.zip, [("exec.json", b"x" * 4097, None)])
        with self.assertRaises(remote.IsolationError):
            remote.safe_extract(self.zip, self.dest, allowed=remote.EXEC_ARTIFACT_NAMES)

    def test_build_artifacts_still_refuse_the_exec_names(self):
        make_zip(self.zip, [("exec.log", b"out", None)])
        with self.assertRaises(remote.IsolationError):
            remote.safe_extract(self.zip, self.dest)


class LaneYmlTests(unittest.TestCase):
    text = LANE_YML.read_text(encoding="utf-8")

    def test_no_expression_inside_a_run_body(self):
        self.assertEqual(run_block_dollar_lines(self.text), [])

    def test_scanner_catches_injected_expressions(self):
        block = self.text.replace('echo "stage ok"', 'echo "${{ inputs.repo }}"')
        self.assertEqual(len(run_block_dollar_lines(block)), 1)
        inline = self.text.replace("      - name: Stage\n", "      - name: Stage\n        run: echo ${{ github.event.inputs.card }}\n", 1)
        self.assertEqual(len(run_block_dollar_lines(inline)), 1)
        dash = self.text + "      - run: echo ${{ secrets.X }}\n"
        self.assertTrue(run_block_dollar_lines(dash))

    def test_every_uses_is_pinned_to_a_full_sha(self):
        self.assertEqual(unpinned_uses(self.text), [])
        self.assertEqual(len(unpinned_uses(self.text.replace("11bd71901bbe5b1630ceea73d27597364c9af683", "v4"))), 1)
        self.assertGreaterEqual(len(re.findall(r"^\s*(?:-\s+)?uses:", self.text, re.M)), 2)

    def test_secrets_only_in_env_and_tool_guarded(self):
        self.assertEqual(secret_problems(self.text), [])
        leaked = self.text.replace('echo "stage ok"', 'echo ${{ secrets.OPENAI_API_KEY }}')
        self.assertTrue(secret_problems(leaked))

    JOB_IF = "if: github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
    GUARD_EXPECTED = [0, 1, 1, 1, 1, 1]

    @staticmethod
    def job_header(text):
        return text.split("\n  lane:\n", 1)[1].split("\n    steps:\n", 1)[0]

    @staticmethod
    def guard_step(text):
        after = text.split("\n  lane:\n", 1)[1].split("\n    steps:\n", 1)[1]
        return re.split(r"\n      - ", after)[0].removeprefix("      - ")

    @staticmethod
    def guard_body(step):
        body = step.split("        run: |\n", 1)[1]
        return "\n".join(line[10:] for line in body.splitlines()) + "\n"

    def guard_results(self, body):
        import shutil
        import subprocess
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is not installed")
        cases = [("refs/heads/main", "main"), ("refs/heads/feat", "main"), ("refs/tags/main", "main"),
                 ("refs/heads/mainx", "main"), ("refs/heads/main", ""), (None, "main")]
        out = []
        for ref, default in cases:
            env = {"PATH": os.environ.get("PATH", ""), "DEFAULT_BRANCH": default}
            if ref is not None:
                env["GITHUB_REF"] = ref
            out.append(subprocess.run([bash, "-c", body], env=env, capture_output=True, text=True).returncode)
        return out

    def test_the_job_header_binds_secrets_to_the_default_branch(self):
        header = self.job_header(self.text)
        self.assertIn("    environment: mc-lane\n", header + "\n")
        self.assertIn("    " + self.JOB_IF + "\n", header + "\n")
        self.assertNotIn("continue-on-error", header)
        self.assertIn("a dispatch runs the dispatched ref's copy of this file", self.text.split("\nname: mc-lane", 1)[0])
        try:
            import yaml
        except ImportError:
            return
        job = yaml.safe_load(self.text)["jobs"]["lane"]
        self.assertEqual(job["environment"], "mc-lane")
        self.assertEqual(job["if"], "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)")

    def test_the_first_step_is_the_default_branch_ref_guard(self):
        first = self.guard_step(self.text)
        self.assertTrue(first.startswith("name: Refuse to run off the default branch"), first[:60])
        env_block = re.search(r"\n        env:\n((?:          .*\n)+)", first + "\n").group(1)
        self.assertIn("DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}", env_block)
        self.assertIn('"$GITHUB_REF" != "refs/heads/$DEFAULT_BRANCH"', first)
        self.assertIn('-z "$DEFAULT_BRANCH"', first)
        self.assertIn("lane.yml runs only from the default branch", first)
        self.assertEqual(run_block_dollar_lines(first), [])
        self.assertNotRegex(first, r"(?m)^\s+if:")
        self.assertNotIn("continue-on-error", first)
        try:
            import yaml
        except ImportError:
            return
        step0 = yaml.safe_load(self.text)["jobs"]["lane"]["steps"][0]
        self.assertEqual(step0["name"], "Refuse to run off the default branch")
        self.assertEqual(step0["env"], {"DEFAULT_BRANCH": "${{ github.event.repository.default_branch }}"})
        self.assertNotIn("if", step0)
        self.assertNotIn("continue-on-error", step0)

    def test_the_guard_script_runs_and_refuses(self):
        body = self.guard_body(self.guard_step(self.text))
        self.assertEqual(self.guard_results(body), self.GUARD_EXPECTED)

    def test_the_guard_test_catches_a_broken_guard(self):
        body = self.guard_body(self.guard_step(self.text))
        self.assertIn("||", body)
        self.assertNotEqual(self.guard_results(body.replace("||", "&&")), self.GUARD_EXPECTED)

    def test_the_gate_lane_runs_no_tool_and_only_verifies(self):
        steps = re.split(r"\n      - ", self.text)
        by_name = {re.match(r"name: (.*)", st).group(1): st for st in steps if st.startswith("name: ")}
        for name in ("Run (codex)", "Run (grok)", "Run (pi)", "Run (fake)"):
            self.assertIn("&& inputs.lane != 'gate'", by_name[name], name)
        self.assertIn("if: inputs.lane != 'gate'", by_name["Extract and verify"])
        self.assertIn("if: inputs.lane != 'gate'", by_name["Stage"])             # gate-verify stages the merge itself
        gate = by_name["Gate verify"]
        self.assertIn("if: inputs.lane == 'gate'", gate)
        self.assertNotIn("secrets.", gate)
        # one implementation of the gate verify: the lane-docker subcommand, not logic copied into the workflow
        self.assertRegex(gate, r'super-board-lane-docker\.py gate-verify --nonce "\$MCL_NONCE"')
        self.assertIn('--merge-sha "$MCL_BASE"', gate)
        self.assertNotIn("verify_pass", gate)
        self.assertNotIn("importlib", gate)

    def test_the_gate_seed_holds_head_and_base_with_history(self):
        fetch = next(st for st in re.split(r"\n      - ", self.text) if st.startswith("name: Fetch the start commit"))
        self.assertIn('fetch_args=("$MCL_START" "$MCL_BASE")', fetch)             # no --depth: a merge needs the merge base
        self.assertIn('fetch_args=(--depth 1 "$MCL_START")', fetch)               # every other lane stays shallow
        self.assertIn('update-ref refs/mc/base "$MCL_BASE"', fetch)
        self.assertIn('update-ref refs/mc/start "$MCL_START"', fetch)

    def run_validate(self, **over):
        """Run the first step's python with a fake event and runner environment; returns (rc, output, github_env text)."""
        import subprocess
        import tempfile
        step = re.search(r"- name: Validate inputs.*?<<'PY'\n(.*?)\n          PY\n", self.text, re.S).group(1)
        code = "\n".join(line[10:] for line in step.splitlines())
        nonce = "a" * 32
        brief = remote.encode_brief(b"gate verify")
        inputs = {"repo": "Shahat87/ams-mission-kit", "start_sha": START, "card": "1", "lane": "gate", "attempt": "1",
                  "tool": "fake", "model": "m", "nonce": nonce, "timeout_min": "10", "brief_gz_b64": brief,
                  "config_b64": remote.encode_config({"image": "x"}), "base_sha": "2" * 40}
        inputs.update(over)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            event, genv = d / "event.json", d / "github_env"
            event.write_text(json.dumps({"inputs": inputs}))
            genv.write_text("")
            env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                   "GITHUB_EVENT_PATH": str(event), "GITHUB_ENV": str(genv), "RUNNER_TEMP": str(d / "tmp")}
            for k in ("repo", "start_sha", "base_sha", "card", "lane", "attempt", "tool", "model", "nonce", "timeout_min"):
                env["INPUT_" + k.upper()] = inputs.get(k, "")
            r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
            return r.returncode, r.stdout + r.stderr, genv.read_text()

    def test_the_validation_step_requires_base_sha_for_the_gate_only(self):
        rc, out, genv = self.run_validate()
        self.assertEqual(rc, 0, out)
        self.assertIn("MCL_BASE=" + "2" * 40, genv)
        for bad in ("", "2" * 39, "G" * 40, "2" * 40 + "x"):
            rc, out, _ = self.run_validate(base_sha=bad)
            self.assertEqual(rc, 1, bad)
            self.assertIn("base_sha", out)
        rc, out, genv = self.run_validate(lane="build", base_sha="")
        self.assertEqual(rc, 0, out)
        self.assertIn("MCL_BASE=\n", genv)
        rc, out, _ = self.run_validate(lane="build", base_sha="2" * 40)
        self.assertEqual(rc, 1)
        self.assertIn("base_sha", out)

    def test_the_exec_lane_runs_no_tool_and_only_the_exec_step(self):
        steps = re.split(r"\n      - ", self.text)
        by_name = {re.match(r"name: (.*)", st).group(1): st for st in steps if st.startswith("name: ")}
        for name in ("Run (codex)", "Run (grok)", "Run (pi)", "Run (fake)"):
            self.assertIn("&& inputs.lane != 'exec'", by_name[name], name)
        for name in ("Stage", "Extract and verify"):
            self.assertIn("if: inputs.lane != 'gate' && inputs.lane != 'exec'", by_name[name], name)
        ex = by_name["Exec"]
        self.assertIn("if: inputs.lane == 'exec'", ex)
        self.assertNotIn("secrets.", ex)
        self.assertRegex(ex, r'super-board-lane-docker\.py exec-sandbox --nonce "\$MCL_NONCE"')
        self.assertIn('--command-file "$MCL_DIR/in/command.json"', ex)
        self.assertIn('--seed "$MCL_DIR/seed" --start-sha "$MCL_START"', ex)
        self.assertNotIn(" sh -c", ex)
        self.assertRegex(self.text, r"      command_json:\n(        .*\n)*?        required: false\n        default: ''\n        type: string\n")

    def exec_validate(self, command_json, **over):
        return self.run_validate(lane="exec", base_sha="", command_json=command_json, **over)

    def test_the_validation_step_rechecks_the_exec_command(self):
        rc, out, genv = self.exec_validate(json.dumps(["pytest", "-q", "a b"]))
        self.assertEqual(rc, 0, out)
        self.assertIn("MCL_LANE=exec", genv)
        bad = ["npm test", "[]", "{}", "not json", json.dumps(["a", ""]), json.dumps(["a", 1]), json.dumps(["a$(id)"]),
               json.dumps(["a" + chr(96) + "id" + chr(96)]), json.dumps(["caf" + chr(0xe9)]), json.dumps(["a" + chr(7)]),
               json.dumps(["x" * 4100]), ""]
        for cmd in bad:
            with self.subTest(cmd[:30]):
                rc, out, _ = self.exec_validate(cmd)
                self.assertEqual(rc, 1)
                self.assertIn("command_json", out)

    def test_only_exec_may_carry_a_command(self):
        for lane, base in (("build", ""), ("gate", "2" * 40)):
            rc, out, _ = self.run_validate(lane=lane, base_sha=base, command_json=json.dumps(["true"]))
            self.assertEqual(rc, 1, lane)
            self.assertIn("command_json", out)
        rc, out, _ = self.run_validate(lane="build", base_sha="")
        self.assertEqual(rc, 0, out)

    def test_the_workflow_declares_an_optional_base_sha_input(self):
        self.assertRegex(self.text, r"      base_sha:\n(        .*\n)*?        required: false\n        default: ''\n        type: string\n")
        self.assertIn("INPUT_BASE_SHA: ${{ inputs.base_sha }}", self.text)

    def test_required_hardening_is_present(self):
        t = self.text
        self.assertIn("run-name: lane ${{ inputs.nonce }}", t)
        self.assertIn("runs-on: [self-hosted, mc-lane]", t)
        self.assertRegex(t, r"permissions:\n  contents: read")
        self.assertIn("persist-credentials: false", t)
        self.assertIn("retention-days: 7", t)
        self.assertIn("name: mc-lane-${{ env.MCL_NONCE }}", t)
        self.assertIn("path: ${{ env.MCL_DIR }}/x", t)
        self.assertIn("GIT_CONFIG_COUNT=1", t)
        self.assertIn("http.https://github.com/.extraheader", t)
        self.assertRegex(t, r"if: always\(\) && env\.MCL_NONCE != ''\n        shell: bash\n        run: \|\n          \"\$MC_PY\" scripts/super-board-lane-docker.py cleanup")
        self.assertEqual(sorted(re.findall(r"inputs\.tool == '(\w+)'", t)), ["codex", "fake", "grok", "pi"])
        for pattern in ("[a-f0-9]{32}", "[0-9a-f]{40}"):
            self.assertIn(pattern, t)


class DelegateLaneTests(unittest.TestCase):
    """Plan 1c T8: lane=delegate is not a card, so it alone may carry card=0."""
    def delegate_cfg(self):
        return cfg(lanes=["build", "qa", "review", "gate", "exec", "delegate"])

    def test_delegate_is_a_default_lane(self):
        self.assertIn("delegate", remote.DEFAULT_LANES)

    def test_card_zero_is_accepted_only_for_delegate(self):
        inputs = remote.build_inputs(self.delegate_cfg(), **good_kwargs(lane="delegate", card=0))
        self.assertEqual((inputs["lane"], inputs["card"]), ("delegate", "0"))
        for lane in ("build", "qa", "review", "gate", "exec"):
            kw = good_kwargs(lane=lane, card=0)
            if lane == "gate":
                kw["base_sha"] = "2" * 40
            if lane == "exec":
                kw["command"] = ["true"]
            with self.subTest(lane):
                with self.assertRaises(remote.UsageError):
                    remote.build_inputs(self.delegate_cfg(), **kw)

    def test_delegate_still_refuses_a_bad_card(self):
        for card in ("x", -1, "1; id", 10_000_001):
            with self.subTest(card):
                with self.assertRaises(remote.UsageError):
                    remote.build_inputs(self.delegate_cfg(), **good_kwargs(lane="delegate", card=card))

    def test_the_board_config_gets_delegate_without_a_timeouts_entry(self):
        board = {"pilot": {"sandbox": {"runner_repo": "o/r"}, "tools": [{"id": "codex"}], "timeouts_min": {"build": 30}}}
        self.assertIn("delegate", remote.remote_cfg_from_board(board)["lanes"])

    def test_lane_yml_accepts_delegate_and_its_card_zero(self):
        text = LANE_YML.read_text(encoding="utf-8")
        m = re.search(r"LANES = \{([^}]*)\}", text)
        self.assertIn('"delegate"', m.group(1))
        self.assertIn('need("CARD", r"[0-9]{1,8}")', text)        # the card regex takes 0
        for line in re.findall(r"if: (inputs\.[^\n]*)", text):    # Stage, Run and Extract skip only gate and exec, never delegate
            self.assertNotIn("'delegate'", line)


class GhStdinTests(unittest.TestCase):
    def test_gh_never_inherits_the_callers_stdin(self):
        # A gh (or test stub) that reads stdin must get EOF at once, not block on the caller's pipe until the timeout.
        r, w = os.pipe()  # an open pipe nobody writes to or closes, like a CI or WSL launcher's stdin
        saved = os.dup(0)
        try:
            os.dup2(r, 0)
            gh = remote.Gh([sys.executable, "-c", "import sys; sys.stdout.write(str(len(sys.stdin.buffer.read())))"])
            t0 = time.time()
            out = gh.run(["x"], timeout=20)
            self.assertEqual(out.decode(), "0")
            self.assertLess(time.time() - t0, 10)
        finally:
            os.dup2(saved, 0)
            for fd in (r, w, saved):
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
