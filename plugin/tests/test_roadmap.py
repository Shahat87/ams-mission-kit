"""Tests for super-board-roadmap.py. Offline: --data fixtures + a PATH-stubbed `gh`.

Run directly:  python3 tests/test_roadmap.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "super-board-roadmap.py"
FIX = ROOT / "tests" / "fixtures" / "roadmap"

_spec = importlib.util.spec_from_file_location("super_board_roadmap", SCRIPT)
assert _spec is not None and _spec.loader is not None
rm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rm)

NOW = "2026-10-27T09:40:00Z"

STUB_PY = r'''
import json, os, sys, time
args = sys.argv[1:]
env = os.environ
stdin = ""
if "--body-file" in args:
    stdin = sys.stdin.buffer.read().decode("utf-8")
with open(env["STUB_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "stdin": stdin}) + "\n")
logged = [json.loads(l) for l in open(env["STUB_LOG"], encoding="utf-8").read().splitlines() if l.strip()]


def fail(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(1)


def out(text):
    sys.stdout.write(text)


if args[:2] == ["issue", "view"]:
    fields = args[args.index("--json") + 1]
    if fields == "isPinned":
        out(json.dumps({"isPinned": env.get("STUB_PINNED") == "1"}))
    elif fields == "comments":
        out('{"comments": []}')
    else:
        seen = sum(1 for c in logged if c["args"][:2] == ["issue", "view"] and "state,body" in c["args"])
        if env.get("STUB_VIEW_MISSING"):
            fail("GraphQL: Could not resolve to an Issue with the number of 5.")
        if env.get("STUB_VIEW_ERR"):
            fail(env["STUB_VIEW_ERR"])
        src = env["STUB_VIEW2"] if (seen >= 2 and env.get("STUB_VIEW2")) else env["STUB_VIEW"]
        out(open(src, encoding="utf-8").read())
elif args[:2] == ["issue", "list"]:
    out(open(env["STUB_LIST"], encoding="utf-8").read() if env.get("STUB_LIST") else "[]")
elif args[:2] == ["issue", "create"]:
    out("https://github.com/acme/widgets/issues/99\n")
elif args[:2] == ["issue", "pin"]:
    if env.get("STUB_PIN_FAIL"):
        fail("pin limit reached")
elif args[:1] == ["api"]:
    if "-X" in args:
        if args[args.index("-X") + 1] == "POST":
            posts = sum(1 for c in logged if "-X" in c["args"] and "POST" in c["args"])
            out(json.dumps({"number": 49 + posts}))
        else:
            out("{}")
    else:
        routes = json.load(open(env["STUB_ROUTES"], encoding="utf-8")) if env.get("STUB_ROUTES") else {}
        joined = " ".join(args)
        for key, body in routes.items():
            if key in joined:
                out(json.dumps(body))
                break
        else:
            out("[]")
'''


RUNNER_PY = r'''
import importlib.util, shutil, subprocess, sys
spec = importlib.util.spec_from_file_location("rm_under_test", @@SCRIPT@@)
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)
_orig_run, _orig_which = subprocess.run, shutil.which
def run(cmd, **kw):
    if cmd and cmd[0] == "gh-stub":
        cmd = [sys.executable, @@STUB@@, *cmd[1:]]
    return _orig_run(cmd, **kw)
subprocess.run = run
shutil.which = lambda n, *a, **k: "gh-stub" if n == "gh" else _orig_which(n, *a, **k)
sys.exit(rm.main(sys.argv[1:]))
'''


def issue(n: int, state: str, ms: int | None, status: str | None = None, title: str | None = None):
    return {"number": n, "title": title or f"Card {n}", "state": state, "milestone": ms}, (
        {"number": n, "title": title or f"Card {n}", "state": state.upper(),
         "milestone": ms, "status": status, "labels": []} if status else None)


def make_data(phases: list[tuple[str, str, list]], loose: list | None = None) -> dict:
    """phases: [(title, milestone_state, [(n, issue_state, status)])]."""
    data: dict = {"milestones": [], "issues": [], "items": [], "reasons": {}}
    for i, (title, mstate, cards) in enumerate(phases, 1):
        data["milestones"].append({"number": i, "title": title, "state": mstate,
                                   "html_url": f"https://github.com/acme/widgets/milestone/{i}"})
        for n, st, status in cards:
            a, b = issue(n, st, i, status)
            data["issues"].append(a)
            if b:
                data["items"].append(b)
    for n, st, status in loose or []:
        _, b = issue(n, st, None, status or "Backlog")
        data["items"].append(b)
    return data


def model_of(data: dict) -> dict:
    return rm.build_model(data)


class Env:
    """Temp dir with config, stub gh on PATH, halt-file override."""

    def __init__(self, tc: unittest.TestCase, config: dict | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="sb-roadmap-"))
        tc.addCleanup(shutil.rmtree, self.dir, True)
        self.log = self.dir / "gh.log"
        self.log.write_text("")
        self.view = self.dir / "view.json"
        self.halt = self.dir / "halt.json"  # absent = no halt
        stub = self.dir / "gh_stub.py"
        stub.write_text(STUB_PY, encoding="utf-8")
        # Run the script through a runner that redirects `gh` to the stub (a .cmd stub
        # would mangle `&` in URLs on Windows; the real gh.exe does not).
        self.runner = self.dir / "runner.py"
        self.runner.write_text(RUNNER_PY.replace("@@SCRIPT@@", repr(str(SCRIPT)))
                               .replace("@@STUB@@", repr(str(stub))), encoding="utf-8")
        self.cfg_path = self.dir / "config.json"
        cfg = config if config is not None else json.loads((FIX / "config.json").read_text())
        self.cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        self.data = self.dir / "data.json"
        self.routes = self.dir / "routes.json"
        self.routes.write_text("{}")
        self.list_file = self.dir / "list.json"

    def base_env(self, **env) -> dict:
        e = dict(os.environ)
        e.update({"STUB_LOG": str(self.log),
                  "STUB_VIEW": str(self.view), "SB_GITHUB_HALT_FILE": str(self.halt),
                  "STUB_ROUTES": str(self.routes)})
        e.update(env)
        return e

    def run_cm(self, plan: dict, **env):
        plan_path = self.dir / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        cmd = [sys.executable, str(self.runner), "create-milestones", "--config", str(self.cfg_path),
               "--plan", str(plan_path)]
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=self.base_env(**env))

    def mutations(self) -> list[list[str]]:
        return [c["args"] for c in self.calls() if "-X" in c["args"]]

    def run(self, verb: str, data: dict | None = None, extra: list[str] | None = None, **env):
        if data is not None:
            self.data.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        cmd = [sys.executable, str(self.runner), verb, "--config", str(self.cfg_path),
               "--now", NOW, "--wave", "4"]
        if data is not None:
            cmd += ["--data", str(self.data)]
        cmd += extra or []
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              env=self.base_env(**env))

    def calls(self) -> list[dict]:
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def verbs(self) -> list[str]:
        return [" ".join(c["args"][:2]) for c in self.calls()]

    def set_view(self, state: str, body: str) -> None:
        self.view.write_text(json.dumps({"state": state, "body": body}), encoding="utf-8")


class PercentAndPhases(unittest.TestCase):
    def test_1_floor_percent_and_active_phase(self):
        cards49 = [(n, "closed", "Done") for n in range(1, 50)] + [(n, "open", "Ready") for n in range(50, 54)]
        cards52 = [(n, "closed", "Done") for n in range(100, 152)] + [(200, "open", "Ready")]
        data = make_data([
            ("Alpha", "open", cards49),
            ("Beta", "open", cards52),
            ("Gamma", "open", [(300, "closed", "Done")]),
        ])
        m = model_of(data)
        alpha, beta, gamma = m["phases"]
        self.assertEqual((alpha["done"], alpha["total"], alpha["pct"]), (49, 53, 92))
        self.assertEqual(beta["pct"], 98)  # 52/53 must never round up to 100
        self.assertNotEqual(rm.tally([{"col": "Done"}] * 52 + [{"col": "Backlog"}])["pct"], 100)
        self.assertEqual(rm.tally([{"col": "Done"}] * 52 + [{"col": "Backlog"}])["pct"], 98)
        self.assertEqual(m["active"], 0)
        self.assertEqual([p["klass"] for p in m["phases"]], ["active", "todo", "done"])
        self.assertEqual(rm.bar(92), "█████████░")
        self.assertEqual(rm.bar(60), "██████░░░░")
        # earlier finished phases are done, active is the first below 100
        data2 = make_data([("A", "open", [(1, "closed", "Done")]),
                           ("B", "open", [(2, "open", "Building")]),
                           ("C", "open", [(3, "open", "Ready")])])
        self.assertEqual([p["klass"] for p in model_of(data2)["phases"]], ["done", "active", "todo"])

    def test_2_closed_counts_done_and_ready_is_backlog(self):
        data = make_data([("A", "open", [
            (1, "closed", "Review"),   # closed, Status not Done -> done
            (2, "open", "Ready"),      # Ready -> Backlog
            (3, "open", "Backlog"),
            (4, "open", None),         # not on board -> Backlog
            (5, "open", "Building"),
        ])])
        p = model_of(data)["phases"][0]
        self.assertEqual(p["counts"], {"Done": 1, "Building": 1, "QA": 0, "Review": 0,
                                       "Blocked": 0, "Backlog": 3})

    def test_3_no_phase_row_without_node(self):
        data = make_data([("A", "open", [(1, "open", "Ready")])], loose=[(50, "open", "Ready"), (51, "closed", None)])
        out = rm.render_block(model_of(data), NOW, 1)
        self.assertIn("⚪ No phase", out)
        self.assertEqual(out.count('P1["'), 1)
        self.assertNotIn("P2[", out)
        self.assertNotIn("No phase<br/>", out)
        table_end = out.index("⚪ No phase")
        note = out.index("Percent counts issues only.")
        self.assertLess(table_end, note)
        self.assertLess(note, out.index("### Now"))

    def test_4_mermaid_escaping(self):
        data = make_data([("Say \"hi\" <b> | x", "open", [(1, "open", "Ready")])])
        out = rm.render_block(model_of(data), NOW, None)
        node = [l for l in out.splitlines() if l.strip().startswith('P1["')][0]
        label = node.strip()[len('P1["'):-2]
        self.assertNotIn('"', label)
        for bad in "<>|":
            self.assertNotIn(bad, label.replace("<br/>", ""))
        row = [l for l in out.splitlines() if l.startswith("| [Say")][0]
        self.assertNotIn("<b>", row)
        self.assertEqual(row.count("\\|"), 1)
        self.assertNotIn("by wave", out)  # wave omitted when absent

    def test_render_sample_layout(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "render", "--config", str(FIX / "config.json"),
             "--data", str(FIX / "data.json"), "--now", NOW, "--wave", "4"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        self.assertTrue(out.startswith(rm.START))
        self.assertIn("updated 2026-10-27 09:40 GMT by wave 4", out)
        self.assertIn("flowchart LR", out)
        self.assertIn("classDef done", out)
        self.assertIn("`█████████░` 92%", out)
        self.assertIn("🛡 dependency gate", out)
        self.assertLess(out.index(rm.END), out.index(rm.YOURS))


class Markers(unittest.TestCase):
    def setUp(self):
        self.block = rm.render_block(model_of(make_data([("A", "open", [(1, "open", "Ready")])])), NOW, 1)

    def test_5a_outside_text_kept_byte_for_byte(self):
        before = "intro \r\n  line  \n\n"
        after = "\n\n✍️ mine\r\ntrailing  \t\n"
        body = before + rm.START + "\nold stuff\n" + rm.END + after
        new, changed = rm.splice_body(body, self.block)
        self.assertTrue(changed)
        self.assertTrue(new.startswith(before + rm.START))
        self.assertTrue(new.endswith(rm.END + after))
        self.assertIn(self.block, new)
        self.assertNotIn("old stuff", new)

    def test_5b_no_markers_block_prepended(self):
        body = "the operator's own notes\n"
        new, changed = rm.splice_body(body, self.block)
        self.assertTrue(changed)
        self.assertTrue(new.startswith(rm.START))
        self.assertTrue(new.endswith("\n" + body))
        self.assertIn(rm.YOURS, new)
        self.assertLess(new.index(rm.YOURS), new.index("the operator's own notes"))


class SyncFlow(unittest.TestCase):
    DATA = make_data([("A", "open", [(1, "open", "Ready")])])

    def test_6_timestamp_only_change_makes_no_edit(self):
        env = Env(self, {"project": {"owner": "acme", "number": 1},
                         "repo": {"remote": "https://github.com/acme/widgets"}, "roadmap": {"issue": 7}})
        old_block = rm.render_block(model_of(self.DATA), "2026-10-26T01:00:00Z", 3)
        env.set_view("OPEN", "top\n" + old_block + "\nbottom")
        p = env.run("sync", self.DATA, STUB_PINNED="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(env.verbs(), ["issue view", "issue view"])  # body read + pin check
        # and a real change does edit, keeping the outside text
        data2 = make_data([("A", "open", [(1, "closed", "Done"), (2, "open", "Ready")])])
        p = env.run("sync", data2, STUB_PINNED="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(env.verbs(), ["issue view", "issue view", "issue view", "issue view",
                                       "issue edit", "issue view"])
        edit = next(c for c in env.calls() if c["args"][:2] == ["issue", "edit"])
        self.assertTrue(edit["stdin"].startswith("top\n" + rm.START))
        self.assertTrue(edit["stdin"].endswith(rm.END + "\nbottom"))

    def _creates(self, cfg: dict, **setup):
        env = Env(self, cfg)
        if setup.get("closed"):
            env.set_view("CLOSED", "old")
        if setup.get("missing"):
            return env, env.run("sync", self.DATA, STUB_VIEW_MISSING="1")
        return env, env.run("sync", self.DATA)

    def _assert_created(self, env, proc, expected_keys):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        v = env.verbs()
        self.assertIn("issue create", v)
        self.assertIn("issue pin", v)
        self.assertNotIn("issue edit", v)
        create = next(c for c in env.calls() if c["args"][:2] == ["issue", "create"])
        self.assertIn(rm.START, create["stdin"])
        pin = next(c for c in env.calls() if c["args"][:2] == ["issue", "pin"])
        self.assertEqual(pin["args"][2], "99")
        self.assertEqual(create["args"][create["args"].index("--repo") + 1], "acme/widgets")
        raw = env.cfg_path.read_text()
        cfg = json.loads(raw)
        self.assertEqual(cfg["roadmap"]["issue"], 99)
        for k, val in expected_keys.items():
            self.assertEqual(cfg[k], val)
        self.assertIn('\n  "project"', raw)  # 2-space indent preserved

    def test_7a_no_roadmap_key_creates_and_pins(self):
        cfg = {"project": {"owner": "acme", "number": 1}, "max_workers": 3,
               "repo": {"remote": "git@github.com:acme/widgets.git"}}
        env, p = self._creates(cfg)
        self._assert_created(env, p, {"max_workers": 3, "project": {"owner": "acme", "number": 1}})

    def test_7b_closed_issue_creates_new(self):
        cfg = {"project": {"owner": "acme", "number": 1}, "roadmap": {"issue": 5, "x": 1},
               "repo": {"remote": "ssh://git@github.com/acme/widgets.git/"}}
        env, p = self._creates(cfg, closed=True)
        self._assert_created(env, p, {})
        self.assertEqual(json.loads(env.cfg_path.read_text())["roadmap"]["x"], 1)

    def test_7c_deleted_issue_creates_new(self):
        cfg = {"project": {"owner": "acme", "number": 1}, "roadmap": {"issue": 5},
               "repo": {"remote": "https://github.com/acme/widgets"}}
        env, p = self._creates(cfg, missing=True)
        self._assert_created(env, p, {})

    def test_8_check_fails_exit_79_no_writes(self):
        env = Env(self)
        env.halt.write_text("{}")
        p = env.run("sync", self.DATA)
        self.assertEqual(p.returncode, 79, p.stderr)
        self.assertEqual(env.calls(), [])
        self.assertEqual(json.loads(env.cfg_path.read_text()), json.loads((FIX / "config.json").read_text()))

    def test_9_no_milestones_exit_3_no_writes(self):
        env = Env(self)
        empty = {"milestones": [], "issues": [], "items": [], "reasons": {}}
        p = env.run("sync", empty)
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertEqual(env.calls(), [])
        p = env.run("render", empty)
        self.assertEqual(p.returncode, 3)
        self.assertEqual(p.stdout, "")


class Misc(unittest.TestCase):
    def test_exit_codes_args_and_config(self):
        run = lambda *a: subprocess.run([sys.executable, str(SCRIPT), *a], capture_output=True, text=True).returncode
        self.assertEqual(run("render"), 64)
        self.assertEqual(run("bogus"), 64)
        self.assertEqual(run("render", "--config", str(FIX / "nope.json")), 66)

    def test_slug_normalization(self):
        for s in ("https://github.com/o/r", "https://github.com/o/r.git", "https://github.com/o/r/",
                  "git@github.com:o/r.git", "ssh://git@github.com/o/r.git", "ssh://git@github.com:22/o/r"):
            self.assertEqual(rm.normalize_slug(s), "o/r", s)


TITLE_URL_CFG = {"project": {"owner": "acme", "number": 1}, "roadmap": {"issue": 7},
                 "repo": {"remote": "https://github.com/acme/widgets"}}
EXISTING_BODY = "top\n" + rm.START + "\nold\n" + rm.END + "\nbottom"


class CountingRules(unittest.TestCase):
    def test_percent_floors_not_rounds(self):
        self.assertEqual(rm.tally([{"col": "Done"}] * 2 + [{"col": "Backlog"}])["pct"], 66)
        self.assertEqual(rm.tally([{"col": "Done"}] * 199 + [{"col": "Backlog"}])["pct"], 99)

    def test_open_issue_with_status_done_counts_done(self):
        p = model_of(make_data([("A", "open", [(1, "open", "Done"), (2, "open", "Ready")])]))["phases"][0]
        self.assertEqual(p["counts"]["Done"], 1)
        self.assertEqual(p["pct"], 50)

    def test_closed_milestone_below_100_is_finished_not_active(self):
        m = model_of(make_data([("A", "closed", [(1, "open", "Ready")]),
                                ("B", "open", [(2, "open", "Ready")])]))
        self.assertEqual([p["klass"] for p in m["phases"]], ["done", "active"])
        self.assertEqual(m["active"], 1)

    def test_empty_open_milestone_is_todo_and_skipped(self):
        data = make_data([("A", "open", []), ("B", "open", [(1, "open", "Ready")])])
        m = model_of(data)
        self.assertEqual(m["active"], 1)
        self.assertEqual([p["klass"] for p in m["phases"]], ["todo", "active"])
        self.assertIn("0% · 0/0", rm.render_block(m, NOW, None))
        only_empty = model_of(make_data([("A", "closed", [(1, "closed", None)]), ("B", "open", [])]))
        out = rm.render_block(only_empty, NOW, None)
        self.assertNotIn("All 2 phases done", out)
        self.assertIn("No active phase", out)

    def test_foreign_repo_board_item_ignored(self):
        def node(n, repo):
            return {"content": {"number": n, "title": f"T{n}", "state": "OPEN",
                                "repository": {"nameWithOwner": repo}, "milestone": {"number": 1}},
                    "status": None}
        items = rm.items_from_nodes([node(1, "Acme/Widgets"), node(2, "other/repo")], "acme/widgets")
        self.assertEqual([i["number"] for i in items], [1])

    def test_markdown_title_cannot_forge_link(self):
        data = make_data([("A", "open", [(1, "open", "Ready", )])])
        data["issues"][0]["title"] = "x](https://evil) [y](z) *b* `c` _d_"
        data["milestones"][0]["title"] = "ph](https://evil)"
        out = rm.render_block(model_of(data), NOW, None)
        md_part = out.split("```mermaid")[0] + out.split("```")[2]  # Mermaid keeps its own escaping
        self.assertNotIn("](https://evil)", md_part)
        self.assertIn("x\\]\\(https://evil\\)", out)

    def test_incomplete_pagination_is_an_error(self):
        full = [{"number": i} for i in range(100)]
        orig = rm.gh
        rm.gh = lambda *a, **k: json.dumps(full)
        try:
            with self.assertRaises(rm.GhError):
                rm.rest_list("repos/a/b/issues?state=all")
        finally:
            rm.gh = orig
        page = {"data": {"repositoryOwner": {"projectV2": {"items": {
            "pageInfo": {"hasNextPage": True, "endCursor": None}, "nodes": []}}}}}
        self.assertTrue(rm.paginate_items(lambda after: page)[1])


class SyncHardening(unittest.TestCase):
    def test_transport_error_creates_nothing(self):
        env = Env(self, TITLE_URL_CFG)
        p = env.run("sync", make_data([("A", "open", [(1, "open", "Ready")])]),
                    STUB_VIEW_ERR="dial tcp: lookup api.github.com: could not resolve host")
        self.assertEqual(p.returncode, 67, p.stderr)
        self.assertNotIn("issue create", env.verbs())
        self.assertNotIn("issue list", env.verbs())

    def test_existing_exact_title_issue_reused(self):
        cfg = {"project": {"owner": "acme", "number": 1}, "repo": {"remote": "https://github.com/acme/widgets"}}
        env = Env(self, cfg)
        env.list_file.write_text(json.dumps([
            {"number": 30, "title": rm.TITLE}, {"number": 12, "title": rm.TITLE},
            {"number": 8, "title": rm.TITLE + " old"}]), encoding="utf-8")
        env.set_view("OPEN", "notes")
        p = env.run("sync", make_data([("A", "open", [(1, "open", "Ready")])]), STUB_LIST=str(env.list_file))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("issue create", env.verbs())
        self.assertEqual(json.loads(env.cfg_path.read_text())["roadmap"]["issue"], 12)
        edit = next(c for c in env.calls() if c["args"][:2] == ["issue", "edit"])
        self.assertEqual(edit["args"][2], "12")
        self.assertTrue(edit["stdin"].endswith("notes") or "notes" in edit["stdin"])
        lst = next(c for c in env.calls() if c["args"][:2] == ["issue", "list"])
        self.assertIn("in:title", " ".join(lst["args"]))
        self.assertIn("--limit 100", " ".join(lst["args"]))

    def test_numeric_string_issue_is_valid(self):
        cfg = dict(TITLE_URL_CFG, roadmap={"issue": "7"})
        env = Env(self, cfg)
        env.set_view("OPEN", EXISTING_BODY)
        p = env.run("sync", make_data([("A", "open", [(1, "open", "Ready")])]), STUB_PINNED="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("issue create", env.verbs())
        self.assertEqual(env.calls()[0]["args"][2], "7")

    def test_body_changed_between_read_and_edit_skips_edit(self):
        env = Env(self, TITLE_URL_CFG)
        env.set_view("OPEN", EXISTING_BODY)
        v2 = env.dir / "view2.json"
        v2.write_text(json.dumps({"state": "OPEN", "body": EXISTING_BODY + "\nnew owner text"}), encoding="utf-8")
        p = env.run("sync", make_data([("A", "open", [(1, "open", "Ready")])]), STUB_VIEW2=str(v2))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("issue edit", env.verbs())
        self.assertIn("changed while syncing", p.stderr)

    def test_pin_checked_every_sync_and_failure_is_soft(self):
        data = make_data([("A", "open", [(1, "open", "Ready")])])
        env = Env(self, TITLE_URL_CFG)
        env.set_view("OPEN", EXISTING_BODY)
        p = env.run("sync", data, STUB_PINNED="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("issue pin", env.verbs())
        env2 = Env(self, TITLE_URL_CFG)
        env2.set_view("OPEN", EXISTING_BODY)
        p = env2.run("sync", data)
        self.assertIn("issue pin", env2.verbs())
        env3 = Env(self, TITLE_URL_CFG)
        env3.set_view("OPEN", EXISTING_BODY)
        p = env3.run("sync", data, STUB_PIN_FAIL="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("could not pin", p.stderr)

    def test_config_write_is_atomic_and_oserror_gives_67(self):
        d = Path(tempfile.mkdtemp(prefix="sb-cfg-"))
        self.addCleanup(shutil.rmtree, d, True)
        cfg = d / "config.json"
        cfg.write_text('{\n  "a": 1\n}\n', encoding="utf-8")
        before = cfg.read_bytes()
        from unittest import mock
        with mock.patch.object(rm.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                rm.write_config_issue(cfg, 5)
        self.assertEqual(cfg.read_bytes(), before)
        self.assertEqual([p.name for p in d.iterdir()], ["config.json"])  # no temp left behind
        rm.write_config_issue(cfg, 5)
        self.assertEqual(json.loads(cfg.read_text()), {"a": 1, "roadmap": {"issue": 5}})
        # end to end: a failing save after create -> number on stderr, exit 67
        import contextlib
        import io
        cfg2 = d / "c2.json"
        cfg2.write_text(json.dumps({"repo": {"remote": "https://github.com/acme/widgets"}}), encoding="utf-8")
        data = d / "data.json"
        data.write_text(json.dumps(make_data([("A", "open", [(1, "open", "Ready")])])), encoding="utf-8")

        def fake_gh(*a, **k):
            if a[:2] == ("issue", "list"):
                return "[]"
            if a[:2] == ("issue", "create"):
                return "https://github.com/acme/widgets/issues/99\n"
            return json.dumps({"isPinned": True})
        err = io.StringIO()
        with mock.patch.object(rm, "run_check", return_value=0), mock.patch.object(rm, "gh", fake_gh), \
                mock.patch.object(rm, "write_config_issue", side_effect=OSError("denied")), \
                contextlib.redirect_stderr(err):
            rc = rm.main(["sync", "--config", str(cfg2), "--data", str(data)])
        self.assertEqual(rc, 67)
        self.assertIn("#99", err.getvalue())

    def test_check_exit_codes(self):
        from unittest import mock
        def fake(code):
            return mock.patch.object(rm.subprocess, "run",
                                     return_value=subprocess.CompletedProcess([], code, "", "x"))
        with fake(79):
            self.assertEqual(rm.run_check(), 79)
        with fake(1):
            self.assertEqual(rm.run_check(), 67)
        with fake(0):
            self.assertEqual(rm.run_check(), 0)
        with mock.patch.object(rm.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(rm.run_check(), 67)
        with mock.patch.object(rm.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("x", 120)) as run:
            self.assertEqual(rm.run_check(), 67)
            self.assertEqual(run.call_args.kwargs["timeout"], 120)
        with mock.patch.object(rm.Path, "is_file", return_value=False):
            self.assertEqual(rm.run_check(), 67)

    def test_gh_timeout_gives_67(self):
        from unittest import mock
        seen = {}

        def boom(cmd, **kw):
            seen.update(kw)
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        d = Path(tempfile.mkdtemp(prefix="sb-to-"))
        self.addCleanup(shutil.rmtree, d, True)
        cfg = d / "config.json"
        cfg.write_text(json.dumps({"repo": {"remote": "https://github.com/acme/widgets"}}), encoding="utf-8")
        with mock.patch.object(rm.shutil, "which", return_value="gh"), \
                mock.patch.object(rm.subprocess, "run", side_effect=boom):
            self.assertEqual(rm.main(["render", "--config", str(cfg)]), 67)
        self.assertEqual(seen["timeout"], 120)


class CreateMilestones(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.env.routes.write_text(json.dumps({"milestones?state=all": [
            {"number": 1, "title": "Phase A", "state": "open"},
            {"number": 2, "title": "Old", "state": "closed"}]}), encoding="utf-8")

    def test_end_to_end_exact_calls(self):
        p = self.env.run_cm({"milestones": [{"title": "Phase A", "issues": [3, 4]},
                                            {"title": "Phase B", "description": "d", "issues": [5]}]})
        self.assertEqual(p.returncode, 0, p.stderr)
        m = self.env.mutations()
        self.assertEqual(m, [
            ["api", "repos/acme/widgets/issues/3", "-X", "PATCH", "-F", "milestone=1"],
            ["api", "repos/acme/widgets/issues/4", "-X", "PATCH", "-F", "milestone=1"],
            ["api", "repos/acme/widgets/milestones", "-X", "POST", "-f", "title=Phase B", "-f", "description=d"],
            ["api", "repos/acme/widgets/issues/5", "-X", "PATCH", "-F", "milestone=50"],
        ])

    def test_bad_plans_exit_64_no_mutation(self):
        bad = [
            {"milestones": [{"title": "X", "issues": [1.9]}]},
            {"milestones": [{"title": "X", "issues": [True]}]},
            {"milestones": [{"title": "X", "issues": [0]}]},
            {"milestones": [{"title": "X", "issues": [5]}, {"title": "Y", "issues": [5]}]},
            {"milestones": [{"title": "Old", "issues": [5]}]},  # matches a CLOSED milestone
            {"milestones": [{"title": "New", "issues": [5]}, {"title": "Old", "issues": [6]}]},
            {"milestones": []},
            {"milestones": [{"title": "", "issues": []}]},
        ]
        for plan in bad:
            p = self.env.run_cm(plan)
            self.assertEqual(p.returncode, 64, (plan, p.stderr))
            self.assertEqual(self.env.mutations(), [], plan)


class ReadBudget(unittest.TestCase):
    def test_seven_milestones_one_issues_call_and_reasons_capped(self):
        slug_cfg = {"project": {"owner": "acme", "number": 1}, "repo": {"remote": "https://github.com/acme/widgets"}}
        env = Env(self, slug_cfg)
        mss = [{"number": i, "title": f"P{i}", "state": "open", "html_url": ""} for i in range(1, 8)]
        issues = [{"number": n, "title": f"I{n}", "state": "open", "milestone": {"number": 1}} for n in range(1, 13)]

        def node(n, repo="acme/widgets"):
            return {"content": {"number": n, "title": f"I{n}", "state": "OPEN",
                                "repository": {"nameWithOwner": repo}, "milestone": {"number": 1}},
                    "status": {"name": "Blocked"}}
        gql = {"data": {"repositoryOwner": {"projectV2": {"items": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [node(n) for n in range(1, 13)] + [node(500, "other/repo")]}}}}}
        env.routes.write_text(json.dumps({"milestones?state=all": mss, "issues?state=all": issues,
                                          "graphql": gql}), encoding="utf-8")
        p = env.run("render", None)
        self.assertEqual(p.returncode, 0, p.stderr)
        calls = [" ".join(c["args"]) for c in env.calls()]
        self.assertEqual(sum("issues?state=all" in c for c in calls), 1)
        self.assertEqual(sum("issues?state=all&milestone=*" in c for c in calls), 1)
        self.assertEqual(sum("--json comments" in c for c in calls), 10)
        self.assertNotIn("#500", p.stdout)


class MissingFields(unittest.TestCase):
    def _data(self, items):
        data = make_data([("A", "open", [(1, "open", "Ready")])])
        data["items"] = items
        return data

    @staticmethod
    def _item(n, status, agent="", skills="", labels=()):
        return {"number": n, "title": f"I{n}", "state": "OPEN", "milestone": 1, "status": status,
                "agent": agent, "skills": skills, "labels": list(labels)}

    def test_section_lists_lane_cards_missing_fields(self):
        items = [
            self._item(1, "Building", "Sonnet · super-build", "tdd", ["Bug"]),   # complete (label case-insensitive)
            self._item(2, "Review", "", "x", ["feature"]),
            self._item(3, "QA", "", "", []),
            self._item(4, "Blocked", "Opus · super-review", "", ["qa"]),
            self._item(5, "Ready"),                                              # not a lane: ignored
            self._item(6, "Backlog"),
            self._item(7, "Review", "Opus · super-review", "x", ["Refactor"]),  # refactor is a type: no label gap
        ]
        out = rm.render_block(model_of(self._data(items)), NOW, 1)
        self.assertNotIn("#7 Review missing", out)
        self.assertIn("### ⚠️ Cards missing fields", out)
        self.assertIn("- #2 Review missing: agent", out)
        self.assertIn("- #3 QA missing: agent,skills,label", out)
        self.assertIn("- #4 Blocked missing: skills", out)
        self.assertNotIn("#1 Building missing", out)
        self.assertNotIn("#5 ", out.split("Cards missing fields")[1])
        self.assertNotIn("#6 ", out.split("Cards missing fields")[1])
        self.assertLess(out.index("### Now"), out.index("Cards missing fields"))
        self.assertLess(out.index("Cards missing fields"), out.index(rm.END))

    def test_section_absent_when_clean(self):
        items = [self._item(1, "Building", "Sonnet · super-build", "tdd", ["feature"])]
        out = rm.render_block(model_of(self._data(items)), NOW, 1)
        self.assertNotIn("Cards missing fields", out)

    def test_items_from_nodes_reads_agent_skills_and_query_asks_for_them(self):
        node = {"content": {"number": 7, "title": "t", "state": "OPEN", "repository": {"nameWithOwner": "acme/widgets"},
                            "milestone": {"number": 1}, "labels": {"nodes": [{"name": "bug"}]}},
                "status": {"name": "Review"},
                "agent": {"name": "Opus · super-review"},
                "skills": {"text": "code-review"}}
        (item,) = rm.items_from_nodes([node], "acme/widgets")
        self.assertEqual((item["agent"], item["skills"], item["labels"]), ("Opus · super-review", "code-review", ["bug"]))
        self.assertIn("ProjectV2ItemFieldTextValue", rm.ITEMS_QUERY)
        for alias in ('status: fieldValueByName(name:"Status")', 'agent: fieldValueByName(name:"Agent")',
                      'skills: fieldValueByName(name:"Skills")'):
            self.assertIn(alias, rm.ITEMS_QUERY)
        self.assertNotIn("fieldValues(", rm.ITEMS_QUERY)


if __name__ == "__main__":
    unittest.main()
