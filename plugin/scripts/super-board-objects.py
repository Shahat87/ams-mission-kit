#!/usr/bin/env python3
"""Objects-only commit builder for lane output (Plan 1c, Task 3; spec section 15.2).

Builds a commit from a parent sha and a patch WITHOUT touching any working tree: a private index in a temp
dir outside every checkout (read-tree, apply --cached, write-tree, commit-tree), then pushes that sha to a branch
and keeps the branch-level taint record. Every git call goes through super-board-git.sh.

Threat model: the patch is tool-authored and hostile. It must never write a file into a checkout, run a hook,
filter, attribute program or external diff driver, or reach a protected branch. Only git objects are written.

CLI (tests and later callers): python3 -I super-board-objects.py build|push|tainted ...

Exit codes: 0 ok | 3 refused because a recorded sha is not yet in the base branch (clear_taint_marker) | 64 usage or a
refused branch/sha/scratch root | 66 missing input (patch file, wrapper, bash, robot gh config) | 69 push failed or was
rejected | 70 internal git failure (including a patch that does not apply).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
WRAPPER = HERE / "super-board-git.sh"
EXIT_OK, EXIT_NOT_MERGED, EXIT_USAGE, EXIT_MISSING, EXIT_PUSH, EXIT_INTERNAL = 0, 3, 64, 66, 69, 70
NL = chr(10)
SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
NONCE_RE = re.compile(r"[0-9a-f]{64}")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")
SLUG_RE = re.compile(r"[A-Za-z0-9._-]+")
CTRL_RE = re.compile("[\x00-\x1f\x7f]")
TRAILER_RE = re.compile(r"\s*mc-lane-tool\s*:", re.I)
ALWAYS_PROTECTED = ("main", "master")
BAD_BASH_DIRS = ("/system32/", "/sysnative/", "/windowsapps/")


class ObjectsError(Exception):
    """A refusal or a git failure. code is one of the exit codes above."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------------------------------------------------------------- the wrapper

def _bash():
    override = os.environ.get("SB_BASH")
    if override and os.environ.get("SB_OBJECTS_TEST") == "1":     # a test-only seam; ignored in production
        return override
    if os.name == "nt":                         # System32 bash.exe is WSL's; the wrapper needs Git for Windows' bash
        git = shutil.which("git")
        if git:
            root = Path(git).resolve().parent
            for cand in (root.parent / "bin" / "bash.exe", root.parent.parent / "bin" / "bash.exe"):
                if cand.is_file():
                    return str(cand)
    found = shutil.which("bash")
    if not found:
        raise ObjectsError(EXIT_MISSING, "bash not found (super-board-git.sh needs it)")
    low = found.replace(chr(92), "/").lower()
    if any(bad in low for bad in BAD_BASH_DIRS):
        raise ObjectsError(EXIT_MISSING, f"refused bash (WSL's launcher, not Git for Windows' bash): {found}")
    return found


def _clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(extra or {})
    return env


def _git(args, *, robot=False, robot_read=False, env=None, check=True):
    """Run git through super-board-git.sh. Returns the CompletedProcess; raises ObjectsError if check and it failed.
    robot = the push shape; robot_read = the credentialed read shapes (ls-remote / fetch of one ref), for a private origin."""
    if not WRAPPER.is_file():
        raise ObjectsError(EXIT_MISSING, f"git wrapper missing: {WRAPPER}")
    flag = ["--robot"] if robot else ["--robot-read"] if robot_read else []
    cmd = [_bash(), WRAPPER.as_posix()] + flag + ["--"] + [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env or _clean_env())
    if check and r.returncode != 0:
        code = r.returncode if r.returncode in (EXIT_USAGE, EXIT_MISSING) else EXIT_INTERNAL
        raise ObjectsError(code, f"git {' '.join(str(a) for a in args[:4])} failed ({r.returncode}): {r.stderr.strip()[:300]}")
    return r


# ---------------------------------------------------------------- helpers

def lane_trailers(tool, nonce_sha):
    """The two trailers every lane commit carries, exactly as verify writes them today (Plan 1c amendment 3)."""
    if not NONCE_RE.fullmatch(str(nonce_sha)):
        raise ObjectsError(EXIT_USAGE, f"nonce sha is not 64 lowercase hex characters: {nonce_sha!r}")
    return f"Mc-Lane-Tool: {CTRL_RE.sub('', str(tool))}" + NL + f"Mc-Lane-Nonce-Sha256: {nonce_sha}"


def git_identity(repo):
    """(name, email) from the repo's own effective git config, else a fixed one; mirrors lane-delegate _git_identity.
    `git config --get` only reads, it never runs a program, so plain git is fine here."""
    out = []
    for key, default in (("user.name", "mc-lane"), ("user.email", "mc-lane@invalid")):
        r = subprocess.run(["git", "-C", str(repo), "config", "--get", key], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=_clean_env())
        value = CTRL_RE.sub("", r.stdout.strip()) if r.returncode == 0 else ""
        out.append(value if value and not re.search("[<>]", value) else default)
    return tuple(out)


def strip_branch(name):
    """Strip refs/remotes/origin/, refs/heads/ and origin/ (repeatedly), keeping the case: the form used for ref lookups."""
    n = str(name).strip()
    changed = True
    while changed:
        changed = False
        for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
            if n.lower().startswith(prefix):
                n = n[len(prefix):]
                changed = True
    return n


def norm_branch(name):
    """strip_branch, lowercased: the form used only to COMPARE names."""
    return strip_branch(name).lower()


def protected_names(base_branch, protected):
    """Every branch a lane push must never touch: main, master, SB_PROTECTED_BRANCHES, the protected list and the base."""
    names = set(ALWAYS_PROTECTED)
    for raw in (os.environ.get("SB_PROTECTED_BRANCHES", "").split(","), list(protected or ()), [base_branch]):
        names.update(norm_branch(x) for x in raw if str(x).strip())
    return names


def check_branch(branch):
    """Refuse (64) anything that is not a plain branch name."""
    if (not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch) or branch.startswith(("-", "/"))
            or ".." in branch or "//" in branch):
        raise ObjectsError(EXIT_USAGE, f"refused branch name: {branch!r}")


def _slug(branch):
    for prefix in ("refs/remotes/origin/", "refs/heads/", "origin/"):
        if branch.startswith(prefix):
            branch = branch[len(prefix):]
            break
    slug = branch.replace("/", "__")
    if not SLUG_RE.fullmatch(slug):
        raise ObjectsError(EXIT_USAGE, f"refused branch name for a taint marker: {branch!r}")
    return slug


def _scratch_root():
    return os.environ.get("SB_SCRATCH_ROOT") or tempfile.gettempdir()


def _inside(path, parent):
    a, b = os.path.normcase(str(path)), os.path.normcase(str(parent)).rstrip("/" + chr(92))
    return a == b or a.startswith(b + os.sep) or a.startswith(b + "/")


def _worktree_paths(repo_p):
    r = _git(["-C", repo_p.as_posix(), "worktree", "list", "--porcelain"])
    return [Path(line[len("worktree "):]).resolve() for line in r.stdout.splitlines() if line.startswith("worktree ")]


def _refuse_scratch_in_checkouts(repo_p, root):
    """The scratch root must not sit inside the repo, its main checkout or any worktree: a write there is a checkout write."""
    for f in [repo_p, _main_checkout(repo_p).resolve()] + _worktree_paths(repo_p):
        if _inside(root, f):
            raise ObjectsError(EXIT_USAGE, f"scratch root {root} is inside a checkout ({f})")


# ---------------------------------------------------------------- build

def build_commit(repo, parent_sha, patch_path, message, trailers, identity):
    """Commit PARENT_SHA + PATCH_PATH as a new commit object and return its sha. Writes only git objects:
    the index lives in a temp dir under the scratch root (never inside a checkout) and is removed in all cases.
    identity is (name, email), passed as GIT_AUTHOR_* / GIT_COMMITTER_* env set here, because the wrapper refuses -c.
    commit-tree never signs unless given -S, so commit.gpgsign does not matter here."""
    repo_p = Path(repo).resolve()
    patch = Path(patch_path).resolve()
    if not patch.is_file():
        raise ObjectsError(EXIT_MISSING, f"patch file not found: {patch}")
    if not SHA_RE.fullmatch(str(parent_sha)):
        raise ObjectsError(EXIT_USAGE, f"parent is not a full sha: {parent_sha!r}")
    name, email = identity
    root = Path(_scratch_root()).resolve()
    _refuse_scratch_in_checkouts(repo_p, root)
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sb-objects-", dir=str(root))).resolve()
    try:
        env = _clean_env({"GIT_INDEX_FILE": (tmp / "index").as_posix(),
                          "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                          "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
        c = ["-C", repo_p.as_posix()]
        _git(c + ["read-tree", parent_sha], env=env)
        _git(c + ["apply", "--cached", "--whitespace=nowarn", "--binary", "-p1", patch.as_posix()], env=env)
        tree = _git(c + ["write-tree"], env=env).stdout.strip()
        if not SHA_RE.fullmatch(tree):
            raise ObjectsError(EXIT_INTERNAL, f"write-tree returned {tree!r}")
        msgfile = tmp / "message"
        msgfile.write_bytes((message.strip() + NL + NL + trailers + NL).encode("utf-8"))
        sha = _git(c + ["commit-tree", tree, "-p", parent_sha, "-F", msgfile.as_posix()], env=env).stdout.strip()
        if not SHA_RE.fullmatch(sha):
            raise ObjectsError(EXIT_INTERNAL, f"commit-tree returned {sha!r}")
        return sha
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- push

def push_commit(repo, sha, branch, base_branch, protected, *, pr=None, nonce_sha=None):
    """Push SHA to origin/BRANCH. Never forces: no plus sign, no --force. Refusals come before any git call.
    The taint marker is written BEFORE the push and stays if the push fails: a failed push may still have reached the
    remote, so the branch errs toward tainted."""
    check_branch(branch)
    if branch.lower() == "head" or branch.startswith("refs/"):
        raise ObjectsError(EXIT_USAGE, f"refused branch name: {branch!r}")
    check_branch(base_branch)
    names = protected_names(base_branch, protected)
    if norm_branch(branch) in names or norm_branch(branch) == "head":
        raise ObjectsError(EXIT_USAGE, f"refused: {branch!r} is the base or a protected branch")
    if not SHA_RE.fullmatch(str(sha)):
        raise ObjectsError(EXIT_USAGE, f"not a full sha: {sha!r}")
    env = _clean_env({"SB_PROTECTED_BRANCHES": ",".join(sorted(names))})
    write_taint_marker(repo, branch, pr, nonce_sha, sha)
    r = _git(["-C", str(Path(repo).resolve().as_posix()), "push", "origin", f"{sha}:refs/heads/{branch}"],
             robot=True, env=env, check=False)
    if r.returncode == 0:
        return
    if r.returncode in (EXIT_USAGE, EXIT_MISSING):
        raise ObjectsError(r.returncode, r.stderr.strip()[:300])
    err = r.stderr.strip()
    # only a stale-tip rejection is a "non-fast-forward"; [remote rejected] (protected branch, hook, ...) is a plain failure
    kind = "rejected (non-fast-forward)" if ("non-fast-forward" in err or "fetch first" in err) else "failed"
    raise ObjectsError(EXIT_PUSH, f"push {kind}: {err[:300]}")


# ---------------------------------------------------------------- taint

def _main_checkout(repo):
    r = _git(["-C", str(Path(repo).resolve().as_posix()), "rev-parse", "--path-format=absolute", "--git-common-dir"])
    common = Path(r.stdout.strip())
    if common.name != ".git":
        raise ObjectsError(EXIT_INTERNAL, f"git common dir is not a .git directory: {common}")
    return common.parent


def _marker_path(repo, branch):
    return _main_checkout(repo) / ".claude" / "super-board" / "inflight" / f"sandbox-taint-{_slug(branch)}.json"


def _live_base_ok(repo, base_branch):
    """True once the local origin/BASE equals the remote's live tip (fetching through the wrapper if it differs).
    Any failure (no network, branch missing, still different) is False: callers fail closed."""
    rp = str(Path(repo).resolve().as_posix())
    base = strip_branch(base_branch)
    r = _git(["-C", rp, "ls-remote", "origin", f"refs/heads/{base}"], robot_read=True, check=False)
    live = r.stdout.split()[0] if r.returncode == 0 and r.stdout.split() else ""
    if not SHA_RE.fullmatch(live):
        return False

    def local():
        q = _git(["-C", rp, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{base}"], check=False)
        return q.stdout.strip() if q.returncode == 0 else ""

    if local() == live:
        return True
    _git(["-C", rp, "fetch", "origin", f"refs/heads/{base}"], robot_read=True, check=False)   # updates refs/remotes/origin/BASE
    return local() == live


def is_tainted(repo, ref, base_branch):
    """True if the branch has a taint marker, or a commit in origin/BASE..REF carries an Mc-Lane-Tool trailer.
    Fails closed (True) if the live base tip cannot be confirmed."""
    if str(ref).startswith("-"):
        raise ObjectsError(EXIT_USAGE, f"refused ref: {ref!r}")
    base = strip_branch(base_branch)
    check_branch(base)
    if _marker_path(repo, ref).exists():
        return True
    if not _live_base_ok(repo, base):
        return True
    r = _git(["-C", str(Path(repo).resolve().as_posix()), "log", "--format=%B", f"refs/remotes/origin/{base}..{ref}"])
    return any(TRAILER_RE.match(line) for line in r.stdout.splitlines())


def write_taint_marker(repo, branch, pr, nonce_sha, sha):
    """Record that SHA was pushed to BRANCH. A second write for the same branch MERGES into the stored record: `shas` and
    `nonce_shas` are appended to (the old single `nonce_sha` key is still read, and still written as the first nonce), and a
    stored non-None `pr` is never replaced by None."""
    check_branch(branch)
    if not SHA_RE.fullmatch(str(sha)):
        raise ObjectsError(EXIT_USAGE, f"not a full sha: {sha!r}")
    path = _marker_path(repo, branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    shas, nonces, old_pr, created = [], [], None, None
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            shas = list(doc.get("shas") or [])
            nonces = list(doc.get("nonce_shas") or [])
            if doc.get("nonce_sha") and doc["nonce_sha"] not in nonces:       # the pre-list format
                nonces.insert(0, doc["nonce_sha"])
            old_pr, created = doc.get("pr"), doc.get("created")
        except (ValueError, OSError, AttributeError, TypeError):
            raise ObjectsError(EXIT_INTERNAL, f"taint marker unreadable; kept as it is: {path}")
    if sha not in shas:
        shas.append(sha)
    if nonce_sha and nonce_sha not in nonces:
        nonces.append(nonce_sha)
    tmp = path.with_name("." + path.name + ".tmp")
    data = {"branch": branch, "pr": pr if pr is not None else old_pr, "nonce_sha": nonces[0] if nonces else None,
            "nonce_shas": nonces, "shas": shas,
            "created": created or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)
    return path


def clear_taint_marker(repo, branch, base_branch):
    """Remove the marker only when EVERY recorded sha is an ancestor of the LIVE origin/BASE. Otherwise code 3 (a sha is not
    merged) or 64 (the live base tip cannot be confirmed) and the marker stays. Returns True if a marker was removed."""
    base = strip_branch(base_branch)
    check_branch(base)
    path = _marker_path(repo, branch)
    if not path.exists():
        return False
    try:
        shas = json.loads(path.read_text(encoding="utf-8")).get("shas")
    except (ValueError, OSError, AttributeError, TypeError):
        raise ObjectsError(EXIT_INTERNAL, "taint marker unreadable; kept")
    if not shas or not all(SHA_RE.fullmatch(str(x)) for x in shas):
        raise ObjectsError(EXIT_NOT_MERGED, "taint marker records no usable sha; kept")
    if not _live_base_ok(repo, base):
        raise ObjectsError(EXIT_USAGE, f"cannot confirm the live tip of origin/{base}; marker kept")
    rp = str(Path(repo).resolve().as_posix())
    for sha in shas:
        r = _git(["-C", rp, "merge-base", "--is-ancestor", sha, f"refs/remotes/origin/{base}"], check=False)
        if r.returncode == 1:
            raise ObjectsError(EXIT_NOT_MERGED, f"{sha} is not in refs/remotes/origin/{base}; marker kept")
        if r.returncode != 0:
            raise ObjectsError(EXIT_INTERNAL, f"merge-base failed ({r.returncode}): {r.stderr.strip()[:300]}")
    path.unlink()
    return True


# ---------------------------------------------------------------- CLI

def _main(argv):
    ap = argparse.ArgumentParser(prog="super-board-objects.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--repo", required=True)
    b.add_argument("--parent", required=True)
    b.add_argument("--patch", required=True)
    b.add_argument("--message", required=True)
    b.add_argument("--tool", required=True)
    b.add_argument("--nonce-sha", required=True)
    p = sub.add_parser("push")
    p.add_argument("--repo", required=True)
    p.add_argument("--sha", required=True)
    p.add_argument("--branch", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--protected", default="", help="comma-separated branch names")
    p.add_argument("--pr", default=None)
    p.add_argument("--nonce-sha", default=None)
    t = sub.add_parser("tainted")
    t.add_argument("--repo", required=True)
    t.add_argument("--ref", required=True)
    t.add_argument("--base", required=True)
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code else EXIT_OK
    try:
        if a.cmd == "build":
            print(build_commit(a.repo, a.parent, a.patch, a.message, lane_trailers(a.tool, a.nonce_sha), git_identity(a.repo)))
        elif a.cmd == "push":
            push_commit(a.repo, a.sha, a.branch, a.base, [x for x in a.protected.split(",") if x],
                        pr=a.pr, nonce_sha=a.nonce_sha)
        else:
            print("tainted" if is_tainted(a.repo, a.ref, a.base) else "clean")
    except ObjectsError as e:
        print(f"super-board-objects: {e.msg}", file=sys.stderr)
        return e.code
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
