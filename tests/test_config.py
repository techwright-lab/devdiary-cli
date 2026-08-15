from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devdiary_attribution.config import (
    ConfigError,
    add_actor,
    default_config_path,
    default_registry,
    load_registry,
    validate_registry,
    write_registry,
)


class ConfigTest(unittest.TestCase):
    def test_registry_round_trip_and_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".devdiary/attribution.json"
            registry = default_registry(
                "urn:acme",
                "urn:acme:human:owner",
                "https://example.test/ingest/v1/sessions",
            )
            write_registry(path, registry)

            self.assertEqual(registry, load_registry(path))
            if os.name != "nt":
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_plaintext_ingest_credentials_are_rejected_as_unknown_fields(self) -> None:
        registry = default_registry("urn:acme", "urn:acme:human:owner", None)
        registry["ingest"]["key"] = "do-not-store-this"

        with self.assertRaisesRegex(ConfigError, "unsupported fields"):
            validate_registry(registry)

    def test_key_environment_cannot_be_redirected_to_an_unrelated_secret(self) -> None:
        registry = default_registry("urn:acme", "urn:acme:human:owner", None)
        registry["ingest"]["key_env"] = "AWS_SECRET_ACCESS_KEY"

        with self.assertRaisesRegex(ConfigError, "DEVDIARY_INGEST_KEY"):
            validate_registry(registry)

    def test_default_registry_path_is_user_scoped(self) -> None:
        with mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": "/tmp/user-config"}, clear=True
        ):
            self.assertEqual(
                Path("/tmp/user-config/devdiary/attribution.json"),
                default_config_path(),
            )

    def test_duplicate_actor_refs_are_rejected_even_when_names_differ(self) -> None:
        registry = default_registry("urn:acme", "urn:acme:human:owner", None)
        registry["actors"] = [actor("Reviewer"), actor("Renamed Reviewer")]

        with self.assertRaisesRegex(ConfigError, "duplicate actor_ref"):
            validate_registry(registry)

    def test_actor_kind_and_optional_principal_must_be_valid_text(self) -> None:
        missing_kind = actor("Reviewer")
        missing_kind.pop("kind")
        with self.assertRaisesRegex(ConfigError, "actor.kind"):
            validate_registry(
                {
                    **default_registry("urn:acme", "urn:acme:human:owner", None),
                    "actors": [missing_kind],
                }
            )

        invalid_principal = actor("Reviewer")
        invalid_principal["principal_ref"] = "\u001b[31m"
        with self.assertRaisesRegex(ConfigError, "control characters"):
            validate_registry(
                {
                    **default_registry("urn:acme", "urn:acme:human:owner", None),
                    "actors": [invalid_principal],
                }
            )

    def test_adapter_cannot_override_authoritative_actor_or_read_secret_like_environment(
        self,
    ) -> None:
        registry = default_registry("urn:acme", "urn:acme:human:owner", None)
        registry["adapters"]["unsafe"] = {"map": {"actor_ref": "env:ACTOR"}}
        with self.assertRaisesRegex(ConfigError, "authoritative field"):
            validate_registry(registry)

        registry["adapters"]["unsafe"] = {
            "map": {"provider": "env:PROVIDER", "model": "env:MODEL_TOKEN"}
        }
        with self.assertRaisesRegex(ConfigError, "DEVDIARY_RUNTIME_PROVIDER"):
            validate_registry(registry)

    def test_adapter_requires_provider_and_model_mappings_together(self) -> None:
        registry = default_registry("urn:acme", "urn:acme:human:owner", None)
        registry["adapters"] = {
            "unsafe": {
                "execution_role": "executor",
                "runtime": "unsafe-runtime",
                "map": {"model": "env:DEVDIARY_RUNTIME_MODEL_UNSAFE"},
            }
        }

        with self.assertRaisesRegex(ConfigError, "provider and model together"):
            validate_registry(registry)

    def test_adapter_mappings_use_target_specific_devdiary_variables(self) -> None:
        registry = default_registry("urn:acme", "urn:acme:human:owner", None)
        registry["adapters"]["unsafe"] = {
            "map": {
                "provider": "env:AUTH",
                "model": "env:DEVDIARY_RUNTIME_MODEL_UNSAFE",
            }
        }

        with self.assertRaisesRegex(ConfigError, "DEVDIARY_RUNTIME_PROVIDER"):
            validate_registry(registry)

    def test_actor_identity_types_are_explicitly_allowlisted(self) -> None:
        candidate = actor("Reviewer")
        candidate["identities"] = {"custom_auth": "test-only-sensitive-value"}

        with self.assertRaisesRegex(ConfigError, "identity type"):
            validate_registry(
                {
                    **default_registry("urn:acme", "urn:acme:human:owner", None),
                    "actors": [candidate],
                }
            )

    def test_registry_schema_matches_adapter_mapping_constraints(self) -> None:
        schema_path = (
            Path(__file__).parents[1]
            / "src/devdiary_attribution/schemas/registry.schema.json"
        )
        mapping = json.loads(schema_path.read_text(encoding="utf-8"))["$defs"][
            "adapter"
        ]["properties"]["map"]

        self.assertFalse(mapping["additionalProperties"])
        self.assertEqual(
            "^env:DEVDIARY_RUNTIME_PROVIDER(?:_[A-Z0-9]+(?:_[A-Z0-9]+)*)?$",
            mapping["properties"]["provider"]["pattern"],
        )

    def test_remote_plain_http_endpoint_is_rejected(self) -> None:
        registry = default_registry(
            "urn:acme", "urn:acme:human:owner", "http://example.test/ingest"
        )
        with self.assertRaisesRegex(ConfigError, "HTTPS"):
            validate_registry(registry)

    def test_endpoint_user_information_is_rejected(self) -> None:
        registry = default_registry(
            "urn:acme",
            "urn:acme:human:owner",
            "https://user:password@example.test/ingest",
        )
        with self.assertRaisesRegex(ConfigError, "user information"):
            validate_registry(registry)

    def test_endpoint_query_and_fragment_are_rejected(self) -> None:
        for endpoint in (
            "https://example.test/ingest?token=test-only-sensitive-value",
            "https://example.test/ingest#test-only-sensitive-value",
        ):
            with (
                self.subTest(endpoint=endpoint),
                self.assertRaisesRegex(ConfigError, "query or fragment"),
            ):
                validate_registry(
                    default_registry("urn:acme", "urn:acme:human:owner", endpoint)
                )

    def test_malformed_endpoints_are_rejected_during_registry_validation(self) -> None:
        urls = [
            "https:///ingest",
            "https://example.test:not-a-port/ingest",
            "https://example.test:70000/ingest",
            "https://[broken/ingest",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ConfigError):
                validate_registry(
                    default_registry("urn:acme", "urn:acme:human:owner", url)
                )

    def test_actor_add_preserves_editable_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            write_registry(
                path, default_registry("urn:acme", "urn:acme:human:owner", None)
            )
            add_actor(path, actor("Reviewer"))

            stored = json.loads(path.read_text())
            self.assertEqual(
                "urn:acme:actor:reviewer", stored["actors"][0]["actor_ref"]
            )

    def test_failed_forced_write_preserves_the_previous_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            original = default_registry("urn:acme", "urn:acme:human:owner", None)
            write_registry(path, original)
            replacement = {**original, "namespace": "urn:replacement"}

            with (
                mock.patch(
                    "devdiary_attribution.config.os.replace",
                    side_effect=OSError("simulated atomic replacement failure"),
                ),
                self.assertRaisesRegex(ConfigError, "could not be written"),
            ):
                write_registry(path, replacement, force=True)

            self.assertEqual(original, load_registry(path))

    def test_registry_writer_refuses_symbolic_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.json"
            target.write_text("do not replace")
            link = Path(directory) / "registry.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symbolic links are unavailable")

            with self.assertRaisesRegex(ConfigError, "symbolic link"):
                write_registry(
                    link,
                    default_registry("urn:acme", "urn:acme:human:owner", None),
                    force=True,
                )
            self.assertEqual("do not replace", target.read_text())

    def test_registry_loader_refuses_a_symbolic_link(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.json"
            registry = default_registry("urn:acme", "urn:acme:human:owner", None)
            target.write_text(json.dumps(registry), encoding="utf-8")
            link = Path(directory) / "registry.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symbolic links are unavailable")

            with self.assertRaisesRegex(ConfigError, "symbolic link"):
                load_registry(link)

    def test_actor_text_cannot_inject_terminal_control_characters(self) -> None:
        candidate = actor("Reviewer\u001b[31m")
        with self.assertRaisesRegex(ConfigError, "control characters"):
            validate_registry(
                {
                    **default_registry("urn:acme", "urn:acme:human:owner", None),
                    "actors": [candidate],
                }
            )


def actor(name: str) -> dict:
    return {
        "actor_ref": "urn:acme:actor:reviewer",
        "kind": "agent",
        "display_name": name,
        "humanized_name": None,
        "attester_ref": "urn:acme:attester:launcher",
        "identities": {},
    }


if __name__ == "__main__":
    unittest.main()
