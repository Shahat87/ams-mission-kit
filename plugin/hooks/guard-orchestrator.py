#!/usr/bin/env python3
"""PreToolUse hook: while a default workflow-backend run holds its wave lock, the parent orchestrator
cannot edit files or run product build / test / install commands (issue #69).

Enforcement needs ALL of these to hold (spec section 2):
  1. .claude/super-board/inflight/workflow-wave.lock exists (a run is on);
  2. the payload session_id equals the first line of .claude/super-board/inflight/orchestrator-session;
     this hook writes that file itself when it allows `super-board-orch.sh lock-acquire` from a
     parent call, and only then: no lock must exist yet and no other session may still be named in
     the file (a leftover binding from a crashed run denies the acquire until one bare
     `super-board-orch.sh lock-release` clears it — lock-release deletes the file with the lock).
     The bound session is therefore always the one whose acquire could create the lock, and a rebind
     over a leftover foreign binding is denied; two parent sessions that both run a first acquire at
     the same moment is excluded by the single-orchestrator rule and is not prevented (the
     binding's last writer wins);
  3. the payload has no agent_id (the parent orchestrator; subagents and workflow lanes carry one
     and are exempt);
  4. SB_ORCH_GUARD_OFF is not 1 (the documented escape hatch, settings.json env).

While it enforces, a Bash command must be one simple allowlisted call (gh reads plus `gh api` — REST
and GraphQL, writes included: outside the fence, which targets product edits and builds, not GitHub
calls; git reads, never with `--output`; `git worktree remove` without --force, pgrep -f / kill -0 /
ps reads, `jq -r` on a board config or `.claude/settings.json`, `date -u`, the installed super-board
helpers; each Python helper also in its bare shebang form, `.claude/bin/super-board-github-read.py …`).
Any unquoted shell operator, newline, command substitution, nested shell or wrapper is denied. The one
exception is a quoted heredoc into `super-board-orch.sh reconcile` (the cards JSON is literal stdin
data there). Write, Edit and MultiEdit are allowed only for the orchestrator's own run files — under
`.claude/super-board/inflight/`, under the payload's `scratchpad_dir` when the payload sets one, or
under `docs/super-board/runs/` — every other path stays denied and `..` never survives normalization;
NotebookEdit and PowerShell are always denied while the fence holds. Every denial exits 2 with a
message that ends "do not retry in another form." and appends one line to
.claude/super-board/orchestrator-violations.jsonl (the first token only, plus a sha256 of the full
source; the file rotates at 1 MiB; an unwritable log never lifts a denial).

GUARDRAIL: catches habit and mistakes, not attacks (spec section 15.3). A malformed payload fails
closed. Windows, MSYS and backslash paths are normalised via mc_guarded.norm, so every spelling of
the same path decides the same way.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mc_guarded  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

FILE_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
WRITE_TOOLS = ("Write", "Edit", "MultiEdit")
RUN_FILE_DIRS = (".claude/super-board/inflight/", "docs/super-board/runs/")
TOOLS = FILE_TOOLS + ("Bash", "PowerShell")
INFLIGHT = ".claude/super-board/inflight"
LOCK = INFLIGHT + "/workflow-wave.lock"
SESSION_FILE = INFLIGHT + "/orchestrator-session"
LOG = ".claude/super-board/orchestrator-violations.jsonl"
ROTATE_AT = 1024 * 1024
ESCAPE_ENV = "SB_ORCH_GUARD_OFF"
TAIL = "do not retry in another form."

SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
NUM_RE = re.compile(r"^\d{1,7}$")
DOTS_RE = re.compile(r"(^|/)\.\.(/|$)")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=$")
BAD_GIT_ARG = re.compile(r"upload-pack|receive-pack|--exec|--output", re.I)
PROJ_PREFIX = re.compile(r"^\$\{?CLAUDE_PROJECT_DIR\}?/", re.I)
HELPER_REL_RE = re.compile(r"^\.claude/bin/(super-board-[a-z0-9-]+\.(?:sh|py))$")
SH_HELPERS = frozenset("super-board-%s.sh" % n for n in
                       ("wave-plan", "preflight", "usage", "throttle", "card", "stop", "orch",
                        "codex-lane", "codex-wave", "host"))
PY_HELPERS = frozenset("super-board-%s.py" % n for n in
                       ("roadmap", "github-read", "skills-ready", "verification-ready", "events"))

MSGS = {
    "file-tool": "Blocked (orchestrator guard): while the run's wave lock is held the orchestrator must not edit"
                 " files — lanes do all product work; do not retry in another form.",
    "powershell": "Blocked (orchestrator guard): while the run's wave lock is held the orchestrator must not run"
                  " PowerShell — lanes do all product work; do not retry in another form.",
    "compound": "Blocked (orchestrator guard): that command is not one simple allowlisted call — an unquoted"
                " shell operator, a newline, a nested shell or a wrapper is denied; do not retry in another form.",
    "not-allowlisted": "Blocked (orchestrator guard): that command is not on the orchestrator allowlist (gh reads and"
                       " gh api, git reads, pgrep/kill -0/ps, jq on a board config or settings.json, date -u, the"
                       " super-board helpers); do not retry in another form.",
    "stale-session": "Blocked (orchestrator guard): a leftover orchestrator-session from a crashed run names another"
                     " session — run bash .claude/bin/super-board-orch.sh lock-release (bare) to clear it, then"
                     " lock-acquire again; do not retry in another form.",
}
MSGS = {k: (v if v.endswith(TAIL) else v + " " + TAIL) for k, v in MSGS.items()}


def deny_msg(rule):
    return MSGS[rule]


def first_line(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return (fh.readline() or "").strip()
    except OSError:
        return ""


def bin_rel(p, project):
    """The helper name (super-board-<name>.<sh|py>) when token p names that helper under .claude/bin — as a plain
    relative path, a $CLAUDE_PROJECT_DIR-anchored path or the project-anchored absolute path — else None."""
    if not isinstance(p, str) or not p or DOTS_RE.search(p) or "*" in p or "?" in p:
        return None
    q = PROJ_PREFIX.sub("", p)
    if q.startswith("$"):
        return None
    q = mc_guarded.norm(q)
    m = HELPER_REL_RE.match(q)
    if m:
        return m.group(1)
    if project:
        root = mc_guarded.norm(str(project)).lower() + "/"
        if q.lower().startswith(root):
            m = HELPER_REL_RE.match(q[len(root):])
            if m:
                return m.group(1)
    return None


def worktree_path(p, project):
    """True when p names a worktree under .claude/worktrees in a plain relative or project-anchored spelling."""
    if not isinstance(p, str) or not p or DOTS_RE.search(p) or "*" in p or "?" in p:
        return False
    q = mc_guarded.norm(p)
    if q.startswith("$"):
        return False
    if q.startswith(".claude/worktrees/"):
        return True
    if project:
        root = mc_guarded.norm(str(project)).lower() + "/.claude/worktrees/"
        return q.lower().startswith(root)
    return False


def config_path(p, project):
    """True when p names a board config under .claude/super-board/configs in a plain relative or
    project-anchored spelling."""
    if not isinstance(p, str) or not p or DOTS_RE.search(p) or "*" in p or "?" in p:
        return False
    q = mc_guarded.norm(p)
    if q.startswith("$"):
        return False
    m = re.match(r"^\.claude/super-board/configs/([A-Za-z0-9._-]+\.json)$", q)
    if m:
        return True
    if project:
        root = mc_guarded.norm(str(project)).lower() + "/.claude/super-board/configs/"
        if q.lower().startswith(root):
            tail = q[len(root):]
            return re.match(r"^[A-Za-z0-9._-]+\.json$", tail) is not None
    return False


def settings_path(p, project):
    """True when p names the project's .claude/settings.json in a plain relative or project-anchored
    spelling — the step-0 install gate's read-only `jq -r` probe."""
    if not isinstance(p, str) or not p or DOTS_RE.search(p) or "*" in p or "?" in p:
        return False
    q = mc_guarded.norm(p)
    if q.startswith("$"):
        return False
    if q == ".claude/settings.json":
        return True
    if project:
        root = mc_guarded.norm(str(project)).lower() + "/"
        return q.lower() == root + ".claude/settings.json"
    return False


def scratch_root(payload, project):
    """The payload's scratchpad_dir, normalised, or \"\" — the one extra place file tools may write.
    Absent, empty, or a value that hides `..`, a wildcard, or that is the project itself (or an
    ancestor of it) counts as unset."""
    sp = payload.get("scratchpad_dir")
    if not isinstance(sp, str) or not sp.strip():
        return ""
    q = mc_guarded.norm(sp)
    if DOTS_RE.search(q) or q.startswith("$") or "*" in q or "?" in q:
        return ""
    if not re.match(r"^([A-Za-z]:/|/)", q, re.I) and project:
        q = mc_guarded.norm(str(project) + "/" + q)
    if project:
        pl = mc_guarded.norm(str(project)).lower()
        ql = q.lower()
        if pl == ql or pl.startswith(ql + "/"):
            return ""
    return q


def run_file_path(p, project, scratch):
    """True when p names a file the fenced orchestrator may create or edit with Write/Edit/MultiEdit:
    under .claude/super-board/inflight/, under the payload's scratchpad_dir (when set), or under
    docs/super-board/runs/ — plain relative or project-anchored spellings; `..` never survives."""
    if not isinstance(p, str) or not p or "*" in p or "?" in p:
        return False
    q = mc_guarded.norm(p)
    if DOTS_RE.search(q) or q.startswith("$"):
        return False
    for prefix in RUN_FILE_DIRS:
        if q.startswith(prefix):
            return True
        if project and q.lower().startswith(mc_guarded.norm(str(project)).lower() + "/" + prefix):
            return True
    if scratch:
        ql = q.lower()
        if ql.startswith(scratch.lower() + "/"):
            return True
    return False


def allowlisted(tokens, project):
    """True when the token list is one of the orchestrator's allowlisted simple commands (spec section 2)."""
    if not tokens:
        return False
    name = tokens[0]
    if name == "gh":
        if len(tokens) < 3:
            return False
        if tokens[1] == "issue":
            return tokens[2] in ("view", "edit", "comment")
        if tokens[1] == "pr":
            return tokens[2] in ("view", "diff", "checks", "comment")
        if tokens[1] == "project":
            return tokens[2] in ("item-list", "item-edit")
        return tokens[1] == "api"
    if name == "git":
        rest = tokens[1:]
        if len(rest) >= 2 and rest[0] == "-C":
            return (worktree_path(rest[1], project) and
                    rest[2:] in (["status", "--porcelain"], ["status", "--porcelain", "--ignored"]))
        if len(rest) >= 2 and rest[0] == "worktree":
            if rest[1] == "list":
                return rest[2:] in ([], ["--porcelain"])
            if rest[1] == "remove":
                return (len(rest) == 3 and not rest[2].startswith("-")
                        and worktree_path(rest[2], project))
            return False
        if rest and rest[0] in ("status", "log", "rev-parse", "ls-remote", "fetch"):
            return not any(BAD_GIT_ARG.search(a) for a in rest[1:])
        return False
    if name == "pgrep":
        return len(tokens) >= 3 and tokens[1] == "-f"
    if name == "kill":
        return len(tokens) == 3 and tokens[1] == "-0" and NUM_RE.match(tokens[2] or "") is not None
    if name == "ps":
        return (len(tokens) == 5 and tokens[1] == "-p" and NUM_RE.match(tokens[2] or "") is not None
                and tokens[3] == "-o" and FIELD_RE.match(tokens[4] or "") is not None)
    if name == "jq":
        return (len(tokens) == 4 and tokens[1] == "-r" and not tokens[2].startswith("-")
                and (config_path(tokens[3], project) or settings_path(tokens[3], project)))
    if name == "date":
        return tokens[1:] == ["-u"]
    if name == "bash":
        return (len(tokens) > 1 and bin_rel(tokens[1], project) in SH_HELPERS)
    if name == "python3":
        return (len(tokens) > 1 and bin_rel(tokens[1], project) in PY_HELPERS)
    # a Python helper may also run as itself (shebang): .claude/bin/super-board-github-read.py …
    return bin_rel(name, project) in PY_HELPERS


def tokenize(command):
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = ""
    return list(lex)


def nested_reason(tokens):
    """Why the token list hides a nested shell or wrapper (spec section 2), or None."""
    for idx, t in enumerate(tokens):
        low = t.lower()
        if low in ("eval", "sudo", "env", "xargs", "wsl", "pwsh", "powershell", "cmd"):
            return "`%s`" % low
        if low in ("bash", "sh") and idx + 1 < len(tokens):
            nxt = tokens[idx + 1].lower()
            if re.match(r"^-[a-z]*c", nxt):
                return "`%s %s`" % (low, nxt)
    return None


def heredoc_split(command, i):
    """Split a heredoc that starts at the unquoted << at position i.

    Returns (quoted, pre, body, after) or None when the operator is malformed, unterminated or has
    junk around it — the caller denies all of those."""
    j = i + 2
    dash = j < len(command) and command[j] == "-"
    if dash:
        j += 1
    while j < len(command) and command[j] in " \t":
        j += 1
    q = ""
    if j < len(command) and command[j] in "'\"":
        q = command[j]
        j += 1
    m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", command[j:])
    if m is None:
        return None
    word = m.group(0)
    j += len(word)
    if q:
        if j >= len(command) or command[j] != q:
            return None
        j += 1
    nl = command.find("\n", j)
    if nl < 0:
        return None
    if re.match(r"[ \t]*\r?$", command[j:nl]) is None:
        return None
    lines = command[nl + 1:].split("\n")
    for k, line in enumerate(lines):
        term = line.lstrip("\t") if dash else line.rstrip("\r")
        if term == word:
            return (bool(q), command[:i], "\n".join(lines[:k]), "\n".join(lines[k + 1:]))
    return None


def scan(command):
    """('deny', why) for a raw compound construct, ('heredoc', split) at the first unquoted <<, or ('ok', None)."""
    i, n, quote = 0, len(command), None
    while i < n:
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                quote = None
            elif c == "`" or command.startswith("$(", i):
                return ("deny", "a live substitution inside double quotes")
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
            i += 1
            continue
        if c == "`" or command.startswith("$(", i):
            return ("deny", "a command substitution")
        if c == "\n" or c == "\r":
            return ("deny", "a newline")
        if c in ";|&<>":
            if c == "<" and command.startswith("<<", i):
                split = heredoc_split(command, i)
                if split is None:
                    return ("deny", "a malformed or unterminated heredoc")
                return ("heredoc", split)
            return ("deny", "an unquoted shell operator (%s)" % c)
        i += 1
    return ("ok", None)


def judge_bash(command, project):
    """The deny rule for one orchestrator Bash command, or None to allow (spec section 2).

    The command is analysed with backslashes already turned into forward slashes (Windows and MSYS
    spellings of the same path must decide the same way); no shell operator can be created or hidden
    by that. The raw text only feeds the audit sha256."""
    command = command.replace("\\", "/")
    kind, info = scan(command)
    if kind == "deny":
        return "compound"
    if kind == "heredoc":
        quoted, pre, _body, after = info
        if not quoted or after.strip():
            return "compound"
        try:
            pre_tokens = tokenize(pre)
        except ValueError:
            return "compound"
        if nested_reason(pre_tokens):
            return "compound"
        if not (len(pre_tokens) >= 3 and pre_tokens[0] == "bash"
                and bin_rel(pre_tokens[1], project) == "super-board-orch.sh"
                and pre_tokens[2] == "reconcile" and allowlisted(pre_tokens, project)):
            return "compound"
        return None
    try:
        tokens = tokenize(command)
    except ValueError:
        return "compound"
    if nested_reason(tokens):
        return "compound"
    if allowlisted(tokens, project):
        return None
    return "not-allowlisted"


def is_lock_acquire(tokens, project):
    return (len(tokens) >= 4 and tokens[0] == "bash"
            and bin_rel(tokens[1], project) == "super-board-orch.sh"
            and tokens[2] == "lock-acquire" and SLUG_RE.match(tokens[3] or "") is not None)


def record_session(session_path, payload):
    """Bind this payload's session as the run's orchestrator (spec section 2)."""
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        raise ValueError("a lock-acquire without a session_id cannot be bound")
    path = Path(session_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(session_id + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


def log_violation(project, payload, tool, rule, source, lock_path):
    """One JSONL line (the first token only, plus a sha256 of the full source); rotates at 1 MiB;
    never raises — an unwritable log never lifts a denial (spec section 3)."""
    try:
        slug = started = ""
        try:
            text = Path(lock_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = re.search(r"^SLUG=(.*)$", text, re.M)
        if m:
            slug = m.group(1).strip()
        m = re.search(r"^STARTED=(.*)$", text, re.M)
        if m:
            started = m.group(1).strip()
        head = " ".join((source or "").split()[:1])
        entry = {"v": 1,
                 "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "session_id": str(payload.get("session_id") or ""),
                 "run": {"slug": slug, "started": started},
                 "tool": tool,
                 "rule": rule,
                 "head": head[:32],
                 "sha256": hashlib.sha256((source or "").encode("utf-8")).hexdigest(),
                 "cwd": mc_guarded.norm(payload.get("cwd") or "")}
        path = Path(project) / LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size >= ROTATE_AT:
            os.replace(str(path), str(path) + ".1")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def deny(project, payload, tool, rule, source, lock_path):
    log_violation(project, payload, tool, rule, source, lock_path)
    print(deny_msg(rule), file=sys.stderr)
    return 2


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        if os.environ.get(ESCAPE_ENV) == "1":
            return 0
        tool = payload.get("tool_name")
        if not isinstance(tool, str) or tool not in TOOLS:
            raise ValueError("tool_name is not one of %s" % "/".join(TOOLS))
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            raise ValueError("tool_input is not an object")
        cwd = mc_guarded.norm(payload.get("cwd") or os.getcwd())
        project = mc_guarded.norm(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)
        lock_path = os.path.join(project, *LOCK.split("/"))
        session_path = os.path.join(project, *SESSION_FILE.split("/"))
        agent_id = payload.get("agent_id")
        scratch = scratch_root(payload, project)
        lock = os.path.isfile(lock_path)
        session_ok = (bool(payload.get("session_id"))
                      and first_line(session_path) == str(payload.get("session_id")))
        enforcing = lock and session_ok and not agent_id
        if tool == "Bash":
            command = tool_input.get("command")
            if not isinstance(command, str) or not command.strip():
                raise ValueError("no command")
            if enforcing:
                rule = judge_bash(command, project)
                if rule is not None:
                    return deny(project, payload, tool, rule, command, lock_path)
                return 0
            if not agent_id and not lock:
                try:
                    tokens = tokenize(command.replace("\\", "/"))
                except ValueError:
                    tokens = None
                if tokens and is_lock_acquire(tokens, project):
                    # Bind only when this acquire can actually create the lock: a foreign leftover
                    # binding is denied (run the bare lock-release first) instead of being allowed to
                    # race the noclobber create and leave the file naming the loser.
                    bound = first_line(session_path)
                    if bound and bound != str(payload.get("session_id") or ""):
                        return deny(project, payload, tool, "stale-session", command, lock_path)
                    record_session(session_path, payload)
            return 0
        if enforcing:
            source = str(tool_input.get("command") or tool_input.get("file_path") or "")
            if tool in FILE_TOOLS:
                if tool in WRITE_TOOLS and run_file_path(tool_input.get("file_path"), project, scratch):
                    return 0
                return deny(project, payload, tool, "file-tool", source, lock_path)
            return deny(project, payload, tool, "powershell", source, lock_path)
        return 0
    except Exception as exc:  # fail closed
        print("Blocked (orchestrator guard): could not evaluate this call (%s: %s); it fails closed. %s"
              % (type(exc).__name__, exc, TAIL), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
