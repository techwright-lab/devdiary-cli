from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devdiary.cli import main
from devdiary.config import load_registry


class CliTest(unittest.TestCase):
    def test_init_actor_add_doctor_and_schema_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / ".devdiary/attribution.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--config",
                            str(config),
                            "init",
                            "--namespace",
                            "urn:acme",
                            "--principal-ref",
                            "urn:acme:human:owner",
                            "--endpoint",
                            "https://example.test/ingest/v1/sessions",
                        ]
                    ),
                )
                self.assertEqual(
                    0,
                    main(
                        [
                            "--config",
                            str(config),
                            "actor",
                            "add",
                            "--ref",
                            "urn:acme:actor:reviewer",
                            "--display-name",
                            "Reviewer",
                            "--attester-ref",
                            "urn:acme:attester:launcher",
                            "--lane",
                            "review",
                            "--alias",
                            "Wright",
                            "--identity",
                            "paperclip_agent_id=agent-123",
                        ]
                    ),
                )
                self.assertEqual(
                    0,
                    main(
                        [
                            "--config",
                            str(config),
                            "adapter",
                            "add",
                            "--name",
                            "my-runtime",
                            "--runtime",
                            "custom-cli",
                            "--session-ref-env",
                            "DEVDIARY_RUNTIME_SESSION_REF_MY_RUNTIME",
                            "--provider-env",
                            "DEVDIARY_RUNTIME_PROVIDER_MY_RUNTIME",
                            "--model-env",
                            "DEVDIARY_RUNTIME_MODEL_MY_RUNTIME",
                        ]
                    ),
                )

            registry = load_registry(config)
            self.assertEqual(
                "urn:acme:actor:reviewer", registry["actors"][0]["actor_ref"]
            )
            self.assertEqual("review", registry["actors"][0]["lane"])
            self.assertEqual(["Wright"], registry["actors"][0]["aliases"])
            self.assertEqual(
                "agent-123", registry["actors"][0]["identities"]["paperclip_agent_id"]
            )
            self.assertEqual(
                "env:DEVDIARY_RUNTIME_MODEL_MY_RUNTIME",
                registry["adapters"]["my-runtime"]["map"]["model"],
            )

            output = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ, {"DEVDIARY_INGEST_KEY": "test-only"}, clear=False
                ),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(
                    0,
                    main(
                        [
                            "--config",
                            str(config),
                            "doctor",
                            "--actor",
                            "urn:acme:actor:reviewer",
                            "--json",
                        ]
                    ),
                )
            checks = json.loads(output.getvalue())
            self.assertTrue(all(check["ok"] for check in checks))
            self.assertNotIn("test-only", output.getvalue())

            schema_output = io.StringIO()
            with contextlib.redirect_stdout(schema_output):
                self.assertEqual(0, main(["schema", "envelope"]))
            self.assertEqual(
                "DevDiary Terminal Attribution Envelope v1",
                json.loads(schema_output.getvalue())["title"],
            )

    def test_run_requires_separator_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "registry.json"
            with contextlib.redirect_stdout(io.StringIO()):
                main(
                    [
                        "--config",
                        str(config),
                        "init",
                        "--namespace",
                        "urn:acme",
                        "--principal-ref",
                        "urn:acme:human:owner",
                    ]
                )
                main(
                    [
                        "--config",
                        str(config),
                        "actor",
                        "add",
                        "--ref",
                        "urn:acme:actor:reviewer",
                        "--display-name",
                        "Reviewer",
                        "--attester-ref",
                        "urn:acme:attester:launcher",
                    ]
                )
            error = io.StringIO()
            with contextlib.redirect_stderr(error):
                result = main(
                    [
                        "--config",
                        str(config),
                        "run",
                        "--actor",
                        "urn:acme:actor:reviewer",
                    ]
                )
            self.assertEqual(2, result)
            self.assertIn("requires a command", error.getvalue())


if __name__ == "__main__":
    unittest.main()
