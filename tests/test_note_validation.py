from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devdiary import note_validation as validation

FIXTURE = Path(__file__).parent / "fixtures" / "note_capture_contract.json"


def scope(name="scope_a"):
    return json.loads(FIXTURE.read_bytes())[name]


def setup():
    return {
        "schema_version": 1,
        "origin": "https://example.test",
        "key_file": "/private/synthetic/key",
        "metadata": scope(),
    }


class NoteValidationTest(unittest.TestCase):
    def rejects(self, action, code):
        with self.assertRaises(validation.NoteError) as raised:
            action()
        self.assertTrue(str(raised.exception) == code, "stable error required")
        self.assertIsNone(raised.exception.__cause__)

    def test_valid_setup_preserves_exact_scope(self):
        value = setup()
        self.assertTrue(
            validation.read_setup(io.BytesIO(json.dumps(value).encode())) == value
        )
        self.assertTrue(validation.validate_scope(scope()) == scope())

    def test_setup_exact_keys_and_types(self):
        for field in ("schema_version", "origin", "key_file", "metadata"):
            value = setup()
            del value[field]
            self.rejects(
                lambda value=value: validation.validate_setup(value), "invalid_setup"
            )
        for field in ("content", "token", "actor_ref", "endpoint_path", "registry"):
            self.rejects(
                lambda field=field: validation.validate_setup(
                    {**setup(), field: "SENTINEL"}
                ),
                "invalid_setup",
            )
        for version in (True, 1.0, "1", None, 2):
            self.rejects(
                lambda version=version: validation.validate_setup(
                    {**setup(), "schema_version": version}
                ),
                "invalid_setup",
            )
        for locator in (
            "relative",
            "~/key",
            "$HOME/key",
            "/a/../key",
            "/a/./key",
            "/a//key",
            "/a/",
            "/a/\x00key",
        ):
            self.rejects(
                lambda locator=locator: validation.validate_setup(
                    {**setup(), "key_file": locator}
                ),
                "invalid_setup",
            )

    def test_scope_exact_server_grammar_and_encoded_budget(self):
        changes = {
            "schema_version": [True, 1.0, "1", 2],
            "subject_ref": [
                None,
                "user_123",
                "12345678-1234-abcd-ABCD-123456789abg",
                "é",
            ],
            "workspace_ref": [
                "workspace_",
                "workspace_a-b",
                "workspace_é",
                "workspace_a\n",
            ],
            "capability": ["notes:read"],
            "endpoint_path": ["/ingest/v1/notes/"],
            "scope_path": ["https://example.test/ingest/v1/notes/scope"],
        }
        for field, values in changes.items():
            for value in values:
                self.rejects(
                    lambda field=field, value=value: validation.validate_scope(
                        {**scope(), field: value}
                    ),
                    "invalid_scope",
                )
        self.rejects(
            lambda: validation.validate_scope({**scope(), "token": "SENTINEL"}),
            "invalid_scope",
        )
        for field in scope():
            value = scope()
            del value[field]
            self.rejects(
                lambda value=value: validation.validate_scope(value), "invalid_scope"
            )
        base = scope()
        prefix = "workspace_"
        base["workspace_ref"] = prefix
        overhead = len(json.dumps(base, separators=(",", ":")).encode())
        base["workspace_ref"] += "a" * (4096 - overhead)
        self.assertTrue(validation.validate_scope(base) == base)
        self.rejects(
            lambda: validation.validate_scope(
                {**base, "workspace_ref": base["workspace_ref"] + "a"}
            ),
            "invalid_scope",
        )

    def test_strict_json_rejects_encodings_duplicates_constants_surrogates_and_depth(
        self,
    ):
        good = json.dumps(setup())
        bad = [
            b"\xef\xbb\xbf" + good.encode(),
            good.encode("utf-16"),
            good.encode("utf-32"),
            b"\xff",
            b"[]",
            b"null",
            b"{} {}",
            b'{"a":NaN}',
            b'{"a":Infinity}',
            b'{"a":1,"a":2}',
            b'{"a":{"b":1,"b":2}}',
            b'{"a":"\\ud800"}',
            b'{"a":"\\udfff"}',
            b'{"a":{"b":"\\ud800"}}',
            b'{"a":' * 9 + b"0" + b"}" * 9,
        ]
        for raw in bad:
            self.rejects(
                lambda raw=raw: validation.read_setup(io.BytesIO(raw)), "invalid_setup"
            )
        self.assertTrue(
            validation.decode_object(
                b'{"a":' * 8 + b"0" + b"}" * 8, 4096, "invalid_scope"
            )
        )

    def test_binary_read_is_bounded_and_exact_limit_is_accepted(self):
        raw = json.dumps(setup()).encode()
        raw += b" " * (262144 - len(raw))

        class Stream(io.BytesIO):
            def read(self, size=-1):
                if size != 262145:
                    raise AssertionError("bounded binary read required")
                return super().read(size)

        self.assertTrue(validation.read_setup(Stream(raw)) == setup())
        self.rejects(lambda: validation.read_setup(Stream(raw + b" ")), "invalid_setup")
        self.rejects(lambda: validation.read_setup(io.StringIO("{}")), "invalid_setup")

    def test_origin_is_canonical_not_normalized(self):
        valid = [
            "https://example.test",
            "https://a-b.example:8443",
            "https://127.0.0.1",
            "https://[::1]",
            "https://[2001:db8::1]:8443",
        ]
        for origin in valid:
            self.assertTrue(validation.validate_origin(origin) == origin)
        bad = [
            None,
            "http://example.test",
            "HTTPS://example.test",
            "https://Example.test",
            "https://example.test/",
            "https://example.test?",
            "https://example.test#",
            "https://u:p@example.test",
            "https://example.test.",
            "https://ex%61mple.test",
            "https://example.test\\x",
            " https://example.test",
            "https://example.test\n",
            "https://[0:0:0:0:0:0:0:1]",
            "https://[::FFFF:7f00:1]",
            "https://::1",
            "https://example.test:443",
            "https://example.test:0444",
            "https://example.test:0",
            "https://example.test:65536",
            "https://example.test:",
            "https://-bad.test",
            "https://a..b",
            "https://é.test",
            "https://127.1",
            "https://2130706433",
            "https://0177.0.0.1",
            "https://127.0.0.1/anything",
        ]
        for origin in bad:
            self.rejects(
                lambda origin=origin: validation.validate_origin(origin),
                "invalid_origin",
            )

    def test_nonfinite_exponents_are_rejected(self):
        for raw in (b'{"a":1e999}', b'{"a":{"b":-1e999}}'):
            self.rejects(
                lambda raw=raw: validation.decode_object(raw, 4096, "invalid_scope"),
                "invalid_scope",
            )

    def test_legacy_numeric_authorities_are_rejected(self):
        for origin in ("https://0x7f000001", "https://0x7f.0.0.1", "https://127.0x1"):
            self.rejects(
                lambda origin=origin: validation.validate_origin(origin),
                "invalid_origin",
            )

    def test_binding_comparison_preserves_authenticated_spelling(self):
        validation.require_matching_scope(scope(), scope())
        self.rejects(
            lambda: validation.require_matching_scope(scope(), scope("scope_b")),
            "binding_mismatch",
        )
        self.rejects(
            lambda: validation.require_matching_scope(
                scope(), {**scope(), "subject_ref": scope()["subject_ref"].lower()}
            ),
            "binding_mismatch",
        )


class NoteQualificationTest(unittest.TestCase):
    def setUp(self):
        if os.environ.get("DEVDIARY_PROFILE_QUALIFICATION") != "1":
            self.skipTest("private profile qualification lane only")

    def test_retained_network_guard_is_enabled(self):
        self.assertTrue(getattr(sys, "_devdiary_qualification_network_guard", False))


class NoteValidationPortabilityTest(unittest.TestCase):
    def run_case(self, case, *, qualification=False, marker=None):
        environment = {"DEVDIARY_PROFILE_QUALIFICATION": "1"} if qualification else {}
        local_sys = SimpleNamespace()
        if marker is not None:
            local_sys._devdiary_qualification_network_guard = marker
        result = unittest.TestResult()
        with (
            mock.patch(f"{__name__}.sys", local_sys),
            mock.patch(f"{__name__}.os", SimpleNamespace(environ=environment)),
        ):
            case.run(result)
        return result

    def test_portable_validator_does_not_require_private_guard_marker(self):
        result = self.run_case(
            NoteValidationTest("test_valid_setup_preserves_exact_scope")
        )
        self.assertTrue(
            result.wasSuccessful() and not result.skipped,
            "portable validator must not require the qualification harness",
        )

    def test_qualification_check_is_explicitly_opted_in(self):
        result = self.run_case(
            NoteQualificationTest("test_retained_network_guard_is_enabled")
        )
        self.assertTrue(
            result.wasSuccessful() and len(result.skipped) == 1,
            "ordinary discovery must not require a private harness",
        )

    def test_requested_qualification_without_guard_is_a_failure_not_a_skip(self):
        result = self.run_case(
            NoteQualificationTest("test_retained_network_guard_is_enabled"),
            qualification=True,
        )
        self.assertTrue(
            len(result.failures) == 1 and not result.errors and not result.skipped,
            "an explicitly requested qualification must require its guard",
        )

    def test_requested_qualification_with_guard_runs(self):
        result = self.run_case(
            NoteQualificationTest("test_retained_network_guard_is_enabled"),
            qualification=True,
            marker=True,
        )
        self.assertTrue(
            result.wasSuccessful() and not result.skipped,
            "guarded qualification must run",
        )


if __name__ == "__main__":
    unittest.main()
