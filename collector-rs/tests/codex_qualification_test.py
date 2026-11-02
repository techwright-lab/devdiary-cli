"""Network-inert control tests and explicitly opt-in stock provider qualification."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("qualify_codex.py")


class CodexQualificationTest(unittest.TestCase):
    def test_default_is_inert_and_reports_unqualified(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, timeout=5, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["runtime_qualified"])
        self.assertEqual(report["model_runs_started"], 0)

    def test_missing_execution_helpers_are_refused_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory)
            (package / "bin").mkdir()
            native = b"\x7fELFfixture"
            (package / "bin/codex").write_bytes(native)
            (package / "bin/codex").chmod(0o700)
            (package / "codex-package.json").write_text(
                json.dumps({"version": "0.166.0", "entrypoint": "bin/codex"})
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--inspect-package",
                    str(package),
                    "--sha256",
                    hashlib.sha256(native).hexdigest(),
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["error"], "package_incomplete")

    def test_complete_package_inspection_pins_every_execution_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"\x7fELFfixture"
            names = [
                "bin/codex",
                "bin/codex-exec-server",
                "bin/codex-code-mode-host",
                "codex-resources/bwrap",
            ]
            for name in names:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o700)
            (root / "codex-package.json").write_text(
                '{"version":"0.166.0","entrypoint":"bin/codex"}'
            )
            args = [
                sys.executable,
                str(SCRIPT),
                "--inspect-package",
                str(root),
                "--sha256",
            ]
            good = subprocess.run(
                [*args, hashlib.sha256(content).hexdigest()],
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertEqual(
                set(json.loads(good.stdout)["package_components"]), set(names)
            )
            bad = subprocess.run(
                [*args, "0" * 64], capture_output=True, timeout=5, check=False
            )
            self.assertNotEqual(bad.returncode, 0)
            self.assertEqual(json.loads(bad.stdout)["error"], "package_hash_mismatch")

    def test_live_arguments_without_consent_do_not_access_paths(self):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--package",
                "/not/a/package",
                "--auth-file",
                "/not/an/auth/file",
                "--max-model-runs",
                "1",
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"runtime_qualified": False, "model_runs_started": 0},
        )


@unittest.skipUnless(
    os.environ.get("DEVDIARY_QUALIFY_CODEX") == "1",
    "explicit stock provider qualification only",
)
class StockCodexQualification(unittest.TestCase):
    def test_real_stock_capture_correlation_replay_and_removal(self):
        package = (
            Path.home()
            / ".codex/packages/standalone/releases/0.166.0-x86_64-unknown-linux-musl"
        )
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--consent-provider-use",
                "--package",
                str(package),
                "--sha256",
                "b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8",
                "--auth-file",
                str(Path.home() / ".codex/auth.json"),
                "--max-model-runs",
                "6",
                "--cases",
                "tool,resume,permission,interrupt,missing_terminal,control_plane",
            ],
            capture_output=True,
            timeout=180,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        report = json.loads(result.stdout)
        self.assertTrue(report["gates"]["tool"]["passed"], report)
        self.assertTrue(report["gates"]["resume"]["passed"], report)
        self.assertTrue(report["gates"]["permission"]["passed"], report)
        self.assertTrue(report["gates"]["interrupt"]["passed"], report)
        self.assertTrue(report["gates"]["missing_terminal"]["passed"], report)
        self.assertTrue(report["gates"]["control_plane"]["passed"], report)
        self.assertTrue(report["gates"]["delivery_removal"]["passed"], report)
        self.assertEqual(report["model_runs_started"], 6)


if __name__ == "__main__":
    unittest.main()
