from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from test_note_store import NoteStoreTest

from devdiary import note_erasure, note_store
from devdiary.note_validation import NoteError


class NoteErasureTest(NoteStoreTest):
    def seed(self, store, *, cleanup="discarded"):
        sentinel = b"ERASURE_SYNTHETIC_OVERFLOW_9b613a_" * 3000
        other = b"OTHER_SYNTHETIC_NOTE_REMAINS_6723"
        with store.transaction():
            store.connection.execute(
                "INSERT INTO notes(note_ref,cleanup) VALUES ('ref:erase',?)", (cleanup,)
            )
            store.connection.execute("INSERT INTO notes(note_ref) VALUES ('ref:other')")
            for ref, body in (("ref:erase", sentinel), ("ref:other", other)):
                store.connection.execute(
                    "INSERT INTO note_operations(operation_id,note_ref,action,state,origin,subject_ref,workspace_ref,endpoint_path,key_file,draft,wire,wire_sha256) VALUES (?,?, 'create','queued',?,?,?,?,?,?,?,?)",
                    ("op:" + ref, ref, *store.binding(), body, body, "fixture"),
                )
        return sentinel, other

    def test_all_payload_fields_controlled_tmp_home_log_inventory(self):
        sentinels = [
            b"INVENTORY_CONTENT_159d",
            b"INVENTORY_NEXT_159d",
            b"INVENTORY_SOURCE_159d",
            b"INVENTORY_REF_159d",
        ]
        with note_store.Store.open(self.state) as store:
            queued = store.enqueue(
                "create",
                {
                    "attributes": {
                        "content": sentinels[0].decode(),
                        "next_step": sentinels[1].decode(),
                        "source": sentinels[2].decode(),
                    },
                    "references": [
                        {"kind": "document", "ref": "vault:" + sentinels[3].decode()}
                    ],
                },
            )
            other = store.enqueue(
                "create", {"attributes": {"content": "INVENTORY_OTHER_RETAINED"}}
            )
            store.enqueue(
                "update",
                {
                    "note_ref": queued["note_ref"],
                    "attributes": {"content": "INVENTORY_PENDING_SUFFIX_159d"},
                },
            )
            store.freeze(queued["seq"])
            store.acknowledge(
                queued["seq"],
                {
                    "operation_id": queued["operation_id"],
                    "note_ref": queued["note_ref"],
                    "note_id": "note_inventory",
                    "revision": 42,
                    "outcome": "deleted",
                },
                200,
            )
            self.assertIn(other["seq"], store.heads())
        with note_store.Store.open(self.state):
            pass
        roots = [self.root]
        logs = []
        if os.environ.get("DEVDIARY_OUTBOX_QUALIFICATION") == "1":
            runtime = Path(tempfile.gettempdir()).resolve().parent
            roots.extend((runtime / "home", runtime / "tmp"))
            logs.extend(
                (runtime.parent / "implementation" / "worker-assurance-r1").glob(
                    "*.log"
                )
            )
        files = set(logs)
        for root in roots:
            files.update(
                path
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            )
        for path in files:
            raw = path.read_bytes()
            for sentinel in (*sentinels, b"INVENTORY_PENDING_SUFFIX_159d"):
                self.assertNotIn(sentinel, raw, path.name)
        print(
            "inventory-evidence",
            len(files),
            "controlled-reachable-DB-sidecar-privateTMP-HOME-logs/sentinels-absent/other-note-retained",
            flush=True,
        )

    def test_observable_write_sync_rename_unlink_and_close_faults_stay_pending(self):
        for fault in (
            "write",
            "fsync",
            "rename",
            "unlink",
            "close",
            "vacuum",
            "purge_sql",
            "final_dir_sync",
        ):
            with self.subTest(fault=fault):
                case = self.root / fault
                case.mkdir(mode=0o700)
                state = case / "state"
                from devdiary.note_profile import create_profile

                create_profile(state, self.profile)
                with note_store.Store.open(state) as store:
                    if fault == "vacuum":
                        store.connection.execute("PRAGMA auto_vacuum=NONE")
                        store.connection.execute("VACUUM")
                    sentinel, other = self.seed(store)
                    if fault in ("vacuum", "purge_sql"):
                        connection = store.connection

                        def execute_failure(
                            statement, parameters=(), connection=connection, fault=fault
                        ):
                            if (fault == "vacuum" and statement == "VACUUM") or (
                                fault == "purge_sql"
                                and statement.startswith(
                                    "UPDATE note_operations SET draft=NULL"
                                )
                            ):
                                raise sqlite3.OperationalError(
                                    "synthetic detectable SQL failure"
                                )
                            return connection.execute(statement, parameters)

                        proxy = mock.Mock(wraps=connection)
                        proxy.execute.side_effect = execute_failure
                        target = mock.patch.object(store, "connection", proxy)
                    elif fault == "final_dir_sync":
                        sync = os.fsync
                        failed = []

                        def late_sync(fd, store=store, sync=sync, failed=failed):
                            if (
                                fd == store.directory_fd
                                and not store.exists(store.intent_name)
                                and not failed
                            ):
                                failed.append(True)
                                raise OSError("synthetic final directory sync failure")
                            return sync(fd)

                        target = mock.patch.object(
                            note_erasure.os, "fsync", side_effect=late_sync
                        )
                    elif fault == "close":
                        original = store.close_connection

                        def close_failure(original=original):
                            original()
                            raise sqlite3.OperationalError(
                                "synthetic observable close failure"
                            )

                        target = mock.patch.object(
                            store, "close_connection", side_effect=close_failure
                        )
                    else:
                        target = mock.patch.object(
                            note_erasure.os,
                            fault,
                            side_effect=OSError("synthetic detectable fault"),
                        )
                    with target:
                        with self.assertRaises(NoteError) as raised:
                            note_erasure.recover(store)
                        self.assertEqual(raised.exception.code, "purge_pending")
                        if store.connected:
                            self.assertNotEqual(
                                store.connection.execute(
                                    "SELECT cleanup FROM notes WHERE note_ref='ref:erase'"
                                ).fetchone()[0],
                                "",
                            )
                with note_store.Store.open(state) as store:
                    self.assertEqual(
                        store.connection.execute(
                            "SELECT draft FROM note_operations WHERE note_ref='ref:other'"
                        ).fetchone()[0],
                        other,
                    )
                    self.assertEqual(store.status()[0]["cleanup_pending"], 0)
                for path in case.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(sentinel[:40], path.read_bytes())
                print(
                    "fault-evidence",
                    fault,
                    "purge_pending/recovered/other-note-preserved",
                    flush=True,
                )

    def test_deleted_ack_any_action_purges_suffix_and_prevents_resurrection(self):
        with note_store.Store.open(self.state) as store:
            created = store.enqueue(
                "create", {"attributes": {"content": "TERMINAL_SYNTHETIC"}}
            )
            ref = created["note_ref"]
            store.enqueue(
                "update",
                {
                    "note_ref": ref,
                    "attributes": {"content": "BLOCKED_SUFFIX_SYNTHETIC"},
                    "references": [
                        {"kind": "document", "ref": "vault:TERMINAL_REF_SYNTHETIC.md"}
                    ],
                },
            )
            other = store.enqueue(
                "create", {"attributes": {"content": "UNRELATED_SYNTHETIC"}}
            )
            store.freeze(created["seq"])
            receipt = {
                "operation_id": created["operation_id"],
                "note_ref": ref,
                "note_id": "note_terminal",
                "revision": 9,
                "outcome": "deleted",
            }
            store.acknowledge(created["seq"], receipt, 200)
            self.assertEqual(store.heads(), [other["seq"]])
            with self.assertRaises(NoteError) as raised:
                store.enqueue(
                    "update",
                    {"note_ref": ref, "attributes": {"content": "resurrection"}},
                )
            self.assertEqual(raised.exception.code, "note_terminal")
            self.assertIsNone(store.freeze(created["seq"]))
        for path in self.state.iterdir():
            if path.is_file():
                raw = path.read_bytes()
                for sentinel in (
                    b"TERMINAL_SYNTHETIC",
                    b"BLOCKED_SUFFIX_SYNTHETIC",
                    b"TERMINAL_REF_SYNTHETIC",
                ):
                    self.assertNotIn(sentinel, raw)
        self.assertIn(
            b"UNRELATED_SYNTHETIC", (self.state / note_store.DB_NAME).read_bytes()
        )

    def test_physical_overflow_purge_reopen_other_note_and_intent(self):
        with note_store.Store.open(self.state) as store:
            sentinel, other = self.seed(store)
            self.assertIn(sentinel[:40], (self.state / note_store.DB_NAME).read_bytes())
            pages = store.connection.execute("PRAGMA page_count").fetchone()[0]
            self.assertGreater(pages, 10)
            with store.transaction():
                store.connection.execute(
                    "UPDATE note_operations SET wire=NULL WHERE note_ref='ref:erase'"
                )
                journal = self.state / (note_store.DB_NAME + "-journal")
                self.assertTrue(journal.is_file())
                self.assertGreater(journal.stat().st_size, 0)
            store.connection.execute(
                "SELECT length(draft) FROM note_operations ORDER BY substr(CAST(draft AS TEXT),1,16)"
            ).fetchall()
            self.assertEqual(
                store.connection.execute("PRAGMA temp_store").fetchone()[0], 2
            )
            note_erasure.recover(store)
            print(
                "physical-evidence",
                "ordinary-overflow-pages",
                pages,
                "DELETE-journal-observed/temp-memory-policy/purge-close-sync-reopen",
                flush=True,
            )
            self.assertFalse((self.state / note_store.INTENT_NAME).exists())
        with note_store.Store.open(self.state) as store:
            row = store.connection.execute(
                "SELECT draft,wire FROM note_operations WHERE note_ref='ref:erase'"
            ).fetchone()
            self.assertEqual(tuple(row), (None, None))
            self.assertEqual(
                store.connection.execute(
                    "SELECT draft FROM note_operations WHERE note_ref='ref:other'"
                ).fetchone()[0],
                other,
            )
            self.assertEqual(
                store.connection.execute("PRAGMA freelist_count").fetchone()[0], 0
            )
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(sentinel[:40], path.read_bytes(), path.name)

    def test_pending_ack_gap_and_detectable_sync_failure_recover_before_open(self):
        with note_store.Store.open(self.state) as store:
            sentinel, _ = self.seed(store)
            with mock.patch.object(
                store, "sync", side_effect=OSError("synthetic sync fault")
            ):
                with self.assertRaises(NoteError) as raised:
                    note_erasure.recover(store)
                self.assertEqual(raised.exception.code, "purge_pending")
        with note_store.Store.open(self.state) as store:
            self.assertEqual(
                store.connection.execute(
                    "SELECT cleanup FROM notes WHERE note_ref='ref:erase'"
                ).fetchone()[0],
                "",
            )
            self.assertIsNone(
                store.connection.execute(
                    "SELECT draft FROM note_operations WHERE note_ref='ref:erase'"
                ).fetchone()[0]
            )
        self.assertNotIn(sentinel[:40], (self.state / note_store.DB_NAME).read_bytes())

    def test_ack_scope_preserves_later_draft_and_invalid_marker_fails_closed(self):
        with note_store.Store.open(self.state) as store:
            self.seed(store, cleanup="ack")
            with store.transaction():
                store.connection.execute(
                    "UPDATE note_operations SET state='acknowledged',cleanup_pending=1 WHERE note_ref='ref:erase'"
                )
                store.connection.execute(
                    "INSERT INTO note_operations(operation_id,note_ref,action,state,origin,subject_ref,workspace_ref,endpoint_path,key_file,draft) VALUES ('op:later','ref:erase','update','queued',?,?,?,?,?,?)",
                    (*store.binding(), b"LATER_DRAFT"),
                )
            note_erasure.recover(store)
            self.assertEqual(
                store.connection.execute(
                    "SELECT draft FROM note_operations WHERE operation_id='op:later'"
                ).fetchone()[0],
                b"LATER_DRAFT",
            )
        marker = self.state / note_store.INTENT_NAME
        marker.write_bytes(b"{}")
        marker.chmod(0o600)
        with self.assertRaises(NoteError) as raised, note_store.Store.open(self.state):
            self.fail("invalid marker must prevent ordinary access")
        self.assertEqual(raised.exception.code, "purge_pending")

    def test_late_public_connection_close_failure_keeps_recovery_intent(self):
        original = note_store.Store.close_connection
        failed = []

        def late_close(store):
            original(store)
            if (
                store.cleanup_started
                and not store.exists(store.intent_name)
                and not failed
            ):
                failed.append(True)
                raise sqlite3.OperationalError("synthetic final public close failure")

        with (
            mock.patch.object(note_store.Store, "close_connection", late_close),
            self.assertRaises(NoteError) as raised,
            note_store.Store.open(self.state) as store,
        ):
            self.seed(store)
            note_erasure.recover(store)
        self.assertEqual(raised.exception.code, "purge_pending")
        self.assertTrue((self.state / note_store.INTENT_NAME).exists())
        with note_store.Store.open(self.state) as store:
            self.assertEqual(
                store.connection.execute(
                    "SELECT draft FROM note_operations WHERE note_ref='ref:other'"
                ).fetchone()[0],
                b"OTHER_SYNTHETIC_NOTE_REMAINS_6723",
            )
        self.assertFalse((self.state / note_store.INTENT_NAME).exists())

    def test_different_note_temp_intent_is_not_overwritten(self):
        from devdiary.note_validation import encode_object

        with note_store.Store.open(self.state) as store:
            self.seed(store)
            raw = encode_object(note_erasure._intent(store, "ref:other"))
        path = self.state / note_store.INTENT_TEMP
        path.write_bytes(raw)
        path.chmod(0o600)
        with self.assertRaises(NoteError) as raised, note_store.Store.open(self.state):
            self.fail("different valid erasure intent overwritten")
        self.assertEqual(raised.exception.code, "purge_pending")
        self.assertEqual(path.read_bytes(), raw)

    def test_busy_wal_checkpoint_fails_closed_without_live_unlink(self):
        with note_store.Store.open(self.state) as store:
            store.enqueue("create", {"attributes": {"content": "busy-wal-synthetic"}})
        connection = sqlite3.connect(
            self.state / note_store.DB_NAME, isolation_level=None
        )
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE notes SET revision=revision")
            wal = self.state / (note_store.DB_NAME + "-wal")
            self.assertTrue(wal.exists())
            with (
                self.assertRaises(NoteError) as raised,
                note_store.Store.open(self.state),
            ):
                self.fail("busy WAL must not be coerced")
            self.assertEqual(raised.exception.code, "purge_pending")
            self.assertTrue(wal.exists())
        finally:
            connection.rollback()
            connection.close()
        with note_store.Store.open(self.state):
            pass
        print(
            "fault-evidence",
            "busy-WAL-checkpoint/refused/no-live-unlink/reopen",
            flush=True,
        )

    def test_controlled_wal_transition_and_historical_dirty_freelist(self):
        with note_store.Store.open(self.state) as store:
            store.connection.execute("PRAGMA auto_vacuum=NONE")
            store.connection.execute("VACUUM")
            self.assertEqual(
                store.connection.execute("PRAGMA auto_vacuum").fetchone()[0], 0
            )
            self.seed(store)
        database = self.state / note_store.DB_NAME
        connection = sqlite3.connect(database, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA secure_delete=OFF")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute(
                "UPDATE note_operations SET draft=NULL,wire=NULL WHERE note_ref='ref:erase'"
            )
            self.assertTrue((self.state / (note_store.DB_NAME + "-wal")).exists())
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]
            self.assertGreater(free_pages, 0)
        finally:
            connection.close()
        self.assertIn(b"ERASURE_SYNTHETIC_OVERFLOW_9b613a_", database.read_bytes())
        with note_store.Store.open(self.state) as store:
            self.assertEqual(
                store.connection.execute("PRAGMA freelist_count").fetchone()[0], 0
            )
            self.assertEqual(
                store.connection.execute("PRAGMA journal_mode").fetchone()[0], "delete"
            )
        for path in self.state.iterdir():
            if path.is_file():
                self.assertNotIn(
                    b"ERASURE_SYNTHETIC_OVERFLOW_9b613a_", path.read_bytes()
                )
        self.assertFalse((self.state / (note_store.DB_NAME + "-wal")).exists())
        self.assertFalse((self.state / (note_store.DB_NAME + "-shm")).exists())
        print(
            "physical-evidence",
            "historical-OFF-freelist-pages",
            free_pages,
            "old-sentinel-observed/VACUUM-as-needed/reopen-clean",
            flush=True,
        )
