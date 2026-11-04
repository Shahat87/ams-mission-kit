#!/usr/bin/env python3
"""GUARDRAIL: catches habit and mistakes; not a security boundary (spec §15.3).

guard-external-tools (G-1), user scope. In a guarded repo (mc_guarded.is_guarded: the project dir, the call's cwd, or a
--cd / -C / --prefix / --cwd target), external coding CLIs (codex, grok, pi, ...) run only on the Lenovo, through
`python3 .claude/bin/super-board-lane-delegate.py delegate`.

  PreToolUse Bash|PowerShell  deny a command that runs a covered tool (command word, npx-style package, or a delegate
                              script), looking through wsl / cmd /c / pwsh -c / bash -c / Start-Process / & / env ...
  PreToolUse mcp__<server>__* deny when that server's configured command or args are a covered tool
  SessionStart                warn (stdout, exit 0) when a covered MCP server is configured; never blocks

Deny = reason on stderr and exit 2. Any exception on PreToolUse = exit 2 (fail closed). No subprocess is ever started.
"""
import json
import os
import posixpath
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mc_guarded  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

COVERED = frozenset("codex grok grok-cli pi cursor-agent copilot opencode opencode-ai aider aider-chat kimi qoder vibe warp "
                    "agy cline".split())
PACKAGES = frozenset(["@openai/codex", "@mariozechner/pi-coding-agent", "@github/copilot", "opencode-ai", "@vibe-kit/grok-cli"])
EXT_RE = re.compile(r"\.(exe|cmd|bat|ps1|js|mjs)$")
PY_RE = re.compile(r"^(python[0-9.]*|pythonw[0-9.]*|py|pypy[0-9]*)$")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
PWSH = {"pwsh", "powershell"}
SKIP_WRAP = {"env", "nohup", "time", "exec", "sudo", "command", "builtin"}
RUNNERS = {"npx", "bunx"}
DIR_OPTS = {"--cd", "--prefix", "--cwd", "-workingdirectory", "--working-directory", "--workdir"}
DIR_SHORT = {"-C"}
CD = {"cd", "chdir", "pushd", "set-location", "sl", "push-location"}
REASON = ("Blocked (guardrail G-1): %s is an external coding tool and %s is a guarded repo "
          "(~/.mission-control/guarded-repos.json). External tools run only on the Lenovo: use "
          "python3 .claude/bin/super-board-lane-delegate.py delegate --tool %s ... (spec §15.4 D-2).")
BROKEN = " The guarded-repo list is missing or invalid, so every repo counts as guarded."


PY_NAME = re.compile(r"^(python[0-9.]*|pythonw[0-9.]*|py|pypy[0-9]*)$")


class Tok(str):
    pass


STATE = {"ps": False}       # tokenise with PowerShell rules (set from tool_name; nested shells switch it)


def segments(command):
    """Split a shell / PowerShell command into simple-command token lists. Quotes are honoured; nothing runs.
    PowerShell: backslash is literal, a backtick escapes, '' / "" inside quotes is a literal quote. Both: a word that
    starts with # begins a comment that runs to the end of the line."""
    ps = STATE["ps"]
    if ps:
        command = re.sub(r"<#.*?#>", " ", command, flags=re.S)       # block comments
    out, tokens, cur, has, quote = [], [], "", False, None
    i, n = 0, len(command)

    def push_token():
        nonlocal cur, has
        if has:
            tokens.append(Tok(cur))
        cur, has = "", False

    while i < n:
        c = command[i]
        if quote:
            if c == quote:
                if ps and i + 1 < n and command[i + 1] == quote:
                    cur += c
                    i += 1
                else:
                    quote = None
            elif ps and c == "`" and quote == '"' and i + 1 < n:
                i += 1
                cur += command[i]
            elif not ps and c == "\\" and quote == '"' and i + 1 < n and command[i + 1] in '"\\$`':
                i += 1
                cur += command[i]
            else:
                cur += c
        elif c in "'\"":
            quote, has = c, True
        elif c == "{" and cur.endswith("$"):                     # ${...}: one word, # inside is not a comment
            j = command.find("}", i)
            j = n - 1 if j < 0 else j
            cur += command[i:j + 1]
            i = j
        elif c == "#" and not has:
            while i + 1 < n and command[i + 1] != "\n":
                i += 1
        elif ps and c == "`" and i + 1 < n:
            i += 1
            cur += command[i]
            has = True
        elif not ps and c == "\\" and i + 1 < n and command[i + 1] in " \t\"'\\;&|()$`":
            i += 1
            cur += command[i]
            has = True
        elif c in " \t":
            push_token()
        elif c in ";\n|&(){}":
            push_token()
            if tokens:
                out.append(tokens)
            tokens = []
        else:
            cur += c
            has = True
        i += 1
    if quote:
        raise ValueError("unterminated quote")
    push_token()
    if tokens:
        out.append(tokens)
    return out


def base(token):
    """Lowercase basename with a known extension stripped; paths in any spelling."""
    b = mc_guarded.norm(token).rsplit("/", 1)[-1].lower()
    return EXT_RE.sub("", b)


def pkg_name(arg):
    """A package argument without its @version, lowercase."""
    return re.sub(r"(?<=.)@[^/@]*$", "", arg.strip().lower())


def covered_pkg(arg):
    n = pkg_name(arg)
    return n if (n in PACKAGES or n in COVERED) else None


EXEC_KW = re.compile(r"subprocess|os\.(system|popen|exec\w*|spawn\w*)|\bPopen|child_process|\bspawn|\bexec\w*\s*\(|"
                     r"\bsystem\s*\(|execa|Deno\.Command|shell_exec|\bexec\b")


def word_in(text):
    """A covered name as a whole word on a line of interpreter code that starts a process (subprocess, os.system,
    child_process, spawn, exec ...). Words on other lines (a variable `pi`, a comment) never count: telling code from
    strings and comments needs a real parser, and a process-starting call takes its command as a string."""
    for line in text.split("\n"):
        if not EXEC_KW.search(line):
            continue
        for pkg in sorted(PACKAGES):
            if re.search(r"(?<![A-Za-z0-9_.@/-])" + re.escape(pkg) + r"(?![A-Za-z0-9_-])", line, re.I):
                return pkg.rsplit("/", 1)[-1]
        for m in re.finditer(r"(?<![A-Za-z0-9_.@/-])([A-Za-z][A-Za-z0-9-]*)(?![A-Za-z0-9_-])", line):
            w = m.group(1).lower()
            if w in COVERED:
                return w
    return None


def delegate_script(token):
    """The tool a delegate script path names: relay.mjs, or a path segment ending -delegate/scripts/."""
    t = str(token).replace("\\", "/")
    m = re.search(r"([A-Za-z0-9-]+)-delegate/scripts/", t)
    if m:
        return m.group(1).lower()
    return "relay.mjs" if "relay.mjs" in t else None


def is_assign(t):
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) is not None


def dir_args(tokens):
    """Directories named by --cd X / --cd=X / -C X / --prefix X / --cwd X in one command."""
    out = []
    for i, t in enumerate(tokens):
        low = str(t).lower()
        if low.startswith("--") and "=" in low and low.split("=", 1)[0] in DIR_OPTS:
            out.append(str(t).split("=", 1)[1])
        elif (low in DIR_OPTS or t in DIR_SHORT) and i + 1 < len(tokens):
            out.append(str(tokens[i + 1]))
    return out


def find_tool(tokens, depth=0):
    """(tool, extra_dirs) when this simple command runs a covered tool, looking through wrappers; else None."""
    if depth > 6:
        raise ValueError("command nests too deeply")
    toks = list(tokens)
    while toks and is_assign(toks[0]):
        toks.pop(0)
    if not toks:
        return None
    name = base(toks[0])
    args = toks[1:]
    d = delegate_script(toks[0])
    if d:
        return d, dir_args(toks)
    if name in COVERED:
        return name, dir_args(toks)
    if name in SKIP_WRAP:
        rest = [a for a in args]
        while rest and (rest[0].startswith("-") or is_assign(rest[0])):
            rest.pop(0)
        return find_tool(rest, depth + 1) if rest else None
    if name == "wsl":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            f = rest.pop(0)
            if f in ("-d", "--distribution", "-u", "--user", "--cd") and rest:
                rest.pop(0)
            if f == "--":
                break
        return find_tool(rest, depth + 1) if rest else None
    if name == "cmd" and len(args) > 1 and args[0].lower() in ("/c", "/k", "/s"):
        return find_text(" ".join(args[1:]), depth + 1, False)
    if name in SHELLS | PWSH:
        for i, a in enumerate(args):
            if a.lower() in ("-c", "-command", "-commandwithargs") and i + 1 < len(args):
                return find_text(" ".join(args[i + 1:]), depth + 1, name in PWSH)
            if not a.startswith("-"):
                d = delegate_script(a)
                return (d, dir_args(toks)) if d else None
        return None
    if name == "start-process":
        prog, rest, i = None, [], 0
        while i < len(args):
            a = args[i]
            if a.lower() == "-filepath" and i + 1 < len(args):
                prog = args[i + 1]
                i += 2
            elif a.startswith("-"):
                i += 2 if a.lower() in ("-argumentlist", "-workingdirectory", "-verb", "-windowstyle") else 1
            else:
                if prog is None:
                    prog = a
                i += 1
        hit = find_tool([prog], depth + 1) if prog else None
        return (hit[0], hit[1] + dir_args(toks)) if hit else None
    if name in RUNNERS or name in ("pnpm", "yarn", "npm") or (name == "bun" and [a for a in args if not a.startswith("-")][:1] == ["x"]):
        if name in RUNNERS:
            return pkg_hit([a for a in args if a != "--"], toks)
        verbs = {"pnpm": "dlx", "yarn": "dlx", "npm": "exec", "bun": "x"}
        pos = [a for a in args if not a.startswith("-")]
        if pos and pos[0] == verbs[name]:
            i = args.index(pos[0])
            return pkg_hit([a for a in args[i + 1:] if a != "--"], toks)
        return None
    if name in ("node", "nodejs", "bun", "deno") or PY_RE.match(name):
        for i, a in enumerate(args):
            if a in ("-e", "--eval", "-p", "--print", "-c") and i + 1 < len(args):
                w = word_in(args[i + 1])
                return (w, dir_args(toks)) if w else None
            if a.startswith("-"):
                continue
            d, b = delegate_script(a), base(a)
            if d:
                return d, dir_args(toks)
            return (b, dir_args(toks)) if b in COVERED else None
    return None


def pkg_hit(rest, toks):
    for a in rest:
        if a.startswith("-"):
            continue
        n = covered_pkg(a) or delegate_script(a)
        return (n, dir_args(toks)) if n else None
    return None


HEREDOC_RE = re.compile(r"(?<!<)<<(?!<)[-~]?[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|([A-Za-z_][A-Za-z0-9_.-]*))")
ARITH_RE = re.compile(r"\(\(.*?\)\)")
CODE_READERS = {"python", "python3", "py", "node", "nodejs", "pythonw", "deno", "bun"}


def feeds(prefix):
    """How the command on a heredoc line reads its stdin: "shell" (bash, sh, zsh, pwsh: a script of commands), "code"
    (python / node with no script argument), or None (cat, git commit -F -, ...: plain data)."""
    try:
        segs = segments(prefix)
    except ValueError:
        return None
    if not segs:
        return None
    toks = list(segs[-1])
    while toks and (is_assign(toks[0]) or base(toks[0]) in SKIP_WRAP):
        toks.pop(0)
    if not toks or [a for a in toks[1:] if not a.startswith("-")]:
        return None
    name = base(toks[0])
    if name in SHELLS or name in PWSH:
        return "shell"
    return "code" if (name in CODE_READERS or PY_NAME.match(name)) else None


def strip_herestrings(command):
    """PowerShell here-strings: @' ... a line starting '@, and @" ... a line starting "@. The body is removed."""
    lines, out, i = command.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        i += 1
        m = re.search(r"@(['\"])[ \t\r]*$", line)
        if not m:
            out.append(line)
            continue
        out.append(line[:m.start()] + "HERESTRING")
        while i < len(lines):
            body = lines[i]
            i += 1
            if body.startswith(m.group(1) + "@"):
                if body[2:].strip():
                    out.append(body[2:])
                break
    return "\n".join(out)


def strip_heredocs(command):
    """(text, code_bodies). Heredoc bodies and their end lines are removed so they are not tokenised as commands. A body
    fed to a shell stays in the text (minus its end line) and is scanned as commands; a body fed to python / node is
    returned in code_bodies. << inside (( )) is arithmetic. Unterminated: the rest is consumed."""
    if STATE["ps"]:
        command = strip_herestrings(command)
    lines, out, bodies, i = command.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        probe = ARITH_RE.sub(lambda m: " " * len(m.group(0)), line)
        pending = []
        for m in HEREDOC_RE.finditer(probe):
            delim = next(g for g in m.groups() if g is not None)
            pending.append((delim, m.group(0)[2:3], feeds(line[:m.start()])))
        for delim, mode, kind in pending:
            code = []
            while i < len(lines):
                body = lines[i]
                i += 1
                chk = body.rstrip("\r")
                chk = chk.lstrip("\t") if mode == "-" else chk.lstrip() if mode == "~" else chk
                if chk == delim:
                    break
                if kind == "shell":
                    out.append(body)
                else:
                    code.append(body)
            if kind == "code" and code:
                bodies.append("\n".join(code))
    return "\n".join(out), bodies


def find_text(text, depth, ps=False):
    """find_tool over every simple command of a nested command string, tracking cd."""
    saved = STATE["ps"]
    STATE["ps"] = ps
    try:
        hits = scan(text, None, depth)
    finally:
        STATE["ps"] = saved
    return hits[0] if hits else None


def scan(command, cwd, depth=0):
    """All (tool, dirs) hits of a command line. dirs: directories the call acts in (cd-tracked cwd plus --cd style targets)."""
    hits = []
    cur = cwd
    for tokens in segments(command):
        name = base(tokens[0]) if tokens else ""
        if name in CD:
            rest = [t for t in tokens[1:] if not t.startswith("-") or t == "-"]
            if rest and "$" not in rest[0] and rest[0] != "-":
                target = mc_guarded.norm(rest[0])
                if cur is not None or target.startswith("/") or re.match(r"^[A-Za-z]:/", target):
                    cur = resolve(cur or "/", rest[0])
            continue
        hit = find_tool(tokens, depth)
        if hit:
            dirs = [resolve(cur, d) if cur is not None else mc_guarded.norm(d) for d in hit[1]]
            if cur is not None:
                dirs.append(cur)
            hits.append((hit[0], dirs))
    return hits


def resolve(base_dir, raw):
    raw = re.sub(r"^~(?=/|\\|$)", lambda m: mc_guarded.home().replace("\\", "/"), raw)
    p = mc_guarded.norm(raw)
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        return p
    return posixpath.normpath(posixpath.join(base_dir, p))


def deny(tool, where, broken):
    reason = REASON % (tool, where, tool) + (BROKEN if broken else "")
    print(reason, file=sys.stderr)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 2


def mcp_servers(project, cwd):
    """{server name: config} from ~/.claude.json (top level and projects[project]) and the project dir's .mcp.json. Unreadable
    files are skipped: the config is not the boundary."""
    out = {}

    def take(d):
        if isinstance(d, dict) and isinstance(d.get("mcpServers"), dict):
            for k, v in d["mcpServers"].items():
                if isinstance(v, dict):
                    out.setdefault(k, v)

    try:
        with open(os.path.join(mc_guarded.home(), ".claude.json"), encoding="utf-8-sig") as fh:
            data = json.load(fh)
        take(data)
        wanted = {mc_guarded.norm(p).lower().rstrip("/") for p in (project, cwd) if p}
        for key, val in (data.get("projects") or {}).items():
            if mc_guarded.norm(key).lower().rstrip("/") in wanted:
                take(val)
    except (OSError, ValueError, AttributeError):
        pass
    for d in (project, cwd):
        if not d:
            continue
        try:
            with open(os.path.join(d, ".mcp.json"), encoding="utf-8-sig") as fh:
                take(json.load(fh))
            break
        except (OSError, ValueError):
            continue
    return out


def server_tool(cfg):
    """The covered tool a configured MCP server runs, or None."""
    cmd = cfg.get("command")
    args = [a for a in (cfg.get("args") or []) if isinstance(a, str)]
    if isinstance(cmd, str):
        if base(cmd) in COVERED:
            return base(cmd)
        hit = find_tool([cmd] + args)
        if hit:
            return hit[0]
    for a in args:
        n = covered_pkg(a) or (base(a) if base(a) in COVERED else None)
        if n:
            return n
    return None


def session_start(project, cwd):
    where = project or cwd
    if not where or not mc_guarded.is_guarded(where):
        return 0
    names = sorted(k for k, v in mcp_servers(project, cwd).items() if server_tool(v))
    if names:
        print("WARNING (guardrail G-1): MCP server(s) %s run an external coding tool in a guarded repo; their tools will be "
              "blocked. External tools run only on the Lenovo (spec section 15.4 D-2)." % ", ".join(names))
    return 0


def main():
    payload = None
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        project = os.environ.get("CLAUDE_PROJECT_DIR") or ""
        cwd = mc_guarded.norm(payload.get("cwd") or os.getcwd())
        event = payload.get("hook_event_name")
        tool = payload.get("tool_name")
        if event == "SessionStart":
            return session_start(project, cwd)
        broken = mc_guarded.load() is None
        if isinstance(tool, str) and tool.startswith("mcp__"):
            parts = tool.split("__", 2)
            server = parts[1] if len(parts) > 2 else ""
            cfg = mcp_servers(project, cwd).get(server)
            t = server_tool(cfg) if cfg else None
            if not t:
                return 0
            where = project or cwd
            return deny(t, where, broken) if mc_guarded.is_guarded(where) else 0
        if tool not in ("Bash", "PowerShell"):
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            raise ValueError("no command")
        home_guarded = next((d for d in ([project] if project else []) + [cwd] if mc_guarded.is_guarded(d)), None)
        STATE["ps"] = tool == "PowerShell"
        try:
            text, bodies = strip_heredocs(command)
            hits = scan(text, cwd)
            hits += [(w, [cwd]) for w in (word_in(b) for b in bodies) if w]
        except Exception:
            if home_guarded:
                raise
            return 0            # cannot parse it, and neither the project nor the cwd is guarded
        for t, dirs in hits:
            for d in ([project] if project else []) + [cwd] + dirs:
                if mc_guarded.is_guarded(d):
                    return deny(t, d, broken)
        return 0
    except Exception as e:      # fail closed
        if payload is not None and isinstance(payload, dict) and payload.get("hook_event_name") == "SessionStart":
            return 0
        print("Blocked (guardrail G-1): guard-external-tools could not evaluate this call (%s: %s); it fails closed."
              % (type(e).__name__, e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
