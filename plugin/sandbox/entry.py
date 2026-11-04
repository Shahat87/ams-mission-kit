#!/usr/bin/env python3
"""mc-lane in-container entrypoint (spec 2026-10-21 sandbox-lanes addendum, section 4 and 14). Stdlib only.

Subcommands:
  run       the tool container: runs one external CLI, streams output, never writes result.json (A1)
  extract   no-network container: builds lane.patch from the tool tree against a pristine clone (A1, A12)
  prepare   applies lane.patch to a clean clone and exits; the HOST runs verify_setup / verify_commands (A13)
  stage     helper container: no-remote clone of the read-only bare seed at one commit into a volume
  put       helper container: unpacks a tar read from stdin (flat regular files) into a volume, how the host
            hands the brief and the verify inputs to a volume without a host bind mount
  --self-test

Everything the tool writes (/out, /work) is untrusted data. Every helper takes its paths as parameters so the
unit tests can run it on a plain temp dir without Docker.

Run-container env contract (the host puts these in the env-file): MCL_TOOL, MCL_MODEL, optional MCL_IDLE_S
(default 600), MCL_HARD_S (default 3600), MCL_ENV_DENY (comma list of extra names to unset). MCL_* never reach
the tool. `run --tool X --model Y` also works and overrides the env.
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

# exit codes
EX_USAGE = 64
EX_NOPERM = 77      # credential file rejected (A11)
EX_CONFIG = 78      # refused to run as uid 0
EX_TIMEOUT = 124
EX_NOTFOUND = 127
EX_PATCH_TOO_BIG = 3   # extract: lane.patch is over PATCH_MAX

ENV_DENY = ("AMS_OPERATOR_EXPECTED", "AMS_OPERATOR_DATA_ROOT", "SAP_LIVE_TESTS")
ENV_DENY_RE = re.compile(r"^(ACTIONS_|RUNNER_|GITHUB_|GH_)|^LANE_REPO_TOKEN$", re.I)
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
REPORT_RE = re.compile(r"<mc-lane-report>(.*?)</mc-lane-report>", re.S)

AUTH_MAX = 64 * 1024
# Bundle caps. They MUST equal the caps super-board-lane-remote.py enforces on bundle.tar (BUNDLE_CAPS,
# SESSION_TOTAL_CAP, BUNDLE_TOTAL_CAP); tests/test_lane_remote.py ties the two together.
PATCH_MAX = 20_000_000
RAW_LOG_MAX = 5_000_000
REPORT_MAX = 64_000
SESSION_MAX = 20_000_000
BUNDLE_MAX = 45_000_000
TAIL_KEEP_CHARS = 2 * 1024 * 1024


def log(msg):
    sys.stderr.write("mc-lane: %s\n" % msg)
    sys.stderr.flush()


# ---------------------------------------------------------------- pure helpers

def strip_ctrl(text):
    """Drop control characters except newline and tab (A10)."""
    return "".join(ch for ch in text if ch in "\n\t" or (ord(ch) >= 32 and ord(ch) != 127))


def parse_report(text, max_bytes=REPORT_MAX):
    """Last <mc-lane-report>...</mc-lane-report>, code fences stripped, parsed as a JSON object.
    Returns the dict, or None when missing, invalid, not an object or over max_bytes."""
    matches = REPORT_RE.findall(text or "")
    if not matches:
        return None
    body = matches[-1].strip()
    body = re.sub(r"^```[A-Za-z0-9_-]*[ \t]*\r?\n?", "", body)
    body = re.sub(r"\r?\n?```[ \t]*$", "", body).strip()
    if len(body.encode("utf-8", "replace")) > max_bytes:
        return None
    try:
        obj = json.loads(body)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def scrub_env(env, extra_deny=()):
    """Copy of env without the deny names, ACTIONS_*/RUNNER_*/GITHUB_*/GH_*, LANE_REPO_TOKEN and MCL_* control vars."""
    deny = {n.upper() for n in ENV_DENY} | {n.upper() for n in extra_deny if n}
    out = {}
    for k, v in env.items():
        if k.upper() in deny or ENV_DENY_RE.search(k) or k.upper().startswith("MCL_"):
            continue
        out[k] = v
    return out


def auth_file_ok(path, expected_uid=1000, max_bytes=AUTH_MAX):
    """A11: regular file (a symlink is rejected), at most 64 KB, owned by expected_uid (None skips the owner check)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
        return False
    if expected_uid is not None and st.st_uid != expected_uid:
        return False
    return True


def open_regular(path):
    """Open a regular file for reading without following a final symlink; None if it is anything else."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
    except OSError:
        os.close(fd)
        return None
    return os.fdopen(fd, "rb")


def _rm_error(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        func(path)
    except OSError:
        pass


def empty_dir_except(root, keep=".git"):
    """A12: remove everything under root except the top-level `keep` entry, so deletions show up in the patch."""
    for e in os.scandir(root):
        if e.name == keep:
            continue
        if e.is_dir(follow_symlinks=False):
            shutil.rmtree(e.path, onerror=_rm_error)
        else:
            try:
                os.unlink(e.path)
            except OSError:
                _rm_error(os.unlink, e.path, None)


def copy_tree_nolinks(src, dst, skip_names=(".git",)):
    """Copy src into dst without following symlinks (a link is recreated as a link). Names in skip_names are skipped
    at every depth. Sockets, fifos and devices are skipped. Never reads through a link."""
    os.makedirs(dst, exist_ok=True)
    for e in os.scandir(src):
        if e.name in skip_names:
            continue
        target = os.path.join(dst, e.name)
        st = os.lstat(e.path)
        if os.path.lexists(target):
            if os.path.isdir(target) and not os.path.islink(target):
                if not stat.S_ISDIR(st.st_mode):
                    shutil.rmtree(target, onerror=_rm_error)
            else:
                os.unlink(target)
        if stat.S_ISLNK(st.st_mode):
            os.symlink(os.readlink(e.path), target)
        elif stat.S_ISDIR(st.st_mode):
            copy_tree_nolinks(e.path, target, skip_names)
        elif stat.S_ISREG(st.st_mode):
            shutil.copyfile(e.path, target, follow_symlinks=False)
            try:
                shutil.copymode(e.path, target, follow_symlinks=False)
            except (OSError, NotImplementedError):
                pass
        # else: special file, skipped


def build_tar(src_dir, tar_path):
    """One tar holding regular files only. Symlinks, directories and specials are skipped and never dereferenced.
    Returns the member names."""
    names = []
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tf:
        for dirpath, dirnames, filenames in os.walk(src_dir, followlinks=False):
            dirnames.sort()
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                fh = open_regular(full)
                if fh is None:
                    continue
                with fh:
                    ti = tarfile.TarInfo(os.path.relpath(full, src_dir).replace(os.sep, "/"))
                    ti.size = os.fstat(fh.fileno()).st_size
                    ti.mode = 0o644
                    ti.mtime = 0
                    tf.addfile(ti, fh)
                names.append(ti.name)
    return names


def format_argv(template, model, brief_file, work):
    return [a.replace("{model}", model).replace("{brief_file}", brief_file).replace("{work}", work) for a in template]


def git_env(home):
    keep = ("PATH", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"HOME": home, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0"})
    return env


GIT_C = ["-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false",
         "-c", "safe.directory=*", "-c", "protocol.file.allow=never"]


def run_git(args, cwd, env, git="git"):
    return subprocess.run([git] + GIT_C + list(args), cwd=cwd, env=env, capture_output=True)


# ---------------------------------------------------------------- run

def install_profile(profiles_root, name, home, dest_rel):
    src = os.path.join(profiles_root, name)
    dest = os.path.join(home, dest_rel)
    if os.path.lexists(dest):
        shutil.rmtree(dest, onerror=_rm_error)
    if os.path.isdir(src):
        copy_tree_nolinks(src, dest, skip_names=())
    else:
        os.makedirs(dest, exist_ok=True)
    return dest


def copy_auth(cred_dir, auth_name, dest, uid=1000):
    """'absent' when the credential dir is not mounted (env auth), 'bad' when auth.json fails A11, else 'copied'.
    The cred volume is mounted read-only (a tool must not be able to swap the account for later runs), so the file is
    COPIED into the tool's tmpfs HOME (mode 0600) where the tool may refresh it for this run only. Refreshed tokens
    never travel back: when the token expires, log in again on the machine that owns the cred volume."""
    if not os.path.isdir(cred_dir):
        return "absent"
    src = os.path.join(cred_dir, auth_name)
    if not auth_file_ok(src, uid):
        return "bad"
    fh = open_regular(src)
    if fh is None:
        return "bad"
    with fh:
        data = fh.read(AUTH_MAX + 1)
    if len(data) > AUTH_MAX:
        return "bad"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.lexists(dest):
        os.unlink(dest)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    return "copied"


def _kill_group(proc):
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def stream_process(argv, cwd, env, brief, raw_log, idle_s, hard_s, log_max=50 * 1024 * 1024):
    """Run argv with brief on stdin (None = no stdin). Stream combined output line by line to raw_log with control
    characters stripped. Returns (exit_code, timed_out, tail_text). Timeout kills the whole process group."""
    kw = {"start_new_session": True} if hasattr(os, "killpg") else {}
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if brief is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    state = {"last": time.monotonic(), "written": 0}
    tail = collections.deque()
    tail_size = [0]
    lf = open(raw_log, "ab")

    def feed():
        try:
            proc.stdin.write(brief)
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    def read():
        for raw in iter(lambda: proc.stdout.readline(65536), b""):
            state["last"] = time.monotonic()
            text = strip_ctrl(raw.decode("utf-8", "replace"))
            if not text:
                continue
            if not text.endswith("\n"):
                text += "\n"
            data = text.encode("utf-8")
            if state["written"] + len(data) <= log_max:
                lf.write(data)
                lf.flush()
                state["written"] += len(data)
            tail.append(text)
            tail_size[0] += len(text)
            while tail_size[0] > TAIL_KEEP_CHARS and tail:
                tail_size[0] -= len(tail.popleft())

    threads = [threading.Thread(target=read, daemon=True)]
    if brief is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    start = time.monotonic()
    timed_out = False
    while proc.poll() is None:
        now = time.monotonic()
        if now - start > hard_s or now - state["last"] > idle_s:
            timed_out = True
            _kill_group(proc)
            proc.wait()
            break
        time.sleep(0.2)
    _kill_group(proc)           # stragglers that kept the pipe open
    for t in threads:
        t.join(5)
    lf.close()
    try:
        proc.stdout.close()
    except OSError:
        pass
    rc = proc.returncode
    if timed_out:
        rc = EX_TIMEOUT
    elif rc is not None and rc < 0:
        rc = 128 - rc
    return rc, timed_out, "".join(tail)


def capture_sessions(home, globs, out_dir, total_cap=SESSION_MAX):
    """Copy session *.jsonl files (regular, no symlink follow) into out_dir with flat unique names, up to total_cap."""
    os.makedirs(out_dir, exist_ok=True)
    total = 0
    n = 0
    seen = set()
    for pat in globs:
        for path in sorted(glob.glob(os.path.join(home, pat), recursive=True)):
            if path in seen or not path.endswith(".jsonl"):
                continue
            seen.add(path)
            fh = open_regular(path)
            if fh is None:
                continue
            with fh:
                size = os.fstat(fh.fileno()).st_size
                if total + size > total_cap:
                    continue
                n += 1
                dest = os.path.join(out_dir, "%03d-%s" % (n, os.path.basename(path)))
                with open(dest, "wb") as df:
                    shutil.copyfileobj(fh, df)
                total += size
    return n


def run_lane(cfg, env=None, euid=None):
    env = dict(os.environ if env is None else env)
    if euid is None:
        euid = os.geteuid() if hasattr(os, "geteuid") else 1000
    if euid == 0:
        log("refusing to run as uid 0")
        return EX_CONFIG
    tool_id = env.get("MCL_TOOL", "")
    model = env.get("MCL_MODEL", "")
    if not MODEL_RE.match(model):
        log("bad or missing MCL_MODEL")
        return EX_USAGE
    try:
        with open(os.path.join(cfg["opt"], "tools.json"), encoding="utf-8") as tj:
            spec = json.load(tj)["tools"][tool_id]
    except (OSError, ValueError, KeyError):
        log("unknown tool %r or unreadable tools.json" % tool_id)
        return EX_USAGE
    try:
        with open(os.path.join(cfg["out"], "brief"), "rb") as bf:
            brief = bf.read()
    except OSError:
        log("no /out/brief")
        return EX_USAGE
    idle_s = float(env.get("MCL_IDLE_S", "600"))
    hard_s = float(env.get("MCL_HARD_S", "3600"))
    home = cfg["home"]
    child = scrub_env(env, [x for x in env.get("MCL_ENV_DENY", "").split(",") if x])
    child["HOME"] = home
    for k, v in spec.get("env", {}).items():
        child[k] = v.replace("{home}", home)
    prof = spec.get("profile")
    if prof:
        install_profile(os.path.join(cfg["opt"], "profiles"), prof["src"], home, prof["dest"])
    af = spec.get("auth_file")
    if af:
        uid = cfg.get("uid", 1000)
        st = copy_auth(os.path.join(cfg["cred"], af["cred_dir"]), af["name"], os.path.join(home, af["dest"]), uid)
        if st == "bad":
            log("credential file rejected (must be a regular file <= 64KB owned by uid %s)" % uid)
            return EX_NOPERM
    brief_file = os.path.join(cfg["tmp"], "mc-brief.txt")
    with open(brief_file, "wb") as bf:
        bf.write(brief)
    argv = format_argv(spec["argv"], model, brief_file, cfg["work"])
    raw_log = os.path.join(cfg["out"], "raw.log")
    try:
        rc, _timed_out, tail = stream_process(argv, cfg["work"], child, brief if spec.get("stdin_brief") else None,
                                              raw_log, idle_s, hard_s)
    except FileNotFoundError:
        with open(raw_log, "ab") as lf:
            lf.write(b"mc-lane: tool executable not found\n")
        return EX_NOTFOUND
    rep = parse_report(tail)
    if rep is not None:
        with open(os.path.join(cfg["out"], "report.json"), "w", encoding="utf-8") as rf:
            json.dump(rep, rf)
    capture_sessions(home, spec.get("session_globs", []), os.path.join(cfg["out"], "session"))
    return rc          # NO result.json here (A1): the host builds it


# ---------------------------------------------------------------- extract

def _read_capped(path, cap, tail=False):
    """Bytes of a regular file; None if not regular or (tail=False) larger than cap. tail=True keeps the last cap bytes."""
    fh = open_regular(path)
    if fh is None:
        return None
    with fh:
        size = os.fstat(fh.fileno()).st_size
        if tail and size > cap:
            fh.seek(size - cap)
            return fh.read(cap)
        data = fh.read(cap + 1)
    return None if len(data) > cap else data


def do_extract(tool, pristine, outsrc, x, git="git"):
    env = git_env(x)
    if not os.path.isdir(os.path.join(pristine, ".git")):
        log("pristine has no .git")
        return 2
    empty_dir_except(pristine, ".git")
    copy_tree_nolinks(tool, pristine, skip_names=(".git",))
    r = run_git(["rev-parse", "HEAD"], pristine, env, git)
    if r.returncode != 0:
        log("rev-parse failed")
        return 2
    start = r.stdout.decode().strip()
    if run_git(["add", "-A"], pristine, env, git).returncode != 0:
        log("git add failed")
        return 2
    d = run_git(["diff", "--cached", "--binary", "--no-renames", "--no-ext-diff", "--no-textconv", start], pristine, env, git)
    if d.returncode != 0:
        log("git diff failed")
        return 2
    patch = d.stdout
    if len(patch) > PATCH_MAX:
        log("lane.patch is over the %d byte cap" % PATCH_MAX)
        return EX_PATCH_TOO_BIG
    stage = os.path.join(x, ".stage")
    if os.path.lexists(stage):
        shutil.rmtree(stage, onerror=_rm_error)
    os.makedirs(os.path.join(stage, "session"))
    with open(os.path.join(stage, "lane.patch"), "wb") as f:
        f.write(patch)
    facts = {"start_sha": start, "patch_sha256": hashlib.sha256(patch).hexdigest(), "patch_bytes": len(patch)}
    with open(os.path.join(stage, "facts.json"), "w") as f:
        json.dump(facts, f)
    data = _read_capped(os.path.join(outsrc, "raw.log"), RAW_LOG_MAX, tail=True)
    if data is not None:
        with open(os.path.join(stage, "raw.log"), "wb") as f:
            f.write(data)
    data = _read_capped(os.path.join(outsrc, "report.json"), REPORT_MAX)
    if data is not None:
        with open(os.path.join(stage, "report.json"), "wb") as f:
            f.write(data)
    total = 0
    n = 0
    budget = min(SESSION_MAX, BUNDLE_MAX - len(patch) - RAW_LOG_MAX - REPORT_MAX - 1024)
    sdir = os.path.join(outsrc, "session")
    if os.path.isdir(sdir) and not os.path.islink(sdir):
        for e in sorted(os.scandir(sdir), key=lambda e: e.name):
            if not e.name.endswith(".jsonl"):
                continue
            data = _read_capped(e.path, budget - total)
            if data is None:
                continue
            total += len(data)
            n += 1
            # tool-chosen names never travel: flat sequence names the Surface can match with a strict regex
            with open(os.path.join(stage, "session", "%03d.jsonl" % n), "wb") as f:
                f.write(data)
    build_tar(stage, os.path.join(x, "bundle.tar"))
    shutil.rmtree(stage, onerror=_rm_error)
    return 0


# ---------------------------------------------------------------- prepare

def do_prepare(repo, patch, x, git="git"):
    """Apply lane.patch to the clean clone at repo (git apply --index) and stop. It runs NO verify command: the
    host runs those itself with `docker exec` and judges the exit codes (A13), because tool-written tests run as the
    same uid in this container and could forge any file written here. /x/prepare.json is a log; the host decides from
    this process's exit code (0 applied, 1 not) and output, which it also gets on stdout."""
    ap = run_git(["apply", "--index", patch], repo, git_env(x), git)
    ok = ap.returncode == 0
    res = {"applied": ok, "error": "" if ok else strip_ctrl(ap.stderr.decode("utf-8", "replace"))[-4000:]}
    try:
        os.makedirs(x, exist_ok=True)
        with open(os.path.join(x, "prepare.json"), "w", encoding="utf-8") as f:
            json.dump(res, f)
    except OSError as e:
        log("prepare.json not written: %s" % e)
    sys.stdout.write(json.dumps(res) + "\n")
    sys.stdout.flush()
    return 0 if ok else 1


# ---------------------------------------------------------------- stage and put (helper containers)

def do_stage(seed, dest, sha, ref="refs/mc/start", git="git", merge_sha=None, merge_ref="refs/mc/base"):
    """No-remote clone of the bare seed at sha into dest. The seed is a read-only host mount whose config the
    runner already checked for credentials. Hooks and filters are off; no remote is left behind.
    merge_sha (the merge gate only): after the checkout, merge that commit (the base) into it with a no-ff merge commit,
    so the gate verifies the merged tree. A conflict leaves nothing usable and returns 3."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        log("bad sha")
        return EX_USAGE
    if merge_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", merge_sha):
        log("bad merge sha")
        return EX_USAGE
    cfg_dir = tempfile.mkdtemp(prefix="mcl-stage-")
    try:
        return _stage(seed, dest, sha, ref, git, cfg_dir, merge_sha, merge_ref)
    finally:
        shutil.rmtree(cfg_dir, ignore_errors=True)


def _stage(seed, dest, sha, ref, git, cfg_dir, merge_sha=None, merge_ref="refs/mc/base"):
    env = git_env(dest)
    # The seed is a read-only bind mount owned by another uid (root on Docker Desktop), and git refuses "dubious
    # ownership" for the upload-pack child of a local fetch. `-c safe.directory` and GIT_CONFIG_COUNT do not reach that
    # child (git clears them for local transport); a global config FILE does. It lives on the container tmpfs, is read
    # only by this stage process, and names no other setting.
    cfg_file = os.path.join(cfg_dir, "gitconfig")
    with open(cfg_file, "w") as cf:
        cf.write("[safe]" + chr(10) + chr(9) + "directory = *" + chr(10))
    env["GIT_CONFIG_GLOBAL"] = cfg_file
    # GIT_C minus protocol.file.allow=never: the seed is a local path
    gc = ["-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", "-c", "core.autocrlf=false",
          "-c", "safe.directory=*"]
    os.makedirs(dest, exist_ok=True)

    def g(args, cwd):
        return subprocess.run([git] + gc + list(args), cwd=cwd, env=env, capture_output=True)

    if os.listdir(dest):
        log("stage destination is not empty")
        return 2
    if g(["init", "-q"], dest).returncode != 0:
        log("git init failed")
        return 2
    f = g(["fetch", "-q", "--no-tags", "--no-write-fetch-head", seed, ref], dest)
    if f.returncode != 0:
        f = g(["fetch", "-q", "--no-tags", seed, sha], dest)
    if f.returncode != 0:
        log("git fetch from the seed failed")
        return 2
    chk = g(["rev-parse", "--verify", sha + "^{commit}"], dest)
    if chk.returncode != 0:
        log("seed does not hold the start commit")
        return 2
    if g(["checkout", "-q", "--detach", sha], dest).returncode != 0:
        log("git checkout failed")
        return 2
    if merge_sha:
        f = g(["fetch", "-q", "--no-tags", "--no-write-fetch-head", seed, merge_ref], dest)
        if f.returncode != 0:
            f = g(["fetch", "-q", "--no-tags", seed, merge_sha], dest)
        if f.returncode != 0 or g(["rev-parse", "--verify", merge_sha + "^{commit}"], dest).returncode != 0:
            log("seed does not hold the base commit")
            return 2
        # identity for the merge commit comes from -c (image-only config, nothing from the repo); hooks stay off via gc
        m = subprocess.run([git, "-c", "user.name=mc-lane", "-c", "user.email=mc-lane@invalid"] + gc +
                           ["merge", "--no-edit", "--no-ff", merge_sha], cwd=dest, env=env, capture_output=True)
        if m.returncode != 0:
            log("merge conflict with base")
            return 3
    return 0


def do_put(dest, stream):
    """Unpack a tar from stream into dest. Flat regular files only, names [A-Za-z0-9._-], 20 MB each."""
    os.makedirs(dest, exist_ok=True)
    total = 0
    try:
        tf = tarfile.open(fileobj=stream, mode="r|")
        for m in tf:
            if not m.isreg() or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", m.name) or m.name in (".", ".."):
                log("put: refused member %r" % m.name)
                return EX_USAGE
            total += m.size
            if m.size > PATCH_MAX or total > BUNDLE_MAX:
                log("put: over the size cap")
                return EX_USAGE
            src = tf.extractfile(m)
            with open(os.path.join(dest, m.name), "wb") as df:
                shutil.copyfileobj(src, df)
    except (tarfile.TarError, OSError) as e:
        log("put: %s" % e)
        return EX_USAGE
    return 0


# ---------------------------------------------------------------- self test + main

def self_test():
    assert strip_ctrl("a\x1b[31mb\x00c\td\n\x7f") == "a[31mbc\td\n"
    assert parse_report("x <mc-lane-report>{\"a\":1}</mc-lane-report> y <mc-lane-report>```json\n{\"b\":2}\n```</mc-lane-report>") == {"b": 2}
    assert parse_report("<mc-lane-report>nope</mc-lane-report>") is None
    assert parse_report("no tags") is None
    e = scrub_env({"PATH": "/bin", "GH_TOKEN": "x", "github_token": "x", "AMS_OPERATOR_EXPECTED": "1", "RUNNER_TEMP": "x",
                   "ACTIONS_ID_TOKEN": "x", "LANE_REPO_TOKEN": "x", "MCL_TOOL": "x", "OPENAI_API_KEY": "k"})
    assert e == {"PATH": "/bin", "OPENAI_API_KEY": "k"}, e
    assert format_argv(["a", "{model}", "{brief_file}"], "m", "/f", "/w") == ["a", "m", "/f"]
    print("ok")
    return 0


def default_cfg():
    return {"work": "/work", "out": "/out", "opt": "/opt/mc-lane", "cred": "/cred", "tmp": "/tmp",
            "home": os.environ.get("HOME", "/home/agent"), "uid": 1000}


def build_parser():
    ap = argparse.ArgumentParser(prog="entry.py")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--tool")
    r.add_argument("--model")
    e = sub.add_parser("extract")
    e.add_argument("--tool-dir", default="/tool")
    e.add_argument("--pristine", default="/pristine")
    e.add_argument("--outsrc", default="/outsrc")
    e.add_argument("--x", default="/x")
    v = sub.add_parser("prepare")
    v.add_argument("--repo", default="/repo")
    v.add_argument("--patch", default="/in/lane.patch")
    v.add_argument("--x", default="/x")
    s = sub.add_parser("stage")
    s.add_argument("--seed", default="/seed")
    s.add_argument("--dest", default="/work")
    s.add_argument("--sha", required=True)
    s.add_argument("--ref", default="refs/mc/start")
    s.add_argument("--merge-sha")
    s.add_argument("--merge-ref", default="refs/mc/base")
    p = sub.add_parser("put")
    p.add_argument("--dest", required=True)
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if a.cmd == "run":
        env = dict(os.environ)
        if a.tool:
            env["MCL_TOOL"] = a.tool
        if a.model:
            env["MCL_MODEL"] = a.model
        return run_lane(default_cfg(), env)
    if a.cmd == "extract":
        return do_extract(a.tool_dir, a.pristine, a.outsrc, a.x)
    if a.cmd == "prepare":
        return do_prepare(a.repo, a.patch, a.x)
    if a.cmd == "stage":
        return do_stage(a.seed, a.dest, a.sha, a.ref, merge_sha=a.merge_sha, merge_ref=a.merge_ref)
    if a.cmd == "put":
        return do_put(a.dest, sys.stdin.buffer)
    ap.print_usage(sys.stderr)
    return EX_USAGE


if __name__ == "__main__":
    sys.exit(main())
