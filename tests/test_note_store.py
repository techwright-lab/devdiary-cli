from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devdiary import note_store
from devdiary.note_profile import BoundProfile, create_profile
from devdiary.note_validation import NoteError


class NoteTempPolicyTest(unittest.TestCase):
    def test_effective_memory_policy_matrix(self):
        for compile_value, readback in ((1, 2), (2, 2), (3, 0), (3, 1), (3, 2)):
            note_store._require_memory_temp_policy(
                ["THREADSAFE=1", "TEMP_STORE=" + str(compile_value)], readback
            )
        for options, readback in (
            (["TEMP_STORE=0"], 2),
            (["TEMP_STORE=1"], 0),
            (["TEMP_STORE=1"], 1),
            (["TEMP_STORE=2"], 1),
            (["TEMP_STORE=3"], 3),
            (["TEMP_STORE=1"], True),
            (["TEMP_STORE=2"], 2.0),
            (["TEMP_STORE=4"], 2),
            (["TEMP_STORE=1", "OMIT_MEMORYDB"], 2),
            ([], 2),
            (["TEMP_STORE=1", "TEMP_STORE=1"], 2),
            (["TEMP_STORE=1", "TEMP_STORE=2"], 2),
            (["TEMP_STORE"], 2),
        ):
            with self.assertRaises(NoteError) as raised:
                note_store._require_memory_temp_policy(options, readback)
            self.assertEqual(raised.exception.code, "platform_unsupported")


class NoteStoreTest(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("POSIX notes writes are unsupported")
        if os.environ.get("DEVDIARY_OUTBOX_QUALIFICATION") == "1":
            self.assertTrue(
                getattr(sys, "_devdiary_qualification_network_guard", False)
            )
        self.temp = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.profile = BoundProfile(
            schema_version=1,
            origin="https://example.test",
            subject_ref="12345678-1234-abcd-ABCD-123456789abc",
            workspace_ref="workspace_A",
            capability="notes:write",
            endpoint_path="/ingest/v1/notes",
            scope_path="/ingest/v1/notes/scope",
            key_file=str(self.root / "key"),
        )
        create_profile(self.state, self.profile)

    def test_compile_policy_refusal_precedes_input_and_preserves_accepted_id(self):
        import io

        from devdiary import notes

        with note_store.Store.open(self.state) as store:
            accepted = store.enqueue(
                "create", {"attributes": {"content": "accepted policy fixture"}}
            )
        connect = sqlite3.connect

        class Unreadable(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise AssertionError("policy admission must precede input/enqueue")

        for options in (
            ["TEMP_STORE=0"],
            ["TEMP_STORE=1", "OMIT_MEMORYDB"],
            [],
            ["TEMP_STORE=1", "TEMP_STORE=2"],
        ):

            def connect_proxy(*args, options=options, **kwargs):
                connection = connect(*args, **kwargs)
                proxy = mock.Mock(wraps=connection)

                def execute(statement, parameters=()):
                    if statement == "PRAGMA compile_options":
                        cursor = mock.Mock()
                        cursor.fetchall.return_value = [(option,) for option in options]
                        return cursor
                    return connection.execute(statement, parameters)

                proxy.execute.side_effect = execute
                return proxy

            with (
                mock.patch.object(
                    note_store.sqlite3, "connect", side_effect=connect_proxy
                ),
                self.assertRaises(NoteError) as raised,
            ):
                notes.local_command(self.state, "create", Unreadable())
            self.assertEqual(raised.exception.code, "platform_unsupported")
            with note_store.Store.open(self.state) as store:
                rows = store.status()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["operation_id"], accepted["operation_id"])
        print(
            "temp-policy-negative-evidence",
            "compile0/OMIT_MEMORYDB/absent/ambiguous refused before input; previous-ID-intact",
            flush=True,
        )

    def test_sqlite_full_rolls_back_without_losing_accepted_intent(self):
        with note_store.Store.open(self.state) as store:
            accepted = store.enqueue(
                "create", {"attributes": {"content": "accepted before full"}}
            )
        with (
            self.assertRaises(NoteError) as raised,
            note_store.Store.open(self.state) as store,
        ):
            pages = store.connection.execute("PRAGMA page_count").fetchone()[0]
            store.connection.execute("PRAGMA max_page_count=" + str(pages))
            store.enqueue("create", {"attributes": {"content": "x" * 16384}})
        self.assertEqual(raised.exception.code, "store_failed")
        with note_store.Store.open(self.state) as store:
            rows = store.status()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["operation_id"], accepted["operation_id"])
        print(
            "fault-evidence", "SQLITE_FULL/rollback/previous-intent-intact", flush=True
        )

    def test_profile_change_cannot_retarget_and_hardlink_lock_refused(self):
        with note_store.Store.open(self.state) as store:
            queued = store.enqueue("create", {"attributes": {"content": "bound A"}})
        path = self.state / "notes-profile.json"
        original = path.read_bytes()
        value = json.loads(original)
        value["workspace_ref"] = "workspace_B"
        path.write_bytes(json.dumps(value).encode())
        with self.assertRaises(NoteError) as raised, note_store.Store.open(self.state):
            self.fail("profile retarget accepted")
        self.assertEqual(raised.exception.code, "binding_mismatch")
        path.write_bytes(original)
        with note_store.Store.open(self.state) as store:
            self.assertEqual(store.status()[0]["operation_id"], queued["operation_id"])
        linked = self.root / "lock-link"
        linked.hardlink_to(self.state / "notes.lock")
        with self.assertRaises(NoteError) as raised, note_store.Store.open(self.state):
            self.fail("multiply-linked lock admitted")
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_exact_operation_and_byte_caps_freeze_expansion_metadata_page(self):
        with note_store.Store.open(self.state) as store:
            with store.transaction():
                store.connection.execute(
                    "INSERT INTO notes(note_ref) VALUES ('ref:capacity')"
                )
                store.connection.executemany(
                    "INSERT INTO note_operations(operation_id,note_ref,action,state,origin,subject_ref,workspace_ref,endpoint_path,key_file,draft) VALUES (?,'ref:capacity','create','queued',?,?,?,?,?,?)",
                    [
                        ("op:cap:" + str(index), *store.binding(), b"x")
                        for index in range(999)
                    ],
                )
            accepted = store.enqueue("create", {"attributes": {"content": "last"}})
            self.assertEqual(
                store.connection.execute(
                    "SELECT COUNT(*) FROM note_operations"
                ).fetchone()[0],
                1000,
            )
            with self.assertRaises(NoteError) as raised:
                store.enqueue("create", {"attributes": {"content": "overflow"}})
            self.assertEqual(raised.exception.code, "queue_full")
            queries = []
            store.connection.set_trace_callback(queries.append)
            try:
                self.assertEqual(len(store.status(limit=100)), 100)
            finally:
                store.connection.set_trace_callback(None)
            projection = next(query for query in queries if "JOIN notes" in query)
            for forbidden in ("draft", "wire", "key_file", "SELECT *", "json_"):
                self.assertNotIn(forbidden, projection)
            self.assertIn(
                "SEARCH o USING INTEGER PRIMARY KEY",
                str(
                    [
                        tuple(row)
                        for row in store.connection.execute(
                            "EXPLAIN QUERY PLAN SELECT o.seq FROM note_operations o WHERE o.seq>? ORDER BY o.seq LIMIT ?",
                            (0, 100),
                        )
                    ]
                ),
            )
            with self.assertRaises(NoteError):
                store.status(limit=True)
            with mock.patch.object(note_store, "MAX_OPERATIONS", 2000):
                with store.transaction():
                    store.connection.execute("UPDATE note_operations SET draft=NULL")
                    store.connection.executemany(
                        "UPDATE note_operations SET draft=zeroblob(262144) WHERE seq=?",
                        [(index,) for index in range(1, 257)],
                    )
                self.assertEqual(
                    store.connection.execute(
                        "SELECT SUM(length(draft)) FROM note_operations"
                    ).fetchone()[0],
                    64 * 1024 * 1024,
                )
                with self.assertRaises(NoteError):
                    store.enqueue(
                        "create", {"attributes": {"content": "byte overflow"}}
                    )
                with store.transaction():
                    draft = b'{"attributes":{"content":"freeze"}}'
                    store.connection.execute(
                        "UPDATE note_operations SET draft=zeroblob(?) WHERE seq=1",
                        (262144 - len(draft),),
                    )
                    store.connection.execute(
                        "UPDATE note_operations SET draft=? WHERE seq=?",
                        (draft, accepted["seq"]),
                    )
                with self.assertRaises(NoteError):
                    store.freeze(accepted["seq"])
                self.assertIsNotNone(
                    store.connection.execute(
                        "SELECT draft FROM note_operations WHERE seq=?",
                        (accepted["seq"],),
                    ).fetchone()[0]
                )
            print(
                "capacity-evidence",
                "1000/1001-and-64MiB/freeze-expansion-rollback/metadata-page100-indexed",
                flush=True,
            )

    def test_ordering_freeze_reopen_ack_and_caps(self):
        with note_store.Store.open(self.state) as store:
            first = store.enqueue(
                "create", {"attributes": {"content": "CREATE_SYNTHETIC"}}
            )
            ref = first["note_ref"]
            second = store.enqueue(
                "update", {"note_ref": ref, "attributes": {"content": "EDIT_ONE"}}
            )
            third = store.enqueue(
                "update", {"note_ref": ref, "attributes": {"content": "EDIT_TWO"}}
            )
            self.assertIsNone(store.freeze(second["seq"]))
            head = store.freeze(first["seq"])
            assert head is not None
            self.assertIsNone(head["expected_revision"])
            raw = head["wire"]
        with note_store.Store.open(self.state) as store:
            replay = store.freeze(first["seq"])
            assert replay is not None
            self.assertEqual(replay["wire"], raw)
            store.acknowledge(
                first["seq"],
                {
                    "operation_id": first["operation_id"],
                    "note_ref": ref,
                    "note_id": "note_synthetic",
                    "revision": 1,
                    "outcome": "applied",
                },
                201,
            )
            update = store.freeze(second["seq"])
            assert update is not None
            self.assertEqual(update["expected_revision"], 1)
            self.assertIsNone(store.freeze(third["seq"]))
            self.assertNotIn(
                b"CREATE_SYNTHETIC", (self.state / note_store.DB_NAME).read_bytes()
            )
            self.assertIn(b"EDIT_TWO", (self.state / note_store.DB_NAME).read_bytes())
            with mock.patch.object(note_store, "MAX_OPERATIONS", 2):
                with self.assertRaises(NoteError) as raised:
                    store.enqueue("create", {"attributes": {"content": "full"}})
                self.assertEqual(raised.exception.code, "queue_full")
            with (
                mock.patch.object(note_store, "MAX_BYTES", 1),
                self.assertRaises(NoteError),
            ):
                store.enqueue("create", {"attributes": {"content": "full"}})
            self.assertEqual(len(store.status()), 3)

    def test_path_foreign_binding_and_single_writer_refusal(self):
        with note_store.Store.open(self.state):
            with (
                self.assertRaises(NoteError) as raised,
                note_store.Store.open(self.state),
            ):
                self.fail("concurrent writer admitted")
            self.assertEqual(raised.exception.code, "store_busy")
        database = self.state / note_store.DB_NAME
        database.unlink()
        database.write_bytes(b"")
        database.chmod(0o600)
        with self.assertRaises(NoteError) as raised, note_store.Store.open(self.state):
            self.fail("arbitrary empty database adopted")
        self.assertEqual(raised.exception.code, "store_corrupt")
        database.unlink()
        with note_store.Store.open(self.state):
            pass
        sidecar = self.state / (note_store.DB_NAME + "-journal")
        sidecar.symlink_to(database)
        with self.assertRaises(NoteError) as raised, note_store.Store.open(self.state):
            self.fail("unsafe sidecar admitted")
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_private_schema_binding_and_public_sqlite_policy(self):
        with note_store.Store.open(self.state) as store:
            connection = store.connection
            self.assertEqual(
                connection.execute("PRAGMA application_id").fetchone()[0], 0x44444E31
            )
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            for pragma, expected in (
                ("secure_delete", 1),
                ("synchronous", 2),
                ("foreign_keys", 1),
                ("journal_mode", "delete"),
                ("temp_store", 2),
            ):
                self.assertEqual(
                    connection.execute("PRAGMA " + pragma).fetchone()[0], expected
                )
            self.assertEqual(
                {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                },
                {"note_store_binding", "notes", "note_operations"},
            )
            row = connection.execute(
                "SELECT origin, subject_ref, workspace_ref, endpoint_path, key_file FROM note_store_binding"
            ).fetchone()
            self.assertEqual(
                tuple(row),
                (
                    self.profile.origin,
                    self.profile.subject_ref,
                    self.profile.workspace_ref,
                    self.profile.endpoint_path,
                    self.profile.key_file,
                ),
            )
            source_id = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
            options = [row[0] for row in connection.execute("PRAGMA compile_options")]
            self.assertTrue(source_id)
            self.assertTrue(options)
            print(
                "sqlite-public-policy-evidence",
                source_id,
                options,
                "secure_delete=1 synchronous=2 journal_mode=delete foreign_keys=1 temp_store=2",
                flush=True,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO notes(note_ref,revision) VALUES ('bad',-1)"
                )
        for name in ("notes.sqlite3", "notes.lock"):
            self.assertEqual((self.state / name).stat().st_mode & 0o777, 0o600)
        inode = (self.state / "notes.lock").stat().st_ino
        with note_store.Store.open(self.state):
            pass
        self.assertEqual((self.state / "notes.lock").stat().st_ino, inode)
