#!/usr/bin/env python3
"""Read-only, head-bound human approval shared by the planner and merge gate.

A trusted request pins the code and human steps BEFORE a human replies `done`.
Labels only describe UI state. They never authorize a merge. No GitHub writes.
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from datetime import datetime
import hashlib
import json
import re
import subprocess
import sys

_reader_spec = importlib.util.spec_from_file_location("github_read", Path(__file__).with_name("super-board-github-read.py"))
github_read = importlib.util.module_from_spec(_reader_spec)
_reader_spec.loader.exec_module(github_read)

PREFIX = "approval-request: "
SHA = re.compile(r"[0-9a-f]{40}\Z")
SCOPE = re.compile(r"[0-9a-f]{64}\Z")
CLOSING = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#([0-9]+)\b")
BLOCK = re.compile(r"Reason tag:[^\n]*🙋|^approval-request:", re.M)
DONE = re.compile(r"\s*done[.!]?\s*\Z", re.I)


def scope_for(plan):
    # Exclude UI-only `done`; a label change must not change what was approved.
    relevant = {key: plan.get(key, []) for key in ("human", "migrations", "run", "needs_you")}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def request_for(repo, pr, head, scope):
    return {"repo": repo.lower(), "pr": int(pr), "head": head, "scope": scope}


def marker(request):
    return PREFIX + json.dumps(request, sort_keys=True, separators=(",", ":"))


def parse_request(comment):
    lines = [line for line in (comment.get("body") or "").splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        return None
    try:
        value = json.loads(lines[0][len(PREFIX):])
        if (not isinstance(value, dict) or not isinstance(value.get("repo"), str)
                or not re.fullmatch(r"[^/\s]+/[^/\s]+", value["repo"])
                or type(value.get("pr")) is not int or value["pr"] < 1
                or not SHA.fullmatch(value.get("head", ""))
                or not SCOPE.fullmatch(value.get("scope", ""))):
            return None
        return value
    except (ValueError, TypeError):
        return None


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing comment timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("comment timestamp lacks timezone")
    return result


def trusted_blocks(comments, permission):
    result = []
    for block in comments:
        if not BLOCK.search(block.get("body") or ""):
            continue
        user = block.get("user") or {}
        if not user.get("login") or user.get("type") not in ("User", "Bot"):
            raise ValueError("requester identity could not be verified")
        if permission(user["login"]) in ("write", "maintain", "admin"):
            result.append(block)
    return result


def validate(comments, expected, permission, trusted=None, actor=None, identity_pinned=False, bot_login=None):
    """Pure verdict; permission(login) is a current repository permission lookup.

    trusted: merge_policy.trusted_approvers (logins). When non-empty, a `done`
    counts only from a listed login that is not the acting (merging) login; an
    unknown actor holds. Empty/None keeps the permission-only behaviour, EXCEPT
    when identity_pinned (notifications.bot_identity is a real login): then an
    empty list holds, because the robot's own `done` could otherwise approve."""
    allow = {str(t).lower() for t in trusted or [] if str(t).strip()}
    def hold(why):
        return {"approved": False, "why": why}

    if not SHA.fullmatch(expected.get("head", "")):
        return hold("current PR head is unavailable")
    if bot_login and str(bot_login).strip().lower() in allow:
        return hold(f"notifications.bot_identity {bot_login} is also in merge_policy.trusted_approvers; "
                    "the pin requires a separate machine-user account")
    if identity_pinned and not allow:
        return hold("notifications.bot_identity is set but merge_policy.trusted_approvers is empty; "
                    "set trusted_approvers to the humans who may approve")
    if allow and not actor:
        return hold("acting login could not be determined; trusted_approvers cannot be enforced")
    try:
        blocks = trusted_blocks(comments, permission)
        if not blocks:
            return hold("no request from a trusted repository collaborator")
        # Any later human block supersedes the old request, including one with
        # missing/malformed metadata. Equal-second requests are ambiguous: hold.
        latest_time = max(timestamp(c.get("created_at")) for c in blocks)
        latest = [c for c in blocks if timestamp(c.get("created_at")) == latest_time]
        if len(latest) != 1:
            return hold("ambiguous newest human request")
        request_comment = latest[0]
        request = parse_request(request_comment)
        if request is None or any(request.get(k) != v for k, v in expected.items()):
            return hold("the latest request does not cover the current code and human steps")
        if timestamp(request_comment.get("updated_at")) != latest_time:
            return hold("approval request was edited; post a fresh request")
        requester = request_comment.get("user") or {}
        if not requester.get("login") or permission(requester["login"]) not in ("write", "maintain", "admin"):
            return hold("requester authority could not be verified")
        if not isinstance(request_comment.get("source"), int) or not request_comment.get("id"):
            return hold("request identity is missing")
        for comment in comments:
            if (not comment.get("id") or not DONE.fullmatch(comment.get("body") or "")
                    or comment.get("source") != request_comment.get("source")):
                continue
            created = timestamp(comment.get("created_at"))
            if created <= latest_time or timestamp(comment.get("updated_at")) != created:
                continue
            author = comment.get("user") or {}
            if allow and (str(author.get("login") or "").lower() not in allow
                          or str(author.get("login") or "").lower() == str(actor).lower()):
                continue
            if (author.get("type") == "User" and author.get("login")
                    and permission(author["login"]) in ("write", "maintain", "admin")):
                return {"approved": True, "why": "trusted human confirmed the current request",
                        "request": request, "requestId": request_comment.get("id"),
                        "approvalId": comment.get("id"), "approver": author["login"]}
        return hold("waiting for a trusted human to comment done on the current request")
    except (ValueError, TypeError, KeyError):
        return hold("approval evidence is incomplete or unreadable")


class GitHub:
    def __init__(self, repo):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("missing or invalid repository")
        self.repo = repo.lower()
        self.permissions = {}

    @staticmethod
    def read(*args):
        kind = ("approval" if "graphql" in args else "comments" if "--paginate" in args
                else "permission" if args[-1].endswith("/permission") else "json")
        return github_read.read(list(args), kind)[1]

    def permission(self, login):
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\[bot\])?", login):
            return None
        if login not in self.permissions:
            value = self.read("api", f"repos/{self.repo}/collaborators/{login}/permission")
            self.permissions[login] = value.get("permission")
        return self.permissions[login]

    def comments(self, number):
        pages = self.read("api", "--paginate", "--slurp",
                          f"repos/{self.repo}/issues/{number}/comments?per_page=100")
        if not isinstance(pages, list) or not pages or any(not isinstance(p, list) for p in pages):
            raise ValueError("unreadable comments")
        if any(not isinstance(c, dict) for page in pages for c in page):
            raise ValueError("unreadable comment")
        return [dict(comment, source=number) for page in pages for comment in page]

    def pr(self, number):
        # Paginate linked issues too: an omitted newer request must never make
        # an older approval authoritative. Remote-repository links are ignored.
        # GitHub fills closingIssuesReferences only for PRs into the default
        # branch, so into any other base the body's closing keywords are read.
        query = '''query($owner:String!,$repo:String!,$pr:Int!,$endCursor:String){
          repository(owner:$owner,name:$repo){defaultBranchRef{name}
           pullRequest(number:$pr){headRefOid state baseRefName body
            closingIssuesReferences(first:100,after:$endCursor){
              pageInfo{hasNextPage endCursor} nodes{number repository{nameWithOwner}}
            }}}}'''
        owner, name = self.repo.split("/")
        pages = self.read("api", "graphql", "--paginate", "--slurp", "-f", f"query={query}",
                          "-F", f"owner={owner}", "-F", f"repo={name}", "-F", f"pr={number}")
        heads, issues, bases, defaults, bodies = set(), set(), set(), set(), set()
        if not isinstance(pages, list) or not pages:
            raise ValueError("unreadable PR")
        for page in pages:
            if page.get("errors"):
                raise ValueError("incomplete PR response")
            repository = page["data"]["repository"]
            pr = repository["pullRequest"]
            if pr["state"] != "OPEN":
                raise ValueError("PR is not open")
            heads.add(pr["headRefOid"])
            base = pr.get("baseRefName")
            default = (repository.get("defaultBranchRef") or {}).get("name")
            if not isinstance(base, str) or not base or not isinstance(default, str) or not default:
                raise ValueError("unreadable PR base branch")
            bases.add(base)
            defaults.add(default)
            body = pr.get("body")
            bodies.add(body if isinstance(body, str) else "")
            links = pr["closingIssuesReferences"]
            for issue in links["nodes"]:
                if issue["repository"]["nameWithOwner"].lower() == self.repo:
                    issues.add(issue["number"])
        if (links["pageInfo"]["hasNextPage"] or len(heads) != 1 or len(bases) != 1
                or len(defaults) != 1 or len(bodies) != 1):
            raise ValueError("PR changed or issue links are incomplete")
        if bases != defaults:
            for repo, num in CLOSING.findall(bodies.pop()):
                if (not repo or repo.lower() == self.repo) and int(num) > 0:
                    issues.add(int(num))
        head = heads.pop()
        if not SHA.fullmatch(head):
            raise ValueError("unreadable PR head")
        return head, issues

    def acting_login(self):
        return (self.read("api", "user") or {}).get("login")

    def check(self, number, head=None, scope=None, issue=None, trusted=None, identity_pinned=False, bot_login=None):
        current_head, issues = self.pr(number)
        if issue is not None and issue not in issues:
            return {"approved": False, "why": "request issue is not linked to this PR"}
        if head is not None and current_head != head:
            return {"approved": False, "headChanged": True, "why": "PR head changed after review"}
        comments = []
        for source in sorted(issues | {number}):
            comments.extend(self.comments(source))
        expected = {"repo": self.repo, "pr": number, "head": current_head}
        if scope is not None:
            expected["scope"] = scope
        actor = None
        if trusted:
            try:
                actor = self.acting_login()
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
                actor = None
        return validate(comments, expected, self.permission, trusted, actor, identity_pinned, bot_login)


def load_policy(path):
    """(trusted_approvers or None, bot_identity is a pinnable login, that login or None).

    bot_identity is trimmed; case-insensitive "auto" = unset; a "[bot]" login is
    GitHub App mode: still pinned for the fail-closed rule; only the gate's
    `gh api user` actor check is skipped for it."""
    if not path:
        return None, False, None
    with open(path, encoding="utf-8") as handle:
        cfg = json.load(handle)
    trusted = ((cfg.get("merge_policy") or {}).get("trusted_approvers")) or None
    note = cfg.get("notifications")
    ident = note.get("bot_identity") if isinstance(note, dict) else None
    ident = ident.strip() if isinstance(ident, str) else ""
    login = None if ident.lower() in ("", "auto") else ident
    return trusted, login is not None, login


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int)
    ap.add_argument("--head")
    ap.add_argument("--plan", help="JSON plan from merge-policy")
    ap.add_argument("--config", help="super-board config; reads merge_policy.trusted_approvers")
    ap.add_argument("--request", action="store_true", help="print a pinned request; no network or writes")
    ap.add_argument("--deps", action="store_true", help="enrich the dependency graph on stdin")
    args = ap.parse_args()
    if args.request:
        if not args.pr or not args.head or not SHA.fullmatch(args.head) or not args.plan:
            ap.error("request requires --pr, full --head and --plan")
        print(marker(request_for(args.repo, args.pr, args.head, scope_for(json.loads(args.plan)))))
        return
    if args.deps:
        trusted, pinned, login = load_policy(args.config)
        graph = json.load(sys.stdin)
        github = GitHub(args.repo) if args.repo else None
        for entry in graph.values():
            entry["needsYouDone"] = False
            entry["approvalRefresh"] = False
            if not entry.get("needsYou") or github is None:
                continue
            try:
                comments = github.comments(entry["number"])
                blocks = trusted_blocks(comments, github.permission)
                latest = max(blocks, key=lambda c: timestamp(c.get("created_at"))) if blocks else {}
                request = parse_request(latest)
                if request is None or request["repo"] != github.repo:
                    continue
                result = github.check(request["pr"], head=request["head"], issue=entry["number"],
                                      trusted=trusted, identity_pinned=pinned, bot_login=login)
                entry["needsYouDone"] = result["approved"]
                entry["approvalRefresh"] = result.get("headChanged", False)
                entry["approvalWhy"] = result["why"]
            except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
                entry["approvalWhy"] = "approval evidence could not be verified"
        print(json.dumps(graph))
        return
    if not args.pr or not args.head or not args.plan:
        ap.error("check requires --pr, --head and --plan")
    try:
        trusted, pinned, login = load_policy(args.config)
        result = GitHub(args.repo).check(args.pr, args.head, scope_for(json.loads(args.plan)),
                                         trusted=trusted, identity_pinned=pinned, bot_login=login)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        result = {"approved": False, "why": "approval evidence could not be verified"}
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except github_read.ReadHalted as error:
        print(f"GitHub read halt: {error}", file=sys.stderr)
        sys.exit(github_read.HALTED)
