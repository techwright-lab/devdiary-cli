"""Unsupported platforms must fail before reading settings or spawning probes."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from devdiary import observer_discovery
from devdiary.cli import main


class ObserverPlatformTest(unittest.TestCase):
    def test_all_commands_are_structured_and_side_effect_free_when_unsupported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = ["--state-dir", str(root / "state")]
            commands = [
                ["discover"],
                [
                    "plan",
                    *state,
                    "--settings",
                    str(root / "settings.json"),
                    "--repository",
                    str(root),
                    "--executable",
                    sys.executable,
                ],
                ["apply", "--plan", str(root / "missing-plan.json"), "--consent"],
                ["remove", *state, "--consent"],
                ["health", *state],
                ["observations", *state],
                ["sync", *state],
                [
                    "connect",
                    *state,
                    "--endpoint",
                    "https://example.test/ingest/v1/observations",
                    "--collector-ref",
                    "fixture",
                    "--repository-ref",
                    "https://github.com/test/repo",
                    "--key-file",
                    str(root / "missing-key"),
                    "--consent",
                ],
            ]
            # Native Windows exercises the real platform detection, not a mock.
            platform = (
                nullcontext()
                if os.name == "nt"
                else patch("devdiary.observer.supported_platform", return_value=False)
            )
            with (
                platform,
                patch("subprocess.Popen", side_effect=AssertionError("spawned")),
            ):
                for arguments in commands:
                    with self.subTest(command=arguments[0]):
                        output = io.StringIO()
                        with redirect_stdout(output):
                            self.assertEqual(2, main(["observer", *arguments]))
                        self.assertEqual(
                            {
                                "error_code": "observer_platform_unsupported",
                                "platform_support": "POSIX_only",
                            },
                            json.loads(output.getvalue()),
                        )
                        self.assertEqual([], list(root.iterdir()))
                with self.assertRaisesRegex(
                    ValueError, "observer_platform_unsupported"
                ):
                    observer_discovery.probe(sys.executable, "--version", str(root))
