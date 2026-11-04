#!/usr/bin/env python3
"""Offline tests for plugin/scripts/super-board-events.py (issue #66).

Every GitHub call goes through a stub at the script's single read boundary
(`run_read`, the function that shells out to super-board-github-read.py). The
stub serves fixture JSON, refuses every write verb exactly like a stub `gh`
that exits non-zero, and records each call so tests can assert the call budget.
The canonical PATH stub `gh` for a full subprocess run lives in
tests/fixtures/events/gh-stub.py and is exercised by the PosixSubprocess class
(skipped where a shebang script cannot be exec'd, e.g. native Windows).

No network, no tokens, no model calls, no gh writes.

Run directly:
  python tests/test_events.py
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "super-board-events.py"
FIXTURES = ROOT / "tests" / "fixtures" / "events"
READ_HELPER = ROOT / "scripts" / "super-board-github-read.py"

SLUG = "evtest"
BOT = "example-bot"
CMD = "/super-board go"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_read_helper():
    return load_module(READ_HELPER, "super_board_github_read_under_test")


MOD = load_module(SCRIPT, "super_board_events_under_test")


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago_iso(minutes):
    return iso(datetime.now(timezone.utc) - timedelta(minutes=minutes))


# ───────────────────────────── the stub read boundary ─────────────────────────────
#
# Mirrors the fixture-serving stub `gh`: routes each gh argv to a fixture file,
# refuses every write verb (anything the real read helper would refuse) and
# refuses any read it has no fixture for, so a regression that invents a call
# fails loudly instead of passing.


class StubRefused(MOD.ReadHalted):
    """The stub `gh` exited non-zero (a write verb, or no fixture for the call)."""


class ReadHalted(MOD.ReadHalted):
    """The read helper halted (its exit 79), like a GitHub outage."""


class FakeGH:
    def __init__(self, fixtures: pathlib.Path, log: list):
        self.fixtures = fixtures
        self.log = log
        self.helper = load_read_helper()

    def serve(self, name):
        path = self.fixtures / name
        if not path.exists():
            raise StubRefused("stub gh: no fixture for this call: %s" % name)
        return json.loads(path.read_text(encoding="utf-8"))

    def __call__(self, kind, args, repo=None):
        self.log.append((kind, list(args)))
        if not self.helper.read_only(args):
            raise StubRefused("stub gh: write verb refused: %s" % " ".join(args)[:120])
        joined = " ".join(args)
        if kind == "permission":
            m = re.search(r"/collaborators/([^/]+)/permission", joined)
            if not m:
                raise StubRefused("stub gh: unrecognized permission call")
            try:
                return self.serve("permissions/%s.json" % m.group(1))
            except FileNotFoundError:
                raise StubRefused("stub gh: no permission fixture for %s" % m.group(1)) from None
        if "graphql" in args:
            query = next((a[len("query="):] for a in args if a.startswith("query=")), "")
            if re.search(r"\bmutation\b", query, re.I):
                raise StubRefused("stub gh: mutation refused")
            if "projectV2" in query:
                return self.serve("board.json")
            if "pullRequest(" in query:
                return self.serve("correlation.json")
            raise StubRefused("stub gh: unrecognized graphql call")
        if "/issues/comments?" in joined:
            return self.serve("comments.json")
        if "/pulls/comments?" in joined:
            return self.serve("pulls-comments.json")
        raise StubRefused("stub gh: unrecognized read: %s" % joined[:120])


class HaltingGH(FakeGH):
    """A stub whose call number `fail_on_call` (0-based) halts like helper exit 79."""

    def __init__(self, fixtures, log, fail_on_call):
        super().__init__(fixtures, log)
        self.fail_on_call = fail_on_call

    def __call__(self, kind, args, repo=None):
        if len(self.log) == self.fail_on_call:
            self.log.append((kind, list(args)))
            raise ReadHalted("GitHub read failed; the run halts")
        return super().__call__(kind, args, repo)


# ───────────────────────────── the shared fixture board ─────────────────────────────
#
# One project (#4) on github.com/owner/repo with (timelineItems empty unless a
# test adds a move):
#   issue 12 "Add thing"  labels [feature]  column Ready
#   issue 13 "QA me"      labels [qa]       column Ready
#   issue 14 "Review me"  labels []         column Review
#   issue 15 "Busy"       labels []         column Building  (inflight/issue-15)
#   issue 16 "Shipped"    labels []         column Done
#   issue 17 "QA col"     labels []         column QA
#   PR 30   linked → 12   column Ready      (a PR board item)


def board_payload(now_iso):
    def item(number, column, content):
        return {"updatedAt": now_iso,
                "fieldValues": {"nodes": [{"name": column, "field": {"name": "Status"}}]},
                "content": content}

    return {"data": {"viewer": {"login": BOT}, "repositoryOwner": {"projectV2": {"items": {
        "pageInfo": {"hasNextPage": False},
        "nodes": [
            item(12, "Ready", {"__typename": "Issue", "number": 12, "title": "Add thing",
                               "url": "https://github.com/owner/repo/issues/12",
                               "labels": {"nodes": [{"name": "feature"}]},
                               "timelineItems": {"nodes": []}}),
            item(13, "Ready", {"__typename": "Issue", "number": 13, "title": "QA me",
                               "url": "https://github.com/owner/repo/issues/13",
                               "labels": {"nodes": [{"name": "qa"}]},
                               "timelineItems": {"nodes": []}}),
            item(14, "Review", {"__typename": "Issue", "number": 14, "title": "Review me",
                                "url": "https://github.com/owner/repo/issues/14",
                                "labels": {"nodes": []},
                                "timelineItems": {"nodes": []}}),
            item(15, "Building", {"__typename": "Issue", "number": 15, "title": "Busy",
                                  "url": "https://github.com/owner/repo/issues/15",
                                  "labels": {"nodes": []},
                                  "timelineItems": {"nodes": []}}),
            item(16, "Done", {"__typename": "Issue", "number": 16, "title": "Shipped",
                              "url": "https://github.com/owner/repo/issues/16",
                              "labels": {"nodes": []},
                              "timelineItems": {"nodes": []}}),
            item(17, "QA", {"__typename": "Issue", "number": 17, "title": "QA col",
                            "url": "https://github.com/owner/repo/issues/17",
                            "labels": {"nodes": []},
                            "timelineItems": {"nodes": []}}),
            item(30, "Ready", {"__typename": "PullRequest", "number": 30, "title": "Fix thing",
                               "url": "https://github.com/owner/repo/pull/30",
                               "closingIssuesReferences": {"nodes": [{"number": 12}]}}),
        ]}}}}}


def move_event(node_id, actor, status="Ready", previous="Building", stamp=None, issue=12, project=4):
    return {"__typename": "ProjectV2ItemStatusChangedEvent", "id": node_id,
            "createdAt": stamp or ago_iso(10), "actor": {"login": actor},
            "previousStatus": previous, "status": status, "project": {"number": project}}


def comment(node_id, body, issue_number, login="alice", minutes_ago=5, stamp=None):
    return {"id": sum(map(ord, node_id)), "node_id": node_id, "body": body,
            "user": {"login": login}, "created_at": stamp or ago_iso(minutes_ago),
            "updated_at": stamp or ago_iso(minutes_ago),
            "issue_url": "https://github.com/owner/repo/issues/%d" % issue_number,
            "html_url": "https://github.com/owner/repo/issues/%d#issuecomment-%s" % (issue_number, node_id)}


def review_comment(node_id, body, pr_number, login="alice", minutes_ago=5, stamp=None):
    return {"id": sum(map(ord, node_id)), "node_id": node_id, "body": body,
            "user": {"login": login}, "created_at": stamp or ago_iso(minutes_ago),
            "updated_at": stamp or ago_iso(minutes_ago),
            "pull_request_url": "https://github.com/owner/repo/pulls/%d" % pr_number,
            "html_url": "https://github.com/owner/repo/pull/%d#discussion_r%s" % (pr_number, node_id)}


def permission(login, level, role=None):
    payload = {"permission": level, "user": {"login": login}}
    if role:
        payload["role_name"] = role
    return payload


# ───────────────────────────── test base ─────────────────────────────


class EvBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / "repo"
        for d in (self.root / ".claude" / "super-board" / "configs",
                  self.root / ".claude" / "super-board" / "inflight"):
            d.mkdir(parents=True)
        self.cfg_path = self.root / ".claude" / "super-board" / "configs" / (SLUG + ".json")
        self.fixtures = self.base / "fixtures"
        self.fixtures.mkdir()
        self.calls = []
        self._orig_run_read = MOD.run_read
        self.fake = FakeGH(self.fixtures, self.calls)
        MOD.run_read = self.fake
        self.fixture("comments.json", [[]])
        self.fixture("pulls-comments.json", [])
        self.write_config()

    def tearDown(self):
        MOD.run_read = self._orig_run_read
        self.tmp.cleanup()

    # -- fixture plumbing

    def write_config(self, bot=BOT, initiators=("alice",), enabled=True, command=CMD, remote=None,
                     sources=None):
        all_sources = {"ready_move": True, "issue_comment": True,
                       "pr_comment": True, "review_comment": True}
        if sources:
            all_sources.update(sources)
        cfg = {
            "description": "event test",
            "project": {"owner": "owner", "title": "Board", "number": 4},
            "repo": {"path": ".", "remote": remote if remote is not None else "https://github.com/owner/repo"},
            "base_branch": "staging",
            "worker_backend": "workflow",
            "notifications": {"channel": "session", "chat_id": None, "bot_identity": bot},
            "events": {"enabled": enabled, "initiators": list(initiators), "poll_seconds": 120,
                       "max_starts_per_hour": 3, "max_starts_per_day": 10, "queue_ttl_hours": 24,
                       "command": command, "sources": all_sources},
        }
        self.cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        return cfg

    def fixture(self, name, payload):
        path = self.fixtures / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def board(self, extra_items=()):
        payload = board_payload(iso(datetime.now(timezone.utc)))
        payload["data"]["repositoryOwner"]["projectV2"]["items"]["nodes"].extend(extra_items)
        self.fixture("board.json", payload)
        return payload

    def add_move(self, event, index=0):
        payload = json.loads((self.fixtures / "board.json").read_text(encoding="utf-8"))
        items = payload["data"]["repositoryOwner"]["projectV2"]["items"]["nodes"]
        items[index]["content"].setdefault("timelineItems", {"nodes": []})["nodes"].append(event)
        self.fixture("board.json", payload)

    def comments(self, *comments):
        self.fixture("comments.json", [list(comments)])

    def pulls_comments(self, *comments):
        self.fixture("pulls-comments.json", list(comments))

    def corr(self, **prs):
        # corr(p31=[12]) — None marks "not a pull request"
        nodes = {}
        for alias, numbers in prs.items():
            nodes[alias] = None if numbers is None else {
                "number": int(alias[1:]), "closingIssuesReferences": {"nodes": [{"number": n} for n in numbers]}}
        self.fixture("correlation.json", {"data": {"repository": nodes}})

    def perm(self, login, level, role=None):
        self.fixture("permissions/%s.json" % login, permission(login, level, role))

    # -- state plumbing

    def ev_dir(self, *parts):
        d = self.root / ".claude" / "super-board" / "events" / SLUG
        for p in parts:
            d = d / p
        return d

    def read_event(self, folder, event_id):
        return json.loads(self.ev_dir(folder, event_id + ".json").read_text(encoding="utf-8"))

    def ids_in(self, folder):
        d = self.ev_dir(folder)
        return sorted(p.stem for p in d.glob("*.json")) if d.exists() else []

    def one_event(self, folder, label):
        ids = self.ids_in(folder)
        self.assertEqual(len(ids), 1, "%s: expected exactly one event in %s, got %r" % (label, folder, ids))
        return self.read_event(folder, ids[0])

    def take_lock(self):
        (self.root / ".claude" / "super-board" / "inflight" / "workflow-wave.lock").write_text(
            "SLUG=%s\nSTARTED=%s\n" % (SLUG, iso(datetime.now(timezone.utc))), encoding="utf-8")

    def run_cli(self, *argv, cfg=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = MOD.main(["--config", str(cfg or self.cfg_path), *argv])
        return code, out.getvalue(), err.getvalue()

    # -- assertions

    def assertStopped(self, got, want_code, want_msg=None, label=""):
        code, out, err = got
        self.assertEqual(code, want_code, "%s: exit %s out=%r err=%r" % (label, code, out, err))
        if want_msg is not None:
            self.assertIn(want_msg, err, "%s: stderr %r lacks %r" % (label, err, want_msg))


class DisabledTests(EvBase):
    def test_next_with_bot_identity_auto_fails_closed(self):
        self.write_config(bot="auto")
        self.board()
        self.assertStopped(self.run_cli("next"), 3, "events disabled: bot_identity", "next bot=auto")

    def test_next_with_bot_identity_in_any_case_fails_closed(self):
        self.write_config(bot="  AUTO ")
        self.board()
        self.assertStopped(self.run_cli("next"), 3, "events disabled: bot_identity", "next bot=AUTO")

    def test_next_with_config_disabled(self):
        self.write_config(enabled=False)
        self.board()
        self.assertStopped(self.run_cli("next"), 3, "events disabled: config", "next disabled")

    def test_poll_disabled_makes_no_calls(self):
        self.write_config(enabled=False)
        self.board()
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, [], "a disabled poll must not read GitHub")


class TrustMatrix(EvBase):
    """The actor is in initiators, holds admin/maintain/write live, and is not the bot."""

    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def test_trusted_ready_move_enqueues_and_claim_records_the_start(self):
        self.add_move(move_event("PVSCE_1", "alice"))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        queued = self.one_event("queue", "ready move")
        self.assertEqual(queued["source"], "ready_move")
        self.assertEqual(queued["actor"], "alice")
        self.take_lock()
        code, out, err = self.run_cli("claim", queued["id"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [], "claim must move the event out of queue/")
        self.assertEqual(self.ids_in("claimed"), [queued["id"]])
        ledger = self.ev_dir("starts.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(ledger), 1, "a start is recorded at claim time")
        self.assertEqual(json.loads(ledger[0])["id"], queued["id"])

    def test_actor_outside_initiators_is_dropped(self):
        self.write_config(initiators=("carol",))
        self.add_move(move_event("PVSCE_1", "alice"))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [])
        dropped = self.one_event("dropped", "outside initiators")
        self.assertIn("initiator", dropped["reason"])

    def test_the_bot_account_never_triggers(self):
        self.write_config(initiators=(BOT, "alice"))
        self.add_move(move_event("PVSCE_BOT", BOT))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [], "bot_identity must never trigger a lane")
        dropped = self.one_event("dropped", "bot actor")
        self.assertIn("bot", dropped["reason"])

    def test_a_bot_suffixed_login_never_triggers(self):
        self.write_config(initiators=("depot-github-app[bot]", "alice"))
        self.add_move(move_event("PVSCE_APP", "Depot-Github-App[BOT]"))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [], "a [bot] login must never trigger a lane")

    def test_claim_refuses_read_permission(self):
        self.write_config(initiators=("mallory",))
        self.perm("mallory", "read")
        self.comments(comment("IC_mallory", CMD, 12, login="mallory"))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        event_id = self.one_event("queue", "mallory comment")["id"]
        self.take_lock()
        code, out, err = self.run_cli("claim", event_id)
        self.assertStopped((code, out, err), 5, "permission", "claim with read permission")
        self.assertEqual(self.ids_in("claimed"), [], "an untrusted claim must not start")
        dropped = self.read_event("dropped", event_id)
        self.assertIn("permission", dropped["reason"])

    def test_claim_allows_maintain_via_permission_field(self):
        self.perm("alice", "maintain")
        self.add_move(move_event("PVSCE_1", "alice"))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        event_id = self.one_event("queue", "maintain move")["id"]
        self.take_lock()
        code, out, err = self.run_cli("claim", event_id)
        self.assertEqual(code, 0, err)
        claimed = self.read_event("claimed", event_id)
        self.assertEqual(claimed["actor_permission"], "maintain")

    def test_claim_allows_maintain_via_role_name(self):
        self.perm("alice", "read", role="maintain")
        self.add_move(move_event("PVSCE_1", "alice"))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        event_id = self.one_event("queue", "role maintain move")["id"]
        self.take_lock()
        code, out, err = self.run_cli("claim", event_id)
        self.assertEqual(code, 0, "role_name maintain must count as a live trusted permission: %s" % err)

    def test_poll_drops_an_untrusted_actor_at_enqueue_time(self):
        self.write_config(initiators=("alice",))
        self.add_move(move_event("PVSCE_untrusted", "stranger"))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [],
                         "an untrusted actor's event is dropped at enqueue, never queued")
        dropped = self.one_event("dropped", "untrusted actor")
        self.assertIn("untrusted-actor", dropped["reason"])
        self.assertTrue(all(kind != "permission" for kind, _ in self.calls),
                        "the enqueue-time trust check is offline and must not read a permission")

    def test_claim_with_a_failing_permission_read_halts(self):
        self.add_move(move_event("PVSCE_1", "alice"))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        event_id = self.one_event("queue", "halt move")["id"]
        self.take_lock()

        def fail_permission(kind, args, repo=None):
            if kind == "permission":
                raise ReadHalted("permission read halted")
            return self.fake(kind, args, repo)

        MOD.run_read = fail_permission
        code, out, err = self.run_cli("claim", event_id)
        self.assertStopped((code, out, err), 79, None, "claim permission read halted")
        self.assertEqual(self.ids_in("claimed"), [], "a halted claim leaves the event queued")
        self.assertEqual(self.ids_in("queue"), [event_id])


class SourceToggles(EvBase):
    """events.sources gates each source; a candidate from a disabled source never queues."""

    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def test_ready_move_false_queues_nothing_and_comment_sources_still_work(self):
        self.write_config(sources={"ready_move": False})
        self.add_move(move_event("PVSCE_off", "alice"))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [], "ready_move: false must not queue a Ready move")
        dropped = self.one_event("dropped", "disabled ready_move")
        self.assertIn("source-disabled", dropped["reason"])
        # decided once: the second poll neither re-drops nor resurrects it
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("dropped"), [dropped["id"]])
        self.assertEqual(self.ids_in("queue"), [])
        # the other sources still work
        self.comments(comment("IC_src", CMD, 12))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        queued = self.one_event("queue", "comment still queues")
        self.assertEqual(queued["source"], "issue_comment")

    def test_a_disabled_comment_source_is_dropped_as_source_disabled(self):
        self.write_config(sources={"issue_comment": False})
        self.comments(comment("IC_off", CMD, 12))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [], "issue_comment: false must not queue a comment")
        dropped = self.one_event("dropped", "disabled issue_comment")
        self.assertIn("source-disabled", dropped["reason"])


class CommandGrammar(EvBase):
    """Only an exact first line evaluates; nothing else in the body matters."""

    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def poll_one(self, body, node="IC_x", login="alice", minutes=5):
        before = set(self.ids_in("queue")) | set(self.ids_in("dropped"))
        self.comments(comment(node, body, 12, login=login, minutes_ago=minutes))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        after = set(self.ids_in("queue")) | set(self.ids_in("dropped"))
        decided = after - before
        self.assertEqual(len(decided), 1, "exactly one decided event per poll: %r" % (decided,))
        event_id_value = decided.pop()
        for folder in ("queue", "dropped"):
            if (self.ev_dir(folder, event_id_value + ".json")).exists():
                return self.read_event(folder, event_id_value)
        self.fail("the decided event vanished")

    def test_exact_first_line_enqueues(self):
        evt = self.poll_one(CMD + "\nplease build card 12")
        self.assertEqual(evt["state"], "queued")

    def test_match_in_mid_body_is_rejected(self):
        evt = self.poll_one("hello\n" + CMD)
        self.assertIn("command", evt["reason"])

    def test_wrong_case_is_rejected(self):
        evt = self.poll_one("/Super-Board Go")
        self.assertIn("command", evt["reason"])

    def test_a_quote_is_rejected(self):
        evt = self.poll_one("> " + CMD)
        self.assertIn("command", evt["reason"])

    def test_trailing_whitespace_is_rejected(self):
        evt = self.poll_one(CMD + " ")
        self.assertIn("command", evt["reason"])

    def test_crlf_line_ending_is_accepted(self):
        evt = self.poll_one(CMD + "\r\nrest")
        self.assertEqual(evt["state"], "queued")

    def test_empty_body_is_rejected(self):
        evt = self.poll_one("")
        self.assertIn("command", evt["reason"])

    def test_configured_command_overrides_the_default(self):
        self.write_config(command="/go now")
        evt = self.poll_one("/go now\n", node="IC_y")
        self.assertEqual(evt["state"], "queued")
        evt = self.poll_one(CMD, node="IC_z")
        self.assertIn("command", evt["reason"])

    def test_comment_bodies_are_never_stored(self):
        evt = self.poll_one(CMD + "\nSECRETPHRASE", node="IC_s")
        raw = self.ev_dir("queue", evt["id"] + ".json").read_text(encoding="utf-8")
        self.assertNotIn("SECRETPHRASE", raw, "comment bodies must never reach the event file")
        self.assertNotIn("body", json.loads(raw), "the event file has no body field")


class Correlation(EvBase):
    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def poll_one(self, one_comment):
        self.comments(one_comment)
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        queued, dropped = self.ids_in("queue"), self.ids_in("dropped")
        self.assertEqual(len(queued) + len(dropped), 1, err)
        return self.read_event("queue" if queued else "dropped", (queued + dropped)[0])

    def test_issue_comment_correlates_to_its_issue(self):
        evt = self.poll_one(comment("IC_1", CMD, 12))
        self.assertEqual(evt["issue"], 12)
        self.assertIsNone(evt["pr"])

    def test_pr_comment_with_one_linked_issue_correlates(self):
        evt = self.poll_one(comment("IC_2", CMD, 30))
        self.assertEqual((evt["issue"], evt["pr"]), (12, 30))

    def test_pr_comment_off_board_resolves_through_the_correlation_call(self):
        self.corr(p31=[12])
        evt = self.poll_one(comment("IC_3", CMD, 31))
        self.assertEqual((evt["issue"], evt["pr"]), (12, 31), "PR 31 is not a board item; the batched read links it to 12")
        self.assertEqual(len(self.calls), 4, "board + comments + pulls + correlation = at most 4 calls")

    def test_pr_with_zero_linked_issues_is_dropped(self):
        self.corr(p31=[])
        evt = self.poll_one(comment("IC_4", CMD, 31))
        self.assertIn("correlation", evt["reason"])

    def test_pr_with_two_linked_issues_on_the_project_is_dropped(self):
        extra = {"updatedAt": ago_iso(60), "fieldValues": {"nodes": [{"name": "Ready", "field": {"name": "Status"}}]},
                 "content": {"__typename": "Issue", "number": 18, "title": "Second",
                             "url": "https://github.com/owner/repo/issues/18",
                             "labels": {"nodes": []}, "timelineItems": {"nodes": []}}}
        self.board(extra_items=[extra])
        self.corr(p31=[12, 18])
        evt = self.poll_one(comment("IC_5", CMD, 31))
        self.assertIn("correlation", evt["reason"])

    def test_pr_whose_only_link_is_not_on_the_project_is_dropped(self):
        self.corr(p31=[99])
        evt = self.poll_one(comment("IC_6", CMD, 31))
        self.assertIn("correlation", evt["reason"])

    def test_review_comment_correlates_through_its_pull_request(self):
        self.pulls_comments(review_comment("PRR_1", CMD, 30))
        self.fixture("comments.json", [[]])
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        evt = self.one_event("queue", "review comment")
        self.assertEqual(evt["source"], "review_comment")
        self.assertEqual((evt["issue"], evt["pr"]), (12, 30))


class Routing(EvBase):
    """The lane comes only from the card's status."""

    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def poll_move(self, event, index=0):
        self.add_move(event, index=index)
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        return self.one_event("queue", "move")

    def test_ready_moves_to_build(self):
        evt = self.poll_move(move_event("PVSCE_1", "alice"))
        self.assertEqual((evt["issue"], evt["lane"]), (12, "build"))

    def test_ready_with_qa_label_moves_to_qa(self):
        evt = self.poll_move(move_event("PVSCE_13", "alice", issue=13), index=1)
        self.assertEqual((evt["issue"], evt["lane"]), (13, "qa"))

    def test_qa_column_routes_to_qa(self):
        evt = self.poll_move(move_event("PVSCE_17", "alice", status="QA", previous="Ready", issue=17), index=5)
        self.assertEqual((evt["issue"], evt["lane"]), (17, "qa"))

    def test_review_column_routes_to_review(self):
        evt = self.poll_move(move_event("PVSCE_14", "alice", status="Review", previous="QA", issue=14), index=2)
        self.assertEqual((evt["issue"], evt["lane"]), (14, "review"))

    def test_a_done_move_is_dropped(self):
        self.add_move(move_event("PVSCE_16", "alice", status="Done", previous="Review", issue=16), index=4)
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [])
        dropped = self.one_event("dropped", "done move")
        self.assertIn("column", dropped["reason"])

    def test_a_move_on_another_project_is_dropped(self):
        self.add_move(move_event("PVSCE_OTHER", "alice", project=9))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [], "another project's move must not enqueue")

    def test_comment_routing_follows_the_card_column(self):
        for number, want in ((12, "build"), (17, "qa"), (14, "review")):
            self.comments(comment("IC_r%s" % number, CMD, number))
            code, out, err = self.run_cli("poll")
            self.assertEqual(code, 0, err)
            evt = self.one_event("queue", "#%s queues" % number)
            self.assertEqual(evt["lane"], want, "#%s in its column must route to %s" % (number, want))
            code, out, err = self.run_cli("fail", evt["id"], "--reason", "test cleanup")
            self.assertEqual(code, 0, err)

    def test_a_comment_on_a_building_card_is_dropped(self):
        self.comments(comment("IC_15", CMD, 15))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [])
        dropped = self.one_event("dropped", "building comment")
        self.assertIn("column", dropped["reason"])


class DedupAndCoalescing(EvBase):
    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def poll(self):
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        return err

    def test_the_same_event_is_not_enqueued_twice(self):
        self.comments(comment("IC_dup", CMD, 12))
        self.poll()
        self.assertEqual(len(self.ids_in("queue")), 1)
        self.poll()
        self.assertEqual(len(self.ids_in("queue")), 1, "seen.jsonl must deduplicate <source>:<node_id>")

    def test_a_newer_event_supersedes_the_older_one(self):
        # comment at -30m, then a Ready move at -10m: the move wins.
        self.comments(comment("IC_old", CMD, 12, minutes_ago=30))
        self.poll()
        self.assertEqual(len(self.ids_in("queue")), 1)
        self.fixture("comments.json", [[]])
        self.add_move(move_event("PVSCE_NEW", "alice", stamp=ago_iso(10)))
        self.poll()
        self.assertEqual(len(self.ids_in("queue")), 1, "one queued event per card")
        queued = self.one_event("queue", "newest wins")
        self.assertEqual(queued["source"], "ready_move", "the newest event supersedes")
        dropped = self.one_event("dropped", "superseded comment")
        self.assertIn("superseded", dropped["reason"])

    def test_an_older_late_arrival_does_not_displace_the_queued_event(self):
        # the move lands first; a stale comment for the same card must not take its place
        self.add_move(move_event("PVSCE_FIRST", "alice", stamp=ago_iso(10)))
        self.poll()
        queued = self.one_event("queue", "move first")
        self.comments(comment("IC_stale", CMD, 12, minutes_ago=11))
        self.poll()
        still = self.read_event("queue", queued["id"])
        self.assertEqual(still["id"], queued["id"], "the newer queued event stays")
        self.assertEqual(len(self.ids_in("queue")), 1)

    def test_events_for_different_cards_both_queue(self):
        self.comments(comment("IC_c12", CMD, 12, minutes_ago=9),
                      comment("IC_c14", CMD, 14, minutes_ago=8))
        self.poll()
        self.assertEqual(len(self.ids_in("queue")), 2)


class Watermark(EvBase):
    def test_the_since_param_overlaps_the_last_tick_by_120s(self):
        self.board()
        seed = ago_iso(5)
        self.comments(comment("IC_seed", CMD, 14, stamp=seed))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        cursor = json.loads(self.ev_dir("cursor.json").read_text(encoding="utf-8"))
        self.assertEqual(cursor["watermark"], seed)
        self.assertIsNone(cursor["last_error"])
        # a second poll reads with since = watermark - 120s
        self.fixture("comments.json", [[]])
        self.add_move(move_event("PVSCE_WM", "alice", stamp=iso(
            datetime.now(timezone.utc) - timedelta(seconds=30))))
        self.calls.clear()
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        watermark = datetime.strptime(seed, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        want_since = iso(watermark - timedelta(seconds=120))
        comment_calls = [args for kind, args in self.calls if "/issues/comments?" in " ".join(args)]
        self.assertEqual(len(comment_calls), 1)
        self.assertIn("since=" + want_since, comment_calls[0][1], "the read must overlap the last tick by 120s")

    def test_the_watermark_advances_to_the_newest_observation(self):
        self.board()
        stamp = ago_iso(1)
        self.comments(comment("IC_wm", CMD, 12, stamp=stamp))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        cursor = json.loads(self.ev_dir("cursor.json").read_text(encoding="utf-8"))
        self.assertEqual(cursor["watermark"], stamp, "watermark = the newest event seen")


class Budget(EvBase):
    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def enqueue(self):
        self.comments(comment("IC_b", CMD, 12))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        return self.ids_in("queue")[0]

    def ledger(self, *starts):
        self.ev_dir().mkdir(parents=True, exist_ok=True)
        self.ev_dir("starts.jsonl").write_text(
            "".join(json.dumps({"id": "x%d" % i, "ts": ts}) + "\n" for i, ts in enumerate(starts)),
            encoding="utf-8")

    def test_the_fourth_start_in_an_hour_is_refused_and_stays_queued(self):
        event_id = self.enqueue()
        self.ledger(ago_iso(10), ago_iso(20), ago_iso(30))
        code, out, err = self.run_cli("next")
        self.assertStopped((code, out, err), 3, "budget exceeded", "4th start in an hour")
        self.assertEqual(self.ids_in("queue"), [event_id], "over-budget events stay queued")
        evt = self.read_event("queue", event_id)
        self.assertTrue(evt["budget_reported"], "the overflow is reported")

    def test_the_overflow_is_reported_once(self):
        event_id = self.enqueue()
        self.ledger(ago_iso(10), ago_iso(20), ago_iso(30))
        code1, out1, err1 = self.run_cli("next")
        first_raw = self.ev_dir("queue", event_id + ".json").read_text(encoding="utf-8")
        code2, out2, err2 = self.run_cli("next")
        second_raw = self.ev_dir("queue", event_id + ".json").read_text(encoding="utf-8")
        self.assertEqual(first_raw, second_raw, "the second report must not rewrite the event file again")
        for err in (err1, err2):
            self.assertIn("budget exceeded", err)

    def test_the_eleventh_start_in_a_day_is_refused(self):
        event_id = self.enqueue()
        self.ledger(ago_iso(30), ago_iso(35), *[ago_iso(120)] * 8)
        code, out, err = self.run_cli("next")
        self.assertStopped((code, out, err), 3, "budget exceeded", "11th start in a day")

    def test_a_start_older_than_a_day_does_not_count(self):
        event_id = self.enqueue()
        self.ledger(ago_iso(30), ago_iso(35), *[ago_iso(25 * 60)] * 8)
        code, out, err = self.run_cli("next")
        self.assertEqual(code, 0, "only the starts inside the 24h window count: %s" % err)

    def test_a_corrupt_ledger_blocks_dispatch(self):
        event_id = self.enqueue()
        self.ev_dir().mkdir(parents=True, exist_ok=True)
        self.ev_dir("starts.jsonl").write_text("{not json}\n", encoding="utf-8")
        code, out, err = self.run_cli("next")
        self.assertStopped((code, out, err), 3, "events budget ledger unreadable", "corrupt ledger")

    def test_a_missing_ledger_blocks_dispatch(self):
        event_id = self.enqueue()
        self.ev_dir("starts.jsonl").unlink()
        code, out, err = self.run_cli("next")
        self.assertStopped((code, out, err), 3, "events budget ledger missing", "missing ledger")

    def test_claim_refuses_when_over_budget(self):
        event_id = self.enqueue()
        self.ledger(ago_iso(10), ago_iso(20), ago_iso(30))
        self.take_lock()
        code, out, err = self.run_cli("claim", event_id)
        self.assertStopped((code, out, err), 5, "budget", "claim over budget")
        self.assertEqual(self.ids_in("queue"), [event_id], "the event stays queued")

    def test_poll_creates_an_empty_ledger_so_next_can_dispatch(self):
        event_id = self.enqueue()
        code, out, err = self.run_cli("next")
        self.assertEqual(code, 0, "poll must create starts.jsonl; a fresh board dispatches: %s" % err)
        self.assertEqual(json.loads(out)["id"], event_id)


class Locks(EvBase):
    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def test_poll_drops_new_events_while_the_wave_lock_is_held(self):
        self.take_lock()
        self.comments(comment("IC_lock", CMD, 12))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [], "the bounce guard drops while a wave runs")
        dropped = self.one_event("dropped", "wave lock")
        self.assertIn("wave lock", dropped["reason"])

    def test_poll_drops_events_for_a_card_with_an_inflight_lock(self):
        (self.root / ".claude" / "super-board" / "inflight" / "issue-12").write_text("1234\n", encoding="utf-8")
        self.comments(comment("IC_inflight", CMD, 12))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [])
        dropped = self.one_event("dropped", "inflight")
        self.assertIn("inflight", dropped["reason"])

    def test_claim_requires_the_wave_lock(self):
        self.comments(comment("IC_nolock", CMD, 12))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        event_id = self.ids_in("queue")[0]
        code, out, err = self.run_cli("claim", event_id)
        self.assertStopped((code, out, err), 5, "wave lock", "claim before lock-acquire")
        self.assertEqual(self.ids_in("queue"), [event_id])


class Recovery(EvBase):
    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def test_an_orphaned_claim_moves_to_failed_and_is_never_rerun(self):
        self.comments(comment("IC_orph", CMD, 12))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        event_id = self.ids_in("queue")[0]
        self.take_lock()
        code, out, err = self.run_cli("claim", event_id)
        self.assertEqual(code, 0, err)
        claimed = self.read_event("claimed", event_id)
        # age the claim past the TTL: the run that claimed it died
        claimed["claimed_at"] = ago_iso(25 * 60)
        self.ev_dir("claimed", event_id + ".json").write_text(json.dumps(claimed), encoding="utf-8")
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertIn("orphaned claim", err, "orphan recovery is reported")
        self.assertEqual(self.ids_in("claimed"), [])
        failed = self.read_event("failed", event_id)
        self.assertIn("orphan", failed["reason"])
        ledger = self.ev_dir("starts.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(ledger), 1, "an orphaned claim is never re-run")

    def test_a_queued_event_past_the_ttl_expires(self):
        self.comments(comment("IC_ttl", CMD, 12, minutes_ago=1))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        event_id = self.ids_in("queue")[0]
        evt = self.read_event("queue", event_id)
        evt["enqueued_at"] = ago_iso(25 * 60)
        self.ev_dir("queue", event_id + ".json").write_text(json.dumps(evt), encoding="utf-8")
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("queue"), [])
        expired = self.read_event("expired", event_id)
        self.assertIn("ttl", expired["reason"])

    def test_next_expires_and_reports_nothing_dispatchable(self):
        self.comments(comment("IC_ttl2", CMD, 12, minutes_ago=1))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        event_id = self.ids_in("queue")[0]
        evt = self.read_event("queue", event_id)
        evt["enqueued_at"] = ago_iso(25 * 60)
        self.ev_dir("queue", event_id + ".json").write_text(json.dumps(evt), encoding="utf-8")
        code, out, err = self.run_cli("next")
        self.assertStopped((code, out, err), 3, None, "expired events are not dispatchable")
        self.assertEqual(self.ids_in("expired"), [event_id])


class ReadFailure(EvBase):
    def test_a_halted_read_leaves_the_queue_unchanged(self):
        self.board()
        self.comments(comment("IC_ok", CMD, 12))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        before_queue = self.ids_in("queue")
        before_seen = self.ev_dir("seen.jsonl").read_text(encoding="utf-8")
        cursor_before = json.loads(self.ev_dir("cursor.json").read_text(encoding="utf-8"))
        # the next tick: the board read fails (helper exit 79)
        self.comments(comment("IC_next", CMD, 13))
        MOD.run_read = HaltingGH(self.fixtures, self.calls, fail_on_call=len(self.calls))
        code, out, err = self.run_cli("poll")
        self.assertStopped((code, out, err), 79, None, "read halt propagates")
        self.assertEqual(self.ids_in("queue"), before_queue, "the queue is unchanged")
        self.assertEqual(self.ev_dir("seen.jsonl").read_text(encoding="utf-8"), before_seen)
        cursor_after = json.loads(self.ev_dir("cursor.json").read_text(encoding="utf-8"))
        self.assertTrue(cursor_after["last_error"], "the failure is recorded in the cursor")
        self.assertEqual(cursor_after["watermark"], cursor_before["watermark"], "no watermark advance on failure")

    def test_a_halted_first_read_enqueues_nothing(self):
        self.board()
        self.comments(comment("IC_f", CMD, 12))
        MOD.run_read = HaltingGH(self.fixtures, self.calls, fail_on_call=0)
        code, out, err = self.run_cli("poll")
        self.assertStopped((code, out, err), 79, None, "first-read halt")
        self.assertEqual(self.ids_in("queue"), [])


class Subcommands(EvBase):
    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")

    def enqueue(self, node="IC_sc"):
        self.comments(comment(node, CMD, 12))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        return self.ids_in("queue")[0]

    def test_event_files_carry_the_spec_field_set(self):
        event_id = self.enqueue()
        evt = self.read_event("queue", event_id)
        for field in ("id", "key", "source", "actor", "actor_permission", "issue", "pr", "lane",
                      "card_status", "node_id", "url", "created_at", "enqueued_at", "state",
                      "reason", "budget_reported"):
            self.assertIn(field, evt, "event field %s missing" % field)
        self.assertEqual(evt["id"], event_id)
        self.assertEqual(evt["state"], "queued")
        self.assertEqual(evt["key"], "issue_comment:IC_sc")
        self.assertEqual(evt["node_id"], "IC_sc")
        self.assertFalse(evt["budget_reported"])
        self.assertIsNone(evt["reason"])

    def test_done_moves_the_event_and_stores_the_result(self):
        event_id = self.enqueue()
        self.take_lock()
        code, out, err = self.run_cli("claim", event_id)
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("done", event_id, "--result", "wave 7: build advanced to QA")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("claimed"), [])
        done = self.read_event("done", event_id)
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["result"], "wave 7: build advanced to QA")

    def test_fail_moves_a_claimed_event_to_failed(self):
        event_id = self.enqueue()
        self.take_lock()
        self.run_cli("claim", event_id)
        code, out, err = self.run_cli("fail", event_id, "--reason", "card moved to Blocked mid-wave")
        self.assertEqual(code, 0, err)
        failed = self.read_event("failed", event_id)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["reason"], "card moved to Blocked mid-wave")

    def test_fail_works_on_a_queued_event(self):
        event_id = self.enqueue()
        code, out, err = self.run_cli("fail", event_id, "--reason", "stale")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.ids_in("failed"), [event_id])

    def test_unknown_ids_are_refused(self):
        for argv in (("claim", "0" * 40), ("done", "0" * 40, "--result", "x"),
                     ("fail", "0" * 40, "--reason", "x")):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 5, err)

    def test_done_without_result_is_a_usage_error(self):
        event_id = self.enqueue()
        self.take_lock()
        self.run_cli("claim", event_id)
        code, out, err = self.run_cli("done", event_id)
        self.assertEqual(code, 64, err)

    def test_status_reports_counts_and_budget(self):
        self.enqueue()
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertEqual(report["counts"]["queued"], 1)
        self.assertEqual(report["budget"]["max_starts_per_hour"], 3)
        self.assertEqual(report["budget"]["max_starts_per_day"], 10)
        self.assertTrue(report["ledger_ok"])

    def test_next_prints_the_event_json(self):
        event_id = self.enqueue()
        code, out, err = self.run_cli("next")
        self.assertEqual(code, 0, err)
        evt = json.loads(out)
        self.assertEqual(evt["id"], event_id)
        self.assertEqual(self.ids_in("queue"), [event_id], "next does not consume the event")

    def test_next_picks_the_oldest_queued_event(self):
        self.comments(comment("IC_o1", CMD, 12, minutes_ago=9),
                      comment("IC_o2", CMD, 14, minutes_ago=8))
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("next")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["node_id"], "IC_o1", "oldest first")

    def test_next_on_an_empty_queue(self):
        self.board()
        code, out, err = self.run_cli("next")
        self.assertStopped((code, out, err), 3, "no queued events", "empty queue")


class WriteVerbTripwire(EvBase):
    def test_a_full_poll_and_claim_only_ever_reads(self):
        self.board()
        self.perm("alice", "write")
        self.add_move(move_event("PVSCE_1", "alice"))
        helper = load_read_helper()
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.take_lock()
        event_id = self.ids_in("queue")[0]
        code, out, err = self.run_cli("claim", event_id)
        self.assertEqual(code, 0, err)
        self.run_cli("done", event_id, "--result", "ok")
        for kind, args in self.calls:
            self.assertTrue(helper.read_only(args), "a write verb reached gh: %s" % (args,))

    def test_the_call_budget_is_four_per_tick(self):
        self.board()
        self.perm("alice", "write")
        self.corr(p31=[12])
        self.comments(comment("IC_cap", CMD, 31))  # off-board number → correlation call
        code, out, err = self.run_cli("poll")
        self.assertEqual(code, 0, err)
        self.assertLessEqual(len(self.calls), 4, "at most 4 calls per tick: %r" % (self.calls,))


@unittest.skipUnless(os.name != "nt", "the PATH stub gh needs POSIX exec")
class PosixSubprocess(EvBase):
    """The same flows through the real script + stub gh on PATH (CI shape)."""

    def setUp(self):
        super().setUp()
        self.board()
        self.perm("alice", "write")
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir()
        stub = self.bin_dir / "gh"
        stub.write_text((FIXTURES / "gh-stub.py").read_text(encoding="utf-8"), encoding="utf-8")
        stub.chmod(0o755)
        self.env = dict(os.environ,
                        PATH=str(self.bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                        SB_EV_FIXTURES=str(self.fixtures),
                        SB_EV_GH_LOG=str(self.base / "gh-log.jsonl"),
                        SB_GITHUB_HALT_FILE=str(self.base / "halt.json"),
                        SB_GITHUB_RETRY_DELAY="0")

    def run_script(self, *argv):
        return subprocess.run([sys.executable, str(SCRIPT), "--config", str(self.cfg_path), *argv],
                              capture_output=True, text=True, env=self.env, cwd=str(self.root), timeout=120)

    def test_poll_status_claim_done_through_the_stub(self):
        self.add_move(move_event("PVSCE_stub", "alice"))  # the seed its in-process twin polls
        p = self.run_script("poll")
        self.assertEqual(p.returncode, 0, p.stderr)
        code, out, err = self.run_cli("status")
        report = json.loads(out)
        self.assertEqual(report["counts"]["queued"], 1, p.stderr)
        event_id = self.ids_in("queue")[0]
        self.take_lock()
        p = self.run_script("claim", event_id)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = self.run_script("done", event_id, "--result", "ok")
        self.assertEqual(p.returncode, 0, p.stderr)
        log = [json.loads(line) for line in
               (self.base / "gh-log.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        helper = load_read_helper()
        for argv in log:
            self.assertTrue(helper.read_only(argv), "a write verb reached the stub gh: %s" % (argv,))


if __name__ == "__main__":
    unittest.main(verbosity=1)
