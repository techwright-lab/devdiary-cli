from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from devdiary_attribution import contract
from devdiary_attribution.config import default_registry


class ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = default_registry("urn:acme", "urn:acme:human:owner", None)
        self.actor = {
            "actor_ref": "urn:acme:actor:reviewer",
            "kind": "agent",
            "display_name": "Reviewer",
            "humanized_name": "Alex",
            "attester_ref": "urn:acme:attester:launcher",
            "identities": {"paperclip_agent_id": "agent-123"},
        }

    def test_declarative_adapter_projects_runtime_session_provider_and_model(
        self,
    ) -> None:
        self.registry["adapters"]["custom"] = {
            "execution_role": "executor",
            "runtime": "custom-cli",
            "map": {
                "session_ref": "env:DEVDIARY_RUNTIME_SESSION_REF_CUSTOM",
                "provider": "env:DEVDIARY_RUNTIME_PROVIDER_CUSTOM",
                "model": "env:DEVDIARY_RUNTIME_MODEL_CUSTOM",
            },
        }

        context = contract.context(
            self.registry,
            self.actor,
            ["runtime"],
            Path("."),
            "custom",
            {
                "DEVDIARY_RUNTIME_SESSION_REF_CUSTOM": "session-1",
                "DEVDIARY_RUNTIME_PROVIDER_CUSTOM": "provider-1",
                "DEVDIARY_RUNTIME_MODEL_CUSTOM": "model-1",
            },
            None,
            None,
        )

        self.assertEqual(
            [
                {
                    "kind": "executor",
                    "system": "custom-cli",
                    "session_ref": "session-1",
                },
                {"kind": "model", "provider": "provider-1", "model": "model-1"},
            ],
            context["execution_chain"],
        )

    def test_context_contains_identity_but_no_attester_or_registry_identity_bindings(
        self,
    ) -> None:
        context = contract.context(
            self.registry,
            self.actor,
            ["runtime"],
            Path("."),
            None,
            {},
            None,
            None,
        )

        self.assertEqual(self.actor["actor_ref"], context["actor"]["ref"])
        self.assertNotIn("attester_ref", context["actor"])
        self.assertNotIn("identities", context["actor"])

    def test_context_never_persists_raw_command_arguments(self) -> None:
        context = contract.context(
            self.registry,
            self.actor,
            ["runtime", "--access-token", "test-only-sensitive-argument"],
            Path("."),
            None,
            {},
            None,
            None,
        )

        self.assertEqual(
            {"classification": "wrapped-process", "argument_count": 2},
            context["command"],
        )
        self.assertNotIn("test-only-sensitive-argument", str(context))

    def test_terminal_timestamp_is_strictly_after_start_for_instant_commands(
        self,
    ) -> None:
        context = contract.context(
            self.registry,
            self.actor,
            ["runtime"],
            Path("."),
            None,
            {},
            None,
            None,
        )
        envelope = contract.terminal_envelope(
            context,
            self.actor,
            "run.completed",
            context["started_at"],
            {
                "repositories": [],
                "commits": [],
                "pull_requests": [],
                "issues": [],
                "artifacts": [],
            },
        )

        started = datetime.fromisoformat(envelope["started_at"])
        ended = datetime.fromisoformat(envelope["ended_at"])
        self.assertGreater(ended, started)


if __name__ == "__main__":
    unittest.main()
