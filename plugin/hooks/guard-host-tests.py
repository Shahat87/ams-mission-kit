#!/usr/bin/env python3
"""PreToolUse(Bash|PowerShell) guard: with pilot.sandbox.provider "docker", a card worktree that holds a tool's patch
never runs a test, build, install or server command on this host.

External CLIs (Codex, Grok, Pi) write code in a container; lane-delegate `verify` applies the patch to the card worktree
(.claude/worktrees/issue-<number>) and drops a marker `.claude/super-board/inflight/sandbox-<number>` in the main repo. While a marker
exists, any command that is IN or POINTS INTO a marked worktree is "hot" and is judged by an ALLOWLIST, conservatively: a
whole class is denied rather than parsed. Hot means: the cwd is a marked worktree, or the text mentions a worktree path in
any spelling (worktrees, issue-N, globs, 8.3 `~1` names), or a cd moved somewhere unknown / into a marked worktree, or a
command substitution or a `$`-path is used. Outside that the guard is inert.

Hot commands may only run: git (known subcommands, no hook / pager / exec / external-diff routes), gh, python / bash / sh /
direct calls of the installed `.claude/bin/super-board-<name>.(sh|py)` scripts (exact names), and read-only tools (cat, ls,
dir, head, tail, wc, grep, rg, find, sed -n, diff, echo, printf, pwd, Get-Content, Select-String, Get-ChildItem, Test-Path,
Get-Item, Resolve-Path). No assignments, command substitution, call operators, redirection (except to the null device), no
pipe into anything that is not on that list. Anything else gets the `exec --sandbox` message. The guard fails CLOSED.
"""
import glob
import json
import os
import posixpath
import re
import sys

REASON = ("Blocked: this card worktree holds code an external tool wrote (marker .claude/super-board/inflight/sandbox-%s); "
          "%s must not run on this host.\n"
          "docker mode: run it with `python3 .claude/bin/super-board-lane-delegate.py exec --sandbox --config <cfg> "
          "--dir <worktree> -- <cmd>`")
CARD_RE = re.compile(r"^(?P<root>.*?)/\.claude/worktrees/issue-(?P<num>\d+)(?:-[^/]*)?(?:/|$)", re.I)
CD = {"cd", "chdir", "pushd", "popd", "set-location", "sl", "push-location", "pop-location"}
DIR_OPTS = {"-C", "--prefix", "--dir", "--cwd", "--cd", "--directory", "--workdir", "--root", "--project",
            "--working-directory", "--manifest-path"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "pwsh", "powershell"}
PY_RE = re.compile(r"^(python[0-9.]*|pythonw[0-9.]*|py|pypy[0-9]*)$")
# The exact board scripts install.sh copies to .claude/bin (tests/test_guard_host_tests.py fails when this drifts from it).
BIN_SCRIPTS = frozenset("""
super-board-agents-md.py super-board-approval.py super-board-calibration.py super-board-card.sh super-board-codex-lane.sh
super-board-codex-wave.sh super-board-collect-state.py super-board-deps.sh super-board-env-check.sh super-board-gh-guard.sh
super-board-github-read.py super-board-host.sh super-board-lane-allowpath.py super-board-lane-delegate.py
super-board-events.py super-board-lane-docker.py super-board-lane-env.py super-board-lane-integrity.py super-board-lane-paths.py
super-board-lane-remote.py super-board-lane-signatures.py super-board-merge-gate.sh super-board-merge-policy.py
super-board-pr-body.sh super-board-preflight.sh super-board-project-verifier.py super-board-run.sh super-board-settings.py
super-board-roadmap.py super-board-autopilot-batch.py super-board-setup.py super-board-skills-ready.py super-board-status.py super-board-stop.sh super-board-throttle.sh
super-board-orch.sh super-board-usage.sh super-board-verification-ready.py super-board-wave-plan.sh super-board-git.sh super-board-objects.py
super-board-watchdog.py super-board-watchdog-install.sh
""".split())
BIN_RE = re.compile(r"(?P<pre>.*?)\.claude/bin/(?P<name>super-board-[A-Za-z0-9_-]+\.(sh|py))")
MENTION_RE = re.compile(r"worktr|issue[-~*?\[]|~\d|(^|[\s'\"=/])\.[a-z]*[*?\[]|\.claude/\S*[*?\[]", re.I)
SUBST_RE = re.compile(r"\$\(|`|\$\{|<\(|>\(")
GIT_SUBS = {"status", "add", "commit", "push", "pull", "fetch", "log", "diff", "show", "branch", "checkout", "switch", "restore",
            "reset", "stash", "rebase", "merge", "cherry-pick", "revert", "tag", "remote", "rev-parse", "rev-list", "ls-files",
            "ls-tree", "cat-file", "blame", "grep", "config", "worktree", "clean", "mv", "rm", "apply", "am", "format-patch",
            "describe", "shortlog", "reflog", "symbolic-ref", "update-ref", "for-each-ref", "merge-base", "name-rev", "diff-tree",
            "diff-index", "show-ref", "check-ignore", "init", "clone", "version", "count-objects", "fsck", "whatchanged"}
GIT_BAD_KEY = re.compile(r"^(alias\.|core\.(hookspath|fsmonitor|pager|sshcommand|editor|askpass|gitproxy|attributesfile|excludesfile)"
                         r"|diff\.external|.*\.textconv|filter\..*|merge\..*\.(driver|name)|diff\..*\.command|credential\..*"
                         r"|sequence\.editor|include\..*|includeif\..*|url\..*|gpg\..*|protocol\..*)$", re.I)
GIT_CONFIG_READ = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch", "--get-color", "--get-colorbool"}
GIT_CONFIG_WRITE = {"--add", "--unset", "--unset-all", "--replace-all", "--edit", "-e", "--rename-section", "--remove-section",
                    "--set", "--file-write"}
FIND_BAD = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}
SED_SCRIPT = re.compile(r"^((\d+|\$)(,(\d+|\$))?|/[^/\\;{}]*/)?(p|=)$|^(\d+|\$)q$")
READONLY = {"cat", "ls", "dir", "head", "tail", "wc", "echo", "printf", "pwd", "get-location", "get-content", "gc", "type",
            "select-string", "sls", "get-childitem", "gci", "test-path", "get-item", "resolve-path"}
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=|^\$env:", re.I)


def msys(p):
    """Git Bash paths (/c/Users/x) -> C:/Users/x on Windows, so os.path sees the real location."""
    if os.name != "nt" or not p.startswith("/"):
        return p
    p = posixpath.normpath(p)
    m = re.match(r"^/([A-Za-z])(?:/(.*))?$", p)
    return "%s:/%s" % (m.group(1), m.group(2) or "") if m else p


def norm(p):
    return posixpath.normpath(msys(p.replace("\\", "/")))


def absolute(p):
    return p.startswith("/") or re.match(r"^[A-Za-z]:/", p) is not None


class Tok(str):
    """A command-line word. odd: it was quoted, escaped or held $ / a backtick, so its text is not what the shell will run."""
    odd = False
    mixed = False


def segments(command):
    """Split a shell / PowerShell command into simple-command token lists. Quotes are honoured; nothing runs."""
    out, tokens, cur, has, quote, odd, mixed, closed = [], [], "", False, None, False, False, False
    i = 0

    def push_token():
        nonlocal cur, has, odd, mixed, closed
        if has:
            t = Tok(cur)
            t.odd, t.mixed = odd, mixed
            tokens.append(t)
        cur, has, odd, mixed, closed = "", False, False, False, False

    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote, closed = None, True
            elif c == "\\" and quote == '"' and i + 1 < len(command) and command[i + 1] in '"\\$`':
                i += 1
                cur += command[i]
            else:
                cur += c
        elif c in "'\"":
            mixed = mixed or has
            quote, has, odd = c, True, True
        elif c == "\\" and i + 1 < len(command) and command[i + 1] in " \t\"'\\;&|()$`":
            i += 1
            cur += command[i]
            has, odd, mixed = True, True, True
        elif c in " \t":
            push_token()
        elif c in ";\n|&(){}":
            push_token()
            if tokens:
                out.append(tokens)
            tokens = []
        else:
            cur += c
            mixed = mixed or closed
            has = True
            if c in "$`":
                odd = True
        i += 1
    if quote:
        raise ValueError("unterminated quote")
    push_token()
    if tokens:
        out.append(tokens)
    return out


def views(command):
    """(outside_all_quotes, outside_single_quotes): the command with quoted text blanked. Single quotes are literal in
    bash and PowerShell; double quotes still substitute, so substitution is looked for outside single quotes only."""
    a, b, quote = [], [], None
    for c in command:
        if quote:
            if c == quote:
                a.append(" ")
                b.append(c if quote == '"' else " ")
                quote = None
            else:
                a.append(" ")
                b.append(" " if quote == "'" else c)
        elif c in "'\"":
            quote = c
            a.append(" ")
            b.append(" " if c == "'" else c)
        else:
            a.append(c)
            b.append(c)
    return "".join(a), "".join(b)


def expand(word, env):
    """~ and known $VAR; None when the value is only known at run time."""
    s = re.sub(r"^~(?=/|$)", lambda m: env.get("HOME", "~"), word)
    unknown = []

    def var(m):
        name = m.group(1) or m.group(2)
        if name not in env:
            unknown.append(name)
        return env.get(name, "")

    s = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", var, s)
    return None if unknown or "$" in s or "`" in s else s


def exe_name(token):
    base = norm(token).rsplit("/", 1)[-1].lower()
    return re.sub(r"\.(exe|cmd|bat|com)$", "", base)


class Ctx:
    def __init__(self, env):
        self.env = env
        self.root = None            # the repo root of the payload cwd, when known
        self.tool = "Bash"
        self.cache = {}

    @staticmethod
    def card(path):
        """(root, n) when path is inside .claude/worktrees/issue-<number>."""
        m = CARD_RE.match(path) if path else None
        return (m.group("root"), m.group("num")) if m else None

    def provider_docker(self, root):
        """True when the board config says provider docker. Unreadable config, or none at all: True (fail closed)."""
        if root in self.cache:
            return self.cache[root]
        base = root + "/.claude/super-board"
        names = []
        try:
            with open(base + "/active", encoding="utf-8") as fh:
                names = [base + "/configs/" + fh.read().strip() + ".json"]
        except OSError:
            pass
        if not names:
            names = sorted(glob.glob(base + "/configs/*.json"))
        verdict = True
        for n in names:
            try:
                with open(n, encoding="utf-8") as fh:
                    cfg = json.load(fh)
                sandbox = (cfg.get("pilot") or {}).get("sandbox") or {}
                provider = sandbox.get("provider")
            except (OSError, ValueError, AttributeError):
                self.cache[root] = True
                return True
            if provider == "docker":
                self.cache[root] = True
                return True
            verdict = False
        self.cache[root] = verdict
        return verdict

    def marked(self, path):
        """The card number when path is inside a card worktree whose tool patch is in place, else None."""
        c = self.card(path)
        if not c:
            return None
        root, n = c
        if os.path.isfile(root + "/.claude/super-board/inflight/sandbox-" + n) and self.provider_docker(root):
            return n
        return None

    def any_marker(self, path):
        """The number of some sandboxed card of this repo, or None."""
        c = self.card(path) if path else None
        root = c[0] if c else self.root
        if not root:
            return None
        hits = sorted(glob.glob(root + "/.claude/super-board/inflight/sandbox-*"))
        if hits and self.provider_docker(root):
            return hits[0].rsplit("sandbox-", 1)[-1]
        return None


def find_root(cwd):
    """The repo root of a path: the part before .claude/worktrees/, else the nearest parent holding .claude/super-board."""
    m = CARD_RE.match(cwd + "/")
    if m:
        return m.group("root")
    d = cwd
    while True:
        if os.path.isdir(d + "/.claude/super-board"):
            return d
        parent = posixpath.dirname(d)
        if parent == d:
            return None
        d = parent


def resolve(base_dir, raw, env):
    """Absolute normalised path of raw relative to base_dir, or None when it is only known at run time."""
    ex = expand(raw, env)
    if ex is None:
        return None
    ex = msys(ex.replace("\\", "/"))
    if absolute(ex):
        return posixpath.normpath(ex)
    return None if base_dir is None else posixpath.normpath(posixpath.join(base_dir, ex))


def real(p):
    """The path with symlinks / junctions resolved (when it exists), forward slashes."""
    try:
        return os.path.realpath(p).replace("\\", "/")
    except (OSError, ValueError):
        return p


def is_marked(ctx, p):
    return ctx.marked(p) or ctx.marked(real(p)) if p else None


INTERP_EVAL = {"-c", "-e", "--eval", "-p", "--print", "-command", "-commandwithargs", "-encodedcommand", "-ec"}


def evals_text(tokens):
    """python -c, node -e / -p, perl -e, ruby -e, deno eval, bun -e, pwsh -c ...: a script given on the command line."""
    name, args = exe_name(tokens[0]), [a.lower() for a in tokens[1:]]
    if PY_RE.match(name) or name in ("node", "nodejs", "perl", "ruby", "bun", "bunx", "deno", "php", "lua") or name in SHELLS:
        return any(a in INTERP_EVAL or (a.startswith("-") and not a.startswith("--") and len(a) > 2 and a[-1] in "ce" and name == "perl")
                   for a in args) or (name == "deno" and "eval" in args)
    return False


def cd_target(tokens):
    """The directory a cd-like segment names, or False when it is not a plain path (bare cd, `-`, popd, ~N, glob, $var)."""
    if exe_name(tokens[0]) in ("popd", "pop-location"):
        return False
    rest = [t for t in tokens[1:] if (not t.startswith("-") or t == "-") and t.lower() != "/d"]
    if not rest:
        return False
    t = rest[0]
    if t == "-" or re.match(r"^~[+\-\d]", t) or any(c in t for c in "*?[]") or "$" in t:
        return False
    return t


def nested(tokens):
    """The inner command string of a nested shell (bash -c, pwsh -Command, cmd /c), or None."""
    name, args = exe_name(tokens[0]), tokens[1:]
    if name in SHELLS:
        for i, a in enumerate(args):
            if a.lower() in ("-c", "-command", "-commandwithargs") and i + 1 < len(args):
                return " ".join(args[i + 1:])
        return None
    if name == "cmd" and len(args) > 1 and args[0].lower() in ("/c", "/k", "/s"):
        return " ".join(args[1:])
    return None


def hot(command, d, ctx, depth=0):
    """Does this command run in, or point into, a marked worktree (conservatively)? Follows cd and nested shell strings."""
    if depth > 4:
        return True
    unq, nosingle = views(command)
    folded = re.sub(r"['\"]\s*\+\s*['\"]", "", command)          # 'a'+'b' -> ab
    if SUBST_RE.search(nosingle) or MENTION_RE.search(command.replace("\\", "/")) or MENTION_RE.search(folded.replace("\\", "/")):
        return True
    if ctx.tool != "PowerShell" and "\\" in unq:                    # a backslash inside a bash word hides characters
        return True
    if ctx.tool == "PowerShell" and (re.search(r"['\"]\s*\+|\+\s*['\"]", command) or re.search(r"\([^)]*\+", command)):
        return True
    if is_marked(ctx, d):
        return True
    for tokens in segments(command):
        joined = " ".join(tokens)
        if MENTION_RE.search(joined.replace("\\", "/")) or any(t.mixed for t in tokens):
            return True
        if exe_name(tokens[0]) in CD:
            t = cd_target(tokens)
            d = resolve(d, t, ctx.env) if t else None
            if d is None or is_marked(ctx, d):
                return True
            continue
        if evals_text(tokens):
            return True
        prev = ""
        for t in tokens:
            if "$" in t and expand(t, ctx.env) is None and ("/" in t or "\\" in t or prev in DIR_OPTS):
                return True
            val = t.split("=", 1)[1] if t.startswith("--") and "=" in t and t.split("=", 1)[0] in DIR_OPTS else (t if prev in DIR_OPTS else None)
            if val and is_marked(ctx, resolve(d, val, ctx.env)):
                return True
            prev = t
        inner = nested(tokens)
        if inner is not None and hot(inner, d, ctx, depth + 1):
            return True
    return False


def bin_ok(text, ctx, card_root=None):
    """Exactly `.claude/bin/super-board-<name>.(sh|py)` with a name from BIN_SCRIPTS, relative to the cwd, or under
    $CLAUDE_PROJECT_DIR, or under the absolute project path: no `..`, no glob, no other `$`."""
    if not isinstance(text, str) or ".." in text or any(c in text for c in "*?[]~`\\ "):
        return False
    m = BIN_RE.fullmatch(text)
    if not m or m.group("name") not in BIN_SCRIPTS:
        return False
    pre = m.group("pre")
    if pre in ("", "$CLAUDE_PROJECT_DIR/"):
        return True
    roots = {r.lower() for r in (ctx.root, card_root) if r}
    return "$" not in pre and pre.endswith("/") and norm(pre).rstrip("/").lower() in {norm(r).lower() for r in roots}


def git_rule(args, d, ctx, deny):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-c" and i + 1 < len(args):
            if GIT_BAD_KEY.match(args[i + 1].split("=", 1)[0]):
                return deny("`git -c %s` (it can run a command)" % args[i + 1].split("=", 1)[0])
            i += 2
        elif a.startswith(("--config-env", "--exec", "--upload-pack", "--receive-pack", "--uploadpack")):
            return deny("`git %s`" % a.split("=")[0])
        elif a.startswith("-C"):
            val = args[i + 1] if a == "-C" and i + 1 < len(args) else a[2:]
            target = resolve(d, val, ctx.env) if val else None
            root = (ctx.root or "").lower()
            if target is not None:
                target = real(target)
                root = real(ctx.root or "").lower()
            if target is None or not root or not (target.lower() + "/").startswith(root + "/"):
                return deny("`git -C %s` (a directory outside this project)" % val)
            i += 2 if a == "-C" else 1
        elif a in ("--git-dir", "--work-tree", "--namespace"):
            i += 2
        else:
            i += 1
    sub, rest = (args[i] if i < len(args) else ""), args[i + 1:]
    low = [x.lower() for x in rest]
    if sub not in GIT_SUBS:
        return deny("`git %s` (not a known git subcommand: it may be an alias or an external command)" % sub)
    for x in rest:
        if x.startswith(("--upload-pack", "--receive-pack", "--uploadpack", "--exec", "--ext-diff", "--textconv",
                         "--open-files-in-pager")):
            return deny("`git %s %s`" % (sub, x.split("=")[0]))
    if sub in ("fetch", "clone", "pull") and any(re.match(r"^-[A-Za-z]*u", x) for x in rest):
        return deny("`git %s -u` (an upload-pack command)" % sub)
    if sub == "rebase" and any(re.match(r"^-[A-Za-z]*[xi]", x) or x == "--interactive" for x in rest):
        return deny("`git rebase` with -x / -i / --interactive")
    if sub == "grep" and any(re.match(r"^-[A-Za-z]*O", x) for x in rest):
        return deny("`git grep -O`")
    if sub == "worktree" and "add" in low:
        return deny("`git worktree add` (its checkout would run this repo's hooks)")
    if sub == "config":
        if any(x in GIT_CONFIG_WRITE for x in low) or not any(x in GIT_CONFIG_READ for x in low):
            return deny("`git config` that writes (only --get / --get-all / --get-regexp / --list reads are allowed)")
    return None


WRAPPER_SUBS = frozenset(("read-tree apply write-tree commit-tree rev-parse log show diff merge-base merge-tree ls-remote fetch "
                          "status cat-file ls-tree rev-list for-each-ref worktree").split())


def wrapper_rule(script, args, d, ctx, deny):
    """super-board-git.sh is a git front end: judge what follows `--` as a git command line, like a plain `git`, and
    refuse any subcommand the wrapper itself does not allow (`push` only with --robot)."""
    if not BIN_RE.fullmatch(script) or BIN_RE.fullmatch(script).group("name") != "super-board-git.sh":
        return None
    args = list(args)
    robot = bool(args) and args[0] == "--robot"
    if robot:
        return deny("`super-board-git.sh --robot` (lane pushes go through super-board-objects.py, never from Bash)")
    if args and args[0] == "--robot-read":
        return deny("`super-board-git.sh --robot-read` (credentialed reads go through the lane scripts, never from Bash)")
    if not args or args[0] != "--":
        return deny("`super-board-git.sh` without `--`")
    git_args = args[1:]
    reason = git_rule(git_args, d, ctx, deny)
    if reason:
        return reason
    i = 0
    while i + 1 < len(git_args) and git_args[i] == "-C":
        i += 2
    sub = git_args[i] if i < len(git_args) else ""
    if sub not in WRAPPER_SUBS and not (robot and sub == "push"):
        return deny("`super-board-git.sh -- %s` (not a subcommand the wrapper allows)" % sub[:40])
    return None


def decide(tokens, args, d, ctx, card):
    """An ALLOWLIST for one simple command of a hot command line. Only the commands below pass."""
    def deny(what):
        return REASON % (card, what)

    raw = tokens[0]
    croot = (ctx.card(d) or (None,))[0] if d else None
    if ASSIGN_RE.match(raw):
        return deny("an environment assignment (`%s`)" % str(raw)[:40])
    if bin_ok(str(raw), ctx, croot):                       # .claude/bin/super-board-x.sh called directly
        return wrapper_rule(str(raw), args, d, ctx, deny)
    if exe_name(raw) in ("bash", "sh") and args and bin_ok(str(args[0]), ctx, croot):
        return wrapper_rule(str(args[0]), args[1:], d, ctx, deny)
    if getattr(raw, "odd", False) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", raw):
        return deny("a command whose name is not a plain word (`%s`)" % str(raw)[:60])
    name = exe_name(raw)
    if name == "git":
        return git_rule(args, d, ctx, deny)
    if name == "gh":
        return deny("`gh %s`" % args[0]) if args and args[0].lower() in ("alias", "extension", "ext") else None
    if PY_RE.match(name):
        if args and bin_ok(str(args[0]), ctx, croot):
            return None
        return deny("python (`%s`)" % " ".join(tokens)[:80])
    if name not in READONLY and name not in ("grep", "rg", "find", "sed", "diff"):
        return deny("`%s`" % " ".join(tokens)[:80])
    if name == "rg" and any(a == "--pre" or a.startswith(("--pre=", "--pre-glob")) for a in args):
        return deny("`rg --pre`")
    if name == "find" and any(a in FIND_BAD or a.startswith("-fprint") for a in args):
        return deny("`find` with an action that runs or writes")
    if name == "diff" and any(a.startswith("--external") for a in args):
        return deny("`diff --external`")
    if name == "sed":
        opts = [a for a in args if a.startswith("-")]
        scripts = [a for a in args if not a.startswith("-")][:1]
        if "-n" not in opts or any(o not in ("-n", "-E", "-r", "--quiet", "--silent") for o in opts) \
                or not scripts or not SED_SCRIPT.match(scripts[0]):
            return deny("`sed` other than `sed -n <N[,M]p>`")
    return None


def check_hot(command, d, ctx, card, depth=0):
    """The deny reason for a hot command line, or None."""
    if depth > 4:
        raise ValueError("command nests too deeply")

    def deny(what):
        return REASON % (card, what)

    unq, nosingle = views(command)
    if SUBST_RE.search(nosingle):
        return deny("a command substitution")
    flat = re.sub(r"2>&1|>&\d|&&", " ", unq)
    if "&" in flat:
        return deny("a call operator or background `&`")
    flat = re.sub(r"\d?>>?\s*(/dev/null|\$null|NUL)(?![A-Za-z0-9_./-])", " ", flat, flags=re.I)
    if ">" in flat:
        return deny("an output redirection")
    for tokens in segments(re.sub(r"2>&1|>&\d", " ", command)):
        if ASSIGN_RE.match(tokens[0]):
            return deny("an environment assignment (`%s`)" % str(tokens[0])[:40])
        if exe_name(tokens[0]) in CD:
            t = cd_target(tokens)
            d = resolve(d, t, ctx.env) if t else None
            continue
        if exe_name(tokens[0]) in SHELLS and any(a.lower() in ("-encodedcommand", "-e", "-ec") for a in tokens[1:]):
            return deny("an encoded command")
        inner = nested(tokens)
        reason = check_hot(inner, d, ctx, card, depth + 1) if inner is not None else decide(tokens, tokens[1:], d, ctx, card)
        if reason:
            return reason
    return None


def scan(command, cwd, ctx):
    """The deny reason for a command line, or None."""
    card = ctx.any_marker(cwd)
    if card is None or not hot(command, cwd, ctx):
        return None
    return check_hot(command, cwd, ctx, ctx.marked(cwd) or card)


def main():
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        if payload.get("tool_name") not in (None, "Bash", "PowerShell"):
            return
        command = (payload.get("tool_input") or {}).get("command")
        if not isinstance(command, str):
            raise ValueError("no command")
        cwd = norm(payload.get("cwd") or os.getcwd())
        ctx = Ctx(dict(os.environ))
        ctx.tool = payload.get("tool_name") or "Bash"
        ctx.root = find_root(cwd)
        reason = scan(command, cwd, ctx)
    except Exception as e:      # fail closed: a command this guard cannot evaluate does not run
        reason = ("Blocked: guard-host-tests could not evaluate this command (%s: %s); it fails closed.\n"
                  "docker mode: run test / build commands with `python3 .claude/bin/super-board-lane-delegate.py exec --sandbox "
                  "--config <cfg> --dir <worktree> -- <cmd>`" % (type(e).__name__, e))
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
