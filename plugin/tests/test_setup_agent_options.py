"""ams.19: setup check lists the missing Agent matrix options as one manual step, never creating them. Offline."""
import importlib.util
import os
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(HERE, "..", "scripts", "super-board-setup.py")


def load():
    spec = importlib.util.spec_from_file_location("sb_setup_agent_options", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def payload(agent_options):
    return {"fields": [
        {"name": "Agent", "type": "ProjectV2SingleSelectField", "id": "F1",
         "options": [{"name": n} for n in agent_options]},
        {"name": "Skills", "type": "ProjectV2Field", "id": "F2"},
        {"name": "Card type", "type": "ProjectV2SingleSelectField", "id": "F3",
         "options": [{"name": t} for t in ("feature", "bug", "docs", "money", "refactor", "tests", "ux", "qa")]},
    ]}


class AgentMatrixTests(unittest.TestCase):
    def setUp(self):
        self.mod = load()

    def test_matrix_is_32_names(self):
        m = self.mod.AGENT_MATRIX
        self.assertEqual(len(m), 32)
        self.assertIn("Haiku · super-build", m)
        self.assertIn("Done · Opus · super-review", m)
        self.assertIn("Codex · super-build", m)
        self.assertIn("Opus · super-lint", m)
        self.assertIn("Done · Haiku · super-lint", m)
        self.assertIn("Done · Codex · super-review", m)
        self.assertFalse([n for n in m if "Fable" in n or "Grok" in n])

    def test_missing_computed_case_insensitive(self):
        have = ["haiku · super-build", "Sonnet · super-qa"]
        miss = self.mod.agent_matrix_missing({"options": [{"name": n} for n in have]})
        self.assertEqual(len(miss), 30)
        self.assertNotIn("Haiku · super-build", miss)
        self.assertNotIn("Sonnet · super-qa", miss)
        self.assertEqual(self.mod.agent_matrix_missing({"options": [{"name": n} for n in self.mod.AGENT_MATRIX]}), [])

    def test_report_adds_one_manual_step_and_creates_nothing(self):
        mod = self.mod
        have = [n for n in mod.AGENT_MATRIX + [o[0] for o in mod.AGENT_OPTIONS] if n != "Done · Haiku · super-qa"]
        calls = []

        def fake_gh_json(*args, input=None):
            calls.append(args)
            if args[:2] == ("project", "view"):
                return {"id": "PVT_1"}
            if args[:2] == ("project", "field-list"):
                return payload(have)
            raise AssertionError("unexpected gh call " + repr(args))

        root = tempfile.mkdtemp()
        with mock.patch.object(mod, "have", lambda t: True), \
                mock.patch.object(mod, "board_bindings", lambda r: [("configs/a.json", "acme", 1)]), \
                mock.patch.object(mod, "gh_json", fake_gh_json), \
                mock.patch.object(mod, "run", side_effect=AssertionError("must not create")):
            red, steps, errors = mod.board_fields_report(root)
        self.assertEqual((red, errors), ({}, {}))
        matrix_steps = [s for s in steps if s.startswith("Add these Agent options in the board UI")]
        self.assertEqual(len(matrix_steps), 1)
        self.assertIn("Done · Haiku · super-qa", matrix_steps[0])
        self.assertIn("the API would wipe values", matrix_steps[0])
        self.assertNotIn("Haiku · super-build", matrix_steps[0])
        self.assertTrue(all(a[0] == "project" for a in calls))

    def test_no_step_when_matrix_complete(self):
        mod = self.mod
        have = list(mod.AGENT_MATRIX) + [o[0] for o in mod.AGENT_OPTIONS]

        def fake_gh_json(*args, input=None):
            return {"id": "PVT_1"} if args[:2] == ("project", "view") else payload(have)

        with mock.patch.object(mod, "have", lambda t: True), \
                mock.patch.object(mod, "board_bindings", lambda r: [("configs/a.json", "acme", 1)]), \
                mock.patch.object(mod, "gh_json", fake_gh_json):
            _red, steps, _e = mod.board_fields_report(tempfile.mkdtemp())
        self.assertFalse([s for s in steps if s.startswith("Add these Agent options")])


if __name__ == "__main__":
    unittest.main()
