#!/usr/bin/env python3
"""Child env is built from an allowlist; profile is a temp dir seeded only with the tool's own files."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lane_env", ROOT / "scripts/super-board-lane-env.py")
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)

PARENT = {
    "PATH": "/usr/bin", "SystemRoot": "C:\\Windows", "TEMP": "/tmp", "TMP": "/tmp",
    "GH_TOKEN": "x", "GITHUB_TOKEN": "x", "GH_CONFIG_DIR": "/real/gh", "AWS_SECRET_ACCESS_KEY": "x",
    "DATABASE_URL": "x", "NPM_CONFIG_USERCONFIG": "x", "HOME": "/real/home", "RUNINFRA_GATEWAY_KEY": "k",
}


class EnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "profile"

    def tearDown(self):
        self.tmp.cleanup()

    def test_allowlist_drops_everything_else(self):
        env = E.child_env(PARENT, self.profile, gh_dir=Path(self.tmp.name) / "gh", keep=[])
        for gone in ["GH_TOKEN", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "NPM_CONFIG_USERCONFIG", "RUNINFRA_GATEWAY_KEY"]:
            self.assertNotIn(gone, env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_git_and_gh_are_neutralised(self):
        env = E.child_env(PARENT, self.profile, gh_dir=Path(self.tmp.name) / "gh", keep=[])
        self.assertEqual(env["GH_CONFIG_DIR"], str(Path(self.tmp.name) / "gh"))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_SYSTEM"], os.devnull)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual((env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]), ("credential.helper", ""))

    def test_profile_vars_point_into_temp_profile(self):
        env = E.child_env(PARENT, self.profile, gh_dir=Path(self.tmp.name) / "gh", keep=[])
        for k in ["HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"]:
            self.assertTrue(env[k].startswith(str(self.profile)), k)

    def test_keep_passes_only_named_tool_key(self):
        env = E.child_env(PARENT, self.profile, gh_dir=Path(self.tmp.name) / "gh", keep=["RUNINFRA_GATEWAY_KEY"])
        self.assertEqual(env["RUNINFRA_GATEWAY_KEY"], "k")
        self.assertNotIn("GH_TOKEN", env)

    def test_keep_never_passes_github_credentials(self):
        env = E.child_env(PARENT, self.profile, gh_dir=Path(self.tmp.name) / "gh", keep=["GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"])
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(env["GH_CONFIG_DIR"], str(Path(self.tmp.name) / "gh"))

    def test_seed_copies_listed_files_only_and_never_back(self):
        real = Path(self.tmp.name) / "real"
        (real / ".codex").mkdir(parents=True)
        (real / ".codex" / "auth.json").write_text("{}")
        (real / ".config" / "gh").mkdir(parents=True)
        (real / ".config" / "gh" / "hosts.yml").write_text("secret")
        E.seed_profile(self.profile, real_home=real, seed=[".codex/auth.json"])
        self.assertTrue((self.profile / "home" / ".codex" / "auth.json").exists())
        self.assertFalse((self.profile / "home" / ".config" / "gh" / "hosts.yml").exists())
        (self.profile / "home" / ".codex" / "auth.json").write_text("changed")
        self.assertEqual((real / ".codex" / "auth.json").read_text(), "{}")

    def test_seed_rejects_paths_outside_home(self):
        with self.assertRaises(ValueError):
            E.seed_profile(self.profile, real_home=Path(self.tmp.name), seed=["../x"])

    def env(self, **kw):
        return E.child_env(PARENT, self.profile, gh_dir=Path(self.tmp.name) / "gh", keep=[], **kw)

    def test_git_hardening_variables(self):
        env = self.env()
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file")
        self.assertEqual(env["GIT_SSH_COMMAND"], "false")
        self.assertEqual(env["GIT_SSH"], "false")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")

    def test_git_config_count_entries(self):
        env = self.env()
        n = int(env["GIT_CONFIG_COUNT"])
        pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(n)}
        self.assertEqual((env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]), ("credential.helper", ""))
        self.assertEqual(pairs["protocol.allow"], "never")
        self.assertEqual(pairs["protocol.file.allow"], "always")
        self.assertEqual(pairs["core.sshCommand"], "false")
        self.assertEqual(pairs["core.fsmonitor"], "false")
        self.assertEqual(pairs["core.hooksPath"], os.devnull)

    def test_extra_keep_nonsecret_filters_secrets(self):
        parent = dict(PARENT, MY_API_KEY="k", JAVA_HOME="/jdk", DB_PASSWORD="p", Some_Token="t", GH_TOKEN="g")
        env = E.child_env(parent, self.profile, gh_dir=Path(self.tmp.name) / "gh", keep=[],
                          extra_keep_nonsecret=["MY_API_KEY", "JAVA_HOME", "DB_PASSWORD", "Some_Token", "GH_TOKEN", "NOT_SET"])
        self.assertEqual(env["JAVA_HOME"], "/jdk")
        for gone in ["MY_API_KEY", "DB_PASSWORD", "Some_Token", "GH_TOKEN", "NOT_SET"]:
            self.assertNotIn(gone, env)


if __name__ == "__main__":
    unittest.main()
