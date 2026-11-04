#!/usr/bin/env python3
"""allow-path comments count only from trusted approvers, unedited, after --since (spec §6.4 step 3)."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lane_allow", ROOT / "scripts/super-board-lane-allowpath.py")
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)


def c(body, login="example-org", kind="User", at="2026-10-21T10:00:00Z", edited=None):
    return {"body": body, "user": {"login": login, "type": kind}, "created_at": at, "updated_at": edited or at}


def perm(login):
    return {"example-org": "admin", "example-bot": "write", "stranger": "read"}.get(login.lower())


class AllowPathTests(unittest.TestCase):
    SINCE = "2026-10-01T00:00:00Z"

    def allowed(self, comments, since=SINCE, **kw):
        return A.allowed_paths(comments, trusted=["example-org"], permission=perm, since=since, **kw)

    def test_trusted_comment_allows_exact_path(self):
        self.assertEqual(self.allowed([c("allow-path: package.json")]), {"package.json"})

    def test_multiple_lines(self):
        self.assertEqual(self.allowed([c("allow-path: a.lock\nallow-path: tsconfig.json")]), {"a.lock", "tsconfig.json"})

    def test_bot_and_untrusted_ignored(self):
        self.assertEqual(self.allowed([c("allow-path: x", login="example-bot"), c("allow-path: y", login="stranger")]), set())

    def test_edited_comment_ignored(self):
        self.assertEqual(self.allowed([c("allow-path: x", edited="2026-10-21T11:00:00Z")]), set())

    def test_before_since_ignored(self):
        self.assertEqual(self.allowed([c("allow-path: x", at="2026-10-20T00:00:00Z")], since="2026-10-21T00:00:00Z"), set())

    def test_wildcards_are_not_paths(self):
        self.assertEqual(self.allowed([c("allow-path: .github/*"), c("allow-path: ../x")]), set())

    def test_empty_trusted_list_allows_nothing(self):
        self.assertEqual(A.allowed_paths([c("allow-path: x")], trusted=[], permission=perm, since=self.SINCE), set())

    def test_since_is_required(self):
        for empty in (None, ""):
            self.assertEqual(self.allowed([c("allow-path: x")], since=empty), set())

    def test_bot_login_in_trusted_allows_nothing(self):
        self.assertEqual(self.allowed([c("allow-path: x")], bot_login="example-org"), set())

    def test_comments_by_bot_login_are_skipped(self):
        trusted = ["example-org", "Helper"]
        cm = [c("allow-path: x", login="Helper")]
        perms = lambda login: "admin"
        self.assertEqual(A.allowed_paths(cm, trusted, perms, self.SINCE), {"x"})
        self.assertEqual(A.allowed_paths(cm, trusted, perms, self.SINCE, bot_login="HELPER2"), {"x"})
        self.assertEqual(A.allowed_paths(cm, trusted, perms, self.SINCE, bot_login="helper"), set())

    def test_comments_by_actor_are_skipped(self):
        trusted = ["example-org", "Helper"]
        cm = [c("allow-path: x", login="Helper"), c("allow-path: y")]
        perms = lambda login: "admin"
        self.assertEqual(A.allowed_paths(cm, trusted, perms, self.SINCE, actor="HELPER"), {"y"})


if __name__ == "__main__":
    unittest.main()
