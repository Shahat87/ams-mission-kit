#!/usr/bin/env bash
# Real-container smoke suite for the docker lane provider (Plan 1b Task 7; spec addendum sections 11 and 14).
#
#   MC_DOCKER_SMOKE=1 bash plugin/tests/smoke-lane-docker.sh
#
# Every scenario runs through the REAL path: scripts/super-board-lane-delegate.py run --provider docker --where local
# (stage -> run -> extract -> verify, all in containers) with the `fake` tool of the mc-lane-smoke image. The fake tool
# (tests/fixtures/lane_fake_tool.py, behaviours smoke_*) reports raw observations from INSIDE the container; this
# harness decides pass or fail and also checks the host side (canary grep over the whole --out tree, local bare
# "remote" refs before/after, lane-delegate verify verdicts). Real tools never run here (A6). The only network use is
# the S6/S18 allowed host (example.com) through the egress proxy. Nothing is ever pushed to GitHub: the "remote" is a
# bare repository the harness creates in a temp dir.
#
# Prints `Sx PASS` or `Sx FAIL: <reason>` per scenario and exits 1 if any FAIL. S9 (three Codex runs in a row) is
# manual on the Lenovo; S15 and S17 are unit tests (tests/test_lane_remote.py). Without MC_DOCKER_SMOKE=1 it prints
# `SKIP (MC_DOCKER_SMOKE not set)` and exits 0. A trap removes every mcl-<nonce> container, volume and network this
# run could have left behind (never the mcl-cred-* credential volumes).
#
# S19 (a detached child is gone after teardown and the next run is clean) and S20 (escape attempts) were added in Plan 1c
# Task 10. MCL_SMOKE_ONLY=S19,S20 runs just those two (plus CLEANUP); unset runs every scenario. Only S19 and S20 can be
# selected: the older scenarios share runs, so filtering them one by one is not supported.
set -u

if [ "${MC_DOCKER_SMOKE:-}" != "1" ]; then
  echo "SKIP (MC_DOCKER_SMOKE not set)"
  exit 0
fi

export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
if ! command -v docker >/dev/null 2>&1 && [ -x "/c/Program Files/Docker/Docker/resources/bin/docker.exe" ]; then
  PATH="/c/Program Files/Docker/Docker/resources/bin:$PATH"; export PATH
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "S0 FAIL: docker not found (install Docker Desktop / Engine)"
  exit 1
fi

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "S0 FAIL: python 3.9+ not found"; exit 1; fi

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin="$(cd "$here/.." && (pwd -W 2>/dev/null || pwd))"
tmp="$(mktemp -d)"

cleanup_docker() {
  local n
  docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E '^mcl-[0-9a-f]{32}-' | while read -r n; do docker rm -f "$n" >/dev/null 2>&1; done
  docker network ls --format '{{.Name}}' 2>/dev/null | grep -E '^mcl-[0-9a-f]{32}-' | while read -r n; do docker network rm "$n" >/dev/null 2>&1; done
  docker volume ls --format '{{.Name}}' 2>/dev/null | grep -E '^mcl-[0-9a-f]{32}-' | while read -r n; do docker volume rm -f "$n" >/dev/null 2>&1; done
}
cleanup() { cleanup_docker; rm -rf "$tmp"; }
trap cleanup EXIT
trap 'exit 130' INT TERM

cat > "$tmp/smoke.py" <<'PYEOF'
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN = Path(os.environ["MCL_PLUGIN_DIR"])
DELEGATE = PLUGIN / "scripts" / "super-board-lane-delegate.py"
DOCKER = shutil.which("docker")
PY = sys.executable
WORK = Path(tempfile.mkdtemp(prefix="mcl-smoke-"))
RES_RE = re.compile(r"^mcl-[0-9a-f]{32}-")
ORDER = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S10", "S11", "S12", "S13", "S14", "S16", "S18", "S19", "S20", "G1", "X1", "CLEANUP"]
ONLY = [x.strip() for x in os.environ.get("MCL_SMOKE_ONLY", "").split(",") if x.strip()]
fails = {}
seen = set()
notes = []


def chk(sid, cond, msg):
    seen.add(sid)
    if not cond:
        fails.setdefault(sid, []).append(msg)


def note(msg):
    print("# " + msg, flush=True)


def dk(*args, timeout=120, inp=None):
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, timeout=timeout, input=inp, encoding="utf-8", errors="replace")


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", "-C", str(cwd), "-c", "user.email=smoke@example.com", "-c", "user.name=smoke", "-c", "core.autocrlf=false", *args],
                       capture_output=True, text=True, env=env, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), r.stderr[-300:]))
    return r.stdout.strip()


def rmtree(p):
    def fix(func, path, _exc):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(p, onexc=fix)
    else:
        shutil.rmtree(p, onerror=fix)


def leftovers():
    out = []
    for kind, cmd in (("container", ["ps", "-a", "--format", "{{.Names}}"]), ("network", ["network", "ls", "--format", "{{.Name}}"]),
                      ("volume", ["volume", "ls", "--format", "{{.Name}}"])):
        r = dk(*cmd)
        out += [(kind, n) for n in r.stdout.split() if RES_RE.match(n)]
    return out


def force_clean():
    for kind, n in leftovers():
        dk(*{"container": ["rm", "-f", n], "network": ["network", "rm", n], "volume": ["volume", "rm", "-f", n]}[kind])


def image_ref(name):
    r = dk("image", "inspect", "--format", "{{.Id}}", name)
    if r.returncode != 0:
        note("image %s missing: building with sandbox/build.sh" % name)
        b = subprocess.run(["bash", str(PLUGIN / "sandbox" / "build.sh")], capture_output=True, text=True, timeout=3600)
        if b.returncode != 0:
            raise RuntimeError("build.sh failed: " + b.stderr[-300:])
        r = dk("image", "inspect", "--format", "{{.Id}}", name)
    ident = r.stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", ident):
        raise RuntimeError("bad image id for %s: %r" % (name, ident))
    return "%s@%s" % (name, ident)


# ---------------------------------------------------------------- canaries and the card repository

def hx():
    return secrets.token_hex(16)


FILE_CANARY, UNTRACKED_CANARY, IGNORED_CANARY, REMOTE_TOKEN = ("smoke-file-" + hx(), "smoke-untracked-" + hx(), "smoke-ignored-" + hx(), "smoke-remote-token-" + hx())
ENV_CANARY = {k: "smoke-%s-%s" % (k.lower(), secrets.token_hex(8)) for k in (
    "AMS_OPERATOR_EXPECTED", "AMS_OPERATOR_DATA_ROOT", "SAP_LIVE_TESTS", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_CACHE_URL", "RUNNER_TEMP", "RUNNER_TOOL_CACHE", "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_ACTIONS", "LANE_REPO_TOKEN", "GH_CONFIG_DIR",
    "OPENAI_API_KEY", "XAI_API_KEY", "RUNINFRA_GATEWAY_KEY")}
ALL_SECRETS = [("file canary", FILE_CANARY), ("untracked canary", UNTRACKED_CANARY), ("ignored canary", IGNORED_CANARY),
               ("remote token", REMOTE_TOKEN)] + [("env " + k, v) for k, v in ENV_CANARY.items()]
CANARY_DIR = WORK / "canaries"
CANARY_DIR.mkdir()
CANARY_FILE = CANARY_DIR / ("canary-" + secrets.token_hex(6))
CANARY_FILE.write_text(FILE_CANARY)


def path_forms(path):
    """The host path of the canary as a container could try to reach it (Docker Desktop and Engine layouts)."""
    s = str(path)
    posix = Path(path).as_posix()
    m = re.match(r"^([A-Za-z]):(/.*)$", posix)
    forms = [s, posix]
    if m:
        d, rest = m.group(1).lower(), m.group(2)
        forms += ["/" + d + rest, "/mnt/" + d + rest, "/run/desktop/mnt/host/" + d + rest, "/host/" + d + rest,
                  "/host_mnt/" + d + rest, "/mnt/host/" + d + rest, "/mnt/wsl/" + d + rest]
    else:
        forms += ["/host" + posix, "/mnt/host" + posix, "/run/desktop/mnt/host" + posix, "/host_mnt" + posix, "/mnt" + posix]
    return sorted(set(forms))


PKG = {"name": "smoke", "version": "1.0.0", "scripts": {"test": "echo ok"}}
VPROBE = r'''
import json, os, socket, ssl
def st(name):
    for l in open("/proc/self/status"):
        if l.startswith(name + ":"):
            return l.split(":", 1)[1].strip()
def tcp(h, p, t=3):
    try:
        socket.create_connection((h, p), timeout=t).close()
        return "connected"
    except OSError as e:
        return type(e).__name__
def res(n):
    try:
        return "resolved:" + socket.gethostbyname(n)
    except OSError:
        return "fail"
def via(host, port):
    from urllib.parse import urlparse
    p = urlparse(os.environ["HTTPS_PROXY"])
    try:
        s = socket.create_connection((p.hostname, p.port or 3128), timeout=20)
        s.sendall(("CONNECT %s:%s HTTP/1.1\r\nHost: %s:%s\r\n\r\n" % (host, port, host, port)).encode())
        h = s.recv(300).decode("latin-1")
        s.close()
        return h.split(" ")[1] if h.startswith("HTTP/") else h[:30]
    except Exception as e:
        return "ERR:" + type(e).__name__
d = {"uid": os.getuid(), "cap": st("CapEff"), "env": sorted(os.environ),
     "dns": {n: res(n) for n in ("example.com", "host.docker.internal", "canary-x.example.net")},
     "tcp": {"%s:%s" % hp: tcp(*hp) for hp in (("host.docker.internal", 443), ("10.0.0.1", 443), ("169.254.169.254", 80), ("192.168.65.254", 80),
                                              ("172.17.0.1", 80), ("1.1.1.1", 443))},
     "ifaces": sorted(l.split(":")[0].strip() for l in open("/proc/net/dev").read().splitlines()[2:])}
if os.environ.get("HTTPS_PROXY"):
    d["proxy"] = {h: via(h, 443) for h in ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "example.com", "1.1.1.1", "host.docker.internal", "169.254.169.254")}
print("VOBS " + json.dumps(d, sort_keys=True))
'''


def make_card_repo(d):
    """A fresh card worktree (git repo with one commit) plus a local bare 'remote' the harness owns."""
    repo, bare = d / "card", d / "remote.git"
    repo.mkdir(parents=True)
    git(d, "init", "-q", "--bare", "-b", "main", str(bare))
    git(repo, "init", "-q", "-b", "main")
    (repo / "kwd.py").write_text("def f():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_kwd.py").write_text("def test_f():\n    assert True\n")
    (repo / "package.json").write_text(json.dumps(PKG, indent=2) + "\n")
    (repo / "probes").mkdir()
    (repo / "probes" / "vprobe.py").write_text(VPROBE)
    (repo / ".gitignore").write_text("ignored-secret.log\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", bare.as_posix())
    git(repo, "remote", "add", "upstream", "https://x-access-token:%s@github.com/owner/product.git" % REMOTE_TOKEN)
    git(repo, "push", "-q", "origin", "HEAD:main")
    git(repo, "branch", "issue-1-x")
    git(repo, "push", "-q", "origin", "issue-1-x")
    (repo / "untracked-secret.txt").write_text(UNTRACKED_CANARY)
    (repo / "ignored-secret.log").write_text(IGNORED_CANARY)
    return repo, bare, git(repo, "rev-parse", "HEAD")


def refs(*repos):
    return "\n".join(git(r, "for-each-ref", "--format=%(refname) %(objectname)") for r in repos)


class Run:
    def __init__(self, tag):
        self.tag, self.err, self.rc, self.obs = tag, None, None, {}
        self.result, self.vsandbox, self.out = None, None, None
        self.inspect, self.extra = {}, {}

    def need(self, *sids):
        if self.err:
            for s in sids:
                chk(s, False, "run %s: %s" % (self.tag, self.err))
        return not self.err


def write_cfg(path, behaviour, image, proxy, egress, verify_cmds, vnet, d):
    cfg = {"verify_commands": verify_cmds, "merge_policy": {"trusted_approvers": ["example-org"]},
           "pilot": {"tools": [{"id": "fake", "via": "fake", "model": behaviour, "egress": egress, "command": ["fake"]}],
                     "timeouts_min": {"build": 5, "qa": 5, "review": 5}, "test_globs": ["tests/**"],
                     "protected_paths": [".github/", ".claude/", ".git", "conftest.py", "*.lock"], "verify_setup": [], "verify_timeout_min": 5,
                     "state_path": str(d / "state.json"), "audit_dir": str(d / "audit"),
                     "sandbox": {"provider": "docker", "where": "local", "image": image, "proxy_image": proxy, "repos": ["owner/product"],
                                 "limits": {"memory": "2g", "cpus": "2", "pids": 256}, "verify_network": vnet}}}
    (d / "state.json").write_text("{}")
    path.write_text(json.dumps(cfg))


def spawn_env(extra=None):
    env = dict(os.environ)
    env.update(ENV_CANARY)
    env.update({"MC_DOCKER_SMOKE": "1", "PYTHONUTF8": "1"})
    env.update(extra or {})
    return env


def lane_run(tag, behaviour, brief, images, egress=(), vnet=False, verify_cmds=None, hook=None, timeout=900, expect_ok=True):
    run = Run(tag)
    d = WORK / tag
    d.mkdir()
    run.dir = d
    note("run %s (%s) ..." % (tag, behaviour))
    t0 = time.time()
    try:
        repo, bare, start = make_card_repo(d)
        run.repo, run.bare, run.start = repo, bare, start
        run.refs_before = refs(bare, repo)
        cfgp, briefp, out = d / "board.json", d / "brief.json", d / "out"
        write_cfg(cfgp, behaviour, images[0], images[1], list(egress), verify_cmds or ["python3 -c pass"], vnet, d)
        brief = dict(brief)
        brief.setdefault("canary_paths", path_forms(CANARY_FILE))
        brief.setdefault("canary_name", CANARY_FILE.name)
        brief.setdefault("untracked", ["untracked-secret.txt", "ignored-secret.log"])
        brief.setdefault("needles", [s[:22] for s in (FILE_CANARY, UNTRACKED_CANARY, IGNORED_CANARY, REMOTE_TOKEN)])
        brief.setdefault("bare_repo", str(bare))
        brief.setdefault("bare_repo_posix", Path(bare).as_posix())
        brief.setdefault("canary_domain", "canary-%s.example.com" % secrets.token_hex(4))
        brief.setdefault("host_sentinel", str(d / "HOST_SENTINEL"))
        briefp.write_text(json.dumps(brief))
        run.nonce, run.out, run.cfg = secrets.token_hex(8), out, cfgp
        cmd = [PY, str(DELEGATE), "run", "--config", str(cfgp), "--tool", "fake", "--worktree", str(repo), "--start", start, "--branch", "issue-1-x",
               "--repo", str(repo), "--brief", str(briefp), "--lane", "build", "--out", str(out), "--card", "1", "--nonce", run.nonce]
        so, se = open(d / "stdout.txt", "wb"), open(d / "stderr.txt", "wb")
        p = subprocess.Popen(cmd, env=spawn_env(), stdout=so, stderr=se, stdin=subprocess.DEVNULL)
        try:
            if hook:
                hook(p, run)
            p.wait(timeout=max(30, timeout - (time.time() - t0)))
        except subprocess.TimeoutExpired:
            p.kill()
            run.err = "lane-delegate run timed out after %ds" % timeout
        finally:
            so.close()
            se.close()
        run.rc = p.returncode
        run.stdout = (d / "stdout.txt").read_text(errors="replace")
        run.stderr = (d / "stderr.txt").read_text(errors="replace")
        run.refs_after = refs(bare, repo)
        raw = out / "raw.log"
        if raw.exists():
            for line in raw.read_text(errors="replace").splitlines():
                if line.startswith("OBS "):
                    try:
                        o = json.loads(line[4:])
                        run.obs[o["k"]] = o["v"]
                    except (ValueError, KeyError):
                        pass
        if (out / "result.json").exists():
            run.result = json.loads((out / "result.json").read_text())
        if (out / "verify.json").exists():
            try:
                run.vsandbox = json.loads((out / "verify.json").read_text())
            except ValueError:
                pass
        if not run.err and expect_ok and run.rc != 0:
            run.err = "lane-delegate run exited %s: %s" % (run.rc, (run.stderr or run.stdout).strip()[-400:])
        if not run.err and expect_ok and not (run.result and run.result.get("kind") == "ok"):
            run.err = "result kind is %r, wanted ok (stderr: %s)" % ((run.result or {}).get("kind"), run.stderr.strip()[-300:])
    except Exception as e:
        run.err = "%s: %s" % (type(e).__name__, e)
    left = leftovers()
    run.left = left
    chk("CLEANUP", not left, "run %s left %s" % (tag, left))
    force_clean()
    note("run %s done in %ds rc=%s%s" % (tag, time.time() - t0, run.rc, (" ERR " + run.err) if run.err else ""))
    return run


GH_STUB = """import json, os, sys
a = sys.argv[1:]
if a[:3] == ["api", "--paginate", "--slurp"]:
    allow = json.loads(os.environ.get("MC_STUB_ALLOW", "[]"))
    body = "".join("allow-path: " + p + chr(10) for p in allow)
    print(json.dumps([[{"user": {"login": "example-org", "type": "User"}, "body": body, "created_at": "2999-01-01T00:00:00Z",
                        "updated_at": "2999-01-01T00:00:00Z"}] if allow else []]))
elif a[:1] == ["api"] and "/collaborators/" in a[1]:
    print(json.dumps({"permission": "admin"}))
elif a[:2] == ["api", "user"]:
    print(json.dumps({"login": "robot"}))
else:
    sys.exit(97)
"""


def lane_verify(run, lane="build", allow="[]"):
    """allow: paths a trusted approver allowed on the card's issue, served by a stub gh (verify never takes them from argv)."""
    stub = WORK / "gh_stub.py"
    stub.write_text(GH_STUB)
    env = spawn_env({"MC_LANE_GH_CMD": json.dumps([PY, str(stub)]), "MC_STUB_ALLOW": allow})
    p = subprocess.run([PY, str(DELEGATE), "verify", "--config", str(run.cfg), "--out", str(run.out), "--worktree", str(run.repo), "--start", run.start,
                        "--lane", lane, "--nonce", run.nonce, "--message", "smoke: verified patch"], capture_output=True, text=True, env=env,
                       encoding="utf-8", errors="replace", timeout=300)
    try:
        doc = json.loads((run.out / "verify.json").read_text())
    except (OSError, ValueError):
        doc = {}
    return p.returncode, doc, p


def leak_hits(run):
    hits = []
    texts = [("lane-delegate stdout", run.stdout.encode()), ("lane-delegate stderr", run.stderr.encode())]
    for f in sorted(run.out.rglob("*")) if run.out.exists() else []:
        if f.is_file():
            texts.append((str(f.relative_to(run.out)), f.read_bytes()))
    for where, data in texts:
        for name, val in ALL_SECRETS:
            if val.encode() in data:
                hits.append("%s in %s" % (name, where))
    return hits


def vobs(run):
    """The probe line the verify container printed (tail of the first verify command)."""
    try:
        for c in (run.vsandbox or {}).get("commands", []):
            m = re.search(r"VOBS (\{.*\})", c.get("tail", ""))
            if m:
                return json.loads(m.group(1))
    except ValueError:
        pass
    return None


def patch_text(run):
    f = run.out / "lane.patch"
    return f.read_text(errors="replace") if f.exists() else ""


def names_with(d_names, wanted):
    return sorted(set(d_names) & set(wanted))


# ---------------------------------------------------------------- hooks (run while lane-delegate is alive)

def wait_container(suffix, p, seconds=240, exact_proxy=False):
    end = time.time() + seconds
    while time.time() < end and p.poll() is None:
        r = dk("ps", "--format", "{{.Names}}")
        for n in r.stdout.split():
            if re.fullmatch(r"mcl-[0-9a-f]{32}-" + suffix, n):
                return n
        time.sleep(1)
    return None


PROXY_PROBE = r'''
import json, os, socket
def req(host, port, method="CONNECT", url=None):
    try:
        s = socket.create_connection(("127.0.0.1", 3128), timeout=30)
        if url:
            s.sendall(("GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n" % url).encode())
        elif method == "CONNECT":
            s.sendall(("CONNECT %s:%s HTTP/1.1\r\nHost: %s:%s\r\n\r\n" % (host, port, host, port)).encode())
        h = s.recv(300).decode("latin-1")
        s.close()
        return h.split(" ")[1] if h.startswith("HTTP/") and " " in h else h[:60]
    except Exception as e:
        return "ERR:" + type(e).__name__
def cap():
    for l in open("/proc/self/status"):
        if l.startswith("CapEff"):
            return l.split()[1]
def direct(h, p):
    try:
        socket.create_connection((h, p), timeout=6).close()
        return "connected"
    except OSError as e:
        return type(e).__name__
d = {"uid": os.getuid(), "cap": cap()}
try:
    open("/smoke-probe", "w").close()
    d["rootfs_write"] = "WROTE"
except OSError as e:
    d["rootfs_write"] = type(e).__name__
d["connect"] = {h: req(h.split("|")[0], int(h.split("|")[1])) for h in (
    "example.com|443", "example.org|443", "1.1.1.1|443", "8.8.8.8|443", "api.github.com|443", "github.com|443", "host.docker.internal|443",
    "gateway.docker.internal|443", "169.254.169.254|443", "10.0.0.1|443", "192.168.65.254|443", "172.17.0.1|443", "localhost|443", "127.0.0.1|443",
    "example.com|80", "example.com|8443")}
d["get"] = {u: req(None, None, url=u) for u in ("http://example.com/", "http://127.0.0.1:3128/squid-internal-mgr/info", "cache_object://127.0.0.1/info")}
d["direct_info"] = {"1.1.1.1:443": direct("1.1.1.1", 443)}
print(json.dumps(d))
'''


def inspect_json(name):
    r = dk("inspect", name)
    try:
        return json.loads(r.stdout)[0]
    except (ValueError, IndexError):
        return {}


def hook_egress(p, run):
    proxy = wait_container(r"proxy", p)
    runc = wait_container(r"run", p)
    if not proxy or not runc:
        run.extra["hook_error"] = "proxy or run container never appeared (%s, %s)" % (proxy, runc)
        return
    run.extra["proxy_inspect"] = inspect_json(proxy)
    ri = inspect_json(runc)
    ri.get("Config", {}).pop("Env", None)          # the env-file values are not for logs
    run.extra["run_inspect"] = ri
    probe = None
    for _ in range(40):                            # squid needs a moment; retry until the allowed host connects
        r = dk("exec", "-i", proxy, "python3", "-", inp=PROXY_PROBE, timeout=600)
        try:
            probe = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            probe = {"error": (r.stderr or r.stdout)[-300:]}
        if probe.get("connect", {}).get("example.com|443") == "200":
            break
        time.sleep(3)
    run.extra["proxy_probe"] = probe
    dk("exec", "-u", "1000", runc, "touch", "/tmp/smoke_done")        # releases the tool


def hook_fork(p, run):
    runc = wait_container(r"run", p)
    if not runc:
        run.extra["hook_error"] = "run container never appeared"
        return
    pids, end = [], time.time() + 150
    while time.time() < end and p.poll() is None:
        r = dk("stats", "--no-stream", "--format", "{{.PIDs}}", runc, timeout=60)
        try:
            pids.append(int(r.stdout.strip()))
        except ValueError:
            pass
        if pids and pids[-1] >= 200:
            time.sleep(3)
            r = dk("stats", "--no-stream", "--format", "{{.PIDs}}", runc, timeout=60)
            try:
                pids.append(int(r.stdout.strip()))
            except ValueError:
                pass
            break
        time.sleep(1)
    run.extra["pids"] = pids
    t0 = time.time()
    r = dk("rm", "-f", runc, timeout=120)
    run.extra["kill_s"] = time.time() - t0
    run.extra["kill_rc"] = r.returncode
    run.extra["gone"] = runc not in dk("ps", "-a", "--format", "{{.Names}}").stdout.split()


# ---------------------------------------------------------------- main

LANE_DOCKER = PLUGIN / "scripts" / "super-board-lane-docker.py"


def g1_seed(d, head_files, base_files):
    """A bare seed holding refs/mc/start (the PR head) and refs/mc/base (the base), both moved on from one commit."""
    repo, seed = d / "repo", d / "seed.git"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("common\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "common")
    common = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "base")
    for name, text in base_files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-q", "-b", "head", common)
    for name, text in head_files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "head")
    git(d, "init", "-q", "--bare", str(seed))
    git(repo, "push", "-q", str(seed), "head:refs/mc/start", "base:refs/mc/base")
    return seed, git(repo, "rev-parse", "head"), git(repo, "rev-parse", "base")


def g1_gate(d, images, seed, head, base, cmds):
    """lane-docker gate-verify for real (the function the merge gate and lane.yml both call). base=None: no merge."""
    nonce = hx()
    d.mkdir(parents=True, exist_ok=True)
    cfgp, out = d / "gate.json", d / "x"
    cfgp.write_text(json.dumps({"image": images[0], "proxy_image": images[1], "where": "local", "verify_setup": [],
                                "verify_commands": cmds, "verify_timeout_min": 3, "limits": {"memory": "2g", "cpus": "2", "pids": 256}}))
    argv = [PY, str(LANE_DOCKER), "gate-verify", "--nonce", nonce, "--config", str(cfgp), "--out-dir", str(out), "--seed", str(seed),
            "--start-sha", head] + (["--merge-sha", base] if base else [])
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
    finally:
        subprocess.run([PY, str(LANE_DOCKER), "cleanup", "--nonce", nonce], capture_output=True, text=True, timeout=120)
    doc = json.loads((out / "verify.json").read_text()) if (out / "verify.json").exists() else None
    return r.returncode, doc, (r.stderr or "")[-300:]


def scenario_g1(images):
    """G1: the gate verifies the MERGED tree. The check passes on the head alone and fails once the base is merged in
    (a file the base adds is exactly what the check forbids); a conflicting base is its own status."""
    d = WORK / "g1"
    d.mkdir()
    forbids_base = "test -f head.txt && test ! -f base.txt"
    seed, head, base = g1_seed(d / "clean", {"head.txt": "h\n"}, {"base.txt": "b\n"})
    rc, doc, err = g1_gate(d / "alone", images, seed, head, None, [forbids_base])
    chk("G1", rc == 0 and doc and doc["status"] == "ok", "head alone must pass (rc=%s %s %s)" % (rc, doc and doc["status"], err))
    rc, doc, err = g1_gate(d / "merged", images, seed, head, base, [forbids_base])
    chk("G1", rc == 0 and doc and doc["status"] == "verify_failed" and [c["exit"] for c in doc["commands"]] == [1],
        "the merged tree must fail the check (rc=%s %s %s)" % (rc, doc, err))
    rc, doc, err = g1_gate(d / "both", images, seed, head, base, ["test -f head.txt && test -f base.txt && git log -1 --format=%p | wc -w | grep -qx 2"])
    chk("G1", rc == 0 and doc and doc["status"] == "ok", "head AND base files, and a two-parent merge commit, must be in /repo (rc=%s %s %s)" % (rc, doc, err))
    seed, head, base = g1_seed(d / "conflict", {"a.txt": "head side\n"}, {"a.txt": "base side\n"})
    rc, doc, err = g1_gate(d / "cfl", images, seed, head, base, ["true"])
    chk("G1", rc == 0 and doc and doc["status"] == "merge_conflict" and doc["commands"] == [],
        "a conflicting base must report merge_conflict and run nothing (rc=%s %s %s)" % (rc, doc, err))
    note("G1 done")


def scenario_x1(iso):
    """X1: `exec --sandbox` runs the command in a fresh verify container, never on the host. The command tries to write a
    host path; the marker must be absent on the host. It also proves the command saw HEAD as uid 1000."""
    if iso.err:
        chk("X1", False, "run iso: %s" % iso.err)
        return
    clone = WORK / "x1repo"
    subprocess.run(["git", "clone", "-q", str(iso.repo), str(clone)], check=True, capture_output=True)
    nulldev = "NUL" if os.name == "nt" else "/dev/null"
    guard = WORK / "x1-guarded-repos.json"                               # D-5: an empty list, so this repo is unguarded and runs locally
    guard.write_text(json.dumps({"version": 1, "repos": []}))
    x1_env = dict(spawn_env(), MC_GUARDED_REPOS_FILE=str(guard))
    refused = subprocess.run([PY, str(DELEGATE), "exec", "--sandbox", "--config", str(iso.cfg), "--dir", str(clone), "--", "true"],
                             capture_output=True, text=True, env=x1_env, encoding="utf-8", errors="replace", timeout=600)
    chk("X1", refused.returncode == 4, "exec --sandbox on a worktree that would run repo hooks must exit 4, got %s" % refused.returncode)
    git(clone, "config", "extensions.worktreeConfig", "true")           # what `run` does to every card worktree
    git(clone, "config", "--worktree", "core.hooksPath", nulldev)
    head = git(clone, "rev-parse", "HEAD")
    marker = WORK / "x1-host-marker"
    code = "import os; open(r'%s', 'w').write('x'); print('wrote')" % marker
    cmd = "id -u && test \"$(git rev-parse HEAD)\" = %s && python3 -c \"%s\"; echo done" % (head, code)
    p = subprocess.run([PY, str(DELEGATE), "exec", "--sandbox", "--config", str(iso.cfg), "--dir", str(clone), "--", cmd],
                       capture_output=True, text=True, env=x1_env, encoding="utf-8", errors="replace", timeout=600)
    chk("X1", not marker.exists(), "the command wrote a marker on the HOST: %s" % marker)
    chk("X1", "1000" in p.stdout.split() and "done" in p.stdout, "exec --sandbox did not run in the container as uid 1000 at HEAD: rc=%s out=%r err=%r" %
        (p.returncode, p.stdout[-300:], p.stderr[-300:]))
    chk("X1", not leftovers(), "exec --sandbox left resources: %s" % leftovers())
    note("X1 done rc=%s" % p.returncode)


def host_procs_with(marker):
    """PIDs of host processes whose command line carries marker, or None if the host could not be listed. The marker
    travels in the environment (Windows) or is absent from this command's own argv (ps), so the probe never matches itself."""
    if os.name == "nt":
        ps = "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like ('*' + $env:MCL_PAT + '*') } | ForEach-Object { $_.ProcessId }"
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                           env=dict(os.environ, MCL_PAT=marker), timeout=120, encoding="utf-8", errors="replace")
        return r.stdout.split() if r.returncode == 0 else None
    r = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    return [ln.split()[0] for ln in r.stdout.splitlines() if marker in ln and ln.split()[0] != str(os.getpid())]


def scenario_s19(images):
    """S19: a detached child (setsid + double fork, SIGHUP ignored) that keeps writing a heartbeat is gone after teardown,
    nothing named for the run survives on the host, and the next run's workspace, /tmp and $HOME show no trace of it."""
    marker = "mcls19-" + secrets.token_hex(8)
    r1 = lane_run("detach1", "smoke_detach", {"marker": marker}, images)
    after = leftovers()
    time.sleep(5)                                    # the child wrote every 0.5 s; anything that survived would still be running
    pids = host_procs_with(marker)
    r2 = lane_run("detach2", "smoke_detach_probe", {"marker": marker}, images, expect_ok=False)   # a probe edits nothing: rc 5 is expected
    if r1.need("S19"):
        o = r1.obs
        chk("S19", o.get("s19.heartbeat_seen") is True and bool(o.get("s19.heartbeat_paths")), "the detached child never wrote its heartbeat, so the test proves nothing: %s" % o.get("s19.heartbeat_paths"))
        chk("S19", o.get("s19.heartbeat_grew") is True, "the heartbeat did not grow while the tool was alive, so the child was not running: %s" % o.get("s19.heartbeat_grew"))
        chk("S19", o.get("s19.tool_exit") == "exiting", "the tool did not reach its exit with the child running: %s" % o.get("s19.tool_exit"))
        chk("S19", getattr(r1, "left", None) == [], "run 1 left docker resources: %s" % getattr(r1, "left", None))
        chk("S19", after == [], "docker ps -a / volume ls / network ls still show run 1 resources: %s" % after)
        # Evidence that the child is gone: it lived in the run container, and the heartbeat files live in that container's /tmp and $HOME and in
        # the run's `wt` volume. The child provably ran (heartbeat grew), and after teardown run 1's container, volumes and networks are
        # all absent. The evidence is r1.left == [] (taken by lane_run BEFORE force_clean, so it is what the run's own teardown left; it covers
        # the wt volume too). `after` is taken after lane_run's force_clean(), so it is empty by construction and is only a sanity check.
        # Nothing can keep writing. The host process scan below is BEST-EFFORT only: on Docker Desktop the container's processes are not host processes at all.
        chk("S19", pids is not None, "could not list host processes (best-effort check)")
        chk("S19", pids == [], "a host process still carries the child's marker (best-effort check): %s" % pids)
    if r2.need("S19"):
        pr = r2.obs.get("s19.probe") or {}
        chk("S19", "hits" in pr, "run 2 did not report its probe: %s" % r2.obs)
        chk("S19", pr.get("hits") == [], "run 2 sees traces of run 1: %s" % pr.get("hits"))
        chk("S19", pr.get("procs") == [], "run 2 sees processes carrying run 1's marker: %s" % pr.get("procs"))
        chk("S19", pr.get("scanned", 0) > 0, "run 2 scanned nothing: %s" % pr)
        late = host_procs_with(marker)
        chk("S19", late == [], "a host process carries the marker after run 2: %s" % late)
    note("S19 done: the container, volumes and networks of run 1 are gone (the evidence); the host process scan is best-effort")


S20_ATTEMPTS = ["mnt_c_list", "mnt_c_read", "mnt_wsl_list", "mnt_wsl_read", "mnt_wsl_glob", "docker_sock_var_run", "docker_sock_run",
                "proc1_root_mnt", "proc1_root_hostname", "cmd_exe_by_name", "wsl_exe_by_name", "cmd_exe_system32", "wsl_exe_system32",
                "mount_drvfs", "mount_drvfs_syscall", "proc_mounts_9p_drvfs", "wsl_localhost_backslash", "wsl_localhost_slashes", "other_distro_path"]


def scenario_s20(images):
    """S20: from inside the run container every escape attempt fails or finds nothing, and none reaches host data."""
    run = lane_run("escape", "smoke_escape", {}, images, expect_ok=False)   # an escape probe edits nothing: rc 5 is expected
    if not run.need("S20"):
        return
    o = run.obs
    for name in S20_ATTEMPTS:
        v = o.get("s20.att:" + name)
        chk("S20", v is not None, "attempt %s was not recorded" % name)
        chk("S20", v is None or v == "empty" or str(v).startswith("fail"), "attempt %s reached something: %s" % (name, v))
    chk("S20", o.get("s20.att:control_started_binary") == "ok", "the tool counted a binary that started as a failure: %s" % o.get("s20.att:control_started_binary"))
    h = o.get("s20.hostnames") or {}
    host_names = {socket.gethostname().lower(), (dk("info", "--format", "{{.Name}}").stdout or "").strip().lower()} - {""}
    if not str(o.get("s20.att:proc1_root_hostname", "")).startswith("fail"):         # a refused read is a valid result: nothing to compare
        chk("S20", bool(h.get("self")) and h.get("proc1") == h.get("self"), "/proc/1/root is not the container's own root: %s" % h)
        chk("S20", str(h.get("proc1", "")).strip().lower() not in host_names, "/proc/1/root/etc/hostname is a host name: %s vs %s" % (h.get("proc1"), sorted(host_names)))
    chk("S20", not leak_hits(run), "canary content leaked: %s" % leak_hits(run))
    note("S20 note: wsl_localhost_backslash and wsl_localhost_slashes are decorative on Linux (odd POSIX names); the others are the real attempts")
    note("S20 attempts: %s" % json.dumps({k[len("s20.att:"):]: v for k, v in sorted(o.items()) if k.startswith("s20.att:")}))


def finish():
    seen.add("CLEANUP")
    code = 0
    for sid in [s for s in ORDER if not ONLY or s in ONLY or s == "CLEANUP"]:
        if sid not in seen:
            fails.setdefault(sid, []).append("not evaluated")
        if sid in fails:
            code = 1
            print("%s FAIL: %s" % (sid, " | ".join(dict.fromkeys(fails[sid]))), flush=True)
        else:
            print("%s PASS" % sid, flush=True)
    print("# S9 manual (Lenovo, real Codex login); S15, S17 are unit tests (test_lane_remote.py)", flush=True)
    return code


def main():
    note("docker: " + dk("version", "--format", "{{.Server.Version}} {{.Server.Os}}").stdout.strip())
    smoke, proxy = image_ref("mc-lane-smoke"), image_ref("mc-lane-proxy")
    images = (smoke, proxy)
    note("images: %s | %s" % (smoke, proxy))
    pre = leftovers()
    if pre:
        note("removing stale lane resources first: %s" % pre)
        force_clean()
    vcmd = ["python3 probes/vprobe.py"]

    if ONLY:
        extra = [x for x in ONLY if x not in ("S19", "S20")]
        if extra:
            print("S0 FAIL: MCL_SMOKE_ONLY supports only S19 and S20, got %s" % ",".join(extra), flush=True)
            return 1
        if "S19" in ONLY:
            scenario_s19(images)
        if "S20" in ONLY:
            scenario_s20(images)
        return finish()

    iso = lane_run("iso", "smoke_isolation", {}, images, verify_cmds=vcmd)
    if not iso.err:
        iso.vrc = lane_verify(iso)                 # positive control for S4; wraps verify.json, so read vobs first
    iso.vo = vobs(iso)
    egr = lane_run("egress", "smoke_egress", {"hosts": [
        "example.com:443", "example.org:443", "1.1.1.1:443", "8.8.8.8:443", "api.github.com:443", "github.com:443", "host.docker.internal:443",
        "169.254.169.254:443", "10.0.0.1:443", "localhost:443", "example.com:80", "example.com:8443"]}, images, egress=["example.com"], hook=hook_egress)
    frk = lane_run("fork", "smoke_forkbomb", {}, images, hook=hook_fork, expect_ok=False)
    git_ = lane_run("git", "smoke_gitattack", {}, images)
    pol = lane_run("policy", "smoke_policy", {}, images)
    rej = lane_run("reject", "smoke_reject", {}, images)
    frg = lane_run("forge", "smoke_forge", {}, images, expect_ok=False)
    vnet = lane_run("vnet", "ok", {}, images, vnet=True, verify_cmds=vcmd)
    vnet.vo = vobs(vnet)

    # ---- S1
    if iso.need("S1"):
        o = iso.obs
        for i, p in enumerate(iso.obs and [v for k, v in sorted(o.items()) if k.startswith("s1.path.")]):
            chk("S1", p["state"] != "READ", "canary readable at %s" % p["path"])
        chk("S1", any(k.startswith("s1.path.") for k in o), "no canary observations (tool output missing)")
        for k in ("/mnt/c", "/host", "/host_mnt", "/run/desktop", "/run/desktop/mnt/host", "/c", "/Users"):
            chk("S1", o.get("s1.exists:" + k) is False, "%s exists in the container" % k)
        chk("S1", (o.get("s1.walk") or {}).get("found") == [], "canary file found by walk: %s" % (o.get("s1.walk") or {}).get("found"))
        bad = [m for m in o.get("s1.mounts", []) if re.search(r"mnt/host|/run/desktop|/mnt/[a-z]\b|^[A-Za-z]:|\\\\", m[0] + m[1]) or m[2] in ("9p", "drvfs", "virtiofs", "fuse.grpcfuse", "grpcfuse", "fakeowner", "fuse.fakeowner")]
        chk("S1", "s1.mounts" in o and not bad, "host mounts visible in the run container: %s" % bad)
        for name in ("untracked-secret.txt", "ignored-secret.log"):
            chk("S1", o.get("s1.untracked:" + name, "READ") != "READ", "uncommitted host file %s is readable in /work" % name)
        hits = [h for h in o.get("s1.needle_hits", ["?"]) if h not in ("/out/brief", "/tmp/mc-brief.txt")]
        chk("S1", "s1.needle_hits" in o and not hits, "canary/token needle found in the container at %s" % hits)
        chk("S1", o.get("s1.needle_in_env") is False, "canary/token needle in the tool env")
        chk("S1", not leak_hits(iso), "canary content leaked: %s" % leak_hits(iso))
    for r_ in (egr, git_, pol, rej, frg, vnet):
        if r_.out and r_.out.exists():
            chk("S1", not leak_hits(r_), "canary content leaked in run %s: %s" % (r_.tag, leak_hits(r_)))

    # ---- S2
    if iso.need("S2"):
        o = iso.obs
        chk("S2", o.get("s2.env_names") == [], "secret-looking env names in the tool: %s" % o.get("s2.env_names"))
        g = o.get("s2.gh_config") or {}
        chk("S2", g.get("gh_dir") is False and g.get("GH_CONFIG_DIR") is None and g.get("gh_binary") is None, "gh config or binary present: %s" % g)
        chk("S2", "gh" not in (g.get("config_dir") or []), "~/.config/gh exists")
        chk("S2", o.get("s1.needle_in_env") is False, "token needle in env")
        vo = iso.vo
        chk("S2", bool(vo) and not names_with(vo["env"], ["GH_TOKEN", "GITHUB_TOKEN", "LANE_REPO_TOKEN"]), "token names in the verify container env: %s" % (vo or {}).get("env"))

    # ---- S3
    if iso.need("S3"):
        o = iso.obs
        chk("S3", o.get("s3.remote_v") == "", "tool clone has a remote: %r" % o.get("s3.remote_v"))
        for k in ("s3.push_noremote", "s3.push_hostpath", "s3.push_file_url", "s3.push_github"):
            v = o.get(k) or {}
            chk("S3", v.get("rc") not in (None, 0), "%s did not fail: %s" % (k, v))
        chk("S3", str(o.get("s3.curl_github_direct", "status")).startswith("fail"), "api.github.com reachable directly: %s" % o.get("s3.curl_github_direct"))
    if egr.need("S3"):
        chk("S3", str(egr.obs.get("s3.curl_github_proxy", "status")).startswith("fail"), "api.github.com reachable through the proxy: %s" % egr.obs.get("s3.curl_github_proxy"))
        v = egr.obs.get("s3.push_github_proxy") or {}
        chk("S3", v.get("rc") not in (None, 0) and "403" in v.get("err", ""), "git push to github.com through the proxy was not refused by it: %s" % v)
    for r_ in (iso, egr, frk, git_, pol, rej, frg, vnet):
        if getattr(r_, "refs_after", None) is not None:
            chk("S3", r_.refs_before == r_.refs_after, "refs of the local bare remote or the card repo changed in run %s:\n%s\n--\n%s" % (r_.tag, r_.refs_before, r_.refs_after))
            chk("S3", "pwned" not in r_.refs_after, "a pwned ref exists in run %s" % r_.tag)

    # ---- S4
    if iso.need("S4"):
        o = iso.obs
        chk("S4", (o.get("s4.add") or {}).get("rc") == 0 and (o.get("s4.commit") or {}).get("rc") == 0, "git add/commit failed in /work: %s %s" % (o.get("s4.add"), o.get("s4.commit")))
        pt = patch_text(iso)
        chk("S4", "kwd.py" in pt and "+    return 2" in pt, "the edit is not in lane.patch")
        vrc, vdoc, vp = iso.vrc
        chk("S4", vrc == 0 and vdoc.get("kind") == "ok", "lane-delegate verify of the good patch: rc=%s kind=%s %s" % (vrc, vdoc.get("kind"), (vp.stderr or "")[-200:]))
        chk("S4", (iso.vsandbox or {}).get("status") == "ok", "sandbox verify status %s" % (iso.vsandbox or {}).get("status"))

    # ---- S5
    if iso.need("S5"):
        o = iso.obs
        chk("S5", o.get("s5.uid_gid") == [1000, 1000], "uid/gid %s" % o.get("s5.uid_gid"))
        st = o.get("s5.status") or {}
        chk("S5", st.get("CapEff") == "0000000000000000" and st.get("CapPrm") == "0000000000000000", "capabilities not empty: %s" % st)
        chk("S5", st.get("NoNewPrivs") == "1", "no_new_privs not set: %s" % st)
        chk("S5", not any((o.get("s5.sockets") or {"?": True}).values()), "docker socket present: %s" % o.get("s5.sockets"))
        for p in ("/smoke-probe", "/etc/smoke-probe", "/usr/smoke-probe", "/opt/mc-lane/smoke-probe"):
            chk("S5", o.get("s5.write:" + p, "WROTE") != "WROTE", "write to %s succeeded" % p)
    if egr.need("S5"):
        ri = egr.extra.get("run_inspect") or {}
        hc = ri.get("HostConfig") or {}
        chk("S5", bool(ri), "run container could not be inspected (%s)" % egr.extra.get("hook_error"))
        if ri:
            mounts = ri.get("Mounts") or []
            chk("S5", sorted(m.get("Destination") for m in mounts) == ["/out", "/work"] and all(m.get("Type") == "volume" for m in mounts), "run container mounts: %s" % [(m.get("Type"), m.get("Destination")) for m in mounts])
            chk("S5", (ri.get("Config") or {}).get("User") == "1000:1000", "Config.User %s" % (ri.get("Config") or {}).get("User"))
            chk("S5", hc.get("ReadonlyRootfs") is True and hc.get("Privileged") is False, "ReadonlyRootfs/Privileged: %s %s" % (hc.get("ReadonlyRootfs"), hc.get("Privileged")))
            chk("S5", "ALL" in (hc.get("CapDrop") or []) and not hc.get("CapAdd"), "CapDrop/CapAdd: %s %s" % (hc.get("CapDrop"), hc.get("CapAdd")))
            chk("S5", any("no-new-privileges" in s for s in (hc.get("SecurityOpt") or [])), "SecurityOpt %s" % hc.get("SecurityOpt"))
            binds = [b.split(":")[0] for b in (hc.get("Binds") or [])]          # named volumes only, never a host path
            chk("S5", all(re.fullmatch(r"mcl-[0-9a-f]{32}-(wt|out)", b) for b in binds) and not hc.get("Devices") and hc.get("PidMode") in ("", None) and hc.get("IpcMode") != "host", "Binds/Devices/PidMode/IpcMode: %s %s %s %s" % (hc.get("Binds"), hc.get("Devices"), hc.get("PidMode"), hc.get("IpcMode")))
            nets = sorted((ri.get("NetworkSettings") or {}).get("Networks", {}))
            chk("S5", len(nets) == 1 and nets[0].endswith("-int"), "run container networks: %s" % nets)
            chk("S5", hc.get("Dns") == ["0.0.0.0"] and hc.get("PidsLimit") == 256, "Dns/PidsLimit: %s %s" % (hc.get("Dns"), hc.get("PidsLimit")))
    if frk.need("S5"):
        fo = frk.obs.get("s5.fork") or {}
        pids = frk.extra.get("pids") or []
        chk("S5", bool(fo) and fo.get("error") is not None and fo.get("spawned", 5000) < 5000, "fork bomb was not stopped: %s" % fo)
        chk("S5", bool(pids) and max(pids) <= 256 and max(pids) >= 100, "docker stats PIDs %s (limit 256, bomb should fill it)" % pids)
        chk("S5", frk.extra.get("kill_rc") == 0 and frk.extra.get("gone") and frk.extra.get("kill_s", 999) < 60, "container not killable under the bomb: %s" % {k: frk.extra.get(k) for k in ("kill_rc", "gone", "kill_s", "hook_error")})

    # ---- S6
    if egr.need("S6"):
        o, allowed = egr.obs, egr.obs.get("s6.via:example.com:443") or {}
        chk("S6", allowed.get("connect") == "200" and str(allowed.get("http", "")).isdigit(), "allowed host example.com not reachable through the proxy: %s" % allowed)
        chk("S6", str(o.get("s6.urllib_allowed", "")).startswith("status:"), "https request to example.com failed: %s" % o.get("s6.urllib_allowed"))
        for k, v in sorted(o.items()):
            if k.startswith("s6.via:") and k != "s6.via:example.com:443":
                c = str(v.get("connect"))
                chk("S6", c.isdigit() and c != "200", "%s was not refused by the proxy: %s" % (k, v))
        chk("S6", sum(1 for k in o if k.startswith("s6.via:")) >= 10, "too few proxy observations")
        chk("S6", all(v != "connected" for v in (o.get("s6.direct") or {"?": "connected"}).values()), "direct connection bypassing the proxy: %s" % o.get("s6.direct"))

    # ---- S7
    if git_.need("S7"):
        pt = patch_text(git_)
        files = sorted(set(re.findall(r"^diff --git a/(\S+) b/", pt, re.M)))
        chk("S7", "+ORIGINAL data" in pt and "+ORIGINAL sub" in pt, "patch lacks the original file content (a filter rewrote it?)")
        chk("S7", "FILTER_RAN" not in pt and "SMOKE_SENTINEL" not in pt, "tool-configured filter output reached the patch")
        chk("S7", files == [".gitattributes", "a.txt", "sub/f.txt"], "patch paths %s" % files)
        chk("S7", not (git_.dir / "HOST_SENTINEL").exists(), "sentinel created on the host")
        chk("S7", (git_.result or {}).get("exit_code") == 0, "extract/run exit")
        ctl = git_.obs.get("s7.control") or {}
        chk("S7", ctl.get("add_rc") == 0 and ctl.get("filter_ran") is True, "control failed, the attack config was not live in the tool repo: %s" % ctl)

    # ---- S8
    if pol.need("S8"):
        rc, doc, p = lane_verify(pol)
        txt = json.dumps(doc.get("problems") or [])
        chk("S8", rc == 3 and doc.get("kind") == "policy_violation", "verify did not reject: rc=%s kind=%s %s" % (rc, doc.get("kind"), (p.stderr or "")[-200:]))
        for needle, what in ((".claude/x", ".claude write"), (".github/x", ".github write"), ("link.py", "symlink"), ("symlink", "symlink reason"), ("kwd.py", "exec-bit file"), ("exec-bit", "exec-bit reason")):
            chk("S8", needle in txt, "%s not named in problems %s" % (what, txt))

    # ---- S10 / S14
    def env_chk(sid, names):
        if iso.need(sid):
            chk(sid, not names_with(iso.obs.get("s10.env_names", names), names), "run container env has %s" % names_with(iso.obs.get("s10.env_names", names), names))
            chk(sid, "s10.env_names" in iso.obs, "no env observation from the run container")
            vo = iso.vo
            chk(sid, bool(vo), "no probe output from the verify container (verify: %s)" % (iso.vsandbox or {}).get("status"))
            chk(sid, not names_with((vo or {}).get("env", names), names), "verify container env has %s" % names_with((vo or {}).get("env", names), names))
        for r_ in (vnet,):
            if r_.need(sid):
                chk(sid, bool(r_.vo) and not names_with(r_.vo["env"], names), "verify container (verify_network) env has %s" % names_with((r_.vo or {}).get("env", names), names))
    env_chk("S10", ["AMS_OPERATOR_EXPECTED", "AMS_OPERATOR_DATA_ROOT", "SAP_LIVE_TESTS"])
    env_chk("S14", ["ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_CACHE_URL", "RUNNER_TEMP", "RUNNER_TOOL_CACHE", "GITHUB_ACTIONS", "GH_TOKEN",
                       "GITHUB_TOKEN", "LANE_REPO_TOKEN", "OPENAI_API_KEY", "XAI_API_KEY", "RUNINFRA_GATEWAY_KEY", "GH_CONFIG_DIR"])

    # ---- S11
    if iso.need("S11"):
        dns = iso.obs.get("s11.dns") or {}
        chk("S11", len(dns) >= 4 and all(v.startswith("fail") for v in dns.values()), "run container resolved names: %s (resolv.conf %r)" % (dns, iso.obs.get("s11.resolv_conf")))
        vo = iso.vo
        chk("S11", bool(vo) and all(v == "fail" for v in vo["dns"].values()), "verify container resolved names: %s" % (vo or {}).get("dns"))
    if egr.need("S11"):
        chk("S11", all(str(v).startswith("fail") for v in (egr.obs.get("s6.direct_dns") or {"?": "ok"}).values()), "run container with a proxy resolved names directly: %s" % egr.obs.get("s6.direct_dns"))
    if vnet.need("S11"):
        chk("S11", bool(vnet.vo) and all(v == "fail" for v in vnet.vo["dns"].values()), "verify container (verify_network) resolved names: %s" % (vnet.vo or {}).get("dns"))

    # ---- S12
    if iso.need("S12"):
        t = iso.obs.get("s12.tcp") or {}
        chk("S12", len(t) >= 6 and "connected" not in t.values(), "run container reached %s" % {k: v for k, v in t.items() if v == "connected"})
        vo = iso.vo
        chk("S12", bool(vo) and vo["tcp"] and "connected" not in vo["tcp"].values(), "verify container reached %s" % {k: v for k, v in (vo or {}).get("tcp", {}).items() if v == "connected"})
        chk("S12", bool(vo) and vo["ifaces"] == ["lo"], "verify container interfaces %s (wanted only lo)" % (vo or {}).get("ifaces"))
    if vnet.need("S12"):
        vo = vnet.vo
        chk("S12", bool(vo) and vo["tcp"] and "connected" not in vo["tcp"].values(), "verify container (verify_network) reached %s" % {k: v for k, v in (vo or {}).get("tcp", {}).items() if v == "connected"})
        px = (vo or {}).get("proxy") or {}
        for h in ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org"):
            chk("S12", px.get(h) == "200", "deps proxy did not allow %s: %s" % (h, px.get(h)))
        for h in ("example.com", "1.1.1.1", "host.docker.internal", "169.254.169.254"):
            chk("S12", str(px.get(h)).isdigit() and px.get(h) != "200", "deps proxy did not refuse %s: %s" % (h, px.get(h)))

    # ---- S13
    if frg.need("S13"):
        res = frg.result or {}
        chk("S13", bool(res) and res.get("kind") != "ok" and res.get("status") != "ok", "forged result accepted: kind=%s status=%s" % (res.get("kind"), res.get("status")))
        chk("S13", all("FORGED-BY-TOOL" not in f.read_text(errors="replace") for f in frg.out.rglob("*") if f.is_file()), "forged marker reached the host --out tree")
        chk("S13", "s13.alive" in frg.obs or res.get("kind") in ("tool_failed", "timeout"), "no observation from the forging tool and no failure kind: %s" % res.get("kind"))
        chk("S13", (frg.vsandbox or {}).get("status") != "ok", "forged verify.json accepted: %s" % (frg.vsandbox or {}).get("status"))
        note("S13 detail: kind=%s exit=%s kill(ppid)=%s kill(1)=%s alive=%s" % (res.get("kind"), res.get("exit_code"), frg.obs.get("s13.kill:%d" % 0), frg.obs.get("s13.kill:1"), frg.obs.get("s13.alive")))

    # ---- S16
    if rej.need("S16"):
        rc, doc, p = lane_verify(rej)
        txt = json.dumps(doc.get("problems") or [])
        chk("S16", rc == 3 and doc.get("kind") == "policy_violation", "verify did not reject: rc=%s kind=%s %s" % (rc, doc.get("kind"), (p.stderr or "")[-200:]))
        for needle in (".husky/pre-commit", ".gitattributes", "package.json"):
            chk("S16", needle in txt, "%s not named in problems %s" % (needle, txt))
        rc2, doc2, _ = lane_verify(rej, allow=json.dumps([".husky/pre-commit"]))
        chk("S16", rc2 == 3 and ".gitattributes" in json.dumps(doc2.get("problems") or []), "allowing one path must not lift the others: rc=%s %s" % (rc2, doc2.get("problems")))

    # ---- S18
    if egr.need("S18"):
        px = egr.extra.get("proxy_inspect") or {}
        pr = egr.extra.get("proxy_probe") or {}
        chk("S18", bool(px) and bool(pr) and "error" not in pr, "proxy container probe failed: %s %s" % (pr, egr.extra.get("hook_error")))
        if px and pr and "error" not in pr:
            hc = px.get("HostConfig") or {}
            chk("S18", (px.get("Config") or {}).get("User") == "13:13" and pr.get("uid") == 13, "proxy user: %s / uid %s" % ((px.get("Config") or {}).get("User"), pr.get("uid")))
            chk("S18", pr.get("cap") == "0000000000000000" and "ALL" in (hc.get("CapDrop") or []) and not hc.get("CapAdd"), "proxy caps: %s %s" % (pr.get("cap"), hc.get("CapDrop")))
            chk("S18", hc.get("ReadonlyRootfs") is True and pr.get("rootfs_write") != "WROTE", "proxy root fs writable: %s %s" % (hc.get("ReadonlyRootfs"), pr.get("rootfs_write")))
            chk("S18", any("no-new-privileges" in s for s in (hc.get("SecurityOpt") or [])) and hc.get("Privileged") is False, "proxy SecurityOpt/Privileged")
            chk("S18", not (px.get("Mounts") or []) and not hc.get("Binds"), "proxy has mounts: %s" % px.get("Mounts"))
            chk("S18", sorted((px.get("NetworkSettings") or {}).get("Networks", {}))[0:1] != [] and any(n.endswith("-int") for n in (px.get("NetworkSettings") or {}).get("Networks", {})), "proxy not on the internal network")
            conn = pr.get("connect") or {}
            chk("S18", conn.get("example.com|443") == "200", "allowed host example.com refused by the proxy itself: %s" % conn.get("example.com|443"))
            for k, v in sorted(conn.items()):
                if k != "example.com|443":
                    chk("S18", str(v).isdigit() and v != "200", "proxy tunnelled %s: %s" % (k, v))
            for k, v in (pr.get("get") or {}).items():
                chk("S18", str(v).isdigit() and v != "200", "proxy answered a plain request %s with %s" % (k, v))
            note("S18 info: direct connect from the proxy container itself to 1.1.1.1:443 = %s (the proxy is the egress gateway; restricting it needs host firewall rules)" % (pr.get("direct_info") or {}).get("1.1.1.1:443"))

    scenario_s19(images)
    scenario_s20(images)
    scenario_g1(images)
    scenario_x1(iso)

    return finish()


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as exc:
        print("S0 FAIL: harness error %s: %s" % (type(exc).__name__, exc), flush=True)
        rc = 1
    finally:
        force_clean()
        if os.environ.get("MCL_SMOKE_KEEP") == "1":
            print("# kept %s" % WORK, flush=True)
        else:
            rmtree(WORK)
    sys.exit(rc)
PYEOF

tmpw="$(cygpath -m "$tmp" 2>/dev/null || echo "$tmp")"
MCL_PLUGIN_DIR="$plugin" "$PY" -u "$tmpw/smoke.py"
rc=$?
cleanup_docker
exit "$rc"
