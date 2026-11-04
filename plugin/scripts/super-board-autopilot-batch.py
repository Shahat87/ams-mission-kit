#!/usr/bin/env python3
"""super-board-autopilot-batch.py — pick the next mission-control autopilot batch for a goal.

Pure function of the cards on stdin and the flags; no network, no side effects.
mission-control (SKILL.md §4b) pipes the goal's cards here once per cycle and moves
the returned batch of Backlog cards to Ready.

    super-board-autopilot-batch.py --goal <slug> --batch-size N < input.json

Input, stdin, one JSON object:
    {"cards": [{"number": 12, "title": "...", "status": "Backlog",
                "labels": ["goal:checkout", "bug"], "body": "..."}],
     "open_issues": [12, 15, 40]}

`open_issues` holds ALL the repo's open issue numbers — not only this goal's, because a blocker
can belong to another goal: plain integers, or the `{"number": N}` objects that
`gh issue list --state open --limit 1000 --json number` prints.

A goal card is a card whose labels include exactly `goal:<slug>`; the match ignores
case. A candidate is a goal card with status `Backlog`. The body's `## Blocked by`
section lists blockers as `- #N — why` bullets, or says exactly `- None.`; a candidate
is eligible only when none of its blocker numbers is in `open_issues` (a blocker that
is itself still a candidate counts as open too). Eligible cards are ordered by issue
number, ascending; `batch` is the first batch_size of them.

Stdout, one JSON object, exit 0:
    {"batch": [12], "eligible": [12, 15], "waiting": [], "in_flight": [], "reason": "ok"}

Reasons:
- "ok"                  the batch is non-empty.
- "empty"               no goal cards in Backlog and none in Ready/Building/QA/Review.
- "in-flight"           goal cards are still in the lanes and no Backlog card is
                        eligible; the run is not done, do not draft new cards.
- "all-blocked-by-deps" there are candidates, but none is eligible.
- "blocked"             a goal card has status `Blocked` and waits on the operator;
                        its numbers are in `waiting` and the batch is empty.

Bad input — unreadable JSON on stdin, a missing or non-list `cards` key, a card that is not a
JSON object or has no integer `number` (bool rejected) or no string `status`, a missing or
non-list `open_issues` key,
an `open_issues` item that is neither an integer nor a `{"number": N}` object (bools rejected),
or --batch-size below 1 — prints a message to stderr and exits 2.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

LANES = ("Ready", "Building", "QA", "Review")
BLOCKED_BY = re.compile(r"^##\s+Blocked by\s*$", re.I)
BULLET = re.compile(r"^-\s*#(\d+)")


def parse_blockers(body: str) -> list[int]:
    """Blocker numbers in the body's `## Blocked by` section; `- None.` means none."""
    blockers: list[int] = []
    in_section = False
    for line in (body or "").splitlines():
        text = line.strip()
        if BLOCKED_BY.match(text):
            in_section = True
            continue
        if not in_section:
            continue
        if text.startswith("#"):
            break
        if text == "- None.":
            return []
        m = BULLET.match(text)
        if m:
            blockers.append(int(m.group(1)))
    return blockers


def is_goal_card(card: dict, slug: str) -> bool:
    """The card's labels include exactly `goal:<slug>`; the match ignores case.

    `gh ... --json labels` returns label objects ("[{\"name\": \"goal:x\", ...}]"); plain
    name strings work too. Anything else is ignored.
    """
    want = f"goal:{slug}".lower()
    for label in card.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if isinstance(name, str) and name.strip().lower() == want:
            return True
    return False


def open_issue_number(item) -> int | None:
    """The issue number in one `open_issues` item; None when the item has neither accepted shape.

    `gh issue list --state open --limit 1000 --json number` returns objects ("number": 12);
    plain integers work too. bools are rejected even though bool is an int subclass.
    """
    if isinstance(item, int) and not isinstance(item, bool):
        return item
    if isinstance(item, dict):
        number = item.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Pick the next autopilot batch for a goal (mission-control §4b).")
    ap.add_argument("--goal", required=True, help="goal slug; matches the label goal:<slug>")
    ap.add_argument("--batch-size", type=int, default=3, help="max cards to promote per cycle (default 3)")
    args = ap.parse_args()
    if args.batch_size < 1:
        print("super-board-autopilot-batch.py: --batch-size must be 1 or more", file=sys.stderr)
        return 2
    try:
        data = json.load(sys.stdin)
    except (ValueError, UnicodeDecodeError) as exc:
        print(f"super-board-autopilot-batch.py: unreadable JSON on stdin: {exc}", file=sys.stderr)
        return 2
    if not isinstance(data, dict) or "cards" not in data:
        print("super-board-autopilot-batch.py: input must be a JSON object with a `cards` key", file=sys.stderr)
        return 2
    cards = data["cards"]
    if not isinstance(cards, list):
        print("super-board-autopilot-batch.py: `cards` must be a list", file=sys.stderr)
        return 2
    if any(not isinstance(c, dict) for c in cards):
        print("super-board-autopilot-batch.py: every element of `cards` must be a JSON object", file=sys.stderr)
        return 2
    for c in cards:
        if "number" not in c or isinstance(c["number"], bool) or not isinstance(c["number"], int):
            print("super-board-autopilot-batch.py: every card needs an integer `number` (bool rejected)", file=sys.stderr)
            return 2
        if not isinstance(c.get("status"), str):
            print("super-board-autopilot-batch.py: every card needs a string `status`", file=sys.stderr)
            return 2
    if not isinstance(data.get("open_issues"), list):
        print("super-board-autopilot-batch.py: input must include an `open_issues` list "
              "(gh issue list --state open --limit 1000 --json number)", file=sys.stderr)
        return 2

    open_issues: set[int] = set()
    for item in data["open_issues"]:
        number = open_issue_number(item)
        if number is None:
            print("super-board-autopilot-batch.py: every element of `open_issues` must be an integer "
                  "or a {\"number\": N} object (gh issue list --state open --limit 1000 --json number)",
                  file=sys.stderr)
            return 2
        open_issues.add(number)
    goal_cards = [c for c in cards if is_goal_card(c, args.goal)]
    blocked = sorted(int(c.get("number", 0)) for c in goal_cards if c.get("status") == "Blocked")
    in_flight = sorted(int(c.get("number", 0)) for c in goal_cards if c.get("status") in LANES)
    candidates = [c for c in goal_cards if c.get("status") == "Backlog"]
    eligible = [int(c.get("number", 0)) for c in sorted(candidates, key=lambda c: int(c.get("number", 0)))
                if all(b not in open_issues for b in parse_blockers(c.get("body") or ""))]

    batch = eligible[:args.batch_size]
    if blocked:
        reason, batch = "blocked", []
    elif batch:
        reason = "ok"
    elif not candidates and not in_flight:
        reason = "empty"
    elif in_flight:
        reason = "in-flight"
    else:
        reason = "all-blocked-by-deps"

    print(json.dumps({"batch": batch, "eligible": eligible, "waiting": blocked,
                      "in_flight": in_flight, "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
