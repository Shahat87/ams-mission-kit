#!/usr/bin/env python3
"""Map a finished tool run to a failure kind (spec §6.3).

Order: missing CLI, then text signatures (they win even when files changed,
because some CLIs exit 0 after an error), then exit code, then changes.
"""
from __future__ import annotations

import json
import re

SIGNATURES = [
    ("auth:env", [r"no api key found for"]),
    ("quota", [r"usage limit", r"insufficient_quota", r"quota", r"rate limit", r"subscribe to a .* plan",
               r"out of credits?", r"credit balance"]),
    ("auth", [r"\b401\b", r"unauthori[sz]ed", r"not logged in", r"please (run .*)?log ?in", r"authenticat(e|ion) (failed|required)",
              r"cryptunprotectdata"]),
    ("model_error", [r"\b400 bad request\b", r"model .* (does not exist|not found)", r"unknown model", r"invalid model"]),
]
ASK = [r"shall i proceed", r"please confirm", r"do you want me to", r"should i (continue|proceed)", r"awaiting (your )?approval"]


def _event_types(obj):
    msg = obj.get("msg")
    return [str(v) for v in (obj.get("type"), obj.get("event"), msg.get("type") if isinstance(msg, dict) else None) if v]


def failure_text(stdout, stderr, final):
    """Text to scan for failure signatures: stderr, the final message, error-typed JSON events and the
    last 10 plain stdout lines. Model prose inside other JSON events is never scanned."""
    keep, plain = [], []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            plain.append(line)
        elif any(("error" in t.lower() or "fail" in t.lower()) for t in _event_types(obj)):
            keep.append(line)
    return chr(10).join([stderr or "", final or "", *keep, *plain[-10:]])


def classify(text, exit_code, changed, read_only=False):
    if exit_code == 127:
        return "missing"
    low = (text or "").lower()
    for kind, patterns in SIGNATURES:
        if any(re.search(p, low) for p in patterns):
            return kind
    if exit_code not in (0, None):
        return "model_error"
    if read_only:
        return "ok"
    if not changed:
        return "ask_confirm" if any(re.search(p, low) for p in ASK) else "no_changes"
    return "ok"
