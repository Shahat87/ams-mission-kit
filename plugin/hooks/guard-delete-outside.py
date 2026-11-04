#!/usr/bin/env python3
"""PreToolUse(Bash|PowerShell) guard: never delete files outside the project.

Denies `rm`, `rmdir`, `unlink`, PowerShell `Remove-Item` (`ri`, `del`, `erase`, `rd`), `find ... -delete` / `find ... -exec rm`, and `git clean`
when a target resolves outside $CLAUDE_PROJECT_DIR, and always when it is `/` or the home
directory itself. Paths are resolved against the hook's `cwd`, honouring `cd X &&`; `~`,
`$HOME` and other set variables are expanded the way the shell would. A word whose value
cannot be known before it runs (a `$(...)`, or a variable assigned earlier in the same
command) is allowed: the guard catches the literal mistakes, not every possible one.

Temp roots stay deletable inside (not the roots themselves): /tmp, /private/tmp,
/var/folders and $TMPDIR. `git clean` is denied only when it would run in a directory
outside the project.

Looks through nested shells (bash -c, pwsh -Command, cmd /c, eval, iex), denies `[IO.Directory]::Delete` /
`[IO.File]::Delete` outside the project (or with a path it cannot resolve), a path-less
`Remove-Item`/`ri`/`del` (its target comes from a pipe), and `powershell|pwsh -e|-ec|-enc|-EncodedCommand`.
Hooks filter command text; the server-side ruleset is the real control for main.

Fails CLOSED: any exception (malformed input included) exits 2 with a message on stderr. The session may
run in bypassPermissions, so a guard that cannot decide must block. Empty input and ordinary payloads pass.
"""
import json
import os
import re
import sys

RM_CMDS = {"rm", "rmdir", "unlink", "trash", "remove-item", "ri", "del", "erase", "rd"}
TEMP_ROOTS = ["/tmp", "/private/tmp", "/var/folders", "/private/var/folders"]


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
                if c == "$" and quote == "'":
                    cur += "\0"  # single-quoted $ is literal; mark it so expand() leaves it
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


SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"}
EVALS = {"eval", "iex", "invoke-expression"}
LAUNCHERS = {"sudo", "command", "env", "call", "time", "exec", "nohup", "xargs", "nice", "builtin"}
ENC_RE = re.compile(r"\b(?:pwsh|powershell)(?:\.exe)?\b[^\n;|&]*?\s-(?:e|ec|en[a-z]*)(?=\s|$)", re.IGNORECASE)


def nested(command, ps, depth=0):
    """(text, ps) for the command and for every string a nested shell would run:
    bash -c '...', pwsh -Command '...', cmd /c ..., eval, Invoke-Expression."""
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


DOTNET_DELETE = re.compile(r"\[(?:System\.)?IO\.(?:Directory|File)\]\s*::\s*Delete\(\s*([^,)]*)", re.IGNORECASE)


def expand(word, env, local):
    """Expand ~ and $VAR like the shell. None when the value cannot be known before it runs."""
    if "$(" in word or "`" in word:
        return None
    s = re.sub(r"^~(?=/|$)", lambda m: env.get("HOME", "~"), word)
    unknowable = []

    def var(m):
        name = m.group(1) or m.group(2)
        if name in local:
            unknowable.append(name)
        return env.get(name, "")

    s = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", var, s)
    if unknowable:
        return None
    return s.replace("\0", "$")


def under(path, root):
    path, root = os.path.normcase(path), os.path.normcase(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def expand_ps(word, env):
    """PowerShell: ~, $HOME and $env:NAME are known; any other $ (variable, $(...)) is not."""
    home = env.get("HOME") or env.get("USERPROFILE") or "~"
    s = re.sub(r"^~(?=[\/]|$)", lambda m: home, word)
    unknown = []

    def var(m):
        name = m.group(1) or m.group(2) or m.group(3)
        val = env.get(name) or env.get(name.upper()) or (home if name.upper() == "HOME" else None)
        if val is None:
            unknown.append(name)
        return val or ""

    s = re.sub(r"\$\{?env:([A-Za-z_]\w*)\}?|\$\{(HOME)\}|\$(HOME)\b", var, s, flags=re.IGNORECASE)
    return None if unknown or "$" in s else s


def verdict(path, project, env):
    """None when the deletion target is fine, else why it is not."""
    home_raw = env.get("HOME") or env.get("USERPROFILE")
    home = os.path.normcase(os.path.normpath(home_raw)) if home_raw else None
    forms = {path, os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))}
    if any(os.path.dirname(p) == p for p in forms) or (home and any(os.path.normcase(p) == home for p in forms)):
        return "is the filesystem root or your home directory"
    roots = {project, os.path.realpath(project)}
    if any(under(p, r) for p in forms for r in roots):
        return None
    temps = TEMP_ROOTS + [os.path.normpath(env[k]) for k in ("TMPDIR", "TEMP", "TMP") if env.get(k)]
    if any(under(p, t) and p.rstrip(os.sep) != t.rstrip(os.sep) for p in forms for t in temps):
        return None
    return "is outside the project (%s)" % project


def targets(command, cwd, env, ps=False):
    """[(command_name, raw_word, resolved_or_None)] for every deletion target."""
    found, d, local = [], cwd, set()
    for tokens in segments(command, ps):
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            local.add(tokens[0].split("=", 1)[0])
            tokens = tokens[1:]
        while tokens and tokens[0] in ("sudo", "command", "nice", "time", "xargs"):
            tokens = tokens[1:]
        if tokens and tokens[0] in ("export", "local", "read", "for"):
            local.update(re.sub(r"=.*", "", t) for t in tokens[1:] if re.match(r"^[A-Za-z_]", t))
            continue
        if not tokens:
            continue
        cmd, args = os.path.basename(tokens[0].replace("\\", "/")), tokens[1:]
        if ps:
            cmd = re.sub(r"\.exe$", "", cmd.lower())
        if cmd in (("cd", "chdir", "set-location", "sl", "push-location", "pushd") if ps else ("cd",)):
            target = (expand_ps(args[0], env) if ps else expand(args[0], env, local)) if args else env.get("HOME")
            d = os.path.normpath(os.path.join(d, target)) if target else d
            continue

        def resolve(word, base=None):
            ex = expand_ps(word, env) if ps else expand(word, env, local)
            if ex is None or ex == "":
                return None
            return os.path.normpath(os.path.join(base or d, ex))

        if cmd in RM_CMDS:
            words, end = [], False
            for a in args:
                if not end and a == "--":
                    end = True
                elif end or not a.startswith("-") or a == "-":
                    words.append(a)
            for w in words:
                found.append((cmd, w, resolve(w)))
            if ps and not words:  # Get-ChildItem x | Remove-Item: the target comes from a pipe
                found.append((cmd, "", ""))
        elif cmd == "find":
            if "-delete" in args or any(a == "-exec" and i + 1 < len(args) and os.path.basename(args[i + 1]) in RM_CMDS
                                        for i, a in enumerate(args)):
                for w in args:
                    if w.startswith("-") or w in ("!", "(", ")"):
                        break
                    found.append(("find", w, resolve(w)))
        elif cmd == "git":
            git_dir, i = d, 0
            while i < len(args) and args[i].startswith("-"):
                if args[i] == "-C" and i + 1 < len(args):
                    git_dir = resolve(args[i + 1]) or git_dir
                    i += 2
                elif args[i] in ("-c", "--git-dir", "--work-tree"):
                    i += 2
                else:
                    i += 1
            if i < len(args) and args[i] == "clean":
                found.append(("git clean", git_dir, git_dir))
    return found


def check(command, cwd, project, env, ps=False):
    """None when allowed, else the deny reason."""
    if not re.search(r"\b(rm|rmdir|unlink|trash|find|clean|remove-item|ri|del|erase|rd)\b|::\s*Delete\(", command,
                     re.IGNORECASE):
        return None
    for m in DOTNET_DELETE.finditer(command):
        raw = m.group(1).strip().strip("\"'")
        ex = expand_ps(raw, env)
        path = os.path.normpath(os.path.join(cwd, ex)) if ex else None
        why = verdict(path, project, env) if path else "cannot be resolved before it runs"
        if why:
            return ('Blocked: .NET delete target "%s" %s.\n'
                    "Deleting outside the project is never part of a task here; use Remove-Item on a path "
                    "inside the repo, or ask the user." % (raw, why))
    for cmd, raw, resolved in targets(command, cwd, env, ps):
        if resolved == "":
            return ("Blocked: %s has no path argument, so its target comes from a pipe and cannot be checked.\n"
                    "Name the path inside the project: Remove-Item -Recurse <path>." % cmd)
        if resolved is None:
            continue
        why = verdict(resolved, project, env)
        if why:
            return (
                'Blocked: %s target "%s" resolves to %s, which %s.\n'
                "Deleting outside the project is never part of a task here. Delete inside the repo "
                "(or a temp dir under /tmp or $TMPDIR), or ask the user to run it themselves."
                % (cmd, raw, resolved, why)
            )
    return None


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = payload.get("tool_name")
        command = (payload.get("tool_input") or {}).get("command")
        if tool not in ("Bash", "PowerShell") or not isinstance(command, str):
            return
        cwd = payload.get("cwd") or os.getcwd()
        project = os.path.normpath(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)
        reason = ("Blocked: PowerShell -EncodedCommand cannot be inspected." if ENC_RE.search(command) else None)
        for text, nps in ([] if reason else nested(command, tool == "PowerShell")):
            reason = check(text, cwd, project, dict(os.environ), nps)
            if reason:
                break
        if reason:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
    except Exception as e:  # fail CLOSED: a guard that cannot decide blocks (the session may run in bypassPermissions)
        sys.stderr.write("guard-delete-outside failed closed (%s: %s); the call was blocked. "
                         "Fix the hook or ask the user to run the command with the ! prefix.\n"
                         % (type(e).__name__, e))
        sys.exit(2)


if __name__ == "__main__":
    main()
