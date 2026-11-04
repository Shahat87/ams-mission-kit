#!/usr/bin/env python3
"""Fake `docker` for tests. Appends argv as one JSON line to $DOCKER_STUB_LOG and answers from the JSON file
$DOCKER_STUB_SCRIPT: a list of rules {"match": [tokens that must all appear in argv], "rc": 0, "stdout": "",
"stderr": "", "rcs": [...], "stdouts": [...], "cp_to_last": "<file>"}. The first matching rule wins; rcs/stdouts are
consumed one per matching call (the last entry repeats). cp_to_last copies that file to the last argv entry (a fake
`docker cp`). Unmatched calls return rc 0 with no output."""
import json
import os
import shutil
import sys

argv = sys.argv[1:]
log = os.environ.get("DOCKER_STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")
script = os.environ.get("DOCKER_STUB_SCRIPT")
rc, out, err = 0, "", ""


def pick(seq, default, n):
    return seq[min(n, len(seq) - 1)] if seq else default


if script and os.path.exists(script):
    with open(script, encoding="utf-8") as fh:
        rules = json.load(fh)
    state_path = script + ".state"
    state = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    for i, rule in enumerate(rules):
        if all(t in argv for t in rule.get("match", [])):
            n = state.get(str(i), 0)
            state[str(i)] = n + 1
            rc = pick(rule.get("rcs"), rule.get("rc", 0), n)
            out = pick(rule.get("stdouts"), rule.get("stdout", ""), n)
            err = rule.get("stderr", "")
            if rule.get("cp_to_last"):
                shutil.copyfile(rule["cp_to_last"], argv[-1])
            with open(state_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            break
sys.stdout.write(out)
sys.stderr.write(err)
sys.exit(rc)
