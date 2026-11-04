"""Offline Sentry / PostHog for the super-collect evals.

seed.sh puts this folder on PYTHONPATH and SC_FIXTURES=<a copy of tests/fixtures/collect>
in the eval HOME's shell rc files, so every python3 the agent runs (collect_sentry.py,
collect_posthog.py, …) answers HTTP from those fixtures through fixture_http.Urlopen.
A URL with no fixture raises URLError: nothing reaches the network. Without SC_FIXTURES
this file does nothing.
"""
import os
import sys

_fx = os.environ.get("SC_FIXTURES")
if _fx and os.path.isfile(os.path.join(_fx, "fixture_http.py")):
    sys.path.insert(0, _fx)
    try:
        import urllib.request

        import fixture_http

        urllib.request.urlopen = fixture_http.Urlopen(_fx)
    finally:
        sys.path.remove(_fx)
