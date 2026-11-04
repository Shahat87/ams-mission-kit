"""Offline Sentry / PostHog for super-collect: a urlopen stand-in that answers from this folder.

Used two ways, so the regression tests and the behavioural evals read the same payloads:
  - tests/test_collect_baseline.py patches `urllib.request.urlopen` with `Urlopen(dir, now)`.
  - evals/_stubs/py/sitecustomize.py installs it in every python3 the eval agent runs, when
    SC_FIXTURES names a copy of this folder.

Timestamps in the JSON are written `{{ago:<n><m|h|d>}}` and rendered relative to `now`, so a
"seen 2 hours ago" error stays 2 hours old whenever the fixture is read. Any URL with no
fixture raises URLError — nothing here ever reaches the network. Stdlib only.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import urllib.error
import urllib.parse

UTC = dt.timezone.utc
_AGO = re.compile(r"\{\{ago:(\d+)([mhd])\}\}")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def render(text, now=None):
    now = now or dt.datetime.now(UTC)
    return _AGO.sub(lambda m: (now - dt.timedelta(**{_UNITS[m.group(2)]: int(m.group(1))}))
                    .strftime("%Y-%m-%dT%H:%M:%SZ"), text)


def load(folder, name, now=None):
    with open(os.path.join(folder, name)) as f:
        return json.loads(render(f.read(), now))


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Urlopen:
    """Callable like urllib.request.urlopen(req, timeout=…). Records every request in .calls."""

    def __init__(self, folder, now=None):
        self.folder, self.now, self.calls = folder, now, []

    def _file(self, name, url):
        path = os.path.join(self.folder, name)
        if not os.path.exists(path):
            raise urllib.error.HTTPError(url, 404, "no fixture " + name, {}, io.BytesIO(b"{}"))
        return load(self.folder, name, self.now)

    def answer(self, req):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        path = urllib.parse.urlsplit(url).path
        # PostHog
        if re.search(r"/api/projects/[^/]+/query/$", path):
            query = json.loads(req.data)["query"]["query"]
            for route in self._file("posthog-hogql.json", url)["routes"]:
                if route.get("equals") == query or (route.get("match") and all(s in query for s in route["match"])):
                    return {"results": route["rows"]}
            return {"results": []}
        m = re.search(r"/error_tracking/issues/([^/]+)/$", path)
        if m:
            name = f"posthog-issue-{m.group(1)}.json"
            return self._file(name, url) if os.path.exists(os.path.join(self.folder, name)) else {"status": "active"}
        # Sentry
        if re.search(r"/api/0/projects/[^/]+/[^/]+/$", path):
            return self._file("sentry-project.json", url)
        m = re.search(r"/issues/(\d+)/events/[a-z]+/$", path)
        if m:
            return self._file(f"sentry-event-{m.group(1)}.json", url)
        m = re.search(r"/issues/(\d+)/tags/$", path)
        if m:
            return self._file(f"sentry-tags-{m.group(1)}.json", url)
        m = re.search(r"/api/0/organizations/[^/]+/issues/(\d+)/$", path)
        if m:
            return self._file(f"sentry-issue-{m.group(1)}.json", url)
        if re.search(r"/api/0/organizations/[^/]+/issues/$", path):
            return self._file("sentry-issues.json", url)
        m = re.search(r"/api/0/organizations/[^/]+/releases/([^/]+)/$", path)
        if m:
            return self._file(f"sentry-release-{urllib.parse.unquote(m.group(1))}.json", url)
        if re.search(r"/api/0/organizations/[^/]+/$", path):
            return {"slug": path.rstrip("/").rsplit("/", 1)[-1]}
        raise urllib.error.URLError(f"offline fixture: no route for {url}")

    def __call__(self, req, timeout=None, **_):
        self.calls.append(req)
        return _Resp(json.dumps(self.answer(req)).encode())
