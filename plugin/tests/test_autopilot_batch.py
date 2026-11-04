"""super-board-autopilot-batch.py: pure batch selection for mission-control autopilot (§4b); no network, no side effects."""
import json
import pathlib
import subprocess
import sys
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "super-board-autopilot-batch.py"
SKILL = pathlib.Path(__file__).resolve().parents[1] / "skills" / "mission-control" / "SKILL.md"


class Base(unittest.TestCase):
    def card(self, number, status="Backlog", labels=("goal:checkout",), body="", title=None):
        return {"number": number, "title": title or f"card {number}", "status": status,
                "labels": list(labels), "body": body}

    def run_batch(self, cards, open_issues=(), batch_size=3, goal="checkout", raw=None):
        payload = raw if raw is not None else json.dumps({"cards": cards, "open_issues": list(open_issues)})
        return subprocess.run([sys.executable, str(SCRIPT), "--goal", goal, "--batch-size", str(batch_size)],
                              input=payload, capture_output=True, text=True, timeout=30)

    def out(self, cards, open_issues=(), batch_size=3, goal="checkout", raw=None):
        p = self.run_batch(cards, open_issues, batch_size, goal, raw)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)


class Matching(Base):
    def test_only_the_exact_goal_label_matches(self):
        out = self.out([self.card(12, labels=("goal:checkout", "bug")),
                        self.card(13, labels=("goal:checkout-v2",)),   # a different goal label
                        self.card(14, labels=("goal:payments",)),      # another goal
                        self.card(15, labels=("bug",)),                # unlabelled for this goal
                        self.card(16, labels=())])
        self.assertEqual(out["eligible"], [12])
        self.assertEqual(out["batch"], [12])
        self.assertEqual(out["reason"], "ok")

    def test_the_label_match_ignores_case(self):
        out = self.out([self.card(12, labels=("GOAL:Checkout",)), self.card(13, labels=("Goal:CHECKOUT",))])
        self.assertEqual(out["eligible"], [12, 13])
        self.assertEqual(out["batch"], [12, 13])

    def test_gh_label_objects_with_a_name_key_match(self):
        # `gh --json labels` returns [{"name": ..., "color": ...}], not plain strings.
        out = self.out([self.card(12, labels=[{"name": "goal:checkout", "color": "0e8a16"}, "bug"]),
                        self.card(13, labels=[{"name": "goal:payments"}])])
        self.assertEqual(out["eligible"], [12])
        self.assertEqual(out["batch"], [12])
        self.assertEqual(out["reason"], "ok")


class BatchAndOrder(Base):
    def test_batch_size_cuts_the_eligible_list(self):
        out = self.out([self.card(n) for n in (12, 13, 14, 15, 16)], batch_size=2)
        self.assertEqual(out["eligible"], [12, 13, 14, 15, 16])
        self.assertEqual(out["batch"], [12, 13])
        self.assertEqual(out["reason"], "ok")

    def test_eligible_is_ordered_by_issue_number(self):
        out = self.out([self.card(40), self.card(12), self.card(15)], batch_size=2)
        self.assertEqual(out["eligible"], [12, 15, 40])
        self.assertEqual(out["batch"], [12, 15])


class Blockers(Base):
    def test_an_open_blocker_makes_the_card_ineligible(self):
        body = "## Blocked by\n- #15 — needs the schema card\n"
        out = self.out([self.card(12, body=body)], open_issues=[15])
        self.assertEqual(out["eligible"], [])
        self.assertEqual(out["batch"], [])
        self.assertEqual(out["reason"], "all-blocked-by-deps")

    def test_a_closed_blocker_leaves_the_card_eligible(self):
        body = "## Blocked by\n- #15 — needs the schema card\n"
        out = self.out([self.card(12, body=body)], open_issues=[])
        self.assertEqual(out["eligible"], [12])
        self.assertEqual(out["batch"], [12])
        self.assertEqual(out["reason"], "ok")

    def test_a_blocker_in_the_same_candidate_set_still_counts_as_open(self):
        body = "## Blocked by\n- #15 — same-goal dependency\n"
        out = self.out([self.card(12, body=body), self.card(15)], open_issues=[12, 15])
        self.assertEqual(out["eligible"], [15])          # 12 waits: #15 is still an open candidate
        self.assertNotIn(12, out["eligible"])
        self.assertEqual(out["batch"], [15])

    def test_none_section_means_no_blockers(self):
        out = self.out([self.card(12, body="## Blocked by\n- None.\n")], open_issues=[40])
        self.assertEqual(out["eligible"], [12])
        self.assertEqual(out["reason"], "ok")

    def test_a_body_without_the_section_has_no_blockers(self):
        out = self.out([self.card(12, body="## Summary\ndo the thing\n")], open_issues=[40])
        self.assertEqual(out["eligible"], [12])

    def test_the_skip_picks_only_unblocked_cards(self):
        out = self.out([self.card(12, body="## Blocked by\n- #15 — waiting\n"),
                        self.card(16, body="## Blocked by\n- None.\n"),
                        self.card(18, body="## Blocked by\n- #10 — done last week\n")],
                       open_issues=[10, 15])
        self.assertEqual(out["eligible"], [16])
        self.assertEqual(out["batch"], [16])
        self.assertEqual(out["reason"], "ok")


class OpenIssues(Base):
    def test_gh_issue_objects_with_a_number_key_work_and_blockers_count_as_open(self):
        # `gh issue list --state open --json number` returns [{"number": ...}], not plain ints.
        body = "## Blocked by\n- #15 — needs the schema card\n"
        out = self.out([self.card(12, body=body), self.card(16)],
                       open_issues=[{"number": 15}, {"number": 40}])
        self.assertEqual(out["eligible"], [16])
        self.assertEqual(out["batch"], [16])
        self.assertEqual(out["reason"], "ok")

    def test_a_mixed_list_of_ints_and_objects_works(self):
        out = self.out([self.card(12, body="## Blocked by\n- #40 — still open\n")],
                       open_issues=[12, {"number": 40}])
        self.assertEqual(out["eligible"], [])
        self.assertEqual(out["reason"], "all-blocked-by-deps")


class Reasons(Base):
    def test_a_blocked_goal_card_stops_the_batch(self):
        out = self.out([self.card(12, status="Blocked"), self.card(13)], open_issues=[])
        self.assertEqual(out["reason"], "blocked")
        self.assertEqual(out["batch"], [])
        self.assertEqual(out["waiting"], [12])

    def test_empty_when_no_goal_cards_are_in_backlog_or_lanes(self):
        out = self.out([self.card(12, status="Done"), self.card(13, labels=("bug",)), self.card(14, labels=())],
                       open_issues=[12])
        self.assertEqual(out["reason"], "empty")
        self.assertEqual(out["batch"], [])
        self.assertEqual(out["eligible"], [])
        self.assertEqual(out["in_flight"], [])

    def test_in_flight_when_lanes_are_busy_and_backlog_is_empty(self):
        out = self.out([self.card(12, status="Ready"), self.card(14, status="QA"), self.card(15, status="Building"),
                        self.card(16, status="Review")], open_issues=[12, 14, 15, 16])
        self.assertEqual(out["reason"], "in-flight")
        self.assertEqual(out["batch"], [])
        self.assertEqual(out["in_flight"], [12, 14, 15, 16])

    def test_all_blocked_by_deps_when_candidates_exist_but_none_is_eligible(self):
        out = self.out([self.card(12, body="## Blocked by\n- #40 — open\n"),
                        self.card(13, body="## Blocked by\n- #41 — open\n")], open_issues=[40, 41])
        self.assertEqual(out["reason"], "all-blocked-by-deps")
        self.assertEqual(out["batch"], [])
        self.assertEqual(out["eligible"], [])


class BadInput(unittest.TestCase):
    def test_unreadable_json_exits_2(self):
        p = Base.run_batch(Base(), (), raw="not json")
        self.assertEqual(p.returncode, 2)
        self.assertNotEqual(p.stderr, "")

    def test_a_missing_cards_key_exits_2(self):
        p = Base.run_batch(Base(), (), raw=json.dumps({"open_issues": []}))
        self.assertEqual(p.returncode, 2)
        self.assertNotEqual(p.stderr, "")

    def test_batch_size_zero_exits_2(self):
        p = Base.run_batch(Base(), (), batch_size=0)
        self.assertEqual(p.returncode, 2)
        self.assertNotEqual(p.stderr, "")

    def test_a_missing_open_issues_key_exits_2(self):
        p = Base.run_batch(Base(), (), raw=json.dumps({"cards": []}))
        self.assertEqual(p.returncode, 2)
        self.assertNotEqual(p.stderr, "")

    def test_open_issues_that_is_not_a_list_exits_2(self):
        p = Base.run_batch(Base(), (), raw=json.dumps({"cards": [], "open_issues": 12}))
        self.assertEqual(p.returncode, 2)
        self.assertNotEqual(p.stderr, "")

    def test_an_open_issues_item_that_is_neither_an_int_nor_a_number_object_exits_2(self):
        # Plain ints and {"number": N} objects are the two valid shapes; bools and the rest exit 2.
        for bad in ("12", {"num": 12}, True, 12.0, None):
            with self.subTest(bad=bad):
                p = Base.run_batch(Base(), (), raw=json.dumps({"cards": [], "open_issues": [bad]}))
                self.assertEqual(p.returncode, 2)
                self.assertNotEqual(p.stderr, "")

    def test_an_explicit_empty_open_issues_list_is_valid(self):
        out = Base.out(Base(), [], open_issues=[])
        self.assertEqual(out["reason"], "empty")
        self.assertEqual(out["batch"], [])

    def test_cards_that_is_not_a_list_exits_2(self):
        p = Base.run_batch(Base(), (), raw=json.dumps({"cards": "oops", "open_issues": []}))
        self.assertEqual(p.returncode, 2)
        self.assertNotEqual(p.stderr, "")

    def test_a_card_that_is_not_a_dict_exits_2(self):
        p = Base.run_batch(Base(), (), raw=json.dumps({"cards": [12], "open_issues": []}))
        self.assertEqual(p.returncode, 2)
        self.assertNotEqual(p.stderr, "")

    def test_a_card_without_a_number_exits_2(self):
        p = Base.run_batch(Base(), (), raw=json.dumps({"cards": [{"title": "x", "status": "Backlog", "labels": []}],
                                                       "open_issues": []}))
        self.assertEqual(p.returncode, 2)
        self.assertNotEqual(p.stderr, "")

    def test_a_card_with_a_non_int_number_exits_2(self):
        # `true` is bool; bool is an int subclass in Python and must be rejected too.
        for bad in ("12", 12.0, True, None):
            with self.subTest(bad=bad):
                p = Base.run_batch(Base(), (), raw=json.dumps({"cards": [{"number": bad, "title": "x"}],
                                                               "open_issues": []}))
                self.assertEqual(p.returncode, 2)
                self.assertNotEqual(p.stderr, "")

    def test_a_card_without_a_string_status_exits_2(self):
        # A missing or non-string `status` would fall into no bucket and read as a false `empty`.
        for bad in ({"number": 12, "title": "x"},                    # no status key
                    {"number": 12, "status": None, "title": "x"},    # null
                    {"number": 12, "status": 12, "title": "x"},       # int
                    {"number": 12, "status": True, "title": "x"},     # bool
                    {"number": 12, "status": ["Backlog"], "title": "x"}):  # list
            with self.subTest(bad=bad):
                p = Base.run_batch(Base(), (), raw=json.dumps({"cards": [bad], "open_issues": []}))
                self.assertEqual(p.returncode, 2)
                self.assertNotEqual(p.stderr, "")


class SkillDoc(unittest.TestCase):
    def test_skill_md_documents_autopilot(self):
        text = SKILL.read_text(encoding="utf-8")
        for needle in ("## 4b. Autopilot", "goal:<slug>", "super-board-autopilot-batch.py", "notified the operator"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
