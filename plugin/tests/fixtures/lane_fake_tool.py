#!/usr/bin/env python3
"""Fake external CLI for lane-delegate tests. argv: <behaviour> [target]. Reads the brief from stdin."""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlparse

b = sys.argv[1]
target = sys.argv[2] if len(sys.argv) > 2 else ""
if b == "exit_early":   # exits without reading the brief (the parent's stdin write may hit a closed pipe)
    sys.exit(0)
brief = sys.stdin.read()
cwd = Path.cwd()


# ---------------------------------------------------------------- smoke behaviours (tests/smoke-lane-docker.sh, spec 11 and 14)
# They run INSIDE the mc-lane-smoke container. The tool model is the behaviour name; parameters come as JSON in the
# brief (stdin). Each probe prints raw observations `OBS {"k":..,"v":..}` to stdout (-> raw.log); the HARNESS decides
# pass or fail. No probe ever prints a file's content (a canary must never reach the logs).

def obs(key, value):
    print("OBS " + json.dumps({"k": key, "v": value}, sort_keys=True), flush=True)


def brief_json():
    try:
        d = json.loads(brief)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def err_name(e):
    return "%s:%s" % (type(e).__name__, getattr(e, "errno", None))


def read_state(path):
    try:
        with open(path, "rb") as fh:
            fh.read(16)
        return "READ"
    except OSError as e:
        return err_name(e)


def tcp(host, port, timeout=4):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return "connected"
    except OSError as e:
        return err_name(e)


def resolve(name):
    try:
        return "resolved:" + socket.gethostbyname(name)
    except OSError as e:
        return "fail:" + type(e).__name__


def status_field(name):
    try:
        for line in open("/proc/self/status"):
            if line.startswith(name + ":"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def run_cmd(argv, timeout=60):
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        return {"rc": r.returncode, "err": (r.stderr or "")[-200:]}
    except Exception as e:
        return {"rc": None, "err": type(e).__name__}


def via_proxy(host, port, tls=False, timeout=25):
    """Raw CONNECT through the proxy named in HTTPS_PROXY. Returns {connect: <status>, http: <status>}."""
    p = urlparse(os.environ.get("HTTPS_PROXY", ""))
    out = {}
    try:
        s = socket.create_connection((p.hostname, p.port or 3128), timeout=timeout)
    except Exception as e:
        return {"connect": "noproxy:" + type(e).__name__}
    try:
        s.sendall(("CONNECT %s:%s HTTP/1.1\r\nHost: %s:%s\r\n\r\n" % (host, port, host, port)).encode())
        head = s.recv(4096).decode("latin-1")
        out["connect"] = head.split(" ")[1] if head.startswith("HTTP/") and " " in head else head[:40]
        if out["connect"] == "200" and tls:
            t = ssl.create_default_context().wrap_socket(s, server_hostname=host)
            t.sendall(("GET / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % host).encode())
            out["http"] = t.recv(64).decode("latin-1").split(" ")[1]
    except Exception as e:
        out["error"] = type(e).__name__
    finally:
        s.close()
    return out


def wait_for(path, seconds):
    end = time.time() + seconds
    while time.time() < end and not os.path.exists(path):
        time.sleep(0.5)
    return os.path.exists(path)


def smoke_isolation(cfg):
    # S1: nothing outside /work is reachable
    for i, p in enumerate(cfg.get("canary_paths", [])):
        obs("s1.path.%d" % i, {"path": p, "state": read_state(p)})
    for r in ("/mnt/c", "/mnt", "/host", "/host_mnt", "/run/desktop", "/run/desktop/mnt/host", "/c", "/Users"):
        obs("s1.exists:" + r, os.path.exists(r))
    for name in cfg.get("untracked", []):
        obs("s1.untracked:" + name, read_state("/work/" + name))
    found, seen, t0 = [], 0, time.time()
    for root, dirs, files in os.walk("/"):
        dirs[:] = [d for d in dirs if os.path.join(root, d) not in ("/proc", "/sys", "/dev")]
        seen += len(files)
        found += [os.path.join(root, f) for f in files if f == cfg.get("canary_name")]
        if time.time() - t0 > 90:
            break
    obs("s1.walk", {"found": found, "files_seen": seen})
    mounts = []
    for line in open("/proc/mounts"):
        src, mp, fs = line.split()[:3]
        if fs not in ("proc", "sysfs", "cgroup2", "devpts", "mqueue"):
            mounts.append([src, mp, fs])
    obs("s1.mounts", mounts)
    needles = cfg.get("needles", [])
    hits = []
    for base in ("/work", "/home/agent", "/out", "/tmp"):
        for root, dirs, files in os.walk(base):
            for f in files:
                try:
                    data = open(os.path.join(root, f), "rb").read(2_000_000)
                except OSError:
                    continue
                if any(n.encode() in data for n in needles):
                    hits.append(os.path.join(root, f))
    obs("s1.needle_hits", sorted(set(hits)))
    obs("s1.needle_in_env", any(n in "\n".join(os.environ.values()) for n in needles))
    # S2: no gh config, no tokens
    obs("s2.env_names", sorted(k for k in os.environ if re.search(r"GH_|GITHUB|TOKEN|SECRET|PASSWORD|API_KEY|LANE_|AMS_|ACTIONS|RUNNER", k, re.I)))
    home = os.path.expanduser("~")
    obs("s2.gh_config", {"gh_dir": os.path.exists(home + "/.config/gh"), "config_dir": sorted(os.listdir(home + "/.config")) if os.path.isdir(home + "/.config") else None,
                         "GH_CONFIG_DIR": os.environ.get("GH_CONFIG_DIR"), "gh_binary": shutil.which("gh"), "home": sorted(os.listdir(home))})
    # S3: push can not work, GitHub is not reachable
    obs("s3.remote_v", subprocess.run(["git", "remote", "-v"], cwd=str(cwd), capture_output=True, text=True).stdout)
    obs("s3.push_noremote", run_cmd(["git", "push"]))
    obs("s3.push_hostpath", run_cmd(["git", "push", cfg.get("bare_repo", "/nonexistent.git"), "HEAD:refs/heads/pwned"]))
    obs("s3.push_file_url", run_cmd(["git", "push", "file://" + cfg.get("bare_repo_posix", "/nonexistent.git"), "HEAD:refs/heads/pwned"]))
    obs("s3.push_github", run_cmd(["git", "push", "https://github.com/owner/product.git", "HEAD:refs/heads/pwned"], timeout=90))
    try:
        r = urllib.request.build_opener(urllib.request.ProxyHandler({})).open("https://api.github.com", timeout=15)
        obs("s3.curl_github_direct", "status:%s" % r.status)
    except Exception as e:
        obs("s3.curl_github_direct", "fail:" + type(e).__name__)
    # S4: edit and commit in /work
    (cwd / "kwd.py").write_text("def f():\n    return 2\n")
    obs("s4.add", run_cmd(["git", "add", "-A"]))
    obs("s4.commit", run_cmd(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "smoke S4"]))
    # S5: identity, capabilities, socket, read-only root
    obs("s5.uid_gid", [os.getuid(), os.getgid()])
    obs("s5.status", {k: status_field(k) for k in ("CapEff", "CapPrm", "CapBnd", "NoNewPrivs", "Seccomp")})
    obs("s5.sockets", {p: os.path.exists(p) for p in ("/var/run/docker.sock", "/run/docker.sock", "/docker.sock")})
    for p in ("/smoke-probe", "/etc/smoke-probe", "/usr/smoke-probe", "/opt/mc-lane/smoke-probe"):
        try:
            open(p, "w").close()
            obs("s5.write:" + p, "WROTE")
        except OSError as e:
            obs("s5.write:" + p, err_name(e))
    # S10 / S14 (run side): the names of every variable the tool can see
    obs("s10.env_names", sorted(os.environ))
    # S11 / S12 (run side): no resolver, no route to the host or the cloud metadata service
    obs("s11.dns", {n: resolve(n) for n in ("example.com", "api.github.com", cfg.get("canary_domain", "canary.example.com"), "host.docker.internal")})
    obs("s11.resolv_conf", open("/etc/resolv.conf").read()[:300])
    proxy_host = urlparse(os.environ.get("HTTPS_PROXY", "")).hostname
    obs("s11.proxy_name", resolve(proxy_host) if proxy_host else None)
    obs("s12.tcp", {a + ":" + str(p): tcp(a, p) for a, p in (("host.docker.internal", 443), ("10.0.0.1", 443), ("169.254.169.254", 80),
                                                              ("192.168.65.254", 80), ("172.17.0.1", 80), ("1.1.1.1", 443), ("8.8.8.8", 53))})
    print("Implemented.")


def smoke_forkbomb(cfg):
    procs, err = [], None
    for _ in range(5000):
        try:
            procs.append(subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        except OSError as e:
            err = err_name(e)
            break
    obs("s5.fork", {"spawned": len(procs), "error": err})
    obs("s5.fork_hold", wait_for("/tmp/smoke_done", 240))     # the harness kills the container while it is full


def smoke_egress(cfg):
    for hp in cfg.get("hosts", []):
        host, port = hp.rsplit(":", 1)
        obs("s6.via:" + hp, via_proxy(host, int(port), tls=(port == "443")))
    obs("s6.direct", {h: tcp(h, 443, 6) for h in ("1.1.1.1", "8.8.8.8", "93.184.215.14")})
    obs("s6.direct_dns", {"example.com": resolve("example.com"), "example.org": resolve("example.org")})
    obs("s3.push_github_proxy", run_cmd(["git", "push", "https://github.com/owner/product.git", "HEAD:refs/heads/pwned"], timeout=90))
    try:
        r = urllib.request.urlopen("https://api.github.com", timeout=25)
        obs("s3.curl_github_proxy", "status:%s" % r.status)
    except Exception as e:
        obs("s3.curl_github_proxy", "fail:" + type(e).__name__ + ":" + str(e)[:100])
    try:
        r = urllib.request.urlopen(cfg.get("allowed_url", "https://example.com"), timeout=25)
        obs("s6.urllib_allowed", "status:%s" % r.status)
    except Exception as e:
        obs("s6.urllib_allowed", "fail:" + type(e).__name__ + ":" + str(e)[:100])
    (cwd / "kwd.py").write_text("def f():\n    return 2\n")
    obs("s18.hold", wait_for("/tmp/smoke_done", 240))         # the harness probes the proxy container meanwhile


def smoke_gitattack(cfg):
    (cwd / "a.txt").write_text("ORIGINAL data\n")
    (cwd / "sub").mkdir(exist_ok=True)
    (cwd / "sub" / "f.txt").write_text("ORIGINAL sub\n")
    (cwd / ".gitattributes").write_text("*.txt filter=evil\n*.txt diff=evil\n")
    script = cwd / ".git" / "evil.sh"          # what every configured filter / hook / command runs
    script.write_text("#!/bin/sh\necho ran >> /pristine/SMOKE_SENTINEL 2>/dev/null\necho ran >> /x/SMOKE_SENTINEL 2>/dev/null\n"
                      "echo ran >> '%s' 2>/dev/null\nexec sed s/ORIGINAL/FILTER_RAN/\n" % cfg.get("host_sentinel", "/nonexistent/sentinel"))
    evil = "sh /work/.git/evil.sh"
    with open(cwd / ".git" / "config", "a") as fh:
        fh.write('[filter "evil"]\n\tclean = %s\n\tsmudge = %s\n\trequired = true\n' % (evil, evil))
        fh.write('[diff "evil"]\n\ttextconv = %s\n\tcommand = %s\n' % (evil, evil))
        fh.write('[core]\n\tfsmonitor = %s\n\thooksPath = /work/.githooks-evil\n\tsshCommand = %s\n' % (evil, evil))
        fh.write('[include]\n\tpath = /work/.git/evil.inc\n')
    (cwd / ".git" / "evil.inc").write_text('[alias]\n\tadd = !%s\n' % evil)
    # control: the attack config is live in the tool's own repository, so only the extract step's isolation can stop it
    add = run_cmd(["git", "-c", "core.fsmonitor=false", "add", "a.txt"])
    staged = subprocess.run(["git", "show", ":a.txt"], cwd=str(cwd), capture_output=True, text=True).stdout
    obs("s7.control", {"add_rc": add["rc"], "add_err": add["err"], "filter_ran": "FILTER_RAN" in staged})
    (cwd / ".git" / "commondir").write_text("/nonexistent-common\n")
    hooks = cwd / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for h in ("pre-commit", "post-checkout", "post-index-change", "reference-transaction"):
        (hooks / h).write_text("#!/bin/sh\n%s\n" % evil)
        (hooks / h).chmod(0o755)
    (cwd / ".git" / "info").mkdir(exist_ok=True)
    (cwd / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    (cwd / "sub" / ".git").mkdir(exist_ok=True)           # a nested repository the copy must not carry over
    (cwd / "sub" / ".git" / "config").write_text('[core]\n\tfsmonitor = %s\n' % evil)
    print("done")


def smoke_policy(cfg):
    (cwd / ".claude").mkdir(exist_ok=True)
    (cwd / ".claude" / "x").write_text("x\n")
    (cwd / ".github").mkdir(exist_ok=True)
    (cwd / ".github" / "x").write_text("x\n")
    os.symlink("kwd.py", cwd / "link.py")
    (cwd / "kwd.py").chmod(0o755)                          # exec-bit change on a tracked file
    print("done")


def smoke_reject(cfg):
    (cwd / ".husky").mkdir(exist_ok=True)
    (cwd / ".husky" / "pre-commit").write_text("#!/bin/sh\necho hook\n")
    with open(cwd / ".gitattributes", "a") as fh:
        fh.write("*.py filter=evil\n")
    pkg = json.loads((cwd / "package.json").read_text())
    pkg.setdefault("scripts", {})["postinstall"] = "node evil.js"
    (cwd / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    print("done")


def smoke_forge(cfg):
    forged = {"schema": "mc-lane.result.v1", "kind": "ok", "status": "ok", "exit_code": 0, "marker": "FORGED-BY-TOOL"}
    for name, doc in (("result.json", forged), ("facts.json", {"patch_sha256": "0" * 64, "marker": "FORGED-BY-TOOL"}),
                      ("verify.json", {"status": "ok", "marker": "FORGED-BY-TOOL", "commands": [{"cmd": "x", "exit": 0, "tail": "", "phase": "verify"}]})):
        try:
            Path("/out", name).write_text(json.dumps(doc))
        except OSError as e:
            obs("s13.write:" + name, err_name(e))
    for pid in (os.getppid(), 1):
        try:
            os.kill(pid, 9)
            obs("s13.kill:%d" % pid, "sent")
        except OSError as e:
            obs("s13.kill:%d" % pid, err_name(e))
    time.sleep(1)
    obs("s13.alive", True)


def s19_paths():
    return ["/tmp/s19-heartbeat", os.path.join(os.environ.get("HOME") or "/home/agent", ".s19-heartbeat"), "/work/.s19-heartbeat"]


def smoke_detach(cfg):
    """S19 run 1: start a detached child (setsid, double fork, SIGHUP ignored) that keeps a heartbeat going, then exit."""
    import signal
    marker = cfg.get("marker", "mcls19-none")
    code = ("import sys, time\n"
            "end = time.time() + 900\n"
            "while time.time() < end:\n"
            "    for p in sys.argv[2:]:\n"
            "        try:\n"
            "            open(p, 'a').write('hb %f\\n' % time.time())\n"
            "        except OSError:\n"
            "            pass\n"
            "    time.sleep(0.5)\n")
    pid = os.fork()
    if pid == 0:
        os.setsid()
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if os.fork() != 0:
            os._exit(0)
        dn = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(dn, fd)
        os.execv(sys.executable, [sys.executable, "-c", code, marker] + s19_paths())
    os.waitpid(pid, 0)
    wait_for(s19_paths()[0], 10)
    time.sleep(1)
    size1 = os.path.getsize(s19_paths()[0]) if os.path.exists(s19_paths()[0]) else 0
    time.sleep(1.5)
    size2 = os.path.getsize(s19_paths()[0]) if os.path.exists(s19_paths()[0]) else 0
    obs("s19.heartbeat_grew", size2 > size1)
    seen_paths = [p for p in s19_paths() if os.path.exists(p) and os.path.getsize(p) > 0]
    obs("s19.heartbeat_paths", seen_paths)
    obs("s19.heartbeat_seen", bool(seen_paths))
    obs("s19.tool_exit", "exiting")
    print("done")


def smoke_detach_probe(cfg):
    """S19 run 2: look for any trace of run 1 in this run's workspace, /tmp, $HOME and the process table."""
    marker = cfg.get("marker", "mcls19-none").encode()
    hits, scanned = [], 0
    for p in s19_paths():
        if os.path.exists(p):
            hits.append(p)
    for base in ("/work", "/tmp", os.environ.get("HOME") or "/home/agent", "/out"):
        for root, dirs, files in os.walk(base):
            for f in files + dirs:
                if "s19" in f.lower() or "heartbeat" in f.lower():
                    hits.append(os.path.join(root, f))
            for f in files:
                fp = os.path.join(root, f)
                if fp in ("/out/brief", "/tmp/mc-brief.txt"):
                    continue
                try:
                    data = open(fp, "rb").read(2_000_000)
                except OSError:
                    continue
                scanned += 1
                if marker in data:
                    hits.append(fp)
    procs = []
    for d in os.listdir("/proc"):
        if d.isdigit() and int(d) != os.getpid():
            try:
                if marker in open("/proc/%s/cmdline" % d, "rb").read():
                    procs.append(d)
            except OSError:
                pass
    obs("s19.probe", {"hits": sorted(set(hits)), "procs": procs, "scanned": scanned})
    print("done")


def smoke_escape(cfg):
    """S20: every attempt to reach the host from inside the run container. Values: ok | fail:<reason> | empty."""
    import glob

    def att(name, value):
        obs("s20.att:" + name, value)

    def listing(path):
        try:
            return "empty" if not os.listdir(path) else "ok"
        except OSError as e:
            return "fail:" + err_name(e)

    def reading(path):
        try:
            with open(path, "rb") as fh:
                return "ok" if fh.read(16) else "empty"
        except OSError as e:
            return "fail:" + err_name(e)

    def sock(path):
        s = socket.socket(socket.AF_UNIX)
        try:
            s.settimeout(3)
            s.connect(path)
            return "ok"
        except OSError as e:
            return "fail:" + err_name(e)
        finally:
            s.close()

    def execute(argv):
        try:
            r = subprocess.run(argv, capture_output=True, timeout=15)
            return "ok" if r.returncode == 0 else "fail:rc%d" % r.returncode
        except Exception as e:
            return "fail:" + type(e).__name__

    def launch(argv):
        """The process STARTED (any return code, or still running at the timeout) -> ok. Only a failure to exec it is a fail."""
        try:
            subprocess.run(argv, capture_output=True, timeout=15)
            return "ok"
        except subprocess.TimeoutExpired:
            return "ok"
        except OSError as e:
            return "fail:" + err_name(e)

    def by_name(name):
        found = shutil.which(name)
        return launch([found]) if found else "fail:notfound"

    att("control_started_binary", by_name("false"))   # a binary that STARTS and exits non-zero: must read as ok (R2)
    att("mnt_c_list", listing("/mnt/c"))
    att("mnt_c_read", reading("/mnt/c"))
    att("mnt_wsl_list", listing("/mnt/wsl"))
    att("mnt_wsl_read", reading("/mnt/wsl"))
    att("mnt_wsl_glob", "empty" if not glob.glob("/mnt/wsl/*") else "ok")
    att("docker_sock_var_run", sock("/var/run/docker.sock") if os.path.exists("/var/run/docker.sock") else reading("/var/run/docker.sock"))
    att("docker_sock_run", sock("/run/docker.sock") if os.path.exists("/run/docker.sock") else reading("/run/docker.sock"))
    att("proc1_root_mnt", listing("/proc/1/root/mnt"))
    try:
        own = open("/etc/hostname").read().strip()
        p1 = open("/proc/1/root/etc/hostname").read().strip()
        att("proc1_root_hostname", "empty" if p1 == own else "ok")
        obs("s20.hostnames", {"self": own, "proc1": p1})
    except OSError as e:
        att("proc1_root_hostname", "fail:" + err_name(e))
    att("cmd_exe_by_name", by_name("cmd.exe"))
    att("wsl_exe_by_name", by_name("wsl.exe"))
    att("cmd_exe_system32", launch(["/mnt/c/Windows/System32/cmd.exe", "/c", "echo", "x"]))
    att("wsl_exe_system32", launch(["/mnt/c/Windows/System32/wsl.exe", "-l"]))
    att("mount_drvfs", execute(  # mount only: its return code maps (rc 0 = mounted = ok)
        ["mount", "-t", "drvfs", "C:", "/mnt/x"]))
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        rc = libc.mount(b"C:", b"/tmp", b"drvfs", 0, None)
        att("mount_drvfs_syscall", "ok" if rc == 0 else "fail:errno%d" % ctypes.get_errno())
    except Exception as e:
        att("mount_drvfs_syscall", "fail:" + type(e).__name__)
    # mount types a host share shows up as. LANE_HOST_MOUNTS lists mount points the lane provides on purpose (exact match, with the
    # reason). There are none: the run container has named volumes only (/work, /out), checked by S5.
    LANE_HOST_MOUNTS = ()
    bad = [l.split()[1] for l in open("/proc/mounts")
           if l.split()[1] not in LANE_HOST_MOUNTS and (l.split()[2] in ("9p", "drvfs", "fakeowner", "virtiofs", "grpcfuse") or l.split()[2].startswith("fuse")
                                                          or l.split()[1].startswith("/mnt/"))]
    att("proc_mounts_9p_drvfs", "ok" if bad else "empty")
    # decorative on Linux: a backslash path and //host/share are just relative/odd POSIX names here; they only prove nothing resolves
    att("wsl_localhost_backslash", reading("\\\\wsl.localhost\\Ubuntu\\etc\\hostname"))
    att("wsl_localhost_slashes", reading("//wsl.localhost/Ubuntu/etc/hostname"))
    att("other_distro_path", "ok" if (listing("/mnt/wsl/Ubuntu") == "ok" or listing("/mnt/wsl/docker-desktop-data") == "ok") else "fail:unreachable")
    print("done")


SMOKE = {"smoke_isolation": smoke_isolation, "smoke_forkbomb": smoke_forkbomb, "smoke_egress": smoke_egress,
         "smoke_gitattack": smoke_gitattack, "smoke_policy": smoke_policy, "smoke_reject": smoke_reject,
         "smoke_forge": smoke_forge, "smoke_detach": smoke_detach, "smoke_detach_probe": smoke_detach_probe,
         "smoke_escape": smoke_escape}

if b in SMOKE:
    SMOKE[b](brief_json())
elif b == "ok":
    (cwd / "kwd.py").write_text("def f():\n    return 2\n"); print("Implemented. All tests pass.")
elif b == "qa_ok":
    (cwd / "tests" / "test_nan.py").write_text("def test_nan():\n    assert True\n"); print("Added tests.")
elif b == "qa_edits_src":
    (cwd / "kwd.py").write_text("x = 1\n"); (cwd / "tests" / "test_x.py").write_text("def test_x(): pass\n"); print("done")
elif b == "no_changes":
    print("Nothing to do.")
elif b == "ask_confirm":
    print("I wrote a plan. Shall I proceed? Please confirm.")
elif b == "quota":
    print("ERROR: You've hit your usage limit."); sys.exit(0)
elif b == "timeout":
    time.sleep(600)
elif b == "commit":
    (cwd / "kwd.py").write_text("def f():\n    return 3\n")
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "tool"], check=False)
    print("committed")
elif b == "push_attempt":
    (cwd / "kwd.py").write_text("def f():\n    return 4\n")
    r = subprocess.run(["git", "push", "origin", "HEAD:main"], capture_output=True, text=True)
    print(f"push rc={r.returncode}")
elif b == "edit_github_dir":
    (cwd / ".github" / "workflows").mkdir(parents=True); (cwd / ".github" / "workflows" / "x.yml").write_text("on: push\n"); print("done")
elif b == "touch_conftest":
    (cwd / "conftest.py").write_text("import os\n"); print("done")
elif b == "symlink":
    os.symlink("kwd.py", cwd / "link.py"); print("done")
elif b in ("edit_board_config_abs_path", "edit_main_hooks_abs_path"):
    Path(target).parent.mkdir(parents=True, exist_ok=True); Path(target).write_text("tampered"); print("done")
elif b == "new_tag":
    subprocess.run(["git", "-C", target, "tag", "evil"], check=False)
    subprocess.run(["git", "-C", target, "push", "origin", "evil"], check=False); print("done")
elif b == "read_env_secret":
    leaked = [k for k in ("GH_TOKEN", "GITHUB_TOKEN") if k in os.environ]
    (cwd / "kwd.py").write_text(f"LEAK = {leaked!r}\n"); print("done")
elif b == "see_dot_git":
    (cwd / "kwd.py").write_text(f"HAS_GIT = {(cwd / '.git').exists()}\n"); print("done")
elif b == "rename_into_github":   # delete a file, create a near-identical one under .github/ -> git sees a rename
    dst = cwd / ".github" / "workflows" / "evil.yml"; dst.parent.mkdir(parents=True)
    dst.write_text((cwd / "kwd.py").read_text()); (cwd / "kwd.py").unlink(); print("done")
elif b == "fsmonitor_in_copy":    # target = marker file; git must never run the configured fsmonitor command
    helper = Path(target).parent / "mk_marker.py"
    helper.write_text("import sys\nopen(sys.argv[1], 'w').write('ran')\n")
    cmd = " ".join("'" + str(x).replace(chr(92), "/") + "'" for x in (sys.executable, helper, target))
    (cwd / ".git").mkdir(exist_ok=True)
    (cwd / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tfsmonitor = " + cmd + "\n")
    (cwd / "kwd.py").write_text("def f():\n    return 5\n"); print("done")
elif b == "noprefix_trick":       # diff.noprefix makes `apply -p1` strip a leading directory: x/.github/... -> .github/...
    (cwd / ".git").mkdir(exist_ok=True)
    cfg = cwd / ".git" / "config"
    old = cfg.read_text() if cfg.exists() else "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
    cfg.write_text(old + "[diff]\n\tnoprefix = true\n")
    d = cwd / "x" / ".github" / "workflows"; d.mkdir(parents=True); (d / "e.yml").write_text("on: push\n")
    (cwd / "kwd.py").write_text("def f():\n    return 6\n"); print("done")
elif b == "detached_child":       # target = file a detached child edits after 3 s; the tool itself exits 0 at once
    code = "import sys,time\ntime.sleep(3)\nopen(sys.argv[1], 'a').write('tampered')\n"
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, "-c", code, target], **kw)
    (cwd / "kwd.py").write_text("def f():\n    return 7\n"); print("done")
else:
    print(f"unknown behaviour {b}"); sys.exit(2)
