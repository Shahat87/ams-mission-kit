#!/usr/bin/env python3
"""PreToolUse(Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell) guard: the session may not rewrite its own guards.

Every other guard reads files the agent could edit: the hooks themselves, the scripts the board runs, the
settings that wire the hooks and carry the robot's GH_CONFIG_DIR, the board configs. Writing any of them
from a session switches the controls off for the next call. This guard refuses writes to, inside the project:

    .claude/hooks/**   .claude/bin/**   .claude/workflows/**   .claude/super-board/configs/**   .claude/super-board/identity-halt
    .claude/settings*.json            (settings.json, settings.local.json)
    .claude/super-board/inflight/sandbox-*   .claude/super-board/lane-results/**   (lane-delegate state: only
        `python3 .claude/bin/super-board-lane-delegate.py ...` writes it, inside its own process, so no exception is needed;
        reading is allowed)
    .claude/super-board/events/**    (event-trigger state: the queue, the budget ledger and the dedup
                                       log; only `python3 .claude/bin/super-board-events.py ...` writes it,
                                       inside its own process, so no exception is needed; reading is allowed)
    .claude/super-board/active        (the active-board pointer: the board's own scripts write it in-process; a Write whose
                                       content is not a plain name, ACTIVE_POINTER_RE, is refused with its own reason)

and, at USER scope (G-4, spec §15.3), wherever the session runs and whatever CLAUDE_PROJECT_DIR is:

    <home>/.mission-control/**        (guarded-repo list, interpreter pin, user-scope hook copies)
    <home>/.claude/settings*.json     (where the user-scope guards are wired)

<home> is USERPROFILE or HOME. Also caught: `~`, `$HOME`, `${HOME}`, `$USERPROFILE`, `$env:USERPROFILE`, `$env:HOME`,
`%USERPROFILE%`, and the absolute Windows (C:, with either slash) and MSYS (`/c/Users/x`, `/mnt/c/...`) spellings of it.

GUARDRAIL: catches habit and mistakes; not a security boundary (spec §15.3).

Judged: the Write / Edit / MultiEdit / NotebookEdit file path, and in a Bash / PowerShell command a
redirect (`>`, `>>`), `tee`, `cp` / `mv` / `install` / `ln` / `rsync` / `dd of=` onto such a path,
`sed -i` / `perl -i`, `touch` / `truncate` / `rm` on it, and `Set-Content` / `Add-Content` / `Out-File` /
`Clear-Content` / `New-Item` / `Copy-Item` / `Move-Item` / `Rename-Item` / `Remove-Item` on it. A
`cd .claude/hooks` makes every later write in the same command count. Looks through nested shells
(`bash -c`, `pwsh -Command`, `cmd /c`, `eval`, `iex`) and denies `powershell|pwsh -e|-ec|-enc|-EncodedCommand`.
The ancestors `.claude`, `.claude/super-board` and `.claude/super-board/inflight` are protected against
delete / move (`rm`, `rmdir`, `unlink`, `rd`, `del`, `Remove-Item`, `mv`, `Move-Item`, ... with or without -r / -Recurse),
and a glob (`*`, `?`, `[`) in a delete / move target is matched, component by component and with the shell's
dot rule, against every protected name below those directories (`rm -rf .claude/*`, `.claude/super-board/conf*`,
`.claude/*/configs`, `cd .claude && rm -rf *` are all refused; `rm -rf *` at the project root is not, the shell's `*`
skips `.claude`). `find ... -delete|-exec rm` is refused when a start path is inside `.claude`, or is the project
root (or above it) and the expression never mentions `.claude` (exclude it: `-not -path './.claude/*'`).
Files under `.claude/worktrees/` are not protected (a worktree's copy is not the session's guards; a PR
that changes them is reviewed and merged through the gate).

ESCAPE: `SB_ALLOW_CONFIG_WRITE=1` in Claude Code's environment AT SESSION START turns this guard off for
that session. That is how the owner, or a sync PR run outside the lanes, updates these files. A session
cannot set it for itself: a hook sees the environment Claude Code was started with, not a later `export`.
The board's own scripts (`super-board-setup.py fix`, `install.sh`) write these files inside their own
process, where no hook looks, so onboarding and upgrades still work.

Fails CLOSED: any exception exits 2 with a message on stderr.
Hooks filter command text; the server-side rulesets are the real control, this is defense in depth.
Known residual bypasses: an interpreter writing the file from code (`python -c "open(...).write(...)"`), a
path assembled at run time, `git checkout`/`git apply` into the directory, an archive extracted over it.
"""
import fnmatch
import json
import os
import posixpath
import re
import sys

SHELL_TOOLS = ("Bash", "PowerShell")
FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
PROTECTED = re.compile(r"(?:^|/)\.claude/(?:hooks|bin|workflows|super-board/configs|super-board/identity-halt)(?:/|$)"
                       r"|(?:^|/)\.claude/settings[^/]*\.json$"
                       r"|(?:^|/)\.claude/super-board/inflight/sandbox-[^/]*(?:/|$)"
                       r"|(?:^|/)\.claude/super-board/lane-results(?:/|$)"
                       r"|(?:^|/)\.claude/super-board/events(?:/|$)"
                       r"|(?:^|/)\.claude/super-board/active$", re.IGNORECASE)
ACTIVE_PATH = re.compile(r"(?:^|/)\.claude/super-board/active$", re.IGNORECASE)
ACTIVE_POINTER_RE = re.compile(r"^[A-Za-z0-9._-]+$")
HOME_VAR = re.compile(r"^(?:~|\$(?:env:)?(?:HOME|USERPROFILE)|%(?:HOME|USERPROFILE)%)(?=/|$)", re.IGNORECASE)
HOME_BRACED = re.compile(r"\$\{((?:env:)?[A-Za-z_][A-Za-z0-9_]*)\}(?=[/\\\s\"'`;|&)]|$)")
_HOMES = []  # USERPROFILE / HOME of the session, set by judge()
_USER_SCOPE = [False]  # --user-scope: only the home rules (G-4), set by judge()
WORKTREE = re.compile(r"(?:^|/)\.claude/worktrees/", re.IGNORECASE)
COPY_LIKE = {"cp", "mv", "install", "ln", "rsync", "copy", "copy-item", "cpi", "move-item", "mi", "move"}
ANY_ARG = {"touch", "truncate", "rm", "rmdir", "unlink", "tee", "set-content", "add-content", "out-file",
           "clear-content", "new-item", "remove-item", "ri", "del", "erase", "rd", "rename-item", "rni", "ren",
           "sc", "ac", "ni", "set-itemproperty", "chmod", "chown"}
INPLACE = {"sed", "perl", "gsed", "ruby"}
CD = {"cd", "chdir", "set-location", "sl", "push-location", "pushd"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"}
EVALS = {"eval", "iex", "invoke-expression"}
LAUNCHERS = {"sudo", "command", "env", "call", "time", "exec", "nohup", "xargs", "nice", "builtin"}
ENC_RE = re.compile(r"\b(?:pwsh|powershell)(?:\.exe)?\b[^\n;|&]*?\s-(?:e|ec|en[a-z]*)(?=\s|$)", re.IGNORECASE)
REDIRECT = re.compile(r"^\d*>>?(.*)$")


def segments(command, ps=False):
    """Split a shell command into simple-command token lists. Quotes are honoured; nothing runs."""
    esc = "`" if ps else "\\"
    seps = ";\n|&(){}" if ps else ";\n|&(){}`"
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
        elif c in seps:
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


def nested(command, ps, depth=0):
    """(text, ps) for the command and every string a nested shell would run."""
    yield command, ps
    if depth > 4:
        return
    for tokens in segments(command, ps):
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        while tokens and tokens[0].lower() in LAUNCHERS:
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[1:]
        if not tokens:
            continue
        name = re.sub(r"\.exe$", "", os.path.basename(tokens[0].replace("\\", "/")).lower())
        args = tokens[1:]
        if name not in SHELLS and name not in EVALS:
            continue
        if name in SHELLS and not any(a.lower() in ("-c", "-command", "/c", "/k", "-lc", "-ic") or
                                      re.match(r"^-[a-z]*c$", a) for a in args):
            continue
        inner = [a for a in args if not a.startswith("-") and not (name == "cmd" and a.startswith("/"))]
        if inner:
            yield from nested(" ".join(inner), ps or name in ("pwsh", "powershell"), depth + 1)


def canon(p):
    """Comparison form of a path: forward slashes, normalised, /c/x and /mnt/c/x as c:/x, lowercase."""
    p = p.replace("\\", "/")
    m = re.match(r"^/mnt/([A-Za-z])(?=/|$)", p) or re.match(r"^/([A-Za-z])(?=/|$)", p)
    if m:
        p = m.group(1) + ":" + p[m.end():]
    return posixpath.normpath(p).lower()


def home_candidates(path, cwd):
    """Canonical forms `path` may denote, expanding ~ / $HOME / $env:USERPROFILE / %USERPROFILE% against the session home."""
    p = (path or "").strip().strip("\"'").replace("\\", "/")
    if not p or not _HOMES:
        return []
    m = HOME_VAR.match(p)
    if m:
        return [canon(h + p[m.end():]) for h in _HOMES]
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        return [canon(p)]
    return [canon(posixpath.join((cwd or "").replace("\\", "/"), p))]


def home_dir_hit(path, cwd):
    """True when `path` is <home>/.claude or <home>/.mission-control itself (deleting or moving it takes the guards with it)."""
    cands = home_candidates(path, cwd)
    for h in (canon(x) for x in _HOMES):
        if any(n in (h + "/.claude", h + "/.mission-control") for n in cands):
            return True
    return False


def home_protected(path, cwd):
    """True when `path` is under <home>/.mission-control or is <home>/.claude/settings*.json (G-4)."""
    cands = home_candidates(path, cwd)
    for h in (canon(x) for x in _HOMES):
        for n in cands:
            if n == h + "/.mission-control" or n.startswith(h + "/.mission-control/"):
                return True
            if re.fullmatch(re.escape(h) + r"/\.claude/settings[^/]*\.json", n):
                return True
    return False


def is_protected(path, cwd):
    """True when `path` (as written in a tool call, relative to cwd) is one of the guarded files."""
    p = (path or "").strip().strip("\"'").replace("\\", "/")
    if not p:
        return False
    if home_protected(p, cwd):
        return True
    if _USER_SCOPE[0]:
        return False
    p = re.sub(r"^\$\{?(?:env:)?CLAUDE_PROJECT_DIR\}?/?", "/", p, flags=re.IGNORECASE)
    n = posixpath.normpath(posixpath.join((cwd or "").replace("\\", "/"), p))
    return bool(PROTECTED.search(n)) and not WORKTREE.search(n)


GLOB = re.compile(r"[*?\[]")
ANC_Q = [(".claude",), (".claude", "super-board"), (".claude", "super-board", "inflight")]
PROT_Q = [(".claude", "hooks"), (".claude", "bin"), (".claude", "workflows"), (".claude", "super-board", "configs"),
          (".claude", "super-board", "identity-halt"), (".claude", "super-board", "lane-results"),
          (".claude", "super-board", "events"),
          (".claude", "super-board", "inflight", "sandbox-1"), (".claude", "settings.json"),
          (".claude", "settings.local.json"), (".claude", "super-board", "active")]
DELETE_LIKE = {"rm", "rmdir", "unlink", "rd", "del", "erase", "remove-item", "ri", "mv", "move", "move-item", "mi",
               "rename-item", "rni", "ren"}
FIND_RM = {"rm", "rmdir", "unlink"}


def comp_match(pc, qc):
    if GLOB.search(pc):
        if qc.startswith(".") and not pc.startswith("."):  # the shell's `*` never matches a leading dot
            return False
        return fnmatch.fnmatchcase(qc.lower(), pc.lower())
    return pc.lower() == qc.lower()


def norm_target(path, cwd):
    p = (path or "").strip().strip("\"'").replace("\\", "/")
    p = re.sub(r"^\$\{?(?:env:)?CLAUDE_PROJECT_DIR\}?/?", "/", p, flags=re.IGNORECASE)
    return posixpath.normpath(posixpath.join((cwd or "").replace("\\", "/"), p)) if p else ""


def delete_hits(path, cwd):
    """True when deleting / moving `path` (glob or not) would remove a protected file or one of the protected
    directories' ancestors (.claude, .claude/super-board, .claude/super-board/inflight)."""
    n = norm_target(path, cwd)
    if not n or WORKTREE.search(n + "/"):
        return False
    comps = [c for c in n.split("/") if c]
    for i in range(len(comps)):
        if not comp_match(comps[i], ".claude"):
            continue
        rest = comps[i:]
        for q in ANC_Q:
            if len(rest) <= len(q) and all(comp_match(rest[j], q[j]) for j in range(len(rest))):
                return True
        for q in PROT_Q:
            if len(rest) >= len(q) and all(comp_match(rest[j], q[j]) for j in range(len(q))):
                return True
    return False


def find_delete(args, cwd, project):
    """The start path of a `find ... -delete | -exec rm ...` that would reach the protected files, or None."""
    starts = []
    for a in args:
        if a.startswith("-") and a not in ("-L", "-H", "-P") or a in ("(", "!", ")"):
            break
        if a not in ("-L", "-H", "-P"):
            starts.append(a)
    exprs = [a.lower() for a in args]
    deleting = "-delete" in exprs or any(
        a in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(args) and
        re.sub(r"\.exe$", "", os.path.basename(args[i + 1].replace("\\", "/")).lower()) in FIND_RM
        for i, a in enumerate(exprs))
    if not deleting:
        return None
    for st in starts or ["."]:
        n = norm_target(st, cwd)
        if not n or WORKTREE.search(n + "/"):
            continue
        if ".claude" in [c.lower() for c in n.split("/")]:
            return st
        proj = (project or cwd or "").replace("\\", "/").rstrip("/")
        if proj == n or proj.startswith(n.rstrip("/") + "/"):
            if ".claude" not in " ".join(args).lower():
                return st
    return None


def shell_write(command, ps, cwd, project=None):
    """The protected path a shell command would write, or None."""
    in_protected = False
    cur = cwd
    for tokens in segments(command, ps):
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        while tokens and tokens[0].lower() in LAUNCHERS:
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                tokens = tokens[1:]
        if not tokens:
            continue
        name = re.sub(r"\.exe$", "", os.path.basename(tokens[0].replace("\\", "/")).lower())
        args = tokens[1:]
        if name in CD:
            if args and is_protected(args[0].rstrip("/") + "/x", cur):
                in_protected = True
            word = next((a for a in args if not a.startswith("-")), None)
            hm = HOME_VAR.match(word.replace("\\", "/")) if word and _HOMES else None
            if hm:  # cd ~ / $HOME / $env:USERPROFILE: later relative targets resolve against home
                rest = word.replace("\\", "/")[hm.end():].lstrip("/")
                cur = posixpath.normpath(posixpath.join(_HOMES[0].replace("\\", "/"), rest))
            elif word and not re.search(r"[$%`~]", word) and word != "-":
                cur = norm_target(word, cur) or cur
            continue
        if name == "find" and not _USER_SCOPE[0]:
            hit = find_delete(args, cur, project)
            if hit:
                return hit
        # redirects: `> path`, `>> path`, `2>path`
        for i, t in enumerate(tokens):
            m = REDIRECT.match(t)
            if not m:
                continue
            target = m.group(1) or (tokens[i + 1] if i + 1 < len(tokens) else "")
            if target.startswith("&"):
                continue
            if is_protected(target, cur) or (in_protected and target):
                return target
        files = [a for a in args if not a.startswith("-") and not REDIRECT.match(a)]
        if name in COPY_LIKE:
            check = files if name in ("mv", "move", "move-item", "mi") else files[-1:]
        elif name in ANY_ARG:
            check = files
        elif name in INPLACE and any(a.startswith("-i") or a == "--in-place" for a in args):
            check = files
        elif name == "dd":
            check = [a[3:] for a in args if a.startswith("of=")]
        else:
            continue
        if name in DELETE_LIKE:
            for f in files:
                if (not _USER_SCOPE[0] and delete_hits(f, cur)) or home_dir_hit(f, cur):
                    return f
        if name in ANY_ARG | COPY_LIKE | INPLACE | {"dd"}:
            for f in check:
                # -Path x / -FilePath x style values are plain args too
                if is_protected(f, cur) or (in_protected and f):
                    return f
    return None


def judge(payload, env, user_scope=False):
    """None when allowed, else the reason. user_scope: apply only the home rules (<home>/.mission-control, <home>/.claude/settings*.json)."""
    if env.get("SB_ALLOW_CONFIG_WRITE") == "1":
        return None
    tool = payload.get("tool_name")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    _HOMES[:] = [env[k] for k in ("USERPROFILE", "HOME") if env.get(k)]
    _USER_SCOPE[0] = bool(user_scope)
    if tool in FILE_TOOLS:
        path = ti.get(FILE_TOOLS[tool]) or ""
        if is_protected(path, cwd):
            if (tool == "Write" and ACTIVE_PATH.search(norm_target(path, cwd))
                    and not ACTIVE_POINTER_RE.match(str(ti.get("content") or "").strip())):
                return "writes %s with content that is not a pointer name (%s)" % (path, ACTIVE_POINTER_RE.pattern)
            return "writes %s" % path
        return None
    if tool in SHELL_TOOLS:
        command = ti.get("command")
        if not isinstance(command, str):
            return None
        command = HOME_BRACED.sub(lambda m: "$" + m.group(1), command)
        if ENC_RE.search(command):
            return "powershell -EncodedCommand (the command cannot be inspected)"
        for text, nps in nested(command, tool == "PowerShell"):
            hit = shell_write(text, nps, cwd, env.get("CLAUDE_PROJECT_DIR") or cwd)
            if hit:
                return "writes %s" % hit
    return None


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        why = judge(payload, os.environ, "--user-scope" in sys.argv[1:])
        if why:
            reason = ("Blocked by guard-config-write (%s): a session may not rewrite .claude/hooks, .claude/bin, "
                      ".claude/workflows, .claude/settings*.json or .claude/super-board/configs. Ask the owner to "
                      "make the change, or to start Claude Code with SB_ALLOW_CONFIG_WRITE=1 for it. The same goes for "
                      "~/.mission-control, ~/.claude/settings*.json and .claude/super-board/active." % why)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
    except Exception as e:  # fail CLOSED: a guard that cannot decide blocks
        sys.stderr.write("guard-config-write failed closed (%s: %s); the call was blocked. "
                         "Fix the hook or ask the owner (SB_ALLOW_CONFIG_WRITE=1 at session start).\n"
                         % (type(e).__name__, e))
        sys.exit(2)


if __name__ == "__main__":
    main()
