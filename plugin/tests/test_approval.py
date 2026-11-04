#!/usr/bin/env python3
"""Offline approval lifecycle tests: no board writes, workers or databases."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("approval", ROOT / "scripts/super-board-approval.py")
approval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(approval)
HEAD = "a" * 40
PLAN = {"human": [{"category": "auth", "why": "login"}], "needs_you": ["run live migration"]}
REQUEST = approval.request_for("x/y", 9, HEAD, approval.scope_for(PLAN))


def comment(body, second, login="owner", actor="User", source=3):
    at = f"2026-10-03T00:00:{second:02d}Z"
    return {"id": second, "body": body, "created_at": at, "updated_at": at,
            "user": {"login": login, "type": actor}, "source": source}


def evidence():
    return [comment("Reason tag: 🙋 needs you\n" + approval.marker(REQUEST), 1), comment("done", 2)]


class ApprovalTests(unittest.TestCase):
    def check(self, comments=None, expected=None, permissions=None):
        return approval.validate(comments if comments is not None else evidence(), expected or REQUEST,
                                 lambda login: (permissions or {"owner": "admin"}).get(login))

    def test_current_request_allows_solo_owner(self):
        self.assertTrue(self.check()["approved"])

    def test_old_approval_never_covers_new_head(self):
        self.assertFalse(self.check(expected=dict(REQUEST, head="b" * 40))["approved"])

    def test_done_before_processing_does_not_get_rebound_to_new_head(self):
        records = evidence()
        self.assertFalse(self.check(records, dict(REQUEST, head="c" * 40))["approved"])

    def test_changed_human_steps_revoke_same_head(self):
        newer = dict(REQUEST, scope=approval.scope_for(dict(PLAN, needs_you=["different operation"])))
        self.assertFalse(self.check(expected=newer)["approved"])

    def test_label_only_or_bare_done_is_not_approval(self):
        self.assertFalse(self.check([comment("done", 2)])["approved"])

    def test_new_block_invalidates_old_done_even_without_marker(self):
        self.assertFalse(self.check(evidence() + [comment("Reason tag: 🙋 needs a new decision", 3)])["approved"])

    def test_new_request_requires_new_done(self):
        newer = evidence() + [comment(approval.marker(REQUEST), 3)]
        self.assertFalse(self.check(newer)["approved"])
        newer.append(comment("Done!", 4))
        self.assertTrue(self.check(newer)["approved"])

    def test_wrong_pr_or_repository_is_rejected(self):
        for key, value in (("pr", 10), ("repo", "other/repo")):
            self.assertFalse(self.check(expected=dict(REQUEST, **{key: value}))["approved"])

    def test_untrusted_approval_and_request_are_rejected(self):
        for index in (0, 1):
            records = evidence()
            records[index]["user"]["login"] = "visitor"
            self.assertFalse(self.check(records)["approved"])
        self.assertFalse(self.check(permissions={"owner": "read"})["approved"])

    def test_verified_non_writer_cannot_replace_trusted_request(self):
        records = evidence() + [comment("Reason tag: 🙋 forged request", 3, login="visitor")]
        self.assertTrue(self.check(records, permissions={"owner": "admin", "visitor": "read"})["approved"])

    def test_bot_done_is_not_a_human_approval(self):
        records = evidence()
        records[1]["user"]["type"] = "Bot"
        self.assertFalse(self.check(records)["approved"])

    def test_missing_actor_or_head_holds(self):
        records = evidence()
        del records[1]["user"]["type"]
        self.assertFalse(self.check(records)["approved"])
        self.assertFalse(self.check(expected=dict(REQUEST, head=""))["approved"])

    def test_edited_request_or_done_holds(self):
        for index in (0, 1):
            records = evidence()
            records[index]["updated_at"] = "2026-10-03T00:00:05Z"
            self.assertFalse(self.check(records)["approved"])

    def test_done_on_another_thread_does_not_confirm_request(self):
        records = evidence()
        records[1]["source"] = 9
        self.assertFalse(self.check(records)["approved"])

    def test_new_pr_request_invalidates_issue_approval(self):
        self.assertFalse(self.check(evidence() + [comment(approval.marker(REQUEST), 3, source=9)])["approved"])

    def test_ambiguous_same_second_requests_hold(self):
        self.assertFalse(self.check(evidence() + [comment(approval.marker(REQUEST), 1, source=9)])["approved"])

    def test_repeat_checks_do_not_invalidate_approval(self):
        records = evidence()
        self.assertEqual(self.check(records), self.check(records))
        self.assertTrue(self.check(records)["approved"])

    def test_ui_done_label_does_not_change_scope(self):
        self.assertEqual(approval.scope_for(PLAN), approval.scope_for(dict(PLAN, done=True)))

    def test_missing_time_and_malformed_marker_hold(self):
        records = evidence()
        del records[0]["created_at"]
        self.assertFalse(self.check(records)["approved"])
        records = evidence()
        records[0]["body"] = "approval-request: {broken"
        self.assertFalse(self.check(records)["approved"])


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.github = approval.GitHub("x/y")
        self.pr_page = {"data": {"repository": {"defaultBranchRef": {"name": "main"},
            "pullRequest": {"headRefOid": HEAD, "state": "OPEN", "baseRefName": "main", "body": "",
            "closingIssuesReferences": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"number": 3, "repository": {"nameWithOwner": "x/y"}}]}}}}}
        self.calls = []
        self.records = evidence()
        self.permission_error = False
        def read(*args):
            self.calls.append(args)
            if "graphql" in args:
                return [self.pr_page]
            if "collaborators" in args[-1]:
                if self.permission_error:
                    raise ValueError("unavailable")
                return {"permission": "admin"}
            if "/issues/3/" in args[-1]:
                # Approval is on page 2; taking only the first page would hold.
                return [[self.records[0]], self.records[1:]]
            if "/issues/9/" in args[-1]:
                return [[]]
            raise AssertionError(args)
        self.github.read = read

    def test_paginated_comments_and_current_permission_are_used(self):
        self.assertTrue(self.github.check(9, HEAD, REQUEST["scope"])["approved"])
        self.assertTrue(any("--paginate" in c for c in self.calls))
        self.assertTrue(any("/collaborators/owner/permission" in c[-1] for c in self.calls))

    def test_changed_remote_head_holds(self):
        self.pr_page["data"]["repository"]["pullRequest"]["headRefOid"] = "b" * 40
        self.assertFalse(self.github.check(9, HEAD, REQUEST["scope"])["approved"])

    def test_incomplete_link_pagination_is_not_accepted(self):
        self.pr_page["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["pageInfo"]["hasNextPage"] = True
        with self.assertRaises(ValueError):
            self.github.check(9, HEAD, REQUEST["scope"])

    def test_unlinked_issue_cannot_supply_approval(self):
        self.assertFalse(self.github.check(9, HEAD, REQUEST["scope"], issue=77)["approved"])

    def staging(self, body, linked=False, base="staging"):
        pr = self.pr_page["data"]["repository"]["pullRequest"]
        pr["baseRefName"] = base
        pr["body"] = body
        if not linked:
            pr["closingIssuesReferences"]["nodes"] = []
        # request + done live on issue 3 (the fake's evidence thread)
        return pr

    def test_non_default_base_body_closes_links_issue(self):
        self.staging("Closes #3")
        self.assertTrue(self.github.check(9, HEAD, REQUEST["scope"], issue=3)["approved"])

    def test_non_default_base_gate_path_reads_body_linked_comments(self):
        self.staging("Closes #3")
        self.assertTrue(self.github.check(9, HEAD, REQUEST["scope"])["approved"])

    def test_default_base_ignores_body_link(self):
        self.staging("Closes #3", base="main")
        result = self.github.check(9, HEAD, REQUEST["scope"], issue=3)
        self.assertFalse(result["approved"])
        self.assertEqual(result["why"], "request issue is not linked to this PR")

    def test_other_repo_body_link_is_ignored(self):
        self.staging("Closes other/repo#3")
        result = self.github.check(9, HEAD, REQUEST["scope"], issue=3)
        self.assertFalse(result["approved"])
        self.assertEqual(result["why"], "request issue is not linked to this PR")

    def test_missing_base_ref_name_fails_closed(self):
        pr = self.staging("Closes #3")
        del pr["baseRefName"]
        with self.assertRaises(ValueError):
            self.github.check(9, HEAD, REQUEST["scope"], issue=3)

    def test_body_keyword_variants(self):
        for body, linked in (("fixes #3", True), ("Resolved: #3", True), ("closes x/y#3", True),
                             ("#3", False), ("see #3", False)):
            with self.subTest(body=body):
                self.staging(body)
                _, issues = self.github.pr(9)
                self.assertEqual(3 in issues, linked)

    def test_unreadable_permission_never_approves(self):
        self.permission_error = True
        self.assertFalse(self.github.check(9, HEAD, REQUEST["scope"])["approved"])


class DependencyIntegrationTests(unittest.TestCase):
    def test_live_planner_resume_requires_current_trusted_request(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            records = evidence()
            records.append(comment("Reason tag: 🙋 forged request", 3, login="visitor"))
            issue = {"number": 3, "title": "Login", "state": "OPEN", "body": "## Blocked by\n- None.",
                     "labels": {"nodes": [{"name": "needs-you:done"}, {"name": "needs-you"}]},
                     "comments": {"nodes": [{"body": c["body"]} for c in records]}}
            fixture = {"records": records, "head": HEAD, "issue": issue}
            fixture_path = work / "fixture.json"
            stub = work / "gh"
            stub.write_text('''#!/usr/bin/env python3
import json, os, sys
f=json.load(open(os.environ["APPROVAL_FIXTURE"]))
args=" ".join(sys.argv[1:])
if "issues(states:OPEN" in args:
    out=[{"data":{"repository":{"issues":{"nodes":[f["issue"]],"pageInfo":{"hasNextPage":False,"endCursor":None}}}}}]
elif "graphql" in args:
    out=[{"data":{"repository":{"defaultBranchRef":{"name":"main"},"pullRequest":{"headRefOid":f["head"],"state":"OPEN","baseRefName":"main","body":"","closingIssuesReferences":{"pageInfo":{"hasNextPage":False},"nodes":[{"number":3,"repository":{"nameWithOwner":"x/y"}}]}}}}}]
elif "/issues/3/comments" in args:
    out=[f["records"]]
elif "/issues/9/comments" in args:
    out=[[]]
elif "/collaborators/owner/permission" in args:
    out={"permission":"admin"}
elif "/collaborators/visitor/permission" in args:
    out={"permission":"read"}
else:
    sys.exit(1)
print(json.dumps(out))
''')
            stub.chmod(0o755)
            env = dict(os.environ, PATH=str(work) + os.pathsep + os.environ["PATH"], APPROVAL_FIXTURE=str(fixture_path), SB_GITHUB_RETRY_DELAY="0", SB_GITHUB_HALT_FILE=str(work / "halt.json"))
            def deps():
                fixture_path.write_text(json.dumps(fixture))
                run = subprocess.run([str(ROOT / "scripts/super-board-deps.sh"), "--repo", "x/y"], env=env,
                                     capture_output=True, text=True, check=True)
                return json.loads(run.stdout)["3"]
            self.assertTrue(deps()["needsYouDone"], "current approval resumes even after an outsider fake block")
            fixture["head"] = "b" * 40
            self.assertFalse(deps()["needsYouDone"], "new code must never inherit old done or label")
            self.assertTrue(deps().get("approvalRefresh"), "stale request must be sent for fresh review/request")
            fixture["head"] = HEAD
            fixture["records"].append(comment("Reason tag: 🙋 a new maintainer request", 4))
            self.assertFalse(deps()["needsYouDone"], "a newer maintainer request must block resume")


class TrustedApproverTests(unittest.TestCase):
    """Layer B: merge_policy.trusted_approvers narrows who may say `done`."""
    PERMS = {"owner": "admin", "alice": "write", "robot": "admin", "visitor": "read"}

    def check(self, records, trusted, actor):
        return approval.validate(records, REQUEST, lambda login: self.PERMS.get(login), trusted, actor)

    def records(self, approver):
        return [comment("Reason tag: 🙋 needs you\n" + approval.marker(REQUEST), 1, login="owner"),
                comment("done", 2, login=approver)]

    def test_absent_or_empty_list_keeps_old_behaviour(self):
        for trusted in (None, [], [" "]):
            self.assertTrue(self.check(self.records("alice"), trusted, None)["approved"])

    def test_listed_approver_counts_case_insensitively(self):
        self.assertTrue(self.check(self.records("alice"), ["Alice"], "robot")["approved"])

    def test_unlisted_collaborator_does_not_count(self):
        self.assertFalse(self.check(self.records("alice"), ["owner"], "robot")["approved"])

    def test_acting_login_cannot_approve_itself(self):
        self.assertFalse(self.check(self.records("robot"), ["robot", "alice"], "Robot")["approved"])

    def test_unknown_actor_holds_when_list_is_set(self):
        result = self.check(self.records("alice"), ["alice"], None)
        self.assertFalse(result["approved"])
        self.assertIn("acting login", result["why"])

    def test_pinned_identity_without_trusted_list_fails_closed(self):
        result = approval.validate(self.records("owner"), REQUEST, lambda login: self.PERMS.get(login),
                                   None, "robot", True)
        self.assertFalse(result["approved"])
        self.assertIn("trusted_approvers", result["why"])
        ok = approval.validate(self.records("alice"), REQUEST, lambda login: self.PERMS.get(login),
                               ["alice"], "robot", True)
        self.assertTrue(ok["approved"])

    def test_load_policy_reads_list_and_identity(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "c.json")
            for cfg, want in (({}, (None, False, None)),
                              ({"notifications": {"bot_identity": " AUTO "}}, (None, False, None)),
                              ({"notifications": {"bot_identity": "x[bot]"}}, (None, True, "x[bot]")),
                              ({"notifications": {"bot_identity": " robot "},
                                "merge_policy": {"trusted_approvers": ["a"]}}, (["a"], True, "robot"))):
                path.write_text(json.dumps(cfg), encoding="utf-8")
                self.assertEqual(approval.load_policy(str(path)), want)

    def test_bot_app_login_is_pinned_for_fail_closed(self):
        held = approval.validate(self.records("alice"), REQUEST, lambda login: self.PERMS.get(login),
                                 [], "app", True, "x[bot]")
        self.assertFalse(held["approved"])
        ok = approval.validate(self.records("alice"), REQUEST, lambda login: self.PERMS.get(login),
                               ["alice"], "app", True, "x[bot]")
        self.assertTrue(ok["approved"])

    def test_bot_identity_that_is_trusted_holds_with_reason(self):
        out = approval.validate(self.records("robot"), REQUEST, lambda login: self.PERMS.get(login),
                                ["alice", "Robot"], "robot", True, " robot ")
        self.assertFalse(out["approved"])
        self.assertIn("trusted_approvers", out["why"])

    def test_listed_but_unprivileged_still_fails(self):
        self.assertFalse(self.check(self.records("visitor"), ["visitor"], "robot")["approved"])


class DepsConfigTests(unittest.TestCase):
    """Layer B: the planner's --deps path enforces trusted_approvers via --config."""
    def run_deps(self, config, approver):
        import contextlib
        import io
        import sys

        class Fake(approval.GitHub):
            def pr(self, number):
                return HEAD, {3}

            def comments(self, number):
                if number != 3:
                    return []
                return [comment("Reason tag: 🙋 needs you\n" + approval.marker(REQUEST), 1, login="owner"),
                        comment("done", 2, login=approver)]

            def permission(self, login):
                return {"owner": "admin", "robot": "admin"}.get(login)

            def acting_login(self):
                return "robot"

        real, argv, stdin = approval.GitHub, sys.argv, sys.stdin
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            args = ["approval.py", "--deps", "--repo", "x/y"]
            if config is not None:
                path = Path(tmp, "c.json")
                path.write_text(json.dumps(config), encoding="utf-8")
                args += ["--config", str(path)]
            approval.GitHub, sys.argv = Fake, args
            sys.stdin = io.StringIO(json.dumps({"3": {"number": 3, "needsYou": True}}))
            try:
                with contextlib.redirect_stdout(out):
                    approval.main()
            finally:
                approval.GitHub, sys.argv, sys.stdin = real, argv, stdin
        return json.loads(out.getvalue())["3"]

    TRUSTED = {"notifications": {"bot_identity": "robot"}, "merge_policy": {"trusted_approvers": ["owner"]}}

    def test_bot_done_never_yields_needs_you_done(self):
        self.assertFalse(self.run_deps(self.TRUSTED, "robot")["needsYouDone"])

    def test_trusted_human_done_still_resumes(self):
        self.assertTrue(self.run_deps(self.TRUSTED, "owner")["needsYouDone"])

    def test_pinned_identity_without_list_fails_closed(self):
        cfg = {"notifications": {"bot_identity": "robot"}}
        self.assertFalse(self.run_deps(cfg, "owner")["needsYouDone"])

    def test_without_config_keeps_old_behaviour(self):
        self.assertTrue(self.run_deps(None, "robot")["needsYouDone"])


if __name__ == "__main__":
    unittest.main()
