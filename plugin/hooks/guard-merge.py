#!/usr/bin/env python3
"""PreToolUse(Bash|PowerShell) guard: no merging a PR and no retargeting a PR onto main from the shell.

ARCHITECTURAL NOTE: hooks filter command TEXT. The real control that keeps main safe is the
server-side ruleset ("main: admin only"); this guard is defense in depth.

A change reaches a base branch only through the merge gate (`.claude/bin/super-board-merge-gate.sh`):
it applies merge_policy, the pinned head and the approval check, then runs its own `gh pr merge`
INSIDE the script process, which no hook sees. Everything a session could do by hand around that
gate is refused here:

  gh pr merge ...                              (any flags, `gh.exe`, a path to gh, `-R repo` in front)
  gh api ... /merge | /merges                  (any method; also when the path is built in a variable)
  gh api graphql ... mergePullRequest | enablePullRequestAutoMerge | updatePullRequest+baseRefName
                                               (also `--input` / `query=@file` / `query=$var` bodies
                                               that cannot be inspected)
  gh pr create|edit --base main | -B main | --base=main | -Bmain | --base $var
  gh pr create with no explicit base (--base / --base= / -B)
  gh api ... -f|-F base=main on a /pulls endpoint (and `--input` bodies there, `base=$var`)
  gh api with a `$`-containing argument and a mutating method (-X PUT|POST|PATCH|DELETE, or -f/-F fields)
                                               on a non-graphql endpoint
  gh @splat                                    (PowerShell splatted arguments cannot be inspected)
  iex / Invoke-Expression with piped input or a non-literal argument
  powershell|pwsh -e|-ec|-enc|-EncodedCommand ...

"main" means main, master and any name in SB_MERGE_PROTECTED (comma-separated), EXCEPT a branch a
board config names as its `base_branch`: a repo whose base IS main opens its PRs against main.

Allowed on purpose: a plain call of `.claude/bin/super-(board|review|qa)-*.sh|py` anchored at the
project (relative, `$CLAUDE_PROJECT_DIR/...` or the project's absolute path). The script runs the
merge itself; other commands chained after it are still checked. Its `--config` is pinned: absent, or a
file that resolves (realpath, case-insensitive on Windows) under `<project-dir>/.claude/super-board/configs/`.
Any other `--config` (a foreign path, a variable, a missing value) is refused, so the gate cannot be
pointed at a config the agent wrote elsewhere (a looser merge_policy).

`gh pr create` must carry an explicit base (`--base X`, `--base=X`, `-B X`): the denial says
"pass --base <base_branch>" with the base_branch read from the board config.

Looks through `bash -c '...'`, `pwsh -Command '...'`, `cmd /c`, `eval`, `Invoke-Expression`,
`Start-Process gh -FilePath/-ArgumentList`, `sudo`, `xargs`, `$( )` and chains. Fails CLOSED: any
exception exits 2 with a message on stderr.

Known residual bypasses: a command name or subcommand built at run time that this guard cannot see,
a request body piped to `gh api --input -` on an endpoint other than /pulls, a script the command
merely calls (including one the agent wrote into .claude/bin; guard-config-write blocks that write),
a graphql query held in a
variable that does not start with `$`, and heredoc text that happens to contain a command line (it is
checked as one: that fails safe, never open).
"""
import glob
import json
import os
import re
import sys

SHELL_TOOLS = ("Bash", "PowerShell")
WRAPPERS = {"sudo", "command", "env", "call", "time", "exec", "nohup", "xargs", "nice", "builtin", "timeout", "stdbuf", "ionice"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "pwsh", "powershell", "cmd"}
PS_EVALS = {"iex", "invoke-expression"}
INTERPRETERS = {"bash", "sh", "python", "python3", "py"}
GATE_NAMES = ("super-board-", "super-review-", "super-qa-")
GRAPHQL_MERGE = re.compile(r"mergePullRequest|enablePullRequestAutoMerge", re.IGNORECASE)
MERGE_PATH = re.compile(r"/merges?(?:[/?#].*)?$", re.IGNORECASE)
BASE_FIELD = re.compile(r"^(?:-[fF])?base=(.+)$", re.IGNORECASE)
ENC_RE = re.compile(r"\b(?:pwsh|powershell)(?:\.exe)?\b[^\n;|&]*?\s-(?:e|ec|en[a-z]*)(?=\s|$)", re.IGNORECASE)
PIPED_IEX = re.compile(r"\|\s*(?:iex|Invoke-Expression)\b", re.IGNORECASE)
GH_WORD = re.compile(r"(?<![\w.-])gh(?:\.exe)?(?![\w.-])", re.IGNORECASE)
MERGE_PATH_TEXT = re.compile(r"/merges?(?=[/?#\s\"';|&)]|$)", re.IGNORECASE)


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


def configured_bases(project):
    names = set()
    for p in glob.glob(os.path.join(project, ".claude", "super-board", "configs", "*.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                base = json.load(fh).get("base_branch")
            if isinstance(base, str) and base:
                names.add(base)
        except (OSError, ValueError, AttributeError):
            pass
    return names


def protected_names(project, env):
    names = {"main", "master"}
    names.update(n.strip() for n in env.get("SB_MERGE_PROTECTED", "").split(",") if n.strip())
    return names - configured_bases(project)


def branch(ref):
    ref = ref.strip().strip("\"'")
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def base_values(args):
    """Every value given to --base / -B in a `gh pr create|edit` argument list."""
    vals = []
    for i, a in enumerate(args):
        if a in ("--base", "-B") and i + 1 < len(args):
            vals.append(args[i + 1])
        elif a.startswith("--base="):
            vals.append(a[len("--base="):])
        elif a.startswith("-B") and len(a) > 2:
            vals.append(a[2:].lstrip("="))
    return vals


def mutating(args):
    """True when a `gh api` call writes: an explicit non-GET method, or -f/-F/--input fields."""
    for i, a in enumerate(args):
        if a in ("-X", "--method") and i + 1 < len(args) and args[i + 1].upper() in ("PUT", "POST", "PATCH", "DELETE"):
            return True
        if a.upper().startswith("--METHOD=") and a.split("=", 1)[1].upper() in ("PUT", "POST", "PATCH", "DELETE"):
            return True
        if a in ("-f", "-F", "--field", "--raw-field", "--input") or re.match(r"^-[fF]\w", a):
            return True
    return False


def judge_gh(args, protected, bases=()):
    """None when this `gh` argument list is fine, else the reason."""
    if any(a.startswith("@") and len(a) > 1 for a in args):
        return "gh with splatted arguments (cannot be inspected)"
    positional, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ("-R", "--repo"):
            i += 1
        elif not a.startswith("-"):
            positional.append(a)
        i += 1
    sub, sub2 = (positional + ["", ""])[:2]
    if sub == "pr":
        if sub2 == "merge":
            return "gh pr merge"
        if sub2 == "create" and not base_values(args):
            return "gh pr create without an explicit base: pass --base %s" % (" or ".join(sorted(bases)) or "<base_branch>")
        if sub2 in ("create", "edit"):
            for v in base_values(args):
                if "$" in v:
                    return "gh pr %s --base from a variable" % sub2
                if branch(v) in protected:
                    return "gh pr %s --base %s" % (sub2, branch(v))
    if sub == "api":
        for t in args:
            if GRAPHQL_MERGE.search(t):
                return "GraphQL merge mutation"
            if MERGE_PATH.search(t) and not t.startswith("-"):
                return "gh api .../merge"
        graphql = "graphql" in positional
        if graphql and (any(t == "--input" for t in args) or any(re.search(r"(?:^|=)@", t) for t in args)):
            return "gh api graphql with a body from a file (cannot be inspected)"
        if graphql and any(re.match(r"^query=[$@]", t) or re.match(r"^-[fF]query=[$@]", t) for t in args):
            return "gh api graphql with a query held in a variable (cannot be inspected)"
        if not graphql and mutating(args) and any("$" in t for t in args):
            return "gh api write with a variable argument (cannot be inspected)"
        if any("pulls" in t for t in positional):
            if any(t == "--input" for t in args):
                return "gh api pulls with --input (cannot be inspected)"
            for t in args:
                m = BASE_FIELD.match(t)
                if m and ("$" in m.group(1) or branch(m.group(1)) in protected):
                    return "gh api pulls base=%s" % branch(m.group(1))
    return None


def is_gate(tokens, project):
    """True for a plain `[bash|python3] .claude/bin/super-*.sh|py ...` call anchored at the project."""
    t = list(tokens)
    if t and re.sub(r"\.exe$", "", os.path.basename(t[0].replace("\\", "/")).lower()) in INTERPRETERS:
        t = t[1:]
        while t and t[0].startswith("-"):
            t = t[1:]
    if not t:
        return False
    path = t[0].replace("\\", "/")
    if ".." in path.split("/") or not re.search(r"\.(?:sh|py)$", path):
        return False
    proj = project.replace("\\", "/").rstrip("/")
    roots = [".claude/bin/", "./.claude/bin/", proj + "/.claude/bin/", "$CLAUDE_PROJECT_DIR/.claude/bin/",
             "${CLAUDE_PROJECT_DIR}/.claude/bin/", "$env:CLAUDE_PROJECT_DIR/.claude/bin/"]
    return any(path.startswith(r + n) for r in roots for n in GATE_NAMES)


PROJECT_VARS = ("${CLAUDE_PROJECT_DIR}", "$CLAUDE_PROJECT_DIR", "$env:CLAUDE_PROJECT_DIR")


def config_values(tokens):
    """(values, problem): every --config / --config=X value in a gate call; problem when one has no value."""
    vals = []
    for i, a in enumerate(tokens):
        if a == "--config":
            if i + 1 >= len(tokens):
                return vals, "--config has no value"
            vals.append(tokens[i + 1])
        elif a.startswith("--config="):
            vals.append(a[len("--config="):])
    return vals, None


def expand_project(v, project):
    v = v.strip()
    for var in PROJECT_VARS:
        if v.startswith(var + "/") or v.startswith(var + "\\"):
            return project + v[len(var):]
    return v


def resolve_in(v, project, cwd):
    """Absolute path of a literal word, relative ones against the effective cwd; None when it cannot be known."""
    v = expand_project(v, project)
    if not v or v.startswith("~") or any(c in v for c in "$%`*?"):
        return None
    if os.path.isabs(v):
        return v
    return os.path.join(cwd, v) if cwd else None


def under(real, root):
    real, root = os.path.normcase(os.path.realpath(real)), os.path.normcase(os.path.realpath(root))
    return real.startswith(root.rstrip("\\/") + os.sep)


def config_pinned(value, project, cwd=None):
    """True when value names an EXISTING file whose realpath is under <project-dir>/.claude/super-board/configs/
    (relative values resolve against the effective cwd; case-insensitive on Windows; a copy under
    .claude/worktrees/** does not qualify)."""
    path = resolve_in(value, project, cwd)
    return bool(path) and os.path.isfile(path) and under(path, os.path.join(project, ".claude", "super-board", "configs"))


def gate_script_path(tokens):
    t = list(tokens)
    if t and re.sub(r"\.exe$", "", os.path.basename(t[0].replace("\\", "/")).lower()) in INTERPRETERS:
        t = t[1:]
        while t and t[0].startswith("-"):
            t = t[1:]
    return t[0] if t else ""


def gate_config_problem(tokens, project, cwd=None):
    """None when a gate call is the project's own script with a --config that is absent or pinned, else the reason."""
    script = resolve_in(gate_script_path(tokens), project, cwd)
    if not script or not under(script, os.path.join(project, ".claude", "bin")):
        return ("board script %r does not resolve to %s/.claude/bin/ from the effective cwd (a worktree copy or a "
                "different directory does not count)" % (gate_script_path(tokens), project))
    vals, problem = config_values(tokens)
    if problem:
        return "board script %s (pass a file under .claude/super-board/configs/)" % problem
    for v in vals:
        if not config_pinned(v, project, cwd):
            return ("board script with --config %r: only an existing file under <project-dir>/.claude/super-board/configs/ "
                    "is allowed (a literal path resolved from the effective cwd, no variables)" % v)
    return None


CD_NAMES = {"cd", "chdir", "pushd", "push-location", "set-location", "sl"}
POP_NAMES = {"popd", "pop-location"}
TAINT_NAMES = {"PATH", "BASH_ENV", "ENV", "CDPATH", "IFS", "SHELLOPTS", "BASHOPTS", "LD_PRELOAD", "LD_LIBRARY_PATH",
               "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONUSERBASE"}
# Known wrappers: (options that take an argument, boolean options). An unknown option => deny: the guard
# cannot tell where the wrapped command starts. `-5`-style numeric (nice) and attached values (-I{}, -n5) are fine.
WRAPPER_OPTS = {
    "sudo": ({"-u", "-g", "-U", "-h", "-p", "-C", "-r", "-t", "-D", "-R", "-T", "--user", "--group", "--chdir"},
             {"-E", "-H", "-n", "-S", "-b", "-i", "-s", "-k", "-K", "-A", "-B", "-P", "-e", "--preserve-env", "--login",
              "--shell", "--non-interactive", "--background", "--"}),
    "env": ({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}, {"-i", "-0", "-v", "--ignore-environment", "--null", "-", "--"}),
    "nice": ({"-n", "--adjustment"}, {"--"}),
    "xargs": ({"-n", "-I", "-L", "-P", "-s", "-E", "-d", "-a", "--max-args", "--replace", "--max-lines", "--max-procs",
               "--max-chars", "--delimiter", "--arg-file", "--eof"},
              {"-0", "-r", "-t", "-p", "-x", "-o", "--null", "--no-run-if-empty", "--verbose", "--interactive", "--exit", "--"}),
    "timeout": ({"-s", "-k", "--signal", "--kill-after"}, {"--preserve-status", "--foreground", "-v", "--verbose", "--"}),
    "stdbuf": ({"-o", "-e", "-i", "--output", "--error", "--input"}, {"--"}),
    "ionice": ({"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid", "--pgid", "--uid"}, {"-t", "--ignore", "--"}),
    "command": (set(), {"-p", "-v", "-V", "--"}),
    "builtin": (set(), set()),
    "exec": ({"-a"}, {"-c", "-l"}),
    "nohup": (set(), set()),
    "call": (set(), set()),
    "time": ({"-f", "-o", "--format", "--output"}, {"-p", "-v", "-a", "-q", "--portability", "--verbose", "--append", "--quiet", "--"}),
}
CHDIR_OPTS = {"env": {"-C", "--chdir"}, "sudo": {"-D", "--chdir"}}


def strip_wrappers(tokens, project, cur):
    """(tokens after the wrappers, effective cwd, env assignments seen, problem). Consumes each known wrapper's
    options with their arguments; an unknown option, or a missing argument, is a problem (deny)."""
    assigns = []
    while tokens and tokens[0].lower() in WRAPPERS:
        wrapper, tokens = tokens[0].lower(), tokens[1:]
        takes, flags = WRAPPER_OPTS[wrapper]
        seen_duration = False
        while tokens:
            t = tokens[0]
            if re.match(r"^[A-Za-z_]\w*=", t) and wrapper in ("env", "sudo", "time", "nice", "command", "exec"):
                assigns.append(t.split("=", 1)[0])
                tokens = tokens[1:]
                continue
            if wrapper == "timeout" and not t.startswith("-") and not seen_duration:
                seen_duration, tokens = True, tokens[1:]
                continue
            if not t.startswith("-") or (t == "-" and "-" not in flags):
                break
            opt, eq, val = t.partition("=")
            if t in takes or (eq and opt in takes):
                if not eq:
                    if len(tokens) < 2:
                        return tokens, cur, assigns, "%s option %s has no argument" % (wrapper, t)
                    val, tokens = tokens[1], tokens[1:]
                if opt in CHDIR_OPTS.get(wrapper, ()):
                    target = resolve_in(val, project, cur)
                    cur = os.path.normpath(target) if target else None
                tokens = tokens[1:]
            elif t in flags:
                tokens = tokens[1:]
            elif len(t) > 2 and t[:2] in takes and not t.startswith("--"):  # attached value: -I{} -n5 -oL
                if t[:2] in CHDIR_OPTS.get(wrapper, ()):
                    target = resolve_in(t[2:], project, cur)
                    cur = os.path.normpath(target) if target else None
                tokens = tokens[1:]
            elif wrapper == "nice" and re.match(r"^-\d+$", t):
                tokens = tokens[1:]
            elif wrapper == "ionice" and re.match(r"^-[cnpPu]\d+$", t):
                tokens = tokens[1:]
            else:
                return tokens, cur, assigns, "%s with the unknown option %s (cannot tell what it wraps)" % (wrapper, t)
    return tokens, cur, assigns, None


def start_process(args):
    """(command, argument words) of a Start-Process call: -FilePath / -ArgumentList, commas split."""
    fp, al, pos, i = None, [], [], 0
    while i < len(args):
        a, low = args[i], args[i].lower()
        if low in ("-filepath", "-file") and i + 1 < len(args):
            fp, i = args[i + 1], i + 2
        elif low.startswith("-argumentlist") or low == "-args":
            if i + 1 < len(args):
                al.append(args[i + 1])
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            pos.append(a)
            i += 1
    fp = fp or (pos[0] if pos else "")
    if not al and len(pos) > 1:
        al = pos[1:2]
    return fp, [w for a in al for w in re.split(r"[,\s]+", a) if w]


def judge_command(command, ps, protected, project, depth=0, cwd=None):
    """None when allowed, else the reason."""
    if depth > 4:
        return "command nesting too deep to inspect"
    if ENC_RE.search(command):
        return "PowerShell -EncodedCommand (cannot be inspected)"
    if PIPED_IEX.search(command):
        return "piped input into Invoke-Expression (cannot be inspected)"
    if GRAPHQL_MERGE.search(command):
        return "GraphQL merge mutation"
    if re.search(r"updatePullRequest", command, re.IGNORECASE) and re.search(r"baseRefName", command, re.IGNORECASE):
        return "GraphQL updatePullRequest with baseRefName (retarget)"
    if GH_WORD.search(command) and MERGE_PATH_TEXT.search(command):
        return "a merge endpoint named in a gh command"
    cur = cwd or project  # effective cwd: the payload's, then any cd / Set-Location / pushd earlier in the command
    tainted = False  # PATH / BASH_ENV ... set earlier in this command: a gate script would run under it
    for tokens in segments(command, ps):
        assigns = []
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            assigns.append(tokens[0].split("=", 1)[0])
            tokens = tokens[1:]
        if not tokens and any(a.upper() in TAINT_NAMES for a in assigns):
            tainted = True  # `PATH=x; bash .claude/bin/...`
        tokens, cur, wrapped, problem = strip_wrappers(tokens, project, cur)
        if problem:
            return problem
        assigns += wrapped
        if tokens and tokens[0].lower() in ("export", "declare", "typeset", "set-item", "si", "set-variable", "setx", "set") \
                and any(re.sub(r"^\$?env:", "", a.split("=", 1)[0], flags=re.IGNORECASE).upper() in TAINT_NAMES
                        for a in tokens[1:] if not a.startswith("-")):
            tainted = True
        if ps and tokens and tokens[0].startswith("$"):  # `$out = gh ...` / `$out=gh ...` / `$env:PATH = ...`
            if re.match(r"^\$env:", tokens[0], re.IGNORECASE) and tokens[0].split("=", 1)[0][5:].upper() in TAINT_NAMES:
                tainted = True
            if len(tokens) > 1 and tokens[1] == "=":
                tokens = tokens[2:]
            elif "=" in tokens[0] and tokens[0].split("=", 1)[1]:
                tokens = [tokens[0].split("=", 1)[1]] + tokens[1:]
        if not tokens:
            continue
        low0 = tokens[0].lower()
        if low0 in POP_NAMES:
            cur = None
            continue
        if low0 in CD_NAMES:
            word = next((a for a in tokens[1:] if not a.startswith("-") or a == "-"), None)
            target = resolve_in(word, project, cur) if word and word != "-" else None  # `cd -`, bare `cd`: unknown
            cur = os.path.normpath(target) if target else None  # unknowable: relative gate paths are refused
            continue
        if is_gate(tokens, project):
            if assigns:
                return "board script run with environment assignments (%s): PATH / BASH_ENV style overrides can swap what it runs" % ", ".join(assigns)
            if tainted:
                return "board script run after PATH / BASH_ENV (or similar) was changed in the same command"
            why = gate_config_problem(tokens, project, cur)
            if why:
                return why
            continue
        cmd, args = tokens[0], tokens[1:]
        name = re.sub(r"\.exe$", "", os.path.basename(cmd.replace("\\", "/")).lower())
        if name in ("start-process", "start", "saps") and args:
            cmd, args = start_process(args)
            name = re.sub(r"\.exe$", "", os.path.basename(cmd.replace("\\", "/")).lower())
        if name == "gh":
            why = judge_gh(args, protected, configured_bases(project))
            if why:
                return why
        elif name in PS_EVALS:
            inner = [a for a in args if not a.startswith("-")]
            if not inner or any("$" in a or "+" in a for a in inner):
                return "Invoke-Expression with piped or non-literal input (cannot be inspected)"
            why = judge_command(" ".join(inner), True, protected, project, depth + 1, cur)
            if why:
                return why
        elif name == "eval" or name in SHELLS:
            inner = [a for a in args if not a.startswith("-") and not (name == "cmd" and a.startswith("/"))]
            if name == "eval" or any(a.lower() in ("-c", "-command", "/c", "/k", "-lc", "-ic") or
                                     re.match(r"^-[a-z]*c$", a) for a in args):
                if inner:
                    why = judge_command(" ".join(inner), ps or name in ("pwsh", "powershell"), protected,
                                        project, depth + 1, cur)
                    if why:
                        return why
        elif re.match(r"^[$%@]", cmd) or cmd.startswith("${"):
            low = [a.lower() for a in args]
            if "merge" in low or any(MERGE_PATH.search(a) for a in args):
                return "dynamic command name with a merge argument"
    return None


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        tool = payload.get("tool_name")
        command = (payload.get("tool_input") or {}).get("command")
        if tool not in SHELL_TOOLS or not isinstance(command, str):
            return
        project = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
        why = judge_command(command, tool == "PowerShell", protected_names(project, os.environ), project,
                            cwd=payload.get("cwd") or None)
        if why:
            reason = ("Blocked by guard-merge (%s): a PR is merged only through the merge gate, and "
                      "never retargeted onto a protected branch from the shell. Run `bash "
                      ".claude/bin/super-board-merge-gate.sh --pr <number> ...` (it applies merge_policy and the "
                      "pinned head), or ask the owner." % why)
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
    except Exception as e:  # fail CLOSED: a guard that cannot decide blocks
        sys.stderr.write("guard-merge failed closed (%s: %s); the call was blocked. "
                         "Fix the hook or ask the user to run the command with the ! prefix.\n"
                         % (type(e).__name__, e))
        sys.exit(2)


if __name__ == "__main__":
    main()
