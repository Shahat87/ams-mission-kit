#!/usr/bin/env python3
"""Snapshot of the state a lane must never change; compare() lists differences.

Scope (spec 6.4): listed files (board config, pilot-state, installed bin, relay
scripts), the WHOLE local git config of the main repo except `branch.*` keys
(sibling lanes write upstream settings), every `config.worktree`, `info/attributes`,
every file in the effective hooks dir, in `core.hooksPath` and in the default hooks
dir, the real `~/.gitconfig`, the lane worktree HEAD and branch ref, the remote lane
branch and the remote tag list. Other branches are ignored.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else f"<error {r.returncode}>"


def _hash(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "<missing>"


def _config_keys(repo):
    out = {}
    for line in _git(repo, "config", "--local", "--list").splitlines():
        key, _, value = line.partition("=")
        k = key.lower()
        if k.startswith("branch."):
            continue
        out.setdefault(k, []).append(value)
    return {k: "\n".join(v) for k, v in out.items()}


def _dir_hashes(prefix, directory):
    out = {}
    if directory and directory.is_dir():
        for f in sorted(directory.iterdir()):
            if f.is_file():
                out[f"{prefix}/{f.name}"] = _hash(f)
    return out


def snapshot(repo, worktree, branch, files, home=None):
    repo = Path(repo)
    home = Path(home) if home is not None else Path.home()
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    hooks_dir = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks"))
    hooks = _dir_hashes("hooks", hooks_dir)
    hooks.update(_dir_hashes("hooks-default", common / "hooks"))
    hooks_path = _git(repo, "config", "--local", "--get", "core.hooksPath")
    if hooks_path and not hooks_path.startswith("<error"):
        hp = Path(hooks_path)
        hooks.update(_dir_hashes("hookspath", hp if hp.is_absolute() else repo / hp))
    extra = {}
    for cw in [common / "config.worktree", *sorted(common.glob("worktrees/*/config.worktree"))]:
        extra[f"config.worktree:{cw.relative_to(common).as_posix()}"] = _hash(cw)
    extra["info/attributes"] = _hash(common / "info" / "attributes")
    extra["~/.gitconfig"] = _hash(home / ".gitconfig")
    extra["~/.config/git/config"] = _hash(home / ".config" / "git" / "config")
    return {
        "files": {str(f): _hash(Path(f)) for f in files},
        "extra": extra,
        "hooks": hooks,
        "config": _config_keys(repo),
        "HEAD": _git(worktree, "rev-parse", "HEAD"),
        "branch": _git(repo, "rev-parse", f"refs/heads/{branch}"),
        "remote_branch": _git(repo, "ls-remote", "origin", f"refs/heads/{branch}"),
        "remote_tags": _git(repo, "ls-remote", "--tags", "origin"),
        "common_dir": str(common),
        "branch_name": branch,
    }


def compare(before, after):
    diffs = []
    for name in sorted(set(before["files"]) | set(after["files"])):
        if before["files"].get(name) != after["files"].get(name):
            diffs.append(f"file {name}")
    for name in sorted(set(before["extra"]) | set(after["extra"])):
        if before["extra"].get(name) != after["extra"].get(name):
            diffs.append(f"{name}")
    for name in sorted(set(before["hooks"]) | set(after["hooks"])):
        if before["hooks"].get(name) != after["hooks"].get(name):
            diffs.append(f"{name}")
    for key in sorted(set(before["config"]) | set(after["config"])):
        if before["config"].get(key) != after["config"].get(key):
            diffs.append(f"git config {key}")
    if before["HEAD"] != after["HEAD"]:
        diffs.append("HEAD of the lane worktree")
    if before["branch"] != after["branch"]:
        diffs.append("local lane branch ref")
    if before["remote_branch"] != after["remote_branch"]:
        diffs.append(f"remote refs/heads/{before['branch_name']}")
    if before["remote_tags"] != after["remote_tags"]:
        diffs.append("remote tags")
    return diffs
