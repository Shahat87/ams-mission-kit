#!/usr/bin/env python3
"""Failure kinds from tool output, never from the exit code alone (spec §6.3)."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lane_sig", ROOT / "scripts/super-board-lane-signatures.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
FIX = ROOT / "tests/fixtures/delegate-signatures"


def text(name):
    return (FIX / name).read_text(encoding="utf-8")


class SignatureTests(unittest.TestCase):
    def test_fixtures(self):
        cases = {
            "quota-codex.txt": "quota", "quota-warp.txt": "quota", "auth-gh.txt": "auth",
            "auth-env-pi.txt": "auth:env", "model-codex-400.txt": "model_error",
        }
        for name, kind in cases.items():
            with self.subTest(name=name):
                self.assertEqual(S.classify(text(name), exit_code=0, changed=False), kind)

    def test_exit_zero_with_no_changes_and_confirm_text_is_ask_confirm(self):
        self.assertEqual(S.classify(text("ask-confirm-codex.txt"), exit_code=0, changed=False), "ask_confirm")

    def test_exit_zero_no_changes_plain_is_no_changes(self):
        self.assertEqual(S.classify("done", exit_code=0, changed=False), "no_changes")

    def test_ok_needs_changes(self):
        self.assertEqual(S.classify(text("ok-codex.txt"), exit_code=0, changed=True), "ok")

    def test_missing_cli(self):
        self.assertEqual(S.classify("", exit_code=127, changed=False), "missing")

    def test_failure_text_wins_over_changes(self):
        self.assertEqual(S.classify(text("quota-codex.txt"), exit_code=0, changed=True), "quota")

    def test_nonzero_unknown_is_model_error(self):
        self.assertEqual(S.classify("segfault", exit_code=3, changed=False), "model_error")

    def test_review_lane_ok_without_changes(self):
        self.assertEqual(S.classify("LGTM with 2 findings", exit_code=0, changed=False, read_only=True), "ok")

    def stream(self, *events):
        return "\n".join(json.dumps(e) for e in events)

    def test_failure_text_ignores_model_prose_in_json_events(self):
        out = self.stream({"type": "thread.started"},
                          {"type": "agent_message", "text": "fixed the 401 quota rate limit handling"})
        self.assertEqual(S.classify(S.failure_text(out, "", ""), 0, changed=True), "ok")

    def test_failure_text_keeps_error_events(self):
        out = self.stream({"type": "agent_message", "text": "working"}, {"type": "turn.failed", "error": "usage limit reached"})
        self.assertEqual(S.classify(S.failure_text(out, "", ""), 0, changed=True), "quota")
        out = self.stream({"msg": {"type": "Error", "message": "usage limit"}})
        self.assertEqual(S.classify(S.failure_text(out, "", ""), 0, changed=True), "quota")
        out = self.stream({"event": "stream_error", "message": "usage limit"})
        self.assertEqual(S.classify(S.failure_text(out, "", ""), 0, changed=True), "quota")

    def test_failure_text_keeps_stderr_final_and_last_plain_lines(self):
        self.assertEqual(S.classify(S.failure_text("", "No API key found for runinfra.", ""), 0, changed=True), "auth:env")
        self.assertEqual(S.classify(S.failure_text("", "", "usage limit hit"), 0, changed=True), "quota")
        lines = "\n".join(["Error: rate limit"] + [f"progress {i}" for i in range(10)])
        self.assertEqual(S.classify(S.failure_text(lines, "", ""), 0, changed=True), "ok")
        self.assertEqual(S.classify(S.failure_text("log\nError: rate limit", "", ""), 0, changed=True), "quota")


if __name__ == "__main__":
    unittest.main()
