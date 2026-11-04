#!/usr/bin/env python3
"""super-board-setup.py — the deterministic half of `/super-board onboard`.

Onboard asks the questions; this script finds things out and fixes the must-haves,
so step 1 (Checks) can say "Fixed for you" without asking anything.

    super-board-setup.py check   [--root DIR] [--pack DIR]            what is missing / old (read-only)
    super-board-setup.py fix     [--root DIR] [--pack DIR] [--no-helpers] [--dry-run]
                                                                       apply every must-have, then re-check
    super-board-setup.py migrate-config <config.json> [--dry-run] [--interactive] [--approver LOGIN]
                                                                       old config keys → v3 keys, plus the settings that follow from
                                                                       other answers: merge_policy human ⇒ human_approves_merge true;
                                                                       a robot bot_identity with no trusted_approvers ⇒ [repo owner]
                                                                       (--interactive asks who; otherwise it fills the owner and prints it)
    super-board-setup.py sync-agents-md [--root DIR] [--dry-run]       AGENTS.md Merge / Migrations rows follow merge_policy / allowed_envs
    super-board-setup.py sync-guard-env [--root DIR] [--dry-run]       settings.json env SB_GH_IDENTITY_GUARD=1 when bot_identity is set
                                                                       (an org owner is never filled in as the approver: --approver is required)
    super-board-setup.py names   [--root DIR]                          two board-name suggestions
    super-board-setup.py branch  [--root DIR]                          branches, deploy source, recommendation
    super-board-setup.py board-rank    --owner O                       the user's boards, best column match first
    super-board-setup.py board-migrate (--config C | --owner O --number N --repo R) [--qa-all] [--prune-empty] [--root DIR] [--dry-run]
                                                                       columns + labels + Skipped → Done
    super-board-setup.py ensure-fields (--config C | --owner O --number N) [--dry-run]
                                                                       create the board's Agent / Skills / Card type fields when missing
                                                                       (idempotent; never deletes or recolors; prints the one manual view step)

Every command prints one JSON object. `--text` on check/fix prints the short ✓ lists the
onboard screen shows instead.

What `fix` does, with NO question (all of it backed up first to
.claude/super-board/backup/<ts>/):
  - installs or refreshes skills, .claude/bin scripts, workflows, guard hooks and their
    settings.json entries (runs the pack's install.sh) when any is missing or older
  - removes the folders of skills that no longer exist: super-refine, cleanup-wt, arch-loop
  - migrates every .claude/super-board/configs/*.json to the v3 keys (migrate-config)
  - `git init` when the folder is not a repo
  - adds Matt Pocock's helper skills with npx when Node is present and they are missing
  - creates the board fields Agent, Skills and Card type on every configured GitHub Project that lacks
    them (`check` reports them red; needs `gh` with project scope). GitHub's API cannot change which
    fields a view shows, so the manual step is always printed: View menu ▾ → Fields → tick them
It never installs a system tool or signs in: those come back in `needs` with the exact
command for this OS, for onboard to ask about.

`board-migrate` is the board half (needs `gh` with project scope): adds the missing
Status options (--prune-empty also drops unused extras such as a new board's Todo),
creates the three labels, maps old type labels, labels every card `qa`
on a board that was "qa-only" (--qa-all), moves Skipped cards to Done, removes the Skipped
option, and puts back any card status the option rewrite cleared. Before any write it saves
all cards and conversion intent in .claude/super-board/migrations/ (under --root, the config project root, or cwd).
Re-run the same command from the same root to recover an interrupted upgrade. Keep the
board idle during migration: GitHub has no atomic compare-and-swap for field rewrites.
Before the first option rewrite it also saves the original card statuses to
.claude/super-board/backup/board-<number>-<ts>.json for manual recovery.
No card is deleted. Detected concurrent edits are preserved or halt recovery.

Exit: 0 ok · 1 check found red items · 2 a gh call failed · 64 usage · 66 pack not found.
Stdlib only.
"""
from __future__ import annotations

import contextlib
import hashlib
import datetime as dt
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys

COLUMNS = ["Backlog", "Ready", "Building", "QA", "Review", "Blocked", "Done"]
LABELS = {
    "qa": ("0e8a16", "Test what exists; skips Building (super-board)"),
    "bug": ("d73a4a", "Something is broken; built, tested, reviewed (super-board)"),
    "feature": ("1d76db", "New behaviour; built, tested, reviewed (super-board)"),
}
# Type labels from older packs and common trackers → the three v3 labels.
OLD_LABELS = {
    "build": "feature", "type:feature": "feature", "kind:feature": "feature", "enhancement": "feature",
    "bug-fix": "bug", "bugfix": "bug", "type:bug": "bug", "kind:bug": "bug",
    "qa-only": "qa", "type:qa": "qa", "kind:qa": "qa",
}
SKILLS = ["super-board", "super-build", "super-qa", "super-review", "super-collect", "visual", "git-sync", "ui-refine-loop"]
OLD_SKILL_DIRS = ["super-refine", "cleanup-wt", "arch-loop"]
BIN = ["super-board-project-verifier.py", "super-board-skills-ready.py", "super-board-host.sh", "super-board-codex-wave.sh", "super-board-codex-lane.sh", "super-board-calibration.py", "super-board-verification-ready.py", "super-board-github-read.py", "super-board-collect-state.py", "super-board-run.sh", "super-board-gh-guard.sh", "super-board-card.sh", "super-board-status.py", "super-board-roadmap.py", "super-board-wave-plan.sh",
       "super-board-deps.sh", "super-board-preflight.sh", "super-board-merge-gate.sh",
       "super-board-merge-policy.py", "super-board-approval.py", "super-board-env-check.sh", "super-board-agents-md.py",
       "super-board-settings.py", "super-board-setup.py", "super-board-usage.sh", "super-board-pr-body.sh",
       "super-review-file-refactor.sh", "super-qa-file-bug.sh", "super-board-stop.sh",
       "super-board-lane-delegate.py", "super-board-lane-paths.py", "super-board-lane-env.py",
       "super-board-lane-integrity.py", "super-board-lane-signatures.py", "super-board-lane-allowpath.py",
       "super-board-lane-docker.py", "super-board-lane-remote.py", "super-board-squid.conf.tmpl"]
WORKFLOWS = ["super-board-wave.js", "ui-refine-loop.js"]
HELPERS_CMD = "npx -y skills@latest add mattpocock/skills --skill '*' -a claude-code -y"

# System tools: why the board needs each, and the install command per OS family.
TOOLS = {
    "git": ("branches, worktrees and merges", {"mac": "brew install git", "apt": "sudo apt-get install -y git",
            "dnf": "sudo dnf install -y git", "win": "winget install --id Git.Git -e"}),
    "gh": ("reads and moves cards on GitHub", {"mac": "brew install gh", "apt": "sudo apt-get install -y gh",
           "dnf": "sudo dnf install -y gh", "win": "winget install --id GitHub.cli -e"}),
    "jq": ("the board scripts read JSON with it", {"mac": "brew install jq", "apt": "sudo apt-get install -y jq",
           "dnf": "sudo dnf install -y jq", "win": "winget install --id jqlang.jq -e"}),
    "node": ("the helper installer; actual skill files are checked separately",
             {"mac": "brew install node", "apt": "sudo apt-get install -y nodejs npm",
              "dnf": "sudo dnf install -y nodejs npm", "win": "winget install --id OpenJS.NodeJS.LTS -e"}),
}


# ── small helpers ──────────────────────────────────────────────────────────────

def out(obj, code=0):
    print(json.dumps(obj, indent=1))
    return code


def run(cmd, cwd=None, check=False, input=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def have(tool):
    return shutil.which(tool) is not None


def _find_on_path(name, path=None, skip_wsl=False):
    """First absolute PATH hit for `name`. Relative and empty entries are skipped (the current directory is
    never searched); surrounding quotes are stripped. With skip_wsl, the WSL launcher dirs are ignored."""
    exts = [""]
    wsl_dirs = ()
    if os.name == "nt":
        exts = [e for e in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
        sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
        wsl_dirs = (os.path.normcase(os.path.join(sysroot, "System32")),
                    os.path.normcase(os.path.join(sysroot, "Sysnative")))
    for d in (path if path is not None else os.environ.get("PATH", "")).split(os.pathsep):
        d = d.strip('"')
        if not d or not os.path.isabs(d):
            continue
        nd = os.path.normcase(os.path.normpath(d))
        if skip_wsl and (nd in wsl_dirs or nd.endswith(os.path.normcase(r"\Microsoft\WindowsApps"))):
            continue
        for ext in exts:
            candidate = os.path.join(d, name + ext)
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _git_bash(path=None):
    """The Git Bash that belongs to the installed git (`git --exec-path` -> <git root>/bin|usr/bin/bash.exe)."""
    git_exe = _find_on_path("git", path)
    if not git_exe:
        return None
    try:
        r = subprocess.run([git_exe, "--exec-path"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        return None
    root = out
    for _ in range(3):
        root = os.path.dirname(root)
    if not root:
        return None
    for rel in (("bin", "bash.exe"), ("usr", "bin", "bash.exe")):
        candidate = os.path.join(root, *rel)
        if os.path.isfile(candidate):
            return candidate
    return None


def bash_exe(path=None):
    """Absolute path of a bash to run. A bare "bash" on Windows resolves to C:\\Windows\\System32\\bash.exe
    (WSL), which mangles C:\\ arguments, so on Windows: a non-WSL bash on PATH, else the installed git's Git Bash,
    else None. Elsewhere: bash on PATH, else the plain string "bash"."""
    found = _find_on_path("bash", path, skip_wsl=True)
    if found:
        return found
    if os.name == "nt":
        return _git_bash(path)
    return "bash"


def bash_path(p):
    """A path handed to bash as an argument: forward slashes on Windows, unchanged elsewhere."""
    return p.replace("\\", "/") if os.name == "nt" else p


def os_family():
    sysname = platform.system()
    if sysname == "Darwin":
        return "mac"
    if sysname == "Windows" or os.environ.get("OS") == "Windows_NT" or re.match(r"(MINGW|MSYS|CYGWIN)", sysname):
        return "win"
    if have("apt-get"):
        return "apt"
    if have("dnf"):
        return "dnf"
    return "apt"


def vtuple(v):
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)
    except ValueError:
        return (0,)


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_raw(path):
    """File text exactly as written (UTF-8, no stripping); "" when unreadable."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except OSError:
        return ""


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def find_pack(explicit=None):
    """The pack checkout whose install.sh can (re)install pieces. None when not found."""
    cands = [explicit, os.environ.get("SUPER_BOARD_PACK"),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    home = os.path.expanduser("~/.claude/plugins")
    if os.path.isdir(home):
        cands += sorted(os.path.dirname(p) for p in glob.glob(os.path.join(home, "**", "install.sh"), recursive=True)
                        if "super-board" in p and "node_modules" not in p)
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "install.sh")) and os.path.isdir(os.path.join(c, "skills", "super-board")):
            return os.path.abspath(c)
    return None


def helper_readiness(root, host="claude", home=None, catalog=None):
    command = [sys.executable, os.path.join(os.path.dirname(__file__), "super-board-skills-ready.py"),
               "--root", root, "--host", host, "--profile", "setup"]
    if home:
        command += ["--home", home]
    if catalog:
        command += ["--catalog", catalog]
    process = run(command)
    try:
        result = json.loads(process.stdout)
    except ValueError:
        result = {"ready": False, "entries": [], "error": process.stderr.strip()}
    return result


def is_git_repo(root):
    return have("git") and run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root).returncode == 0


def snippet_commands(pack):
    snip = load_json(os.path.join(pack, "hooks", "settings-snippet.json")) if pack else None
    if not snip:
        return []
    return [h["command"] for entries in snip["hooks"].values() for e in entries for h in e.get("hooks", [])]


def settings_commands(root):
    s = load_json(os.path.join(root, ".claude", "settings.json")) or {}
    return {h.get("command") for entries in (s.get("hooks") or {}).values() for e in entries
            for h in e.get("hooks", [])}


# ── config migration ───────────────────────────────────────────────────────────

def machine_tz():
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def real_login(v):
    """A machine-user login, or None for unset / "auto" / a GitHub App ("...[bot]")."""
    v = v.strip() if isinstance(v, str) else ""
    return v if v and v.lower() != "auto" and not v.lower().endswith("[bot]") else None


def repo_owner(cfg):
    """The repo / project owner login a config names, or None."""
    owner = (cfg.get("project") or {}).get("owner")
    if isinstance(owner, str) and owner:
        return owner
    m = re.search(r"github\.com[:/]([^/]+)/", (cfg.get("repo") or {}).get("remote") or "")
    return m.group(1) if m else None


def owner_is_org(owner):
    """True when GitHub says `owner` is an organization (an org login cannot approve: a person must).
    Unknown (no gh, offline, not signed in) counts as a person."""
    if not have("gh"):
        return False
    r = run(["gh", "api", f"users/{owner}", "--jq", ".type"])
    return r.returncode == 0 and r.stdout.strip().lower() == "organization"


NO_APPROVER = "merge_policy.trusted_approvers NOT filled"


def derive_answers(c, changes, approver=None):
    """Settings that follow from other answers (in place): merge_policy "human" means
    human_approves_merge true; a robot login with no trusted_approvers defaults them to the repo owner,
    because an empty list holds every merge (config-schema.json)."""
    mp = c.get("merge_policy")
    if isinstance(mp, dict) and mp.get("default") == "human" and c.get("human_approves_merge") is not True:
        c["human_approves_merge"] = True
        changes.append("human_approves_merge: true (merge_policy.default is \"human\")")
    bot = real_login((c.get("notifications") or {}).get("bot_identity"))
    owner = approver or repo_owner(c)
    if bot and isinstance(mp, dict) and not mp.get("trusted_approvers") and owner and owner.lower() != bot.lower():
        if not approver and owner_is_org(owner):
            changes.append(f"{NO_APPROVER}: \"{owner}\" is an organization, which cannot approve; pass "
                           "--approver <login of the person who approves merges> (or answer the prompt)")
            return
        mp["trusted_approvers"] = [owner]
        changes.append(f"merge_policy.trusted_approvers: [\"{owner}\"] (the repo owner: a robot login with an empty "
                       "list would hold every merge; edit the list to add other approvers)")


def migrate_config(cfg, approver=None):
    """Return (new config, [human-readable change lines]). Pure; never drops a project setting
    that still means something. `approver` overrides the owner default for trusted_approvers."""
    c = json.loads(json.dumps(cfg))
    changes = []
    variant = c.pop("variant", None)
    if variant is not None:
        changes.append(f"variant \"{variant}\" removed — labels route cards now"
                       + (" (cards get the qa label)" if variant == "qa-only" else ""))
        if variant == "qa-only":
            c["_qa_all"] = True  # read by board-migrate --qa-all, then dropped
    cols = c.get("columns")
    if cols != COLUMNS:
        extra = [x for x in (cols or []) if x not in COLUMNS and x != "Skipped"]
        c["columns"] = COLUMNS + extra
        if cols and "Skipped" in cols:
            changes.append("Skipped column removed from config")
        missing = [x for x in COLUMNS if x not in (cols or [])]
        if missing:
            changes.append("columns added: " + ", ".join(missing))
    tgt = c.get("target") or {}
    if tgt.get("type") == "url":
        c["target"] = {"type": "repo+url" if c.get("repo") else "repo", "url": tgt.get("url")}
        changes.append("URL-only target → repo board (a live site alone: /super-qa <url>)")
    col = c.get("collect")
    if isinstance(col, dict) and any(k in col for k in ("errors", "feedback_paths", "lookback_runs")):
        errors = col.pop("errors", None)
        col.pop("feedback_paths", None)
        col.pop("lookback_runs", None)
        if "sources" not in col:
            col["sources"] = (["sentry"] if errors in ("auto", "sentry") else []) + ["github", "prs", "architecture"]
        changes.append("collect: intake/lookback keys → sources " + ", ".join(col["sources"]))
    ref = c.get("refine")
    if isinstance(ref, dict) and "qa_hook_rounds" in ref:
        ref.pop("qa_hook_rounds")
        changes.append("refine.qa_hook_rounds removed (the board no longer runs ui-refine-loop)")
    if "merge_policy" not in c:
        c["merge_policy"] = {"default": "human" if c.get("human_approves_merge") else "auto"}
        changes.append(f"merge rule: merge_policy.default \"{c['merge_policy']['default']}\" (from human_approves_merge)")
    if "migrations" not in c:
        c["migrations"] = {"allowed_envs": ["test", "staging"],
                           "target_env": "live" if c.get("base_branch", "main") in ("main", "master") else "staging"}
        changes.append("migrations: test + staging allowed (defaults)")
    n = c.get("notifications")
    if isinstance(n, dict) and n.get("channel") not in (None, "session"):
        changes.append(f"notifications: \"{n.get('channel')}\" → session (super-board sends no messages)")
        n["channel"], n["chat_id"] = "session", None
    if not c.get("timezone"):
        c["timezone"] = machine_tz()
        changes.append(f"timezone {c['timezone']} added")
    if not c.get("worker_backend"):
        c["worker_backend"] = "workflow"
        changes.append("worker_backend \"workflow\" added")
    derive_answers(c, changes, approver)
    return c, changes


def guard_env_sync(root, dry=False):
    """When a board config sets notifications.bot_identity, `.claude/settings.json` env carries
    SB_GH_IDENTITY_GUARD=1 so the identity guard is switched on by the session's own environment, not
    only by files the agent can edit. True when it was missing (and is now written unless dry)."""
    cfgs = [c for c in (load_json(p) for p in sorted(glob.glob(os.path.join(root, ".claude", "super-board", "configs", "*.json"))))
            if isinstance(c, dict)]
    ident = [(c.get("notifications") or {}).get("bot_identity") for c in cfgs]
    if not any(isinstance(i, str) and i.strip() and i.strip().lower() != "auto" for i in ident):
        return False
    path = os.path.join(root, ".claude", "settings.json")
    if os.path.exists(path):
        data = load_json(path)
        if not isinstance(data, dict):
            return False  # unparseable: leave it alone; the identity guard refuses gh until it is fixed
    else:
        data = {}
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    if env.get("SB_GH_IDENTITY_GUARD") == "1":
        return False
    if not dry:
        env["SB_GH_IDENTITY_GUARD"] = "1"
        data["env"] = env
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json(path, data)
    return True


# ── AGENTS.md rows that follow the config ──────────────────────────────────────

AGENTS_ROWS = {
    "Merge": "| Merge | every merge waits for the owner (merge_policy.default human) · the robot opens PRs, a human merges |",
    "Migrations": "| Migrations | no robot migrations (migrations.allowed_envs is empty) · every DB change → 🙋 needs you |",
}


def agents_sync(root, dry=False):
    """Make the managed block's Merge and Migrations rows say what the config(s) say. A restrictive
    answer rewrites the row; the default answer puts the template's row back. Returns the rows changed."""
    path = os.path.join(root, "AGENTS.md")
    text = read_raw(path)
    cfgs = [c for c in (load_json(p) for p in sorted(glob.glob(os.path.join(root, ".claude", "super-board", "configs", "*.json"))))
            if isinstance(c, dict)]
    if not text or not cfgs:
        return []
    human = any((c.get("merge_policy") or {}).get("default") == "human" or c.get("human_approves_merge") is True
                for c in cfgs)
    no_migr = all(isinstance((c.get("migrations") or {}).get("allowed_envs"), list)
                  and (c.get("migrations") or {}).get("allowed_envs") == [] for c in cfgs)
    want = {"Merge": human, "Migrations": no_migr}
    tpl = {}
    for line in read_raw(os.path.join(root, ".claude", "skills", "super-board", "references", "agents-md-block.md")).splitlines():
        for k in AGENTS_ROWS:
            if line.startswith(f"| {k} |"):
                tpl[k] = line
    lines, changed, inside = text.split("\n"), [], False
    for i, line in enumerate(lines):
        if line.startswith("<!-- super-board:begin"):
            inside = True
        elif line.startswith("<!-- super-board:end"):
            inside = False
        elif inside:
            for k, row in AGENTS_ROWS.items():
                if line.startswith(f"| {k} |"):
                    target = row if want[k] else tpl.get(k)
                    if target and line.rstrip("\r") != target:
                        lines[i] = target + ("\r" if line.endswith("\r") else "")
                        changed.append(k)
    if changed and not dry:
        tmp = path + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)
    return changed


# ── check / fix ────────────────────────────────────────────────────────────────

def installed_version(root):
    return read(os.path.join(root, ".claude", "skills", "super-board", "VERSION")) or None


PR_RULE_KEYS = ("pre_pr_commands", "pr_body_notes")


def pr_rules_error(cfg):
    """None when the optional `pr_rules` key is absent or well-formed, else the reason.
    Shape: {"pre_pr_commands": [non-blank strings], "pr_body_notes": [non-blank strings]}; both keys optional."""
    if not isinstance(cfg, dict) or "pr_rules" not in cfg or cfg["pr_rules"] is None:
        return None
    rules = cfg["pr_rules"]
    if not isinstance(rules, dict):
        return "pr_rules must be an object with pre_pr_commands and pr_body_notes arrays"
    unknown = sorted(set(rules) - set(PR_RULE_KEYS))
    if unknown:
        return f"pr_rules has unknown key(s) {', '.join(unknown)} (allowed: {', '.join(PR_RULE_KEYS)})"
    for key in PR_RULE_KEYS:
        if key not in rules:
            continue
        val = rules[key]
        if not isinstance(val, list) or any(not isinstance(v, str) for v in val):
            return f"pr_rules.{key} must be an array of strings"
        if any(not v.strip() for v in val):
            return f"pr_rules.{key} must not contain a blank entry"
        if key == "pre_pr_commands" and any("\x00" in v for v in val):
            return "pr_rules.pre_pr_commands must not contain NUL bytes (shells cannot preserve them)"
    return None


def check(root, pack, host="claude", home=None, catalog=None):
    claude = os.path.join(root, ".claude")
    pack_version = read(os.path.join(pack, "VERSION")) if pack else None
    have_v = installed_version(root)
    upgrade_rec = load_json(os.path.join(claude, "super-board", "upgrade.json")) or {}
    missing = []
    missing += [f"skill {s}" for s in SKILLS if not os.path.isfile(os.path.join(claude, "skills", s, "SKILL.md"))]
    missing += [f"script {b}" for b in BIN if not os.path.isfile(os.path.join(claude, "bin", b))]
    missing += [f"workflow {w}" for w in WORKFLOWS if not os.path.isfile(os.path.join(claude, "workflows", w))]
    hook_files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(pack, "hooks", "*.py"))) if pack else []
    missing += [f"hook {h}" for h in hook_files if not os.path.isfile(os.path.join(claude, "hooks", h))]
    for sh_hook in ("mc-identity-check.sh", "sb-python.sh"):
        if pack and os.path.isfile(os.path.join(pack, "hooks", sh_hook)):
            missing += [] if os.path.isfile(os.path.join(claude, "hooks", sh_hook)) else [f"hook {sh_hook}"]
    wired = settings_commands(root)
    unwired = [c for c in snippet_commands(pack) if c not in wired]
    if unwired:
        missing.append(f"settings.json: {len(unwired)} guard entr{'y' if len(unwired) == 1 else 'ies'}")
    old_dirs = [d for d in OLD_SKILL_DIRS if os.path.isdir(os.path.join(claude, "skills", d))]
    configs = {}
    verification = {}
    pr_rules = {}
    for p in sorted(glob.glob(os.path.join(claude, "super-board", "configs", "*.json"))):
        cfg = load_json(p)
        ready = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__),
                                "super-board-verification-ready.py"), "--config", p],
                               capture_output=True, text=True)
        if ready.returncode:
            verification[os.path.relpath(p, root)] = ready.stderr.strip()
        bad_rules = pr_rules_error(cfg)
        if bad_rules:
            pr_rules[os.path.relpath(p, root)] = bad_rules
        if isinstance(cfg, dict):
            _, ch = migrate_config(cfg)
            if ch:
                configs[os.path.relpath(p, root)] = ch
    older = bool(upgrade_rec.get("from")) or bool(old_dirs) or any(
        "variant" in " ".join(ch) or "Skipped" in " ".join(ch) or "collect:" in " ".join(ch) for ch in configs.values())
    if have_v and pack_version and vtuple(have_v) < vtuple(pack_version):
        older = True
    from_v = upgrade_rec.get("from") or (have_v if have_v and pack_version and vtuple(have_v) < vtuple(pack_version) else None)
    fam = os_family()
    needs = []
    for t in ("git", "gh", "jq", "node"):
        if not have(t if t != "node" else "npx"):
            why, cmds = TOOLS[t]
            cmd = cmds[fam]
            if t == "node":
                cmd += " && " + HELPERS_CMD
            needs.append({"kind": "tool", "name": t, "why": why, "command": cmd})
    readiness = helper_readiness(root, host, home, catalog)
    helpers = readiness["ready"]
    git = is_git_repo(root)
    ok = []
    if not missing:
        ok.append("super-board skills, scripts and board engine present")
        if hook_files:
            ok.append(f"{len(hook_files)} safety guards on")
    if git:
        ok.append("git repo found")
    if helpers:
        ok.append("required worker skills resolved for " + host)
    agents_rows = agents_sync(root, dry=True)
    guard_env = guard_env_sync(root, dry=True)
    board_fields, manual, board_fields_errors = board_fields_report(root)
    red = bool(missing or old_dirs or configs or verification or not git or needs or not helpers or agents_rows
               or guard_env or board_fields or pr_rules)
    return {
        "root": root, "pack": pack, "pack_version": pack_version, "installed_version": have_v,
        "upgrade": older, "from": from_v or ("older" if older else None),
        "added_skills": upgrade_rec.get("added_skills", []),
        "missing": missing, "old_dirs": old_dirs, "configs": configs, "agents_rows": agents_rows, "guard_env": guard_env, "verification": verification, "git": git,
        "pr_rules_errors": pr_rules, "board_fields": board_fields, "board_fields_unreadable": board_fields_errors, "manual_steps": manual,
        "helper_skills": helpers, "helper_readiness": readiness, "needs": needs, "os": fam, "ok": ok, "green": not red,
    }


def backup(root, paths, stamp):
    dest = os.path.join(root, ".claude", "super-board", "backup", stamp)
    for p in paths:
        if not os.path.exists(p):
            continue
        rel = os.path.relpath(p, root)
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.isdir(p):
            shutil.copytree(p, target, dirs_exist_ok=True)  # a second fix in the same minute reuses the folder
        else:
            shutil.copy2(p, target)
    return os.path.relpath(dest, root) + "/"


def fix(root, pack, helpers=True, dry=False, host="claude", home=None, catalog=None):
    before = check(root, pack, host, home, catalog)
    fixed, notes = [], []
    claude = os.path.join(root, ".claude")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    to_backup = [os.path.join(claude, "settings.json"), os.path.join(claude, "super-board", "configs"),
                 os.path.join(root, "AGENTS.md")]
    to_backup += [os.path.join(claude, "skills", d) for d in before["old_dirs"]]
    bdir = None
    if not dry and (before["missing"] or before["old_dirs"] or before["configs"] or before["upgrade"]
                    or before["agents_rows"] or before["guard_env"]):
        bdir = backup(root, to_backup, stamp)

    if before["missing"]:
        if not pack:
            return {**before, "fixed": [], "error": "pack-not-found"}
        if not dry:
            bash = bash_exe()
            if bash is None:
                return {**before, "fixed": fixed,
                        "error": "Git Bash not found: install Git for Windows (it ships bash) and re-run"}
            r = run([bash, bash_path(os.path.join(pack, "install.sh")), bash_path(root)],
                    input="", cwd=root)
            if r.returncode != 0:
                return {**before, "fixed": fixed, "error": "install.sh failed: " + (r.stderr or r.stdout)[-300:]}
            rec = load_json(os.path.join(claude, "super-board", "upgrade.json")) or {}
            before["added_skills"] = rec.get("added_skills", before["added_skills"])
        if before["upgrade"]:
            fixed.append("scripts, board engine and safety guards updated")
        else:
            fixed.append("super-board skills, scripts and board engine installed")
            if any(m.startswith("hook") or m.startswith("settings") for m in before["missing"]):
                n = len(glob.glob(os.path.join(pack, "hooks", "*.py")))
                fixed.append(f"{n} safety guards switched on")
                fixed.append("settings.json entries added (backup kept)")
    elif before["upgrade"]:
        fixed.append("scripts, board engine and safety guards updated")
    if before["upgrade"]:
        added = before["added_skills"]
        fixed.insert(0, "skills updated" + (f"; added {', '.join(added)}" if added else ""))

    if before["old_dirs"]:
        if not dry:
            for d in before["old_dirs"]:
                shutil.rmtree(os.path.join(claude, "skills", d), ignore_errors=True)
        fixed.append("removed old folders: " + ", ".join(before["old_dirs"]))

    for rel, ch in before["configs"].items():
        p = os.path.join(root, rel)
        new, _ = migrate_config(load_json(p))
        if not dry:
            write_json(p, new)
        if before["upgrade"]:
            fixed.append(f"config moved to the new keys ({len(ch)} change{'s' if len(ch) != 1 else ''})")
        else:
            fixed.append(f"config repaired: {rel}")
        notes += ch

    rows = agents_sync(root, dry)
    if rows:
        fixed.append("AGENTS.md rows now follow the config: " + ", ".join(rows))

    if guard_env_sync(root, dry):
        fixed.append("settings.json env: SB_GH_IDENTITY_GUARD=1 (the identity guard no longer depends on editable files)")

    for rel, info in before["board_fields"].items():
        try:
            done = ensure_fields(info["owner"], info["number"], dry)
        except (RuntimeError, ValueError, OSError) as exc:
            notes.append(f"board fields on {info['owner']}/{info['number']} not created: {str(exc)[:200]}")
            continue
        bits = done["created"]
        if bits:
            fixed.append(f"board fields {'would be created' if dry else 'created'} on {info['owner']}/{info['number']}: "
                         + ", ".join(bits))
        notes += done["problems"]

    if not before["git"] and have("git"):
        if not dry:
            run(["git", "init", "-q"], cwd=root)
        fixed.append("git repo created (git init)")

    if helpers and not before["helper_skills"] and have("npx"):
        if not dry:
            bash = bash_exe()
            if bash is None:
                notes.append("helper install skipped: Git Bash not found")
            else:
                run([bash, "-c", HELPERS_CMD], cwd=root, input="")
                confirmed = helper_readiness(root, host, home, catalog)
                if confirmed["ready"]:
                    fixed.append("worker skills re-resolved after helper installation")
                else:
                    notes.append("helper install attempted; required skills remain unavailable: " +
                                 ", ".join(e["name"] for e in confirmed.get("entries", []) if e["status"] == "missing"))
        else:
            notes.append("would attempt helper installation, then re-resolve actual host-visible files")

    if not dry:
        up = os.path.join(claude, "super-board", "upgrade.json")
        if os.path.exists(up):
            os.remove(up)
    after = check(root, pack, host, home, catalog) if not dry else before
    return {**after, "upgraded": before["upgrade"], "from": before["from"], "fixed": fixed,
            "notes": notes, "backup": bdir}


def text_report(res):
    lines = []
    if res.get("upgraded"):
        lines.append(f"Upgraded for you (backup: {res.get('backup') or '—'})")
    else:
        lines.append("Fixed for you:" if res.get("fixed") else "Checks:")
    done = res.get("fixed", []) + [o for o in res.get("ok", []) if not res.get("upgraded")
                                    and not any(o.split()[0] in f for f in res.get("fixed", []))]
    for f in done:
        lines.append(f"  ✓ {f}")
    if res.get("green"):
        lines.append("  ✓ re-checked: all green")
    elif "fixed" not in res:
        red = res.get("missing", [])[:3] + [f"old folder {d}" for d in res.get("old_dirs", [])] + \
            [f"config {c} uses old keys" for c in res.get("configs", {})] + \
            [f"AGENTS.md {r} row does not match the config" for r in res.get("agents_rows", [])] + \
            (["settings.json env lacks SB_GH_IDENTITY_GUARD=1 (a robot identity is configured)"] if res.get("guard_env") else []) + \
            [f"board fields missing on {i['owner']}/{i['number']}: {', '.join(i['missing'])}"
             for i in res.get("board_fields", {}).values()] + \
            ([] if res.get("git") else ["not a git repo"])
        lines += [f"  ✗ {r}" for r in red]
        if red:
            lines.append("  → fix: super-board-setup.py fix")
    for entry in res.get("helper_readiness", {}).get("entries", []):
        if entry["status"] == "missing":
            lines.append(f"  ✗ missing {entry['name']} for {res['helper_readiness']['host']}; install it in that host and restart")
    if res.get("helper_readiness", {}).get("error"):
        lines.append("  ✗ " + res["helper_readiness"]["error"])
    for rel, error in res.get("board_fields_unreadable", {}).items():
        lines.append(f"  ! could not read the board fields for {rel}: {error}")
    for step in res.get("manual_steps", []):
        lines.append(f"  ☐ {step}")
    for path, error in res.get("pr_rules_errors", {}).items():
        lines.append(f"  ✗ {path}: {error}")
    for path, error in res.get("verification", {}).items():
        lines.append(f"  ✗ {path}: {error}")
    for n in res.get("needs", []):
        lines.append(f"  Needs your machine: {n['name']} isn't installed. It's needed for {n['why']}.")
        lines.append(f"    {n['command']}")
    return "\n".join(lines)


# ── board names and branches ──────────────────────────────────────────────────

def names(root):
    pkg = load_json(os.path.join(root, "package.json")) or {}
    base = (pkg.get("name") or "").split("/")[-1]
    if not base:
        m = re.search(r'^name\s*=\s*"([^"]+)"', read(os.path.join(root, "pyproject.toml")), re.M)
        base = m.group(1) if m else ""
    readme_title = ""
    for f in ("README.md", "readme.md", "README"):
        m = re.search(r"^#\s+(.+)$", read(os.path.join(root, f)), re.M)
        if m:
            readme_title = re.split(r"\s+[—–-]\s+|:\s", m.group(1).strip())[0].strip()
            break
    folder = os.path.basename(os.path.abspath(root))
    cap = lambda s: s[:1].upper() + s[1:]
    first = cap(base or readme_title or folder)
    second = cap(readme_title or base or folder)
    empty = not base and not readme_title and not os.listdir(root) if os.path.isdir(root) else True
    if empty:
        return {"names": [f"{folder} board", "My first board"], "from": "folder"}
    n1, n2 = f"{first} board", f"{second} build board"
    return {"names": [n1, n2], "from": "package.json" if base else ("README" if readme_title else "folder")}


def branch(root):
    git = lambda *a: run(["git", *a], cwd=root)
    if not is_git_repo(root):
        return {"git": False, "branches": [], "staging": False, "deploy": None,
                "recommend": "main", "create_staging": False}
    local = [b for b in git("branch", "--format=%(refname:short)").stdout.split() if b]
    remote = [b.split("/", 1)[1] for b in git("branch", "-r", "--format=%(refname:short)").stdout.split()
              if "/" in b and not b.endswith("/HEAD")]
    allb = sorted(set(local + remote))
    head = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD").stdout.strip().split("/", 1)[-1]
    default = head or ("main" if "main" in allb else ("master" if "master" in allb else (local[0] if local else "main")))
    staging = next((b for b in ("staging", "develop", "dev") if b in allb), None)
    deploy = None
    if os.path.exists(os.path.join(root, "vercel.json")) or os.path.isdir(os.path.join(root, ".vercel")):
        deploy = f"Vercel deploys {default}"
    elif os.path.exists(os.path.join(root, "netlify.toml")):
        deploy = f"Netlify deploys {default}"
    else:
        for wf in glob.glob(os.path.join(root, ".github", "workflows", "*.y*ml")):
            body = read(wf)
            if re.search(r"deploy|vercel|netlify|fly|render|heroku|pages", body, re.I) and re.search(
                    rf"branches:\s*\[?\s*['\"]?{re.escape(default)}\b", body):
                deploy = f"{os.path.basename(wf)} deploys {default}"
                break
    return {"git": True, "branches": [b for b in allb if not b.startswith(("issue-", "feat/", "fix/"))][:8],
            "default": default, "staging": staging, "deploy": deploy,
            "recommend": staging or "staging", "create_staging": staging is None,
            "create_command": None if staging else f"git push origin {default}:staging"}


# ── GitHub board: rank and migrate ────────────────────────────────────────────

def gh_json(*args, input=None):
    r = run(["gh", *args], input=input)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[:300]}")
    return json.loads(r.stdout or "{}")


def status_field(owner, number):
    fields = gh_json("project", "field-list", str(number), "--owner", owner, "--format", "json",
                     "--limit", "100").get("fields", [])
    return next((f for f in fields if f.get("name") == "Status"), None)


def board_rank(owner):
    projects = gh_json("project", "list", "--owner", owner, "--format", "json", "--limit", "50").get("projects", [])
    ranked = []
    for p in projects:
        if p.get("closed"):
            continue
        f = status_field(owner, p["number"]) or {}
        have = [o["name"] for o in f.get("options", [])]
        low = {h.lower() for h in have}
        match = [c for c in COLUMNS if c.lower() in low]
        ranked.append({"number": p["number"], "title": p.get("title"), "url": p.get("url"),
                       "cards": (p.get("items") or {}).get("totalCount", 0),
                       "matches": len(match), "missing": [c for c in COLUMNS if c not in match],
                       "extra": [h for h in have if h.lower() not in {c.lower() for c in COLUMNS}]})
    ranked.sort(key=lambda r: (-r["matches"], -r["cards"], -r["number"]))
    best = ranked[0] if ranked and ranked[0]["matches"] >= 4 else None
    return {"owner": owner, "boards": ranked, "recommend": best and best["number"]}


OPTIONS_Q = "query($id:ID!){node(id:$id){... on ProjectV2SingleSelectField{options{id name color description}}}}"
UPDATE_M = ("mutation($id:ID!,$opts:[ProjectV2SingleSelectFieldOptionInput!]!){updateProjectV2Field(input:"
            "{fieldId:$id,singleSelectOptions:$opts}){projectV2Field{... on ProjectV2SingleSelectField{options{id name}}}}}")
COLORS = {"Backlog": "GRAY", "Ready": "BLUE", "Building": "YELLOW", "QA": "ORANGE", "Review": "PURPLE",
          "Blocked": "RED", "Done": "GREEN"}


def gql(query, variables):
    return gh_json("api", "graphql", "--input", "-", input=json.dumps({"query": query, "variables": variables}))


def item_labels(it):
    raw = (it.get("labels") or []) + ((it.get("content") or {}).get("labels") or [])
    return {(x.get("name") if isinstance(x, dict) else str(x)).lower() for x in raw}


# Explicit cursors keep the recovery snapshot complete on boards of any size.
ITEMS_Q = '''query($id:ID!,$after:String){node(id:$id){... on ProjectV2{
items(first:100,after:$after){totalCount pageInfo{hasNextPage endCursor} nodes{
id updatedAt type status:fieldValueByName(name:"Status"){
... on ProjectV2ItemFieldSingleSelectValue{name optionId}}
content{__typename ... on Issue{id number repository{nameWithOwner}
labels(first:100){totalCount pageInfo{hasNextPage endCursor} nodes{name}}}}}}}}}'''
ITEM_Q = '''query($id:ID!){node(id:$id){... on ProjectV2Item{
id updatedAt type status:fieldValueByName(name:"Status"){
... on ProjectV2ItemFieldSingleSelectValue{name optionId}}}}}'''
LABELS_Q = '''query($id:ID!,$after:String){node(id:$id){... on Issue{
labels(first:100,after:$after){totalCount pageInfo{hasNextPage endCursor} nodes{name}}}}}'''


def node_query(query, variables):
    result = gql(query, variables)
    if not isinstance(result, dict) or result.get('errors'):
        raise RuntimeError('GitHub returned an incomplete GraphQL response')
    node = (result.get('data') or {}).get('node')
    if not isinstance(node, dict):
        raise RuntimeError('GitHub did not return the requested board, field or item')
    return node


def connection_page(connection, seen, expected=None):
    if not isinstance(connection, dict):
        raise RuntimeError('GitHub omitted a required page')
    total, page, nodes = connection.get('totalCount'), connection.get('pageInfo'), connection.get('nodes')
    if (type(total) is not int or total < 0 or not isinstance(nodes, list) or
            not isinstance(page, dict) or type(page.get('hasNextPage')) is not bool or
            (expected is not None and total != expected)):
        raise RuntimeError('GitHub returned an incomplete or changing page count')
    cursor = page.get('endCursor') if page['hasNextPage'] else None
    if page['hasNextPage'] and (not isinstance(cursor, str) or not cursor or cursor in seen or not nodes):
        raise RuntimeError('GitHub pagination did not advance')
    return nodes, total, cursor


def issue_labels(issue_id, first=None):
    labels, seen, total, cursor = [], set(), None, None
    while True:
        conn = first if first is not None else node_query(LABELS_Q, {'id': issue_id, 'after': cursor}).get('labels')
        first = None
        nodes, total, cursor = connection_page(conn, seen, total)
        for label in nodes:
            if not isinstance(label, dict) or not isinstance(label.get('name'), str):
                raise RuntimeError('GitHub omitted an issue label')
            labels.append(label['name'])
        if cursor is None:
            break
        seen.add(cursor)
    if len(labels) != total or len(set(labels)) != total:
        raise RuntimeError('GitHub returned incomplete or duplicate issue labels')
    return sorted(labels)


def item_state(node):
    if (not isinstance(node, dict) or not isinstance(node.get('id'), str) or
            not isinstance(node.get('updatedAt'), str) or 'status' not in node):
        raise RuntimeError('GitHub returned an incomplete board item')
    status = node['status']
    if status is not None and (not isinstance(status, dict) or not isinstance(status.get('name'), str)
                               or not isinstance(status.get('optionId'), str)):
        raise RuntimeError('GitHub omitted the item Status value')
    return {'id': node['id'], 'status': status and status['name'], 'updatedAt': node['updatedAt']}


def board_items(pid):
    items, seen, total, cursor = [], set(), None, None
    while True:
        conn = node_query(ITEMS_Q, {'id': pid, 'after': cursor}).get('items')
        nodes, total, cursor = connection_page(conn, seen, total)
        for node in nodes:
            item = item_state(node)
            content = node.get('content')
            types = {'ISSUE': 'Issue', 'PULL_REQUEST': 'PullRequest', 'DRAFT_ISSUE': 'DraftIssue', 'REDACTED': None}
            kind = node.get('type')
            if kind not in types or 'content' not in node or (
                    kind == 'REDACTED' and content is not None) or (
                    kind != 'REDACTED' and (not isinstance(content, dict) or content.get('__typename') != types[kind])):
                raise RuntimeError('GitHub omitted the board item type or content; cannot safely migrate its labels')
            if isinstance(content, dict) and content.get('__typename') == 'Issue':
                repo = (content.get('repository') or {}).get('nameWithOwner')
                if not content.get('id') or type(content.get('number')) is not int or not repo:
                    raise RuntimeError('GitHub omitted the issue identity')
                item['issue'] = {'id': content['id'], 'number': content['number'], 'repo': repo,
                                 'labels': issue_labels(content['id'], content.get('labels'))}
            items.append(item)
        if cursor is None:
            break
        seen.add(cursor)
    if len(items) != total or len({it['id'] for it in items}) != total:
        raise RuntimeError('GitHub returned incomplete or duplicate board items')
    return items


def field_options(fid, label='Status'):
    opts = node_query(OPTIONS_Q, {'id': fid}).get('options')
    if not isinstance(opts, list) or not opts or any(
            not isinstance(o, dict) or any(not isinstance(o.get(k), str) for k in ('id', 'name', 'color', 'description'))
            for o in opts):
        raise RuntimeError(f'GitHub returned incomplete {label} options')
    if len({o['name'].lower() for o in opts}) != len(opts) or len({o['id'] for o in opts}) != len(opts):
        raise RuntimeError(f'GitHub returned ambiguous {label} options')
    return opts


def option_values(opts):
    return [{k: o[k] for k in ('name', 'color', 'description')} for o in opts]


def recovery_digest(saved):
    payload = {k: v for k, v in saved.items() if k != 'checksum'}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def save_recovery(path, saved):
    saved['checksum'] = recovery_digest(saved)
    durable_json(path, saved)


def durable_json(path, data):
    """Commit and read back each checkpoint before allowing the next remote write."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    temp = path + '.tmp'
    with open(temp, 'w') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    if os.name != 'nt':
        fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    if load_json(path) != data:
        raise RuntimeError('Could not verify the saved upgrade recovery record')


@contextlib.contextmanager
def migration_lock(path):
    """OS-released lock: a crashed upgrade never leaves a stale lock blocking recovery."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    # Upgrades of existing installations need this before onboard reaches its ignore-file step.
    try:
        with open(os.path.join(directory, '.gitignore'), 'x') as ignore:
            ignore.write('*\n')
    except FileExistsError:
        pass
    with open(path + '.lock', 'a+b') as stream:
        try:
            if os.name == 'nt':
                import msvcrt
                stream.write(b'\0'); stream.flush(); stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('Another upgrade is using this board recovery record') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def board_migrate(owner, number, repo, qa_all=False, dry=False, prune_empty=False, root=None, config_path=None):
    """Save first, reconcile each write, resume an interrupted upgrade without re-snapshotting."""
    proj = gh_json('project', 'view', str(number), '--owner', owner, '--format', 'json')
    if not isinstance(proj, dict) or not isinstance(proj.get('id'), str) or not proj.get('url'):
        raise RuntimeError('GitHub omitted the project identity')
    pid = proj['id']
    path = os.path.join(root or os.getcwd(), '.claude', 'super-board', 'migrations',
                        hashlib.sha256(pid.encode()).hexdigest()[:24] + '.json')
    binding = {'project': pid, 'owner': owner, 'number': number, 'repo': repo}
    if dry:
        return prepare_migration(binding, proj['url'], qa_all, prune_empty)['result']
    try:
        with migration_lock(path):
            saved = load_json(path)
            if os.path.exists(path) and not isinstance(saved, dict):
                raise RuntimeError('Recovery record is unreadable; restore it from backup before retrying')
            if os.path.exists(path) and saved.get('checksum') != recovery_digest(saved):
                raise RuntimeError('Recovery record is damaged; restore it from backup before retrying')
            if saved and not saved.get('complete'):
                if saved.get('version') != 1 or saved.get('binding') != binding:
                    raise RuntimeError('Recovery record does not match this board/repository')
            else:
                saved = prepare_migration(binding, proj['url'], qa_all, prune_empty)
                save_recovery(path, saved)  # no GitHub mutation has occurred
            saved['result']['recovery_file'] = path
            resume_migration(saved, path)
            if config_path:
                current = load_json(config_path)
                if not isinstance(current, dict):
                    raise RuntimeError('Config is unreadable; keeping the upgrade pending')
                configured = current.get('project') or {}
                if configured.get('owner') != owner or int(configured.get('number', 0)) != number:
                    raise RuntimeError('Board config changed during upgrade; keeping the recovery record')
                if current.pop('_qa_all', None) is not None:
                    durable_json(os.path.abspath(config_path), current)
            saved['complete'] = True
            save_recovery(path, saved)
            return saved['result']
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise RuntimeError(f'{exc}. Upgrade incomplete; keep {path}, fix the error and re-run the same board-migrate command. '
                           'Do not run the board until recovery succeeds.') from exc


def prepare_migration(binding, url, qa_all, prune_empty):
    field = status_field(binding['owner'], binding['number'])
    if not field or not field.get('id'):
        raise RuntimeError('project has no Status field')
    opts = field_options(field['id'])
    items = board_items(binding['project'])
    names = {o['name'].lower(): o for o in opts}
    in_use = {it['status'] for it in items}
    if any(s and s.lower() not in names for s in in_use):
        raise RuntimeError('Board Status values changed while taking the snapshot')
    desired = []
    for c in COLUMNS:
        desired.append(option_values([names[c.lower()]])[0] if c.lower() in names else
                       {'name': c, 'color': COLORS[c], 'description': ''})
    desired += option_values([o for o in opts if o['name'].lower() not in {c.lower() for c in COLUMNS}
                              and o['name'].lower() != 'skipped' and (not prune_empty or o['name'] in in_use)])
    result = {'url': url, 'added_columns': [c for c in COLUMNS if c.lower() not in names],
              'labels_created': [], 'labels_mapped': 0, 'qa_labelled': 0,
              'skipped_moved': 0, 'skipped_removed': 'skipped' in names, 'restored': 0,
              'preserved_edits': [], 'removed_items': [], 'status_backup': None}
    for item in items:
        item['want'] = 'Done' if (item['status'] or '').lower() == 'skipped' else item['status']
        result['skipped_moved'] += int(item['want'] != item['status'])
        issue = item.get('issue')
        if not issue:
            if qa_all and item['status'] not in ('Done', 'Skipped'):
                raise RuntimeError('QA-only conversion needs an accessible issue for every active card')
            continue
        before = issue['labels']
        after = set(before)
        for label in before:
            if label.lower() in OLD_LABELS:
                after.discard(label)
                after.add(OLD_LABELS[label.lower()])
                result['labels_mapped'] += 1
        if qa_all and item['status'] not in ('Done', 'Skipped') and not {n.lower() for n in after} & set(LABELS):
            after.add('qa')
            result['qa_labelled'] += 1
        issue['want'] = sorted(after)
        if before != issue['want'] and issue['repo'].lower() != binding['repo'].lower():
            raise RuntimeError('Board has an issue from another repository needing label conversion; migrate it in its own repository first')
    labels = repository_labels(binding['repo'])
    result['labels_created'] = [n for n in LABELS if n not in {s.lower() for s in labels}]
    return {'version': 1, 'qa_all': qa_all, 'prune_empty': prune_empty, 'binding': binding, 'field': field['id'], 'options': opts, 'desired': desired,
            'items': items, 'result': result, 'options_started': False, 'options_done': False,
            'labels_done': [], 'statuses_done': [], 'complete': False}


def repository_labels(repo):
    # --slurp yields every page separately, so a capped CLI list cannot hide a label.
    pages = gh_json('api', f'repos/{repo}/labels?per_page=100', '--paginate', '--slurp')
    if not isinstance(pages, list) or not pages or any(not isinstance(p, list) for p in pages):
        raise RuntimeError('GitHub returned incomplete repository labels')
    labels = [label for page in pages for label in page]
    if any(not isinstance(l, dict) or not isinstance(l.get('name'), str) for l in labels):
        raise RuntimeError('GitHub omitted a repository label')
    return [l['name'] for l in labels]


def resume_migration(saved, path):
    repo, pid, fid = saved['binding']['repo'], saved['binding']['project'], saved['field']
    checkpoint = lambda: save_recovery(path, saved)
    # Reconcile first; a lost response never causes a blind create/edit replay.
    for name, (color, desc) in LABELS.items():
        if name not in {s.lower() for s in repository_labels(repo)}:
            run(['gh', 'label', 'create', name, '--repo', repo, '--color', color, '--description', desc])
            if name not in {s.lower() for s in repository_labels(repo)}:
                raise RuntimeError(f'Could not verify creation of label {name}')
    for item in saved['items']:
        issue = item.get('issue')
        if not issue or issue['labels'] == issue['want']:
            continue
        current = issue_labels(issue['id'])
        if item['id'] in saved['labels_done']:
            if current != issue['want']:
                raise RuntimeError('Issue labels changed after conversion; review the saved recovery record')
            continue
        if current == issue['want']:
            saved['labels_done'].append(item['id']); checkpoint(); continue
        if current != issue['labels']:
            raise RuntimeError('Issue labels changed during upgrade; preserving the edit and stopping')
        checkpoint()  # persist the original and desired labels before the mutation
        args = ['gh', 'issue', 'edit', str(issue['number']), '--repo', issue['repo']]
        added, removed = set(issue['want']) - set(current), set(current) - set(issue['want'])
        if added:
            args += ['--add-label', ','.join(sorted(added))]
        if removed:
            args += ['--remove-label', ','.join(sorted(removed))]
        run(args)
        if issue_labels(issue['id']) != issue['want']:
            raise RuntimeError('Could not verify issue label conversion')
        saved['labels_done'].append(item['id']); checkpoint()

    opts = field_options(fid)
    rewrite = option_values(saved['options']) != saved['desired']
    if not saved['options_done']:
        if saved['options_started'] and option_values(opts) == saved['desired']:
            saved['options_done'] = True; checkpoint()
        else:
            if opts != saved['options']:
                raise RuntimeError('Status options changed during upgrade; preserving the edit and stopping')
            now = board_items(pid)
            signature = lambda items: sorted((i['id'], i['status'], i['updatedAt']) for i in items)
            if signature(now) != signature(saved['items']):
                raise RuntimeError('Board cards changed before the Status rewrite; preserving edits and stopping')
            if rewrite:
                if not saved['result'].get('status_backup'):
                    # Keep the original manual backup alongside the resumable recovery record.
                    directory = os.path.join(os.path.dirname(os.path.dirname(path)), 'backup')
                    backup = os.path.join(directory, f"board-{saved['binding']['number']}-{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json")
                    durable_json(backup, {item['id']: item['status'] for item in saved['items']})
                    saved['result']['status_backup'] = backup
                saved['options_started'] = True; checkpoint()
                try:
                    gql(UPDATE_M, {'id': fid, 'opts': saved['desired']})
                except (RuntimeError, ValueError):
                    pass  # the server may have applied it; only read-back settles this
                opts = field_options(fid)
                if option_values(opts) != saved['desired']:
                    raise RuntimeError('Could not verify the Status options rewrite')
            saved['options_done'] = True; checkpoint()
    if option_values(opts) != saved['desired']:
        raise RuntimeError('Status options changed after the rewrite; recovery will not overwrite them')
    ids = {o['name'].lower(): o['id'] for o in opts}
    # One complete read also distinguishes removed cards from unavailable partial responses.
    now = {it['id']: it for it in board_items(pid)}
    for item in saved['items']:
        key, want = item['id'], item['want']
        if key in saved['statuses_done']:
            continue  # later user edits, including clearing Status, belong to the user
        if key not in now:
            saved['result']['removed_items'].append(key)
            saved['statuses_done'].append(key); checkpoint(); continue
        current = item_state(node_query(ITEM_Q, {'id': key}))
        if current['status'] == want or want is None:
            saved['statuses_done'].append(key); checkpoint(); continue
        if current['status'] is not None and not (current['status'] == item['status'] and (current['status'] or '').lower() == 'skipped'):
            saved['result']['preserved_edits'].append(key)
            saved['statuses_done'].append(key); checkpoint(); continue
        pending = saved.get('pending_status')
        if pending and pending['id'] == key and current != pending:
            raise RuntimeError('An unfinished status restore conflicts with a newer edit; review it before resuming')
        saved['pending_status'] = current; checkpoint()
        run(['gh', 'project', 'item-edit', '--id', key, '--project-id', pid, '--field-id', fid,
             '--single-select-option-id', ids[want.lower()]])
        if item_state(node_query(ITEM_Q, {'id': key}))['status'] != want:
            raise RuntimeError('Could not verify a restored task status')
        saved['result']['restored'] += 1
        saved['statuses_done'].append(key)
        saved.pop('pending_status', None); checkpoint()
    # The upgrade is not complete until labels and columns still read back correctly.
    if option_values(field_options(fid)) != saved['desired']:
        raise RuntimeError('Status options changed before upgrade completion')
    for item in saved['items']:
        issue = item.get('issue')
        if issue and issue['labels'] != issue['want'] and issue_labels(issue['id']) != issue['want']:
            raise RuntimeError('Issue labels changed before upgrade completion')


# ── GitHub board: the Agent / Skills / Card type fields ────────────────────────

AGENT_OPTIONS = [
    ("Haiku · super-build", "YELLOW", "Build lane, Haiku"),
    ("Sonnet · super-build", "ORANGE", "Build lane, Sonnet"),
    ("Opus · super-build", "RED", "Build lane, Opus"),
    ("Haiku · super-qa", "PINK", "QA lane, Haiku"),
    ("Sonnet · super-qa", "PURPLE", "QA lane, Sonnet"),
    ("Opus · super-review", "BLUE", "Review lane, Opus"),
    ("Sonnet · super-review", "GRAY", "Review lane, Sonnet"),
    ("Done · Opus · super-review", "GREEN", "Finished by Review, Opus"),
]
CARD_TYPES = ["feature", "bug", "docs", "money", "refactor", "tests", "ux", "qa"]
BOARD_FIELDS = [
    {"name": "Agent", "type": "SINGLE_SELECT", "options": AGENT_OPTIONS},
    {"name": "Skills", "type": "TEXT"},
    {"name": "Card type", "type": "SINGLE_SELECT", "options": [(t, "GRAY", "") for t in CARD_TYPES]},
]
FIELDS_VIEW_STEP = ("One manual step (GitHub's API cannot change which fields a board view shows): open the board, "
                    "View menu ▾ → Fields → tick Agent, Skills, Card type.")
CREATE_FIELD_M = ("mutation($pid:ID!,$name:String!,$opts:[ProjectV2SingleSelectFieldOptionInput!]!){createProjectV2Field("
                  "input:{projectId:$pid,dataType:SINGLE_SELECT,name:$name,singleSelectOptions:$opts}){projectV2Field{"
                  "... on ProjectV2SingleSelectField{id name}}}}")


AGENT_MODELS = ("Haiku", "Sonnet", "Opus", "Codex")
AGENT_LANES = ("build", "qa", "review", "lint")
AGENT_MATRIX = [f"{pre}{m} · super-{lane}" for pre in ("", "Done · ") for m in AGENT_MODELS for lane in AGENT_LANES]


def create_options(spec):
    """Options for a NEW single-select field. A new Agent field gets the whole matrix (no card values exist
    yet, so nothing can be wiped); an existing field is never rewritten."""
    if spec["name"] != "Agent":
        return spec["options"]
    known = {n: (c, d) for n, c, d in spec["options"]}
    return [(n, *known.get(n, ("GRAY", ""))) for n in AGENT_MATRIX] +         [(n, c, d) for n, c, d in spec["options"] if n not in AGENT_MATRIX]


def agent_matrix_missing(field):
    """Names of the 32 matrix Agent options (<Model> · super-<lane>, 4 models x 4 lanes, plus the same with
    `Done · `) the Agent field lacks. Pure: reads the already-fetched field payload; creating options is never done here."""
    have_ = {str(o.get("name", "")).lower() for o in (field or {}).get("options") or [] if isinstance(o, dict)}
    return [n for n in AGENT_MATRIX if n.lower() not in have_]


def agent_matrix_step(missing):
    return ("Add these Agent options in the board UI (Agent field → Edit; the API would wipe values): "
            + ", ".join(missing))


def _is_select(f):
    t = str(f.get("type") or "").replace("_", "").lower()
    return "singleselect" in t


def fields_state(owner, number):
    """What the board has of BOARD_FIELDS: {"missing": [names], "options_missing": {name: [options]},
    "wrong_type": [names], "project": node id}. Read-only: project view + field-list."""
    proj = gh_json("project", "view", str(number), "--owner", owner, "--format", "json")
    pid = proj.get("id") if isinstance(proj, dict) else None
    if not isinstance(pid, str) or not pid:
        raise RuntimeError("GitHub omitted the project identity")
    fields = gh_json("project", "field-list", str(number), "--owner", owner, "--format", "json",
                     "--limit", "100").get("fields", [])
    by = {str(f.get("name", "")).lower(): f for f in fields if isinstance(f, dict)}
    state = {"missing": [], "options_missing": {}, "wrong_type": [], "project": pid, "_ids": {},
             "agent_matrix_missing": []}
    for spec in BOARD_FIELDS:
        f = by.get(spec["name"].lower())
        if f is None:
            state["missing"].append(spec["name"])
        elif (spec["type"] == "SINGLE_SELECT") != _is_select(f):
            state["wrong_type"].append(spec["name"])
        elif spec["type"] == "SINGLE_SELECT":
            have_ = {str(o.get("name", "")).lower() for o in f.get("options") or [] if isinstance(o, dict)}
            lacking = [o[0] for o in spec["options"] if o[0].lower() not in have_]
            if spec["name"] == "Agent":
                state["agent_matrix_missing"] = agent_matrix_missing(f)
            if lacking:
                state["options_missing"][spec["name"]] = lacking
                state["_ids"][spec["name"]] = f.get("id")
    return state


def option_step(name, options):
    return "; ".join(f"add option {o} in the board UI: {name} field ▾ → Edit" for o in options)


def fields_red(state):
    return state["missing"] + [f"{n} (missing option(s): {', '.join(o)})" for n, o in state["options_missing"].items()] +         [f"{n} (exists with another type)" for n in state["wrong_type"]]


def ensure_fields(owner, number, dry=False):
    """Create the Agent / Skills / Card type fields that are missing. Only ever CREATES: an existing field is
    never mutated (rewriting a single-select's options regenerates option ids and clears card values). An
    existing field that lacks options, or has the wrong type, is reported in `problems` with the manual step.
    Nothing is deleted. Returns what was (or with dry=True would be) done."""
    st = fields_state(owner, number)
    res = {"owner": owner, "number": number, "created": [], "options_missing": st["options_missing"],
           "problems": [], "wrong_type": st["wrong_type"], "dry_run": dry, "manual_step": FIELDS_VIEW_STEP}
    for spec in BOARD_FIELDS:
        name = spec["name"]
        if name in st["missing"]:
            if not dry:
                if spec["type"] == "TEXT":
                    run(["gh", "project", "field-create", str(number), "--owner", owner, "--name", name,
                         "--data-type", "TEXT"], check=True)
                else:
                    gh_json("api", "graphql", "--input", "-", input=json.dumps({
                        "query": CREATE_FIELD_M, "variables": {"pid": st["project"], "name": name, "opts": [
                            {"name": n, "color": c, "description": d} for n, c, d in create_options(spec)]}}))
            res["created"].append(name)
        elif name in st["options_missing"]:
            res["problems"].append(f"{name}: " + option_step(name, st["options_missing"][name]))
    for name in st["wrong_type"]:
        res["problems"].append(f"{name} exists with another type; rename or delete it on the board, then re-run")
    return res


def board_bindings(root):
    """[(config path relative to root, owner, number)] for every board config that names a GitHub Project."""
    found = []
    for p in sorted(glob.glob(os.path.join(root, ".claude", "super-board", "configs", "*.json"))):
        cfg = load_json(p)
        proj = (cfg.get("project") if isinstance(cfg, dict) else None) or {}
        if not isinstance(proj, dict):
            continue
        try:
            number = int(proj.get("number"))
        except (TypeError, ValueError):
            continue
        if isinstance(proj.get("owner"), str) and proj["owner"]:
            found.append((os.path.relpath(p, root), proj["owner"], number))
    return found


def board_fields_report(root):
    """({config: {...missing}} for boards lacking fields, [manual steps], {config: error}). Read-only.
    Without gh nothing is read (the `needs` list already says gh is missing)."""
    red, errors, complete, matrix = {}, {}, False, []
    if not have("gh"):
        return red, [], errors
    for rel, owner, number in board_bindings(root):
        try:
            st = fields_state(owner, number)
        except (RuntimeError, ValueError, OSError) as exc:
            errors[rel] = str(exc)[:200]
            continue
        matrix += [n for n in st.get("agent_matrix_missing", []) if n not in matrix]
        lacking = fields_red(st)
        if lacking:
            red[rel] = {"owner": owner, "number": number, "missing": lacking}
        else:
            complete = True
    steps = ([FIELDS_VIEW_STEP] if complete else []) + ([agent_matrix_step(matrix)] if matrix else [])
    return red, steps, errors


# ── CLI ────────────────────────────────────────────────────────────────────────

def flag(argv, name, default=None):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 64
    cmd, rest = argv[0], argv[1:]
    root = os.path.abspath(flag(rest, "--root", os.getcwd()))
    dry = "--dry-run" in rest
    try:
        if cmd in ("check", "fix"):
            pack = find_pack(flag(rest, "--pack"))
            host, home, catalog = flag(rest, "--host", "claude"), flag(rest, "--home", os.environ.get("SUPER_BOARD_SKILLS_HOME")), flag(rest, "--catalog", os.environ.get("SUPER_BOARD_SKILLS_CATALOG"))
            if host not in ("claude", "codex"):
                raise ValueError("--host must be claude or codex")
            res = check(root, pack, host, home, catalog) if cmd == "check" else fix(root, pack, "--no-helpers" not in rest, dry, host, home, catalog)
            if res.get("error") == "pack-not-found":
                print(json.dumps(res, indent=1))
                return 66
            if "--text" in rest:
                print(text_report(res))
                return 0 if res["green"] else 1
            return out(res, 0 if res["green"] else 1)
        if cmd == "migrate-config":
            path = rest[0] if rest and not rest[0].startswith("-") else None
            if not path:
                return 64
            cur = load_json(path) or {}
            approver = flag(rest, "--approver")
            if "--interactive" in rest and not approver and sys.stdin.isatty():
                _, probe = migrate_config(cur)
                if any(c.startswith("merge_policy.trusted_approvers") for c in probe):
                    owner = repo_owner(cur) or ""
                    org = any(c.startswith(NO_APPROVER) for c in probe)
                    ans = input("Who may approve this board's merges (GitHub login of a person)? "
                                + ("" if org else f"[{owner}] ")).strip()
                    approver = ans or (None if org else owner) or None
            new, changes = migrate_config(cur, approver)
            if not dry and changes:
                write_json(path, new)
            needs = any(c.startswith(NO_APPROVER) for c in changes)
            return out({"config": path, "changes": changes, "written": bool(changes and not dry),
                        "needs_approver": needs})
        if cmd == "sync-guard-env":
            return out({"root": root, "changed": guard_env_sync(root, dry), "written": not dry})
        if cmd == "sync-agents-md":
            return out({"root": root, "changed": agents_sync(root, dry), "written": not dry})
        if cmd == "names":
            return out(names(root))
        if cmd == "branch":
            return out(branch(root))
        if cmd == "board-rank":
            owner = flag(rest, "--owner")
            if not owner:
                return 64
            return out(board_rank(owner))
        if cmd == "ensure-fields":
            cfg_path = flag(rest, "--config")
            cfg = load_json(cfg_path) if cfg_path else {}
            proj = (cfg.get("project") if isinstance(cfg, dict) else None) or {}
            owner = flag(rest, "--owner") or proj.get("owner")
            number = flag(rest, "--number") or proj.get("number")
            if not (owner and number):
                return 64
            return out(ensure_fields(owner, int(number), dry))
        if cmd == "board-migrate":
            # --config fills owner / number / repo and the qa-all flag an upgraded
            # "qa-only" config left behind (`_qa_all`, removed once applied).
            cfg_path = flag(rest, "--config")
            if cfg_path and "--root" not in rest:
                config_dir = os.path.dirname(os.path.abspath(cfg_path))
                suffix = os.path.join('.claude', 'super-board', 'configs')
                root = config_dir[:-len(suffix)].rstrip(os.sep) if config_dir.endswith(os.sep + suffix) else config_dir
            cfg = load_json(cfg_path) if cfg_path else {}
            cfg = cfg if isinstance(cfg, dict) else {}
            proj = cfg.get("project") or {}
            m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", ((cfg.get("repo") or {}).get("remote") or ""))
            owner = flag(rest, "--owner") or proj.get("owner")
            number = flag(rest, "--number") or proj.get("number")
            repo = flag(rest, "--repo") or (m.group(1) if m else None)
            if not (owner and number and repo):
                return 64
            qa_all = "--qa-all" in rest or bool(cfg.get("_qa_all"))
            res = board_migrate(owner, int(number), repo, qa_all, dry, "--prune-empty" in rest, root, cfg_path)
            return out(res)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:
        return out({"error": str(e)}, 2)
    print(__doc__, file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
