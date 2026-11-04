#!/usr/bin/env python3
"""PreToolUse(Bash|PowerShell) guard: no direct or force pushes to a protected base branch.

Opt-in. `super-board onboard` asks once ("Block direct/force pushes to main?") and
`install.sh --protect-main` wires it. Lanes push feature branches and merge through
`super-board-merge-gate.sh` (`gh pr merge`), so nothing in the loop needs to push a base.

Protected: main, master, every `base_branch` in .claude/super-board/configs/*.json, and
any name in SB_PROTECTED_BRANCHES (comma-separated). A `git push` is denied when it would
update or delete one of them: an explicit refspec (`feat:main`, `+main`, `HEAD:refs/heads/main`,
`--delete main`), `--all` / `--mirror`, or a bare `git push` / `git push origin HEAD` while the
protected branch is checked out. Force flags (`-f`, `--force`, `--force-with-lease`) are named in
the reason; force pushes to feature branches stay allowed (the rebase pass needs them).

SB_ALLOW_PROTECTED_PUSH=1 in Claude Code's environment turns it off for a session.

Fails CLOSED: any exception while judging a push exits 2 with a message on stderr, so a
guard that cannot decide blocks the call (the session may run in bypassPermissions mode).
Looks through nested shells (bash -c, pwsh -Command, cmd /c, eval, iex), denies a refspec built from a
variable (`git push origin $b`) and `powershell|pwsh -e|-ec|-enc|-EncodedCommand`. Hooks filter command
text; the server-side ruleset is the real control for main.
Known residual bypasses: a git alias (`git -c alias.x=push x`), a push made by a script the
command merely calls, and a command whose name is built at run time.
"""
import glob
import json
import os
import re
import subprocess
import sys

GIT_VALUE_OPTS = {"-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
PUSH_VALUE_OPTS = {"--repo", "--receive-pack", "--exec", "-o", "--push-option", "--signed"}
FORCE = re.compile(r"^(-f|--force|--force-with-lease(=.*)?|--force-if-includes)$")


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


def branch_name(ref):
    ref = ref.lstrip("+")
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


CD_CMDS = {"cd", "chdir", "set-location", "sl", "push-location", "pushd"}


def pushes(command, cwd, ps=False):
    """[(git_dir, flags, positional)] for every `git push` in the command."""
    found, d = [], cwd
    for tokens in segments(command, ps):
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if tokens and tokens[0].lower() in ("sudo", "command", "env", "call"):
            tokens = tokens[1:]
        if not tokens:
            continue
        cmd, args = tokens[0], tokens[1:]
        if cmd.lower() in CD_CMDS and args and "$" not in args[0]:
            d = os.path.normpath(os.path.join(d, os.path.expanduser(args[0])))
            continue
        if re.sub(r"\.exe$", "", os.path.basename(cmd.replace("\\", "/")).lower()) != "git":
            continue
        git_dir, i = d, 0
        while i < len(args) and args[i].startswith("-"):
            if args[i] == "-C" and i + 1 < len(args):
                git_dir = os.path.normpath(os.path.join(git_dir, os.path.expanduser(args[i + 1])))
                i += 2
            elif args[i] in GIT_VALUE_OPTS:
                i += 2
            else:
                i += 1
        if i >= len(args) or args[i] != "push":
            continue
        flags, positional, j = [], [], i + 1
        while j < len(args):
            a = args[j]
            if a == "--":
                positional += args[j + 1:]
                break
            if a in PUSH_VALUE_OPTS:
                j += 1
            elif a.startswith("-"):
                flags.append(a)
            elif not re.match(r"^\d*[<>]", a):
                positional.append(a)
            j += 1
        found.append((git_dir, flags, positional))
    return found


def current_branch(d):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        r = subprocess.run(["git", "-C", d, "symbolic-ref", "--short", "-q", "HEAD"],
                           capture_output=True, text=True, env=env, timeout=10)
        return r.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def protected_branches(project_dir):
    names = {"main", "master"}
    for p in glob.glob(os.path.join(project_dir, ".claude", "super-board", "configs", "*.json")):
        try:
            base = json.load(open(p, encoding="utf-8")).get("base_branch")
            if isinstance(base, str) and base:
                names.add(base)
        except (OSError, ValueError, AttributeError):
            pass
    names.update(n.strip() for n in os.environ.get("SB_PROTECTED_BRANCHES", "").split(",") if n.strip())
    return names


def check(command, cwd, protected, branch_of, ps=False):
    """None when allowed, else the deny reason. branch_of(dir) -> checked-out branch or None."""
    if not re.search(r"\bpush\b", command):
        return None
    for git_dir, flags, positional in pushes(command, cwd, ps):
        force = any(FORCE.match(f) for f in flags) or any(p.startswith("+") for p in positional[1:])
        deleting = any(f in ("-d", "--delete") for f in flags)
        hit = None
        if "--all" in flags or "--mirror" in flags or "--branches" in flags:
            hit = "every branch (" + next(f for f in flags if f in ("--all", "--mirror", "--branches")) + ")"
        elif len(positional) <= 1:
            if not deleting and "--tags" not in flags:
                cur = branch_of(git_dir)
                if cur in protected:
                    hit = cur
        else:
            for spec in positional[1:]:
                if "$" in spec:
                    hit = "a refspec built from a variable (" + spec + ")"
                    break
                dst = spec.split(":", 1)[1] if ":" in spec else spec
                if dst.lstrip("+") == "HEAD" or (":" not in spec and spec.lstrip("+") == "HEAD"):
                    dst = branch_of(git_dir) or dst
                name = branch_name(dst)
                if name in protected:
                    hit = name
                    break
        if hit:
            kind = "Force push" if force else ("Delete" if deleting else "Direct push")
            return (
                "Blocked: %s to protected branch %s.\n"
                "Base branches change only through a reviewed PR and the merge gate "
                "(super-board-merge-gate.sh). Push a feature branch and open a PR instead:\n"
                "  git push -u origin <feature-branch>\n"
                "Protected here: %s. If this push really is intended, ask the user to run it, "
                "or start Claude Code with SB_ALLOW_PROTECTED_PUSH=1." % (kind, hit, ", ".join(sorted(protected)))
            )
    return None


def main():
    try:
        if os.environ.get("SB_ALLOW_PROTECTED_PUSH") == "1":
            return
        payload = json.loads(sys.stdin.read() or "{}")
        tool = payload.get("tool_name")
        command = (payload.get("tool_input") or {}).get("command")
        if tool not in ("Bash", "PowerShell") or not isinstance(command, str):
            return
        cwd = payload.get("cwd") or os.getcwd()
        project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
        reason = ("PowerShell -EncodedCommand cannot be inspected" if ENC_RE.search(command) else None)
        for text, nps in ([] if reason else nested(command, tool == "PowerShell")):
            reason = check(text, cwd, protected_branches(project), current_branch, nps)
            if reason:
                break
        if reason:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
    except Exception as e:  # fail CLOSED: a guard that cannot decide blocks
        sys.stderr.write("guard-protected-push failed closed (%s: %s); the call was blocked. Fix the hook, or "
                         "set SB_ALLOW_PROTECTED_PUSH=1 / ask the user to run the command.\n" % (type(e).__name__, e))
        sys.exit(2)


if __name__ == "__main__":
    main()
