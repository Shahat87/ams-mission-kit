#!/usr/bin/env python3
"""GUARDRAIL: catches habit and mistakes; not a security boundary (spec §15.3).

mc_guarded: the guarded-repo list (spec §15.4 D-5), shared by guard-external-tools.py and guard-taint.py.

The list is <USERPROFILE or HOME>/.mission-control/guarded-repos.json:
  {"version": 1, "repos": [{"repo": "owner/name", "common_dir": "<realpath, forward slashes>" | null, "added": "<ISO-8601 UTC>"}]}
A repo is guarded when any remote (normalised) matches an entry, or its git common dir matches a recorded one. A missing,
unreadable or invalid list means EVERY repo is guarded (fail closed).

Nothing here runs git or any other subprocess: a guarded repo's .git/config may hold a hostile core.fsmonitor, so .git
files are read directly.

CLI (python -I mc_guarded.py ...):
  add --repo owner/name [--root DIR]   exit 0 and `guarded: owner/name (common dir: <path or none>)`; 64 bad slug / usage;
                                       70 the existing list is broken (never overwritten)
  check <path>                         prints `guarded` or `unguarded`
"""
import datetime
import json
import os
import posixpath
import re
import sys
import tempfile

SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PART = r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)"
REMOTE_RES = [re.compile(p, re.I) for p in (
    r"^https://(?:[^@/\s]+@)?github\.com/" + _PART + r"(?:\.git)?/?$",
    r"^ssh://git@github\.com(?::\d+)?/" + _PART + r"(?:\.git)?/?$",
    r"^git@github\.com:" + _PART + r"(?:\.git)?/?$",
    r"^github\.com:" + _PART + r"(?:\.git)?/?$",
)]


class BrokenListError(RuntimeError):
    """The guarded-repo list exists but cannot be read as a valid list; it must not be overwritten."""


def home():
    return os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~")


def guarded_file():
    return os.path.join(home(), ".mission-control", "guarded-repos.json")


def msys(p):
    """Git Bash paths (/c/Users/x) -> C:/Users/x on Windows."""
    if os.name != "nt" or not p.startswith("/"):
        return p
    p = posixpath.normpath(p)
    m = re.match(r"^/([A-Za-z])(?:/(.*))?$", p)
    return "%s:/%s" % (m.group(1), m.group(2) or "") if m else p


def norm(p):
    """C:\\x, /c/x and C:/x are the same path; forward slashes, normalised, not resolved."""
    return posixpath.normpath(msys(str(p).replace("\\", "/")))


def real(p):
    """Realpath with forward slashes."""
    try:
        return os.path.realpath(norm(p)).replace("\\", "/")
    except (OSError, ValueError):
        return norm(p)


def _key(p):
    return p.lower() if os.name == "nt" else p


def load():
    """The list entries, or None when the file is missing, unreadable or invalid."""
    try:
        with open(guarded_file(), encoding="utf-8-sig") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            return None
        repos = data.get("repos")
        if not isinstance(repos, list):
            return None
        for e in repos:
            if not isinstance(e, dict) or not isinstance(e.get("repo"), str) or not SLUG_RE.match(e["repo"]):
                return None
            if e.get("common_dir") is not None and not isinstance(e["common_dir"], str):
                return None
        return repos
    except (OSError, ValueError):
        return None


def normalize_remote(url, insteadof_rules):
    """'owner/name' (lowercase) for a github.com remote in a supported shape, else None. insteadOf rules are
    (instead_of_prefix, base) pairs, applied first, longest prefix wins, as git does."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    best = None
    for rule in insteadof_rules or ():
        prefix, base = rule
        if prefix and url.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, base)
    if best:
        url = best[1] + url[len(best[0]):]
    for rx in REMOTE_RES:
        m = rx.match(url)
        if m:
            return ("%s/%s" % (m.group(1), m.group(2))).lower()
    return None


def _strip_value(v):
    """A git config value: quotes honoured, an unquoted # or ; starts a comment."""
    out, quote, i = [], False, 0
    while i < len(v):
        c = v[i]
        if c == "\\" and i + 1 < len(v):
            nxt = v[i + 1]
            out.append({"n": "\n", "t": "\t", "b": "\b"}.get(nxt, nxt))
            i += 2
            continue
        if c == '"':
            quote = not quote
        elif not quote and c in "#;":
            break
        else:
            out.append(c)
        i += 1
    return "".join(out).strip()


def parse_config(path):
    """([remote urls], [(insteadOf, base)]) from a git config file; tolerant, no includes followed."""
    remotes, rules = [], []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return remotes, rules
    section = sub = None
    head = re.compile(r'^\s*\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]')
    for line in lines:
        m = head.match(line)
        if m:
            section, sub = m.group(1).lower(), m.group(2)
            if sub is not None:
                sub = re.sub(r"\\(.)", r"\1", sub)
            rest = line[m.end():].strip()
            if rest and not rest.startswith(("#", ";")):
                line = rest                     # `[remote "x"] url = y` on one line
            else:
                continue
        if section is None:
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip().lower(), _strip_value(val)
        if section == "remote" and key == "url" and val:
            remotes.append(val)
        elif section == "url" and key == "insteadof" and val and sub:
            rules.append((val, sub))
    return remotes, rules


def repo_info(path):
    """(toplevel, git_dir, common_dir) as forward-slash realpaths for the repo holding path, else None. Walks up to the
    first `.git`; follows a `gitdir:` file and a `commondir` file. Reads files only."""
    d = real(path)
    while True:
        dotgit = d + "/.git"
        if os.path.isdir(dotgit):
            gitdir = dotgit
            break
        if os.path.isfile(dotgit):
            try:
                with open(dotgit, encoding="utf-8", errors="replace") as fh:
                    first = fh.readline().strip()
            except OSError:
                return None
            if not first.lower().startswith("gitdir:"):
                return None
            target = norm(first.split(":", 1)[1].strip())
            if not (target.startswith("/") or re.match(r"^[A-Za-z]:/", target)):
                target = posixpath.join(d, target)
            gitdir = real(target)
            break
        parent = posixpath.dirname(d)
        if parent == d:
            return None
        d = parent
    common = gitdir
    try:
        with open(gitdir + "/commondir", encoding="utf-8", errors="replace") as fh:
            c = norm(fh.readline().strip())
        if c:
            common = real(c if (c.startswith("/") or re.match(r"^[A-Za-z]:/", c)) else posixpath.join(gitdir, c))
    except OSError:
        pass
    return d, real(gitdir), real(common)


def repo_facts(path):
    """(common_dir, [remote urls], insteadof_rules) for the repo holding path, or None when path is not in a repo."""
    info = repo_info(path)
    if not info:
        return None
    common = info[2]
    remotes, rules = parse_config(common + "/config")
    _, grules = parse_config(norm(home()) + "/.gitconfig")
    return common, remotes, rules + grules


def is_guarded(path):
    entries = load()
    if entries is None:
        return True
    facts = repo_facts(path)
    if facts is None:
        return False
    common, remotes, rules = facts
    slugs = {e["repo"].lower() for e in entries}
    commons = {_key(real(e["common_dir"])) for e in entries if e.get("common_dir")}
    if _key(common) in commons:
        return True
    return any(normalize_remote(u, rules) in slugs for u in remotes)


def add(repo, root=None):
    """Add (or update) a guarded repo; returns the entry's common_dir (or None). Never overwrites a broken file."""
    if not isinstance(repo, str) or not SLUG_RE.match(repo):
        raise ValueError("not an owner/name slug: %r" % (repo,))
    path = guarded_file()
    if os.path.exists(path):
        entries = load()
        if entries is None:
            raise BrokenListError("%s exists but is not a valid guarded-repo list; fix or remove it by hand" % path)
    else:
        entries = []
    common = None
    if root:
        facts = repo_facts(root)
        if facts and any(normalize_remote(u, facts[2]) == repo.lower() for u in facts[1]):
            common = facts[0]
    for e in entries:
        if e["repo"].lower() == repo.lower():
            if common:
                e["common_dir"] = common
            shown = e.get("common_dir")
            break
    else:
        entries.append({"repo": repo, "common_dir": common,
                        "added": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        shown = common
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".guarded-repos.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "repos": entries}, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return shown


def main(argv):
    if len(argv) >= 2 and argv[0] == "check" and len(argv) == 2:
        print("guarded" if is_guarded(argv[1]) else "unguarded")
        return 0
    if argv and argv[0] == "add":
        repo = root = None
        i = 1
        while i < len(argv):
            if argv[i] == "--repo" and i + 1 < len(argv):
                repo, i = argv[i + 1], i + 2
            elif argv[i] == "--root" and i + 1 < len(argv):
                root, i = argv[i + 1], i + 2
            else:
                print("usage: mc_guarded.py add --repo owner/name [--root DIR]", file=sys.stderr)
                return 64
        if repo is None:
            print("usage: mc_guarded.py add --repo owner/name [--root DIR]", file=sys.stderr)
            return 64
        try:
            common = add(repo, root)
        except ValueError as e:
            print("mc_guarded: %s" % e, file=sys.stderr)
            return 64
        except BrokenListError as e:
            print("mc_guarded: %s" % e, file=sys.stderr)
            return 70
        print("guarded: %s (common dir: %s)" % (repo, common or "none"))
        return 0
    print("usage: mc_guarded.py add --repo owner/name [--root DIR] | check <path>", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
