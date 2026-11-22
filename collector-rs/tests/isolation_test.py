"""No harness can reach the real ~/.claude or ~/.codex. A stand-in folder
plays the real home, so these tests never touch the real one."""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import install_test
import isolation
import saved_connection

TESTS = Path(__file__).resolve().parent
# Scripts that read the vendor login from the real home on purpose. They
# check each environment they build instead of guarding the whole process.
LIVE = ("qualify_claude.py", "qualify_codex.py")


class IsolationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="isolation-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.real = self.root / "real-home"
        (self.real / ".codex").mkdir(parents=True)
        (self.real / ".claude").mkdir()
        self.home = self.root / "home"
        stand_in = patch.object(isolation, "real_home", return_value=self.real)
        stand_in.start()
        self.addCleanup(stand_in.stop)

    def test_a_home_that_leads_to_the_real_one_is_refused(self):
        link = self.root / "codex-link"
        link.symlink_to(self.real / ".codex")
        fine = str(self.home)
        for env in [
            {"HOME": str(self.real)},
            {},
            {"HOME": fine, "CODEX_HOME": str(self.real / ".codex")},
            {"HOME": fine, "CODEX_HOME": str(self.real / ".codex/profiles/a")},
            {"HOME": fine, "CODEX_HOME": str(self.real)},
            {"HOME": fine, "CLAUDE_CONFIG_DIR": str(self.real / ".claude")},
            {"HOME": fine, "CODEX_HOME": str(link)},
            {"HOME": str(self.real / ".claude/..")},
        ]:
            with self.subTest(env=env), self.assertRaises(isolation.RealHome):
                isolation.checked(env)
        for env in [
            {"HOME": fine},
            {"HOME": fine, "CODEX_HOME": str(self.home / "control-plane-codex")},
            # Only the real ~/.claude and ~/.codex are off limits below it.
            {"HOME": str(self.real / "projects/scratch")},
        ]:
            with self.subTest(env=env):
                self.assertEqual(isolation.checked(env), env)

    def test_a_guarded_harness_drops_the_callers_homes_and_starts_nothing_that_reaches_them(
        self,
    ):
        caller = {
            "HOME": str(self.real),
            "CODEX_HOME": str(self.real / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.real / ".claude"),
        }
        # This module is guarded already; start from the unguarded Popen.
        unguarded = isolation.Guarded.__bases__[0]
        with (
            patch.dict(os.environ, caller),
            patch.object(subprocess, "Popen", unguarded),
        ):
            isolation.guard()
            isolation.guard()
            self.assertIs(subprocess.Popen, isolation.Guarded)
            self.assertNotIn("CODEX_HOME", os.environ)
            self.assertNotIn("CLAUDE_CONFIG_DIR", os.environ)
            home = Path(os.environ["HOME"])
            self.assertNotEqual(home.resolve(), self.real)
            self.assertEqual(list(home.iterdir()), [])

            started = self.root / "started"
            for env in [
                {"HOME": str(self.real), "PATH": "/usr/bin:/bin"},
                {"PATH": "/usr/bin:/bin"},
                {**caller, "HOME": str(self.home)},
            ]:
                with self.subTest(env=env), self.assertRaises(isolation.RealHome):
                    subprocess.run(
                        ["/bin/sh", "-c", f"touch '{started}'"], env=env, check=False
                    )
                self.assertFalse(started.exists())
            # A process that inherits the harness's environment gets its HOME.
            shown = subprocess.run(
                ["/bin/sh", "-c", 'printf %s "$HOME"'],
                capture_output=True,
                check=True,
            )
            self.assertEqual(shown.stdout.decode(), str(home))

    def test_every_harness_that_starts_a_process_guards_it(self):
        unguarded = []
        for path in sorted(TESTS.glob("*.py")):
            source = path.read_text()
            starts = re.search(r"^import (subprocess|pty)$", source, re.MULTILINE)
            entry = 'if __name__ == "__main__":' in source
            if path.name in ("isolation.py", "isolation_test.py") or not (
                starts and entry
            ):
                continue
            if path.name in LIVE:
                guarded = "isolation.checked(" in source
            else:
                guarded = "def setUpModule():\n    isolation.guard()" in source
            if not guarded:
                unguarded.append(path.name)
        self.assertEqual(unguarded, [])

    def test_harness_environments_leave_out_the_callers_homes(self):
        caller = {
            "HOME": str(self.real),
            "CODEX_HOME": str(self.real / ".codex"),
            "CLAUDE_CONFIG_DIR": str(self.real / ".claude"),
            "PATH": "/usr/bin:/bin",
        }
        # The install harness's own environment, without its mock server.
        fixture = object.__new__(install_test.InstallTest)
        fixture.home = self.home
        fixture.config = self.root / "config"
        fixture.scheduler = install_test.FakeScheduler(
            self.root, self.home, fixture.config
        )
        with patch.dict(os.environ, caller):
            built = [
                fixture.env(),
                saved_connection.environment(self.root, self.home),
                saved_connection.environment(self.root, self.home, caller),
            ]
        for env in built:
            with self.subTest(env=env):
                self.assertEqual(env["HOME"], str(self.home))
                self.assertNotIn("CODEX_HOME", env)
                self.assertNotIn("CLAUDE_CONFIG_DIR", env)
                isolation.checked(env)


def setUpModule():
    isolation.guard()


if __name__ == "__main__":
    unittest.main()
