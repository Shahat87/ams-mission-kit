#!/usr/bin/env python3
"""Classify paths an external tool changed. Pure functions, no I/O.

normalise(): case-fold, `\\` -> `/`, strip trailing dots/spaces per segment;
reject anything Windows could alias (8.3 short names, `:` streams), absolute
paths and `..`. A path that cannot be normalised is treated as protected.
"""
from __future__ import annotations

import fnmatch
import re

SHORT_NAME = re.compile(r"~\d")
SYMLINK, SUBMODULE, EXECUTABLE = "120000", "160000", "100755"

# Plan 1b A3: in docker mode these are rejected whatever pilot.protected_paths says. Only an allow-path entry from a
# trusted approver lifts one (exact normalised path). Directory globs match at any depth, bare names match any path
# segment, case-folded. `package.json`: A3 names "a change to a `scripts` key"; the simpler safe rule is that ANY
# change to ANY package.json needs allow-path (a lifecycle script or a dependency swap is the same risk).
DOCKER_REJECT = [".claude/**", ".github/**", ".husky/**", ".githooks/**", ".vscode/**",
                 ".gitattributes", ".gitmodules", ".lfsconfig", ".npmrc", ".yarnrc*", "pip.conf", "package.json",
                 # tool config that steers later agent runs or the host's shell/version managers (Plan 1b review M5)
                 ".mcp.json", "CLAUDE.md", "AGENTS.md", ".envrc", ".pre-commit-config.yaml", ".nvmrc",
                 ".python-version", ".tool-versions",
                 # hooks and host-side tooling configs (Plan 1b re-review): dependency hooks, git-hook managers, staged-file runners,
                 # container and IDE config that an editor or `devcontainer` on this machine would act on
                 ".pnpmfile.cjs", "lefthook.yml", "lefthook.yaml", ".lefthook.yml", ".lefthook.yaml", ".lintstagedrc*",
                 "lint-staged.config.*", ".devcontainer/**", ".idea/**",
                 # true launchers (spec 15.2 step 1): PATHEXT entries and the other extensions Windows opens or runs on a
                 # double click, refused at any depth. Tool-written bytes must never become such a file.
                 "*.com", "*.exe", "*.bat", "*.cmd", "*.vbs", "*.vbe", "*.jse", "*.wsf", "*.wsh", "*.msc",
                 "*.dll", "*.hta", "*.cpl", "*.pif", "*.reg", "*.url", "*.lnk", "*.scr", "*.msi"]
# Script files that are also ordinary source (.js, .ps1, .psm1) are refused only at the repo root, where a bare-name
# lookup with the repo as cwd (git.js, gh.ps1) could pick them up. In subfolders they are normal code (2026-10-28).
DOCKER_REJECT_ROOT = ["*.js", "*.ps1", "*.psm1"]


def normalise(path):
    if not isinstance(path, str) or not path.strip():
        return None
    p = path.replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ":" in p:
        return None
    parts = []
    for seg in p.split("/"):
        if seg.strip(". ") == "" and seg.count(".") >= 2:   # "..", ".. ", "..." resolve upward on Windows
            return None
        seg = seg.rstrip(". ")
        if seg in ("", "."):
            continue
        if seg == ".." or SHORT_NAME.search(seg):
            return None
        parts.append(seg.lower())
    return "/".join(parts) or None


def _pattern_hit(norm, pattern):
    pat = pattern.replace("\\", "/").lower()
    segs = norm.split("/")
    if pat.endswith("/"):                       # directory at any depth
        name = pat.rstrip("/")
        return any(fnmatch.fnmatchcase(s, name) for s in segs[:-1])
    if "/" in pat:                              # anchored path glob
        return fnmatch.fnmatchcase(norm, pat) or fnmatch.fnmatchcase(norm, pat.lstrip("*/"))
    # bare name or basename glob: any segment (covers nested `.git/`, `conftest.py`)
    return any(fnmatch.fnmatchcase(s, pat) for s in segs)


def protected_hit(path, protected):
    norm = normalise(path)
    if norm is None:
        return "<unsafe path>"
    for pattern in protected:
        if _pattern_hit(norm, pattern):
            return pattern
    return None


def docker_reject_hit(path):
    """The DOCKER_REJECT glob a path matches, or None. An unparseable path is a hit."""
    norm = normalise(path)
    if norm is None:
        return "<unsafe path>"
    for glob in DOCKER_REJECT:
        pattern = glob[:-2] if glob.endswith("/**") else glob      # `.husky/**` -> directory `.husky/`
        if _pattern_hit(norm, pattern):
            return glob
    if "/" not in norm:
        for glob in DOCKER_REJECT_ROOT:
            if fnmatch.fnmatchcase(norm, glob):
                return "/" + glob                                   # shown as a root-anchored glob
    return None


def is_test_file(path, test_globs):
    norm = normalise(path)
    if norm is None:
        return False
    for g in test_globs:
        g = g.replace("\\", "/").lower()
        if fnmatch.fnmatchcase(norm, g) or fnmatch.fnmatchcase(norm, g.removeprefix("**/")):
            return True
    return False


def classify(changes, protected, test_globs, lane, allowed, docker_mode=False):
    """changes: [{status, old, new, old_mode, new_mode}] -> list of problem strings.
    docker_mode adds the hard-coded DOCKER_REJECT list (A3); `allowed` lifts it like any protected path."""
    allowed_norm = {normalise(a) for a in allowed}
    problems = []
    for c in changes:
        paths = [p for p in (c.get("old"), c.get("new")) if p]
        new_mode, old_mode = c.get("new_mode"), c.get("old_mode")
        label = c.get("old") or c.get("new")
        if new_mode == SYMLINK:
            problems.append(f"{c['new']}: symlink"); continue
        if new_mode == SUBMODULE:
            problems.append(f"{c['new']}: submodule"); continue
        if docker_mode and c.get("status") == "A" and new_mode == EXECUTABLE:
            problems.append(f"{c['new']}: exec-bit (a new executable file)"); continue
        if old_mode and new_mode and old_mode != new_mode:
            problems.append(f"{label}: exec-bit change"); continue
        hit = None
        for p in paths:
            if normalise(p) in allowed_norm:
                continue
            h = protected_hit(p, protected) or (docker_reject_hit(p) if docker_mode else None)
            if h:
                hit = (p, h)
                break
        if hit:
            problems.append(f"{hit[0]}: protected ({hit[1]})"); continue
        if lane == "qa" and not all(is_test_file(p, test_globs) for p in paths):
            problems.append(f"{label}: not a test file (qa lane)")
    return problems
