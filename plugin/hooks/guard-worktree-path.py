#!/usr/bin/env python3
"""PreToolUse(Bash|PowerShell) guard: a git worktree may only live under <repo>/.claude/worktrees/.

A worktree created next to the repo (`git worktree add ../proj-feature`) sits outside
every sweep -- cleanup-wt, Claude Code's own -- long after its PR merges. Claude
Code's docs show that very pattern, so it keeps coming back.

Reads the Bash command, finds every `git worktree add` / `git worktree move`,
resolves its target (honouring `cd X &&` and `git -C X`) and denies the call when the
target is outside `.claude/worktrees/` of the repo git will run in. Its own errors
never block a tool call: anything unexpected exits 0 with no output.

Adapted from an earlier internal guard hook (JavaScript), ported to Python.
"""
import json
import os
import posixpath
import re
import subprocess
import sys

ALLOWED_DIR = os.path.join(".claude", "worktrees")
ADD_VALUE_OPTS = {"-b", "-B", "--reason"}
GIT_VALUE_OPTS = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}


def segments(command, ps=False):
    """Split a shell command into simple-command token lists. Quotes are honoured; nothing runs.
    ps=True: PowerShell rules (backtick escapes, backslash is an ordinary path character)."""
    esc = "`" if ps else "\\"
    out, tokens, cur, has, quote = [], [], "", False, None
    i = 0

    def push_token():
        nonlocal cur, has
        if has:
            tokens.append(cur)
        cur, has = "", False

    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            elif c == esc and quote == '"' and i + 1 < len(command):
                i += 1
                cur += command[i]
            else:
                cur += c
        elif c in "'\"":
            quote, has = c, True
        elif c == esc and i + 1 < len(command):
            i += 1
            cur += command[i]
            has = True
        elif c in " \t":
            push_token()
        elif c in ";\n|&()":
            push_token()
            if tokens:
                out.append(tokens)
            tokens = []
        else:
            cur += c
            has = True
        i += 1
    push_token()
    if tokens:
        out.append(tokens)
    return out


def expand(word, env):
    """Expand ~, $VAR, ${VAR}. None when a variable is unknown or a substitution is present."""
    s = re.sub(r"^~(?=/|$)", lambda m: env.get("HOME", "~"), word)
    unknown = []

    def var(m):
        name = m.group(1) or m.group(2)
        if name not in env:
            unknown.append(name)
        return env.get(name, "")

    s = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", var, s)
    if unknown or "$(" in s or "`" in s:
        return None
    return s


def msys(p):
    """Git Bash paths (/c/Users/x) -> C:/Users/x on Windows, so os.path sees the real location."""
    if os.name != "nt" or not p.startswith("/"):
        return p
    p = posixpath.normpath(p)  # "/.." is "/", "/c/../d/x" is "/d/x", as in Git Bash
    m = re.match(r"^/([A-Za-z])(?:/(.*))?$", p)
    return "%s:/%s" % (m.group(1), m.group(2) or "") if m else p


def worktree_targets(command, cwd, env, ps=False):
    """[(raw, git_dir, resolved_or_None)] for every worktree add/move target."""
    found, d = [], cwd
    for tokens in segments(command, ps):
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            continue
        cmd, args = tokens[0], tokens[1:]
        if cmd.lower() in ("cd", "chdir", "set-location", "sl", "push-location", "pushd"):
            target = expand(args[0], env) if args else env.get("HOME")
            if target:
                d = os.path.normpath(os.path.join(d, msys(target)))
            continue
        if re.sub(r"\.exe$", "", os.path.basename(cmd.replace("\\", "/")).lower()) != "git":
            continue
        git_dir, i = d, 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-C":
                target = expand(args[i + 1] if i + 1 < len(args) else "", env)
                if target:
                    git_dir = os.path.normpath(os.path.join(git_dir, msys(target)))
                i += 2
            elif args[i] in GIT_VALUE_OPTS:
                i += 2
            else:
                i += 1
        if i >= len(args) or args[i] != "worktree" or i + 1 >= len(args):
            continue
        sub = args[i + 1]
        if sub not in ("add", "move"):
            continue
        positional, j = [], i + 2
        while j < len(args):
            a = args[j]
            if a == "--":
                positional += args[j + 1:]
                break
            if a in ADD_VALUE_OPTS:
                j += 1
            elif not a.startswith("-") and not re.match(r"^\d*[<>]", a):
                positional.append(a)
            j += 1
        idx = 0 if sub == "add" else 1
        if len(positional) <= idx:
            continue
        raw = positional[idx]
        ex = expand(raw, env)
        found.append((raw, git_dir, None if ex is None else os.path.normpath(os.path.join(git_dir, msys(ex)))))
    return found


def fold(p):
    """Comparison key: git prints forward slashes, os.path.normpath yields backslashes on Windows."""
    return os.path.normcase(os.path.normpath(p))


def repo_root(toplevel):
    """The main checkout's root, from it or any worktree under .claude/worktrees/."""
    toplevel = os.path.normpath(toplevel)
    marker = os.sep + ALLOWED_DIR + os.sep
    at = os.path.normcase(toplevel).find(os.path.normcase(marker))
    return toplevel if at == -1 else toplevel[:at]


def check(command, cwd, root_of, env, ps=False):
    """None when allowed, else the deny reason. root_of(dir) -> repo root or None."""
    if not re.search(r"\bworktree\b", command):
        return None
    for raw, git_dir, resolved in worktree_targets(command, cwd, env, ps):
        repo = root_of(git_dir) or root_of(cwd)
        if not repo:
            continue  # not a repository: git itself will refuse
        allowed = os.path.join(os.path.normpath(repo), ALLOWED_DIR)
        if resolved and fold(resolved).startswith(fold(allowed) + os.sep):
            continue
        where = ("resolves to " + resolved) if resolved else "cannot be resolved before it runs"
        return (
            'Blocked: worktree path "%s" %s, outside %s/.\n'
            "Worktrees live only under .claude/worktrees/ so cleanup-wt and Claude Code's sweep can see them.\n"
            'Prefer the Agent tool\'s isolation: "worktree" or `claude --worktree <name>`. If you need git directly:\n'
            "  git worktree add .claude/worktrees/<name> -b <branch> <base>\n"
            "(from the repo root, with a literal path)." % (raw, where, allowed)
        )
    return None


def toplevel_of(d):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, env=env, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = payload.get("tool_name")
        command = (payload.get("tool_input") or {}).get("command")
        if tool not in ("Bash", "PowerShell") or not isinstance(command, str):
            return
        cwd = msys(payload.get("cwd") or os.getcwd())

        def root_of(d):
            top = toplevel_of(d) or (os.environ.get("CLAUDE_PROJECT_DIR") if d == cwd else None)
            return repo_root(top) if top else None

        reason = check(command, cwd, root_of, dict(os.environ), tool == "PowerShell")
        if reason:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
    except Exception:
        pass  # a broken guard must never block unrelated work


if __name__ == "__main__":
    main()
