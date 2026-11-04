#!/usr/bin/env python3
"""Stub `gh` for test_events.py (issue #66).

Serves read-only fixture JSON, refuses every write verb, and appends each
invocation as one JSON line to $SB_EV_GH_LOG so tests can audit the calls.
Environment:
  SB_EV_FIXTURES  directory holding board.json, comments.json,
                  pulls-comments.json, correlation.json and permissions/<login>.json
  SB_EV_GH_LOG    argv log file (JSON lines)
  SB_EV_FAIL      when set, every call fails (helper exit-79 shape)
Routing mirrors the in-process FakeGH in test_events.py.
"""
import json
import os
import pathlib
import re
import sys

argv = sys.argv[1:]
log_path = pathlib.Path(os.environ["SB_EV_GH_LOG"])
log_path.parent.mkdir(parents=True, exist_ok=True)
with log_path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\n")


def refuse(msg):
    sys.stderr.write("stub gh: %s\n" % msg)
    sys.exit(1)


def serve(name):
    path = pathlib.Path(os.environ["SB_EV_FIXTURES"]) / name
    if not path.exists():
        refuse("no fixture for this call: %s" % name)
    sys.stdout.write(path.read_text(encoding="utf-8"))
    sys.exit(0)


if os.environ.get("SB_EV_FAIL"):
    refuse("forced failure")
if not argv or argv[0] != "api":
    refuse("only 'api' is served")
joined = " ".join(argv)

if "graphql" in argv:
    query = next((a[len("query="):] for a in argv if a.startswith("query=")), "")
    if not query:
        refuse("graphql without query=")
    if re.search(r"\bmutation\b", query, re.I):
        refuse("mutation refused")
    if "projectV2" in query:
        serve("board.json")
    if "pullRequest(" in query:
        serve("correlation.json")
    refuse("unrecognized graphql call")

if any(a in ("-X", "--method") or a.startswith(("-f", "-F", "--field", "--raw-field", "--input"))
       for a in argv[1:]):
    refuse("write verb refused")

m = re.search(r"/collaborators/([^/]+)/permission", joined)
if m:
    serve("permissions/%s.json" % m.group(1))
if "/issues/comments?" in joined:
    serve("comments.json")
if "/pulls/comments?" in joined:
    serve("pulls-comments.json")
refuse("no fixture for this read: %s" % joined[:120])
