import io
import json
import unittest
from pathlib import Path
from unittest import mock

from devdiary import note_import
from devdiary.note_profile import BoundProfile
from devdiary.note_validation import NoteError

FIXTURE = Path(__file__).parent / "fixtures" / "note_journal_selected.json"
NAMESPACE = "urn:devdiary:journal:6f77999a-4d3c-4b34-9401-12d65216c533"
NOTE_REF = "note-import:9a0dfb7d-3f6f-54d1-9277-d8f30cf6ba32"
OPERATION_ID = "note-op:bb7baeba-9577-5540-a74a-5de03763ebe9"
ENTRY_SHA = "b57a80d45f8c083d001d99869ed36b118e860d3cbcdac15235b85b694c7a85ed"
CLOCK_SHA = "c44e932ee30ed4d4818dbe4cc44a2114eccec04dea6d68b756c6bd0b5104f6b1"
SELECTION_SHA = "145442f179b148d98a3e5a59199c912a0db927406f82ab33762059ae9c04cffb"


def profile(
    *,
    origin: str = "https://notes.invalid",
    subject_ref: str = "00000000-0000-4000-8000-000000000001",
    workspace_ref: str = "workspace_task5a",
    key_file: str = "/synthetic/notes-key",
) -> BoundProfile:
    return BoundProfile(
        schema_version=1,
        origin=origin,
        subject_ref=subject_ref,
        workspace_ref=workspace_ref,
        capability="notes:write",
        endpoint_path="/ingest/v1/notes",
        scope_path="/ingest/v1/notes/scope",
        key_file=key_file,
    )


def manifest():
    value = json.loads(FIXTURE.read_bytes())
    value["entries"] = value["entries"][:1]
    return value


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class NoteImportTest(unittest.TestCase):
    def rejects_raw(self, raw: bytes):
        with self.assertRaises(NoteError) as raised:
            note_import.parse_manifest(raw, profile())
        self.assertEqual(raised.exception.code, "invalid_import")
        self.assertEqual(str(raised.exception), "invalid_import")
        self.assertIsNone(raised.exception.__cause__)

    def test_parser_and_returned_values_do_not_use_external_io(self):
        raw = encoded(manifest())
        binding = profile()
        denied = AssertionError("pure_import_performed_io")
        with (
            mock.patch("builtins.open", side_effect=denied),
            mock.patch("io.open", side_effect=denied),
            mock.patch("os.open", side_effect=denied),
            mock.patch("socket.create_connection", side_effect=denied),
            mock.patch("subprocess.Popen", side_effect=denied),
        ):
            selection = note_import.parse_manifest(raw, binding)
            metadata = selection.metadata()
            intent = selection.entries[0].create_intent()
        self.assertEqual(metadata["entries"][0]["note_ref"], NOTE_REF)
        self.assertEqual(intent["attributes"]["content"], "Synthetic checkpoint.")

    def test_character_boundaries_preserve_unicode_without_fabricating_times(self):
        value = manifest()
        entry = value["entries"][0]
        entry["content"] = "🌕" * 16384
        entry["next_step"] = "✓" * 4096
        entry["references"] = [{"kind": "document", "ref": "vault:" + "x" * 2042}]
        result = note_import.parse_manifest(encoded(value), profile())
        intent = result.entries[0].create_intent()
        self.assertEqual(intent["attributes"]["content"], entry["content"])
        self.assertEqual(intent["attributes"]["next_step"], entry["next_step"])
        self.assertNotIn("occurred_at", intent["attributes"])
        self.assertNotIn("created_at", intent["attributes"])
        entry["references"][0]["ref"] += "x"
        self.rejects_raw(encoded(value))

    def test_normalized_final_wire_has_its_own_byte_budget(self):
        value = manifest()
        entry = value["entries"][0]
        entry["content"] = "🌕" * 16384
        entry["references"] = [
            {"kind": "document", "ref": "vault:" + "文" * 2042} for _ in range(32)
        ]
        trim = (max(0, len(encoded(value)) - 262044) + 2) // 3
        self.assertLess(trim, 2042)
        entry["references"][-1]["ref"] = "vault:" + "文" * (2042 - trim)
        raw = encoded(value)
        self.assertLessEqual(len(raw), 262144)
        normalized = {
            "action": "create",
            "note_ref": NOTE_REF,
            "operation_id": OPERATION_ID,
            "attributes": {
                "content": entry["content"],
                "next_step": None,
                "source": "journal-selected-v1",
                "recorded_at": entry["recorded_at"],
            },
            "references": [
                {**ref, "relation": "context"} for ref in entry["references"]
            ],
        }
        self.assertGreater(len(encoded(normalized)), 262144)
        self.rejects_raw(raw)

    def test_prose_and_clock_changes_keep_identity_but_change_their_digests(self):
        value = manifest()
        value["entries"][0]["content"] = "  Synthetic Å and A\u030a.  "
        selection = note_import.parse_manifest(encoded(value), profile())
        row = selection.metadata()["entries"][0]
        self.assertEqual(row["note_ref"], NOTE_REF)
        self.assertNotEqual(row["entry_sha256"], ENTRY_SHA)
        self.assertEqual(row["recorded_at_sha256"], CLOCK_SHA)
        self.assertEqual(
            selection.entries[0].create_intent()["attributes"]["content"],
            value["entries"][0]["content"],
        )
        value = manifest()
        value["entries"][0]["recorded_at"] = "2026-11-05T12:47:22+00:00"
        other = note_import.parse_manifest(encoded(value), profile())
        self.assertNotEqual(
            other.metadata()["entries"][0]["recorded_at_sha256"],
            row["recorded_at_sha256"],
        )
        self.assertEqual(
            other.entries[0].create_intent()["attributes"]["recorded_at"],
            "2026-11-05T12:47:22+00:00",
        )

    def test_object_order_is_normalized_but_selection_and_reference_order_are_not(self):
        value = manifest()
        normal = note_import.parse_manifest(encoded(value), profile()).metadata()
        reordered = json.loads(json.dumps(value, sort_keys=True, indent=2))
        self.assertEqual(
            note_import.parse_manifest(encoded(reordered), profile()).metadata(), normal
        )
        full = json.loads(FIXTURE.read_bytes())
        before = note_import.parse_manifest(encoded(full), profile()).metadata()
        full["entries"].reverse()
        after = note_import.parse_manifest(encoded(full), profile()).metadata()
        self.assertEqual(after["entries"], list(reversed(before["entries"])))
        self.assertNotEqual(after["selection_sha256"], before["selection_sha256"])
        references = [
            {"kind": "document", "ref": "vault:first.md"},
            {"kind": "note", "ref": "note_synthetic", "relation": "follows_from"},
            {"kind": "document", "ref": "vault:first.md"},
        ]
        value["entries"][0]["references"] = references
        selected = note_import.parse_manifest(encoded(value), profile())
        self.assertEqual(
            selected.entries[0].create_intent()["references"],
            [
                {"kind": "document", "ref": "vault:first.md", "relation": "context"},
                references[1],
                {"kind": "document", "ref": "vault:first.md", "relation": "context"},
            ],
        )
        references.reverse()
        # Use a genuinely different order, not the symmetric duplicated list.
        references[0], references[1] = references[1], references[0]
        changed = note_import.parse_manifest(encoded(value), profile())
        self.assertNotEqual(
            changed.metadata()["entries"][0]["entry_sha256"],
            selected.metadata()["entries"][0]["entry_sha256"],
        )

    def test_strict_raw_json_and_exact_byte_limit_use_the_existing_safe_decoder(self):
        raw = encoded(manifest())
        exact = raw + b" " * (262144 - len(raw))
        self.assertEqual(
            note_import.parse_manifest(exact, profile()).metadata()["selection_sha256"],
            SELECTION_SHA,
        )
        for candidate in (
            exact + b" ",
            b"\xff",
            b"[]",
            b"null",
            b'{"schema_version":1,"schema_version":1}',
            b'{"x":NaN}',
            b'{"x":Infinity}',
            b'{"x":1e9999}',
            b'{"x":"\\ud800"}',
            b'{"x":' + b"[" * 9 + b"0" + b"]" * 9 + b"}",
        ):
            with self.subTest(length=len(candidate)):
                self.rejects_raw(candidate)
        # A real typed binary stream supplies deliberate non-bytes faults.
        for value in ("not bytes", None, 1, bytearray(raw)):
            with (
                io.BytesIO(raw) as stream,
                mock.patch.object(stream, "read", return_value=value),
            ):
                candidate = stream.read()
                self.assertIs(type(candidate), type(value))
                self.rejects_raw(candidate)

    def test_all_frozen_identity_vectors_and_same_text_distinct_entries(self):
        cases = (
            (
                profile(),
                NAMESPACE,
                "synthetic-research-summary",
                NOTE_REF,
                OPERATION_ID,
            ),
            (
                profile(),
                NAMESPACE,
                "synthetic-research-summary-2",
                "note-import:0735cbf8-9e4f-56b0-bfe2-47145d4c1a3e",
                "note-op:77f69478-1dd7-51a7-98be-cfc4b729c143",
            ),
            (
                profile(subject_ref="00000000-0000-4000-8000-000000000002"),
                NAMESPACE,
                "synthetic-research-summary",
                "note-import:55f8d7c6-0062-58c0-ba1c-770480a70736",
                "note-op:b4bea299-cb62-5cb6-8214-9b971c6d1ce1",
            ),
            (
                profile(workspace_ref="workspace_task5b"),
                NAMESPACE,
                "synthetic-research-summary",
                "note-import:f3cd4ab9-27ff-5149-85a5-650db302dec4",
                "note-op:e4e8b78d-064b-5c8c-9bc1-02817b93e943",
            ),
            (
                profile(),
                "urn:devdiary:journal:6f77999a-4d3c-4b34-9401-12d65216c534",
                "synthetic-research-summary",
                "note-import:b92e810d-ea79-5c07-8e2c-58d4dc67e7f0",
                "note-op:f1037a6d-0076-5f63-ab0a-6b04c615ad4a",
            ),
        )
        for binding, namespace, entry_id, note_ref, operation_id in cases:
            with self.subTest(entry_id=entry_id, namespace=namespace):
                value = manifest()
                value["source_namespace"] = namespace
                value["entries"][0]["entry_id"] = entry_id
                selection = note_import.parse_manifest(encoded(value), binding)
                row = selection.metadata()["entries"][0]
                self.assertEqual(row["note_ref"], note_ref)
                self.assertEqual(row["create_operation_id"], operation_id)
        full = json.loads(FIXTURE.read_bytes())
        selection = note_import.parse_manifest(encoded(full), profile())
        rows = selection.metadata()["entries"]
        self.assertEqual(rows[0]["note_ref"], cases[0][3])
        self.assertEqual(rows[1]["note_ref"], cases[1][3])
        self.assertNotEqual(rows[0]["note_ref"], rows[1]["note_ref"])
        third = selection.entries[2].create_intent()
        self.assertNotIn("recorded_at", third["attributes"])
        self.assertNotIn("occurred_at", third["attributes"])
        self.assertEqual(third["attributes"]["source"], "journal-selected-v1")
        self.assertEqual(third["references"][0]["relation"], "context")

    def test_profile_locator_and_origin_bind_selection_but_not_entry_identity(self):
        for binding in (
            profile(origin="https://other.invalid"),
            profile(key_file="/synthetic/rotated-locator"),
        ):
            with self.subTest(origin=binding.origin):
                metadata = note_import.parse_manifest(
                    encoded(manifest()), binding
                ).metadata()
                self.assertNotEqual(metadata["selection_sha256"], SELECTION_SHA)
                self.assertEqual(metadata["entries"][0]["note_ref"], NOTE_REF)
                self.assertEqual(metadata["entries"][0]["entry_sha256"], ENTRY_SHA)
                self.assertNotIn(binding.key_file, encoded(metadata).decode())

    def test_intents_and_metadata_are_fresh_and_selection_values_are_immutable(self):
        selection = note_import.parse_manifest(encoded(manifest()), profile())
        intent = selection.entries[0].create_intent()
        intent["attributes"]["content"] = "Tampered synthetic value."
        intent["references"].append({"kind": "document", "ref": "vault:tampered"})
        metadata = selection.metadata()
        metadata["entries"][0]["note_ref"] = "tampered"
        metadata["entries"].clear()
        self.assertEqual(
            selection.entries[0].create_intent()["attributes"]["content"],
            "Synthetic checkpoint.",
        )
        self.assertEqual(selection.entries[0].create_intent()["references"], [])
        self.assertEqual(selection.metadata()["entries"][0]["note_ref"], NOTE_REF)
        for target, attribute, replacement in (
            (selection, "entries", ()),
            (selection.entries[0], "note_ref", "tampered"),
        ):
            with self.assertRaises(AttributeError):
                setattr(target, attribute, replacement)

    def test_selected_values_and_reference_failures_have_only_safe_import_errors(self):
        base = manifest()["entries"][0]
        for field, value in (
            ("content", None),
            ("content", True),
            ("content", ""),
            ("content", " \t\n"),
            ("content", "x" * 16385),
            ("next_step", None),
            ("next_step", False),
            ("next_step", []),
            ("next_step", "x" * 4097),
            ("recorded_at", None),
            ("recorded_at", 1),
            ("recorded_at", "2026-02-30T12:00:00Z"),
            ("recorded_at", "2026-11-05"),
            ("references", None),
            ("references", {}),
            ("references", [None]),
            ("references", [{"kind": "document"}]),
            ("references", [{"kind": "actor", "ref": "synthetic"}]),
            (
                "references",
                [
                    {
                        "kind": "external",
                        "ref": "https://notes.invalid?token=synthetic-private",
                    }
                ],
            ),
            ("references", [{"kind": "document", "ref": "/synthetic/private.md"}]),
            ("references", [{"kind": "document", "ref": "vault:../private.md"}]),
            (
                "references",
                [{"kind": "document", "ref": "vault:source.md", "owner": "synthetic"}],
            ),
            ("references", [{"kind": "document", "ref": "vault:source.md"}] * 33),
        ):
            with self.subTest(field=field, value_type=type(value).__name__):
                self.rejects_raw(
                    encoded({**manifest(), "entries": [{**base, field: value}]})
                )

    def test_entry_identity_is_unique_bounded_ascii_and_fields_are_allowlisted(self):
        base = manifest()["entries"][0]
        for entry_id in (
            None,
            True,
            1,
            [],
            "",
            "a" * 101,
            "two words",
            "é",
            "line\nend",
        ):
            with self.subTest(entry_id=entry_id):
                value = {**manifest(), "entries": [{**base, "entry_id": entry_id}]}
                self.rejects_raw(encoded(value))
        for entry in (
            None,
            True,
            [],
            "entry",
            {},
            {"entry_id": "only-id"},
            {"content": "Only content."},
        ):
            with self.subTest(entry=entry):
                self.rejects_raw(encoded({**manifest(), "entries": [entry]}))
        for field in (
            "source",
            "context",
            "occurred_at",
            "created_at",
            "owner",
            "actor",
            "elapsed",
            "approval",
            "attributes",
        ):
            with self.subTest(field=field):
                self.rejects_raw(
                    encoded({**manifest(), "entries": [{**base, field: "synthetic"}]})
                )
        self.rejects_raw(
            encoded(
                {
                    **manifest(),
                    "entries": [base, {**base, "content": "Different synthetic text."}],
                }
            )
        )
        valid = [
            {**base, "entry_id": entry_id}
            for entry_id in ("a" * 100, "Case", "case", "_.:-0")
        ]
        result = note_import.parse_manifest(
            encoded({**manifest(), "entries": valid}), profile()
        )
        self.assertEqual(result.metadata()["count"], 4)
        self.assertEqual(
            len({row["note_ref"] for row in result.metadata()["entries"]}), 4
        )

    def test_selection_is_a_nonempty_list_of_at_most_one_hundred_entries(self):
        base = manifest()["entries"][0]
        hundred = [{**base, "entry_id": f"entry-{index}"} for index in range(100)]
        for entries in (
            None,
            True,
            {},
            "entries",
            [],
            hundred + [{**base, "entry_id": "entry-100"}],
        ):
            with self.subTest(entries_type=type(entries).__name__):
                self.rejects_raw(encoded({**manifest(), "entries": entries}))
        result = note_import.parse_manifest(
            encoded({**manifest(), "entries": hundred}), profile()
        )
        self.assertEqual(result.metadata()["count"], 100)
        self.assertEqual(
            len({row["note_ref"] for row in result.metadata()["entries"]}), 100
        )

    def test_source_namespace_is_a_canonical_uuid_urn_not_a_path(self):
        for namespace in (
            None,
            True,
            1,
            [],
            {},
            NAMESPACE.upper(),
            NAMESPACE + " ",
            NAMESPACE.replace("6f77999a", "6F77999A"),
            "6f77999a-4d3c-4b34-9401-12d65216c533",
            "/synthetic/journal.json",
            "urn:devdiary:journal:not-a-uuid",
            "urn:devdiary:journal:6f77999a4d3c4b34940112d65216c533",
        ):
            with self.subTest(namespace=namespace):
                self.rejects_raw(encoded({**manifest(), "source_namespace": namespace}))
        nil = "urn:devdiary:journal:00000000-0000-0000-0000-000000000000"
        result = note_import.parse_manifest(
            encoded({**manifest(), "source_namespace": nil}), profile()
        )
        self.assertEqual(result.metadata()["source_namespace"], nil)

    def test_manifest_has_exact_keys_and_integer_version(self):
        for version in (True, False, 1.0, None, "1", 0, 2):
            with self.subTest(version=version):
                self.rejects_raw(encoded({**manifest(), "schema_version": version}))
        value = manifest()
        del value["schema_version"]
        self.rejects_raw(encoded(value))
        for field in ("owner", "workspace_ref", "actor", "preview_digest", "source"):
            with self.subTest(field=field):
                self.rejects_raw(encoded({**manifest(), field: "synthetic-private"}))

    def test_metadata_matches_literal_digests_without_prose_or_key_locator(self):
        selection = note_import.parse_manifest(encoded(manifest()), profile())
        self.assertEqual(
            selection.metadata(),
            {
                "schema_version": 1,
                "source_namespace": NAMESPACE,
                "count": 1,
                "selection_sha256": SELECTION_SHA,
                "entries": [
                    {
                        "entry_id": "synthetic-research-summary",
                        "note_ref": NOTE_REF,
                        "create_operation_id": OPERATION_ID,
                        "entry_sha256": ENTRY_SHA,
                        "recorded_at_sha256": CLOCK_SHA,
                    }
                ],
            },
        )
        for output in (encoded(selection.metadata()).decode(), repr(selection)):
            self.assertNotIn("Synthetic checkpoint.", output)
            self.assertNotIn("/synthetic/notes-key", output)

    def test_valid_selected_entry_has_literal_identity_and_normalized_intent(self):
        selection = note_import.parse_manifest(encoded(manifest()), profile())
        self.assertIsNotNone(selection)
        metadata = selection.metadata()
        self.assertEqual(metadata["count"], 1)
        self.assertEqual(metadata["source_namespace"], NAMESPACE)
        entry = metadata["entries"][0]
        self.assertEqual(entry["entry_id"], "synthetic-research-summary")
        self.assertEqual(entry["note_ref"], NOTE_REF)
        self.assertEqual(entry["create_operation_id"], OPERATION_ID)
        self.assertEqual(
            selection.entries[0].create_intent(),
            {
                "attributes": {
                    "content": "Synthetic checkpoint.",
                    "next_step": None,
                    "source": "journal-selected-v1",
                    "recorded_at": "2026-11-05T12:47:22Z",
                },
                "references": [],
            },
        )
