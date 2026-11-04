#!/usr/bin/env python3
"""Merge-gate verify probe: exits non-zero if the verify environment can reach GitHub credentials.

Run as a verify command through lane-delegate. It fails when
  - GH_TOKEN or GITHUB_TOKEN is set,
  - `gh auth status` succeeds (gh is missing or logged out: fine), or
  - `git credential fill` for github.com prints a `password=` line.
"""
import os
import subprocess
import sys

problems = []

leaked = [k for k in ("GH_TOKEN", "GITHUB_TOKEN") if os.environ.get(k)]
if leaked:
    problems.append(f"token variables present: {leaked}")

try:
    gh = subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=30)
    if gh.returncode == 0:
        problems.append("`gh auth status` succeeded")
except (FileNotFoundError, subprocess.TimeoutExpired):
    pass

try:
    cred = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                          capture_output=True, text=True, timeout=30)
    if any(line.startswith("password=") for line in cred.stdout.splitlines()):
        problems.append("`git credential fill` returned a password for github.com")
except subprocess.TimeoutExpired:
    pass
except FileNotFoundError:
    pass

if problems:
    print("verify_probe: " + "; ".join(problems), file=sys.stderr)
    sys.exit(1)
