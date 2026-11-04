#!/usr/bin/env python3
"""Layer B merge-policy hardening: case, removed lines, renames, default validation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

POLICY = Path(__file__).resolve().parents[1] / "scripts/super-board-merge-policy.py"
ALWAYS = {"auth": {"labels": [], "paths": ["**/auth/**"], "keywords": ["is_admin"]}}


def classify(paths, diff="", policy=None):
    cfg = {"base_branch": "staging",
           "merge_policy": dict({"default": "auto", "always_human": ALWAYS}, **(policy or {}))}
    meta = {"labels": [], "body": "", "additions": 1, "deletions": 0, "changedFiles": len(paths),
            "files": [{"path": p, "additions": 1, "deletions": 0} for p in paths]}
    with tempfile.TemporaryDirectory() as tmp:
        files = {}
        for name, value in (("c.json", cfg), ("m.json", meta)):
            files[name] = Path(tmp, name)
            files[name].write_text(json.dumps(value), encoding="utf-8")
        diff_path = Path(tmp, "d.diff")
        diff_path.write_text(diff, encoding="utf-8")
        run = subprocess.run([sys.executable, str(POLICY), "--config", str(files["c.json"]),
                              "--meta", str(files["m.json"]), "--diff", str(diff_path)],
                             capture_output=True, text=True, check=True)
    return json.loads(run.stdout)["human"]


def categories(human):
    return sorted(h["category"] for h in human)


class LayerBPolicyTests(unittest.TestCase):
    def test_baseline_plain_change_is_not_human(self):
        self.assertEqual(classify(["src/core/x.py"]), [])

    def test_path_globs_ignore_case(self):
        self.assertEqual(categories(classify(["src/Auth/login.py"])), ["auth"])
        self.assertEqual(categories(classify(["SRC/AUTH/login.py"])), ["auth"])

    def test_pattern_case_is_ignored_too(self):
        human = classify(["src/auth/x.py"], policy={"always_human": {"auth": {"paths": ["**/AUTH/**"]}}})
        self.assertEqual(categories(human), ["auth"])

    def test_keyword_on_removed_line_counts(self):
        diff = "--- a/src/core/x.py\n+++ b/src/core/x.py\n@@ -1 +1 @@\n-    if not user.is_admin:\n+    pass\n"
        self.assertEqual(categories(classify(["src/core/x.py"], diff)), ["auth"])

    def test_file_headers_are_not_content(self):
        diff = "--- a/is_admin.txt\n+++ b/is_admin.txt\n@@ -1 +1 @@\n-x\n+y\n"
        self.assertEqual(classify(["src/core/x.py"], diff), [])

    def test_rename_out_of_a_protected_path_is_classified(self):
        diff = ("diff --git a/src/auth/x.py b/src/core/x.py\nsimilarity index 100%\n"
                "rename from src/auth/x.py\nrename to src/core/x.py\n")
        human = classify(["src/core/x.py"], diff)
        self.assertEqual(categories(human), ["auth"])
        self.assertIn("src/auth/x.py", human[0]["why"])

    def test_copy_from_protected_path_is_classified(self):
        diff = "copy from src/auth/x.py\ncopy to src/core/x.py\n"
        self.assertEqual(categories(classify(["src/core/x.py"], diff)), ["auth"])

    def test_default_is_validated_and_lowercased(self):
        self.assertEqual(categories(classify(["a.py"], policy={"default": "HUMAN"})), ["default"])
        self.assertEqual(classify(["a.py"], policy={"default": "Auto"}), [])
        bad = classify(["a.py"], policy={"default": "maybe"})
        self.assertEqual(categories(bad), ["default"])
        self.assertIn("treated as human", bad[0]["why"])


if __name__ == "__main__":
    unittest.main()
