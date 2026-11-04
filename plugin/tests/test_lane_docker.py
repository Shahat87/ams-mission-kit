#!/usr/bin/env python3
"""lane-docker: argv builders, env file, result builder, tar validation, containment (no real docker)."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lane_docker", ROOT / "scripts/super-board-lane-docker.py")
L = importlib.util.module_from_spec(spec)
spec.loader.exec_module(L)
STUB = ROOT / "tests/fixtures/docker_stub.py"

NONCE = "0123456789abcdef0123456789abcdef"
DIGEST = "mc-lane@sha256:" + "a" * 64
SHA = "b" * 40
CFG = {
    "where": "lenovo",
    "limits": {"memory": "4g", "cpus": "4", "pids": 1024},
    "verify_network": False,
    "env_deny": ["AMS_OPERATOR_EXPECTED", "AMS_OPERATOR_DATA_ROOT", "SAP_LIVE_TESTS"],
    "tools": [
        {"id": "codex", "auth": {"kind": "file"}, "egress": ["chatgpt.com"]},
        {"id": "codex-key", "auth": {"kind": "env", "keys": ["OPENAI_API_KEY"]}},
        {"id": "pi", "auth": {"kind": "env", "keys": ["RUNINFRA_GATEWAY_KEY"]}},
        {"id": "fake"},
    ],
}


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


class RunArgvTests(unittest.TestCase):
    def setUp(self):
        self.a = L.run_argv(CFG, NONCE, "/tmp/x.env", DIGEST, "pi")

    def test_name(self):
        self.assertEqual(flag_value(self.a, "--name"), f"mcl-{NONCE}-run")

    def test_user(self):
        self.assertEqual(flag_value(self.a, "--user"), "1000:1000")

    def test_cap_drop_all(self):
        self.assertEqual(flag_value(self.a, "--cap-drop"), "ALL")

    def test_no_new_privileges(self):
        self.assertEqual(flag_value(self.a, "--security-opt"), "no-new-privileges")

    def test_read_only(self):
        self.assertIn("--read-only", self.a)

    def test_tmpfs_both(self):
        self.assertIn("/tmp:rw,nosuid,nodev,size=1g", self.a)
        self.assertIn("/home/agent:rw,nosuid,nodev,size=512m,uid=1000,gid=1000", self.a)

    def test_limits(self):
        self.assertEqual(flag_value(self.a, "--memory"), "4g")
        self.assertEqual(flag_value(self.a, "--cpus"), "4")
        self.assertEqual(flag_value(self.a, "--pids-limit"), "1024")

    def test_network_and_dns(self):
        self.assertEqual(flag_value(self.a, "--network"), f"mcl-{NONCE}-int")
        self.assertEqual(flag_value(self.a, "--dns"), "0.0.0.0")

    def test_env_file(self):
        self.assertEqual(flag_value(self.a, "--env-file"), "/tmp/x.env")

    def test_only_two_volumes(self):
        vols = [self.a[i + 1] for i, t in enumerate(self.a) if t == "-v"]
        self.assertEqual(vols, [f"mcl-{NONCE}-wt:/work", f"mcl-{NONCE}-out:/out"])

    def test_workdir_image_entry(self):
        self.assertEqual(flag_value(self.a, "-w"), "/work")
        self.assertEqual(self.a[-5:], ["--entrypoint", "python3", DIGEST, "/opt/mc-lane/entry.py", "run"][-5:])
        self.assertEqual(flag_value(self.a, "--entrypoint"), "python3")
        self.assertLess(self.a.index("--entrypoint"), self.a.index(DIGEST))

    def test_no_dash_e(self):
        for t in self.a:
            self.assertNotEqual(t, "-e")
            self.assertNotEqual(t, "--env")
            self.assertFalse(t.startswith("--env="), t)
            self.assertFalse(t.startswith("-e") and not t.startswith("--"), t)

    def test_no_forbidden_flags(self):
        joined = " ".join(self.a)
        for bad in ("--privileged", "--device", "docker.sock", "--mount", "--volumes-from"):
            self.assertNotIn(bad, joined)
        for bad in ("--pid", "--ipc", "--userns", "--network=host"):
            self.assertNotIn(bad, self.a)

    def test_no_host_path_mounts(self):
        for i, t in enumerate(self.a):
            if t == "-v":
                src = self.a[i + 1].split(":")[0]
                self.assertRegex(src, r"^mcl-[a-z0-9-]+$")      # a named volume, never a path
                self.assertFalse(src.startswith(("/", ".", "~")))

    def test_cred_volume_only_for_codex_file(self):
        codex = L.run_argv(CFG, NONCE, "/e", DIGEST, "codex")
        self.assertIn("mcl-cred-codex:/cred/codex:ro", codex)                  # L1: read-only mount
        self.assertNotIn("mcl-cred-codex:/cred/codex", codex)
        for tool in ("pi", "fake", "codex-key"):
            self.assertFalse([a for a in L.run_argv(CFG, NONCE, "/e", DIGEST, tool) if "mcl-cred-codex" in a], tool)

    def test_image_needs_digest(self):
        for bad in ("mc-lane", "mc-lane:latest", "mc-lane@sha256:abc", "mc-lane@sha256:" + "g" * 64):
            with self.assertRaises(ValueError):
                L.run_argv(CFG, NONCE, "/e", bad, "pi")

    def test_nonce_validated(self):
        for bad in ("", "ABC", NONCE.upper(), NONCE[:-1], NONCE + "0", "../" + NONCE[3:]):
            with self.assertRaises(ValueError):
                L.run_argv(CFG, bad, "/e", DIGEST, "pi")

    def test_require_digest(self):
        self.assertEqual(L.require_digest(DIGEST), DIGEST)
        with self.assertRaises(ValueError):
            L.require_digest("x:y")


class OtherArgvTests(unittest.TestCase):
    def test_network_create(self):
        self.assertEqual(L.network_create_argv("n"), ["network", "create", "--internal", "--ipv6=false", "n"])

    def test_proxy_connect_bridge(self):
        self.assertEqual(L.proxy_connect_argv(NONCE), ["network", "connect", "bridge", f"mcl-{NONCE}-proxy"])

    def test_extract_no_network_and_readonly_sources(self):
        a = L.extract_argv(CFG, NONCE, DIGEST)
        self.assertEqual(flag_value(a, "--network"), "none")
        vols = [a[i + 1] for i, t in enumerate(a) if t == "-v"]
        self.assertIn(f"mcl-{NONCE}-wt:/tool:ro", vols)
        self.assertIn(f"mcl-{NONCE}-out:/outsrc:ro", vols)
        self.assertIn(f"mcl-{NONCE}-pristine:/pristine", vols)
        self.assertIn(f"mcl-{NONCE}-x:/x", vols)
        self.assertEqual(flag_value(a, "--cap-drop"), "ALL")
        self.assertIn("--read-only", a)
        self.assertNotIn("-e", a)

    def test_verify_default_no_network(self):
        a = L.verify_argv(CFG, NONCE, DIGEST)
        self.assertEqual(flag_value(a, "--network"), "none")
        self.assertNotIn("--env-file", a)

    def test_verify_container_only_idles_and_exec_argv_shapes(self):
        a = L.verify_argv(CFG, NONCE, DIGEST)
        self.assertEqual(a[-4:], ["--entrypoint", "sleep", DIGEST, "infinity"])
        self.assertNotIn("/opt/mc-lane/entry.py", a)                  # no verify logic runs as the container's main process
        self.assertEqual(flag_value(a, "--cap-drop"), "ALL")
        self.assertIn("--read-only", a)
        self.assertEqual(flag_value(a, "--user"), "1000:1000")
        self.assertEqual(L.exec_argv(NONCE, "pytest -q"), ["exec", "-u", "1000:1000", "-w", "/repo", f"mcl-{NONCE}-verify",
                                                           "sh", "-c", "pytest -q"])

    def test_verify_network_uses_deps(self):
        a = L.verify_argv(dict(CFG, verify_network=True), NONCE, DIGEST, "/v.env")
        self.assertEqual(flag_value(a, "--network"), f"mcl-{NONCE}-deps")
        self.assertEqual(flag_value(a, "--dns"), "0.0.0.0")
        self.assertEqual(flag_value(a, "--env-file"), "/v.env")

    def test_stage_has_only_seed_host_mount_readonly(self):
        a = L.stage_argv(CFG, NONCE, DIGEST, "/runner/seed.git", SHA, "repo")
        vols = [a[i + 1] for i, t in enumerate(a) if t == "-v"]
        self.assertEqual(vols, ["/runner/seed.git:/seed:ro", f"mcl-{NONCE}-repo:/work"])
        self.assertEqual(flag_value(a, "--network"), "none")
        with self.assertRaises(ValueError):
            L.stage_argv(CFG, NONCE, DIGEST, "/s", SHA, "out")

    def test_proxy_argv_hardening(self):
        a = L.proxy_argv(CFG, NONCE, "/p.env", "proxy@sha256:" + "c" * 64)
        self.assertEqual(flag_value(a, "--user"), "13:13")
        self.assertEqual(flag_value(a, "--cap-drop"), "ALL")
        self.assertIn("--read-only", a)
        self.assertEqual(flag_value(a, "--network"), f"mcl-{NONCE}-int")
        self.assertEqual(flag_value(a, "--name"), f"mcl-{NONCE}-proxy")
        with self.assertRaises(ValueError):
            L.proxy_argv(CFG, NONCE, "/p.env", "proxy:latest")
        d = L.proxy_argv(CFG, NONCE, "/p.env", "proxy@sha256:" + "c" * 64, deps=True)
        self.assertEqual(flag_value(d, "--network"), f"mcl-{NONCE}-deps")

    def test_squid_conf(self):
        conf = L.render_squid_conf(["api.openai.com", "chatgpt.com"])
        self.assertIn("acl allow_hosts dstdomain api.openai.com chatgpt.com", conf)
        self.assertIn("http_access deny all", conf)
        for bad in ("1.2.3.4", "localhost", "evil.com; deny", ".openai.com", "a b.com"):
            with self.assertRaises(ValueError):
                L.render_squid_conf([bad])
        with self.assertRaises(ValueError):
            L.render_squid_conf([])

    def test_squid_conf_is_rendered_from_the_one_template(self):
        tmpl = (ROOT / "sandbox" / "proxy" / "squid.conf.tmpl").read_text(encoding="utf-8")
        self.assertEqual(L.render_squid_conf(["a.example.com", "b.example.com"]),
                         tmpl.replace("{allow_hosts}", "a.example.com b.example.com"))
        conf = L.render_squid_conf(["a.example.com"])
        for needle in ("0.0.0.0/8", "100.64.0.0/10", "internal_names", "host.docker.internal", "http_access deny private"):
            self.assertIn(needle, conf)
        src = (ROOT / "scripts" / "super-board-lane-docker.py").read_text(encoding="utf-8")
        self.assertNotIn("http_access deny ip_literal", src)                                 # no second source of truth


class InstalledLayoutTests(unittest.TestCase):
    def test_the_squid_template_is_found_beside_an_installed_script(self):
        """install.sh puts super-board-squid.conf.tmpl next to the script in .claude/bin; there is no sandbox/ dir there."""
        with tempfile.TemporaryDirectory() as d:
            bindir = Path(d) / ".claude" / "bin"
            bindir.mkdir(parents=True)
            shutil.copy(ROOT / "scripts" / "super-board-lane-docker.py", bindir / "super-board-lane-docker.py")
            shutil.copy(ROOT / "sandbox" / "proxy" / "squid.conf.tmpl", bindir / "super-board-squid.conf.tmpl")
            sp = importlib.util.spec_from_file_location("installed_lane_docker", bindir / "super-board-lane-docker.py")
            m = importlib.util.module_from_spec(sp)
            sp.loader.exec_module(m)
            self.assertEqual(m.squid_template_path().name, "super-board-squid.conf.tmpl")
            self.assertIn("acl allow_hosts dstdomain a.example.com", m.render_squid_conf(["a.example.com"]))
            (bindir / "super-board-squid.conf.tmpl").unlink()
            with self.assertRaises(RuntimeError):
                m.squid_template_path()


class CheckLocalTests(unittest.TestCase):
    def test_lenovo_unrestricted(self):
        L.check_local({"where": "lenovo"}, "codex", DIGEST, {})

    def test_local_ok_for_smoke(self):
        L.check_local({"where": "local"}, "fake", "mc-lane-smoke@sha256:" + "a" * 64, {"MC_DOCKER_SMOKE": "1"})

    def test_local_refusals(self):
        c = {"where": "local"}
        smoke, env = "mc-lane-smoke@sha256:" + "a" * 64, {"MC_DOCKER_SMOKE": "1"}
        for args in [("codex", smoke, env), ("fake", DIGEST, env), ("fake", smoke, {}),
                     ("fake", smoke, {"MC_DOCKER_SMOKE": "0"})]:
            with self.assertRaises(ValueError, msg=str(args)):
                L.check_local(c, *args)


class EnvTests(unittest.TestCase):
    def lines(self, tool="pi", environ=None, cfg=CFG):
        return L.env_lines(tool, cfg, {"RUNINFRA_GATEWAY_KEY": "k1", "OPENAI_API_KEY": "k2"} if environ is None
                           else environ, NONCE, "runinfra/glm-5-3-flash", hard_s=120)

    def test_template_and_proxy(self):
        ls = self.lines()
        for want in ("PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/home/agent", "LANG=C.UTF-8", "RUNINFRA_GATEWAY_KEY=k1",
                     f"HTTPS_PROXY=http://mcl-{NONCE}-proxy:3128", f"HTTP_PROXY=http://mcl-{NONCE}-proxy:3128",
                     "NO_PROXY=", "NODE_USE_ENV_PROXY=1", "MCL_IDLE_S=600", "MCL_HARD_S=120", "MCL_TOOL=pi",
                     "MCL_MODEL=runinfra/glm-5-3-flash",
                     "MCL_ENV_DENY=AMS_OPERATOR_EXPECTED,AMS_OPERATOR_DATA_ROOT,SAP_LIVE_TESTS"):
            self.assertIn(want, ls)

    def test_only_dispatched_tool_key(self):
        joined = "\n".join(self.lines("pi"))
        self.assertNotIn("OPENAI_API_KEY", joined)
        self.assertNotIn("k2", joined)

    def test_host_env_does_not_leak(self):
        ls = self.lines(environ={"RUNINFRA_GATEWAY_KEY": "k1", "PATH": "C:\\win", "USERNAME": "x"})
        self.assertNotIn("PATH=C:\\win", ls)
        self.assertFalse(any(l.startswith("USERNAME") for l in ls))

    def test_file_auth_adds_no_key(self):
        self.assertFalse(any("=" in l and l.split("=")[0].endswith("KEY") for l in self.lines("codex", {})))

    def test_refuses_denied_names(self):
        for key in ("AMS_OPERATOR_EXPECTED", "ACTIONS_RUNTIME_TOKEN", "RUNNER_TEMP", "GITHUB_TOKEN", "GH_TOKEN",
                    "LANE_REPO_TOKEN", "GITHUB_API_KEY"):
            cfg = dict(CFG, tools=[{"id": "t", "auth": {"kind": "env", "keys": [key]}}])
            with self.assertRaises(ValueError, msg=key):
                L.env_lines("t", cfg, {key: "v"}, NONCE, "m")

    def test_refuses_newline_values(self):
        for v in ("a\nB=1", "a\rb", "a\x00b"):
            with self.assertRaises(ValueError):
                self.lines(environ={"RUNINFRA_GATEWAY_KEY": v})

    def test_refuses_missing_and_bad_key(self):
        with self.assertRaises(ValueError):
            self.lines(environ={})
        cfg = dict(CFG, tools=[{"id": "t", "auth": {"kind": "env", "keys": ["A=B"]}}])
        with self.assertRaises(ValueError):
            L.env_lines("t", cfg, {"A=B": "v"}, NONCE, "m")

    def test_write_env_file_posix(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.env"
            names = L.write_env_file(p, ["A=secret", "B=2"], windows=False)
            self.assertEqual(names, ["A", "B"])
            self.assertEqual(p.read_text(), "A=secret\nB=2\n")
            if os.name != "nt":
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_write_env_file_windows_calls_acl_before_content(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.env"

            def acl(path):
                calls.append((path, p.read_bytes()))

            names = L.write_env_file(p, ["A=secret"], windows=True, acl=acl)
            self.assertEqual(names, ["A"])
            self.assertEqual(calls, [(p, b"")])               # content was not on disk yet when the ACL was set
            self.assertEqual(p.read_text(), "A=secret\n")

    def test_icacls_command(self):
        with mock.patch.object(L.subprocess, "run") as run, mock.patch.dict(os.environ, {"USERNAME": "svc"}):
            L._icacls("C:/x.env")
            args = run.call_args[0][0]
            self.assertEqual(args[0], "icacls")
            self.assertEqual(args[2:], ["/inheritance:r", "/grant:r", "svc:F"])


class BuildResultTests(unittest.TestCase):
    INPUTS = {"nonce": NONCE, "card": 7, "lane": "build", "attempt": 1, "tool": "codex", "model": "gpt-5.6-luna"}
    FACTS = {"start_sha": SHA, "patch_sha256": "d" * 64, "patch_bytes": 10}

    def test_ok(self):
        r = L.build_result({"exit_code": 0}, self.FACTS, self.INPUTS)
        self.assertEqual((r["schema"], r["kind"]), ("mc-lane.result.v1", "ok"))
        self.assertEqual((r["nonce"], r["start_sha"], r["patch_sha256"], r["model"]), (NONCE, SHA, "d" * 64, "gpt-5.6-luna"))

    def test_timeout(self):
        self.assertEqual(L.build_result({"exit_code": 124}, self.FACTS, self.INPUTS)["kind"], "timeout")
        self.assertEqual(L.build_result({"exit_code": 137, "timed_out": True}, self.FACTS, self.INPUTS)["kind"], "timeout")

    def test_failures(self):
        self.assertEqual(L.build_result({"exit_code": 1}, self.FACTS, self.INPUTS)["kind"], "tool_failed")
        self.assertEqual(L.build_result({"exit_code": 1}, self.FACTS, self.INPUTS, "auth")["kind"], "auth")
        empty = dict(self.FACTS, patch_bytes=0)
        self.assertNotEqual(L.build_result({"exit_code": 0}, empty, self.INPUTS)["kind"], "ok")
        self.assertEqual(L.build_result({"exit_code": 0}, empty, self.INPUTS, "ask_confirm")["kind"], "ask_confirm")

    def test_signature_cannot_make_ok(self):
        self.assertNotEqual(L.build_result({"exit_code": 1}, self.FACTS, self.INPUTS, "ok")["kind"], "ok")

    def test_ignores_forged_result_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            out.mkdir()
            (out / "result.json").write_text(json.dumps({"kind": "ok", "patch_sha256": "f" * 64, "start_sha": "e" * 40}))
            real_open = open
            opened = []

            def spy(file, *a, **k):
                opened.append(str(file))
                return real_open(file, *a, **k)

            with mock.patch("builtins.open", spy):
                r = L.build_result({"exit_code": 1}, self.FACTS, self.INPUTS)
            self.assertEqual(opened, [])
            self.assertEqual(r["kind"], "tool_failed")
            self.assertEqual(r["patch_sha256"], "d" * 64)
            self.assertEqual(r["start_sha"], SHA)


def make_tar(path, members):
    with tarfile.open(path, "w") as tf:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
            elif kind == "symlink":
                ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
                tf.addfile(ti)
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)


class TarTests(unittest.TestCase):
    def check(self, members, max_total=1000):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.tar"
            make_tar(p, members)
            return L.validate_tar(p, max_total)

    def test_ok(self):
        self.assertEqual(self.check([("lane.patch", "file", b"abc"), ("session/a.jsonl", "file", b"x")]),
                         ["lane.patch", "session/a.jsonl"])

    def test_refusals(self):
        for members in ([("link", "symlink", b"")], [("../x", "file", b"a")], [("a/../../x", "file", b"a")],
                        [("/etc/x", "file", b"a")], [("C:/x", "file", b"a")], [("a\\b", "file", b"a")],
                        [("d", "dir", b"")], [("big", "file", b"a" * 2000)]):
            with self.assertRaises(ValueError, msg=str(members[0][:2])):
                self.check(members)

    def test_total_not_per_member(self):
        with self.assertRaises(ValueError):
            self.check([("a", "file", b"x" * 600), ("b", "file", b"x" * 600)])


class StubCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.log, self.script = t / "log.jsonl", t / "script.json"
        self.patch = mock.patch.dict(os.environ, {
            "MCL_DOCKER_ARGV": json.dumps([sys.executable, str(STUB)]),
            "DOCKER_STUB_LOG": str(self.log), "DOCKER_STUB_SCRIPT": str(self.script)})
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def script_rules(self, rules):
        self.script.write_text(json.dumps(rules))

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []


class DockerEntryTests(StubCase):
    def test_docker_goes_through_stub(self):
        r = L._docker(["version"])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [["version"]])

    def test_output_is_decoded_as_utf8_with_replacement_not_the_console_codepage(self):
        code = "import sys; sys.stdout.buffer.write(b'ok \\xe2\\x9c\\x93 bad \\xff'); sys.exit(0)"
        with mock.patch.dict(os.environ, {"MCL_DOCKER_ARGV": json.dumps([sys.executable, "-c", code])}):
            r = L._docker(["x"])
        self.assertEqual(r.stdout, "ok "+chr(0x2713)+" bad "+chr(0xfffd))

    def test_missing_docker_raises(self):
        with mock.patch.dict(os.environ, {"MCL_DOCKER": "no-such-docker-xyz"}):
            os.environ.pop("MCL_DOCKER_ARGV")
            with self.assertRaises(RuntimeError):
                L._docker(["version"])


class ExecSandboxTests(StubCase):
    """H1: one command in a fresh verify container; every process is a docker call, nothing runs on the host."""

    def setUp(self):
        super().setUp()
        self.seed = Path(self.tmp.name) / "seed.git"
        self.seed.mkdir()
        self.cfg = dict(FULL_CFG, verify_network=False)
        self.out = []

    def run_cmd(self, command, cfg=None):
        return L.exec_sandbox(cfg or self.cfg, NONCE, self.seed, SHA, command, Path(self.tmp.name) / "state", self.out.append)

    def test_stages_starts_verify_container_and_execs_as_uid_1000(self):
        self.script_rules([{"match": ["exec"], "rc": 7, "stdout": "tests: \u001b[31mred\u001b[0m\r\nline2\n"}])
        self.assertEqual(self.run_cmd(["pytest", "-q", "a b"]), 7)                  # the docker exit code
        calls = self.calls()
        stage = [c for c in calls if c[0] == "run" and "stage" in c]
        self.assertEqual(len(stage), 1)
        self.assertIn(f"mcl-{NONCE}-repo:/work", stage[0])
        verify = [c for c in calls if c[:1] == ["run"] and f"mcl-{NONCE}-verify" in c]
        self.assertEqual(len(verify), 1)
        self.assertEqual(verify[0][verify[0].index("--network") + 1], "none")      # verify_network false
        ex, = [c for c in calls if c[0] == "exec"]
        self.assertEqual(ex[:6], ["exec", "-u", "1000:1000", "-w", "/repo", f"mcl-{NONCE}-verify"])
        self.assertEqual(ex[6:], ["sh", "-c", "pytest -q 'a b'"])
        self.assertEqual("".join(self.out), "tests: [31mred[0m\nline2\n")         # escape and CR characters stripped

    def test_one_argument_is_a_shell_line_and_gate_image_wins(self):
        self.script_rules([])
        cfg = dict(self.cfg, gate_image="gate@sha256:" + "d" * 64)
        self.assertEqual(self.run_cmd(["npm test && npm run lint"], cfg), 0)
        ex, = [c for c in self.calls() if c[0] == "exec"]
        self.assertEqual(ex[-1], "npm test && npm run lint")
        stage, = [c for c in self.calls() if c[0] == "run" and "stage" in c]
        self.assertIn("gate@sha256:" + "d" * 64, stage)

    def test_verify_network_uses_the_deps_proxy(self):
        self.script_rules([])
        self.assertEqual(self.run_cmd(["true"], dict(self.cfg, verify_network=True)), 0)
        flat = [" ".join(c) for c in self.calls()]
        self.assertTrue(any(c.startswith("network create --internal") and f"mcl-{NONCE}-deps" in c for c in flat))
        verify, = [c for c in self.calls() if c[:1] == ["run"] and f"mcl-{NONCE}-verify" in c]
        self.assertEqual(verify[verify.index("--network") + 1], f"mcl-{NONCE}-deps")

    def test_verify_setup_runs_first_in_the_same_container(self):
        self.script_rules([])
        cfg = dict(self.cfg, verify_setup=["pnpm install --offline", "make gen"])
        self.assertEqual(self.run_cmd(["pytest"], cfg), 0)
        execs = [c for c in self.calls() if c[0] == "exec"]
        self.assertEqual([c[-1] for c in execs], ["pnpm install --offline", "make gen", "pytest"])
        self.assertEqual({c[5] for c in execs}, {f"mcl-{NONCE}-verify"})            # one fresh container for setup and command
        self.assertEqual(len([c for c in self.calls() if c[:1] == ["run"] and f"mcl-{NONCE}-verify" in c]), 1)

    def test_a_failing_verify_setup_is_the_result_and_the_command_never_runs(self):
        self.script_rules([{"match": ["exec"], "rcs": [3, 0], "stdout": "ERR no network\n"}])
        cfg = dict(self.cfg, verify_setup=["pnpm install --offline"])
        self.assertEqual(self.run_cmd(["pytest"], cfg), 3)
        self.assertEqual([c[-1] for c in self.calls() if c[0] == "exec"], ["pnpm install --offline"])
        text = "".join(self.out)
        self.assertIn("verify_setup failed (3): pnpm install --offline", text)
        self.assertIn("ERR no network", text)

    def test_setup_and_command_share_one_deadline(self):
        self.script_rules([])
        cfg = dict(self.cfg, verify_setup=["slow-setup"], verify_timeout_min=1)
        ticks = iter([0.0, 59.9, 61.0, 61.0])                 # the setup used the whole minute
        with mock.patch.object(L.time, "monotonic", side_effect=lambda: next(ticks)):
            self.assertEqual(self.run_cmd(["pytest"], cfg), 124)
        self.assertEqual([c[-1] for c in self.calls() if c[0] == "exec"], ["slow-setup"])
        self.assertIn("deadline exceeded before the command started", "".join(self.out))

    def test_bad_inputs_run_nothing(self):
        for args in ((self.cfg, "x" * 40, ["true"]), (self.cfg, SHA, []), (dict(self.cfg, image="mc-lane:latest"), SHA, ["true"])):
            with self.assertRaises(ValueError):
                L.exec_sandbox(args[0], NONCE, self.seed, args[1], args[2], Path(self.tmp.name) / "st", self.out.append)
        self.assertEqual(self.calls(), [])


    def test_shell_false_runs_the_list_as_argv_with_no_sh_and_no_join(self):
        self.script_rules([])
        self.assertEqual(L.exec_argv(NONCE, ["npm", "test"], shell=False),
                         ["exec", "-u", "1000:1000", "-w", "/repo", f"mcl-{NONCE}-verify", "npm", "test"])
        self.assertEqual(self.run_cmd_argv(["npm test && curl x"]), 0)
        ex, = [c for c in self.calls() if c[0] == "exec"]
        self.assertEqual(ex[6:], ["npm test && curl x"])                       # ONE literal element
        self.assertNotIn("sh", ex)
        self.assertNotIn("-c", ex)

    def test_shell_false_keeps_every_element_literal_and_setup_stays_a_shell_line(self):
        self.script_rules([])
        cfg = dict(self.cfg, verify_setup=["make gen"])
        self.assertEqual(L.exec_sandbox(cfg, NONCE, self.seed, SHA, ["pytest", "-q", "a b", "$X;y"], Path(self.tmp.name) / "state",
                                        self.out.append, shell=False), 0)
        execs = [c for c in self.calls() if c[0] == "exec"]
        self.assertEqual(execs[0][6:], ["sh", "-c", "make gen"])
        self.assertEqual(execs[1][6:], ["pytest", "-q", "a b", "$X;y"])

    def run_cmd_argv(self, command):
        return L.exec_sandbox(self.cfg, NONCE, self.seed, SHA, command, Path(self.tmp.name) / "state", self.out.append, shell=False)


class ContainmentTests(StubCase):
    def removed(self):
        c = self.calls()
        return ([x[2] for x in c if x[:2] == ["rm", "-f"]],
                [x[2] for x in c if x[:2] == ["network", "rm"]],
                [x[3] for x in c if x[:2] == ["volume", "rm"]])

    def assert_all_removed(self):
        ctr, net, vol = self.removed()
        for role in L.CONTAINER_ROLES:
            self.assertIn(f"mcl-{NONCE}-{role}", ctr)
        self.assertEqual(sorted(net), sorted(f"mcl-{NONCE}-{r}" for r in L.NETWORK_ROLES))
        self.assertEqual(sorted(vol), sorted(f"mcl-{NONCE}-{r}" for r in L.VOLUME_ROLES))

    def test_start_detached_and_clean_exit(self):
        with L.DockerContainment(NONCE) as c:
            name = c.start(L.run_argv(CFG, NONCE, "/e", DIGEST, "pi"))
        self.assertEqual(name, f"mcl-{NONCE}-run")
        first = self.calls()[0]
        self.assertEqual(first[:2], ["run", "-d"])
        self.assert_all_removed()

    def test_wait_returns_exit_code(self):
        self.script_rules([{"match": ["inspect"], "stdouts": ["true\n", "true\n", "false\n"]},
                           {"match": ["wait"], "stdout": "3\n"}])
        c = L.DockerContainment(NONCE)
        info = c.wait("n", 5, poll_s=0, sleep=lambda s: None)
        self.assertEqual(info, {"exit_code": 3, "timed_out": False})

    def test_close_runs_after_timeout(self):
        self.script_rules([{"match": ["inspect"], "stdout": "true\n"}])
        ticks = iter(range(0, 1000))
        with L.DockerContainment(NONCE) as c:
            c.started.append(f"mcl-{NONCE}-run")
            info = c.wait(f"mcl-{NONCE}-run", 3, poll_s=0, sleep=lambda s: None, clock=lambda: next(ticks))
        self.assertEqual(info, {"exit_code": 124, "timed_out": True})
        self.assertEqual(L.build_result(info, {"patch_bytes": 5}, {}, None)["kind"], "timeout")
        self.assertGreaterEqual(self.removed()[0].count(f"mcl-{NONCE}-run"), 2)     # kill + close
        self.assert_all_removed()

    def test_close_runs_after_exception(self):
        with self.assertRaises(KeyError):
            with L.DockerContainment(NONCE):
                raise KeyError("boom")
        self.assert_all_removed()

    def test_close_attempts_everything_when_first_fails(self):
        self.script_rules([{"match": ["rm", "-f"], "rcs": [1, 0], "stderr": "boom"}])
        c = L.DockerContainment(NONCE)
        actions = c.close()
        self.assert_all_removed()
        self.assertEqual(actions[0][1], 1)
        self.assertEqual(len(actions), len(L.CONTAINER_ROLES) + len(L.NETWORK_ROLES) + len(L.VOLUME_ROLES))

    def test_close_survives_docker_raising(self):
        def boom(args, **kw):
            raise RuntimeError("no docker")
        c = L.DockerContainment(NONCE, docker=boom)
        actions = c.close()
        self.assertEqual(len(actions), len(L.CONTAINER_ROLES) + len(L.NETWORK_ROLES) + len(L.VOLUME_ROLES))
        self.assertTrue(all("RuntimeError" in str(a[1]) for a in actions))

    def test_start_failure_raises(self):
        self.script_rules([{"match": ["run"], "rc": 125, "stderr": "name in use"}])
        with self.assertRaises(RuntimeError):
            L.DockerContainment(NONCE).start(L.run_argv(CFG, NONCE, "/e", DIGEST, "pi"))

    def test_kill_is_rm_f(self):
        c = L.DockerContainment(NONCE)
        c.started.append("abc")
        c.kill()
        self.assertEqual(self.calls(), [["rm", "-f", "abc"]])

    def test_bad_nonce(self):
        with self.assertRaises(ValueError):
            L.DockerContainment("nope")


# ---------------------------------------------------------------- contracts between the three modules (no docker)

import hashlib
import re
import shutil
import subprocess
import zipfile

_es = importlib.util.spec_from_file_location("mc_lane_entry", ROOT / "sandbox/entry.py")
ENTRY = importlib.util.module_from_spec(_es)
_es.loader.exec_module(ENTRY)
_rs = importlib.util.spec_from_file_location("lane_remote", ROOT / "scripts/super-board-lane-remote.py")
REMOTE = importlib.util.module_from_spec(_rs)
_rs.loader.exec_module(REMOTE)

HAVE_GIT = shutil.which("git") is not None
HAVE_SH = shutil.which("sh") is not None and os.name != "nt"
START_PLACEHOLDER = "c" * 40
FULL_CFG = dict(CFG, image=DIGEST, proxy_image="proxy@sha256:" + "c" * 64, verify_commands=["true"],
                verify_timeout_min=5, tools=[{"id": "pi-delegate", "auth": {"kind": "env", "keys": ["RUNINFRA_GATEWAY_KEY"]},
                                              "egress": ["api.runinfra.ai"]}, {"id": "fake"}])


def mounts(argv):
    return [argv[i + 1].split(":")[1] for i, t in enumerate(argv) if t == "-v"]


def entry_args(argv, image=DIGEST):
    """What follows the image in a docker run argv: [/opt/mc-lane/entry.py, subcommand, flags...]."""
    tail = argv[argv.index(image) + 1:]
    assert tail[0] == "/opt/mc-lane/entry.py", tail
    return tail[1:]


class ContractTests(unittest.TestCase):
    """The three modules were written separately; these tests pin the interfaces to each other."""

    def test_argv_is_parsed_by_entry_py_itself(self):
        parser = ENTRY.build_parser()
        run = parser.parse_args(entry_args(L.run_argv(FULL_CFG, NONCE, "/e", DIGEST, "pi")))
        self.assertEqual(run.cmd, "run")

        ex_argv = L.extract_argv(FULL_CFG, NONCE, DIGEST)
        ex = parser.parse_args(entry_args(ex_argv))
        self.assertEqual(ex.cmd, "extract")
        for path in (ex.tool_dir, ex.pristine, ex.outsrc, ex.x):
            self.assertIn(path, mounts(ex_argv), path)              # every path entry.py uses is a mount target

        vf_argv = L.verify_argv(FULL_CFG, NONCE, DIGEST)
        vm = mounts(vf_argv)
        pr_argv = L.prepare_argv(NONCE)                         # entry.py prepare, reached by docker exec (A13)
        self.assertEqual(pr_argv[:6], ["exec", "-u", "1000:1000", "-w", "/repo", f"mcl-{NONCE}-verify"])
        self.assertEqual(pr_argv[6:8], ["python3", "/opt/mc-lane/entry.py"])
        vf = parser.parse_args(pr_argv[8:])
        self.assertEqual(vf.cmd, "prepare")
        for path in (vf.repo, os.path.dirname(vf.patch), vf.x):
            self.assertIn(path, vm, path)
        self.assertEqual(vf.patch, "/in/lane.patch")
        vols = " ".join(vf_argv)
        self.assertNotIn(f"mcl-{NONCE}-wt", vols)                   # the verify container never sees the tool's trees
        self.assertNotIn(f"mcl-{NONCE}-out", vols)
        self.assertIn(f"mcl-{NONCE}-vin:/in:ro", vols)

        st_argv = L.stage_argv(FULL_CFG, NONCE, DIGEST, "/seed.git", SHA, "wt")
        st = parser.parse_args(entry_args(st_argv))
        self.assertEqual((st.cmd, st.sha, st.seed, st.dest), ("stage", SHA, "/seed", "/work"))
        self.assertIn(st.seed, mounts(st_argv))
        self.assertIn(st.dest, mounts(st_argv))
        self.assertIsNone(st.merge_sha)                              # only the gate merges
        base = "c" * 40
        mg_argv = L.stage_argv(FULL_CFG, NONCE, DIGEST, "/seed.git", SHA, "repo", base)
        mg = parser.parse_args(entry_args(mg_argv))
        self.assertEqual((mg.sha, mg.merge_sha, mg.merge_ref), (SHA, base, "refs/mc/base"))
        with self.assertRaises(ValueError):
            L.stage_argv(FULL_CFG, NONCE, DIGEST, "/seed.git", SHA, "repo", "not-a-sha")

        for role, dest in (("out", "/dst"), ("vin", "/dst")):
            pu_argv = L.put_argv(FULL_CFG, NONCE, DIGEST, role)
            pu = parser.parse_args(entry_args(pu_argv))
            self.assertEqual((pu.cmd, pu.dest), ("put", dest))
            self.assertIn(dest, mounts(pu_argv))

    def test_entrypoint_is_forced_so_the_image_entrypoint_cannot_double_up(self):
        for a in (L.run_argv(FULL_CFG, NONCE, "/e", DIGEST, "pi"), L.extract_argv(FULL_CFG, NONCE, DIGEST),
                  L.put_argv(FULL_CFG, NONCE, DIGEST, "out")):
            self.assertEqual(flag_value(a, "--entrypoint"), "python3")
            self.assertLess(a.index("--entrypoint"), a.index(DIGEST))
        v = L.verify_argv(FULL_CFG, NONCE, DIGEST)
        self.assertEqual(flag_value(v, "--entrypoint"), "sleep")
        self.assertLess(v.index("--entrypoint"), v.index(DIGEST))

    def test_run_env_file_carries_what_entry_py_reads(self):
        lines = L.env_lines("pi-delegate", FULL_CFG, {"RUNINFRA_GATEWAY_KEY": "k"}, NONCE, "runinfra/glm-5-3-flash",
                            hard_s=300, idle_s=60)
        env = dict(l.split("=", 1) for l in lines)
        self.assertEqual((env["MCL_TOOL"], env["MCL_MODEL"]), ("pi", "runinfra/glm-5-3-flash"))
        self.assertEqual((env["MCL_IDLE_S"], env["MCL_HARD_S"]), ("60", "300"))
        self.assertTrue(ENTRY.MODEL_RE.match(env["MCL_MODEL"]))
        tools = json.loads((ROOT / "sandbox/tools.json").read_text())["tools"]
        self.assertIn(env["MCL_TOOL"], tools)                       # entry.py can resolve the tool
        # entry.py strips every MCL_* variable from the tool's environment and keeps the tool's own key
        child = ENTRY.scrub_env(env)
        self.assertNotIn("MCL_TOOL", child)
        self.assertEqual(child["RUNINFRA_GATEWAY_KEY"], "k")
        for bad in ("x; rm -rf /", "", "a b", "-m", "x" * 101):
            with self.assertRaises(ValueError, msg=bad):
                L.env_lines("pi", FULL_CFG, {"RUNINFRA_GATEWAY_KEY": "k"}, NONCE, bad)

    def test_tool_ids_are_one_namespace(self):
        self.assertEqual(L.norm_tool("pi-delegate"), "pi")
        self.assertEqual(L.norm_tool("codex"), "codex")
        self.assertEqual(L.tool_cfg(FULL_CFG, "pi")["egress"], ["api.runinfra.ai"])
        self.assertEqual(L.tool_cfg(FULL_CFG, "pi-delegate")["egress"], ["api.runinfra.ai"])
        self.assertEqual(REMOTE.norm_tool("pi-delegate"), L.norm_tool("pi-delegate"))
        yml = (ROOT / "sandbox/lane.yml").read_text()
        enum = set(re.search(r'TOOLS = \{([^}]*)\}', yml).group(1).replace('"', "").replace(" ", "").split(","))
        tools = set(json.loads((ROOT / "sandbox/tools.json").read_text())["tools"]) | {"fake"}   # fake: smoke image only
        self.assertEqual(enum, tools)

    def test_header_lists_the_repo_allowlist_key(self):
        self.assertIn("pilot.sandbox.repos", L.__doc__ if hasattr(L, "__doc__") else "")

    def test_caps_agree_across_modules(self):
        self.assertEqual(REMOTE.BUNDLE_CAPS["lane.patch"], ENTRY.PATCH_MAX)
        self.assertEqual(REMOTE.BUNDLE_CAPS["raw.log"], ENTRY.RAW_LOG_MAX)
        self.assertEqual(REMOTE.BUNDLE_CAPS["report.json"], ENTRY.REPORT_MAX)
        self.assertEqual(REMOTE.SESSION_TOTAL_CAP, ENTRY.SESSION_MAX)
        self.assertEqual(REMOTE.BUNDLE_TOTAL_CAP, ENTRY.BUNDLE_MAX)
        self.assertEqual(L.BUNDLE_MAX, ENTRY.BUNDLE_MAX)
        self.assertEqual(L.PATCH_MAX, ENTRY.PATCH_MAX)
        self.assertEqual(L.MODEL_RE.pattern, ENTRY.MODEL_RE.pattern)
        self.assertGreater(REMOTE.ARTIFACT_NAMES["bundle.tar"], ENTRY.BUNDLE_MAX)
        self.assertGreaterEqual(REMOTE.ZIP_TOTAL_CAP, sum(REMOTE.ARTIFACT_NAMES.values()) - 2_000_000)
        self.assertEqual(tuple(L.VERIFY_STATUSES), tuple(REMOTE.VERIFY_STATUSES))

    def test_remote_payload_is_what_the_docker_module_reads(self):
        board = {"verify_commands": ["pytest -q"], "pilot": {
            "verify_timeout_min": 20,
            "sandbox": {"runner_repo": "example-org/mc-lane-runner", "repos": ["Shahat87/ams-mission-kit"],
                        "image": DIGEST, "proxy_image": "p@sha256:" + "b" * 64, "limits": {"memory": "2g"},
                        "env_deny": ["SAP_LIVE_TESTS"]},
            "tools": [{"id": "pi-delegate", "auth": {"kind": "env", "keys": ["RUNINFRA_GATEWAY_KEY"]},
                       "egress": ["api.runinfra.ai"]}]}}
        payload = REMOTE.runner_payload(REMOTE.remote_cfg_from_board(board), "pi-delegate")
        L.require_digest(payload["image"])
        L.require_digest(payload["proxy_image"])
        self.assertEqual(L.tool_cfg(payload, "pi")["egress"], ["api.runinfra.ai"])
        lines = L.env_lines("pi", payload, {"RUNINFRA_GATEWAY_KEY": "k"}, NONCE, "runinfra/m")
        self.assertIn("RUNINFRA_GATEWAY_KEY=k", lines)
        self.assertIn("MCL_ENV_DENY=SAP_LIVE_TESTS", lines)
        self.assertIn("--memory", L.run_argv(payload, NONCE, "/e", DIGEST, "pi"))
        L.render_squid_conf(L.tool_cfg(payload, "pi")["egress"])

    # -- the artifact chain: entry.py extract -> docker module host facts -> zip -> lane-remote validate

    def _repo_with_change(self, d):
        d = Path(d)
        pristine = d / "pristine"
        pristine.mkdir()
        g = lambda *a, cwd=pristine: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=str(cwd),
                                                    check=True, capture_output=True)
        g("init", "-q")
        (pristine / "a.txt").write_text("alpha\n")
        g("add", "-A")
        g("commit", "-qm", "start")
        start = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(pristine), capture_output=True, text=True).stdout.strip()
        tool = d / "tool"
        tool.mkdir()
        (tool / "a.txt").write_text("alpha\nbeta\n")
        (tool / "new.txt").write_text("new\n")
        (tool / "pkg").mkdir()
        (tool / "pkg" / "conftest.py").write_text("import pytest\n")
        (tool / "package.json").write_text("{}\n")
        outsrc = d / "outsrc"
        (outsrc / "session").mkdir(parents=True)
        (outsrc / "raw.log").write_text("log line\n")
        (outsrc / "report.json").write_text('{"summary": "x"}')
        (outsrc / "session" / "weird name!.jsonl").write_text("{}\n")           # tool-chosen name must not travel
        (outsrc / "result.json").write_text('{"kind": "ok"}')                    # forged; must not travel
        x = d / "x"
        x.mkdir()
        self.assertEqual(ENTRY.do_extract(str(tool), str(pristine), str(outsrc), str(x)), 0)
        return start, x / "bundle.tar", d

    def _artifact(self, d, wait_info=None, verify=None):
        start, bundle, d = self._repo_with_change(d)
        inputs = {"nonce": NONCE, "card": 7, "lane": "build", "attempt": 1, "tool": "pi-delegate", "model": "runinfra/m"}
        facts = L.build_host_facts(bundle, wait_info or {"exit_code": 0, "timed_out": False}, inputs, start)
        art = d / "art"
        art.mkdir()
        shutil.copy(bundle, art / "bundle.tar")
        (art / "facts.json").write_text(json.dumps(facts))
        (art / "verify.json").write_text(json.dumps(verify or {"status": "ok", "commands": [
            {"cmd": "true", "exit": 0, "tail": "", "phase": "verify"}]}))
        return start, art, facts

    def _zip_round_trip(self, art, dest):
        z = art.parent / "artifact.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for n in ("bundle.tar", "facts.json", "verify.json"):
                zf.write(art / n, n)
        dest.mkdir()
        REMOTE.safe_extract(z, dest)
        return dest

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_entry_extract_then_host_facts_then_remote_validate(self):
        with tempfile.TemporaryDirectory() as d:
            start, art, facts = self._artifact(d)
            self.assertEqual(facts["start_sha"], start)
            self.assertEqual((facts["tool"], facts["card"], facts["attempt"], facts["exit_code"], facts["timed_out"]),
                             ("pi", 7, 1, 0, False))
            self.assertGreater(facts["patch_bytes"], 0)
            self.assertEqual(facts["harness_files"], ["package.json", "pkg/conftest.py"])      # A13, from the real patch
            self.assertEqual(L.build_result({"exit_code": 0}, facts, {"tool": "pi"})["kind"], "ok")     # patch_bytes is read
            got = self._zip_round_trip(art, Path(d) / "dl")
            res = REMOTE.validate(got, NONCE, start)
            self.assertEqual(res["patch_sha256"], facts["patch_sha256"])
            self.assertTrue(res["verify_ok"])
            self.assertEqual(res["harness_files"], ["package.json", "pkg/conftest.py"])
            self.assertEqual((res["exit_code"], res["timed_out"]), (0, False))
            names = sorted(p.relative_to(got / "bundle").as_posix() for p in (got / "bundle").rglob("*") if p.is_file())
            self.assertEqual(names, ["facts.json", "lane.patch", "raw.log", "report.json", "session/001.jsonl"])
            shutil.rmtree(got / "bundle")                           # validate unpacks next to the zip contents
            cli = subprocess.run([sys.executable, str(ROOT / "scripts/super-board-lane-remote.py"), "validate", "--dir",
                                  str(got), "--nonce", NONCE, "--start-sha", start], capture_output=True)
            self.assertEqual(cli.returncode, 0, cli.stderr)

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_one_flipped_byte_in_lane_patch_is_exit_4(self):
        with tempfile.TemporaryDirectory() as d:
            start, art, _facts = self._artifact(d)
            members = {}
            with tarfile.open(art / "bundle.tar") as tf:
                for m in tf:
                    members[m.name] = tf.extractfile(m).read()
            patch = bytearray(members["lane.patch"])
            patch[-2] ^= 0x01
            members["lane.patch"] = bytes(patch)
            with tarfile.open(art / "bundle.tar", "w") as tf:
                for n, data in members.items():
                    ti = tarfile.TarInfo(n)
                    ti.size = len(data)
                    tf.addfile(ti, io.BytesIO(data))
            got = self._zip_round_trip(art, Path(d) / "dl")
            with self.assertRaises(REMOTE.IsolationError):
                REMOTE.validate(got, NONCE, start)
            cli = subprocess.run([sys.executable, str(ROOT / "scripts/super-board-lane-remote.py"), "validate", "--dir",
                                  str(got), "--nonce", NONCE, "--start-sha", start], capture_output=True)
            self.assertEqual(cli.returncode, 4)

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_host_facts_ignore_the_run_containers_claims(self):
        with tempfile.TemporaryDirectory() as d:
            start, bundle, _ = self._repo_with_change(d)
            inputs = {"nonce": NONCE, "card": 7, "lane": "build", "attempt": 1, "tool": "pi", "model": "m"}
            facts = L.build_host_facts(bundle, {"exit_code": 1, "timed_out": False}, inputs, start)
            self.assertEqual(facts["exit_code"], 1)                  # from `docker wait`, not from any /out file
            with self.assertRaises(ValueError):
                L.build_host_facts(bundle, {"exit_code": 0}, inputs, "d" * 40)       # staged commit differs
            self.assertTrue(L.build_host_facts(bundle, {"exit_code": 124}, inputs, start)["timed_out"])

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_harness_files_from_a_real_patch(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "r"
            repo.mkdir()
            g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=str(repo),
                                          check=True, capture_output=True)
            g("init", "-q")
            (repo / "tox.ini").write_text("[tox]\nenvlist = py\n")
            (repo / "app.py").write_text("x = 1\n")
            g("add", "-A")
            g("commit", "-qm", "start")
            for rel in ("sub/dir/conftest.py", "conftest.py", "package.json", "web/package.json", "pyproject.toml",
                        "setup.cfg", "pytest.ini", "vitest.config.ts", "web/vite.config.mjs", "jest.config.js",
                        "tsconfig.json", "web/tsconfig.base.json", "src/app.py", "notes/my-package.json",
                        "notconftest.py", "tsconfig.txt", "sp ace/conftest.py"):
                f = repo / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("content of " + rel + "\n")
            (repo / "app.py").write_text("x = 2\n")
            g("mv", "tox.ini", "docs-tox.ini")                      # a rename: the old side counts too
            g("add", "-A")
            patch = subprocess.run(["git", "diff", "--cached", "-M", "--binary"], cwd=str(repo), capture_output=True,
                                   check=True).stdout
        self.assertEqual(L.harness_files(patch), sorted([
            "conftest.py", "jest.config.js", "package.json", "pyproject.toml", "pytest.ini", "setup.cfg",
            "sp ace/conftest.py", "sub/dir/conftest.py", "tox.ini", "tsconfig.json", "vitest.config.ts",
            "web/package.json", "web/tsconfig.base.json", "web/vite.config.mjs"]))
        self.assertEqual(L.harness_files(b""), [])
        with self.assertRaises(ValueError):
            L.harness_files(b"this is not a patch\n")

    def test_forged_ok_verify_json_is_refused(self):
        for forged in ({"status": "ok", "commands": []},
                       {"status": "ok", "commands": [{"cmd": "x", "exit": 1, "tail": "", "phase": "verify"}]}):
            with self.assertRaises(REMOTE.IsolationError):
                REMOTE._check_verify(forged)

    def test_lane_yml_calls_only_flags_the_cli_defines(self):
        yml = (ROOT / "sandbox/lane.yml").read_text()
        parser = L.build_parser()
        sub = parser._subparsers._group_actions[0].choices
        calls = re.findall(r'super-board-lane-docker\.py\s+([\w-]+)((?:[^\n]*\\\n)*[^\n]*)', yml)
        seen = set()
        for name, rest in calls:
            self.assertIn(name, sub)
            seen.add(name)
            flags = set(re.findall(r"--[a-z-]+", rest))
            known = {o for act in sub[name]._actions for o in act.option_strings}
            self.assertLessEqual(flags, known, name)
            required = {act.option_strings[0] for act in sub[name]._actions if act.required}
            self.assertLessEqual(required, flags, f"{name} misses required flags")
        self.assertEqual(seen, {"stage", "run", "extract", "verify", "cleanup", "gate-verify", "exec-sandbox"})


# ---------------------------------------------------------------- CLI with a fake docker

class FakeContainment:
    """Stands in for DockerContainment in CLI tests: records start() argv and what the env-file held at that moment."""
    instances = []

    def __init__(self, nonce, docker=None):
        self.nonce = nonce
        self.started = []
        self.env_seen = {}
        FakeContainment.instances.append(self)

    def start(self, argv):
        argv = list(argv)
        name = argv[argv.index("--name") + 1]
        self.started.append(argv)
        if "--env-file" in argv:
            self.env_seen[name] = Path(argv[argv.index("--env-file") + 1]).read_text()
        return name

    wait_result = {"exit_code": 0, "timed_out": False}

    def wait(self, name, deadline_s, **kw):
        return dict(type(self).wait_result)

    def close(self):
        return []


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.mcl = self.t / "mcl"
        (self.mcl / "x").mkdir(parents=True)
        (self.mcl / "seed").mkdir()
        (self.mcl / "brief.txt").write_bytes(b"do the thing\n")
        self.cfg = self.mcl / "config.json"
        self.cfg.write_text(json.dumps(FULL_CFG))
        self.calls = []
        self.puts = []
        self.cp_sources = {}
        self.exec_rc, self.exec_out, self.exec_timeout, self.exec_calls = {}, {}, set(), []
        self.pass_no = 1                       # becomes 2 when the host re-stages /repo for the base run (A14)
        self.stage_rc = 0                      # the exit code of every `docker run ... stage` helper
        FakeContainment.instances = []
        FakeContainment.wait_result = {"exit_code": 0, "timed_out": False}

        def fake_docker(args, **kw):
            args = [str(a) for a in args]
            self.calls.append(args)
            if "input" in kw:
                self.puts.append((args, kw["input"]))
            if args[:3] == ["volume", "rm", "-f"] and args[3].endswith("-repo"):
                self.pass_no = 2
            if args[0] == "exec":
                key = "prepare" if "prepare" in args else args[-1]
                self.exec_calls.append((self.pass_no, key, kw.get("timeout")))
                if (self.pass_no, key) in self.exec_timeout:
                    raise subprocess.TimeoutExpired(args, kw.get("timeout"))
                return subprocess.CompletedProcess(args, self.exec_rc.get((self.pass_no, key), 0),
                                                   self.exec_out.get((self.pass_no, key), ""), "")
            if args[0] == "run" and "stage" in args:
                return subprocess.CompletedProcess(args, self.stage_rc, "", "")
            if args[0] == "cp":
                src = self.cp_sources.get(args[1].split(":", 1)[1])
                if src is None:
                    return subprocess.CompletedProcess(args, 1, "", "no such file")
                Path(args[2]).write_bytes(src)
            return subprocess.CompletedProcess(args, 0, "", "")

        self.patches = [mock.patch.object(L, "_docker", fake_docker),
                        mock.patch.object(L, "DockerContainment", FakeContainment),
                        mock.patch.dict(os.environ, {"RUNINFRA_GATEWAY_KEY": "sekrit"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def cli(self, sub, *extra):
        return L.main([sub, "--nonce", NONCE, "--config", str(self.cfg), "--tool", "pi", "--model", "runinfra/m",
                       "--lane", "build", "--timeout-min", "10", "--out-dir", str(self.mcl / "x"), *extra])

    def stage(self):
        return self.cli("stage", "--seed", str(self.mcl / "seed"), "--brief", str(self.mcl / "brief.txt"),
                        "--start-sha", SHA)

    def test_stage_builds_network_proxy_volumes_clones_and_brief(self):
        self.assertEqual(self.stage(), 0)
        flat = [" ".join(c) for c in self.calls]
        self.assertTrue(any(c.startswith("network create --internal") and f"mcl-{NONCE}-int" in c for c in flat))
        self.assertTrue(any(c == f"network connect bridge mcl-{NONCE}-proxy" for c in flat))
        for role in ("wt", "out", "pristine", "repo", "x"):
            self.assertIn(f"volume create mcl-{NONCE}-{role}", flat)
        clones = [c for c in self.calls if c[0] == "run" and "stage" in c]
        self.assertEqual(len(clones), 3)
        for c in clones:
            self.assertIn(f"{(self.mcl / 'seed').resolve()}:/seed:ro", c)
        proxy = FakeContainment.instances[0].started[0]
        self.assertIn(f"mcl-{NONCE}-proxy", proxy)
        conf = base64_conf(self.mcl / "state" / f"{NONCE}-proxy.env")
        self.assertIn("acl allow_hosts dstdomain api.runinfra.ai", conf)
        (args, data), = self.puts                                    # the brief travels on stdin, never argv
        self.assertIn(f"mcl-{NONCE}-out:/dst", args)
        self.assertNotIn("do the thing", " ".join(" ".join(c) for c in self.calls))
        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
            self.assertEqual(tf.extractfile("brief").read(), b"do the thing\n")
        self.assertEqual(json.loads((self.mcl / "state" / "stage.json").read_text())["start_sha"], SHA)

    def test_fake_tool_gets_no_proxy_and_no_route(self):
        self.assertEqual(L.main(["stage", "--nonce", NONCE, "--config", str(self.cfg), "--tool", "fake", "--model", "m",
                                 "--lane", "build", "--timeout-min", "10", "--out-dir", str(self.mcl / "x"),
                                 "--seed", str(self.mcl / "seed"), "--brief", str(self.mcl / "brief.txt"),
                                 "--start-sha", SHA]), 0)
        self.assertEqual(FakeContainment.instances[0].started, [])

    def test_run_writes_env_file_then_deletes_it_and_writes_run_json(self):
        FakeContainment.wait_result = {"exit_code": 7, "timed_out": False}
        self.assertEqual(self.cli("run"), 0)
        inst = FakeContainment.instances[0]
        env = inst.env_seen[f"mcl-{NONCE}-run"]
        for want in ("MCL_TOOL=pi", "MCL_MODEL=runinfra/m", "MCL_HARD_S=600", "RUNINFRA_GATEWAY_KEY=sekrit"):
            self.assertIn(want, env.splitlines())
        self.assertFalse(list((self.mcl / "state").glob("*.env")))                  # key file is gone
        self.assertEqual(json.loads((self.mcl / "state" / "run.json").read_text()), {"exit_code": 7, "timed_out": False})
        self.assertEqual(sorted(os.listdir(self.mcl / "x")), [])                    # nothing host-only is uploaded
        FakeContainment.wait_result = {"exit_code": 124, "timed_out": True}
        self.assertEqual(self.cli("run"), 0)
        self.assertTrue(json.loads((self.mcl / "state" / "run.json").read_text())["timed_out"])

    def test_run_refuses_bad_model_and_tool_before_docker(self):
        for args in (["--model", "a b"], ["--tool", "evil;x"]):
            argv = ["run", "--nonce", NONCE, "--config", str(self.cfg), "--tool", "pi", "--model", "m", "--lane", "build",
                    "--timeout-min", "10", "--out-dir", str(self.mcl / "x")]
            argv[argv.index(args[0]) + 1] = args[1]
            self.assertEqual(L.main(argv), 64)
        self.assertEqual(self.calls, [])

    @unittest.skipUnless(HAVE_GIT, "git needed")
    def test_extract_and_verify_leave_exactly_the_three_artifact_files(self):
        with tempfile.TemporaryDirectory() as d:
            c = ContractTests()
            start, bundle, _ = c._repo_with_change(d)
            self.cp_sources["/x/bundle.tar"] = bundle.read_bytes()
            (self.mcl / "state").mkdir(exist_ok=True)
            (self.mcl / "state" / "stage.json").write_text(json.dumps({"start_sha": start}))
            (self.mcl / "state" / "run.json").write_text(json.dumps({"exit_code": 0, "timed_out": False}))
            self.assertEqual(self.cli("extract", "--card", "7", "--attempt", "1"), 0)
            facts = json.loads((self.mcl / "x" / "facts.json").read_text())
            self.assertEqual((facts["nonce"], facts["start_sha"], facts["tool"], facts["card"]), (NONCE, start, "pi", 7))
            cp = [c for c in self.calls if c[0] == "cp"]
            self.assertEqual(cp, [["cp", f"mcl-{NONCE}-extract:/x/bundle.tar", str(self.mcl / "x" / "bundle.tar")]])
            # verify: host-judged, every command a docker exec; nothing is copied out of the verify container
            self.cp_sources["/x/verify.json"] = b"forged"
            self.assertEqual(self.cli("verify"), 0)
            v = json.loads((self.mcl / "x" / "verify.json").read_text())
            self.assertEqual((v["status"], [c["exit"] for c in v["commands"]]), ("ok", [0]))
            self.assertEqual([c for c in self.calls if c[0] == "cp"], cp)
            self.assertEqual(sorted(os.listdir(self.mcl / "x")), ["bundle.tar", "facts.json", "verify.json"])
            args, data = self.puts[-1]
            self.assertIn(f"mcl-{NONCE}-vin:/dst", args)
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                self.assertEqual(sorted(tf.getnames()), ["lane.patch"])
            vstart = FakeContainment.instances[-1].started[0]
            self.assertEqual(flag_value(vstart, "--network"), "none")
            self.assertEqual(flag_value(vstart, "--entrypoint"), "sleep")
            # the whole directory is accepted by the Surface
            z = Path(d) / "a.zip"
            with zipfile.ZipFile(z, "w") as zf:
                for n in os.listdir(self.mcl / "x"):
                    zf.write(self.mcl / "x" / n, n)
            dest = Path(d) / "dl"
            dest.mkdir()
            REMOTE.safe_extract(z, dest)
            REMOTE.validate(dest, NONCE, start)

    # -- host-judged verify (A13) and the base comparison (A14)

    def verify_cfg(self, cmds, setup=()):
        self.cfg.write_text(json.dumps(dict(FULL_CFG, verify_commands=list(cmds), verify_setup=list(setup))))

    def write_bundle(self, with_stage=True):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = b"diff --git a/b.txt b/b.txt\n"
            ti = tarfile.TarInfo("lane.patch")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        (self.mcl / "x" / "bundle.tar").write_bytes(buf.getvalue())
        (self.mcl / "state").mkdir(exist_ok=True)
        if with_stage:
            (self.mcl / "state" / "stage.json").write_text(json.dumps({"start_sha": SHA, "seed": str(self.mcl / "seed")}))

    def verdict(self):
        self.assertEqual(self.cli("verify"), 0)
        v = json.loads((self.mcl / "x" / "verify.json").read_text())
        REMOTE._check_verify(v)                                  # the Surface accepts everything the host writes
        return v

    def execs(self, pass_no):
        return [k for p, k, _t in self.exec_calls if p == pass_no]

    def test_forged_verify_json_in_the_container_cannot_change_the_verdict(self):
        self.write_bundle()
        forged_ok = json.dumps({"status": "ok", "commands": [{"cmd": "pytest -q", "exit": 0, "tail": "", "phase": "verify"}]})
        self.cp_sources["/x/verify.json"] = forged_ok.encode()
        self.verify_cfg(["pytest -q"])
        self.exec_rc = {(1, "pytest -q"): 1, (2, "pytest -q"): 0}          # fails on the patch, passes on the base
        v = self.verdict()
        self.assertEqual(v["status"], "verify_failed")
        self.assertEqual(v["commands"][0]["exit"], 1)
        self.assertFalse([c for c in self.calls if c[0] == "cp"])           # the host never reads the container's file
        # and the other way round: a forged failure cannot sink a passing run
        self.cp_sources["/x/verify.json"] = json.dumps({"status": "verify_failed", "commands": []}).encode()
        self.exec_rc = {}
        self.pass_no = 1
        self.assertEqual(self.verdict()["status"], "ok")

    def test_each_exit_code_is_the_docker_exec_result(self):
        self.write_bundle()
        self.verify_cfg(["lint", "test", "build"], setup=["make deps"])
        self.exec_rc = {(1, "test"): 3, (2, "test"): 0}
        self.exec_out = {(1, "test"): "ok\x1b[31m red\x00" + "x" * 5000}
        v = self.verdict()
        self.assertEqual(v["status"], "verify_failed")
        self.assertEqual([(c["phase"], c["exit"]) for c in v["commands"]],
                         [("setup", 0), ("verify", 0), ("verify", 3), ("verify", 0)])
        self.assertEqual(self.execs(1), ["prepare", "make deps", "lint", "test", "build"])
        self.assertEqual(self.execs(2), ["make deps", "test"])             # base: same setup, only the failing command
        failed = v["commands"][2]
        self.assertEqual(len(failed["tail"]), 4000)
        self.assertNotIn("\x1b", failed["tail"])
        self.assertNotIn("\x00", failed["tail"])
        self.assertIs(failed["inherited"], False)
        run = [c for c in self.calls if c[0] == "exec" and c[-1] == "lint"]
        self.assertEqual(run, [["exec", "-u", "1000:1000", "-w", "/repo", f"mcl-{NONCE}-verify", "sh", "-c", "lint"]])

    def test_deadline_is_the_hosts_rm_f_then_timeout(self):
        self.write_bundle()
        self.verify_cfg(["fast", "slow", "never"])
        self.exec_timeout = {(1, "slow")}
        v = self.verdict()
        self.assertEqual(v["status"], "timeout")
        self.assertEqual([(c["cmd"], c["exit"]) for c in v["commands"]], [("fast", 0), ("slow", 124)])
        self.assertNotIn("never", self.execs(1))
        self.assertGreaterEqual(self.calls.count(["rm", "-f", f"mcl-{NONCE}-verify"]), 1)
        self.assertEqual(self.pass_no, 1)                                   # a timeout is not compared with the base
        for _p, _k, t in self.exec_calls:
            self.assertTrue(0 < t <= 300, t)                                # verify_timeout_min=5 bounds every exec

    def test_deadline_already_spent_runs_nothing(self):
        (self.mcl / "state").mkdir(exist_ok=True)
        status, res = L.verify_pass(FULL_CFG, NONCE, DIGEST, self.mcl / "state", None, [], ["true"], 0)
        self.assertEqual(status, "timeout")
        self.assertEqual([(c["cmd"], c["exit"]) for c in res], [("true", 124)])
        self.assertEqual(self.exec_calls, [])

    def test_zero_commands_is_verify_failed_without_docker(self):
        self.write_bundle()
        self.verify_cfg([], setup=["make deps"])
        v = self.verdict()
        self.assertEqual((v["status"], v["commands"]), ("verify_failed", []))
        self.assertEqual(self.calls, [])

    def test_failed_setup_skips_the_commands(self):
        self.write_bundle()
        self.verify_cfg(["never run"], setup=["bad setup"])
        self.exec_rc = {(1, "bad setup"): 5}
        v = self.verdict()
        self.assertEqual(v["status"], "verify_failed")                      # base setup passes: the patch broke it
        self.assertEqual([(c["cmd"], c["phase"], c["exit"]) for c in v["commands"]], [("bad setup", "setup", 5)])
        self.assertNotIn("never run", self.execs(1) + self.execs(2))
        self.assertEqual(self.execs(2), ["bad setup"])

    def test_setup_broken_on_the_base_too_is_base_broken(self):
        self.write_bundle()
        self.verify_cfg(["never run"], setup=["bad setup"])
        self.exec_rc = {(1, "bad setup"): 5, (2, "bad setup"): 5}
        v = self.verdict()
        self.assertEqual(v["status"], "base_broken")
        self.assertNotIn("never run", self.execs(1) + self.execs(2))

    def test_failure_that_the_base_has_too_is_inherited(self):
        self.write_bundle()
        self.verify_cfg(["pytest -q"], setup=["pip install -e ."])
        self.exec_rc = {(1, "pytest -q"): 1, (2, "pytest -q"): 1}
        v = self.verdict()
        self.assertEqual(v["status"], "inherited")          # M6: never a plain ok
        self.assertIs(v["commands"][-1]["inherited"], True)
        self.assertEqual(v["notes"], ["inherited from base: pytest -q"])
        self.assertEqual(self.execs(2), ["pip install -e .", "pytest -q"])  # no prepare on the base: no patch
        flat = [" ".join(c) for c in self.calls]
        i = flat.index(f"volume rm -f mcl-{NONCE}-repo")                    # a fresh /repo staged at start_sha
        self.assertEqual(flat[i + 1], f"volume create mcl-{NONCE}-repo")
        st = self.calls[i + 2]
        self.assertEqual(st[:2], ["run", "--rm"])
        self.assertIn(f"mcl-{NONCE}-repo:/work", st)
        self.assertEqual(st[-1], SHA)
        self.assertEqual(FakeContainment.instances[1].started[0][-4:], ["--entrypoint", "sleep", DIGEST, "infinity"])

    def test_failure_the_base_does_not_have_is_verify_failed(self):
        self.write_bundle()
        self.verify_cfg(["pytest -q"])
        self.exec_rc = {(1, "pytest -q"): 1}
        v = self.verdict()
        self.assertEqual(v["status"], "verify_failed")
        self.assertIs(v["commands"][-1]["inherited"], False)
        self.assertEqual(v["base"]["commands"][0]["exit"], 0)

    def test_one_new_failure_among_inherited_ones_is_verify_failed(self):
        self.write_bundle()
        self.verify_cfg(["a", "b", "c"])
        self.exec_rc = {(1, "a"): 1, (2, "a"): 1, (1, "b"): 1, (2, "b"): 0}
        v = self.verdict()
        self.assertEqual(v["status"], "verify_failed")
        self.assertEqual([c.get("inherited") for c in v["commands"]], [True, False, None])
        self.assertEqual(self.execs(2), ["a", "b"])

    def test_apply_failure_is_final_and_never_runs_a_command(self):
        self.write_bundle()
        self.verify_cfg(["pytest -q"])
        self.exec_rc = {(1, "prepare"): 1}
        self.exec_out = {(1, "prepare"): json.dumps({"applied": False, "error": "patch does not apply"}) + "\n"}
        v = self.verdict()
        self.assertEqual(v["status"], "apply_failed")
        self.assertEqual(v["commands"][0]["tail"], "patch does not apply")
        self.assertEqual(self.execs(1), ["prepare"])
        self.assertEqual(self.pass_no, 1)

    def test_base_check_that_cannot_run_never_turns_a_failure_into_ok(self):
        self.write_bundle(with_stage=False)
        self.verify_cfg(["pytest -q"])
        self.exec_rc = {(1, "pytest -q"): 1}
        v = self.verdict()
        self.assertEqual(v["status"], "verify_failed")
        self.assertTrue(v["notes"][0].startswith("base check unavailable"))

    def test_extract_with_tampered_bundle_fails(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            ti = tarfile.TarInfo("../evil")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"x"))
        self.cp_sources["/x/bundle.tar"] = buf.getvalue()
        self.assertEqual(self.cli("extract"), 64)                    # ValueError from validate_tar, nothing written
        self.assertFalse((self.mcl / "x" / "facts.json").exists())

    # -- gate-verify: the one implementation of the merge gate's verify (spec section 10)

    def gate(self, *extra, start=SHA):
        return L.main(["gate-verify", "--nonce", NONCE, "--config", str(self.cfg), "--out-dir", str(self.mcl / "x"),
                       "--seed", str(self.mcl / "seed"), "--start-sha", start, *extra])

    def gate_doc(self):
        v = json.loads((self.mcl / "x" / "verify.json").read_text())
        REMOTE._check_verify(v)                                  # the Surface accepts everything the host writes
        return v

    def test_gate_verify_stages_the_merge_and_runs_each_command_as_an_exec_with_no_patch(self):
        self.verify_cfg(["lint", "test"], setup=["make deps"])
        base = "c" * 40
        self.assertEqual(self.gate("--merge-sha", base), 0)
        stages = [c for c in self.calls if c[0] == "run" and "stage" in c]
        self.assertEqual(len(stages), 1)                                       # /repo only: no wt, no pristine, no out
        self.assertIn(f"mcl-{NONCE}-repo:/work", stages[0])
        self.assertEqual(stages[0][stages[0].index("--sha") + 1], SHA)
        self.assertEqual(stages[0][stages[0].index("--merge-sha") + 1], base)
        self.assertEqual(self.execs(1), ["make deps", "lint", "test"])         # no prepare: there is no patch
        self.assertFalse([c for c in self.calls if c[0] == "cp"])
        self.assertFalse([c for c in self.calls if c[:2] == ["network", "create"]])        # no tool network, no tool proxy
        v = self.gate_doc()
        self.assertEqual((v["status"], [c["exit"] for c in v["commands"]]), ("ok", [0, 0, 0]))
        self.assertEqual(sorted(os.listdir(self.mcl / "x")), ["verify.json"])

    def test_gate_verify_without_a_base_stages_the_head_as_is(self):
        self.verify_cfg(["test"])
        self.assertEqual(self.gate(), 0)
        stage = next(c for c in self.calls if c[0] == "run" and "stage" in c)
        self.assertNotIn("--merge-sha", stage)

    def test_gate_verify_failure_is_not_compared_with_the_base_here(self):
        self.verify_cfg(["test"])
        self.exec_rc = {(1, "test"): 2}
        self.assertEqual(self.gate(), 0)
        v = self.gate_doc()
        self.assertEqual(v["status"], "verify_failed")
        self.assertEqual(self.pass_no, 1)                                       # the caller asks again for the base commit
        self.assertNotIn("base", v)

    def test_gate_verify_a_failed_setup_is_verify_failed_and_skips_the_commands(self):
        self.verify_cfg(["never"], setup=["bad setup"])
        self.exec_rc = {(1, "bad setup"): 4}
        self.assertEqual(self.gate(), 0)
        self.assertEqual(self.gate_doc()["status"], "verify_failed")
        self.assertEqual(self.execs(1), ["bad setup"])

    def test_gate_verify_zero_commands_is_verify_failed_without_docker(self):
        self.verify_cfg([])
        self.assertEqual(self.gate(), 0)
        self.assertEqual(self.gate_doc()["status"], "verify_failed")
        self.assertEqual(self.calls, [])

    def test_gate_verify_merge_conflict_is_its_own_status_and_runs_nothing(self):
        self.verify_cfg(["test"])
        self.stage_rc = L.STAGE_MERGE_CONFLICT
        self.assertEqual(self.gate("--merge-sha", "c" * 40), 0)
        v = self.gate_doc()
        self.assertEqual((v["status"], v["commands"], v["notes"]), ("merge_conflict", [], ["merge conflict with base"]))
        self.assertEqual(self.exec_calls, [])

    def test_gate_verify_any_other_stage_failure_is_an_error_with_no_verdict(self):
        self.verify_cfg(["test"])
        self.stage_rc = 2
        self.assertEqual(self.gate("--merge-sha", "c" * 40), 70)
        self.assertFalse((self.mcl / "x" / "verify.json").exists())
        self.stage_rc = L.STAGE_MERGE_CONFLICT                                  # rc 3 without a merge request is not a conflict
        self.assertEqual(self.gate(), 70)

    def test_gate_verify_takes_a_real_image_on_local_but_a_tool_run_does_not(self):
        real = dict(FULL_CFG, where="local", image="mc-lane-ams@sha256:" + "c" * 64, verify_commands=["test"])
        self.cfg.write_text(json.dumps(real))
        with mock.patch.dict(os.environ, {"MC_DOCKER_SMOKE": ""}):
            self.assertEqual(self.gate(), 0)                                    # no smoke image, no MC_DOCKER_SMOKE needed
            self.assertEqual(self.gate_doc()["status"], "ok")
            self.assertEqual(self.cli("run"), 64)                               # A6 still binds tool runs
            self.assertEqual(self.stage(), 64)

    def test_gate_verify_refuses_malformed_input(self):
        self.verify_cfg(["test"])
        self.assertEqual(self.gate(start="zz"), 64)
        self.assertEqual(self.gate("--merge-sha", "nope"), 64)
        self.cfg.write_text(json.dumps(dict(FULL_CFG, image="mc-lane:latest", verify_commands=["t"])))
        self.assertEqual(self.gate(), 64)
        self.assertEqual(self.calls, [])

    def test_cleanup_calls_close(self):
        self.assertEqual(L.main(["cleanup", "--nonce", NONCE]), 0)
        self.assertEqual(L.main(["cleanup", "--nonce", "nope"]), 64)


class ExecSandboxCliTests(StubCase):
    """lane.yml's Exec step: exec-sandbox reads an argv list from a file and runs it with no shell."""

    def setUp(self):
        super().setUp()
        t = Path(self.tmp.name)
        self.seed, self.out_dir, self.cf = t / "seed", t / "o" / "x", t / "command.json"
        self.seed.mkdir()
        self.cfgf = t / "config.json"
        self.cfgf.write_text(json.dumps(dict(FULL_CFG, verify_network=False)))

    def cli(self, command):
        self.cf.write_text(command if isinstance(command, str) else json.dumps(command))
        return L.main(["exec-sandbox", "--nonce", NONCE, "--config", str(self.cfgf), "--seed", str(self.seed), "--start-sha", SHA,
                       "--command-file", str(self.cf), "--out-dir", str(self.out_dir)])

    def test_reads_the_list_runs_it_as_argv_and_writes_log_and_rc(self):
        self.script_rules([{"match": ["exec"], "rc": 3, "stdout": "boom\n"}])
        self.assertEqual(self.cli(["npm test && curl x", "-q"]), 0)                # the step succeeds whatever the rc
        ex, = [c for c in self.calls() if c[0] == "exec"]
        self.assertEqual(ex[6:], ["npm test && curl x", "-q"])
        self.assertEqual((self.out_dir / "exec.log").read_text(), "boom\n")
        self.assertEqual(json.loads((self.out_dir / "exec.json").read_text()), {"rc": 3})

    def test_a_bad_list_is_refused_with_64_and_nothing_runs(self):
        for bad in ('"npm test"', "[]", '["a", 1]', '["a", ""]', '["a$(b)"]', '["a`b`"]', '["caf\u00e9"]', '["a\u0007"]',
                    json.dumps(["x" * 5000]), "not json"):
            self.assertEqual(self.cli(bad), 64, bad)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.out_dir / "exec.json").exists())

    def raising(self, stderr="nope"):
        """Run cmd_exec_sandbox directly (main() would turn the raise into 70) with a docker that fails the volume create."""
        self.script_rules([{"match": ["volume", "create"], "rc": 1, "stderr": stderr}])
        self.cf.write_text(json.dumps(["pytest"]))
        ns = L.build_parser().parse_args(["exec-sandbox", "--nonce", NONCE, "--config", str(self.cfgf), "--seed", str(self.seed),
                                          "--start-sha", SHA, "--command-file", str(self.cf), "--out-dir", str(self.out_dir)])
        with self.assertRaises(RuntimeError):
            L.cmd_exec_sandbox(ns)
        return json.loads((self.out_dir / "exec.json").read_text())

    def test_exec_json_is_written_with_a_null_rc_when_the_run_raises_and_the_error_still_propagates(self):
        doc = self.raising()
        self.assertIsNone(doc["rc"])
        self.assertTrue(doc["error"].startswith("RuntimeError: "), doc)

    def test_the_error_text_loses_control_characters_and_is_capped(self):
        doc = self.raising("bad" + chr(27) + "[31m" + chr(10) + "x" * 600)
        self.assertLessEqual(len(doc["error"]), 200)
        self.assertFalse(any(ord(ch) < 32 or ord(ch) == 127 for ch in doc["error"]))

    def test_main_maps_the_raise_to_70_and_exec_json_exists(self):
        self.script_rules([{"match": ["volume", "create"], "rc": 1, "stderr": "nope"}])
        self.assertEqual(self.cli(["pytest"]), 70)
        self.assertIsNone(json.loads((self.out_dir / "exec.json").read_text())["rc"])


class StreamExecBoundTests(StubCase):
    """_stream_exec reads in bounded chunks: output with no newline cannot grow memory without limit."""

    def test_a_megabyte_with_no_newline_arrives_in_chunks_of_at_most_64k(self):
        self.script_rules([{"match": ["exec"], "rc": 0, "stdout": "x" * (1024 * 1024)}])
        out = []
        self.assertEqual(L._stream_exec(NONCE, ["exec", "c"], 60, out.append), 0)
        self.assertEqual(sum(len(c) for c in out), 1024 * 1024)
        self.assertLessEqual(max(len(c) for c in out), 65536)
        self.assertGreater(len(out), 1)

    def test_a_multibyte_character_split_across_chunks_survives(self):
        text = chr(0x2713) * 40000                                    # 120000 bytes: a chunk boundary falls inside a character
        self.script_rules([{"match": ["exec"], "rc": 0, "stdout": text}])
        out = []
        self.assertEqual(L._stream_exec(NONCE, ["exec", "c"], 60, out.append), 0)
        self.assertEqual("".join(out), text)


def base64_conf(env_path):
    import base64
    line = Path(env_path).read_text().strip()
    return base64.b64decode(line.split("=", 1)[1]).decode()


if __name__ == "__main__":
    unittest.main()
