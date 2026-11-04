#!/usr/bin/env python3
"""Environment and profile for processes that run untrusted code (spec §6.4).

child_env() starts from nothing: only ALLOW variables are copied from the parent,
plus the tool's own `keep` keys (never a GitHub credential), plus fixed git/gh
neutralisers and profile variables pointing into a temp profile.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil

ALLOW = ["PATH", "PATHEXT", "SystemRoot", "SYSTEMROOT", "ComSpec", "COMSPEC", "TEMP", "TMP", "TMPDIR",
         "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG", "LC_ALL", "PYTHONUTF8", "TERM"]
NEVER = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_CONFIG_DIR",
         "GIT_ASKPASS", "SSH_ASKPASS", "SSH_AUTH_SOCK"}
SECRET_NAME = re.compile(r"(TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL|AUTH)", re.I)


def profile_vars(profile: Path):
    home = profile / "home"
    return {"HOME": str(home), "USERPROFILE": str(home),
            "APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local")}


def child_env(parent, profile: Path, gh_dir: Path, keep, extra_keep_nonsecret=None):
    env = {k: parent[k] for k in ALLOW if k in parent}
    for k in keep or []:
        if k in parent and k not in NEVER:
            env[k] = parent[k]
    for k in extra_keep_nonsecret or []:
        if k in parent and k not in NEVER and not SECRET_NAME.search(k):
            env[k] = parent[k]
    env.update(profile_vars(profile))
    env.update({
        "GH_CONFIG_DIR": str(gh_dir),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_SSH_COMMAND": "false",
        "GIT_SSH": "false",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    overrides = [("credential.helper", ""), ("protocol.allow", "never"), ("protocol.file.allow", "always"),
                 ("core.sshCommand", "false"), ("core.fsmonitor", "false"), ("core.hooksPath", os.devnull)]
    env["GIT_CONFIG_COUNT"] = str(len(overrides))
    for i, (key, value) in enumerate(overrides):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def make_dirs(profile: Path, gh_dir: Path):
    for v in profile_vars(profile).values():
        Path(v).mkdir(parents=True, exist_ok=True)
    gh_dir.mkdir(parents=True, exist_ok=True)


def seed_profile(profile: Path, real_home: Path, seed):
    """Copy each listed file (relative to the real home) into the temp profile. One way only."""
    home = profile / "home"
    real_home = real_home.resolve()
    for rel in seed or []:
        src = (real_home / rel).resolve()
        if real_home not in src.parents:
            raise ValueError(f"seed path escapes home: {rel}")
        if not src.is_file():
            continue
        dst = home / Path(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
