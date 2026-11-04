#!/usr/bin/env python3
"""Read `allow-path: <exact path>` lines from trusted approvers on a card's issue.

Pure function allowed_paths() for tests; main() reads the issue's comments with
the approval module's GitHub reader (same permission lookup, same trusted list).
"""
from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
from pathlib import Path
import re
import sys

LINE = re.compile(r"^\s*allow-path:\s*(\S+)\s*$", re.I | re.M)
BAD = re.compile(r"[*?\[\]]|(^|/)\.\.(/|$)|^[/\\]|^[A-Za-z]:")


def _t(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _norm(login):
    return str(login or "").strip().lower()


def allowed_paths(comments, trusted, permission, since, bot_login=None, actor=None):
    """`since` is required (falsy -> nothing). The robot (bot_login) and the acting login never count,
    and a bot_login that is itself trusted disables the feature (as super-board-approval.py does)."""
    allow = {str(t).lower() for t in trusted or [] if str(t).strip()}
    if not allow or not since:
        return set()
    skip = {n for n in (_norm(bot_login), _norm(actor)) if n}
    if _norm(bot_login) in allow:
        return set()
    out = set()
    for cm in comments:
        user = cm.get("user") or {}
        login = str(user.get("login") or "")
        if user.get("type") != "User" or login.lower() not in allow or login.lower() in skip:
            continue
        if permission(login) not in ("write", "maintain", "admin"):
            continue
        if not cm.get("created_at") or cm.get("updated_at") != cm.get("created_at"):
            continue
        if _t(cm["created_at"]) <= _t(since):
            continue
        for path in LINE.findall(cm.get("body") or ""):
            if not BAD.search(path):
                out.add(path)
    return out


def main():
    ap = argparse.ArgumentParser(description="list allow-path entries for an issue")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--bot-login")
    ap.add_argument("--actor")
    args = ap.parse_args()
    spec = importlib.util.spec_from_file_location("approval", Path(__file__).with_name("super-board-approval.py"))
    approval = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(approval)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    trusted = ((cfg.get("merge_policy") or {}).get("trusted_approvers")) or []
    bot_login = args.bot_login or approval.load_policy(args.config)[2]
    gh = approval.GitHub(args.repo)
    print(json.dumps(sorted(allowed_paths(gh.comments(args.issue), trusted, gh.permission, args.since,
                                          bot_login=bot_login, actor=args.actor))))


if __name__ == "__main__":
    sys.exit(main())
