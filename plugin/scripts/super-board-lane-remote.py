#!/usr/bin/env python3
"""Surface side of a Lenovo lane (design spec kept in the maintainers' docs, not shipped).

  dispatch  validate inputs (A4 rules), start the lane.yml workflow in the runner repo, print the run id
  wait      poll the run until it completes; at the deadline cancel it and report a timeout
  download  fetch artifact mc-lane-<nonce> as a raw zip, validate every entry, extract nothing unsafe
  validate  check nonce / start_sha / patch hash / verify.json of an extracted artifact

Everything inside the artifact is data produced next to untrusted tool code. It is checked here and
never executed. Text from the artifact or from gh is stripped of control characters before printing.

Exit codes: 0 ok / 4 isolation (artifact failed a check) / 5 lane timed out (wait) / 64 usage
(bad input, brief too big) / 70 internal (gh missing or too old, gh call failed, run or artifact not found).

gh is called as `gh` unless MC_LANE_GH_CMD holds a JSON list (tests: [python, stub.py]).
`gh workflow run --json` reads the inputs as JSON from stdin (checked in `gh workflow run --help`,
gh 2.101.0: "--json  Read workflow inputs as JSON via STDIN"), so the brief never appears on argv.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile

EXIT_OK, EXIT_ISOLATION, EXIT_TIMEOUT, EXIT_USAGE, EXIT_INTERNAL = 0, 4, 5, 64, 70

MIN_GH = (2, 63, 1)
MAX_INPUT_BYTES = 60000
NONCE_RE = re.compile(r"^[a-f0-9]{32}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^(?!\.{1,2}/)[A-Za-z0-9_.-]{1,100}/(?!\.{1,2}$)[A-Za-z0-9_.-]{1,100}$")      # owner and name are never . or ..
WORKFLOW_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}\.ya?ml$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,79}$")
B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
DEFAULT_LANES = ("build", "qa", "review", "gate", "exec", "delegate")
TIMEOUT_RANGE = (5, 120)
INT_RANGES = {"card": (1, 10_000_000), "attempt": (1, 99)}
DELEGATE_CARD_MIN = 0        # lane=delegate is not a card: it alone may carry card 0

# These caps equal the ones entry.py (PATCH_MAX, RAW_LOG_MAX, REPORT_MAX, SESSION_MAX, BUNDLE_MAX) and the docker
# module (BUNDLE_MAX) enforce while building the bundle; the contract tests compare them.
ARTIFACT_NAMES = {"bundle.tar": 46_000_000, "verify.json": 1_000_000, "facts.json": 64_000}
ZIP_TOTAL_CAP = 47_000_000
# facts.json inside the bundle is extract's own; the top-level facts.json (host-built) is what validate trusts.
# lane=exec uploads x/ with exactly these two files (no bundle, no patch, no verify.json).
EXEC_ARTIFACT_NAMES = {"exec.log": 5_000_000, "exec.json": 4096}
EXEC_MAX_JSON = 4096
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
BUNDLE_CAPS = {"lane.patch": 20_000_000, "raw.log": 5_000_000, "report.json": 64_000, "facts.json": 64_000}
VERIFY_STATUSES = ("ok", "inherited", "verify_failed", "apply_failed", "timeout", "base_broken", "merge_conflict")
SESSION_RE = re.compile(r"^session/[A-Za-z0-9._-]{1,100}\.jsonl$")
SESSION_TOTAL_CAP = 20_000_000
BUNDLE_TOTAL_CAP = 45_000_000

CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class LaneError(Exception):
    code = EXIT_INTERNAL


class UsageError(LaneError):
    code = EXIT_USAGE


class IsolationError(LaneError):
    code = EXIT_ISOLATION


class InternalError(LaneError):
    code = EXIT_INTERNAL


def norm_tool(tool) -> str:
    """One tool id everywhere (`pi-delegate` -> `pi`); the docker module and lane.yml use the same rule."""
    tool = str(tool or "")
    return tool[:-len("-delegate")] if tool.endswith("-delegate") else tool


def clean(value, limit=300) -> str:
    """A10: text that came from gh or the artifact, safe to print."""
    text = CONTROL_RE.sub("?", str(value))
    return text if len(text) <= limit else text[:limit] + "..."


# ---------------------------------------------------------------- gh

class Gh:
    def __init__(self, cmd=None):
        if cmd is None:
            raw = os.environ.get("MC_LANE_GH_CMD")
            cmd = json.loads(raw) if raw else ["gh"]
        self.cmd = list(cmd)

    def run(self, args, stdin: bytes | None = None, stdout_path: Path | None = None, timeout=300):
        argv = self.cmd + list(args)
        # Never inherit the caller's stdin: a gh (or stub) that reads it would block until the timeout.
        stdin = b"" if stdin is None else stdin
        try:
            if stdout_path is not None:
                with open(stdout_path, "wb") as fh:
                    proc = subprocess.run(argv, input=stdin, stdout=fh, stderr=subprocess.PIPE, timeout=timeout)
                out = b""
            else:
                proc = subprocess.run(argv, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
                out = proc.stdout
        except FileNotFoundError:
            raise InternalError("gh is not installed or not on PATH")
        except subprocess.TimeoutExpired:
            raise InternalError(f"gh {clean(' '.join(args[:3]))} timed out")
        if proc.returncode != 0:
            raise InternalError(f"gh {clean(' '.join(args[:3]))} failed ({proc.returncode}): "
                                f"{clean(proc.stderr.decode('utf-8', 'replace'))}")
        return out

    def json(self, args, **kw):
        out = self.run(args, **kw)
        try:
            return json.loads(out.decode("utf-8"))
        except ValueError:
            raise InternalError(f"gh {clean(' '.join(args[:3]))} returned invalid JSON")


def parse_gh_version(text: str):
    m = re.search(r"gh version (\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(x) for x in m.groups()) if m else None


def check_gh_version(gh: Gh):
    """A9: the minimum gh is 2.63.1."""
    out = gh.run(["--version"]).decode("utf-8", "replace")
    ver = parse_gh_version(out)
    if ver is None:
        raise InternalError("cannot read the gh version")
    if ver < MIN_GH:
        raise InternalError(f"gh {'.'.join(map(str, ver))} is too old, need >= {'.'.join(map(str, MIN_GH))}")
    return ver


def _poll_seconds() -> float:
    return float(os.environ.get("MC_LANE_POLL_S", "3"))


# ---------------------------------------------------------------- config

def remote_cfg_from_board(board: dict) -> dict:
    """What build_inputs needs, taken from the board config (pilot.sandbox, pilot.tools, pilot.timeouts_min)."""
    pilot = board.get("pilot") or {}
    sandbox = pilot.get("sandbox") or {}
    tools = pilot.get("tools") or []
    lanes = tuple(sorted(set(DEFAULT_LANES) | set((pilot.get("timeouts_min") or {}).keys())))
    return {
        "runner_repo": sandbox.get("runner_repo"),
        "workflow": sandbox.get("workflow") or "lane.yml",
        "repos": list(sandbox.get("repos") or []),
        "tools": [t["id"] for t in tools if isinstance(t, dict) and t.get("id")],
        "models": {t["id"]: t.get("models") for t in tools if isinstance(t, dict) and t.get("id")},
        "lanes": lanes,
        "tool_cfgs": [t for t in tools if isinstance(t, dict) and t.get("id")],
        # what the runner needs and nothing else; tools[] is narrowed to the dispatched tool by runner_payload()
        "payload": {
            "image": sandbox.get("image"),
            "proxy_image": sandbox.get("proxy_image"),
            "limits": sandbox.get("limits") or {},
            "env_deny": list(sandbox.get("env_deny") or []),
            "deps_hosts": list(sandbox.get("deps_hosts") or []),
            "verify_network": bool(sandbox.get("verify_network", False)),
            "verify_setup": list(pilot.get("verify_setup") or []),
            "verify_commands": list(board.get("verify_commands") or []),
            "verify_timeout_min": pilot.get("verify_timeout_min"),
        },
    }


def runner_payload(cfg: dict, tool: str) -> dict:
    """The config_b64 content: exactly the --config of super-board-lane-docker.py on the runner. Only the dispatched
    tool's entry (id normalised, auth shape and egress, never a secret value) goes along."""
    want = norm_tool(tool)
    payload = dict(cfg.get("payload") or {})
    payload["tools"] = [dict(t, id=norm_tool(t.get("id"))) for t in cfg.get("tool_cfgs") or []
                        if norm_tool(t.get("id")) == want]
    return payload


def load_remote_cfg(path) -> dict:
    try:
        board = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise UsageError(f"cannot read config {clean(path)}: {clean(e)}")
    return remote_cfg_from_board(board)


# ---------------------------------------------------------------- inputs (A4)

def encode_brief(brief: bytes) -> str:
    return base64.b64encode(gzip.compress(brief, mtime=0)).decode("ascii")


def encode_config(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")).decode("ascii")


def _int_in(name, value, lo, hi) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,9}", str(value)):
        raise UsageError(f"{name} must be an integer")
    n = int(value)
    if not lo <= n <= hi:
        raise UsageError(f"{name} must be between {lo} and {hi}")
    return n


def validate_exec_command(cmd) -> str:
    """The lane=exec command as the wire value `command_json`: a non-empty argv list of non-empty printable-ASCII strings
    (0x20..0x7e), none holding `$(` or a backtick, at most 4096 bytes as compact JSON. lane.yml checks the same rules again."""
    if isinstance(cmd, str):
        raise UsageError("command must be an argv list, not a shell string")
    if not isinstance(cmd, list) or not cmd:
        raise UsageError("command must be a non-empty argv list")
    for c in cmd:
        if not isinstance(c, str) or not c or not all(0x20 <= ord(ch) <= 0x7e for ch in c):
            raise UsageError("every command element must be a non-empty string of printable ASCII")
        if "$(" in c or "`" in c:
            raise UsageError("command elements must not contain $( or a backtick")
    wire = json.dumps(cmd, separators=(",", ":"))
    if len(wire.encode("utf-8")) > EXEC_MAX_JSON:
        raise UsageError(f"command is over {EXEC_MAX_JSON} bytes")
    return wire


def build_inputs(cfg, *, repo, start_sha, card, lane, attempt, tool, model, nonce, timeout_min,
                 brief_gz_b64, config_b64, base_sha="", command=None) -> dict:
    """The workflow inputs, all strings. Same rules the first step of lane.yml applies. base_sha is the merge gate's
    base commit: 40 hex and required when lane=gate (the runner merges it into start_sha before verifying), and it
    must be empty for every other lane. command is the lane=exec argv list (required there, None for every other lane)."""
    if not NONCE_RE.fullmatch(str(nonce)):
        raise UsageError("nonce must be 32 lowercase hex characters")
    if not SHA_RE.fullmatch(str(start_sha)):
        raise UsageError("start_sha must be 40 lowercase hex characters")
    if lane == "gate":
        if not SHA_RE.fullmatch(str(base_sha or "")):
            raise UsageError("base_sha must be 40 lowercase hex characters when lane=gate")
    elif base_sha:
        raise UsageError("base_sha is only for lane=gate")
    if lane == "exec":
        command_json = validate_exec_command(command)
    elif command is not None:
        raise UsageError("command is only for lane=exec")
    if repo not in (cfg.get("repos") or []):
        raise UsageError("repo is not in the configured allowlist")
    if lane not in (cfg.get("lanes") or DEFAULT_LANES):
        raise UsageError("lane is not allowed")
    if norm_tool(tool) not in {norm_tool(t) for t in cfg.get("tools") or []}:
        raise UsageError("tool is not allowed")
    wire_tool = norm_tool(tool)
    models = next((m for t, m in (cfg.get("models") or {}).items() if norm_tool(t) == wire_tool and m), None)
    if models:
        if model not in models:
            raise UsageError("model is not allowed for this tool")
    if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
        raise UsageError("model has characters outside [A-Za-z0-9._:/-]")
    card_lo, card_hi = INT_RANGES["card"]
    card_n = _int_in("card", card, DELEGATE_CARD_MIN if lane == "delegate" else card_lo, card_hi)
    attempt_n = _int_in("attempt", attempt, *INT_RANGES["attempt"])
    timeout_n = _int_in("timeout_min", timeout_min, *TIMEOUT_RANGE)
    for name, val in (("brief_gz_b64", brief_gz_b64), ("config_b64", config_b64)):
        if not isinstance(val, str) or not val or not B64_RE.fullmatch(val):
            raise UsageError(f"{name} must be non-empty base64")
    inputs = {
        "repo": repo, "start_sha": start_sha, "card": str(card_n), "lane": lane, "attempt": str(attempt_n),
        "tool": wire_tool, "model": model, "nonce": nonce, "timeout_min": str(timeout_n),
        "brief_gz_b64": brief_gz_b64, "config_b64": config_b64,
    }
    if lane == "gate":
        inputs["base_sha"] = base_sha
    if lane == "exec":
        inputs["command_json"] = command_json
    total = sum(len(k.encode()) + len(v.encode()) for k, v in inputs.items())
    if total > MAX_INPUT_BYTES:
        raise UsageError(f"inputs are {total} bytes, over the {MAX_INPUT_BYTES} limit (brief too big)")
    return inputs


# ---------------------------------------------------------------- dispatch / wait

def find_run(runner_repo, workflow, nonce, gh: Gh, find_timeout_s=60.0, poll_s=None) -> int:
    poll = _poll_seconds() if poll_s is None else poll_s
    want = f"lane {nonce}"
    end = time.time() + find_timeout_s
    while True:
        runs = gh.json(["run", "list", "--repo", runner_repo, "--workflow", workflow,
                        "--json", "databaseId,displayTitle,createdAt", "--limit", "20"])
        for run in runs if isinstance(runs, list) else []:
            if isinstance(run, dict) and run.get("displayTitle") == want and isinstance(run.get("databaseId"), int):
                return run["databaseId"]
        if time.time() >= end:
            raise InternalError("dispatched run not found by nonce")
        time.sleep(poll)


def dispatch(runner_repo, workflow, inputs, gh: Gh, find_timeout_s=60.0, poll_s=None) -> int:
    if not REPO_RE.fullmatch(str(runner_repo)):
        raise UsageError("runner_repo must be owner/name")
    if not WORKFLOW_RE.fullmatch(str(workflow)):
        raise UsageError("workflow must be a .yml file name")
    payload = json.dumps(inputs).encode("utf-8")
    gh.run(["workflow", "run", workflow, "--repo", runner_repo, "--json"], stdin=payload)
    return find_run(runner_repo, workflow, inputs["nonce"], gh, find_timeout_s, poll_s)


def wait(runner_repo, run_id, deadline, gh: Gh, poll_s=None) -> dict:
    """deadline is an epoch time. At the deadline the run is cancelled and the state is 'timeout'."""
    poll = _poll_seconds() if poll_s is None else poll_s
    failures = 0
    while True:
        try:
            view = gh.json(["run", "view", str(run_id), "--repo", runner_repo, "--json", "status,conclusion"])
            failures = 0
            if isinstance(view, dict) and view.get("status") == "completed":
                return {"state": "completed", "conclusion": clean(view.get("conclusion"))}
        except InternalError:
            failures += 1
            if failures >= 3:
                raise
        if time.time() >= deadline:
            cancelled = True
            try:
                gh.run(["run", "cancel", str(run_id), "--repo", runner_repo])
            except InternalError:
                cancelled = False
            return {"state": "timeout", "cancelled": cancelled}
        time.sleep(max(0.0, min(poll, deadline - time.time())))


# ---------------------------------------------------------------- zip (A9)

def _check_name(name: str, allowed) -> None:
    if (not name or any(ord(c) < 0x20 for c in name) or "\\" in name or name.startswith("/")
            or ":" in name or re.match(r"^[A-Za-z]:", name)):
        raise IsolationError(f"unsafe entry name {clean(name, 80)!r}")
    if any(p in ("", ".", "..") for p in name.split("/")):
        raise IsolationError(f"unsafe entry name {clean(name, 80)!r}")
    if name not in allowed:
        raise IsolationError(f"unexpected entry {clean(name, 80)!r}")


def safe_extract(zip_path, dest, allowed=None, per_file_caps=None, total_cap=ZIP_TOTAL_CAP) -> list:
    """Validate every entry first; extract only if all pass. Nothing is left behind on failure."""
    allowed = dict(ARTIFACT_NAMES if allowed is None else allowed)
    if per_file_caps:
        allowed.update(per_file_caps)
    dest = Path(dest)
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise IsolationError(f"artifact is not a valid zip: {clean(e)}")
    with zf:
        infos = zf.infolist()
        seen, total = set(), 0
        for info in infos:
            _check_name(info.filename, allowed)
            if info.filename in seen:
                raise IsolationError(f"duplicate entry {clean(info.filename, 80)!r}")
            seen.add(info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000 or info.filename.endswith("/") or info.is_dir():
                raise IsolationError(f"link or directory entry {clean(info.filename, 80)!r}")
            if mode not in (0, 0o100000):
                raise IsolationError(f"special file entry {clean(info.filename, 80)!r}")
            if info.file_size > allowed[info.filename]:
                raise IsolationError(f"entry {clean(info.filename, 80)!r} is over its size cap")
            total += info.file_size
        if total > total_cap:
            raise IsolationError("artifact is over the total size cap")
        written = []
        try:
            for info in infos:
                target = dest / info.filename
                cap = allowed[info.filename]
                size = 0
                with zf.open(info) as src, open(target, "wb") as out:
                    written.append(target)
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > cap:
                            raise IsolationError(f"entry {clean(info.filename, 80)!r} grew past its size cap")
                        out.write(chunk)
        except BaseException:
            for p in written:
                p.unlink(missing_ok=True)
            raise
    return [p.name for p in written]


def download(runner_repo, run_id, nonce, gh: Gh, out_dir=None, lane=None) -> Path:
    """Fetch artifact mc-lane-<nonce>. lane="exec" accepts exec.log and exec.json only; every other lane keeps the bundle rules."""
    if not REPO_RE.fullmatch(str(runner_repo)):
        raise UsageError("runner_repo must be owner/name")
    if not NONCE_RE.fullmatch(str(nonce)):
        raise UsageError("nonce must be 32 lowercase hex characters")
    check_gh_version(gh)
    name = f"mc-lane-{nonce}"
    listing = gh.json(["api", f"repos/{runner_repo}/actions/runs/{int(run_id)}/artifacts"])
    arts = [a for a in (listing.get("artifacts") if isinstance(listing, dict) else None) or []
            if isinstance(a, dict) and a.get("name") == name and not a.get("expired")]
    if len(arts) != 1 or not isinstance(arts[0].get("id"), int):
        raise InternalError(f"expected exactly one live artifact {name}, found {len(arts)}")
    if out_dir is None:
        dest = Path(tempfile.mkdtemp(prefix="mc-lane-"))
    else:
        dest = Path(out_dir)
        dest.mkdir(parents=True, exist_ok=True)
        if any(dest.iterdir()):
            raise UsageError("out dir is not empty")
    tmp = Path(tempfile.mkdtemp(prefix="mc-lane-zip-"))
    try:
        zpath = tmp / "artifact.zip"
        gh.run(["api", f"repos/{runner_repo}/actions/artifacts/{arts[0]['id']}/zip"], stdout_path=zpath)
        if zpath.stat().st_size > ZIP_TOTAL_CAP + 1_000_000:
            raise IsolationError("artifact zip is over the size cap")
        safe_extract(zpath, dest, allowed=EXEC_ARTIFACT_NAMES if lane == "exec" else None)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


# ---------------------------------------------------------------- on-origin check (F10)

def _branch_ok(branch) -> bool:
    return (isinstance(branch, str) and bool(BRANCH_RE.fullmatch(branch)) and not branch.startswith(("-", "/"))
            and ".." not in branch and "//" not in branch)


def read_branch_tip(slug, branch, gh: Gh):
    """The tip of BRANCH in repository SLUG itself, read through the API (repos/SLUG/git/ref/heads/BRANCH, object.sha), so the
    tip and the compare below talk about the same repository whatever the local origin remote says. None when the branch does not
    exist (HTTP 404); every other failure is a UsageError (64)."""
    if not REPO_RE.fullmatch(str(slug)):
        raise UsageError("slug must be owner/name")
    if not _branch_ok(branch):
        raise UsageError("branch name refused")
    try:
        doc = gh.json(["api", f"repos/{slug}/git/ref/heads/{branch}"])
    except InternalError as e:
        if "HTTP 404" in str(e):
            return None
        raise UsageError(f"cannot read the tip of {slug}@{branch}: {clean(e, 120)}")
    except OSError as e:
        raise UsageError(f"cannot run gh to read the tip of {slug}@{branch}: {clean(e, 120)}")
    obj = doc.get("object") if isinstance(doc, dict) else None
    sha = obj.get("sha") if isinstance(obj, dict) else None
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise UsageError("the API returned a malformed branch tip")
    return sha


def check_on_origin(repo_dir, slug, branch, sha, gh: Gh, read_tip) -> None:
    """Live proof that commit SHA is on origin's BRANCH: read the tip (read_tip(), which the delegate wires to read_branch_tip so
    the tip is the one of SLUG itself), ask the compare API for sha...tip (identical or ahead means sha is in the branch's
    history; a fork-network-only commit is not, which is why the commits API is never used), then read the tip again: a
    different tip is a race and fails closed. repo_dir is only for the caller's read_tip. Every failure is a UsageError (64)."""
    if not REPO_RE.fullmatch(str(slug)):
        raise UsageError("slug must be owner/name")
    if not SHA_RE.fullmatch(str(sha)):
        raise UsageError("sha must be 40 lowercase hex characters")
    if not _branch_ok(branch):
        raise UsageError("branch name refused")
    tip = read_tip()
    if not tip:
        raise UsageError("branch not on origin")
    if not SHA_RE.fullmatch(str(tip)):
        raise UsageError("origin returned a malformed branch tip")
    try:
        doc = gh.json(["api", f"repos/{slug}/compare/{sha}...{tip}"])
    except InternalError as e:
        raise UsageError(f"sha is not on origin/{branch} (compare failed: {clean(e, 120)})")
    if not isinstance(doc, dict) or doc.get("status") not in ("identical", "ahead"):
        raise UsageError(f"sha is not on origin/{branch}")
    if read_tip() != tip:
        raise UsageError("branch moved during check; re-run")


# ---------------------------------------------------------------- validate

def unpack_bundle(directory) -> Path:
    """bundle.tar -> <dir>/bundle/. Regular files with known names only, bounded, written by us."""
    directory = Path(directory)
    tar_path = directory / "bundle.tar"
    out = directory / "bundle"
    if out.exists():
        raise IsolationError("bundle directory already exists")
    try:
        tf = tarfile.open(tar_path, "r:")
    except (tarfile.TarError, OSError) as e:
        raise IsolationError(f"bundle.tar unreadable: {clean(e)}")
    pending, total, session_total, seen = [], 0, 0, set()
    with tf:
        for m in tf:
            name = m.name
            if not m.isreg():
                raise IsolationError(f"bundle member {clean(name, 80)!r} is not a regular file")
            if name in seen:
                raise IsolationError(f"duplicate bundle member {clean(name, 80)!r}")
            seen.add(name)
            if name in BUNDLE_CAPS:
                cap = BUNDLE_CAPS[name]
            elif SESSION_RE.fullmatch(name):
                cap = SESSION_TOTAL_CAP
                session_total += m.size
            else:
                raise IsolationError(f"unexpected bundle member {clean(name, 80)!r}")
            if m.size > cap or session_total > SESSION_TOTAL_CAP:
                raise IsolationError(f"bundle member {clean(name, 80)!r} is over its size cap")
            total += m.size
            if total > BUNDLE_TOTAL_CAP:
                raise IsolationError("bundle is over the total size cap")
            pending.append((name, m))
        if "lane.patch" not in seen:
            raise IsolationError("bundle has no lane.patch")
        (out / "session").mkdir(parents=True)
        try:
            for name, m in pending:
                src = tf.extractfile(m)
                data = src.read(m.size + 1) if src else b""
                if len(data) != m.size:
                    raise IsolationError(f"bundle member {clean(name, 80)!r} size mismatch")
                (out / name).write_bytes(data)
        except BaseException:
            shutil.rmtree(out, ignore_errors=True)
            raise
    return out


def _read_json(path: Path, what: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise IsolationError(f"{what} is missing or not JSON")


def _check_command(c) -> None:
    if (not isinstance(c, dict) or not isinstance(c.get("cmd"), str)
            or isinstance(c.get("exit"), bool) or not isinstance(c.get("exit"), int)
            or not isinstance(c.get("tail", ""), str) or not isinstance(c.get("phase", ""), str)
            or not isinstance(c.get("inherited", False), bool)):
        raise IsolationError("verify.json command entry has the wrong shape")


def _check_verify(v) -> None:
    """The canonical verify.json is written by the docker module on the runner host (A13), never by a container:
    {status, commands: [{cmd, exit, tail, phase, inherited?}], notes?: [str], base?: {status, commands}}.
    `inherited: true` marks a verify command that also fails on the base (A14). Plain `ok` allows no failing command;
    status `inherited` is the passing-with-known-failures verdict: at least one failure, every failure an inherited
    verify command (the lane takes it as passing but flags it)."""
    if not isinstance(v, dict) or v.get("status") not in VERIFY_STATUSES or not isinstance(v.get("commands"), list):
        raise IsolationError("verify.json has the wrong shape")
    for c in v["commands"]:
        _check_command(c)
    notes = v.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise IsolationError("verify.json notes must be a list of strings")
    base = v.get("base")
    if base is not None:
        if not isinstance(base, dict) or not isinstance(base.get("commands"), list):
            raise IsolationError("verify.json base has the wrong shape")
        for c in base["commands"]:
            _check_command(c)
    if v["status"] in ("ok", "inherited"):
        word = v["status"]
        if not any(c.get("phase") == "verify" for c in v["commands"]):
            raise IsolationError(f"verify.json says {word} but ran no verify command")      # zero commands is never ok (A3)
        failed = [c for c in v["commands"] if c["exit"] != 0]
        for c in failed:
            if not (word == "inherited" and c.get("phase") == "verify" and c.get("inherited") is True):
                raise IsolationError(f"verify.json says {word} but a command failed")
        if word == "inherited" and not failed:
            raise IsolationError("verify.json says inherited but no command failed")


def validate(directory, nonce, start_sha) -> dict:
    """Any failure raises IsolationError (exit 4). Returns the facts the host may trust."""
    directory = Path(directory)
    if not NONCE_RE.fullmatch(str(nonce)) or not SHA_RE.fullmatch(str(start_sha)):
        raise UsageError("nonce or start_sha has the wrong format")
    facts = _read_json(directory / "facts.json", "facts.json")
    if not isinstance(facts, dict):
        raise IsolationError("facts.json has the wrong shape")
    if facts.get("nonce") != nonce:
        raise IsolationError("facts.json nonce does not match the dispatched nonce")
    if facts.get("start_sha") != start_sha:
        raise IsolationError("facts.json start_sha does not match the dispatched start_sha")
    claimed = facts.get("patch_sha256")
    if not isinstance(claimed, str) or not re.fullmatch(r"[0-9a-f]{64}", claimed):
        raise IsolationError("facts.json patch_sha256 is missing or malformed")
    if "exit_code" in facts and facts["exit_code"] is not None and (
            isinstance(facts["exit_code"], bool) or not isinstance(facts["exit_code"], int)):
        raise IsolationError("facts.json exit_code is not an integer")
    if "timed_out" in facts and not isinstance(facts["timed_out"], bool):
        raise IsolationError("facts.json timed_out is not a boolean")
    harness = facts.get("harness_files", [])
    if not isinstance(harness, list) or not all(isinstance(h, str) for h in harness):
        raise IsolationError("facts.json harness_files is not a list of strings")
    verify = _read_json(directory / "verify.json", "verify.json")
    _check_verify(verify)
    bundle = unpack_bundle(directory)
    actual = hashlib.sha256((bundle / "lane.patch").read_bytes()).hexdigest()
    if actual != claimed:
        raise IsolationError("lane.patch sha256 does not match facts.json")
    return {"nonce": nonce, "start_sha": start_sha, "patch_path": str(bundle / "lane.patch"),
            "patch_sha256": actual, "exit_code": facts.get("exit_code"), "timed_out": facts.get("timed_out", False),
            "verify": verify, "verify_ok": verify["status"] in ("ok", "inherited"), "harness_files": harness,
            "bundle_dir": str(bundle)}


# ---------------------------------------------------------------- CLI

def _print(obj) -> None:
    print(json.dumps(obj, sort_keys=True))


def cmd_dispatch(args, gh):
    cfg = load_remote_cfg(args.config)
    if not cfg["runner_repo"]:
        raise UsageError("pilot.sandbox.runner_repo is not set")
    try:
        brief = Path(args.brief).read_bytes()
    except OSError as e:
        raise UsageError(f"cannot read brief: {clean(e)}")
    nonce = args.nonce or secrets.token_hex(16)
    inputs = build_inputs(cfg, repo=args.repo, start_sha=args.start_sha, card=args.card, lane=args.lane,
                          attempt=args.attempt, tool=args.tool, model=args.model, nonce=nonce,
                          timeout_min=args.timeout_min, brief_gz_b64=encode_brief(brief),
                          config_b64=encode_config(runner_payload(cfg, args.tool)), base_sha=args.base_sha or "")
    check_gh_version(gh)
    run_id = dispatch(cfg["runner_repo"], cfg["workflow"], inputs, gh)
    _print({"run_id": run_id, "nonce": nonce})
    return EXIT_OK


def cmd_wait(args, gh):
    cfg = load_remote_cfg(args.config)
    deadline = time.time() + (float(args.timeout_min) + float(args.grace_min)) * 60
    res = wait(cfg["runner_repo"], args.run_id, deadline, gh)
    _print(res)
    return EXIT_OK if res["state"] == "completed" else EXIT_TIMEOUT


def cmd_download(args, gh):
    cfg = load_remote_cfg(args.config)
    dest = download(cfg["runner_repo"], args.run_id, args.nonce, gh, args.out_dir)
    _print({"dir": str(dest)})
    return EXIT_OK


def cmd_validate(args, gh):
    res = validate(args.dir, args.nonce, args.start_sha)
    _print({k: (clean(v) if isinstance(v, str) else v) for k, v in res.items()})
    return EXIT_OK


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dispatch")
    d.add_argument("--config", required=True)
    d.add_argument("--repo", required=True)
    d.add_argument("--start-sha", required=True)
    d.add_argument("--base-sha", help="lane=gate only: the base commit the runner merges into start-sha")
    d.add_argument("--card", required=True)
    d.add_argument("--lane", required=True)
    d.add_argument("--attempt", required=True)
    d.add_argument("--tool", required=True)
    d.add_argument("--model", required=True)
    d.add_argument("--timeout-min", required=True)
    d.add_argument("--brief", required=True, help="file holding the brief text")
    d.add_argument("--nonce", help="32 hex chars; random when omitted")
    d.set_defaults(fn=cmd_dispatch)
    w = sub.add_parser("wait")
    w.add_argument("--config", required=True)
    w.add_argument("--run-id", required=True, type=int)
    w.add_argument("--timeout-min", required=True)
    w.add_argument("--grace-min", default="15")
    w.set_defaults(fn=cmd_wait)
    dl = sub.add_parser("download")
    dl.add_argument("--config", required=True)
    dl.add_argument("--run-id", required=True, type=int)
    dl.add_argument("--nonce", required=True)
    dl.add_argument("--out-dir")
    dl.set_defaults(fn=cmd_download)
    v = sub.add_parser("validate")
    v.add_argument("--dir", required=True)
    v.add_argument("--nonce", required=True)
    v.add_argument("--start-sha", required=True)
    v.set_defaults(fn=cmd_validate)
    return p


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code in (0, None) else EXIT_USAGE
    try:
        return args.fn(args, Gh())
    except LaneError as e:
        print(f"{type(e).__name__}: {clean(e)}", file=sys.stderr)
        return e.code
    except Exception as e:  # noqa: BLE001 - a bug here must not look like a verdict
        print(f"internal error: {clean(e)}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
