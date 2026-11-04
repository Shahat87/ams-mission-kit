#!/usr/bin/env python3
"""GUARDRAIL: catches habit and mistakes; not a security boundary (spec §15.3).

guard-taint (G-2, as amended by F6), project scope, PreToolUse Bash|PowerShell. In a guarded repo it keeps tool-written
code off this host's disk.

Taint facts come from the main checkout's .claude/super-board/inflight/:
  sandbox-taint-*.json   {branch, pr, shas}      tainted branch, PR number, shas
  sandbox-delegate-*     {tool, nonce_sha256, commit, pr}
  sandbox-NUM            card NUM is tainted; its worktree is .claude/worktrees/issue-NUM
Any branch matching mc-delegate/* is tainted.

  Rule A (by worktree, F6): in a tainted worktree (card marker, or HEAD / upstream is a tainted branch) deny pull, merge,
         rebase, reset --hard, checkout, switch, cherry-pick, restore --source, apply, read-tree -u, whatever the arguments.
  Rule B (by ref text): anywhere in the guarded repo, deny checkout, switch, worktree add, merge, rebase, cherry-pick, pull,
         reset --hard, restore --source and apply when an argument names a tainted branch or a tainted sha (7+ hex prefix).
  Rule C: deny `gh pr checkout` of a tainted PR number or branch.

Deny = reason on stderr and exit 2; any exception = exit 2. No subprocess is ever started (git included): .git files are read.
"""
import glob
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

REASON = ("Blocked (guardrail G-2): %s would put tool-written code on disk (tainted: %s). Read it with "
          ".claude/bin/super-board-git.sh show <sha>, test it with exec --sandbox --ref <sha> (spec §15.2 O-1/O-2).")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
PWSH = {"pwsh", "powershell"}
SKIP_WRAP = {"env", "nohup", "time", "exec", "sudo", "command", "builtin"}
CD = {"cd", "chdir", "pushd", "set-location", "sl", "push-location"}
EXT_RE = re.compile(r"\.(exe|cmd|bat|com)$")
CARD_RE = re.compile(r"/\.claude/worktrees/issue-(\d+)(?:-[^/]*)?$", re.I)
HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")
A_SUBS = {"pull", "merge", "rebase", "checkout", "switch", "cherry-pick", "apply"}
B_SUBS = {"checkout", "switch", "merge", "rebase", "cherry-pick", "pull", "apply"}


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
            tokens.append(cur)
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
    return EXT_RE.sub("", mc_guarded.norm(token).rsplit("/", 1)[-1].lower())


def is_assign(t):
    return re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) is not None


def resolve(base_dir, raw):
    raw = re.sub(r"^~(?=/|\\|$)", lambda m: mc_guarded.home().replace("\\", "/"), raw)
    p = mc_guarded.norm(raw)
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        return p
    return posixpath.normpath(posixpath.join(base_dir, p))


PY_NAME = re.compile(r"^(python[0-9.]*|pythonw[0-9.]*|py|pypy[0-9]*)$")
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


class Facts:
    """The taint facts of one guarded repo, read from the main checkout's inflight directory."""

    def __init__(self, project):
        info = mc_guarded.repo_info(project)
        self.branches, self.prs, self.shas, self.cards = set(), set(), set(), set()
        self.common = info[2] if info else None
        self.remotes = {"origin"}
        self.any_record = False
        if not info:
            return
        try:
            with open(self.common + "/config", encoding="utf-8-sig", errors="replace") as fh:
                self.remotes |= set(re.findall(r'^\s*\[\s*remote\s+"([^"]+)"\s*\]', fh.read(), re.M))
        except OSError:
            pass
        main = posixpath.dirname(self.common) if self.common.endswith("/.git") else info[0]
        inflight = main + "/.claude/super-board/inflight"
        for f in sorted(glob.glob(glob.escape(inflight) + "/sandbox-*")):
            name = f.replace("\\", "/").rsplit("/", 1)[-1]
            self.any_record = True
            if name.startswith("sandbox-taint-") and name.endswith(".json"):
                self.record(self.read_json(f), ("branch",), ("shas", "sha", "commit"))
            elif name.startswith("sandbox-delegate-"):
                self.record(self.read_json(f), (), ("commit", "shas", "sha"))
            else:
                m = re.fullmatch(r"sandbox-(\d+)", name)
                if m:
                    self.cards.add(m.group(1))
                    data = self.read_json(f, tolerant=True)
                    if isinstance(data, dict):
                        self.record(data, (), ("commit",))

    @staticmethod
    def read_json(path, tolerant=False):
        try:
            with open(path, encoding="utf-8-sig") as fh:
                return json.load(fh)
        except ValueError:
            if tolerant:
                return None
            raise

    def record(self, data, branch_keys, sha_keys):
        if not isinstance(data, dict):
            raise ValueError("taint record is not an object")
        for k in branch_keys:
            v = data.get(k)
            if isinstance(v, str) and v:
                self.branches.add(v)
        if data.get("pr") not in (None, ""):
            self.prs.add(str(data["pr"]))
        for k in sha_keys:
            v = data.get(k)
            for s in (v if isinstance(v, list) else [v]):
                if isinstance(s, str) and HEX_RE.match(s.lower()):
                    self.shas.add(s.lower())

    def tainted_branch(self, name):
        if not name:
            return False
        name = re.sub(r"^refs/remotes/[^/]+/", "", name)
        if name.startswith("refs/heads/"):
            name = name[len("refs/heads/"):]
        else:
            for r in self.remotes:
                if name.startswith(r + "/"):
                    name = name[len(r) + 1:]
                    break
        return name.startswith("mc-delegate/") or name in self.branches

    def tainted_sha(self, text):
        t = text.lower()
        return bool(HEX_RE.match(t)) and any(s.startswith(t) for s in self.shas)

    def names_taint(self, arg):
        """What a git argument names that is tainted (a description), or None."""
        a = arg
        if a.startswith("-"):
            if "=" not in a:
                return None
            a = a.split("=", 1)[1]
        a = re.split(r"[~^]|@\{", a, maxsplit=1)[0]
        if self.tainted_branch(a):
            return a
        if self.tainted_sha(a):
            return a[:12]
        return None

    def worktree_taint(self, d):
        """Why the worktree holding d is tainted (a description), or None."""
        info = mc_guarded.repo_info(d)
        if not info:
            return None
        top, gitdir, common = info
        m = CARD_RE.search(top)
        if m and m.group(1) in self.cards:
            return "worktree issue-%s (marker sandbox-%s)" % (m.group(1), m.group(1))
        try:
            with open(gitdir + "/HEAD", encoding="utf-8", errors="replace") as fh:
                head = fh.readline().strip()
        except OSError:
            head = ""
        branch = head[len("ref: refs/heads/"):] if head.startswith("ref: refs/heads/") else None
        if branch is not None:
            if self.tainted_branch(branch):
                return "branch " + branch
            up = upstream(common, branch)
            if up and self.tainted_branch(up):
                return "upstream " + up
        elif self.tainted_sha(head):
            return "HEAD " + head[:12]
        return None


def upstream(common, branch):
    """The branch name `branch.<b>.merge` points at in the common config, else None."""
    section, found = None, None
    try:
        with open(common + "/config", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for line in lines:
        m = re.match(r'^\s*\[\s*branch\s+"((?:[^"\\]|\\.)*)"\s*\]', line)
        if m:
            section = re.sub(r"\\(.)", r"\1", m.group(1))
            continue
        if re.match(r"^\s*\[", line):
            section = None
            continue
        if section == branch:
            k, _, v = line.partition("=")
            if k.strip().lower() == "merge":
                v = re.split(r"[#;]", v, maxsplit=1)[0].strip().strip('"')
                found = v[len("refs/heads/"):] if v.startswith("refs/heads/") else v
    return found


def git_parts(args, cur):
    """(sub, rest, dir) of a git command line: global options skipped, -C applied to the directory."""
    i, d = 0, cur
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            d = resolve(d, args[i + 1])
            i += 2
        elif a.startswith("-C") and not a.startswith("--") and len(a) > 2:
            d = resolve(d, a[2:])
            i += 1
        elif a in ("-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") and "=" not in a:
            i += 2
        else:
            i += 1
    return (args[i] if i < len(args) else ""), args[i + 1:], d


def deny(what, tainted):
    reason = REASON % (what, tainted)
    print(reason, file=sys.stderr)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    return 2


class Lazy:
    """Builds the taint facts on first use, so only git / gh commands ever read (or trip over) the taint records."""

    def __init__(self, project):
        self.project, self.value = project, None

    def get(self):
        if self.value is None:
            self.value = Facts(self.project)
        return self.value


def judge(tokens, cur, lazy):
    """(what, tainted) when this git / gh command is denied, else None."""
    toks = list(tokens)
    while toks and is_assign(toks[0]):
        toks.pop(0)
    while toks and base(toks[0]) in SKIP_WRAP:
        toks.pop(0)
        while toks and (toks[0].startswith("-") or is_assign(toks[0])):
            toks.pop(0)
    if not toks:
        return None
    name, args = base(toks[0]), toks[1:]
    if name not in ("git", "gh"):
        return None
    facts = lazy.get()
    if name == "gh":
        pos = [a for a in args if not a.startswith("-")]
        if any(pos[i:i + 2] == ["pr", "checkout"] for i in range(len(pos))):
            after, tail, k = args[args.index("checkout") + 1:], [], 0
            while k < len(after):
                if after[k] in ("-R", "--repo", "-b", "--branch"):
                    k += 2
                    continue
                if not after[k].startswith("-"):
                    tail.append(after[k])
                k += 1
            if tail:
                t = tail[0]
                m = re.search(r"/pull/(\d+)", t)
                num = m.group(1) if m else (t if t.isdigit() else None)
                if num is not None and num in facts.prs:
                    return "`gh pr checkout %s`" % t, "PR #%s" % num
                if num is None and facts.tainted_branch(t.split(":", 1)[-1]):
                    return "`gh pr checkout %s`" % t, t
        return None
    if name != "git":
        return None
    sub, rest, d = git_parts(args, cur)
    low = [a.lower() for a in rest]
    hard_reset = sub == "reset" and "--hard" in low
    restore_src = sub == "restore" and any(a == "-s" or a.startswith("--source") for a in rest)
    read_tree_u = sub == "read-tree" and any(re.match(r"^-[A-Za-z]*u", a) for a in rest)
    wt_add = sub == "worktree" and bool(rest) and rest[0] == "add"
    why = facts.worktree_taint(d)
    if why and (sub in A_SUBS or hard_reset or restore_src or read_tree_u):
        return "`git %s`" % sub, why
    if sub in B_SUBS or hard_reset or restore_src or wt_add or read_tree_u:
        for a in rest:
            hit = facts.names_taint(a)
            if hit:
                return "`git %s %s`" % (sub, a), hit
    return None


def scan(command, cwd, facts, depth=0):
    if depth > 6:
        raise ValueError("command nests too deeply")
    cur = cwd
    for tokens in segments(command):
        name = base(tokens[0])
        if name in CD:
            rest = [t for t in tokens[1:] if not t.startswith("-") or t == "-"]
            if rest and "$" not in rest[0] and rest[0] != "-":
                cur = resolve(cur, rest[0])
            continue
        args = tokens[1:]
        inner = None
        if name in SHELLS | PWSH:
            for i, a in enumerate(args):
                if a.lower() in ("-c", "-command", "-commandwithargs") and i + 1 < len(args):
                    inner = " ".join(args[i + 1:])
                    break
        elif name == "cmd" and len(args) > 1 and args[0].lower() in ("/c", "/k", "/s"):
            inner = " ".join(args[1:])
        elif name == "wsl":
            rest = list(args)
            while rest and rest[0].startswith("-"):
                f = rest.pop(0)
                if f in ("-d", "--distribution", "-u", "--user", "--cd") and rest:
                    rest.pop(0)
                if f == "--":
                    break
            inner = " ".join(rest) if rest else None
        if inner is not None:
            saved = STATE["ps"]
            STATE["ps"] = name in PWSH
            try:
                r = scan(inner, cur, facts, depth + 1)
            finally:
                STATE["ps"] = saved
            if r:
                return r
            continue
        r = judge(tokens, cur, facts)
        if r:
            return r
    return None


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        if payload.get("tool_name") not in (None, "Bash", "PowerShell"):
            return 0
        cwd = mc_guarded.norm(payload.get("cwd") or os.getcwd())
        project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
        if not mc_guarded.is_guarded(project):
            return 0
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            raise ValueError("no command")
        STATE["ps"] = payload.get("tool_name") == "PowerShell"
        try:
            lazy = Lazy(project)
            hit = scan(strip_heredocs(command)[0], cwd, lazy)
        except Exception:
            # Parsing failed (or a taint record is unreadable). Deny only a call that holds git or gh as a word: G-2 must
            # do nothing in a repo with no taint, so a call with no git / gh never trips over its own parser.
            if re.search(r"\b(git|gh)(\.exe)?\b", command) and ("mc-delegate" in command or lazy.get().any_record):
                raise
            return 0
        return deny(*hit) if hit else 0
    except Exception as e:      # fail closed
        print("Blocked (guardrail G-2): guard-taint could not evaluate this call (%s: %s); it fails closed."
              % (type(e).__name__, e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
