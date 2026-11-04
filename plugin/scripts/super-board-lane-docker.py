#!/usr/bin/env python3
"""Docker sandbox provider for external tool lanes (Plan 1b, spec addendum §4, §6, §14).

Pure builders for every `docker` argv (run, extract, verify, proxy, stage, network), the env-file writer, the
host-side result builder, the tar validator and `DockerContainment`. All docker calls go through `_docker()`.

Conventions:
  * argv lists returned here do NOT include the docker binary; `_docker(args)` prepends it.
  * the tool's output is untrusted data (A1): `build_result` never reads JSON the run container wrote.
  * nothing here ever passes `-e`, `--privileged`, `--device`, the docker socket or a host path to the run container.

Proxy config delivery (our choice): the rendered squid.conf is base64-encoded into a non-secret env-file
(MCL_SQUID_CONF_B64) and the proxy command is `sh -c` decode-to-/tmp then `exec squid`. This keeps the proxy
`--read-only`, needs no extra volume and no `docker cp` into a tmpfs.

Test hooks: MCL_DOCKER_ARGV (JSON list) or MCL_DOCKER (path) replace the docker binary.

Container entry: every image run is `--entrypoint python3 <image> /opt/mc-lane/entry.py <subcommand> <flags>`. The
argv built here is parsed by entry.py's own parser in tests/test_lane_docker.py (contract test), so the two cannot
drift. Subcommands used: run (env contract MCL_TOOL, MCL_MODEL, MCL_IDLE_S, MCL_HARD_S, MCL_ENV_DENY in the
env-file), extract, prepare (via `docker exec`), stage, put. The verify container is the exception: it idles
(`--entrypoint sleep <image> infinity`) and the host `docker exec`s into it (A13).

Artifact layout (canonical = what entry.py writes), all produced on the runner host under --out-dir:
  bundle.tar   docker-cp'd from the extract container (/x/bundle.tar), validated: lane.patch, facts.json (extract's),
               raw.log, report.json, session/NNN.jsonl
  facts.json   written by THIS module (build_host_facts) from the bundle's lane.patch and extract facts, the
               `docker wait` result and the dispatch inputs; never copied from the run container (A1)
  verify.json  written by THIS module, never copied out of a container (A13): {status: ok|verify_failed|
               apply_failed|timeout|base_broken, commands: [{cmd, exit, tail, phase, inherited?}], notes?, base?}.
               Each exit code is the `docker exec` exit code the host saw; `inherited` marks a verify command that
               also fails on the base (A14)
Host-only state (never uploaded) lives in <out-dir>/../state: run.json {exit_code, timed_out}, stage.json,
env-files (deleted right after the run container exits).

Config keys read from the --config JSON (the runner payload built by super-board-lane-remote.py from the board's
pilot block): image and proxy_image (pilot.sandbox, digests), limits, verify_network, env_deny, deps_hosts
(pilot.sandbox.deps_hosts, default pypi.org, files.pythonhosted.org, registry.npmjs.org), tools (pilot.tools[], only
the dispatched one: id, auth, egress), verify_setup, verify_commands, verify_timeout_min (pilot.verify_*). The
repo allowlist pilot.sandbox.repos is NOT read here: it is enforced on the Surface by super-board-lane-remote.py
(build_inputs) and again by the fixed REPOS set in lane.yml's first step; it is listed so all config keys are in
one place.

CLI (called by sandbox/lane.yml): stage | run | extract | verify | cleanup, see main().
"""
from __future__ import annotations

import argparse
import base64
import codecs
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import shlex
import tarfile
import tempfile
import threading
import time

NONCE_RE = re.compile(r"^[a-f0-9]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]([a-z0-9-]*[a-z0-9])?$")
DENY_PREFIXES = ("ACTIONS_", "RUNNER_", "GITHUB_", "GH_")
DENY_NAMES = {"LANE_REPO_TOKEN"}
FIXED_ENV = ["PATH=/usr/local/bin:/usr/bin:/bin", "HOME=/home/agent", "LANG=C.UTF-8"]
ENTRY_SCRIPT = "/opt/mc-lane/entry.py"
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")       # same rule as entry.py
CONTAINER_ROLES = ("run", "extract", "verify", "stage", "proxy", "depsproxy")
VOLUME_ROLES = ("wt", "out", "pristine", "x", "repo", "vin", "vx")
DEFAULT_DEPS_HOSTS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org")
VERIFY_STATUSES = ("ok", "inherited", "verify_failed", "apply_failed", "timeout", "base_broken", "merge_conflict")
BUNDLE_MAX = 45_000_000          # = sum of the caps in entry.py / super-board-lane-remote.py
PATCH_MAX = 20_000_000
FACTS_MAX = 64_000
GRACE_S = 120
STAGE_MERGE_CONFLICT = 3        # entry.py stage: merging the base into the head conflicted
NETWORK_ROLES = ("int", "deps")
SMOKE_IMAGE_PREFIX = "mc-lane-smoke"
IDLE_S_DEFAULT = 600
EXEC_USER = "1000:1000"
TAIL_CHARS = 4000
# A13: files that can make a tool-written test pass by themselves; flagged to the Reviewer from the patch alone.
HARNESS_RE = re.compile(r"^(conftest\.py|pytest\.ini|pyproject\.toml|setup\.cfg|tox\.ini|package\.json"
                        r"|(vitest|vite|jest)\.config\..+|tsconfig.*\.json)$")


def _nonce(nonce):
    if not isinstance(nonce, str) or not NONCE_RE.match(nonce):
        raise ValueError("nonce must match ^[a-f0-9]{32}$")
    return nonce


def container_name(nonce, role):
    assert role in CONTAINER_ROLES
    return f"mcl-{_nonce(nonce)}-{role}"


def volume_name(nonce, role):
    assert role in VOLUME_ROLES
    return f"mcl-{_nonce(nonce)}-{role}"


def network_name(nonce, role):
    assert role in NETWORK_ROLES
    return f"mcl-{_nonce(nonce)}-{role}"


def require_digest(ref):
    if not isinstance(ref, str) or not DIGEST_RE.search(ref):
        raise ValueError(f"image reference must end in @sha256:<64 hex>: {ref!r}")
    return ref


def norm_tool(tool):
    """One id everywhere: the board may call a tool `pi-delegate`; the workflow, entry.py's tools.json and this
    module use `pi`. super-board-lane-remote.py applies the same rule."""
    tool = str(tool or "")
    return tool[:-len("-delegate")] if tool.endswith("-delegate") else tool


def tool_cfg(cfg, tool):
    want = norm_tool(tool)
    for t in cfg.get("tools") or []:
        if isinstance(t, dict) and norm_tool(t.get("id")) == want:
            return t
    return {}


def _entry(image, *args):
    """`--entrypoint python3 <image> /opt/mc-lane/entry.py args...`: independent of the image's own ENTRYPOINT."""
    return ["--entrypoint", "python3", image, ENTRY_SCRIPT, *args]


def check_local(cfg, tool, image, environ):
    """A6: where=local is smoke-only."""
    if cfg.get("where") != "local":
        return
    if tool != "fake":
        raise ValueError("where=local accepts only the fake tool")
    if not str(image).startswith(SMOKE_IMAGE_PREFIX):
        raise ValueError(f"where=local accepts only the {SMOKE_IMAGE_PREFIX} image")
    if environ.get("MC_DOCKER_SMOKE") != "1":
        raise ValueError("where=local needs MC_DOCKER_SMOKE=1")


# ---------------------------------------------------------------- argv builders

def _hardening(limits):
    lim = limits or {}
    return ["--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g",
            "--tmpfs", "/home/agent:rw,nosuid,nodev,size=512m,uid=1000,gid=1000",
            "--memory", str(lim.get("memory", "4g")), "--cpus", str(lim.get("cpus", "4")),
            "--pids-limit", str(lim.get("pids", 1024))]


def run_argv(cfg, nonce, env_file, image, tool):
    """Spec §4 run container. The only mounts: worktree volume, out volume, and (codex + auth file) the cred volume."""
    require_digest(image)
    argv = ["run", "--name", container_name(nonce, "run"), "--rm=false"] + _hardening(cfg.get("limits")) + [
        "--network", network_name(nonce, "int"), "--dns", "0.0.0.0", "--env-file", str(env_file),
        "-v", f"{volume_name(nonce, 'wt')}:/work", "-v", f"{volume_name(nonce, 'out')}:/out"]
    if tool == "codex" and (tool_cfg(cfg, tool).get("auth") or {}).get("kind") == "file":
        argv += ["-v", "mcl-cred-codex:/cred/codex:ro"]         # A11: only codex runs see it, read-only (entry.py copies auth.json)
    return argv + ["-w", "/work"] + _entry(image, "run")


def extract_argv(cfg, nonce, image):
    """No network. Tool tree read-only at /tool, run output read-only at /outsrc (regular files only are copied),
    fresh clone at /pristine (made by stage, already at the start commit), one tar written to /x. The flags are
    exactly entry.py's `extract` parser."""
    require_digest(image)
    return ["run", "--name", container_name(nonce, "extract"), "--rm=false"] + _hardening(cfg.get("limits")) + [
        "--network", "none",
        "-v", f"{volume_name(nonce, 'wt')}:/tool:ro", "-v", f"{volume_name(nonce, 'out')}:/outsrc:ro",
        "-v", f"{volume_name(nonce, 'pristine')}:/pristine", "-v", f"{volume_name(nonce, 'x')}:/x",
        "-w", "/pristine"] + _entry(image, "extract", "--tool-dir", "/tool", "--pristine", "/pristine",
                                    "--outsrc", "/outsrc", "--x", "/x")


def verify_argv(cfg, nonce, image, env_file=None):
    """Fresh clone at /repo, a small input volume (lane.patch) read-only at /in, a fresh volume at /x. The container
    only idles (`sleep infinity`): the host applies the patch and runs every command with `docker exec` and takes the
    exit codes from Docker (A13). It never sees the tool's out volume or worktree. --network none unless
    cfg.verify_network (default false), then the deps network (a proxy that allows only the registries, A7). The
    caller enforces the deadline."""
    require_digest(image)
    net = network_name(nonce, "deps") if cfg.get("verify_network") else "none"
    argv = ["run", "--name", container_name(nonce, "verify"), "--rm=false"] + _hardening(cfg.get("limits")) + [
        "--network", net]
    if net != "none":
        argv += ["--dns", "0.0.0.0"]
        if env_file:
            argv += ["--env-file", str(env_file)]
    argv += ["-v", f"{volume_name(nonce, 'repo')}:/repo", "-v", f"{volume_name(nonce, 'vin')}:/in:ro",
             "-v", f"{volume_name(nonce, 'vx')}:/x", "-w", "/repo"]
    return argv + ["--entrypoint", "sleep", image, "infinity"]


def exec_argv(nonce, cmd, shell=True):
    """One verify_setup / verify_commands entry as its own `docker exec` (A13). shell=True (default): CMD is a shell line,
    run as `sh -c`. shell=False: CMD is an argv list handed to docker exec as it is - no sh, no -c, nothing joined or quoted."""
    head = ["exec", "-u", EXEC_USER, "-w", "/repo", container_name(nonce, "verify")]
    if shell:
        return head + ["sh", "-c", str(cmd)]
    if not isinstance(cmd, (list, tuple)) or not cmd:
        raise ValueError("shell=False needs a non-empty argv list")
    return head + [str(c) for c in cmd]


def prepare_argv(nonce):
    """entry.py `prepare` through `docker exec`: applies /in/lane.patch to /repo and exits."""
    return ["exec", "-u", EXEC_USER, "-w", "/repo", container_name(nonce, "verify"), "python3", ENTRY_SCRIPT,
            "prepare", "--repo", "/repo", "--patch", "/in/lane.patch", "--x", "/x"]


def stage_argv(cfg, nonce, image, seed, start_sha, target="wt", merge_sha=None):
    """Helper container: no-remote clone from the read-only bare seed into a named volume. Runs in the foreground
    with --rm so the name `mcl-<nonce>-stage` is free for the next target. `seed` is a host path (the only host
    mount anywhere; never a run container)."""
    require_digest(image)
    if not SHA_RE.match(start_sha or ""):
        raise ValueError("start_sha must be 40 hex")
    if target not in ("wt", "pristine", "repo"):
        raise ValueError("stage target must be wt, pristine or repo")
    if merge_sha is not None and not SHA_RE.match(merge_sha):
        raise ValueError("merge_sha must be 40 hex")
    extra = ["--merge-sha", merge_sha] if merge_sha else []      # the gate only: merge the base into the head in the container
    return ["run", "--rm", "--name", container_name(nonce, "stage")] + _hardening(cfg.get("limits")) + [
        "--network", "none", "-v", f"{seed}:/seed:ro", "-v", f"{volume_name(nonce, target)}:/work",
        "-w", "/work"] + _entry(image, "stage", "--seed", "/seed", "--dest", "/work", "--sha", start_sha, *extra)


def put_argv(cfg, nonce, image, role):
    """Helper container that unpacks a tar from its stdin into a volume (entry.py `put`): how the host hands the
    brief (role out) and the verify inputs (role vin) to a volume without a host bind mount. Foreground, --rm."""
    require_digest(image)
    if role not in ("out", "vin"):
        raise ValueError("put role must be out or vin")
    return ["run", "--rm", "-i", "--name", container_name(nonce, "stage")] + _hardening(cfg.get("limits")) + [
        "--network", "none", "-v", f"{volume_name(nonce, role)}:/dst", "-w", "/dst"] + _entry(
            image, "put", "--dest", "/dst")


def network_create_argv(name):
    return ["network", "create", "--internal", "--ipv6=false", name]


def proxy_connect_argv(nonce, deps=False):
    """The proxy is created on the internal network only, then also joined to the default bridge for egress."""
    return ["network", "connect", "bridge", container_name(nonce, "depsproxy" if deps else "proxy")]


def squid_template_path():
    """The ONE squid config source: plugin/sandbox/proxy/squid.conf.tmpl (also copied into the proxy image). In an
    installed .claude/bin it sits beside this script as super-board-squid.conf.tmpl."""
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "sandbox" / "proxy" / "squid.conf.tmpl", here / "super-board-squid.conf.tmpl",
                 here / "squid.conf.tmpl"):
        if cand.is_file():
            return cand
    raise RuntimeError("squid.conf.tmpl not found (plugin/sandbox/proxy/squid.conf.tmpl)")


def render_squid_conf(hosts):
    hs = []
    for h in hosts:
        if not HOST_RE.match(h or ""):
            raise ValueError(f"bad egress host: {h!r}")
        hs.append(h)
    if not hs:
        raise ValueError("no egress hosts")
    tmpl = squid_template_path().read_text(encoding="utf-8")
    if tmpl.count("{allow_hosts}") != 1:
        raise RuntimeError("squid.conf.tmpl must hold the {allow_hosts} placeholder exactly once")
    return tmpl.replace("{allow_hosts}", " ".join(hs))


def proxy_env_lines(hosts):
    b64 = base64.b64encode(render_squid_conf(hosts).encode()).decode()
    return [f"MCL_SQUID_CONF_B64={b64}"]


def proxy_argv(cfg, nonce, env_file, proxy_image, deps=False):
    """Non-root (uid 13), no caps, read-only, tmpfs only; conf comes from env-file MCL_SQUID_CONF_B64 (module doc)."""
    require_digest(proxy_image)
    role = "depsproxy" if deps else "proxy"
    squid_tmp = "rw,nosuid,nodev,size=64m,uid=13,gid=13"
    return ["run", "--name", container_name(nonce, role), "--rm=false", "--user", "13:13", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:" + squid_tmp, "--tmpfs", "/var/run:" + squid_tmp,
            "--tmpfs", "/var/log/squid:" + squid_tmp, "--tmpfs", "/var/spool/squid:" + squid_tmp,
            "--memory", "256m", "--pids-limit", "128",
            "--network", network_name(nonce, "deps" if deps else "int"), "--env-file", str(env_file),
            proxy_image, "sh", "-c",
            'printf %s "$MCL_SQUID_CONF_B64" | base64 -d > /tmp/squid.conf && exec squid -N -f /tmp/squid.conf']


# ---------------------------------------------------------------- env file

def _refuse_key(key, cfg):
    if not KEY_RE.match(key):
        raise ValueError(f"bad env key: {key!r}")
    if key in (cfg.get("env_deny") or []) or key in DENY_NAMES or key.startswith(DENY_PREFIXES):
        raise ValueError(f"env key refused: {key}")


def env_lines(tool, cfg, environ, nonce, model, hard_s=3600, idle_s=IDLE_S_DEFAULT):
    """A8: fixed Linux template + the dispatched tool's own auth keys + proxy vars + the control vars entry.py's
    `run` reads (MCL_TOOL, MCL_MODEL, MCL_IDLE_S, MCL_HARD_S, MCL_ENV_DENY). No host variable passes through.
    The model is checked with entry.py's own rule, so a bad one fails here, not in the container."""
    if not isinstance(model, str) or not MODEL_RE.match(model):
        raise ValueError(f"bad model: {model!r}")
    tool = norm_tool(tool)
    if not re.match(r"^[a-z][a-z0-9]{0,15}$", tool):
        raise ValueError(f"bad tool id: {tool!r}")
    deny = [str(n) for n in (cfg.get("env_deny") or [])]
    for n in deny:
        if not KEY_RE.match(n):
            raise ValueError(f"bad env_deny name: {n!r}")
    lines = list(FIXED_ENV)
    auth = tool_cfg(cfg, tool).get("auth") or {}
    keys = auth.get("keys") or ([auth["key"]] if auth.get("key") else [])
    for key in keys if auth.get("kind", "env") == "env" else []:
        _refuse_key(key, cfg)
        if key not in environ:
            raise ValueError(f"auth key {key} is not set in the environment")
        lines.append(f"{key}={environ[key]}")
    proxy = f"http://{container_name(nonce, 'proxy')}:3128"
    lines += [f"HTTPS_PROXY={proxy}", f"HTTP_PROXY={proxy}", f"https_proxy={proxy}", f"http_proxy={proxy}",
              "NO_PROXY=", "NODE_USE_ENV_PROXY=1", f"MCL_TOOL={tool}", f"MCL_MODEL={model}",
              f"MCL_IDLE_S={int(idle_s)}", f"MCL_HARD_S={int(hard_s)}"]
    if deny:
        lines.append("MCL_ENV_DENY=" + ",".join(deny))
    for line in lines:
        if any(c in line for c in "\r\n\x00"):
            raise ValueError("env value contains a newline")
    return lines


def deps_env_lines(nonce):
    """Proxy variables for the verify container (no secrets): the registries-only proxy on the deps network."""
    proxy = f"http://{container_name(nonce, 'depsproxy')}:3128"
    return [f"HTTPS_PROXY={proxy}", f"HTTP_PROXY={proxy}", f"https_proxy={proxy}", f"http_proxy={proxy}",
            "NO_PROXY=", "NODE_USE_ENV_PROXY=1"]


def _icacls(path):
    user = os.environ.get("USERNAME") or os.getlogin()
    subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"], check=True,
                   capture_output=True)


def write_env_file(path, lines, windows=None, acl=_icacls):
    """0600 on POSIX; on Windows icacls restricts it to the current user BEFORE the content is written.
    Returns key names only."""
    windows = (os.name == "nt") if windows is None else windows
    path = Path(path)
    data = ("\n".join(lines) + "\n").encode()
    if windows:
        path.write_bytes(b"")
        acl(path)
        with open(path, "wb") as fh:
            fh.write(data)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(path, 0o600)
    return [line.split("=", 1)[0] for line in lines]


# ---------------------------------------------------------------- result and tar (host side, A1)

PASSING_VERIFY = ("ok", "inherited")


def build_result(wait_info, facts, inputs, signatures_kind=None, verify_status=None):
    """mc-lane.result.v1 from three sources only: wait info (exit code, timeout), extract facts, dispatch inputs.
    Never reads any file the run container wrote. verify_status (the host-judged verdict, when there is one) folds in:
    a change only counts as `ok` when its tests passed (ok or inherited), else it is `verify_failed`."""
    code = wait_info.get("exit_code")
    if wait_info.get("timed_out") or code == 124:
        kind = "timeout"
    elif code == 0 and (facts.get("patch_bytes") or 0) > 0:
        kind = "ok"
    elif signatures_kind and signatures_kind != "ok":
        kind = signatures_kind
    else:
        kind = "no_changes" if code == 0 else "tool_failed"
    if kind == "ok" and verify_status is not None and verify_status not in PASSING_VERIFY:
        kind = "verify_failed"
    res = {"schema": "mc-lane.result.v1", "kind": kind, "exit_code": code}
    for k in ("nonce", "card", "lane", "attempt", "tool", "model"):
        res[k] = inputs.get(k)
    res["start_sha"] = facts.get("start_sha")
    res["patch_sha256"] = facts.get("patch_sha256")
    return res


def _intish(v):
    return int(v) if isinstance(v, (int, str)) and not isinstance(v, bool) and re.fullmatch(r"[0-9]{1,9}", str(v)) else None


def patch_paths(patch):
    """Every path a patch touches (both sides of a rename), from `git apply --numstat -z` in an empty scratch repo."""
    if not patch.strip():                           # no changes: git apply would call it an error
        return []
    git = shutil.which("git")
    if not git:
        raise ValueError("git is needed to list the paths of lane.patch")
    with tempfile.TemporaryDirectory(prefix="mcl-np-") as d:
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR") if k in os.environ}
        env.update({"HOME": d, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                    "LC_ALL": "C"})
        subprocess.run([git, "init", "-q", d], env=env, capture_output=True, check=True, timeout=60)
        r = subprocess.run([git, "-c", "core.quotepath=false", "apply", "--numstat", "-z"], cwd=d, env=env, input=patch,
                           capture_output=True, timeout=120)
    if r.returncode != 0:
        raise ValueError("git apply --numstat cannot read lane.patch")
    toks, paths, i = r.stdout.split(b"\0"), [], 0
    while i < len(toks):
        m = re.match(rb"^(\d+|-)\t(\d+|-)\t(.*)$", toks[i], re.S)
        i += 1
        if not m:
            continue
        if m.group(3):
            paths.append(m.group(3))
        else:                                       # rename / copy: "a<TAB>b<TAB>" NUL old NUL new
            paths += toks[i:i + 2]
            i += 2
    for line in patch.split(b"\n"):                 # numstat -z names only the new side of a rename
        if line.startswith(b"rename from "):
            paths.append(line[len(b"rename from "):].rstrip(b"\r").strip(b'"'))
    return [p.decode("utf-8", "replace") for p in paths if p]


def harness_files(patch):
    """A13: changed test-harness files (conftest.py, pytest.ini, pyproject.toml, setup.cfg, tox.ini, vitest/vite/jest
    config, tsconfig*.json, package.json), sorted, from the patch only."""
    return sorted({p for p in patch_paths(patch) if HARNESS_RE.match(p.rsplit("/", 1)[-1])})


def build_host_facts(bundle_path, wait_info, inputs, expected_start=None):
    """The top-level facts.json (A1): built on the runner host from (1) the validated bundle.tar the extract
    container wrote (lane.patch, its facts.json), (2) the `docker wait` result, (3) the dispatch inputs. Nothing
    from the run container's /out is read. The patch hash is recomputed here, never trusted."""
    names = validate_tar(bundle_path, BUNDLE_MAX)
    if "lane.patch" not in names or "facts.json" not in names:
        raise ValueError("bundle.tar lacks lane.patch or facts.json")
    with tarfile.open(bundle_path, "r:") as tf:
        patch = tf.extractfile("lane.patch").read(PATCH_MAX + 1)
        inner = tf.extractfile("facts.json").read(FACTS_MAX + 1)
    if len(patch) > PATCH_MAX or len(inner) > FACTS_MAX:
        raise ValueError("bundle member over its cap")
    try:
        inner = json.loads(inner.decode("utf-8"))
    except ValueError:
        raise ValueError("bundle facts.json is not JSON")
    if not isinstance(inner, dict) or not SHA_RE.match(str(inner.get("start_sha"))):
        raise ValueError("bundle facts.json has no valid start_sha")
    digest = hashlib.sha256(patch).hexdigest()
    if inner.get("patch_sha256") != digest:
        raise ValueError("extract facts patch_sha256 does not match lane.patch")
    if expected_start and inner["start_sha"] != expected_start:
        raise ValueError("extract start_sha differs from the staged start commit")
    code = wait_info.get("exit_code")
    return {
        "schema": "mc-lane.facts.v1", "nonce": _nonce(inputs.get("nonce")), "card": _intish(inputs.get("card")),
        "lane": inputs.get("lane"), "attempt": _intish(inputs.get("attempt")), "tool": norm_tool(inputs.get("tool")),
        "model": inputs.get("model"), "start_sha": inner["start_sha"], "patch_sha256": digest,
        "patch_bytes": len(patch), "harness_files": harness_files(patch), "exit_code": code if isinstance(code, int) and not isinstance(code, bool) else None,
        "timed_out": bool(wait_info.get("timed_out")) or code == 124}


def validate_tar(path, max_total):
    """Member names of a tar that holds only regular files with safe relative names within max_total bytes."""
    names, total = [], 0
    with tarfile.open(path, "r:*") as tf:
        for m in tf:
            n = m.name
            if not m.isreg():
                raise ValueError(f"tar member is not a regular file: {n}")
            if n.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", n) or "\\" in n or ".." in n.split("/"):
                raise ValueError(f"unsafe tar path: {n}")
            total += m.size
            if total > max_total:
                raise ValueError("tar exceeds size limit")
            names.append(n)
    return names


# ---------------------------------------------------------------- docker entry point and containment

def _docker_prefix():
    raw = os.environ.get("MCL_DOCKER_ARGV")
    if raw:
        return [str(a) for a in json.loads(raw)]
    exe = os.environ.get("MCL_DOCKER") or "docker"
    found = shutil.which(exe)
    if not found:
        raise RuntimeError(f"docker executable not found: {exe}")
    return [found]


def _docker(args, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 120)
    if kw["text"]:                      # never the console's code page: tool and test output is arbitrary bytes
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    return subprocess.run(_docker_prefix() + [str(a) for a in args], **kw)


class DockerContainment:
    """Same start/kill/close shape as lane-delegate's Containment. close() removes everything named mcl-<nonce>-*
    (containers, then networks, then volumes), trying each even if one fails. Use as a context manager."""

    def __init__(self, nonce, docker=_docker):
        self.nonce = _nonce(nonce)
        self.docker = docker
        self.actions = []
        self.started = []

    def _do(self, args):
        try:
            r = self.docker(args)
            self.actions.append((list(args), r.returncode))
            return r
        except Exception as e:                      # keep cleaning no matter what
            self.actions.append((list(args), f"{type(e).__name__}: {e}"))
            return None

    def start(self, argv):
        argv = list(argv)
        if argv[0] == "run" and "-d" not in argv:
            argv.insert(1, "-d")
        name = argv[argv.index("--name") + 1] if "--name" in argv else None
        r = self.docker(argv)
        self.actions.append((argv, r.returncode))
        if r.returncode != 0:
            raise RuntimeError(f"docker run failed: {(r.stderr or '').strip()[:300]}")
        if name:
            self.started.append(name)
        return name

    def wait(self, name, deadline_s, poll_s=1.0, sleep=time.sleep, clock=time.monotonic):
        """Poll `inspect .State.Running`; once stopped `docker wait` returns the exit code. Past the deadline the
        container is killed and the result is exit 124 / timed_out."""
        end = clock() + deadline_s
        while True:
            r = self.docker(["inspect", "-f", "{{.State.Running}}", name])
            if r.returncode == 0 and r.stdout.strip() == "false":
                w = self.docker(["wait", name])
                try:
                    return {"exit_code": int(w.stdout.strip()), "timed_out": False}
                except ValueError:
                    return {"exit_code": None, "timed_out": False}
            if r.returncode != 0:
                return {"exit_code": None, "timed_out": False}
            if clock() >= end:
                self.kill(name)
                return {"exit_code": 124, "timed_out": True}
            sleep(poll_s)

    def kill(self, name=None):
        for n in [name] if name else list(self.started):
            self._do(["rm", "-f", n])

    def close(self):
        n = self.nonce
        for role in CONTAINER_ROLES:
            self._do(["rm", "-f", container_name(n, role)])
        for role in NETWORK_ROLES:
            self._do(["network", "rm", network_name(n, role)])
        for role in VOLUME_ROLES:
            self._do(["volume", "rm", "-f", volume_name(n, role)])
        return self.actions

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ---------------------------------------------------------------- CLI (called by sandbox/lane.yml)

BRIEF_MAX = 2_000_000


def _load_config(path):
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read config: {e}")
    if not isinstance(cfg, dict):
        raise ValueError("config must be a JSON object")
    return cfg


def _setup(a, need_model=True):
    """Common validation. Returns (cfg, nonce, tool, image, state_dir, out_dir)."""
    nonce = _nonce(a.nonce)
    cfg = _load_config(a.config)
    tool = norm_tool(a.tool)
    if not re.match(r"^[a-z][a-z0-9]{0,15}$", tool):
        raise ValueError("bad tool id")
    if need_model and not MODEL_RE.match(a.model or ""):
        raise ValueError("bad model")
    image = require_digest(cfg.get("image"))
    check_local(cfg, tool, image, os.environ)
    out_dir = Path(a.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    state = out_dir.parent / "state"       # never inside out_dir: out_dir is what the workflow uploads
    state.mkdir(parents=True, exist_ok=True)
    return cfg, nonce, tool, image, state, out_dir


def _timeout_s(a):
    try:
        m = int(a.timeout_min)
    except (TypeError, ValueError):
        raise ValueError("timeout-min must be an integer")
    if not 5 <= m <= 120:
        raise ValueError("timeout-min must be 5..120")
    return m * 60


def _need(r, what):
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
        raise RuntimeError(f"{what} failed (rc {r.returncode}): {err.strip()[:300]}")
    return r


def make_put_tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size, ti.mode, ti.mtime = len(data), 0o644, 0
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _put(cfg, nonce, image, role, members):
    _need(_docker(put_argv(cfg, nonce, image, role), input=make_put_tar(members), text=False, timeout=300),
          f"put into {role}")


def _start_deps(cfg, nonce, state, dk):
    """verify_network: the second network and the proxy that allows only the package registries (A7)."""
    if not cfg.get("verify_network"):
        return
    _need(_docker(network_create_argv(network_name(nonce, "deps"))), "deps network create")
    de = state / f"{nonce}-depsproxy.env"
    write_env_file(de, proxy_env_lines(list(cfg.get("deps_hosts") or DEFAULT_DEPS_HOSTS)))
    dk.start(proxy_argv(cfg, nonce, de, require_digest(cfg.get("proxy_image")), deps=True))
    _need(_docker(proxy_connect_argv(nonce, deps=True)), "deps proxy bridge connect")


def cmd_stage(a):
    cfg, nonce, tool, image, state, _out = _setup(a)
    if not SHA_RE.match(a.start_sha or ""):
        raise ValueError("start-sha must be 40 hex")
    seed = Path(a.seed or "")
    if not seed.is_dir():
        raise ValueError("seed directory not found")
    brief = Path(a.brief or "").read_bytes()
    if len(brief) > BRIEF_MAX:
        raise ValueError("brief too large")
    (state / "stage.json").write_text(json.dumps({"start_sha": a.start_sha, "seed": str(seed.resolve())}))
    hosts = list(tool_cfg(cfg, tool).get("egress") or [])
    dk = DockerContainment(nonce)
    _need(_docker(network_create_argv(network_name(nonce, "int"))), "network create")
    if hosts:                                   # no egress hosts (the fake tool): no proxy, no route out at all
        pe = state / f"{nonce}-proxy.env"
        write_env_file(pe, proxy_env_lines(hosts))
        dk.start(proxy_argv(cfg, nonce, pe, require_digest(cfg.get("proxy_image"))))
        _need(_docker(proxy_connect_argv(nonce)), "proxy bridge connect")
    _start_deps(cfg, nonce, state, dk)
    for role in ("wt", "out", "pristine", "repo", "x"):
        _need(_docker(["volume", "create", volume_name(nonce, role)]), f"volume create {role}")
    for target in ("wt", "pristine", "repo"):
        _need(_docker(stage_argv(cfg, nonce, image, str(seed.resolve()), a.start_sha, target), timeout=900),
              f"stage {target}")
    _put(cfg, nonce, image, "out", {"brief": brief})
    return 0


def cmd_run(a):
    cfg, nonce, tool, image, state, _out = _setup(a)
    hard_s = _timeout_s(a)
    envf = state / f"{nonce}.env"
    lines = env_lines(tool, cfg, os.environ, nonce, a.model, hard_s=hard_s)
    write_env_file(envf, lines)
    try:
        dk = DockerContainment(nonce)
        name = dk.start(run_argv(cfg, nonce, envf, image, tool))
        info = dk.wait(name, hard_s + GRACE_S)
    finally:
        envf.unlink(missing_ok=True)            # holds the tool's key: gone as soon as the container is
    info["timed_out"] = bool(info.get("timed_out")) or info.get("exit_code") == 124
    (state / "run.json").write_text(json.dumps(info))
    return 0


def _wait_info(state):
    try:
        info = json.loads((state / "run.json").read_text())
        return {"exit_code": info.get("exit_code"), "timed_out": bool(info.get("timed_out"))}
    except (OSError, ValueError):
        return {"exit_code": None, "timed_out": False}


def cmd_extract(a):
    cfg, nonce, tool, image, state, out_dir = _setup(a)
    dk = DockerContainment(nonce)
    name = dk.start(extract_argv(cfg, nonce, image))
    info = dk.wait(name, 900)
    if info["exit_code"] != 0:
        raise RuntimeError(f"extract container exited {info['exit_code']}")
    bundle = out_dir / "bundle.tar"
    _need(_docker(["cp", f"{name}:/x/bundle.tar", str(bundle)], timeout=600), "docker cp bundle.tar")
    try:
        expected = json.loads((state / "stage.json").read_text()).get("start_sha")
    except (OSError, ValueError):
        expected = None
    inputs = {"nonce": nonce, "card": a.card, "lane": a.lane, "attempt": a.attempt, "tool": tool, "model": a.model}
    facts = build_host_facts(bundle, _wait_info(state), inputs, expected)      # validates the tar first
    (out_dir / "facts.json").write_text(json.dumps(facts, sort_keys=True))
    return 0


def _read_bundle_patch(bundle):
    validate_tar(bundle, BUNDLE_MAX)
    with tarfile.open(bundle, "r:") as tf:
        return tf.extractfile("lane.patch").read(PATCH_MAX + 1)


def _strip_ctrl(text):
    """Drop control characters except newline and tab (A10)."""
    return "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127))


def _cmd_list(v):
    if v is None:
        return []
    return [v] if isinstance(v, str) else [str(i) for i in v]


def _exec(nonce, argv, left_s):
    """One `docker exec` under the host's deadline: (exit code from Docker, output tail, timed_out). Past the
    deadline the whole verify container is removed (the exec'd process dies with it)."""
    try:
        r = _docker(argv, timeout=max(left_s, 1), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        _docker(["rm", "-f", container_name(nonce, "verify")])
        return 124, "deadline exceeded", True
    out = (r.stdout or "") + (r.stderr or "")
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    return r.returncode, _strip_ctrl(out)[-TAIL_CHARS:], False


def _apply_error(tail):
    try:
        return str(json.loads(tail.strip().splitlines()[-1]).get("error") or tail)[-TAIL_CHARS:]
    except (ValueError, IndexError, AttributeError):
        return tail


def verify_pass(cfg, nonce, image, state, patch, setup, cmds, budget_s, clock=time.monotonic):
    """One verify container, driven entirely from the host (A13). patch=None skips the apply (the base run).
    Returns (status, results): ok | verify_failed | apply_failed | timeout | setup_failed. A failed setup skips the
    verify commands; a failed verify command does not stop the next one."""
    envf = None
    if cfg.get("verify_network"):
        envf = state / f"{nonce}-verify.env"
        write_env_file(envf, deps_env_lines(nonce))
    name = DockerContainment(nonce).start(verify_argv(cfg, nonce, image, envf))
    end, results, status = clock() + budget_s, [], "ok"
    try:
        if patch is not None:
            rc, tail, timed = _exec(nonce, prepare_argv(nonce), end - clock())
            if timed or rc != 0:
                results.append({"cmd": "git apply --index", "exit": rc, "tail": _apply_error(tail), "phase": "prepare"})
                return ("timeout" if timed else "apply_failed"), results
        for phase, items in (("setup", setup), ("verify", cmds)):
            for c in items:
                left = end - clock()
                if left <= 0:
                    results.append({"cmd": c, "exit": 124, "tail": "deadline exceeded before start", "phase": phase})
                    return "timeout", results
                rc, tail, timed = _exec(nonce, exec_argv(nonce, c), left)
                results.append({"cmd": c, "exit": rc, "tail": tail, "phase": phase})
                if timed:
                    return "timeout", results
                if rc != 0:
                    if phase == "setup":
                        return "setup_failed", results
                    status = "verify_failed"
        return status, results
    finally:
        _docker(["rm", "-f", name])


def _restage_repo(cfg, nonce, image, state):
    """A fresh /repo volume at start_sha, no patch (A14): the same stage step cmd_stage ran."""
    try:
        st = json.loads((state / "stage.json").read_text())
        seed, sha = Path(st["seed"]), st["start_sha"]
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("stage.json lacks the seed and start_sha")
    if not seed.is_dir():
        raise RuntimeError("seed directory is gone")
    _need(_docker(["volume", "rm", "-f", volume_name(nonce, "repo")]), "volume rm repo")
    _need(_docker(["volume", "create", volume_name(nonce, "repo")]), "volume create repo")
    _need(_docker(stage_argv(cfg, nonce, image, str(seed.resolve()), sha, "repo"), timeout=900), "stage base")


def judge(cfg, nonce, image, state, status, results, setup, budget_s):
    """A14: a failure on the patch only counts when the base does not fail the same way. Returns the verify.json."""
    out = {"status": status, "commands": results}
    if status not in ("verify_failed", "setup_failed"):
        return out
    failing = [r for r in results if r.get("phase") == "verify" and r["exit"] != 0]
    try:
        _restage_repo(cfg, nonce, image, state)
        bstatus, bres = verify_pass(cfg, nonce, image, state, None, setup, [r["cmd"] for r in failing], budget_s)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
        out["status"] = "verify_failed"
        out["notes"] = [f"base check unavailable: {type(e).__name__}"]
        return out
    out["base"] = {"status": bstatus, "commands": bres}
    if status == "setup_failed":
        out["status"] = "base_broken" if bstatus == "setup_failed" else "verify_failed"
        if bstatus == "setup_failed":
            out["notes"] = ["setup fails on the base too"]
        return out
    if bstatus in ("setup_failed", "timeout"):
        out["status"] = "verify_failed"
        out["notes"] = [f"base check inconclusive: {bstatus}"]
        for r in failing:
            r["inherited"] = False
        return out
    base_verify = [r for r in bres if r.get("phase") == "verify"]
    for i, r in enumerate(failing):
        r["inherited"] = i < len(base_verify) and base_verify[i]["exit"] != 0
    if all(r["inherited"] for r in failing):
        out["status"] = "inherited"             # the lane treats it as passing, but never as a plain ok (A14, review M6)
        out["notes"] = ["inherited from base: " + r["cmd"][:200] for r in failing]
    else:
        out["status"] = "verify_failed"
    return out


def cmd_verify(a):
    """Host-judged verify (A13, A14). Writes <out-dir>/verify.json itself; nothing a container wrote decides it."""
    cfg, nonce, tool, image, state, out_dir = _setup(a, need_model=False)
    patch = _read_bundle_patch(out_dir / "bundle.tar")
    try:
        minutes = int(cfg.get("verify_timeout_min") or 30)
    except (TypeError, ValueError):
        raise ValueError("verify_timeout_min must be an integer")
    if not 1 <= minutes <= 240:
        raise ValueError("verify_timeout_min must be 1..240")
    setup, cmds = _cmd_list(cfg.get("verify_setup")), _cmd_list(cfg.get("verify_commands"))
    target = out_dir / "verify.json"
    if not cmds:                                    # zero commands is never ok (A3)
        target.write_text(json.dumps({"status": "verify_failed", "commands": [],
                                      "notes": ["no verify commands configured"]}))
        return 0
    for role in ("vin", "vx"):
        _need(_docker(["volume", "create", volume_name(nonce, role)]), f"volume create {role}")
    _put(cfg, nonce, image, "vin", {"lane.patch": patch})
    budget = minutes * 60
    status, results = verify_pass(cfg, nonce, image, state, patch, setup, cmds, budget)
    target.write_text(json.dumps(judge(cfg, nonce, image, state, status, results, setup, budget)))
    return 0


EXEC_READ_CHUNK = 65536


def _stream_exec(nonce, argv, left_s, write):
    """`docker exec` whose merged stdout+stderr goes to write() as it arrives: utf-8 with replacement, control
    characters stripped (A10). Past the deadline the verify container is removed. Returns the docker exit code (124 on timeout)."""
    proc = subprocess.Popen(_docker_prefix() + [str(a) for a in argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    done, timed = threading.Event(), threading.Event()

    def watchdog():
        if not done.wait(max(left_s, 1)):
            timed.set()
            _docker(["rm", "-f", container_name(nonce, "verify")])

    threading.Thread(target=watchdog, daemon=True).start()
    decoder = codecs.getincrementaldecoder("utf-8")("replace")          # a character split across two chunks stays whole
    try:
        fd = proc.stdout.fileno()
        while True:
            raw = os.read(fd, EXEC_READ_CHUNK)                              # bounded: output with no newline cannot pile up
            if not raw:
                break
            text = _strip_ctrl(decoder.decode(raw))
            if text:
                write(text)
        tail = _strip_ctrl(decoder.decode(b"", final=True))
        if tail:
            write(tail)
        rc = proc.wait()
    finally:
        done.set()
        proc.stdout.close()
    return 124 if timed.is_set() else rc


def exec_sandbox(cfg, nonce, seed, start_sha, command, state, write, shell=True):
    """Run ONE command in a fresh verify container staged from a bare seed at start_sha (spec section 14 H1): the host
    never runs it. verify_network and limits as for a verify run; deadline = verify_timeout_min. verify_setup (pilot.verify_setup) runs first
    in the same container, under the same deadline, and a failing setup is the result. With verify_network false the setup
    has no network: the gate image (pilot.sandbox.gate_image) must already carry the project's dependencies. Returns the
    docker exit code of the command (124 on timeout). shell=False (lane=exec): COMMAND is an argv list that goes to docker exec
    untouched (no sh, no join, no quoting); verify_setup stays a shell line. The caller owns cleanup(nonce)."""
    image = require_digest(cfg.get("gate_image") or cfg.get("image"))
    if not SHA_RE.match(start_sha or ""):
        raise ValueError("start-sha must be 40 hex")
    seed = Path(seed)
    if not seed.is_dir():
        raise ValueError("seed directory not found")
    if not command:
        raise ValueError("no command")
    try:
        minutes = max(1, math.ceil(float(cfg.get("verify_timeout_min") or 30)))
    except (TypeError, ValueError):
        raise ValueError("verify_timeout_min must be a number")
    if minutes > 240:
        raise ValueError("verify_timeout_min must be 1..240")
    dk = DockerContainment(nonce)
    state.mkdir(parents=True, exist_ok=True)
    _start_deps(cfg, nonce, state, dk)
    for role in ("repo", "vin", "vx"):
        _need(_docker(["volume", "create", volume_name(nonce, role)]), f"volume create {role}")
    _need(_docker(stage_argv(cfg, nonce, image, str(seed.resolve()), start_sha, "repo"), timeout=900), "stage repo")
    envf = None
    if cfg.get("verify_network"):
        envf = state / f"{nonce}-verify.env"
        write_env_file(envf, deps_env_lines(nonce))
    dk.start(verify_argv(cfg, nonce, image, envf))
    end = time.monotonic() + minutes * 60                       # one deadline for verify_setup and the command together
    for setup_cmd in _cmd_list(cfg.get("verify_setup")):        # the same setup the verify run does (the container is fresh)
        left = end - time.monotonic()
        if left <= 0:
            write("exec --sandbox: deadline exceeded before verify_setup finished\n")
            return 124
        rc = _stream_exec(nonce, exec_argv(nonce, setup_cmd), left, write)
        if rc != 0:
            write(f"exec --sandbox: verify_setup failed ({rc}): {_strip_ctrl(setup_cmd)[:200]}\n")
            return rc
    left = end - time.monotonic()
    if left <= 0:
        write("exec --sandbox: deadline exceeded before the command started\n")
        return 124
    if not shell:
        return _stream_exec(nonce, exec_argv(nonce, list(command), shell=False), left, write)
    line = str(command[0]) if len(command) == 1 else shlex.join([str(c) for c in command])     # one word = a shell line
    return _stream_exec(nonce, exec_argv(nonce, line), left, write)


GATE_DOC_NO_COMMANDS = {"status": "verify_failed", "commands": [], "notes": ["no verify commands configured"]}


def cmd_gate_verify(a):
    """The one implementation of the merge gate's verify (spec section 10), used by lane-delegate (where=local) and by
    lane.yml (where=lenovo): stage --start-sha into /repo (merging --merge-sha, the base, into it when given), run
    verify_setup / verify_commands as host-driven docker execs with no patch (A13), write <out-dir>/verify.json.
    No tool runs, so there is no tool proxy and no A6 smoke-only rule: any digest-pinned image is accepted. The base
    comparison (A14) stays with the caller, which asks again for the base commit. Exit 0 = a verdict file was written
    (a merge conflict is the status `merge_conflict`); anything else is an error and no verdict."""
    nonce = _nonce(a.nonce)
    cfg = _load_config(a.config)
    image = require_digest(cfg.get("image"))
    if not SHA_RE.match(a.start_sha or ""):
        raise ValueError("start-sha must be 40 hex")
    if a.merge_sha is not None and not SHA_RE.match(a.merge_sha):
        raise ValueError("merge-sha must be 40 hex")
    seed = Path(a.seed or "")
    if not seed.is_dir():
        raise ValueError("seed directory not found")
    try:
        minutes = max(1, math.ceil(float(cfg.get("verify_timeout_min") or 30)))
    except (TypeError, ValueError):
        raise ValueError("verify_timeout_min must be a number")
    if minutes > 240:
        raise ValueError("verify_timeout_min must be 1..240")
    out_dir = Path(a.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    state = out_dir.parent / "state"
    state.mkdir(parents=True, exist_ok=True)
    setup, cmds = _cmd_list(cfg.get("verify_setup")), _cmd_list(cfg.get("verify_commands"))
    target = out_dir / "verify.json"
    if not cmds:                                    # zero commands is never ok (A3)
        target.write_text(json.dumps(GATE_DOC_NO_COMMANDS))
        return 0
    (state / "stage.json").write_text(json.dumps({"start_sha": a.start_sha, "seed": str(seed.resolve())}))
    _start_deps(cfg, nonce, state, DockerContainment(nonce))
    for role in ("repo", "vin", "vx"):
        _need(_docker(["volume", "create", volume_name(nonce, role)]), f"volume create {role}")
    st = _docker(stage_argv(cfg, nonce, image, str(seed.resolve()), a.start_sha, "repo", a.merge_sha), timeout=900)
    if st.returncode == STAGE_MERGE_CONFLICT and a.merge_sha:
        target.write_text(json.dumps({"status": "merge_conflict", "commands": [],
                                      "notes": ["merge conflict with base"]}))
        return 0
    _need(st, "stage repo")
    status, results = verify_pass(cfg, nonce, image, state, None, setup, cmds, minutes * 60)
    target.write_text(json.dumps({"status": "verify_failed" if status == "setup_failed" else status, "commands": results}))
    return 0


EXEC_MAX_JSON = 4096
EXEC_LOG_CAP = 5_000_000


def check_exec_command(cmd):
    """The lane=exec argv rules (same as lane-remote validate_exec_command; lane.yml re-checks them): a non-empty list of
    non-empty printable-ASCII strings, none with `$(` or a backtick, at most 4096 bytes as compact JSON."""
    if not isinstance(cmd, list) or not cmd:
        raise ValueError("command must be a non-empty argv list")
    for c in cmd:
        if not isinstance(c, str) or not c or not all(0x20 <= ord(ch) <= 0x7e for ch in c):
            raise ValueError("every command element must be a non-empty printable ASCII string")
        if "$(" in c or "`" in c:
            raise ValueError("command elements must not contain $( or a backtick")
    if len(json.dumps(cmd, separators=(",", ":")).encode("utf-8")) > EXEC_MAX_JSON:
        raise ValueError(f"command is over {EXEC_MAX_JSON} bytes")
    return cmd


def cmd_exec_sandbox(a):
    """lane=exec on the runner: stage --start-sha from the seed, run the argv list from --command-file in the verify container
    with no shell, append the output to <out-dir>/exec.log (capped) and write <out-dir>/exec.json {"rc": N}. Exit 0 = the
    command ran (any rc); anything else is a staging or usage error. The caller owns cleanup."""
    nonce = _nonce(a.nonce)
    cfg = _load_config(a.config)
    if not SHA_RE.match(a.start_sha or ""):
        raise ValueError("start-sha must be 40 hex")
    try:
        cmd = json.loads(Path(a.command_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read command file: {e}")
    check_exec_command(cmd)
    out_dir = Path(a.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path, size = out_dir / "exec.log", [0]

    def write(text):
        data = text.encode("utf-8", "replace")[:max(0, EXEC_LOG_CAP - size[0])]
        if data:
            size[0] += len(data)
            with open(log_path, "ab") as fh:
                fh.write(data)

    log_path.write_bytes(b"")
    try:
        rc = exec_sandbox(cfg, nonce, a.seed, a.start_sha, cmd, out_dir.parent / "state", write, shell=False)
    except Exception as e:
        # exec.json always exists: the delegate reads a null rc as a runner failure (70); the raise still fails the step
        msg = "".join(ch for ch in f"{type(e).__name__}: {e}" if ord(ch) >= 32 and ord(ch) != 127)[:200]
        (out_dir / "exec.json").write_text(json.dumps({"rc": None, "error": msg}), encoding="utf-8")
        raise
    (out_dir / "exec.json").write_text(json.dumps({"rc": int(rc)}), encoding="utf-8")
    return 0


def cmd_cleanup(a):
    DockerContainment(_nonce(a.nonce)).close()
    return 0


def build_parser():
    p = argparse.ArgumentParser(description="Docker sandbox provider for external tool lanes")
    sub = p.add_subparsers(dest="cmd", required=True)
    gv = sub.add_parser("gate-verify")
    for flag in ("--nonce", "--config", "--out-dir", "--seed", "--start-sha"):
        gv.add_argument(flag, required=True)
    gv.add_argument("--merge-sha")
    gv.set_defaults(fn=cmd_gate_verify)
    ex = sub.add_parser("exec-sandbox")
    for flag in ("--nonce", "--config", "--out-dir", "--seed", "--start-sha", "--command-file"):
        ex.add_argument(flag, required=True)
    ex.set_defaults(fn=cmd_exec_sandbox)
    for name, fn in (("stage", cmd_stage), ("run", cmd_run), ("extract", cmd_extract), ("verify", cmd_verify),
                     ("cleanup", cmd_cleanup)):
        sp = sub.add_parser(name)
        sp.add_argument("--nonce", required=True)
        if name != "cleanup":
            sp.add_argument("--config", required=True)
            sp.add_argument("--tool", required=True)
            sp.add_argument("--model", required=True)
            sp.add_argument("--lane", required=True)
            sp.add_argument("--timeout-min", required=True)
            sp.add_argument("--out-dir", required=True)
        if name == "stage":
            sp.add_argument("--seed", required=True)
            sp.add_argument("--brief", required=True)
            sp.add_argument("--start-sha", required=True)
        if name == "extract":
            sp.add_argument("--card")
            sp.add_argument("--attempt")
        sp.set_defaults(fn=fn)
    return p


def main(argv=None):
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return 0 if e.code in (0, None) else 64
    try:
        return a.fn(a)
    except ValueError as e:
        print(f"usage error: {e}", file=sys.stderr)
        return 64
    except (RuntimeError, OSError, subprocess.SubprocessError, tarfile.TarError) as e:
        print(f"docker lane error: {type(e).__name__}: {e}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
