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
            def read(self, size: int | None = -1) -> bytes:
                if size != 262145:
                    raise AssertionError("bounded binary read required")
                return super().read(size)

        self.assertTrue(validation.read_setup(Stream(raw)) == setup())
        self.rejects(lambda: validation.read_setup(Stream(raw + b" ")), "invalid_setup")
        malformed = Stream(raw)
        with mock.patch.object(
            Stream, "read", return_value=json.dumps(setup())
        ) as read:
            self.rejects(lambda: validation.read_setup(malformed), "invalid_setup")
        read.assert_called_once_with(262145)

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


class NoteWriteValidationTest(unittest.TestCase):
    def rejects(self, action, code="invalid_note"):
        with self.assertRaises(validation.NoteError) as raised:
            action()
        self.assertEqual(str(raised.exception), code)
        self.assertIsNone(raised.exception.__cause__)

    def fixture(self, name):
        path = FIXTURE.with_name("note_outbox_contract.json")
        return json.loads(path.read_bytes())[name]

    def test_literal_create_and_receipt(self):
        request = self.fixture("create")
        self.assertEqual(validation.validate_write(request), request)
        receipt = self.fixture("create_receipt")
        self.assertEqual(
            validation.validate_receipt(
                validation.encode_object(receipt), 201, request
            ),
            receipt,
        )

    def test_action_absence_types_and_literal_receipts(self):
        for action, status in (("create", 201), ("update", 200), ("delete", 200)):
            request = self.fixture(action)
            self.assertEqual(validation.validate_write(request), request)
            receipt = self.fixture(action + "_receipt")
            self.assertEqual(
                validation.validate_receipt(
                    validation.encode_object(receipt), status, request
                ),
                receipt,
            )
        create = self.fixture("create")
        for revision in (None, True, 0, 1, 1.0, "1"):
            self.rejects(
                lambda revision=revision: validation.validate_write(
                    {**create, "expected_revision": revision}
                )
            )
        for action in ("update", "delete"):
            request = self.fixture(action)
            for revision in (None, True, False, 0, -1, 1.0, "1"):
                self.rejects(
                    lambda revision=revision, request=request: (
                        validation.validate_write(
                            {**request, "expected_revision": revision}
                        )
                    )
                )
            missing = dict(request)
            del missing["expected_revision"]
            self.rejects(lambda missing=missing: validation.validate_write(missing))
            for context in (None, "workspace"):
                self.rejects(
                    lambda context=context, request=request: validation.validate_write(
                        {**request, "context": context}
                    )
                )
        for field in ("recorded_at", "actor_ref", "owner", "source"):
            self.rejects(
                lambda field=field: validation.validate_write({**create, field: "x"})
            )
        for context in (None, {}, True, "other"):
            self.rejects(
                lambda context=context: validation.validate_write(
                    {**create, "context": context}
                )
            )
        for action in ("update", "delete"):
            self.rejects(
                lambda action=action: validation.validate_write(
                    {**self.fixture(action), "attributes": {"recorded_at": None}}
                )
            )
        self.rejects(
            lambda: validation.validate_write(
                {**self.fixture("delete"), "references": []}
            )
        )

    def test_receipt_exact_types_status_mapping_and_terminal_dominance(self):
        request = self.fixture("create")
        receipt = self.fixture("create_receipt")
        for field in receipt:
            incomplete = dict(receipt)
            del incomplete[field]
            self.rejects(
                lambda incomplete=incomplete: validation.validate_receipt(
                    validation.encode_object(incomplete), 201, request
                ),
                "invalid_ack",
            )
        changes = {
            "operation_id": [None, True, "different"],
            "note_ref": [None, 1, "different"],
            "note_id": [None, True, "note_", "note_é", "note_a b", "note_a\n"],
            "revision": [True, 1.0, "1", None, 0, -1, 2],
            "outcome": [None, {}, "other", "replayed", "deleted"],
            "content": ["PRIVATE_SENTINEL"],
        }
        for field, values in changes.items():
            for value in values:
                self.rejects(
                    lambda field=field, value=value: validation.validate_receipt(
                        validation.encode_object({**receipt, field: value}),
                        201,
                        request,
                    ),
                    "invalid_ack",
                )
        for status in (200, 202, 204, 301, 409, True, 201.0):
            self.rejects(
                lambda status=status: validation.validate_receipt(
                    validation.encode_object(receipt), status, request
                ),
                "invalid_ack",
            )
        self.rejects(
            lambda: validation.validate_receipt(
                validation.encode_object(receipt), 201, request, "note_different"
            ),
            "invalid_ack",
        )
        for action in ("create", "update"):
            request = self.fixture(action)
            replay = {**self.fixture(action + "_receipt"), "outcome": "replayed"}
            self.assertEqual(
                validation.validate_receipt(
                    validation.encode_object(replay), 200, request
                ),
                replay,
            )
        stale_delete = {**self.fixture("delete"), "expected_revision": 20}
        for request in (self.fixture("create"), self.fixture("update"), stale_delete):
            deleted = {
                **self.fixture("delete_receipt"),
                "operation_id": request["operation_id"],
            }
            self.assertEqual(
                validation.validate_receipt(
                    validation.encode_object(deleted), 200, request
                ),
                deleted,
            )
        for outcome in ("applied", "replayed"):
            self.rejects(
                lambda outcome=outcome: validation.validate_receipt(
                    validation.encode_object(
                        {**self.fixture("delete_receipt"), "outcome": outcome}
                    ),
                    200,
                    self.fixture("delete"),
                ),
                "invalid_ack",
            )
        for revision in (1, 3):
            self.rejects(
                lambda revision=revision: validation.validate_receipt(
                    validation.encode_object(
                        {**self.fixture("update_receipt"), "revision": revision}
                    ),
                    200,
                    self.fixture("update"),
                ),
                "invalid_ack",
            )


class NoteWriteBoundaryTest(unittest.TestCase):
    def request(self):
        return {
            "action": "create",
            "operation_id": "op:1",
            "note_ref": "ref:1",
            "attributes": {"content": "synthetic"},
        }

    def rejects(self, value):
        with self.assertRaises(validation.NoteError) as raised:
            validation.validate_write(value)
        self.assertEqual(raised.exception.code, "invalid_note")

    def test_character_timestamp_and_reference_boundaries(self):
        base = self.request()
        for field, limit in (("content", 16384), ("next_step", 4096), ("source", 200)):
            attrs = {**base["attributes"], field: "é" * limit}
            self.assertEqual(
                validation.validate_write({**base, "attributes": attrs})["attributes"],
                attrs,
            )
            self.rejects({**base, "attributes": {**attrs, field: "é" * (limit + 1)}})
        for time in (None, "2026-11-05T12:47:22Z", "2026-11-05T12:47:22.123+05:30"):
            value = {**base, "attributes": {**base["attributes"], "occurred_at": time}}
            self.assertEqual(validation.validate_write(value), value)
        for time in ("2026-11-05T12:47:22", "2026-99-05T12:47:22Z", True, {}, "xZ"):
            self.rejects(
                {**base, "attributes": {**base["attributes"], "occurred_at": time}}
            )
        ref = {"kind": "document", "ref": "vault:a/b.md"}
        for refs in ([], [ref], [ref] * 32):
            value = {**base, "references": refs}
            self.assertEqual(validation.validate_write(value), value)
        for refs in (None, [None], [ref, None], {}, "x", [ref] * 33):
            self.rejects({**base, "references": refs})
        for ref in (
            {"kind": "note", "ref": "note_abc", "relation": "supersedes"},
            {"kind": "document", "ref": "vault:é/😀.md"},
            {"kind": "external", "ref": "https://example.test/a?q=ok"},
            {"kind": "external", "ref": "urn:example:abc"},
            {"kind": "run", "ref": "a-b_1"},
        ):
            self.assertEqual(
                validation.validate_write({**base, "references": [ref]})["references"],
                [ref],
            )
        for ref in (
            {"kind": "note", "ref": "a b"},
            {"kind": "note", "ref": "x", "relation": None},
            {"kind": "work", "ref": "x", "relation": "supersedes"},
            {"kind": "document", "ref": "vault:../x"},
            {"kind": "document", "ref": "/x"},
            {"kind": "document", "ref": "a//b"},
            {"kind": "document", "ref": "a%20b"},
            {"kind": "external", "ref": "https://user:pass@example.test"},
            {"kind": "external", "ref": "https://example.test/?%74oken=x"},
            {"kind": "external", "ref": "https://example.test/#api_key=x"},
            {"kind": "external", "ref": "file:///x"},
            {"kind": "external", "ref": "javascript:x"},
            {"kind": "external", "ref": "https://"},
            {"kind": "external", "ref": "https://é.test"},
            {"kind": "document", "ref": "x" * 2049},
            {"kind": "document", "ref": "x", "owner": "x"},
        ):
            self.rejects({**base, "references": [ref]})

    def test_final_wire_budget_and_strict_bounded_reader(self):
        value = {
            **self.request(),
            "references": [{"kind": "document", "ref": "😀" * 2048} for _ in range(32)],
        }
        self.rejects(value)
        value["references"][-1]["ref"] = ""
        remaining = 262144 - len(validation.encode_object(value))
        value["references"][-1]["ref"] = "😀" * (remaining // 4) + "x" * (remaining % 4)
        raw = validation.freeze_write(value)
        self.assertEqual(len(raw), 262144)
        self.assertEqual(validation.read_write(io.BytesIO(raw)), value)
        value["references"][-1]["ref"] += "x"
        self.rejects(value)
        for raw in (
            b"\xff",
            b"{} {}",
            b"[]",
            b'{"a":NaN}',
            b'{"a":1e999}',
            b'{"a":"\\ud800"}',
            b'{"a":1,"a":2}',
            b'{"a":' * 9 + b"0" + b"}" * 9,
            b"\xef\xbb\xbf{}",
            b" " * 262145,
        ):
            with self.assertRaises(validation.NoteError):
                validation.read_write(io.BytesIO(raw))

        class Stream(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                if size != 262145:
                    raise AssertionError("bounded read required")
                return super().read(size)

        self.assertEqual(
            validation.read_write(Stream(validation.encode_object(self.request()))),
            self.request(),
        )
        with (
            mock.patch.object(Stream, "read", return_value="not binary"),
            self.assertRaises(validation.NoteError),
        ):
            validation.read_write(Stream())

    def test_receipt_budget_duplicates_and_new_codes_stay_outside_configure(self):
        request = self.request()
        receipt = {
            "operation_id": "op:1",
            "note_ref": "ref:1",
            "note_id": "note_x",
            "revision": 1,
            "outcome": "applied",
        }
        raw = validation.encode_object(receipt)
        self.assertEqual(
            validation.validate_receipt(raw + b" " * (4096 - len(raw)), 201, request),
            receipt,
        )
        for candidate in (
            raw + b" " * (4097 - len(raw)),
            b'{"a":1,"a":2}',
            b'{"a":"\\ud800"}',
        ):
            with self.assertRaises(validation.NoteError) as raised:
                validation.validate_receipt(candidate, 201, request)
            self.assertEqual(raised.exception.code, "invalid_ack")
        for code in ("invalid_note", "invalid_ack"):
            self.assertNotIn(code, validation.ERROR_CODES)
            self.assertEqual(validation.NoteError(code).code, code)


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
