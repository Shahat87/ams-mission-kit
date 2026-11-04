#!/usr/bin/env python3
"""Path classifier for external-tool diffs (spec §5.1 matcher, §6.4 verify steps 2-3)."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lane_paths", ROOT / "scripts/super-board-lane-paths.py")
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

PROTECTED = [".github/", ".claude/", ".git", ".husky/", "conftest.py", "package.json", "*.lock", "jest.config.*", "scripts/"]
TESTS = ["tests/**", "**/test_*.py", "**/*.test.*"]


class NormaliseTests(unittest.TestCase):
    def test_backslash_case_and_trailing_dots(self):
        self.assertEqual(P.normalise(".GitHub\\Workflows\\ci.yml. "), ".github/workflows/ci.yml")

    def test_short_names_and_streams_are_rejected(self):
        for bad in ["GITHUB~1/ci.yml", "src/a.py:secret", "src/a.py::$DATA", "../outside.py", ".. /x.py", "a/.../b.py", "a/.. /b", "/abs/x.py", "C:/x.py"]:
            with self.subTest(bad=bad):
                self.assertIsNone(P.normalise(bad))


class ProtectedTests(unittest.TestCase):
    def check(self, path):
        return P.protected_hit(path, PROTECTED)

    def test_dir_prefix_any_case(self):
        self.assertEqual(self.check(".GITHUB/workflows/x.yml"), ".github/")

    def test_bare_name_matches_at_any_depth(self):
        self.assertEqual(self.check("pkg/sub/conftest.py"), "conftest.py")
        self.assertEqual(self.check("vendor/lib/.git/config"), ".git")
        self.assertEqual(self.check("web/.husky/pre-push"), ".husky/")

    def test_glob_on_basename(self):
        self.assertEqual(self.check("apps/web/yarn.lock"), "*.lock")
        self.assertEqual(self.check("jest.config.ts"), "jest.config.*")

    def test_ordinary_source_is_free(self):
        self.assertIsNone(self.check("billing/fee.py"))
        self.assertIsNone(self.check("src/github_client.py"))
        self.assertIsNone(self.check("docs/scripts.md"))

    def test_unparseable_path_counts_as_protected(self):
        self.assertEqual(self.check("GITHUB~1/x"), "<unsafe path>")


class TestGlobTests(unittest.TestCase):
    def test_test_files(self):
        for ok in ["tests/test_kwd.py", "pkg/test_fee.py", "web/a.test.ts"]:
            self.assertTrue(P.is_test_file(ok, TESTS), ok)

    def test_source_is_not_test(self):
        for no in ["kwd.py", "billing/fee.py", "testing.md"]:
            self.assertFalse(P.is_test_file(no, TESTS), no)


class ChangeTests(unittest.TestCase):
    def test_classify_reports_old_and_new_paths_and_modes(self):
        changes = [
            {"status": "R", "old": ".github/ci.yml", "new": "ci.yml", "old_mode": "100644", "new_mode": "100644"},
            {"status": "A", "old": None, "new": "link", "old_mode": None, "new_mode": "120000"},
            {"status": "M", "old": "run.sh", "new": "run.sh", "old_mode": "100644", "new_mode": "100755"},
            {"status": "A", "old": None, "new": "sub", "old_mode": None, "new_mode": "160000"},
            {"status": "M", "old": "kwd.py", "new": "kwd.py", "old_mode": "100644", "new_mode": "100644"},
        ]
        problems = P.classify(changes, PROTECTED, TESTS, lane="build", allowed=set())
        self.assertIn(".github/ci.yml: protected (.github/)", problems)
        self.assertIn("link: symlink", problems)
        self.assertIn("run.sh: exec-bit change", problems)
        self.assertIn("sub: submodule", problems)
        self.assertFalse(any(p.startswith("kwd.py") for p in problems))

    def test_allowed_exact_path_is_not_a_problem(self):
        changes = [{"status": "M", "old": "package.json", "new": "package.json", "old_mode": "100644", "new_mode": "100644"}]
        self.assertEqual(P.classify(changes, PROTECTED, TESTS, lane="build", allowed={"package.json"}), [])

    def test_qa_lane_allows_only_test_files(self):
        changes = [
            {"status": "M", "old": "kwd.py", "new": "kwd.py", "old_mode": "100644", "new_mode": "100644"},
            {"status": "A", "old": None, "new": "tests/test_nan.py", "old_mode": None, "new_mode": "100644"},
        ]
        self.assertEqual(P.classify(changes, PROTECTED, TESTS, lane="qa", allowed=set()), ["kwd.py: not a test file (qa lane)"])

    def test_qa_lane_protected_test_dir_still_protected(self):
        changes = [{"status": "A", "old": None, "new": "tests/conftest.py", "old_mode": None, "new_mode": "100644"}]
        self.assertEqual(P.classify(changes, PROTECTED, TESTS, lane="qa", allowed=set()), ["tests/conftest.py: protected (conftest.py)"])

    def test_rename_into_protected_path_is_reported(self):
        changes = [{"status": "R", "old": "docs/ci-example.yml", "new": ".github/workflows/evil.yml", "old_mode": "100644", "new_mode": "100644"}]
        self.assertEqual(P.classify(changes, PROTECTED, TESTS, lane="build", allowed=set()),
                         [".github/workflows/evil.yml: protected (.github/)"])

    def test_rename_out_of_protected_path_still_reported(self):
        changes = [{"status": "R", "old": ".github/x", "new": "y", "old_mode": "100644", "new_mode": "100644"}]
        self.assertEqual(P.classify(changes, PROTECTED, TESTS, lane="build", allowed=set()),
                         [".github/x: protected (.github/)"])


class DockerRejectTests(unittest.TestCase):
    """Plan 1b A3: a hard-coded reject list for docker mode, whatever pilot.protected_paths says."""
    SAMPLES = {
        ".claude/**": ".claude/settings.json", ".github/**": ".github/workflows/x.yml", ".husky/**": ".husky/pre-commit",
        ".githooks/**": ".githooks/pre-push", ".vscode/**": ".vscode/tasks.json", ".gitattributes": ".gitattributes",
        ".gitmodules": ".gitmodules", ".lfsconfig": ".lfsconfig", ".npmrc": ".npmrc", ".yarnrc*": ".yarnrc.yml",
        "pip.conf": "pip.conf", "package.json": "package.json",
        ".mcp.json": ".mcp.json", "CLAUDE.md": "CLAUDE.md", "AGENTS.md": "AGENTS.md", ".envrc": ".envrc",
        ".pre-commit-config.yaml": ".pre-commit-config.yaml", ".nvmrc": ".nvmrc", ".python-version": ".python-version",
        ".tool-versions": ".tool-versions",
        # fix round 2: hooks, dependency hooks and editor / container config that runs on the host
        ".pnpmfile.cjs": ".pnpmfile.cjs", "lefthook.yml": "lefthook.yml", "lefthook.yaml": "lefthook.yaml",
        ".lefthook.yml": ".lefthook.yml", ".lefthook.yaml": ".lefthook.yaml", ".lintstagedrc*": ".lintstagedrc.json", "lint-staged.config.*": "lint-staged.config.mjs",
        ".devcontainer/**": ".devcontainer/devcontainer.json", ".idea/**": ".idea/workspace.xml",
        # plan 1c T4 (spec 15.2 step 1): executable or launchable extensions, every one rejected
        "*.com": "x.com",
        "*.exe": "x.exe",
        "*.bat": "x.bat",
        "*.cmd": "x.cmd",
        "*.vbs": "x.vbs",
        "*.vbe": "x.vbe",
        "*.jse": "x.jse",
        "*.wsf": "x.wsf",
        "*.wsh": "x.wsh",
        "*.msc": "x.msc",
        "*.dll": "x.dll",
        "*.hta": "x.hta",
        "*.cpl": "x.cpl",
        "*.pif": "x.pif",
        "*.reg": "x.reg",
        "*.url": "x.url",
        "*.lnk": "x.lnk",
        "*.scr": "x.scr",
        "*.msi": "x.msi",
    }

    @staticmethod
    def change(path, status="M"):
        return [{"status": status, "old": None if status == "A" else path, "new": path, "old_mode": "100644", "new_mode": "100644"}]

    def classify(self, path, allowed=(), docker_mode=True, lane="build", status="M"):
        return P.classify(self.change(path, status), [], [], lane, set(allowed), docker_mode=docker_mode)

    def test_every_glob_has_a_sample(self):
        self.assertEqual(set(self.SAMPLES), set(P.DOCKER_REJECT))

    def test_each_glob_is_rejected_in_docker_mode_only(self):
        for glob, path in self.SAMPLES.items():
            with self.subTest(glob):
                self.assertEqual(len(self.classify(path)), 1, path)
                self.assertIn(f"({glob})", self.classify(path)[0])
                self.assertEqual(self.classify(path, docker_mode=False), [])

    def test_matches_at_any_depth_and_any_case(self):
        for path in ("web/.husky/pre-commit", "a/b/.github/workflows/ci.yml", "packages/app/package.json", ".HUSKY/pre-commit",
                     "sub/.NPMRC", "x/.vscode/launch.json", "a/.yarnrc", "pkg/CLAUDE.md", "a/b/agents.md",
                     "svc/.envrc", "x/.nvmrc", "web/.pnpmfile.cjs", "a/.lintstagedrc", "a/lint-staged.config.js",
                     "pkg/.devcontainer/Dockerfile", "x/.idea/runConfigurations/t.xml", "svc/LEFTHOOK.yml"):
            with self.subTest(path):
                self.assertEqual(len(self.classify(path)), 1, path)

    def test_executable_extensions_are_rejected_at_any_depth_and_any_case(self):
        for ext in ['com', 'exe', 'bat', 'cmd', 'vbs', 'vbe', 'jse', 'wsf', 'wsh', 'msc', 'dll', 'hta', 'cpl', 'pif', 'reg', 'url', 'lnk', 'scr', 'msi']:
            for path in ("x." + ext, "a/b/Run." + ext.upper(), "x." + ext + ". "):
                with self.subTest(path):
                    self.assertEqual(len(self.classify(path, status="A")), 1, path)
        self.assertEqual(self.classify("tools/setup.CMD", status="A", docker_mode=False), [])

    def test_script_source_extensions_are_rejected_only_at_the_repo_root(self):
        # .js/.ps1/.psm1 are normal source in subfolders; at the root a bare-name lookup (git.js) could pick them up.
        for path in ("git.js", "GH.PS1", "x.psm1", "x.js. "):
            with self.subTest(path):
                self.assertEqual(len(self.classify(path, status="A")), 1, path)
        for path in ("src/app.js", "web/src/index.JS", "scripts/build.ps1", "tools/mod.psm1"):
            with self.subTest(path):
                self.assertEqual(self.classify(path, status="A"), [])
        self.assertEqual(P.DOCKER_REJECT_ROOT, ["*.js", "*.ps1", "*.psm1"])

    def test_a_lookalike_extension_is_free(self):
        for path in ("src/app.jsx", "src/app.json", "docs/join.url.md", "src/cmd.py", "src/x.exe.txt", "lib/dll_notes.md"):
            with self.subTest(path):
                self.assertEqual(self.classify(path, status="A"), [])

    def test_allow_path_lifts_it_by_exact_path(self):
        self.assertEqual(self.classify(".husky/pre-commit", allowed=[".husky/pre-commit"]), [])
        self.assertEqual(len(self.classify(".husky/pre-push", allowed=[".husky/pre-commit"])), 1)
        self.assertEqual(self.classify("package.json", allowed=["package.json"]), [])

    def test_any_package_json_change_needs_allow_path(self):
        # the simple safe rule: not only a `scripts` edit, any change (a dependency swap is the same risk)
        self.assertEqual(len(self.classify("package.json")), 1)
        self.assertEqual(len(self.classify("apps/web/package.json", status="A")), 1)

    def test_ordinary_files_are_free(self):
        for path in ("src/package_json.py", "docs/github.md", "kwd.py", "src/huskies.py", "tests/test_a.py"):
            with self.subTest(path):
                self.assertEqual(self.classify(path), [])

    def test_symlink_and_mode_rules_are_not_liftable(self):
        link = [{"status": "A", "old": None, "new": "l", "old_mode": None, "new_mode": "120000"}]
        self.assertEqual(P.classify(link, [], [], "build", {"l"}, docker_mode=True), ["l: symlink"])

    def test_default_is_unchanged_for_callers_that_do_not_pass_the_flag(self):
        self.assertEqual(P.classify(self.change(".husky/pre-commit"), [], [], "build", set()), [])

    def test_a_new_executable_file_is_rejected_in_docker_mode_and_cannot_be_lifted(self):
        new = [{"status": "A", "old": None, "new": "tools/run_me", "old_mode": None, "new_mode": "100755"}]
        self.assertEqual(P.classify(new, [], [], "build", set(), docker_mode=True), ["tools/run_me: exec-bit (a new executable file)"])
        self.assertEqual(P.classify(new, [], [], "build", {"tools/run_me"}, docker_mode=True),
                         ["tools/run_me: exec-bit (a new executable file)"])
        self.assertEqual(P.classify(new, [], [], "build", set()), [])                    # only docker mode adds the rule

    def test_an_added_plain_file_and_an_unchanged_executable_are_free(self):
        plain = [{"status": "A", "old": None, "new": "tools/run_me", "old_mode": None, "new_mode": "100644"}]
        self.assertEqual(P.classify(plain, [], [], "build", set(), docker_mode=True), [])
        edited = [{"status": "M", "old": "tools/run_me", "new": "tools/run_me", "old_mode": "100755", "new_mode": "100755"}]
        self.assertEqual(P.classify(edited, [], [], "build", set(), docker_mode=True), [])
        deleted = [{"status": "D", "old": "tools/run_me", "new": None, "old_mode": "100755", "new_mode": None}]
        self.assertEqual(P.classify(deleted, [], [], "build", set(), docker_mode=True), [])


if __name__ == "__main__":
    unittest.main()
