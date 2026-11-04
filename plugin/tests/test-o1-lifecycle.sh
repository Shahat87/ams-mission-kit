#!/usr/bin/env bash
# O-1 acceptance test for the whole lane lifecycle (Plan 1c Task 10; spec addendum 15.9 F11), plus a static guard test.
#
#   bash tests/test-o1-lifecycle.sh        (offline: no docker, no network, no GitHub; the remote and docker are stubs)
#
# Part B, the lifecycle. One temp bare `origin` and one temp main checkout (guarded through MC_GUARDED_REPOS_FILE) go through
#   card `run` -> `verify` -> QA `commit-onto` -> Review `exec --sandbox --ref` (where=lenovo) -> the merge gate (exits 0, 2, 5)
#   -> a `delegate` write.
# A recording `git` shim sits first on PATH (and a Popen hook covers Python callers on Windows, where PATH cannot shadow git.exe).
# It logs every `worktree add`, `checkout`, `switch`, `merge`, `read-tree -u` and every `apply` without `--cached`, with the sha or
# ref each one touches. Then:
#   - every sha in the log must be reachable from origin/<base> (git merge-base --is-ancestor), and
#   - no byte the tool wrote may appear as a file in the main checkout (or the card worktree).
# Self-checks: a seeded unreachable sha MUST make the reachability assertion fail, and the shim MUST record a control checkout.
#
# Part C, the static guard. super-board-merge-gate.sh and super-board-lane-delegate.py mark their guarded path with
#   `# >>> guarded-path (objects only)` ... `# <<< guarded-path`. The markers must balance, and between them there must be no
# `worktree add`, no `"worktree", "add"`, no `checkout` and no `switch` (comment lines and Python docstrings are ignored).
# Self-check: a temp copy with a `git checkout` injected between the markers MUST make the check fail.
#
# What looks like a bug and is not: the gate's `worktree add` and sandbox_clone's `checkout` are on the UNGUARDED path, outside
# the markers. `/proc/1/root`-style facts do not apply here (that is the docker smoke).
set -u

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
plugin="$(cd "$here/.." && pwd)"

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "FAIL: python 3.9+ not found"; exit 1; fi
for tool in git jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "FAIL: $tool not found"; exit 1; }
done

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

bash_path="${BASH:-bash}"
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) bash_path="$(cygpath -m "$bash_path" 2>/dev/null || echo "$bash_path")" ;; esac

cat > "$tmp/o1.py" <<'PYEOF'
import contextlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
import ast
import importlib.util
import tokenize
from pathlib import Path
from unittest import mock

TESTS = Path(os.environ["MC_O1_TESTS_DIR"])
PLUGIN = TESTS.parent
sys.path.insert(0, str(TESTS))
import test_lane_delegate as T            # LaneRepo, write_guard_file, GH_STUB: the existing stubs, reused as they are

REALGIT = shutil.which("git")
BASH = os.environ["MC_O1_BASH"]
OPEN = "# >>> guarded-path (objects only)"
CLOSE = "# <<< guarded-path"


def real(*args, cwd=None, check=True):
    r = subprocess.run([REALGIT, *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (" ".join(args), r.stderr[-300:]))
    return r.stdout.strip()


# ---------------------------------------------------------------- the recording shim
#
# Coverage: the PATH shim sees every bash and POSIX caller. On Windows git.exe cannot be shadowed by a script, so the Popen hook (a
# sitecustomize) is the only cover for Python callers, and it reaches only Python children that keep PYTHONPATH (lane-env ALLOW drops it
# for scrubbed-env children). Full shim coverage is therefore proven in WSL/POSIX only; the hook also logs the call but not its post-state.
#
# Every git call is logged as   <cwd> TAB <scrubbed 0|1> TAB <arg> TAB <arg> ...
# and, for a tree-touching call, followed by   @R TAB <token> TAB <sha or ->   lines: what each ref-like token resolved to AT CALL TIME,
# with the real git (the temp clones are gone by the time the log is read). A call whose -C directory does not exist logs
# @R TAB * TAB NODIR. The log path is BAKED INTO the shim: lane-delegate runs most of its git with a scrubbed env (lane-env ALLOW
# keeps neither MC_GITLOG nor anything else we add), so the log path can never come from the environment. `scrubbed` is 1 when
# MC_GITLOG was missing from the env of the call, which is how the test proves such calls are seen. A shim that cannot write its
# log exits 97.

LIB = r'''
import os, re, subprocess


def parse_args(cwd, args):
    d, i = cwd, 0
    while i < len(args):
        a = args[i]
        if a == "-C" and i + 1 < len(args):
            d = os.path.normpath(os.path.join(d, args[i + 1]))
            i += 2
        elif a == "-c" and i + 1 < len(args):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    return d, (args[i] if i < len(args) else ""), list(args[i + 1:])


APPLY_SAFE = {"--cached", "--numstat", "--stat", "--summary", "--check", "--binary", "--whitespace=nowarn"}


def apply_exempt(args):
    """A git apply that cannot touch a working tree: --cached (index only), or a read-only flag (--numstat, --stat, --summary, --check).
    Strict: every long flag must be an exact member of APPLY_SAFE. Anything else (--apply and its prefixes --ap/--app, --no-cached,
    --no-numstat, ...) can turn tree writes back on, so it is never exempt."""
    flags = [a for a in args if a.startswith("--")]
    if any(f not in APPLY_SAFE for f in flags):
        return False
    return any(f in ("--cached", "--numstat", "--stat", "--summary", "--check") for f in flags)


def ref_tokens(rest):
    """Ref-like operands of a call: plain operands, the value of --opt=<val>, and -s<val> (restore --source short form)."""
    for tok in rest:
        if tok.startswith("--") and "=" in tok:
            v = tok.split("=", 1)[1]
            if v:
                yield v
        elif re.fullmatch(r"-s.+", tok):
            yield tok[2:]
        elif not tok.startswith("-"):
            yield tok


def tree_touching(sub, rest):
    """Does this git call put a commit's content into a working tree (or move HEAD onto one)?"""
    if sub == "worktree":
        return rest[:1] == ["add"]
    if sub in ("checkout", "switch", "merge", "checkout-index", "restore", "stash", "pull", "rebase", "cherry-pick", "revert", "am"):
        return True
    if sub == "clone":                                   # a bare clone and --no-checkout leave no working tree
        return not any(a in ("--no-checkout", "-n", "--bare", "--mirror") for a in rest)
    if sub == "reset":
        return any(a in ("--hard", "--keep", "--merge") for a in rest)
    if sub == "read-tree":
        return any(len(a) > 1 and a[0] == "-" and a[1] != "-" and "u" in a[1:] for a in rest)
    if sub == "apply":
        return not apply_exempt(rest)
    return False


def clean(x):
    return str(x).replace("\t", " ").replace("\n", " ").replace("\r", " ")


def record(log, real, flag, cwd, args):
    d, sub, rest = parse_args(cwd, args)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("\t".join([clean(cwd), flag] + [clean(a) for a in args]) + "\n")
        if tree_touching(sub, rest):
            for tok in ref_tokens(rest):
                if not os.path.isdir(d):
                    fh.write("@R\t*\tNODIR\n")
                    break
                if os.path.exists(os.path.join(d, tok)):
                    continue
                r = subprocess.run([real, "-C", d, "rev-parse", "--verify", "-q", tok + "^{commit}"], capture_output=True, text=True)
                fh.write("@R\t%s\t%s\n" % (clean(tok), r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "-"))


def record_post(log, real, cwd, args):
    """After a tree-touching call: the state it left behind, for the calls that carry no judged token (reset --hard, checkout -- .,
    checkout-index -a, stash pop). HEAD and the index tree (write-tree), read with the real git. The working-tree files themselves are
    NOT judged: only where HEAD points and what the index holds."""
    d, sub, rest = parse_args(cwd, args)
    if not tree_touching(sub, rest) or not os.path.isdir(d):
        return
    def out(*a):
        r = subprocess.run([real, "-C", d, *a], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "-"
    if out("rev-parse", "--git-dir") == "-":
        return
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("@P\t%s\t%s\n" % (out("rev-parse", "HEAD"), out("write-tree")))
'''
exec(LIB)

SHIM_PY = '''import os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from o1lib import record, record_post
LOG = __LOG__
REAL = __REAL__
flag = "0" if os.environ.get("MC_GITLOG") else "1"
try:
    record(LOG, REAL, flag, os.getcwd(), sys.argv[1:])
except OSError as e:
    sys.stderr.write("git shim: cannot write the log %s: %s\\n" % (LOG, e))
    sys.exit(97)
rc = subprocess.call([REAL] + sys.argv[1:])
try:
    record_post(LOG, REAL, os.getcwd(), sys.argv[1:])
except OSError as e:
    sys.stderr.write("git shim: cannot write the log %s: %s\\n" % (LOG, e))
    sys.exit(97)
sys.exit(rc)
'''

HOOK_TAIL = '''
_LOG = __LOG__
_REAL = __REAL__
_orig = subprocess.Popen.__init__


def _init(self, args, *a, **k):
    if not isinstance(args, (str, bytes)) and args and str(args[0]) != _REAL and os.path.basename(str(args[0])).lower() in ("git", "git.exe"):
        env = k.get("env")
        record(_LOG, _REAL, "1" if env is not None and "MC_GITLOG" not in env else "0", str(k.get("cwd") or os.getcwd()), [str(x) for x in list(args)[1:]])
    _orig(self, args, *a, **k)


def install():
    subprocess.Popen.__init__ = _init


def uninstall():
    subprocess.Popen.__init__ = _orig


install()
'''


def posix(p):
    return str(p).replace("\\", "/")


class Recorder:
    """A git shim dir (first on PATH) per log file, with the log path baked in. On Windows, where git.exe cannot be shadowed from PATH by
    a script, a sitecustomize hook logs the git calls Python makes."""
    def __init__(self, root: Path):
        self.root = root
        self.dirs = {}
        self.log = root / "git.log"
        self.log.write_text("")

    def make(self, log: Path):
        d = self.root / ("shim%d" % (len(self.dirs) + 1))
        (d / "hook").mkdir(parents=True)
        (d / "o1lib.py").write_text(LIB, encoding="utf-8")
        baked = lambda text: text.replace("__LOG__", repr(posix(log))).replace("__REAL__", repr(posix(REALGIT)))
        (d / "gitshim.py").write_text(baked(SHIM_PY), encoding="utf-8")
        (d / "git").write_bytes(('#!/usr/bin/env bash\nexec "%s" "%s" "$@"\n' % (posix(sys.executable), posix(d / "gitshim.py"))).encode("utf-8"))
        (d / "git").chmod(0o755)
        (d / "hook" / "sitecustomize.py").write_text(LIB + baked(HOOK_TAIL), encoding="utf-8")
        return d

    def shim_dir(self, log):
        log = Path(log)
        if str(log) not in self.dirs:
            self.dirs[str(log)] = self.make(log)
        return self.dirs[str(log)]

    @contextlib.contextmanager
    def on(self, log=None):
        log = Path(log or self.log)
        d = self.shim_dir(log)
        env = {"MC_GITLOG": str(log), "PATH": str(d) + os.pathsep + os.environ["PATH"]}
        hook = None
        if os.name == "nt":
            env["PYTHONPATH"] = str(d / "hook") + os.pathsep + os.environ.get("PYTHONPATH", "")
        with mock.patch.dict(os.environ, env):
            if os.name == "nt":
                spec = importlib.util.spec_from_file_location("mc_o1_hook_%d" % len(self.dirs), d / "hook" / "sitecustomize.py")
                hook = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(hook)
            try:
                yield
            finally:
                if hook is not None:
                    hook.uninstall()


# ---------------------------------------------------------------- the log analysis

def parse_log(text):
    calls = []
    for l in text.splitlines():
        if not l.strip():
            continue
        parts = l.split("\t")
        if parts[0] in ("@R", "@P"):
            if calls and parts[0] == "@R":
                calls[-1]["res"].append((parts[1], parts[2]))
            elif calls:
                calls[-1]["post"] = (parts[1], parts[2])
            continue
        d, sub, rest = parse_args(parts[0], parts[2:])
        calls.append({"dir": d, "sub": sub, "rest": rest, "scrubbed": parts[1] == "1", "res": [], "post": None})
    return calls


def line(cwd, *args, scrubbed=0):
    """One call log line, the way the shim writes it (no @R records)."""
    return "\t".join([str(cwd), str(scrubbed), *args]) + "\n"


def _under(path, roots):
    p = os.path.normcase(os.path.realpath(str(path)))
    return any(p == r or p.startswith(r + os.sep) for r in (os.path.normcase(os.path.realpath(str(x))) for x in roots))


def unreachable(text, origin_bare, base="main", protected=()):
    """[(subcommand, token, why-or-sha)] for every tree-touching call that
      - names a sha or ref that is not an ancestor of origin/<base> (a full sha is judged by origin alone; a token with no @R record is a path),
      - names a ref-like token that did not resolve to a commit when the call was made, or runs in a directory that did not exist,
      - left HEAD somewhere that is not on origin/<base>, or an index tree that is not the tree of a commit on origin/<base> (@P record), or
      - is a non-exempt apply run in one of the PROTECTED trees (the main checkout, the card worktree): that fails outright."""
    bad = []
    trees = None
    for c in parse_log(text):
        if not tree_touching(c["sub"], c["rest"]):
            continue
        if _under(c["dir"], protected):
            bad.append((c["sub"], "*", ("apply" if c["sub"] == "apply" else "tree-touching call") + " in a protected tree"))
        res = dict(c["res"])
        if res.get("*") == "NODIR":
            bad.append((c["sub"], "*", "the call directory did not exist"))
            continue
        for tok in ref_tokens(c["rest"]):
            if re.fullmatch(r"[0-9a-f]{40}", tok):
                sha = tok
            elif tok in res:
                sha = res[tok]
                if sha == "-":
                    bad.append((c["sub"], tok, "does not resolve to a commit"))
                    continue
            else:
                continue
            rc = subprocess.run([REALGIT, "-C", str(origin_bare), "merge-base", "--is-ancestor", sha, "refs/heads/" + base], capture_output=True).returncode
            if rc != 0:
                bad.append((c["sub"], tok, sha))
        if c["post"]:
            head, tree = c["post"]
            if head != "-" and subprocess.run([REALGIT, "-C", str(origin_bare), "merge-base", "--is-ancestor", head, "refs/heads/" + base],
                                              capture_output=True).returncode != 0:
                bad.append((c["sub"], "HEAD", "HEAD after the call is not on origin/" + base + ": " + head))
            if tree != "-":
                if trees is None:
                    trees = set(subprocess.run([REALGIT, "-C", str(origin_bare), "log", "--format=%T", "refs/heads/" + base],
                                               capture_output=True, text=True).stdout.split())
                if tree not in trees:
                    bad.append((c["sub"], "index", "the index tree after the call is not the tree of a commit on origin/" + base + ": " + tree))
    return bad


# ---------------------------------------------------------------- the static guard

def docstring_lines(text):
    lines = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) and isinstance(first.value.value, str):
                lines.update(range(first.lineno, first.end_lineno + 1))
    return lines


def logical_lines(text, is_python):
    """[(first line number, text)]: each statement with its continuation lines joined, comments dropped, so an argv list split across lines is
    matched as one."""
    out = []
    if is_python:
        cur, start = [], None
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING):
                continue
            if tok.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                if cur:
                    out.append((start, " ".join(cur)))
                cur, start = [], None
                continue
            if start is None:
                start = tok.start[0]
            cur.append(tok.string)
        return out
    buf, start = [], None
    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if not buf and (not s or s.startswith("#")):
            continue
        if start is None:
            start = n
        if s.endswith("\\"):
            buf.append(s[:-1])
            continue
        buf.append(s)
        out.append((start, " ".join(buf)))
        buf, start = [], None
    return out


GIT_SH = r"(?:^|[^\w-])(?:pr_)?git\b|super-board-git\.sh"


def git_words(t, py, name):
    """End offsets of each use of the git subcommand NAME: a quoted token in Python; in shell the word after git, pr_git or
    super-board-git.sh anywhere in the statement (so X=$(git apply ...) and `if git -C d apply ...` count)."""
    if py:
        return [m.end() for m in re.finditer(r"""["']%s["']""" % re.escape(name), t)]
    g = re.search(GIT_SH, t)
    if not g:
        return []
    return [g.end() + m.end() for m in re.finditer(r"(?<![\w-])%s(?![\w-])" % re.escape(name), t[g.end():])]


def call_tail(t, pos, py):
    """The text of ONE call after position pos: up to the next git call or command separator, so a second call in the same statement is
    judged on its own arguments."""
    rx = r"""["']git["']|\bgit\s*\(|\bsubprocess\b""" if py else r"&&|\|\||[;|`]|\$\("
    m = re.compile(rx).search(t, pos)
    return t[pos:m.start()] if m else t[pos:]


def _apply_writes(t, py):
    for pos in git_words(t, py, "apply"):
        if not apply_exempt(re.findall(r"--[\w-]+", call_tail(t, pos, py))):
            return True
    return False


def _clone_with_tree(t, py):
    return re.search(r"\bclone\b", t) and not re.search(r"--no-checkout|--bare|--mirror|[\"']-n[\"']|\s-n\s", t)


FORBIDDEN = [
    ("worktree add", lambda t, py: re.search(r"worktree\s+add", t)),
    ('"worktree", "add"', lambda t, py: re.search(r"""["']worktree["']\s*,\s*["']add["']""", t)),
    ("checkout", lambda t, py: re.search(r"(?<![\w-])checkout(-index)?(?![\w-])", t)),
    ("switch", lambda t, py: re.search(r"\bswitch\b", t)),
    ("merge", lambda t, py: git_words(t, py, "merge")),         # a git subcommand; merge-tree, merge-base and English ("does not merge") stay allowed
    ("clone with a working tree", _clone_with_tree),
    ("read-tree -u", lambda t, py: re.search(r"""read-tree\b.*[\s"',]-[A-Za-z]*u[A-Za-z]*\b""", t)),
    ("restore", lambda t, py: re.search(r"\brestore\b", t)),
    ("reset --hard", lambda t, py: re.search(r"\breset\b.*--(hard|keep|merge)\b", t)),
    ("apply without --cached", _apply_writes),
] + [(name, (lambda n: lambda t, py: git_words(t, py, n))(name)) for name in ("stash", "pull", "rebase", "cherry-pick", "am")]


def guard_problems(text, is_python):
    """(problems, region count) for one script. Problems: unbalanced markers, no markers at all, or a forbidden call between them."""
    problems, regions, inside, start, spans = [], 0, False, 0, []
    for n, raw in enumerate(text.splitlines(), 1):
        s = raw.strip()
        if s == OPEN:
            if inside:
                problems.append("line %d: a guarded-path block opens inside another one (opened at %d)" % (n, start))
            inside, start = True, n
            regions += 1
        elif s == CLOSE:
            if not inside:
                problems.append("line %d: a guarded-path block closes without being open" % n)
            else:
                spans.append((start, n))
            inside = False
    if inside:
        problems.append("the guarded-path block opened at line %d never closes" % start)
    if regions == 0:
        problems.append("no guarded-path markers")
    skip = docstring_lines(text) if is_python else set()
    for n, t in logical_lines(text, is_python):
        if n in skip or not any(a < n < b for a, b in spans):
            continue
        for label, match in FORBIDDEN:
            if match(t, is_python):
                problems.append("line %d: %s on the guarded path: %s" % (n, label, t[:100]))
    return problems, regions


GATE = PLUGIN / "scripts" / "super-board-merge-gate.sh"
DELEGATE = PLUGIN / "scripts" / "super-board-lane-delegate.py"


class StaticGuard(unittest.TestCase):
    def test_both_scripts_have_balanced_markers_and_nothing_forbidden_between_them(self):
        for path, is_py in ((GATE, False), (DELEGATE, True)):
            with self.subTest(path.name):
                problems, regions = guard_problems(path.read_text(encoding="utf-8"), is_py)
                self.assertEqual(problems, [])
                self.assertGreaterEqual(regions, 1)

    def test_self_check_an_injected_call_between_the_markers_is_caught(self):
        gate = GATE.read_text(encoding="utf-8")
        self.assertEqual(guard_problems(gate, False)[0], [], "the real gate must be clean before it is tampered with")
        for label, text in (("checkout", '  git -C "$REPO_PATH" checkout "$HEAD_SHA"'),
                            ("worktree add", '  git -C "$REPO_PATH" worktree add --detach "$SCRATCH" "$HEAD_SHA"'),
                            ("switch", '  git -C "$REPO_PATH" switch -d "$HEAD_SHA"'),
                            ("clone with a working tree", '  git clone "$REPO_PATH" "$SCRATCH"'),
                            ("read-tree -u", '  git -C "$REPO_PATH" read-tree -um "$HEAD_SHA"'),
                            ("restore", '  git -C "$REPO_PATH" restore --source "$HEAD_SHA" .'),
                            ("reset --hard", '  git -C "$REPO_PATH" reset --hard "$HEAD_SHA"'),
                            ("apply without --cached", '  git -C "$REPO_PATH" apply "$PATCH"'),
                            ("checkout", '  git -C "$REPO_PATH" \\\n     checkout "$HEAD_SHA"')):
            with self.subTest(label + " " + text[:30]):
                tampered = gate.replace(OPEN, OPEN + "\n" + text, 1)
                self.assertNotEqual(tampered, gate)
                problems, _ = guard_problems(tampered, False)
                self.assertTrue(any(label in p for p in problems), problems)
        for fine in ('  git clone --no-checkout "$REPO_PATH" "$SCRATCH"', '  git clone --bare "$REPO_PATH" "$SCRATCH"',
                     '  git -C "$REPO_PATH" apply --cached "$PATCH"', '  git -C "$REPO_PATH" read-tree "$HEAD_SHA"'):
            with self.subTest("allowed " + fine):
                self.assertEqual(guard_problems(gate.replace(OPEN, OPEN + "\n" + fine, 1), False)[0], [])
        deleg = DELEGATE.read_text(encoding="utf-8")
        self.assertEqual(guard_problems(deleg, True)[0], [])
        for label, text in (("checkout", '    git(repo, "checkout", sha)'), ('"worktree", "add"', '    git(repo, "worktree", "add", d, sha)'),
                            ("clone with a working tree", '    subprocess.run(["git", "clone",\n                    str(a), str(b)])'),
                            ("read-tree -u", '    subprocess.run(["git", "read-tree",\n                    "-u", sha])'),
                            ("restore", '    git(repo, "restore", "--source", sha, ".")'),
                            ("reset --hard", '    git(repo, "reset",\n        "--hard", sha)'),
                            ("apply without --cached", '    git(repo, "apply", str(patch))'),
                            ("switch", '    git(repo, "switch", "-d", sha)')):
            with self.subTest("py " + label):
                problems, _ = guard_problems(deleg.replace(OPEN, OPEN + "\n" + text, 1), True)
                self.assertTrue(any(label in p for p in problems), problems)
        for text in ('    git(repo, "apply", "--stat", "--apply", p)', '    subprocess.run(["git", "apply", "--numstat", "--app", p], check=True)'):
            with self.subTest("py " + text[:40]):
                problems, _ = guard_problems(deleg.replace(OPEN, OPEN + chr(10) + text, 1), True)
                self.assertTrue(any("apply without --cached" in x for x in problems), problems)
        for fine in ('    subprocess.run(["git", "clone",\n                    "--no-checkout", str(a), str(b)])', '    git(repo, "apply", "--cached", str(patch))'):
            with self.subTest("py allowed " + fine[:40]):
                self.assertEqual(guard_problems(deleg.replace(OPEN, OPEN + "\n" + fine, 1), True)[0], [])

    def test_self_check_shell_apply_forms_and_the_other_tree_writers(self):
        gate = GATE.read_text(encoding="utf-8")
        for label, text in (("apply without --cached", '  pr_git -C "$REPO_PATH" apply "$PATCH"'),
                            ("apply without --cached", '  X=$(git -C "$REPO_PATH" apply "$PATCH")'),
                            ("apply without --cached", '  if git -C "$REPO_PATH" apply "$PATCH"; then :; fi'),
                            ("apply without --cached", '  git -C "$REPO_PATH" apply --numstat --apply "$PATCH"'),
                            ("apply without --cached", '  git apply --cached "$PATCH" && git apply --stat --app "$PATCH"'),
                            ("merge", '  git -C "$REPO_PATH" merge "$HEAD_SHA"'),
                            ("stash", '  git -C "$REPO_PATH" stash pop'),
                            ("pull", '  git -C "$REPO_PATH" pull origin main'),
                            ("rebase", '  git -C "$REPO_PATH" rebase "$HEAD_SHA"'),
                            ("cherry-pick", '  git -C "$REPO_PATH" cherry-pick "$HEAD_SHA"'),
                            ("am", '  git -C "$REPO_PATH" am "$PATCH"')):
            with self.subTest(text.strip()[:50]):
                problems, _ = guard_problems(gate.replace(OPEN, OPEN + "\n" + text, 1), False)
                self.assertTrue(any(label in x for x in problems), problems)
        for fine in ('  git -C "$REPO_PATH" merge-tree --write-tree "$A" "$B"', '  git -C "$REPO_PATH" merge-base "$A" "$B"',
                     '  git -C "$REPO_PATH" apply --cached "$PATCH"', '  pr_git -C "$REPO_PATH" apply --check --stat "$PATCH"',
                     '  echo "I am here"', '  git -C "$REPO_PATH" log --merges'):
            with self.subTest("allowed " + fine.strip()[:50]):
                self.assertEqual(guard_problems(gate.replace(OPEN, OPEN + "\n" + fine, 1), False)[0], [])
        deleg = DELEGATE.read_text(encoding="utf-8")
        for fine in ('    git(repo, "merge-tree", "--write-tree", a, b)', '    git(repo, "merge-base", a, b)'):
            with self.subTest("py allowed " + fine.strip()[:50]):
                self.assertEqual(guard_problems(deleg.replace(OPEN, OPEN + "\n" + fine, 1), True)[0], [])
        problems, _ = guard_problems(deleg.replace(OPEN, OPEN + "\n" + '    x = [git(r, "apply", "--cached", p), git(r, "apply", "--numstat", "--apply", p)]', 1), True)
        self.assertTrue(any("apply without --cached" in x for x in problems), "the second call of the statement writes the tree: %s" % problems)
        problems, _ = guard_problems(deleg.replace(OPEN, OPEN + "\n" + '    git(repo, "merge", sha)', 1), True)
        self.assertTrue(any("merge" in x for x in problems), problems)

    def test_self_check_the_same_call_outside_the_markers_is_not_flagged_and_broken_markers_are(self):
        gate = GATE.read_text(encoding="utf-8")
        outside = gate.replace(CLOSE, CLOSE + '\n  git -C "$REPO_PATH" checkout "$HEAD_SHA"', 1)
        self.assertEqual(guard_problems(outside, False)[0], [], "a checkout after the closing marker is the unguarded path")
        self.assertTrue(guard_problems(gate.replace(CLOSE, "# closed", 1), False)[0])
        self.assertTrue(guard_problems(gate.replace(OPEN, "# opened", 1), False)[0])
        self.assertTrue(guard_problems("echo hi\n", False)[0])


# ---------------------------------------------------------------- the lifecycle

NEW_TEST = ("diff --git a/tests/test_new.py b/tests/test_new.py" + chr(10) + "new file mode 100644" + chr(10) + "--- /dev/null" + chr(10)
            + "+++ b/tests/test_new.py" + chr(10) + "@@ -0,0 +1,2 @@" + chr(10) + "+def test_n():" + chr(10) + "+    assert True" + chr(10))

GATE_GH_WRAPPER = '''
import json, os, pathlib, subprocess, sys
d = pathlib.Path(os.environ["MC_STUB_DIR"])
inputs = json.loads((d / "inputs.json").read_text()) if (d / "inputs.json").exists() else {}
env = dict(os.environ)
if inputs.get("lane") == "gate":              # a gate returns a verify.json and no patch; its facts must name this call's start sha
    import hashlib, io, tarfile
    bundle = d / ("gate-bundle-" + inputs["start_sha"][:12] + ".tar")
    with tarfile.open(bundle, "w:") as tf:
        for name, data in (("lane.patch", b""), ("raw.log", b""),
                           ("facts.json", json.dumps({"start_sha": inputs["start_sha"], "patch_sha256": hashlib.sha256(b"").hexdigest()}).encode())):
            ti = tarfile.TarInfo(name); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
    env["MC_STUB_BUNDLE"] = str(bundle)
if os.environ.get("MC_GATE_FAIL_VERIFY") and inputs.get("lane") == "gate" and inputs.get("start_sha") != os.environ["MC_GATE_BASE_SHA"]:
    env["MC_STUB_VERIFY"] = os.environ["MC_GATE_FAIL_VERIFY"]
sys.exit(subprocess.run([sys.executable, os.environ["MC_REAL_GH_STUB"], *sys.argv[1:]], env=env).returncode)
'''


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.r = r = T.LaneRepo("lenovo")
        self.t = Path(r.tmp.name)
        self.rec = Recorder(self.t)
        self.tool = "TOOLBYTES-" + secrets.token_hex(6)
        self.tool2 = "TOOLBYTES-" + secrets.token_hex(6)
        # the main checkout stands on a pushed branch (what `delegate` needs); origin's base branch is main
        real("checkout", "-q", "-b", "feat-x", cwd=r.main)
        real("push", "-q", "origin", "feat-x", cwd=r.main)
        self.guard = T.write_guard_file(self.t, {"version": 1, "repos": [{"slug": "owner/product", "common_dir": str(r.main / ".git")}]})

        def cfg(c):
            c["base_branch"] = "main"
            c["repo"] = {"path": str(r.main), "remote": "https://github.com/owner/product.git"}
            c["pilot"]["tools"].append({"id": "codex", "via": "codex-delegate", "model": "gpt-5.6-luna"})
        r.set_cfg(cfg)
        r.extra_env = {"MC_GUARDED_REPOS_FILE": str(self.guard), "MC_STUB_COMPARE": "identical", "MC_STUB_COMPARE_STRICT": "1",
                       "MC_STUB_EXEC_LOG": "ok from the runner" + chr(10), "MC_STUB_EXEC_JSON": json.dumps({"rc": 0})}
        self.gh_dir = self.t / "gh-stub-bin"
        self.gh_dir.mkdir()
        text = (TESTS / "test-merge-gate.sh").read_text(encoding="utf-8")
        m = re.search(r"cat > \"\$TMP/bin/gh\" <<'STUB'\n(.*?)\nSTUB\n", text, re.S)
        self.assertIsNotNone(m, "the gh stub of test-merge-gate.sh was not found")
        (self.gh_dir / "gh").write_bytes(m.group(1).replace("\r", "").encode("utf-8") + b"\n")
        (self.gh_dir / "gh").chmod(0o755)
        (self.t / "gate_gh.py").write_text(GATE_GH_WRAPPER, encoding="utf-8")
        self.fail_verify = self.t / "fail-verify.json"
        self.fail_verify.write_text(json.dumps({"status": "verify_failed", "commands": [{"cmd": "make test", "exit": 2, "tail": "boom", "phase": "verify"}]}))

    def tearDown(self):
        self.r.cleanup()

    def where(self, value):
        self.r.set_cfg(lambda c: c["pilot"]["sandbox"].update(where=value))

    def base_sha(self):
        return real("rev-parse", "refs/heads/main", cwd=self.r.origin)

    def gate(self, head, fail_head_verify=False, env_extra=None):
        """The real merge gate (bash) with the real lane-delegate behind it; gh is the stub of test-merge-gate.sh, the runner is the stub gh.py."""
        r = self.r
        e = r.env()
        e.update(STUB_OID=head, STUB_BASE="main", GH_LOG=str(self.t / "gh.log"), SB_GITHUB_RETRY_DELAY="0",
                 SB_GITHUB_HALT_FILE=str(self.t / ("halt-%s.json" % secrets.token_hex(3))), MC_GUARDED_REPOS_FILE=str(self.guard),
                 PATH=str(self.gh_dir) + os.pathsep + e["PATH"], MC_REAL_GH_STUB=str(r.stub / "gh.py"),
                 MC_LANE_GH_CMD=json.dumps([sys.executable, str(self.t / "gate_gh.py")]), MC_GATE_BASE_SHA=self.base_sha())
        if fail_head_verify:
            e["MC_GATE_FAIL_VERIFY"] = str(self.fail_verify)
        e.update(env_extra or {})
        p = subprocess.run([BASH, str(GATE), "--config", str(r.config), "--pr", "1", "--lock-timeout", "6", "--expect-head", head],
                           capture_output=True, text=True, env=e, timeout=600, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout + p.stderr

    def scan_for(self, needles, *roots):
        found = []
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for f in filenames:
                    data = (Path(dirpath) / f).read_bytes()
                    found += [str(Path(dirpath, f)) for n in needles if n.encode() in data]
        return found

    def test_the_whole_lifecycle_never_checks_out_a_sha_that_is_not_on_origin_base(self):
        r, rec = self.r, self.rec
        tool = self.tool

        def build(copy):
            (copy / "kwd.py").write_text("def f():\n    return 2  # %s\n" % tool)
            (copy / "lib_o1.py").write_text("X = '%s'\n" % tool)

        # 1. the card: run, then verify (a sandbox run, then the commit built from objects and pushed by sha)
        self.where("local")
        r.simulate(build)
        with rec.on():
            p, res = r.run()
        self.assertIsNotNone(res, p.stderr)
        self.assertEqual(res["kind"], "ok", p.stderr)
        with rec.on():
            rc, v, vp = r.verify()
        self.assertEqual((rc, v["kind"]), (0, "ok"), vp.stderr)
        card_tip = v["commit"]
        self.assertEqual(r.origin_tip(), card_tip)

        # 2. QA: commit-onto the tainted tip
        r.pr_head = (card_tip, "issue-1-x", 7)
        patch, msg = self.t / "claude.patch", self.t / "msg.txt"
        patch.write_bytes(NEW_TEST.encode("utf-8"))
        msg.write_text("claude: add a test", encoding="utf-8")
        with rec.on():
            p = r.inproc("commit-onto", "--config", str(r.config), "--repo", str(r.main), "--card", "1", "--patch", str(patch), "--message-file", str(msg))
        self.assertEqual(p.returncode, 0, p.stderr)
        qa_tip = json.loads(p.stdout)["commit"]
        self.assertEqual(r.origin_tip(), qa_tip)
        self.assertNotEqual(qa_tip, card_tip)

        # 3. Review: exec --sandbox --ref on the Lenovo (the remote is the stub gh)
        self.where("lenovo")
        (r.stub / "inputs.json").unlink(missing_ok=True)             # a stale file from an earlier step must not satisfy the assertion below
        with rec.on():
            p = r.inproc("exec", "--sandbox", "--config", str(r.config), "--dir", str(r.wt), "--ref", qa_tip, "--branch", "issue-1-x", "--", "pytest", "-q")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((r.stub / "inputs.json").exists(), "exec --ref never dispatched")
        dispatched = json.loads((r.stub / "inputs.json").read_text())
        self.assertEqual((dispatched.get("lane"), dispatched.get("start_sha")), ("exec", qa_tip), "exec --ref dispatched the wrong lane or sha")

        # 4. the merge gate. The PR head is the QA tip. 0: green. 2: the head's verify fails, the base's passes. 5: the base moved and conflicts.
        real("update-ref", "refs/heads/feat", qa_tip, cwd=r.origin)
        with rec.on():
            rc0, out0 = self.gate(qa_tip)
        self.assertEqual(rc0, 0, out0)
        self.assertIn("verified green", out0)
        with rec.on():
            rc2, out2 = self.gate(qa_tip, fail_head_verify=True)
        self.assertEqual(rc2, 2, out2)
        self.assertIn("send it back to Build for a rebase pass", out2)
        clone = self.t / "base-mover"
        real("clone", "-q", "-b", "main", str(r.origin), str(clone))
        (clone / "kwd.py").write_text("def f():\n    return 3  # the base moved\n")
        real("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "base moves", cwd=clone)
        real("push", "-q", "origin", "HEAD:main", cwd=clone)
        with rec.on():
            rc5, out5 = self.gate(qa_tip)
        self.assertEqual(rc5, 5, out5)
        self.assertIn("conflicts: kwd.py", out5)

        # 5. delegate write (main checkout on feat-x, pushed)
        tool2 = self.tool2
        r.simulate(lambda copy: (copy / "delegated.py").write_text("D = '%s'\n" % tool2))
        with rec.on():
            p = r.inproc("delegate", "--config", str(r.config), "--repo", str(r.main), "--tool", "codex", "--model", "gpt-5.6-luna",
                         "--mode", "write", "--brief", str(r.brief))
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertEqual(out["mode"], "write")

        # A. every sha the log names is reachable from origin/main
        text = rec.log.read_text(encoding="utf-8")
        calls = parse_log(text)
        touching = [(c["sub"], c["rest"]) for c in calls if tree_touching(c["sub"], c["rest"])]
        scrubbed = sum(1 for c in calls if c["scrubbed"])
        print("o1: %d git calls recorded, %d made with a scrubbed env, %d tree-touching: %s" % (len(calls), scrubbed, len(touching), touching[:6]), flush=True)
        self.assertGreater(scrubbed, 0, "no git call made with a scrubbed env was recorded: the shim is blind to lane-delegate's own git")
        self.assertGreaterEqual(sum(1 for c in calls if c["sub"] == "merge-tree"), 3, "the gate never ran merge-tree: the shim saw nothing\n" + text[-1500:])
        self.assertEqual(unreachable(text, r.origin, protected=(r.main, r.wt)), [], "tree-touching git calls on shas that are not on origin/main")
        # B. no byte the tool wrote is a file in the main checkout or the card worktree
        self.assertEqual(self.scan_for([tool, tool2], r.main, r.wt), [])
        # the tool's commits really are off origin/main (so the assertions above had something to catch)
        for sha in (card_tip, qa_tip, out["commit"]):
            self.assertNotEqual(subprocess.run([REALGIT, "-C", str(r.origin), "merge-base", "--is-ancestor", sha, "refs/heads/main"]).returncode, 0)
        self.card_tip = card_tip

    def test_self_check_a_seeded_unreachable_sha_fails_the_assertion(self):
        r = self.r
        self.where("local")
        r.simulate(lambda copy: (copy / "kwd.py").write_text("def f():\n    return 2\n"))
        p, res = r.run()
        self.assertEqual((res or {}).get("kind"), "ok", p.stderr)
        rc, v, _ = r.verify()
        self.assertEqual(rc, 0, v)
        off_main = v["commit"]
        self.assertNotEqual(subprocess.run([REALGIT, "-C", str(r.origin), "merge-base", "--is-ancestor", off_main, "refs/heads/main"]).returncode, 0)
        base = r.start
        cwd = str(r.main)
        good = line(cwd, "checkout", "-q", base)
        bad = line(cwd, "-C", cwd, "checkout", "-q", off_main)
        self.assertEqual(unreachable(good, r.origin), [])
        self.assertEqual([b[2] for b in unreachable(good + bad, r.origin)], [off_main])
        for sub in (["worktree", "add", "--detach", "/x/y", off_main], ["switch", "-d", off_main], ["merge", off_main], ["pull", "origin", off_main],
                    ["rebase", off_main], ["cherry-pick", off_main], ["revert", off_main], ["am", off_main], ["restore", "--source", off_main],
                    ["reset", "--hard", off_main], ["reset", "--keep", off_main], ["reset", "--merge", off_main], ["clone", off_main, "/x/y"],
                    ["read-tree", "-um", base, off_main], ["apply", "--index", off_main]):
            self.assertTrue(unreachable(line(cwd, *sub), r.origin), sub)
        for sub in (["apply", "--numstat", "--apply", "x.patch"], ["apply", "--stat", "--app", "x.patch"], ["apply", "--check", "--appl", "x.patch"]):
            self.assertTrue(tree_touching(sub[0], sub[1:]), "apply with --apply writes the tree even next to a read-only flag: %s" % sub)
        for fine in (["apply", "--cached", "x.patch", off_main], ["merge-tree", "--write-tree", base, off_main], ["clone", "--no-checkout", off_main, "/x/y"],
                     ["clone", "--bare", off_main, "/x/y"], ["reset", "--soft", off_main], ["read-tree", base, off_main]):
            self.assertEqual(unreachable(line(cwd, *fine), r.origin), [], fine)

    def test_self_check_resolution_happens_at_call_time_and_a_missing_dir_or_unresolved_token_fails(self):
        r, rec = self.r, self.rec
        ctl = self.t / "control-calltime"
        real("clone", "-q", "-b", "main", str(r.origin), str(ctl))
        base = real("rev-parse", "HEAD", cwd=ctl)
        log = self.t / "calltime.log"
        log.write_text("")
        with rec.on(log):
            subprocess.run(["git", "-C", str(ctl), "checkout", "-q", "main"], check=True, capture_output=True)            # the ref resolves now ...
            subprocess.run(["git", "-C", str(ctl), "checkout", "-q", "no-such-ref"], capture_output=True)                  # ... this one never does
            subprocess.run(["git", "-C", str(self.t / "gone-dir"), "checkout", "-q", base], capture_output=True)           # ... and this dir does not exist
        shutil.rmtree(ctl, ignore_errors=True)                                                                            # the clone is gone before the analysis
        why = {tok: w for _, tok, w in unreachable(log.read_text(encoding="utf-8"), r.origin)}
        self.assertEqual(why, {"no-such-ref": "does not resolve to a commit", "*": "the call directory did not exist"})

    def test_self_check_a_shim_that_cannot_write_its_log_exits_97(self):
        d = self.rec.shim_dir(self.t / "no-such-dir" / "x.log")
        p = subprocess.run([sys.executable, str(d / "gitshim.py"), "--version"], capture_output=True, text=True)
        self.assertEqual(p.returncode, 97, p.stderr)
        self.assertIn("cannot write the log", p.stderr)

    def test_self_check_post_state_and_protected_apply_are_judged(self):
        r, rec = self.r, self.rec
        cwd = str(r.main)
        base = r.start
        base_tree = real("rev-parse", base + "^{tree}", cwd=r.main)
        bogus_tree = "e" * 40
        off = "f" * 40
        # judged from a log: HEAD and the index tree after a call that names no token
        ok = line(cwd, "reset", "--hard") + "@P\t%s\t%s\n" % (base, base_tree)
        self.assertEqual(unreachable(ok, r.origin), [])
        head_bad = line(cwd, "reset", "--hard") + "@P\t%s\t%s\n" % (off, base_tree)
        self.assertEqual([b[1] for b in unreachable(head_bad, r.origin)], ["HEAD"])
        tree_bad = line(cwd, "stash", "pop") + "@P\t%s\t%s\n" % (base, bogus_tree)
        self.assertEqual([b[1] for b in unreachable(tree_bad, r.origin)], ["index"])
        # a non-exempt apply in the main checkout or the card worktree fails outright; elsewhere or exempt it does not
        for tree in (r.main, r.wt):
            self.assertEqual([b[2] for b in unreachable(line(str(tree), "apply", "x.patch"), r.origin, protected=(r.main, r.wt))], ["apply in a protected tree"])
            self.assertEqual([b[2] for b in unreachable(line(cwd, "-C", str(tree), "apply", "--numstat", "--apply", "x.patch"), r.origin, protected=(r.main, r.wt))],
                             ["apply in a protected tree"])
        self.assertEqual(unreachable(line(cwd, "apply", "--cached", "x.patch"), r.origin, protected=(r.main, r.wt)), [])
        self.assertEqual(unreachable(line(cwd, "apply", "--numstat", "x.patch"), r.origin, protected=(r.main, r.wt)), [])
        self.assertEqual(unreachable(line(str(self.t), "apply", "x.patch"), r.origin, protected=(r.main, r.wt)), [])
        # prefixes and last-option-wins flags turn tree writes back on: never exempt
        for flags in (["--stat", "--ap"], ["--cached", "--no-cached"], ["--numstat", "--no-numstat"], ["--check", "--a"]):
            self.assertFalse(apply_exempt(flags + ["x.patch"]), flags)
        self.assertTrue(apply_exempt(["--cached", "--whitespace=nowarn", "--binary", "-p1", "x.patch"]))
        # any tree-touching call in a protected tree fails outright; --opt=<sha> and -s<sha> values are judged
        self.assertEqual([b[2] for b in unreachable(line(cwd, "restore", "--source=" + base, "--", "."), r.origin, protected=(r.main, r.wt))],
                         ["tree-touching call in a protected tree"])
        self.assertEqual([b[2] for b in unreachable(line(str(self.t), "restore", "--source=" + off, "--", "."), r.origin)], [off])
        self.assertEqual([b[2] for b in unreachable(line(str(self.t), "restore", "-s" + off, "--", "."), r.origin)], [off])
        self.assertEqual(unreachable(line(str(self.t), "restore", "--source=" + base, "--", "."), r.origin), [])
        # and from the real shim: a local commit that is not on origin, then a reset --hard that names nothing
        ctl = self.t / "control-post"
        real("clone", "-q", "-b", "main", str(r.origin), str(ctl))
        (ctl / "local.txt").write_text("x")
        real("add", "local.txt", cwd=ctl)
        real("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "local only", cwd=ctl)
        log = self.t / "post.log"
        log.write_text("")
        with rec.on(log):
            subprocess.run(["git", "-C", str(ctl), "reset", "--hard"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(ctl), "checkout", "-q", "--", "."], check=True, capture_output=True)
        text = log.read_text(encoding="utf-8")
        self.assertIn("@P\t", text, "the shim recorded no post-state")
        self.assertEqual({b[1] for b in unreachable(text, r.origin)}, {"HEAD", "index"})

    def test_self_check_the_shim_records_python_and_bash_callers(self):
        r, rec = self.r, self.rec
        ctl = self.t / "control"
        real("clone", "-q", "-b", "main", str(r.origin), str(ctl))
        base = real("rev-parse", "HEAD", cwd=ctl)
        log = self.t / "control.log"
        log.write_text("")
        with rec.on(log):
            subprocess.run(["git", "-C", str(ctl), "checkout", "-q", "--detach", base], check=True, capture_output=True)
            subprocess.run([BASH, "-c", 'git -C "$1" checkout -q "$2"', "x", str(ctl), "main"], check=True, capture_output=True)
        subs = [(c["sub"], c["rest"]) for c in parse_log(log.read_text(encoding="utf-8")) if c["sub"] == "checkout"]
        self.assertGreaterEqual(len(subs), 2, "the shim missed a checkout: %s" % log.read_text())
        self.assertEqual(unreachable(log.read_text(encoding="utf-8"), r.origin), [], "a checkout of a base-reachable sha is fine")

    def test_self_check_a_call_made_with_a_scrubbed_env_is_still_logged(self):
        """lane-delegate runs most of its git with child_env/git_env, which drops every variable not on lane-env ALLOW (MC_GITLOG
        included). The shim must still log those calls: a control call with exactly that env has to appear in the log."""
        r, rec = self.r, self.rec
        ctl = self.t / "control-scrubbed"
        real("clone", "-q", "-b", "main", str(r.origin), str(ctl))
        base = real("rev-parse", "HEAD", cwd=ctl)
        log = self.t / "scrubbed.log"
        log.write_text("")
        with rec.on(log):
            env = T.D.git_env(self.t / "scrub")
            self.assertNotIn("MC_GITLOG", env, "the control env must be scrubbed")
            subprocess.run(["git", "-C", str(ctl), "checkout", "-q", "--detach", base], check=True, capture_output=True, env=env)
        calls = [c for c in parse_log(log.read_text(encoding="utf-8")) if c["sub"] == "checkout"]
        self.assertEqual(len(calls), 1, "a git call made with a scrubbed env never reached the log: %r" % log.read_text())
        self.assertTrue(calls[0]["scrubbed"], "the shim did not see the call as scrubbed")


if __name__ == "__main__":
    unittest.main(verbosity=1)
PYEOF

MC_O1_TESTS_DIR="$here" MC_O1_BASH="$bash_path" "$PY" -u "$tmp/o1.py" 2>&1
rc=$?
if [ "$rc" -ne 0 ]; then echo "FAIL: test-o1-lifecycle.sh (exit $rc)"; exit 1; fi
echo "PASS: test-o1-lifecycle.sh"
