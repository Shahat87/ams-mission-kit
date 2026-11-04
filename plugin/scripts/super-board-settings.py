#!/usr/bin/env python3
"""super-board-settings.py — merge into .claude/settings.json without losing anything.

    super-board-settings.py hooks <settings.json> <snippet.json> [...] [--dry-run]
    super-board-settings.py allow <settings.json> <rule> [<rule> ...]  [--dry-run]
    super-board-settings.py user-hooks <settings.json> --hooks-dir <dir> [--dry-run]

`hooks` adds each hook command from the snippets once per event + matcher
(install.sh and onboard's protect-main step); a snippet command left under an
older matcher is removed first (`- <line>` in --dry-run), user hooks never. So is a plugin-shaped LEGACY
command for a hook the snippets now launch through sb-python.sh: `python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/<name>.py`,
the `python` variant, and `cd "$CLAUDE_PROJECT_DIR" && python3 .claude/hooks/<name>.py --auto --fetch 2>/dev/null || true`
(exact matches, any event and matcher; user hooks, other paths and unknown shapes stay). `user-hooks` merges the
user-scope guards that `install.sh --guard-repo` wires (guard-external-tools.py on PreToolUse + SessionStart,
guard-config-write.py --user-scope on PreToolUse), each as `bash "<hooks-dir>/sb-python.sh" <hook>.py [args]`, built
in memory, with the same once-per-event+matcher, backup and invalid-JSON rules as `hooks`. Any earlier command that
names `/.mission-control/hooks/<same hook file>` and is not identical to the new one (the old pinned-path form) is
removed first, so a re-pin replaces and never duplicates; user hooks are never touched. `allow` adds permission rules to
`permissions.allow` once each (onboard's Permissions step, after the user
approved the printed diff). Both keep every existing key, back the file up
(`settings.json.bak-<ts>`) before writing, write atomically, and leave an
invalid settings.json untouched. `--dry-run` prints what would be added
(`+ <line>`) and writes nothing.

Exit: 0 ok (including "already present"), 2 settings.json invalid, 64 usage.
"""
from __future__ import annotations

import json
import re
import os
import shutil
import sys
import time


def load(path: str):
    if not os.path.exists(path):
        return {}
    try:
        data = json.load(open(path))
    except ValueError as e:
        print(f"    ✗ {path} is not valid JSON ({e}); left untouched — merge by hand", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print(f"    ✗ {path} is not a JSON object; left untouched", file=sys.stderr)
        sys.exit(2)
    return data


def save(path: str, data: dict) -> None:
    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"    ✓ backup: {backup}")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _tools(matcher) -> set | None:
    """Tool set of a plain matcher ('Bash|PowerShell'); None for '', '*', '.*' or a regex."""
    if not isinstance(matcher, str) or not matcher or not re.fullmatch(r"[A-Za-z0-9_]+(\|[A-Za-z0-9_]+)*", matcher):
        return None
    return set(matcher.split("|"))


def drop_stale(hooks: dict, snippets: list[dict]) -> list[str]:
    """Remove a plugin command from an entry only when the entry's matcher is a strict subset
    of the tool set a snippet wants for that command (a widened matcher, Bash -> Bash|PowerShell).
    Everything else stays: '', '*', '.*', wider or disjoint sets, user hooks, malformed entries.
    Entries left without hooks are dropped."""
    wanted: dict[str, dict[str, list[set]]] = {}
    for s in snippets:
        for event, entries in s["hooks"].items():
            for entry in entries:
                for h in entry["hooks"]:
                    t = _tools(entry.get("matcher"))
                    if t is not None:
                        wanted.setdefault(event, {}).setdefault(h["command"], []).append(t)
    removed = []
    for event, cmds in wanted.items():
        current = hooks.get(event, [])
        if not isinstance(current, list):
            continue
        for e in list(current):
            if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
                continue
            have = _tools(e.get("matcher"))
            if have is None:
                continue
            kept = []
            for h in e["hooks"]:
                stale = (isinstance(h, dict) and any(have < t for t in cmds.get(h.get("command"), [])))
                if stale:
                    removed.append(f"{event} {e.get('matcher')}: {h['command']}")
                else:
                    kept.append(h)
            if len(kept) != len(e["hooks"]):
                if kept:
                    e["hooks"] = kept
                else:
                    current.remove(e)
    return removed


LAUNCHER_RE = re.compile(r"(?:^|\s)\S*sb-python\.sh\"?\s+([A-Za-z0-9_-]+\.py)(?:\s|$)")
LEGACY_SHAPES = (
    '{py} "$CLAUDE_PROJECT_DIR"/.claude/hooks/{name}',
    'cd "$CLAUDE_PROJECT_DIR" && {py} .claude/hooks/{name} --auto --fetch 2>/dev/null || true',
)


def legacy_commands(snippets: list[dict]) -> set:
    """Exact plugin-shaped python3 / python commands for every hook file a snippet launches through sb-python.sh."""
    names = set()
    for s in snippets:
        for entries in s["hooks"].values():
            for entry in entries:
                for h in entry["hooks"]:
                    m = LAUNCHER_RE.search(h.get("command", ""))
                    if m:
                        names.add(m.group(1))
    return {shape.format(py=py, name=name) for name in names for py in ("python3", "python") for shape in LEGACY_SHAPES}


def drop_legacy(hooks: dict, legacy: set) -> list[str]:
    """Remove legacy commands from every entry of every event; entries left without hooks are dropped."""
    removed = []
    for event, current in hooks.items():
        if not isinstance(current, list):
            continue
        for e in list(current):
            if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
                continue
            kept = []
            for h in e["hooks"]:
                if isinstance(h, dict) and h.get("command") in legacy:
                    removed.append(f"{event} {e.get('matcher') or '*'}: {h['command']}")
                else:
                    kept.append(h)
            if len(kept) != len(e["hooks"]):
                if kept:
                    e["hooks"] = kept
                else:
                    current.remove(e)
    return removed


def user_snippet(hooks_dir: str) -> dict:
    """The user-scope guards `install.sh --guard-repo` wires, through the user-scope launcher."""
    def cmd(hook, *args):
        launcher = f'bash "{hooks_dir.rstrip("/")}/sb-python.sh" {hook}'
        return {"type": "command", "command": " ".join([launcher, *args])}
    return {"hooks": {
        "PreToolUse": [
            {"matcher": "Bash|PowerShell|mcp__.*", "hooks": [cmd("guard-external-tools.py")]},
            {"matcher": "Edit|MultiEdit|Write|NotebookEdit|Bash|PowerShell",
             "hooks": [cmd("guard-config-write.py", "--user-scope")]},
        ],
        "SessionStart": [{"hooks": [cmd("guard-external-tools.py")]}],
    }}


def drop_old_user_hooks(hooks: dict, snippet: dict) -> list[str]:
    """Remove an earlier user-scope command for a hook file the snippet wires (it names
    /.mission-control/hooks/<file>) unless it is identical to a new command."""
    new = set()
    files = set()
    for entries in snippet["hooks"].values():
        for entry in entries:
            for h in entry["hooks"]:
                new.add(h["command"])
                m = LAUNCHER_RE.search(h["command"])
                if m:
                    files.add(m.group(1))
    pats = [re.compile(r"/\.mission-control/hooks/" + re.escape(name) + r"(?![A-Za-z0-9_.-])") for name in files]
    removed = []
    for event, current in hooks.items():
        if not isinstance(current, list):
            continue
        for e in list(current):
            if not isinstance(e, dict) or not isinstance(e.get("hooks"), list):
                continue
            kept = []
            for h in e["hooks"]:
                c = h.get("command") if isinstance(h, dict) else None
                if isinstance(c, str) and c not in new and any(p.search(c) for p in pats):
                    removed.append(f"{event} {e.get('matcher') or '*'}: {c}")
                else:
                    kept.append(h)
            if len(kept) != len(e["hooks"]):
                if kept:
                    e["hooks"] = kept
                else:
                    current.remove(e)
    return removed


def merge_hooks(settings: dict, snippets: list[dict]) -> tuple[list[str], list[str]]:
    hooks = settings.setdefault("hooks", {})
    removed = drop_legacy(hooks, legacy_commands(snippets)) + drop_stale(hooks, snippets)
    added = []
    for event, entries in [kv for s in snippets for kv in s["hooks"].items()]:
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            continue
        for entry in entries:
            matcher = entry.get("matcher")
            target = next((e for e in current if isinstance(e, dict) and e.get("matcher") == matcher
                           and isinstance(e.get("hooks"), list)), None)
            if target is None:
                target = {"matcher": matcher} if matcher is not None else {}
                target["hooks"] = []
                current.append(target)
            have = {h.get("command") for e in current if isinstance(e, dict) and e.get("matcher") == matcher
                    and isinstance(e.get("hooks"), list)
                    for h in e["hooks"] if isinstance(h, dict)}
            for h in entry["hooks"]:
                if h["command"] not in have:
                    target.setdefault("hooks", []).append(h)
                    have.add(h["command"])
                    added.append(f"{event} {matcher or '*'}: {h['command']}")
    return added, removed


def merge_allow(settings: dict, rules: list[str]) -> list[str]:
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    added = []
    for r in rules:
        if r not in allow:
            allow.append(r)
            added.append(r)
    return added


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    if len(args) < 3 or args[0] not in ("hooks", "allow", "user-hooks"):
        print(__doc__, file=sys.stderr)
        return 64
    verb, path, rest = args[0], args[1], args[2:]
    if verb == "user-hooks":
        opts = dict(zip(rest[0::2], rest[1::2]))
        if len(rest) != 2 or set(opts) != {"--hooks-dir"}:
            print(__doc__, file=sys.stderr)
            return 64
    settings = load(path)
    if verb == "hooks":
        added, removed = merge_hooks(settings, [json.load(open(p)) for p in rest])
        noun = "hook command(s)"
    elif verb == "user-hooks":
        snip = user_snippet(opts["--hooks-dir"])
        old = drop_old_user_hooks(settings.setdefault("hooks", {}), snip)
        added, removed = merge_hooks(settings, [snip])
        removed = old + removed
        noun = "hook command(s)"
    else:
        added, removed = merge_allow(settings, rest), []
        noun = "permission rule(s)"
    if not added and not removed:
        print("    ✓ already present — nothing to change")
        return 0
    if dry:
        for r in removed:
            print(f"- {r}")
        for a in added:
            print(f"+ {a}")
        return 0
    save(path, settings)
    if removed:
        print(f"    ✓ {len(removed)} stale {noun} replaced (old matcher removed)")
    print(f"    ✓ {len(added)} {noun} added")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
