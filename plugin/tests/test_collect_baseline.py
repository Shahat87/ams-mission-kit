"""Baseline regression suite for super-collect's fetchers, driven by canned payloads.

Pins today's behaviour of collect_sentry.py, collect_posthog.py and collect_prs.py end to end
(CLI `main()` included) against the fixtures in tests/fixtures/collect/ — the same payloads the
behavioural evals in evals/collect-* serve. No network: urlopen is the fixture router and `gh`
is a fake runner.

Tests named `test_known_bug_*` assert the behaviour SKILL.md promises where the code does not
deliver it yet; they are expectedFailure so the suite stays green, and turn into an
"unexpected success" (a failure) the day the bug is fixed — then drop the decorator.

    python3 tests/test_collect_baseline.py
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, "fixtures", "collect")
sys.path.insert(0, os.path.join(HERE, "..", "skills", "super-collect", "scripts"))
sys.path.insert(0, FX)
import collect_common as cc  # noqa: E402
import collect_posthog as ph  # noqa: E402
import collect_prs as prs  # noqa: E402
import collect_sentry as sn  # noqa: E402
import fixture_http  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 11, 12, tzinfo=UTC)
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith(("SENTRY_", "POSTHOG_"))}
ENV = dict(CLEAN_ENV, SENTRY_AUTH_TOKEN="eval-sentry-token", POSTHOG_PERSONAL_API_KEY="phx_eval")

SENTRY = {"org": "acme", "project": "web", "host": "https://sentry.example.test", "min_events": 2}
POSTHOG = {"host": "https://eu.i.posthog.com", "project_id": 123}


def cfg_file(d, collect, remote="https://github.com/example/shop.git"):
    p = os.path.join(d, "cfg.json")
    with open(p, "w") as f:
        json.dump({"project": {"owner": "example", "number": 1, "title": "Shop"},
                   "repo": {"remote": remote}, "collect": collect}, f)
    return p


def fixture_text(name, json_=False):
    with open(os.path.join(FX, name)) as f:
        text = fixture_http.render(f.read(), NOW)
    return json.loads(text) if json_ else text


class Fixed(dt.datetime):
    """datetime whose now() is NOW, so the CLI's window maths is deterministic."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


@contextlib.contextmanager
def cli(module, collect):
    """Run module.main() against the fixtures with a pinned clock; yields (run, urlopen)."""
    opener = fixture_http.Urlopen(FX, NOW)
    with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, ENV, clear=True), \
            mock.patch.object(module.urllib.request, "urlopen", opener), \
            mock.patch.object(cc.dt, "datetime", Fixed):  # cc.dt is the datetime module: pins every now()
        path = cfg_file(d, collect)

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = module.main(["--config", path, *argv])
            text = buf.getvalue()
            return rc, (json.loads(text) if text.strip().startswith("{") else text)
        yield run, opener


class Window(unittest.TestCase):
    def test_default_window_is_14_days(self):
        self.assertEqual(cc.window_days({}), 14)
        self.assertEqual(cc.window_days({"collect": {}}), 14)
        self.assertEqual(cc.since(None, cc.window_days({}), now=NOW), NOW - dt.timedelta(days=14))

    def test_config_window_days_overrides_default(self):
        self.assertEqual(cc.window_days({"collect": {"window_days": 30}}), 30)

    def test_since_forms(self):
        self.assertEqual(cc.since("30d", now=NOW), NOW - dt.timedelta(days=30))
        self.assertEqual(cc.since("2w", now=NOW), NOW - dt.timedelta(weeks=2))
        self.assertEqual(cc.since("36h", now=NOW), NOW - dt.timedelta(hours=36))
        self.assertEqual(cc.since("2026-09-01", now=NOW), dt.datetime(2026, 9, 1, tzinfo=UTC))
        self.assertEqual(cc.since("2026-09-01T08:00:00Z", now=NOW), dt.datetime(2026, 9, 1, 8, tzinfo=UTC))

    def test_sentry_cli_default_window(self):
        with cli(sn, {"sentry": SENTRY}) as (run, opener):
            rc, out = run("list")
        self.assertEqual(rc, 0)
        self.assertEqual(out["since"], (NOW - dt.timedelta(days=14)).isoformat())
        self.assertIn("statsPeriod=14d", opener.calls[-1].full_url)

    def test_sentry_cli_since_override(self):
        with cli(sn, {"sentry": SENTRY, "window_days": 7}) as (run, opener):
            rc, out = run("list", "--since", "30d")
        self.assertEqual(out["since"], (NOW - dt.timedelta(days=30)).isoformat())
        self.assertIn("statsPeriod=30d", opener.calls[-1].full_url)
        with cli(sn, {"sentry": SENTRY, "window_days": 7}) as (run, opener):
            rc, out = run("list")
        self.assertEqual(out["since"], (NOW - dt.timedelta(days=7)).isoformat())

    def test_posthog_cli_since_override_lands_in_hogql(self):
        with cli(ph, {"posthog": POSTHOG}) as (run, opener):
            rc, out = run("list", "--since", "2026-09-01")
        self.assertEqual(rc, 0)
        self.assertEqual(out["since"], "2026-09-01T00:00:00+00:00")
        queries = [json.loads(c.data)["query"]["query"] for c in opener.calls]
        self.assertTrue(all("toDateTime('2026-09-01 00:00:00')" in q for q in queries), queries)

    def test_prs_cli_window_becomes_merged_qualifier(self):
        calls = []

        def fake_graphql(query, variables, run=None):
            calls.append(variables)
            return fixture_text("prs-graphql.json", json_=True)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cc.dt, "datetime", Fixed), \
                mock.patch.object(prs, "gh_graphql", fake_graphql), \
                contextlib.redirect_stdout(io.StringIO()) as buf:
            rc = prs.main(["--config", cfg_file(d, {}), "list"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0]["q"], "repo:example/shop is:pr is:merged merged:>=2026-09-27")
        out = json.loads(buf.getvalue())
        self.assertEqual(out["since"], (NOW - dt.timedelta(days=14)).isoformat())
        self.assertEqual(out["superReviewReports"], 5)


class SentryFixture(unittest.TestCase):
    def test_list_normalises_fixture(self):
        with cli(sn, {"sentry": SENTRY}) as (run, opener):
            rc, out = run("list")
        self.assertEqual(out["source"], "sentry")
        c = {x["id"]: x for x in out["candidates"]}
        self.assertEqual(list(c), ["4411", "4490"])  # 4502 is under min_events 2; sorted by score
        a = c["4411"]
        self.assertEqual(a["fingerprint"], "err|sentry|4411")
        self.assertEqual((a["events"], a["users"], a["events24h"]), (37, 14, 6))
        self.assertTrue(a["stillHappening"])
        self.assertEqual(a["shortId"], "WEB-4411")
        self.assertFalse(c["4490"]["stillHappening"])
        self.assertEqual(set(a), {"fingerprint", "score", "id", "shortId", "title", "culprit", "level", "unhandled",
                                  "substatus", "events", "users", "events24h", "firstSeen", "lastSeen",
                                  "stillHappening", "permalink"})
        self.assertNotIn("eval-sentry-token", json.dumps(out))

    def test_event_strips_pii(self):
        with cli(sn, {"sentry": SENTRY}) as (run, _):
            rc, ev = run("event", "4411")
        self.assertEqual(ev["release"], "web@1.4.2")
        self.assertNotIn("user.email", ev["tags"])
        self.assertEqual(ev["exceptions"][0]["frames"][0], {"file": "src/checkout/summary.ts", "line": 12, "function": "buildSummary"})
        self.assertNotIn("jane@example.com", json.dumps(ev))
        with cli(sn, {"sentry": SENTRY}) as (run, _):
            rc, tags = run("tags", "4411")
        self.assertNotIn("user.email", tags)

    def test_check_recurring_vs_quiet_since_fix(self):
        fix_merged = (NOW - dt.timedelta(days=10)).strftime("%Y-%m-%d")
        with cli(sn, {"sentry": SENTRY}) as (run, _):
            _, live = run("check", "4411", "--after", fix_merged)
            _, quiet = run("check", "4490", "--after", fix_merged)
        self.assertTrue(live["seenAfter"])
        self.assertFalse(quiet["seenAfter"])
        self.assertIsNone(quiet["seenAfterRelease"])  # unresolved in Sentry: no release to compare

    def test_missing_fixture_is_unavailable_not_empty(self):
        with cli(sn, {"sentry": dict(SENTRY, project="mobile")}) as (run, _):
            with mock.patch.object(fixture_http.Urlopen, "answer",
                                   side_effect=sn.urllib.error.HTTPError("u", 404, "x", {}, io.BytesIO(b""))):
                rc, out = run("list")
        self.assertEqual(rc, 2)
        self.assertEqual(out["status"], "unavailable")

    def test_unreachable_host_reports_unavailable(self):
        """SKILL.md:36-37: a fetcher that cannot reach its source (DNS failure, connection
        refused, timeout) prints status unavailable and exits 2, never a traceback."""
        for err in (sn.urllib.error.URLError("nodename nor servname provided"), TimeoutError("timed out")):
            with self.subTest(err=err), cli(sn, {"sentry": SENTRY}) as (run, _):
                with mock.patch.object(fixture_http.Urlopen, "answer", side_effect=err):
                    rc, out = run("list")
                self.assertEqual(rc, 2)
                self.assertEqual(out["status"], "unavailable")


class PostHogFixture(unittest.TestCase):
    def test_list_finds_nothing_over_thresholds(self):
        with cli(ph, {"posthog": POSTHOG}) as (run, opener):
            rc, out = run("list")
        self.assertEqual(rc, 0)
        self.assertEqual(out["candidates"], [])  # vitals under limits; group signals filtered server-side
        self.assertEqual(opener.calls[0].full_url, "https://eu.posthog.com/api/projects/123/query/")

    def test_events_inventory_is_the_tracking_gap_input(self):
        with cli(ph, {"posthog": POSTHOG}) as (run, _):
            rc, out = run("events")
        self.assertEqual(rc, 0)
        self.assertEqual(set(out), {"source", "since", "silentSignals", "events"})
        names = [e["event"] for e in out["events"]]
        self.assertIn("signup_completed", names)
        self.assertFalse([n for n in names if "receipt" in n])  # the gap the eval expects a ticket for
        self.assertEqual(sorted(out["silentSignals"]), ["dead_click", "survey"])
        self.assertEqual(out["events"][0], {"event": "$pageview", "count": 4210, "users": 388,
                                            "lastSeen": "2026-10-11T11:00:00Z"})

    def test_check_reports_stopped_and_issue_status(self):
        with cli(ph, {"posthog": POSTHOG}) as (run, _):
            rc, out = run("check", "exception", "0193-issue", "--after", "2026-10-01")
        self.assertTrue(out["stopped"])
        self.assertEqual(out["issueStatus"], "active")

    def test_unreachable_host_reports_unavailable(self):
        """Same contract as Sentry."""
        for err in (ph.urllib.error.URLError("connection refused"), TimeoutError("timed out")):
            with self.subTest(err=err), cli(ph, {"posthog": POSTHOG}) as (run, _):
                with mock.patch.object(fixture_http.Urlopen, "answer", side_effect=err):
                    rc, out = run("list")
                self.assertEqual(rc, 2)
                self.assertEqual(out["status"], "unavailable")


class PRsFixture(unittest.TestCase):
    def prs(self):
        page = fixture_text("prs-graphql.json", json_=True)
        run = lambda cmd, **kw: mock.Mock(returncode=0, stdout=json.dumps(page), stderr="")
        return prs.list_prs("example/shop", NOW - dt.timedelta(days=14), run=run)

    def test_normalises_fixture(self):
        out, truncated = self.prs()
        self.assertFalse(truncated)
        self.assertEqual([p["number"] for p in out], [21, 24, 26])
        p21 = out[0]
        self.assertEqual(sum(c["superReview"] for c in p21["comments"]), 2)
        self.assertTrue(p21["comments"][0]["author"]["bot"])
        self.assertEqual(p21["threads"][0]["path"], "src/reports/monthly.ts")
        self.assertTrue(out[1]["reviews"][0]["author"]["bot"])  # coderabbitai
        self.assertEqual(set(p21), {"number", "title", "url", "mergedAt", "author", "comments", "reviews", "threads"})

    def test_script_does_not_cluster(self):
        """Clustering (2+ PRs, one root cause) is the agent's job, not the script's: the JSON is
        per-PR only. Behaviour is pinned by evals/collect-prs-pattern."""
        out, _ = self.prs()
        self.assertTrue(all(set(p) == set(out[0]) for p in out))

    def test_max_prs_truncates(self):
        page = fixture_text("prs-graphql.json", json_=True)
        run = lambda cmd, **kw: mock.Mock(returncode=0, stdout=json.dumps(page), stderr="")
        out, truncated = prs.list_prs("example/shop", NOW, max_prs=2, run=run)
        self.assertEqual(len(out), 2)
        self.assertTrue(truncated)


class FixtureRouter(unittest.TestCase):
    def test_unknown_url_never_reaches_network(self):
        import urllib.request
        with self.assertRaises(urllib.error.URLError):
            fixture_http.Urlopen(FX, NOW)(urllib.request.Request("https://example.com/anything"))

    def test_ago_renders_relative_to_now(self):
        self.assertEqual(fixture_http.render('"{{ago:2h}}"', NOW), '"2026-10-11T10:00:00Z"')


if __name__ == "__main__":
    unittest.main(verbosity=1)
