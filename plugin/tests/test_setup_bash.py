"""ams.18: super-board-setup.py must run Git Bash (not WSL) with forward-slash paths on Windows. Offline."""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(HERE, "..", "scripts", "super-board-setup.py")


def load():
    spec = importlib.util.spec_from_file_location("sb_setup_under_test", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_check(*_a, **_k):
    return {"missing": ["skill:x"], "old_dirs": [], "configs": {}, "upgrade": False, "agents_rows": [],
            "guard_env": False, "added_skills": [], "board_fields": {}, "git": True, "helper_skills": True,
            "from": None}


class BashExeTests(unittest.TestCase):
    def setUp(self):
        self.mod = load()
        self.tmp = tempfile.mkdtemp()
        self.name = "bash.exe" if os.name == "nt" else "bash"
        self.bindir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bindir)
        self.fake = os.path.join(self.bindir, self.name)
        with open(self.fake, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.fake, 0o755)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.old_cwd)

    def test_a_returns_absolute_fake_and_ignores_relative_entry(self):
        rel = "relbin"
        os.makedirs(os.path.join(self.tmp, rel))
        with open(os.path.join(self.tmp, rel, self.name), "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(os.path.join(self.tmp, rel, self.name), 0o755)
        got = self.mod.bash_exe(os.pathsep.join(["", rel, self.bindir]))
        self.assertEqual(os.path.normcase(got), os.path.normcase(self.fake))
        self.assertTrue(os.path.isabs(got))

    @unittest.skipUnless(os.name == "nt", "WSL launcher dirs exist only on Windows")
    def test_a_skips_wsl_launchers_before_git_bash(self):
        sysroot = os.path.join(self.tmp, "Windows")
        wsl = os.path.join(sysroot, "System32")
        apps = os.path.join(self.tmp, "Microsoft", "WindowsApps")
        for d in (wsl, apps):
            os.makedirs(d)
            with open(os.path.join(d, self.name), "w") as f:
                f.write("#!/bin/sh\n")
        with mock.patch.dict(os.environ, {"SystemRoot": sysroot}):
            got = self.mod.bash_exe(os.pathsep.join([wsl, apps, self.bindir]))
        self.assertEqual(os.path.normcase(got), os.path.normcase(self.fake))

    def test_a_posix_falls_back_to_plain_bash(self):
        with mock.patch.object(self.mod.os, "name", "posix"):
            self.assertEqual(self.mod.bash_exe(os.pathsep.join(["", "relbin"])), "bash")

    def test_a_quoted_path_entry_is_accepted(self):
        got = self.mod.bash_exe('"' + self.bindir + '"')
        self.assertEqual(os.path.normcase(got), os.path.normcase(self.fake))

    def _nt_tree(self, with_bin):
        """A fake Git for Windows tree + a PATH dir holding only git.exe. Returns (gitdir, root)."""
        root = os.path.join(self.tmp, "Git")
        gitdir = os.path.join(self.tmp, "gitcmd")
        os.makedirs(gitdir)
        with open(os.path.join(gitdir, "git.exe"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(root, "usr", "bin"))
        with open(os.path.join(root, "usr", "bin", "bash.exe"), "w") as f:
            f.write("x")
        if with_bin:
            os.makedirs(os.path.join(root, "bin"))
            with open(os.path.join(root, "bin", "bash.exe"), "w") as f:
                f.write("x")
        return gitdir, root

    def _nt_bash_exe(self, gitdir, root):
        exec_path = os.path.join(root, "mingw64", "libexec", "git-core")

        def fake_run(cmd, **kw):
            self.assertEqual(cmd[1:], ["--exec-path"])
            self.assertTrue(kw.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, exec_path + "\n", "")

        with mock.patch.object(self.mod.os, "name", "nt"), \
                mock.patch.dict(os.environ, {"PATHEXT": ".exe", "SystemRoot": os.path.join(self.tmp, "Windows")}), \
                mock.patch.object(self.mod.subprocess, "run", fake_run):
            return self.mod.bash_exe(gitdir)

    def test_d_nt_uses_git_bash_usr_bin(self):
        gitdir, root = self._nt_tree(with_bin=False)
        got = self._nt_bash_exe(gitdir, root)
        self.assertEqual(os.path.normcase(got), os.path.normcase(os.path.join(root, "usr", "bin", "bash.exe")))

    def test_d_nt_git_bash_bin_wins_over_usr_bin(self):
        gitdir, root = self._nt_tree(with_bin=True)
        got = self._nt_bash_exe(gitdir, root)
        self.assertEqual(os.path.normcase(got), os.path.normcase(os.path.join(root, "bin", "bash.exe")))

    def test_d_nt_nothing_found_is_none(self):
        with mock.patch.object(self.mod.os, "name", "nt"), \
                mock.patch.dict(os.environ, {"PATHEXT": ".EXE"}):
            self.assertIsNone(self.mod.bash_exe(os.path.join(self.tmp, "empty")))


class InstallArgvTests(unittest.TestCase):
    PACK = "C:\\pk"
    ROOT = "C:\\proj\\root"
    SENTINEL = "SENTINEL-BASH"

    def captured_argv(self, osname):
        mod = load()
        calls = []

        def fake_run(cmd, cwd=None, check=False, input=None):
            calls.append((cmd, cwd))
            return subprocess.CompletedProcess(cmd, 1, "", "stop")

        with mock.patch.object(mod, "check", fake_check), mock.patch.object(mod, "run", fake_run), \
                mock.patch.object(mod, "backup", lambda *a, **k: None), \
                mock.patch.object(mod, "bash_exe", lambda *a, **k: self.SENTINEL), \
                mock.patch.object(mod.os, "name", osname):
            mod.fix(self.ROOT, self.PACK)
        self.assertEqual(len(calls), 1)
        return calls[0]

    def test_e_install_uses_bash_exe(self):
        cmd, _ = self.captured_argv("posix")
        self.assertEqual(cmd[0], self.SENTINEL)

    def test_e_helpers_site_uses_bash_exe(self):
        mod = load()
        calls = []

        def helper_check(*a, **k):
            return {**fake_check(), "missing": [], "helper_skills": False}

        def fake_run(cmd, cwd=None, check=False, input=None):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(mod, "check", helper_check), mock.patch.object(mod, "run", fake_run), \
                mock.patch.object(mod, "have", lambda t: t == "npx"), \
                mock.patch.object(mod, "bash_exe", lambda *a, **k: self.SENTINEL), \
                mock.patch.object(mod, "helper_readiness", lambda *a, **k: {"ready": True}), \
                mock.patch.object(mod, "agents_sync", lambda *a, **k: []), \
                mock.patch.object(mod, "guard_env_sync", lambda *a, **k: False):
            mod.fix(self.ROOT, self.PACK)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], self.SENTINEL)

    def test_f_no_git_bash_returns_error_without_running(self):
        mod = load()

        def boom(*a, **k):
            raise AssertionError("run() must not be called")

        with mock.patch.object(mod, "check", fake_check), mock.patch.object(mod, "run", boom), \
                mock.patch.object(mod, "backup", lambda *a, **k: None), \
                mock.patch.object(mod, "bash_exe", lambda *a, **k: None):
            res = mod.fix(self.ROOT, self.PACK)
        self.assertTrue(res["error"].startswith("Git Bash not found"))

    def test_b_nt_uses_forward_slashes_and_native_cwd(self):
        cmd, cwd = self.captured_argv("nt")
        self.assertEqual(cmd[1], "C:/pk/install.sh")
        self.assertEqual(cmd[2], "C:/proj/root")
        self.assertEqual(cwd, self.ROOT)

    def test_c_non_nt_paths_unchanged(self):
        cmd, _ = self.captured_argv("posix")
        self.assertEqual(cmd[1], os.path.join(self.PACK, "install.sh"))
        self.assertEqual(cmd[2], self.ROOT)


if __name__ == "__main__":
    unittest.main()
