#!/usr/bin/env python3
"""Offline tests for super-board-watchdog.py (issue #68).

Every GitHub call goes through a stub `gh` on PATH that serves fixture JSON and
exits non-zero for any verb except `api graphql`. Every notification goes
through a stub notifier via SB_WATCHDOG_NOTIFY. `schtasks` is never involved
here (that is test-watchdog-install.sh). No network, no tokens, no model calls.

Cases 1-15 are the numbered cases from spec section 6; case 16 adds the
card-loop rule and the pagination/max-pages caps, which the numbered list
implies but does not name. Cases 17-20 cover the review findings: the halt
recorded as one operator-queue with reason `github-halt`, a pause suppressing
only stalls (loops still fire), the operator-queue "every evaluated card has
a wait reason" clause, and the transient deps payload (even when deps fails).

Run directly:
  python tests/test_watchdog.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "super-board-watchdog.py"
FIXTURES = ROOT / "tests" / "fixtures" / "watchdog"

SLUG = "wdtest"

GH_STUB_SOURCE = '''#!/usr/bin/env python3
"""Stub gh: logs argv, refuses every verb except `api graphql`, serves fixtures."""
import json, os, pathlib, sys, time

argv = sys.argv[1:]
log = pathlib.Path(os.environ["SB_WD_GH_LOG"])
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
if argv[:2] != ["api", "graphql"]:
    sys.stderr.write("stub gh: only 'api graphql' is allowed\\n")
    sys.exit(1)
if os.environ.get("SB_WD_FAIL"):
    sys.stderr.write("stub gh: forced failure\\n")
    sys.exit(1)
time.sleep(float(os.environ.get("SB_WD_SLEEP", "0")))
fixdir = pathlib.Path(os.environ["SB_WD_FIXTURES"])
page = "page2.json" if any(a.startswith("after=") for a in argv) else "page1.json"
sys.stdout.write((fixdir / page).read_text(encoding="utf-8"))
'''

NOTIFY_STUB_SOURCE = '''#!/usr/bin/env python3
"""Stub notifier: records the toast text (from the env, never argv), then exits."""
import json, os, pathlib, sys

log = pathlib.Path(sys.argv[1])
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps({"args": sys.argv[2:], "text": os.environ.get("SB_WATCHDOG_TEXT", "")}) + "\\n")
sys.exit(int(os.environ.get("SB_NOTIFY_RC", "0")))
'''


_TS_CACHE: dict = {}


def ago_iso(minutes: int) -> str:
    """A 'minutes ago' ISO stamp, stable WITHIN one test: the fixture writes it
    and the assertion reads it back, so the two must be the exact same string.
    setUp clears the cache per test."""
    cached = _TS_CACHE.get(minutes)
    if cached is None:
        cached = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _TS_CACHE[minutes] = cached
    return cached


def epoch_in(minutes: int) -> int:
    return int(time.time() + minutes * 60)


def page_item(number: int, column: str, *, state: str = "OPEN", body: str = "",
              comments: list | None = None, pr_minutes: int | None = None,
              updated_minutes: int = 10, title: str = "fixture card") -> dict:
    """One project item in the exact shape the watchdog's GraphQL read returns."""
    timeline = {"nodes": []}
    if pr_minutes is not None:
        timeline = {"nodes": [{"subject": {"commits": {"nodes": [
            {"committedDate": ago_iso(pr_minutes)}
        ]}}}]}
    return {
        "updatedAt": ago_iso(updated_minutes),
        "fieldValues": {"nodes": [{"name": column, "field": {"name": "Status"}}]},
        "content": {
            "__typename": "Issue",
            "number": number,
            "title": title,
            "state": state,
            "body": body,
            "labels": {"nodes": [{"name": "bug"}]},
            "assignees": {"nodes": []},
            "comments": {"nodes": comments or []},
            "timelineItems": timeline,
        },
    }


def comment(login: str, minutes: int, body: str) -> dict:
    return {"author": {"login": login}, "createdAt": ago_iso(minutes), "body": body}


BOT = "super-board-bot[bot]"
VIEWER = "octo-operator"
STRANGER = "stranger-account"


class WatchdogTestCase(unittest.TestCase):
    """Harness: a staged repo, a stub gh, a stub notifier, fixture pages."""

    def setUp(self) -> None:
        _TS_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self.temp.name)
        self.repo = self.tmp / "repo"
        (self.repo / ".claude" / "super-board" / "configs").mkdir(parents=True)
        self.config = self.repo / ".claude" / "super-board" / "configs" / f"{SLUG}.json"
        shutil.copyfile(FIXTURES / "wave-config.json", self.config)

        self.fixdir = self.tmp / "fixtures"
        self.fixdir.mkdir()
        shutil.copyfile(FIXTURES / "board-page1.json", self.fixdir / "page1.json")
        shutil.copyfile(FIXTURES / "board-page2.json", self.fixdir / "page2.json")

        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        stub = self.bindir / "gh_stub.py"
        stub.write_text(GH_STUB_SOURCE, encoding="utf-8")
        gh_sh = self.bindir / "gh"
        gh_sh.write_text(f'#!/bin/sh\nexec python3 "{stub}" "$@"\n', encoding="utf-8")
        gh_sh.chmod(0o755)
        if os.name == "nt":
            bat = self.bindir / "gh.bat"
            bat.write_text(
                f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n', encoding="utf-8"
            )

        self.gh_log = self.tmp / "gh-calls.log"
        self.notify_log = self.tmp / "notify.log"
        notify_stub = self.tmp / "notify_stub.py"
        notify_stub.write_text(NOTIFY_STUB_SOURCE, encoding="utf-8")

        self.env = dict(os.environ)
        self.env["PATH"] = str(self.bindir) + os.pathsep + self.env.get("PATH", "")
        self.env["SB_GITHUB_HALT_FILE"] = str(self.tmp / "halt.json")
        self.env["SB_USAGE_FILE"] = str(self.tmp / "usage.json")
        self.env["SB_WD_GH_LOG"] = str(self.gh_log)
        self.env["SB_WD_FIXTURES"] = str(self.fixdir)
        self.env["SB_WATCHDOG_NOTIFY"] = (
            f'"{sys.executable}" "{notify_stub}" "{self.notify_log}"'
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    # ── harness helpers ────────────────────────────────────────────────

    def run_watchdog(self, extra_env: dict | None = None,
                     script: pathlib.Path | None = None) -> subprocess.CompletedProcess:
        """One --once poll; `script` defaults to the repo's watchdog (a copy of
        the scripts dir can be polled instead, e.g. one with a failing deps)."""
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(script or SCRIPT), "--config", str(self.config), "--once"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(self.repo), env=env, timeout=300,
        )

    def wd_dir(self) -> pathlib.Path:
        return self.repo / ".claude" / "super-board" / "watchdog" / SLUG

    def state_path(self) -> pathlib.Path:
        return self.wd_dir() / "state.json"

    def jsonl_path(self) -> pathlib.Path:
        return self.wd_dir() / "incidents.jsonl"

    def read_state(self) -> dict:
        return json.loads(self.state_path().read_text(encoding="utf-8"))

    def jsonl_lines(self) -> list[dict]:
        if not self.jsonl_path().exists():
            return []
        text = self.jsonl_path().read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def gh_calls(self, log: pathlib.Path | None = None) -> list[list[str]]:
        path = log or self.gh_log
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def notify_entries(self) -> list[dict]:
        if not self.notify_log.exists():
            return []
        return [json.loads(line) for line in self.notify_log.read_text(encoding="utf-8").splitlines() if line.strip()]

    def load_page1(self) -> dict:
        """The CURRENT page 1 (setUp seeds it from the committed fixture, so
        only_items + set_item_field compose instead of clobbering)."""
        return json.loads((self.fixdir / "page1.json").read_text(encoding="utf-8"))

    def write_page1(self, page: dict) -> None:
        (self.fixdir / "page1.json").write_text(json.dumps(page), encoding="utf-8")

    def items(self) -> list[dict]:
        return self.load_page1()["data"]["repositoryOwner"]["projectV2"]["items"]["nodes"]

    def find_item(self, number: int) -> dict:
        for node in self.items():
            if node["content"]["number"] == number:
                return node
        raise AssertionError(f"fixture has no item #{number}")

    def only_items(self, *numbers: int) -> None:
        """Keep only the named items on page 1."""
        page = self.load_page1()
        nodes = [n for n in page["data"]["repositoryOwner"]["projectV2"]["items"]["nodes"]
                 if n["content"]["number"] in numbers]
        page["data"]["repositoryOwner"]["projectV2"]["items"]["nodes"] = nodes
        self.write_page1(page)

    def set_item_field(self, number: int, **fields) -> None:
        """Patch one item's content fields and/or its timestamps, in minutes ago."""
        page = self.load_page1()
        for node in page["data"]["repositoryOwner"]["projectV2"]["items"]["nodes"]:
            if node["content"]["number"] != number:
                continue
            if "column" in fields:
                node["fieldValues"]["nodes"][0]["name"] = fields["column"]
            if "updated_minutes" in fields:
                node["updatedAt"] = ago_iso(fields["updated_minutes"])
            if "title" in fields:
                node["content"]["title"] = fields["title"]
            if "body" in fields:
                node["content"]["body"] = fields["body"]
            if "comments" in fields:
                node["content"]["comments"]["nodes"] = fields["comments"]
            if "pr_minutes" in fields:
                if fields["pr_minutes"] is None:
                    node["content"]["timelineItems"] = {"nodes": []}
                else:
                    node["content"]["timelineItems"] = {"nodes": [
                        {"subject": {"commits": {"nodes": [
                            {"committedDate": ago_iso(fields["pr_minutes"])}]}}}]}
        self.write_page1(page)

    def make_lock(self, started_minutes_ago: int = 30) -> pathlib.Path:
        lock = self.repo / ".claude" / "super-board" / "inflight" / "workflow-wave.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        started = ago_iso(started_minutes_ago)
        lock.write_text(f"SLUG={SLUG}\nSTARTED={started}\n", encoding="utf-8")
        old = time.time() - started_minutes_ago * 60
        os.utime(lock, (old, old))
        return lock

    def drop_lock(self) -> None:
        lock = self.repo / ".claude" / "super-board" / "inflight" / "workflow-wave.lock"
        lock.unlink(missing_ok=True)

    def make_halt(self) -> None:
        halt = pathlib.Path(self.env["SB_GITHUB_HALT_FILE"])
        halt.parent.mkdir(parents=True, exist_ok=True)
        halt.write_text(json.dumps({"reason": "test halt", "at": int(time.time())}), encoding="utf-8")

    def halt_path(self) -> pathlib.Path:
        return pathlib.Path(self.env["SB_GITHUB_HALT_FILE"])

    def use_usage(self, fixture: str, *, used: int | None = None,
                  reset_in_minutes: int = 60) -> None:
        snap = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
        five = snap["rate_limits"]["five_hour"]
        if used is not None:
            five["used_percentage"] = used
        five["resets_at"] = epoch_in(reset_in_minutes)
        snap["recorded_at"] = int(time.time()) - 30
        pathlib.Path(self.env["SB_USAGE_FILE"]).write_text(json.dumps(snap), encoding="utf-8")

    def patch_config(self, **watchdog_fields) -> None:
        cfg = json.loads(self.config.read_text(encoding="utf-8"))
        cfg.setdefault("watchdog", {}).update(watchdog_fields)
        self.config.write_text(json.dumps(cfg), encoding="utf-8")

    # The default fixture board: #2 Ready, #7 Blocked human-gated, #12 Review,
    # #21 Blocked (unparseable), #30 Done. Timestamps are 2026-01-01, i.e. the
    # deep past for anything the test does not patch.

    # ── case 1 ─────────────────────────────────────────────────────────

    def test_01_stall_reported_once_then_again_after_new_activity(self):
        self.only_items(12)
        self.set_item_field(12, column="Review", updated_minutes=180,
                            comments=[comment(BOT, 180, "Build handoff for #12.")])
        self.make_lock(started_minutes_ago=30)

        first = self.run_watchdog()
        self.assertEqual(first.returncode, 0, first.stderr)
        lines = self.jsonl_lines()
        self.assertEqual(len(lines), 1, f"one incident expected, got {lines}")
        self.assertEqual(lines[0]["kind"], "card-stall")
        self.assertEqual(lines[0]["card"], 12)
        self.assertEqual(lines[0]["column"], "Review")
        self.assertEqual(lines[0]["since"], ago_iso(180))
        self.assertTrue(lines[0]["run_active"])
        self.assertTrue(179 <= lines[0]["evidence"]["minutes"] <= 182)
        self.assertEqual(lines[0]["notify"], "sent")
        state = self.read_state()
        self.assertEqual(len(state["reported"]), 1)
        self.assertEqual(list(state["reported"].values())[0]["kind"], "card-stall")
        entries = self.notify_entries()
        self.assertEqual(len(entries), 1)
        self.assertRegex(entries[0]["text"], r"^super-board wdtest: card-stall #12 \(Review, 18\d\d?m\)$")

        # two more polls: same key, silence.
        for _ in range(2):
            again = self.run_watchdog()
            self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(self.jsonl_lines()), 1)
        self.assertEqual(len(self.notify_entries()), 1)

        # new activity resets the clock: no incident, and the old key is pruned.
        self.set_item_field(12, updated_minutes=1, comments=[comment(BOT, 1, "review pushed")])
        fresh = self.run_watchdog()
        self.assertEqual(fresh.returncode, 0, fresh.stderr)
        self.assertEqual(len(self.jsonl_lines()), 1)
        self.assertEqual(self.read_state()["reported"], {})

        # stalled again on a newer clock: reported once more, under a new key.
        self.set_item_field(12, updated_minutes=200, comments=[comment(BOT, 200, "parked again")])
        second = self.run_watchdog()
        self.assertEqual(second.returncode, 0, second.stderr)
        lines = self.jsonl_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["kind"], "card-stall")
        self.assertNotEqual(lines[0]["key"], lines[1]["key"])
        self.assertEqual(len(self.notify_entries()), 2)

    # ── case 2 ─────────────────────────────────────────────────────────

    def test_02_operator_queue_once_then_new_card_new_report(self):
        self.only_items(7)  # Blocked, human-gated, fresh
        self.set_item_field(7, updated_minutes=5)

        first = self.run_watchdog()
        self.assertEqual(first.returncode, 0, first.stderr)
        lines = self.jsonl_lines()
        self.assertEqual(len(lines), 1, f"one report expected, got {lines}")
        self.assertEqual(lines[0]["kind"], "operator-queue")
        self.assertIsNone(lines[0]["card"])
        self.assertEqual(lines[0]["waits"], [{"card": 7, "reason": "human-gated"}])
        key = lines[0]["key"]

        for _ in range(3):  # three silent polls
            self.assertEqual(self.run_watchdog().returncode, 0)
        self.assertEqual(len(self.jsonl_lines()), 1)
        self.assertEqual(self.read_state()["reported"][key]["kind"], "operator-queue")

        # a second human-gated card changes the wait set: one new report.
        extra = page_item(8, "Blocked", body="## Blocked by\n\n- None.\n",
                          comments=[comment(BOT, 4, "Needs a ruling.\n\nblocked-by: -")],
                          updated_minutes=4)
        page = self.load_page1()
        page["data"]["repositoryOwner"]["projectV2"]["items"]["nodes"].append(extra)
        self.write_page1(page)
        second = self.run_watchdog()
        self.assertEqual(second.returncode, 0, second.stderr)
        lines = self.jsonl_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1]["kind"], "operator-queue")
        self.assertNotEqual(key, lines[1]["key"])
        self.assertEqual(lines[1]["waits"], [
            {"card": 7, "reason": "human-gated"}, {"card": 8, "reason": "human-gated"}])

    # ── case 3 ─────────────────────────────────────────────────────────

    def test_03_stranger_blocked_by_does_not_silence_card(self):
        self.only_items(2, 7)
        self.set_item_field(2, column="Ready", updated_minutes=5)
        self.set_item_field(
            7, updated_minutes=120,
            comments=[comment(BOT, 120, "parked on #2\n\nblocked-by: 2"),
                      comment(STRANGER, 2, "unblocking this myself\n\nblocked-by: -")])
        self.drop_lock()

        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.jsonl_lines()
        queues = [line for line in lines if line["kind"] == "operator-queue"]
        self.assertEqual(len(queues), 1, f"expected the queue report, got {lines}")
        reasons = {w["card"]: w["reason"] for w in queues[0]["waits"]}
        self.assertEqual(reasons.get(7), "depends-on:#2",
                         "a stranger's `blocked-by: -` must not human-gate the card")
        self.assertNotIn("human-gated", reasons.values())

    # ── case 4 ─────────────────────────────────────────────────────────

    def test_04_stranger_comment_does_not_reset_stall_clock(self):
        self.only_items(12)
        self.set_item_field(12, column="Building", updated_minutes=180,
                            comments=[comment(BOT, 180, "build handoff"),
                                      comment(STRANGER, 3, "any progress here?")])
        self.make_lock(started_minutes_ago=30)

        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in self.jsonl_lines() if line["kind"] == "card-stall"]
        self.assertEqual(len(lines), 1, "the stranger's comment must not reset the clock")
        self.assertEqual(lines[0]["since"], ago_iso(180))
        self.assertTrue(179 <= lines[0]["evidence"]["minutes"] <= 182)

    # ── case 5 ─────────────────────────────────────────────────────────

    def test_05_unparseable_blocked_card_is_unknown_state(self):
        self.only_items(21)  # body says None and names #12: the deps landmine
        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.jsonl_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["kind"], "unknown-state")
        self.assertEqual(lines[0]["card"], 21)
        self.assertEqual(lines[0]["column"], "Blocked")

    # ── case 6 ─────────────────────────────────────────────────────────

    def test_06_ready_without_lock_gets_no_run_and_no_stall(self):
        self.only_items(12)
        self.set_item_field(12, column="Ready", updated_minutes=180,
                            comments=[comment(BOT, 180, "handoff")])
        self.drop_lock()

        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        kinds = [line["kind"] for line in self.jsonl_lines()]
        self.assertNotIn("card-stall", kinds)
        queues = [line for line in self.jsonl_lines() if line["kind"] == "operator-queue"]
        self.assertTrue(queues, "a Ready card with no run is exactly what the operator must hear about")
        reasons = {w["card"]: w["reason"] for w in queues[0]["waits"]}
        self.assertEqual(reasons.get(12), "no-run")

    # ── case 7 ─────────────────────────────────────────────────────────

    def test_07_run_stall(self):
        self.only_items(30)  # a Done card: nothing active, everything old
        self.make_lock(started_minutes_ago=120)

        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.jsonl_lines()
        self.assertEqual(len(lines), 1, f"expected one run-stall, got {lines}")
        self.assertEqual(lines[0]["kind"], "run-stall")
        self.assertIsNone(lines[0]["card"])
        self.assertIsNone(lines[0]["column"])
        self.assertTrue(lines[0]["run_active"])
        self.assertEqual(lines[0]["evidence"]["lock_started"], ago_iso(120))
        self.assertTrue(119 <= lines[0]["evidence"]["minutes"] <= 123)

    # ── case 8 ─────────────────────────────────────────────────────────

    def test_08_usage_pause_suppresses_stalls(self):
        self.only_items(12)
        self.set_item_field(12, column="Building", updated_minutes=180,
                            comments=[comment(BOT, 180, "handoff")])
        self.make_lock(started_minutes_ago=30)
        self.use_usage("usage-pause.json", used=97, reset_in_minutes=60)

        paused = self.run_watchdog()
        self.assertEqual(paused.returncode, 0, paused.stderr)
        self.assertEqual(self.jsonl_lines(), [], "stalls are suppressed during a usage pause")

        # control: the same board with no usage snapshot still reports the stall.
        os.unlink(self.env["SB_USAGE_FILE"])
        control = self.run_watchdog()
        self.assertEqual(control.returncode, 0, control.stderr)
        kinds = [line["kind"] for line in self.jsonl_lines()]
        self.assertIn("card-stall", kinds)

    # ── case 9 ─────────────────────────────────────────────────────────

    def test_09_halt_makes_no_gh_calls(self):
        self.only_items(12)
        self.set_item_field(12, column="Building", updated_minutes=180,
                            comments=[comment(BOT, 180, "handoff")])
        self.make_lock(started_minutes_ago=120)
        self.make_halt()

        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.gh_calls(), [], "a halt must stop every GitHub call")
        lines = self.jsonl_lines()
        self.assertEqual([line["kind"] for line in lines], ["operator-queue"],
                         "the halt itself is the only incident the poll reports")
        self.assertEqual(lines[0]["waits"], [{"card": None, "reason": "github-halt"}])
        self.assertTrue(self.state_path().exists())
        self.assertTrue(self.read_state()["last_poll"])

    # ── case 10 ────────────────────────────────────────────────────────

    def test_10_gh_failure_is_blind_without_halt(self):
        # pre-seed a reported key that no current incident would keep alive:
        # a blind poll must NOT prune it.
        self.wd_dir().mkdir(parents=True, exist_ok=True)
        self.state_path().write_text(json.dumps({
            "version": 1, "slug": SLUG, "last_poll": ago_iso(30), "last_ok": ago_iso(30),
            "blind_since": None,
            "reported": {"deadbeef1234": {"kind": "card-stall", "first": ago_iso(30)}},
        }), encoding="utf-8")

        failed = self.run_watchdog(extra_env={"SB_WD_FAIL": "1"})
        self.assertEqual(failed.returncode, 0, "a read failure still exits 0")
        lines = self.jsonl_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["kind"], "watchdog-blind")
        self.assertEqual(self.notify_entries()[0]["text"], "super-board wdtest: watchdog-blind")
        state = self.read_state()
        self.assertIn("deadbeef1234", state["reported"], "blind polls must not prune state")
        self.assertTrue(state["blind_since"])
        self.assertFalse(self.halt_path().exists(), "the watchdog must never write the halt file")

    # ── case 11 ────────────────────────────────────────────────────────

    def test_11_rate_limit_floor_stops_reading(self):
        page = self.load_page1()
        page["data"]["rateLimit"]["remaining"] = 300  # below gh_points_floor (500)
        page["data"]["repositoryOwner"]["projectV2"]["items"]["pageInfo"] = {
            "hasNextPage": True, "endCursor": "cursor-1"}
        self.write_page1(page)
        self.only_items(12)
        self.set_item_field(12, column="Building", updated_minutes=180,
                            comments=[comment(BOT, 180, "handoff")])
        self.make_lock(started_minutes_ago=30)

        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.gh_calls()), 1, "the floor must stop the read after page 1")
        kinds = [line["kind"] for line in self.jsonl_lines()]
        self.assertEqual(kinds, ["card-stall"], "the page already read is still evaluated")

    # ── case 12 ────────────────────────────────────────────────────────

    def test_12_hostile_title_never_leaks(self):
        hostile_title = '"><svg onload=alert(1)> secret-title-x7'
        self.only_items(12)
        self.set_item_field(12, column="Building", updated_minutes=180,
                            title=hostile_title, body="secret-body-q3\n\n## Blocked by\n\n- None.\n",
                            comments=[comment(BOT, 180, "handoff secret-trusted-c1"),
                                      comment(STRANGER, 3, "secret-comment-z9")])
        self.make_lock(started_minutes_ago=30)

        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        kinds = [line["kind"] for line in self.jsonl_lines()]
        self.assertIn("card-stall", kinds, "the hostile card still stays alertable")

        markers = ["secret-title-x7", "secret-body-q3", "secret-comment-z9", "secret-trusted-c1",
                   "<svg", "onerror", "alert(1)"]
        watched = [self.state_path(), self.jsonl_path(), self.notify_log]
        for path in watched:
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                for marker in markers:
                    self.assertNotIn(marker, text, f"{marker} leaked into {path.name}")
        for marker in markers:
            self.assertNotIn(marker, result.stdout)
            self.assertNotIn(marker, result.stderr)
        # positive control: the number and the column did make it out.
        recorded = [json.loads(line) for line in
                    self.jsonl_path().read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertTrue(any(inc.get("card") == 12 for inc in recorded))
        self.assertTrue(any(inc.get("column") == "Building" for inc in recorded))

    # ── case 13 ────────────────────────────────────────────────────────

    def test_13_concurrent_second_invocation_exits(self):
        # (a) a fresh overlap lock held by "someone else" -> immediate exit 0.
        lock_dir = self.wd_dir() / "lock"
        lock_dir.mkdir(parents=True)
        (lock_dir / "owner.json").write_text("{}", encoding="utf-8")
        other_log = self.tmp / "other-gh.log"
        result = self.run_watchdog(extra_env={"SB_WD_GH_LOG": str(other_log)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.gh_calls(other_log), [], "the second invocation must not read")
        self.assertFalse(self.state_path().exists(), "the second invocation must not write state")

        # (b) a real concurrent invocation: the first one sleeps inside gh.
        self.drop_lock()
        shutil.rmtree(lock_dir, ignore_errors=True)
        first_log = self.tmp / "first-gh.log"
        env = dict(self.env)
        env["SB_WD_GH_LOG"] = str(first_log)
        env["SB_WD_SLEEP"] = "8"
        first = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--config", str(self.config), "--once"],
            cwd=str(self.repo), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2.0)
        started = time.time()
        second = self.run_watchdog()
        elapsed = time.time() - started
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertLess(elapsed, 6.0, "the second invocation must exit while the first holds the lock")
        first.wait(timeout=120)
        self.assertTrue(self.gh_calls(first_log), "the first invocation did its own read")

    # ── case 14 ────────────────────────────────────────────────────────

    def test_14_failed_notify_is_never_retried(self):
        self.only_items(12)
        self.set_item_field(12, column="Building", updated_minutes=180,
                            comments=[comment(BOT, 180, "handoff")])
        self.make_lock(started_minutes_ago=30)

        failed = self.run_watchdog(extra_env={"SB_NOTIFY_RC": "1"})
        self.assertEqual(failed.returncode, 0, failed.stderr)
        lines = self.jsonl_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["notify"], "failed")
        self.assertEqual(len(self.notify_entries()), 1)

        # even with the notifier healthy again, the same key is never re-sent.
        retry = self.run_watchdog()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(len(self.jsonl_lines()), 1)
        self.assertEqual(len(self.notify_entries()), 1)

    # ── case 15 ────────────────────────────────────────────────────────

    def test_15_source_has_no_model_calls_or_write_verbs(self):
        source = SCRIPT.read_text(encoding="utf-8")
        model_patterns = [
            (r"(?<![.\w])claude", "bare `claude` (only .claude/ paths are allowed)"),
            (r"Claude", "capitalized `Claude`"),
            (r"codex exec", "`codex exec`"),
            (r"codex-delegate", "`codex-delegate`"),
            (r"(?i)anthropic", "anthropic"),
            (r"Workflow", "`Workflow` (dynamic workflow references)"),
        ]
        for pattern, why in model_patterns:
            self.assertIsNone(re.search(pattern, source),
                              f"model-call token found ({why}): {re.search(pattern, source)}")
        write_patterns = [
            r"\bgh\s+(issue|pr|project|repo|label|release|auth)\b",
            r"\bmutation\b",
            r"-X\s+(POST|PUT|PATCH|DELETE)\b",
            r"\.env\b",
            r"hosts\.yml",
        ]
        for pattern in write_patterns:
            self.assertIsNone(re.search(pattern, source), f"forbidden verb/token: {pattern}")
        self.assertIn("api", source)
        self.assertIn("graphql", source)

    # ── case 16 (extra: card-loop + pagination caps) ───────────────────

    def test_16_card_loop_and_pagination_caps(self):
        # card-loop: rebuild_cap (2) + 1 trusted comments carry the same hash.
        self.only_items(12)
        self.set_item_field(12, column="QA", updated_minutes=80, comments=[
            comment(BOT, 200, "failed again\n\nroot-cause-hash: aabbccddeeff"),
            comment(BOT, 150, "failed again\n\nroot-cause-hash: aabbccddeeff"),
            comment(BOT, 80, "failed again\n\nroot-cause-hash: aabbccddeeff"),
        ])
        self.make_lock(started_minutes_ago=30)
        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        loops = [line for line in self.jsonl_lines() if line["kind"] == "card-loop"]
        self.assertEqual(len(loops), 1, f"expected the loop incident, got {self.jsonl_lines()}")
        self.assertEqual(loops[0]["evidence"]["hash"], "aabbccddeeff")
        self.assertEqual(loops[0]["since"], ago_iso(200))
        # a stranger's hash line must not count: only 3 trusted ones exist.
        self.assertFalse(any(l["evidence"]["hash"] == "ffffffffffff" for l in self.jsonl_lines()))

        # pagination: page 1 has a next page, so exactly two gh calls happen.
        page = self.load_page1()
        page["data"]["repositoryOwner"]["projectV2"]["items"]["pageInfo"] = {
            "hasNextPage": True, "endCursor": "cursor-1"}
        self.write_page1(page)
        (self.fixdir / "page2.json").write_text(
            json.dumps(json.loads((FIXTURES / "board-page2.json").read_text(encoding="utf-8"))),
            encoding="utf-8")
        two = self.run_watchdog()
        self.assertEqual(two.returncode, 0, two.stderr)
        self.assertEqual(len(self.gh_calls()), 3)  # 1 (first poll) + 2 (two-page poll)

        # max_pages caps the read: with max_pages=1 the second page is never fetched.
        self.patch_config(max_pages=1)
        capped = self.run_watchdog()
        self.assertEqual(capped.returncode, 0, capped.stderr)
        self.assertEqual(len(self.gh_calls()), 4)  # + 1 call under the max_pages cap

    # ── case 17 (extra: the halt is recorded, once, with reason github-halt) ─

    def test_17_halt_reports_one_operator_queue_with_github_halt(self):
        self.make_lock(started_minutes_ago=120)
        self.make_halt()

        first = self.run_watchdog()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.gh_calls(), [], "a halt must stop every GitHub call")
        lines = self.jsonl_lines()
        self.assertEqual(len(lines), 1, f"the halt must be recorded once, got {lines}")
        self.assertEqual(lines[0]["kind"], "operator-queue")
        self.assertIsNone(lines[0]["card"])
        self.assertEqual(lines[0]["waits"], [{"card": None, "reason": "github-halt"}])
        key = lines[0]["key"]
        self.assertEqual(self.notify_entries()[0]["text"], "super-board wdtest: operator-queue")

        for _ in range(2):  # the halt persists: same key, silence
            self.assertEqual(self.run_watchdog().returncode, 0)
        self.assertEqual(len(self.jsonl_lines()), 1)
        self.assertEqual(len(self.notify_entries()), 1)
        reported = self.read_state()["reported"]
        self.assertEqual(list(reported), [key], "the halt key is constant while the halt persists")
        self.assertEqual(reported[key]["kind"], "operator-queue")

        # the halt lifts: the next full read reports nothing new and prunes the key.
        self.halt_path().unlink()
        self.drop_lock()
        self.only_items(30)
        self.set_item_field(30, updated_minutes=5)
        lifted = self.run_watchdog()
        self.assertEqual(lifted.returncode, 0, lifted.stderr)
        self.assertEqual(len(self.jsonl_lines()), 1,
                         "a lifted halt reports nothing new (the log is append-only)")
        self.assertEqual(self.read_state()["reported"], {},
                         "the halt key must be pruned on the next full read")

    # ── case 18 (extra: a pause suppresses only stalls, not loops) ────────

    def test_18_usage_pause_suppresses_stalls_but_not_loops(self):
        self.only_items(12)
        # stalled AND looping: the newest trusted comment is 100m old
        # (> card_stall_minutes=90) and three trusted comments (rebuild_cap=2
        # + 1) repeat the same root-cause hash.
        self.set_item_field(12, column="QA", updated_minutes=180, comments=[
            comment(BOT, 200, "failed again\n\nroot-cause-hash: aabbccddeeff"),
            comment(BOT, 150, "failed again\n\nroot-cause-hash: aabbccddeeff"),
            comment(BOT, 100, "failed again\n\nroot-cause-hash: aabbccddeeff"),
        ])
        self.make_lock(started_minutes_ago=30)
        self.use_usage("usage-pause.json", used=97, reset_in_minutes=60)

        paused = self.run_watchdog()
        self.assertEqual(paused.returncode, 0, paused.stderr)
        kinds = [line["kind"] for line in self.jsonl_lines()]
        self.assertEqual(kinds, ["card-loop"],
                         "the pause suppresses the stall but must not hide the loop")
        self.assertEqual(self.jsonl_lines()[0]["evidence"]["hash"], "aabbccddeeff")

        # positive control: the same board without the usage snapshot reports
        # the stall too, while the loop stays reported exactly once (same key).
        os.unlink(self.env["SB_USAGE_FILE"])
        control = self.run_watchdog()
        self.assertEqual(control.returncode, 0, control.stderr)
        kinds = [line["kind"] for line in self.jsonl_lines()]
        self.assertIn("card-stall", kinds)
        self.assertEqual(kinds.count("card-loop"), 1)

    # ── case 19 (extra: operator-queue needs EVERY evaluated card to wait) ──

    def test_19_operator_queue_requires_every_card_to_wait(self):
        self.only_items(2, 7)
        self.set_item_field(2, updated_minutes=5)  # Ready, fresh, run active: no wait reason
        self.set_item_field(7, updated_minutes=5)  # Blocked, human-gated: waits on you
        self.make_lock(started_minutes_ago=30)

        result = self.run_watchdog()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.jsonl_lines(), [],
            "one evaluated card without a wait reason vetoes the operator-queue report")

        # positive control: without the wave lock the Ready card waits too
        # (no-run), every evaluated card has a reason, and the report fires.
        self.drop_lock()
        control = self.run_watchdog()
        self.assertEqual(control.returncode, 0, control.stderr)
        queues = [line for line in self.jsonl_lines() if line["kind"] == "operator-queue"]
        self.assertEqual(len(queues), 1, f"expected the queue report, got {self.jsonl_lines()}")
        reasons = {w["card"]: w["reason"] for w in queues[0]["waits"]}
        self.assertEqual(reasons, {2: "no-run", 7: "human-gated"})

    # ── case 20 (extra: the deps payload is transient, even on failure) ────

    def test_20_deps_payload_deleted_after_poll_even_when_deps_fails(self):
        # success: the card-stall proves the full read ran deps.sh; the payload
        # file must still be gone afterwards.
        self.only_items(12)
        self.set_item_field(12, column="Building", updated_minutes=180,
                            comments=[comment(BOT, 180, "handoff")])
        self.make_lock(started_minutes_ago=30)
        ok = self.run_watchdog()
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual([line["kind"] for line in self.jsonl_lines()], ["card-stall"],
                         "control: the poll did a full read through deps.sh")
        self.assertFalse((self.wd_dir() / "deps-payload.json").exists(),
                         "the temp deps payload must be deleted after the poll")

        # failure: a scripts copy whose deps.sh always exits nonzero leaves no
        # payload behind either; the poll goes blind, never silent.
        scripts_copy = self.tmp / "scripts-deps-fail"
        scripts_copy.mkdir()
        shutil.copyfile(SCRIPT, scripts_copy / SCRIPT.name)
        (scripts_copy / "super-board-deps.sh").write_text(
            "#!/bin/sh\necho 'stub deps.sh: forced failure' >&2\nexit 7\n",
            encoding="utf-8")
        failed = self.run_watchdog(script=scripts_copy / SCRIPT.name)
        self.assertEqual(failed.returncode, 0, failed.stderr)
        self.assertEqual([line["kind"] for line in self.jsonl_lines()],
                         ["card-stall", "watchdog-blind"],
                         "a deps failure blinds the poll on top of the earlier stall")
        self.assertFalse((self.wd_dir() / "deps-payload.json").exists(),
                         "a failed deps.sh must still leave no payload behind")


if __name__ == "__main__":
    unittest.main()
