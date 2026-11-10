from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import threading
import traceback
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_note_validation import scope

from devdiary import note_profile as profile
from devdiary import notes
from devdiary.cli import _configure_notes, main, parser
from devdiary.note_profile import require_bound_profile
from devdiary.note_validation import NoteError


class NoteOutboxTest(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("POSIX notes writes are unsupported")
        self.temp = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.key = self.root / "key"
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(("dn_live_" + "a" * 43).encode())
        profile.create_profile(
            self.state,
            profile.BoundProfile(
                origin="https://example.test", key_file=str(self.key), **scope()
            ),
        )

    def cli_command(
        self, command: str, value: dict, extra: tuple[str, ...] = ()
    ) -> tuple[int, dict]:
        output, error = io.StringIO(), io.StringIO()
        stream = SimpleNamespace(buffer=io.BytesIO(json.dumps(value).encode()))
        with (
            mock.patch("sys.stdin", stream),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            code = main(
                [
                    "--config",
                    "unused-root-prefix",
                    "notes",
                    command,
                    "--state-dir",
                    str(self.state),
                    *extra,
                ]
            )
        self.assertEqual(error.getvalue(), "")
        for private in (str(self.key), "dn_live_" + "a" * 43, "CLI_PRIVATE_CONTENT"):
            self.assertNotIn(private, output.getvalue())
        return code, json.loads(output.getvalue())

    def test_cli_local_commands_safe_errors_and_nonzero_pending(self):
        from devdiary.note_transport import DeliveryError

        with (
            mock.patch.object(notes, "post_operation") as post,
            mock.patch(
                "devdiary.cli.load_registry",
                side_effect=AssertionError("notes must not load registry"),
            ),
        ):
            code, queued = self.cli_command(
                "create", {"attributes": {"content": "CLI_PRIVATE_CONTENT"}}
            )
            self.assertEqual(code, 0)
            self.assertEqual(queued["state"], "queued")
            post.assert_not_called()
        ref = queued["note_ref"]
        self.assertEqual(
            self.cli_command(
                "update",
                {"note_ref": ref, "attributes": {"content": "CLI_PRIVATE_CONTENT"}},
            )[0],
            0,
        )
        code, status = self.cli_command("status", {})
        self.assertEqual(code, 0)
        self.assertEqual(len(status["operations"]), 2)
        with mock.patch.object(
            notes, "post_operation", side_effect=DeliveryError("unauthorized", False)
        ):
            code, result = self.cli_command("retry", {})
        self.assertEqual(
            (code, result["state"], result["error_code"]),
            (2, "blocked", "unauthorized"),
        )
        self.key.unlink()
        with (
            mock.patch.object(
                notes, "read_key_file", side_effect=AssertionError("offline discard")
            ),
            mock.patch.object(
                notes, "post_operation", side_effect=AssertionError("offline discard")
            ),
        ):
            code, result = self.cli_command("discard", {"note_ref": ref})
        self.assertEqual(
            (code, result["state"], result["remote_deletion"]), (0, "abandoned", False)
        )
        _, fresh = self.cli_command(
            "create", {"attributes": {"content": "CLI_PRIVATE_CONTENT"}}
        )
        with mock.patch.object(notes, "post_operation") as post:
            self.assertEqual(
                self.cli_command("delete", {"note_ref": fresh["note_ref"]})[1]["state"],
                "cancelled",
            )
            post.assert_not_called()
        for command, value in (
            ("create", {"attributes": {"content": "x"}, "context": None}),
            ("update", {"note_ref": ref, "attributes": None}),
            ("delete", {"note_ref": ref, "expected_revision": True}),
            ("status", {"limit": True}),
            ("retry", {"limit": 101}),
            ("discard", {"note_ref": ref, "unknown": "CLI_PRIVATE_CONTENT"}),
        ):
            self.assertEqual(
                self.cli_command(command, value), (2, {"error_code": "invalid_note"})
            )
        for option in ("--content", "--key-file", "--origin", "--subject-ref"):
            self.assertEqual(
                self.cli_command("create", {}, (option, "CLI_PRIVATE_CONTENT")),
                (2, {"error_code": "invalid_setup"}),
            )
        altered = NoteError("invalid_note")
        altered.code = "CLI_PRIVATE_CONTENT"
        for failure in (RuntimeError("CLI_PRIVATE_CONTENT"), altered):
            with mock.patch.object(notes, "local_command", side_effect=failure):
                self.assertEqual(
                    self.cli_command("status", {}), (2, {"error_code": "store_failed"})
                )

    def test_guard_before_input_queue_only_and_explicit_bounded_retry(self):
        from devdiary.note_store import Store
        from devdiary.note_transport import DeliveryError

        stream = io.BytesIO(b'{"attributes":{"content":"SYNTHETIC_OUTBOX"}}')
        with mock.patch.object(notes, "post_operation") as post:
            result = notes.local_command(self.state, "create", stream)
            post.assert_not_called()
        ref = result["note_ref"]
        seen = []

        def lost_ack(origin, key, raw, subject, workspace, note_id):
            request = json.loads(raw)
            with contextlib.closing(
                sqlite3.connect(self.state / "notes.sqlite3")
            ) as independent:
                row = independent.execute(
                    "SELECT state,wire,attempts FROM note_operations WHERE operation_id=?",
                    (request["operation_id"],),
                ).fetchone()
                self.assertEqual(row, ("dispatched", raw, len(seen) + 1))
            seen.append(raw)
            if len(seen) == 1:
                raise DeliveryError("network_failure", True)
            return {
                "operation_id": request["operation_id"],
                "note_ref": ref,
                "note_id": "note_fake",
                "revision": 1,
                "outcome": "replayed",
            }, 200

        with (
            mock.patch.object(notes, "post_operation", side_effect=lost_ack),
            mock.patch.object(notes.time, "sleep") as sleep,
        ):
            result = notes.local_command(self.state, "retry", io.BytesIO(b"{}"))
        self.assertEqual(result["state"], "acknowledged")
        self.assertEqual(seen[0], seen[1])
        sleep.assert_called_once_with(0.2)
        self.assertNotIn(
            b"SYNTHETIC_OUTBOX", (self.state / "notes.sqlite3").read_bytes()
        )
        with Store.open(self.state) as store:
            self.assertEqual(store.status()[0]["revision"], 1)

        class Unreadable(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise AssertionError("guard must precede prose")

        with self.assertRaises(NoteError):
            notes.local_command(self.root / "missing", "create", Unreadable())

    def test_parent_entry_sync_accepts_a_new_trusted_0755_parent(self):
        parent = self.root / "trusted-parent"
        parent.mkdir(mode=0o700)
        before = parent.lstat()
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.root.stat().st_uid, os.getuid())
        self.assertEqual(before.st_uid, os.getuid())
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            self.assertEqual(
                (opened.st_dev, opened.st_ino), (before.st_dev, before.st_ino)
            )
            self.assertEqual(opened.st_uid, os.getuid())
            os.fchmod(fd, 0o755)
            self.assertEqual(os.fstat(fd).st_mode & 0o777, 0o755)
            current = parent.lstat()
            self.assertEqual(
                (current.st_dev, current.st_ino), (opened.st_dev, opened.st_ino)
            )
        finally:
            os.close(fd)
        state = parent / "state"
        profile.create_profile(state, require_bound_profile(self.state))
        synced = []
        fsync = os.fsync

        def record_parent_sync(fd):
            fsync(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (before.st_dev, before.st_ino):
                synced.append(True)

        class InputAfterSync(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                if not synced:
                    raise AssertionError("trusted parent was not synced before input")
                return super().read(size)

        with mock.patch.object(os, "fsync", side_effect=record_parent_sync):
            queued = notes.local_command(
                state,
                "create",
                InputAfterSync(
                    b'{"attributes":{"content":"trusted-parent-synthetic"}}'
                ),
            )
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(parent.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((state / "notes.sqlite3").stat().st_mode & 0o777, 0o600)
        print(
            "parent-entry-evidence",
            "new-owned0755-parent/private0700-root/state0700/data0600/admitted",
            flush=True,
        )

    def test_parent_entry_identity_refusal_closes_the_anchored_descriptor(self):
        from devdiary import note_store

        other = self.root / "other-directory"
        other.mkdir(mode=0o700)
        real_open, real_close, real_stat = os.open, os.close, os.stat
        with note_store.Store.open(self.state) as store:
            for relationship in ("caller_parent", "parent_entry"):
                opened, closed, mismatched = [], [], []

                def open_parent(path, flags, mode=0o777, *, dir_fd=None, opened=opened):
                    fd = real_open(path, flags, mode, dir_fd=dir_fd)
                    if path == ".." and dir_fd == store.directory_fd:
                        opened.append(fd)
                    return fd

                def close_parent(fd, opened=opened, closed=closed):
                    real_close(fd)
                    if fd in opened:
                        closed.append(fd)

                def changed_identity(
                    path,
                    *,
                    dir_fd=None,
                    follow_symlinks=True,
                    relationship=relationship,
                    opened=opened,
                    mismatched=mismatched,
                ):
                    if opened and (
                        (
                            relationship == "caller_parent"
                            and path == self.state.parent
                            and dir_fd is None
                        )
                        or (
                            relationship == "parent_entry"
                            and path == self.state.name
                            and dir_fd == opened[-1]
                        )
                    ):
                        mismatched.append(True)
                        return real_stat(other)
                    return real_stat(
                        path, dir_fd=dir_fd, follow_symlinks=follow_symlinks
                    )

                store_os = SimpleNamespace(**vars(os))
                store_os.open, store_os.close, store_os.stat = (
                    open_parent,
                    close_parent,
                    changed_identity,
                )
                with (
                    mock.patch.object(note_store, "os", store_os),
                    self.assertRaises(NoteError) as raised,
                ):
                    store.sync()
                self.assertEqual(raised.exception.code, "unsafe_path")
                self.assertTrue(mismatched)
                self.assertEqual(len(opened), 1)
                self.assertEqual(closed, opened)
        alias = self.root / "state-alias"
        alias.symlink_to(self.state, target_is_directory=True)
        with self.assertRaises(NoteError) as raised:
            notes.local_command(alias, "status", io.BytesIO(b"{}"))
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_parent_entry_open_and_close_errors_refuse_before_input(self):
        from devdiary import note_store

        queued = notes.local_command(
            self.state,
            "create",
            io.BytesIO(b'{"attributes":{"content":"previously-accepted"}}'),
        )
        real_open, real_close = os.open, os.close

        class Unreadable(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise AssertionError("parent resource failure must precede input")

        for fault in ("open", "close"):
            opened, closed, refused = [], [], []

            def open_parent(
                path,
                flags,
                mode=0o777,
                *,
                dir_fd=None,
                fault=fault,
                opened=opened,
                refused=refused,
            ):
                if path == ".." and fault == "open":
                    refused.append(True)
                    raise OSError("SYNTHETIC_PARENT_OPEN_FAILURE")
                fd = real_open(path, flags, mode, dir_fd=dir_fd)
                if path == "..":
                    opened.append(fd)
                return fd

            def close_parent(fd, opened=opened, closed=closed):
                real_close(fd)
                if fd in opened:
                    closed.append(fd)
                    raise OSError("SYNTHETIC_PARENT_CLOSE_FAILURE")

            store_os = SimpleNamespace(**vars(os))
            store_os.open, store_os.close = open_parent, close_parent
            with (
                mock.patch.object(note_store, "os", store_os),
                self.assertRaises(NoteError) as raised,
            ):
                notes.local_command(self.state, "create", Unreadable())
            self.assertEqual(raised.exception.code, "store_failed")
            self.assertIsNone(raised.exception.__cause__)
            if fault == "open":
                self.assertTrue(refused)
                self.assertEqual(opened, [])
            else:
                self.assertEqual(len(opened), 1)
                self.assertEqual(closed, opened)
            rows = notes.local_command(self.state, "status", io.BytesIO(b"{}"))[
                "operations"
            ]
            self.assertEqual(
                [row["operation_id"] for row in rows], [queued["operation_id"]]
            )

    def test_existing_binding_is_synced_again_before_input_after_prior_failure(self):
        from devdiary.note_store import Store

        with Store.open(self.state):
            pass

        class Unreadable(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise AssertionError("binding durability must precede input")

        with (
            mock.patch.object(
                Store, "sync", side_effect=OSError("synthetic binding sync failure")
            ),
            self.assertRaises(NoteError) as raised,
        ):
            notes.local_command(self.state, "create", Unreadable())
        self.assertEqual(raised.exception.code, "store_failed")

    def test_deleted_result_and_retry_bounds_do_not_clear_conflicts(self):
        from devdiary.note_store import Store
        from devdiary.note_transport import DeliveryError

        queued = notes.local_command(
            self.state,
            "create",
            io.BytesIO(b'{"attributes":{"content":"retry-bound"}}'),
        )
        with (
            mock.patch.object(
                notes,
                "post_operation",
                side_effect=DeliveryError("network_failure", True),
            ) as post,
            mock.patch.object(notes.time, "sleep") as sleep,
        ):
            result = notes.local_command(self.state, "retry", io.BytesIO(b"{}"))
        self.assertEqual(result["state"], "pending")
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(0.2), mock.call(0.4)])
        with mock.patch.object(
            notes, "post_operation", side_effect=DeliveryError("conflict", False)
        ) as post:
            self.assertEqual(
                notes.local_command(self.state, "retry", io.BytesIO(b"{}"))["state"],
                "blocked",
            )
            self.assertEqual(
                notes.local_command(self.state, "retry", io.BytesIO(b"{}"))["state"],
                "blocked",
            )
            self.assertEqual(post.call_count, 1)
        with Store.open(self.state) as store:
            self.assertIsNotNone(
                store.connection.execute(
                    "SELECT wire FROM note_operations WHERE seq=?", (queued["seq"],)
                ).fetchone()[0]
            )
            store.discard(queued["note_ref"])
        fresh = notes.local_command(
            self.state,
            "create",
            io.BytesIO(b'{"attributes":{"content":"terminal-result"}}'),
        )
        receipt = {
            "operation_id": fresh["operation_id"],
            "note_ref": fresh["note_ref"],
            "note_id": "note_terminal_result",
            "revision": 8,
            "outcome": "deleted",
        }
        with mock.patch.object(notes, "post_operation", return_value=(receipt, 200)):
            result = notes.local_command(self.state, "retry", io.BytesIO(b"{}"))
        self.assertEqual(result["state"], "deleted")

    def test_flush_maximum_100_heads_and_bounded_retry_after(self):
        from devdiary.note_store import Store
        from devdiary.note_transport import DeliveryError

        with Store.open(self.state) as store:
            for _ in range(101):
                store.enqueue("create", {"attributes": {"content": "bounded-head"}})
            seen = []

            def accept(origin, key, wire, subject, workspace, note_id):
                request = json.loads(wire)
                seen.append(request["operation_id"])
                return {
                    "operation_id": request["operation_id"],
                    "note_ref": request["note_ref"],
                    "note_id": "note_head" + str(len(seen)),
                    "revision": 1,
                    "outcome": "applied",
                }, 201

            with mock.patch.object(notes, "post_operation", side_effect=accept):
                result = notes.flush(store)
            self.assertEqual(result, {"state": "pending", "operations": 100})
            self.assertEqual(len(set(seen)), 100)
            with (
                mock.patch.object(
                    notes,
                    "post_operation",
                    side_effect=DeliveryError("rate_limited", True, -1),
                ) as post,
                mock.patch.object(notes.time, "sleep") as sleep,
            ):
                self.assertEqual(notes.flush(store)["state"], "pending")
                post.assert_called_once()
                sleep.assert_not_called()
            with (
                mock.patch.object(
                    notes,
                    "post_operation",
                    side_effect=DeliveryError("rate_limited", True, 30),
                ) as post,
                mock.patch.object(notes.time, "sleep") as sleep,
            ):
                self.assertEqual(notes.flush(store)["state"], "pending")
                self.assertEqual(post.call_count, 3)
                self.assertEqual(sleep.call_args_list, [mock.call(30), mock.call(30)])

    def test_real_loopback_lost_ack_rotation_binding_and_dedup(self):
        from devdiary.note_transport import post_operation

        accepted = {}
        writes = []
        received = []
        state = self.state
        expected_subject = scope()["subject_ref"]
        expected_workspace = scope()["workspace_ref"]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                wire = self.rfile.read(int(self.headers["Content-Length"]))
                request = json.loads(wire)
                received.append(wire)
                with contextlib.closing(
                    sqlite3.connect(state / "notes.sqlite3")
                ) as independent:
                    row = independent.execute(
                        "SELECT wire,state FROM note_operations WHERE operation_id=?",
                        (request["operation_id"],),
                    ).fetchone()
                    if row != (wire, "dispatched"):
                        raise AssertionError("committed dispatch missing")
                if (
                    self.headers["DevDiary-Notes-Subject"] != expected_subject
                    or self.headers["DevDiary-Notes-Workspace"] != expected_workspace
                    or self.headers["Authorization"] == "Bearer dn_live_" + "b" * 43
                ):
                    raw, status = b'{"error":"binding_mismatch"}', 409
                elif request["operation_id"] not in accepted:
                    receipt = {
                        "operation_id": request["operation_id"],
                        "note_ref": request["note_ref"],
                        "note_id": "note_loopback" + str(len(accepted)),
                        "revision": 1,
                        "outcome": "applied",
                    }
                    accepted[request["operation_id"]] = receipt
                    writes.append(request["operation_id"])
                    # Simulate a response lost AFTER the fake writer committed.
                    self.close_connection = True
                    return
                else:
                    raw = json.dumps(
                        {**accepted[request["operation_id"]], "outcome": "replayed"}
                    ).encode()
                    status = 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def loopback(origin, key, wire, subject, workspace, note_id):
            return post_operation(
                f"http://127.0.0.1:{port}",
                key,
                wire,
                subject,
                workspace,
                note_id,
                _loopback_port=port,
            )

        try:
            queued = notes.local_command(
                self.state,
                "create",
                io.BytesIO(b'{"attributes":{"content":"LOOPBACK_SYNTHETIC"}}'),
            )
            self.key.write_text("dn_live_" + "b" * 43)
            with mock.patch.object(notes, "post_operation", side_effect=loopback):
                self.assertEqual(
                    notes.local_command(self.state, "retry", io.BytesIO(b"{}"))[
                        "state"
                    ],
                    "blocked",
                )
                self.assertEqual(writes, [])
                self.key.write_text("dn_live_" + "c" * 43)
                with mock.patch.object(notes.time, "sleep"):
                    result = notes.local_command(self.state, "retry", io.BytesIO(b"{}"))
            self.assertEqual(result["state"], "acknowledged")
            self.assertEqual(writes, [queued["operation_id"]])
            self.assertEqual(received, [received[0]] * 3)
            self.assertNotIn(
                b"LOOPBACK_SYNTHETIC", (self.state / "notes.sqlite3").read_bytes()
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_cancel_offline_discard_and_credential_substitution(self):
        result = notes.local_command(
            self.state,
            "create",
            io.BytesIO(b'{"attributes":{"content":"CANCEL_SYNTHETIC"}}'),
        )
        ref = result["note_ref"]
        with mock.patch.object(notes, "post_operation") as post:
            cancelled = notes.local_command(
                self.state, "delete", io.BytesIO(json.dumps({"note_ref": ref}).encode())
            )
            self.assertEqual(cancelled["state"], "cancelled")
            post.assert_not_called()
        second = notes.local_command(
            self.state,
            "create",
            io.BytesIO(b'{"attributes":{"content":"DISCARD_SYNTHETIC"}}'),
        )
        from devdiary.note_transport import DeliveryError

        def mismatch(origin, key, wire, subject, workspace, note_id):
            self.assertEqual(key, "dn_live_" + "b" * 43)
            self.assertEqual(subject, scope()["subject_ref"])
            self.assertEqual(workspace, scope()["workspace_ref"])
            raise DeliveryError("binding_mismatch", False)

        self.key.write_text("dn_live_" + "b" * 43)
        with mock.patch.object(notes, "post_operation", side_effect=mismatch):
            blocked = notes.local_command(self.state, "retry", io.BytesIO(b"{}"))
        self.assertEqual(blocked["state"], "blocked")
        self.key.unlink()
        with (
            mock.patch.object(
                notes, "read_key_file", side_effect=AssertionError("offline")
            ),
            mock.patch.object(
                notes, "post_operation", side_effect=AssertionError("offline")
            ),
        ):
            discarded = notes.local_command(
                self.state,
                "discard",
                io.BytesIO(json.dumps({"note_ref": second["note_ref"]}).encode()),
            )
        self.assertEqual(discarded["state"], "abandoned")
        self.assertFalse(discarded["remote_deletion"])
        self.assertNotIn(
            b"DISCARD_SYNTHETIC", (self.state / "notes.sqlite3").read_bytes()
        )


class NotesTest(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("notes configure integration requires POSIX")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"
        self.key = self.root / "key"
        self.secret = "dn_live_" + "a" * 43
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(self.secret.encode())

    def setup_value(self, metadata=None):
        return {
            "schema_version": 1,
            "origin": "https://example.test",
            "key_file": str(self.key),
            "metadata": scope() if metadata is None else metadata,
        }

    def cli(self, value=None, *, arguments=None, raw=None):
        output, error = io.StringIO(), io.StringIO()
        stream = SimpleNamespace(
            buffer=io.BytesIO(
                raw
                if raw is not None
                else json.dumps(value or self.setup_value()).encode()
            )
        )
        with (
            mock.patch("sys.stdin", stream),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            result = main(
                arguments or ["notes", "configure", "--state-dir", str(self.state)]
            )
        self.assertTrue(error.getvalue() == "", "stderr escaped")
        self.assertTrue(
            self.secret not in output.getvalue()
            and str(self.key) not in output.getvalue(),
            "private value escaped",
        )
        return result, output.getvalue()

    def assert_error(self, result, code):
        self.assertTrue(
            result
            == (2, json.dumps({"error_code": code}, separators=(",", ":")) + "\n"),
            "safe error required",
        )

    def test_configure_matches_metadata_and_never_uses_registry_or_runner(self):
        with (
            mock.patch(
                "devdiary.cli.load_registry",
                side_effect=AssertionError("registry accessed"),
            ),
            mock.patch(
                "devdiary.cli.runner.run_command",
                side_effect=AssertionError("runner called"),
            ),
            mock.patch.dict(
                os.environ,
                {
                    "DEVDIARY_INGEST_KEY": "SENTINEL",
                    "DEVDIARY_RUNTIME_SESSION_REF": "SENTINEL",
                },
            ),
            mock.patch.object(notes, "get_scope", return_value=scope()) as get,
        ):
            result = self.cli(
                arguments=[
                    "--config",
                    str(self.root / "absent"),
                    "notes",
                    "configure",
                    "--state-dir",
                    str(self.state),
                ]
            )
        self.assertTrue(result == (0, '{"profile":"configured"}\n'))
        self.assertTrue(get.call_args.args == ("https://example.test", self.secret))
        bound = require_bound_profile(self.state)
        self.assertTrue(bound.subject_ref == scope()["subject_ref"])
        self.assertTrue(
            list(self.state.iterdir()) == [self.state / "notes-profile.json"]
        )

    def test_owned_temporary_base_alias_uses_canonical_configure_fixture(self):
        base = self.root / "temporary-base"
        base.mkdir(mode=0o700)
        alias = self.root / "temporary-alias"
        alias.symlink_to(base, target_is_directory=True)
        temporary_directory = tempfile.TemporaryDirectory
        case = NotesTest(
            "test_configure_matches_metadata_and_never_uses_registry_or_runner"
        )
        result = unittest.TestResult()
        with mock.patch.object(
            tempfile,
            "TemporaryDirectory",
            side_effect=lambda: temporary_directory(dir=alias),
        ):
            case.run(result)
        self.assertTrue(case.temp.name.startswith(str(alias) + os.sep))
        self.assertFalse(result.errors, "owned alias fixture setup must complete")
        self.assertFalse(result.skipped, "POSIX alias fixture must run")
        self.assertTrue(
            result.wasSuccessful(),
            "owned configure fixture must canonicalize its temporary root",
        )

    def test_expected_a_actual_b_is_rejected_without_alternative_identity_or_profile(
        self,
    ):
        with mock.patch.object(notes, "get_scope", return_value=scope("scope_b")):
            result = self.cli()
        self.assert_error(result, "binding_mismatch")
        self.assertFalse(self.state.exists())
        self.assertTrue(scope("scope_b")["subject_ref"] not in result[1])

    def test_invalid_setup_and_scope_fail_before_state_creation(self):
        for field in ("content", "token", "actor", "workspace_ref"):
            with mock.patch.object(notes, "get_scope") as get:
                self.assert_error(
                    self.cli({**self.setup_value(), field: "SENTINEL"}), "invalid_setup"
                )
                get.assert_not_called()
            self.assertFalse(self.state.exists())
        for code in ("unauthorized", "forbidden", "invalid_scope", "network_failure"):
            with mock.patch.object(notes, "get_scope", side_effect=NoteError(code)):
                self.assert_error(self.cli(), code)
            self.assertFalse(self.state.exists())
        self.assert_error(self.cli(raw=b'{"metadata":"SENTINEL"}'), "invalid_setup")

    def test_platform_and_unsafe_key_or_state_fail_before_network(self):
        with (
            mock.patch("devdiary.note_profile.os.O_NOFOLLOW", 0),
            mock.patch.object(notes, "get_scope") as get,
        ):
            self.assert_error(self.cli(), "platform_unsupported")
            get.assert_not_called()
        self.key.unlink()
        self.key.symlink_to(self.root / "missing")
        with mock.patch.object(notes, "get_scope") as get:
            self.assert_error(self.cli(), "unsafe_path")
            get.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_existing_a_cannot_be_retargeted_by_b_and_same_a_rotation_preserves_tuple(
        self,
    ):
        with mock.patch.object(notes, "get_scope", return_value=scope()):
            self.assertTrue(self.cli()[0] == 0)
        raw = (self.state / "notes-profile.json").read_bytes()
        replacement = "dn_live_" + "b" * 43
        self.key.write_bytes(replacement.encode())
        with mock.patch.object(
            notes, "get_scope", return_value=scope("scope_b")
        ) as get:
            self.assert_error(
                self.cli(self.setup_value(scope("scope_b"))), "profile_exists"
            )
            get.assert_not_called()
        self.assertTrue((self.state / "notes-profile.json").read_bytes() == raw)
        bound = require_bound_profile(self.state)
        from devdiary.note_profile import read_key_file
        from devdiary.note_validation import require_matching_scope

        with (
            mock.patch.object(notes, "get_scope", return_value=scope("scope_b")),
            self.assertRaises(NoteError) as rejected,
        ):
            require_matching_scope(
                scope(),
                notes.get_scope(bound.origin, read_key_file(bound.key_file)),
            )
        self.assertTrue(rejected.exception.code == "binding_mismatch")
        self.assertTrue(scope("scope_b")["subject_ref"] not in str(rejected.exception))
        self.assertTrue((self.state / "notes-profile.json").read_bytes() == raw)
        with mock.patch.object(notes, "get_scope", return_value=scope()) as get:
            require_matching_scope(
                scope(), notes.get_scope(bound.origin, read_key_file(bound.key_file))
            )
        self.assertTrue(get.call_args.args[1] == replacement)
        self.assertTrue(require_bound_profile(self.state) == bound)
        self.state = self.root / "separate_b"
        with mock.patch.object(notes, "get_scope", return_value=scope("scope_b")):
            self.assertTrue(self.cli(self.setup_value(scope("scope_b")))[0] == 0)
        self.assertTrue(
            require_bound_profile(self.state).subject_ref
            == scope("scope_b")["subject_ref"]
        )

    def test_configure_parent_sync_failure_blocks_store_input_until_entry_is_synced(
        self,
    ):
        parent = self.root.stat()
        parent_identity = (parent.st_dev, parent.st_ino)
        fsync = os.fsync
        synced = []
        refused = []

        def fail_parent_sync(fd):
            info = os.fstat(fd)
            identity = (info.st_dev, info.st_ino)
            if identity == parent_identity:
                refused.append(identity)
                raise OSError("SYNTHETIC_PARENT_SYNC_FAILURE")
            fsync(fd)
            synced.append(identity)

        with (
            mock.patch.object(notes, "get_scope", return_value=scope()),
            mock.patch.object(os, "fsync", side_effect=fail_parent_sync),
        ):
            self.assert_error(self.cli(), "profile_failed")
        self.assertEqual(refused, [parent_identity])
        for path in (self.state / "notes-profile.json", self.state):
            info = path.stat()
            self.assertIn((info.st_dev, info.st_ino), synced)
        bound = require_bound_profile(self.state)
        self.assertEqual(bound.subject_ref, scope()["subject_ref"])
        profile_bytes = (self.state / "notes-profile.json").read_bytes()

        class Unreadable(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise AssertionError("parent-entry durability must precede input")

        with (
            mock.patch.object(os, "fsync", side_effect=fail_parent_sync),
            self.assertRaises(NoteError) as raised,
        ):
            notes.local_command(self.state, "create", Unreadable())
        self.assertEqual(raised.exception.code, "store_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(len(refused), 2)
        self.assertEqual(
            notes.local_command(self.state, "status", io.BytesIO(b"{}")),
            {"operations": []},
        )

        parent_synced = []

        def record_sync(fd):
            fsync(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == parent_identity:
                parent_synced.append(True)

        class InputAfterParentSync(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                if not parent_synced:
                    raise AssertionError("input preceded successful parent sync")
                return super().read(size)

        with mock.patch.object(os, "fsync", side_effect=record_sync):
            queued = notes.local_command(
                self.state,
                "create",
                InputAfterParentSync(
                    b'{"attributes":{"content":"parent-entry-synthetic"}}'
                ),
            )
        self.assertEqual(queued["state"], "queued")
        self.assertTrue(parent_synced)
        with mock.patch.object(os, "fsync", side_effect=fail_parent_sync):
            self.assert_error(
                self.cli(
                    arguments=["notes", "create", "--state-dir", str(self.state)],
                    raw=b'{"attributes":{"content":"must-not-queue"}}',
                ),
                "store_failed",
            )
        self.assertEqual(len(refused), 3)
        status = notes.local_command(self.state, "status", io.BytesIO(b"{}"))
        self.assertEqual(
            [row["operation_id"] for row in status["operations"]],
            [queued["operation_id"]],
        )
        self.assertEqual(
            (self.state / "notes-profile.json").read_bytes(), profile_bytes
        )
        self.assertEqual(require_bound_profile(self.state), bound)
        print(
            "parent-entry-evidence",
            "selective-configure-parent-failure/profile-valid/unread-input/safe-refusal/reopen-ID-preserved",
            flush=True,
        )

    def test_sync_failure_has_no_configured_output_and_existing_file_is_preserved(self):
        with (
            mock.patch.object(notes, "get_scope", return_value=scope()),
            mock.patch(
                "devdiary.note_profile.os.fsync", side_effect=OSError("SENTINEL_SYNC")
            ),
        ):
            self.assert_error(self.cli(), "profile_failed")
        self.assertTrue(
            require_bound_profile(self.state).subject_ref == scope()["subject_ref"]
        )
        with mock.patch.object(notes, "get_scope") as get:
            self.assert_error(self.cli(), "profile_exists")
            get.assert_not_called()

    def test_unexpected_errors_are_content_free_and_cannot_inject_error_codes(self):
        altered = NoteError("invalid_scope")
        altered.code = "SENTINEL_CODE"
        for failure in (
            RuntimeError("SENTINEL_PATH_SECRET"),
            NoteError("SENTINEL_CODE"),
            NoteError("invalid_note"),
            NoteError("purge_pending"),
            altered,
        ):
            with mock.patch.object(notes, "get_scope", side_effect=failure):
                self.assert_error(self.cli(), "configure_failed")
            self.assertFalse(self.state.exists())

    def test_configure_helper_raises_only_fresh_sanitized_errors(self):
        corrupted = NoteError("invalid_scope")
        corrupted.code = "SENTINEL_CODE"
        for original, code in (
            (RuntimeError("SENTINEL_PATH_SECRET"), "configure_failed"),
            (NoteError("invalid_scope"), "invalid_scope"),
            (NoteError("purge_pending"), "configure_failed"),
            (corrupted, "configure_failed"),
        ):
            output, error = io.StringIO(), io.StringIO()
            stream = SimpleNamespace(
                buffer=io.BytesIO(json.dumps(self.setup_value()).encode())
            )
            with (
                mock.patch("sys.stdin", stream),
                mock.patch.object(notes, "get_scope", side_effect=original),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
                self.assertRaises(NoteError) as raised,
            ):
                _configure_notes(argparse.Namespace(state_dir=str(self.state)))
            normalized = raised.exception
            self.assertTrue(
                normalized is not original, "fresh sanitized error required"
            )
            self.assertTrue(normalized.code == code, "allowlisted error required")
            self.assertTrue(
                normalized.__cause__ is None, "exception cause must not be forwarded"
            )
            self.assertTrue(normalized.__suppress_context__)
            displayed = "".join(traceback.format_exception(normalized))
            self.assertTrue(
                all(
                    "SENTINEL" not in value
                    for value in (str(normalized), repr(normalized), displayed)
                ),
                "exception display must not expose the original failure",
            )
            self.assertTrue(
                output.getvalue() == "" and error.getvalue() == "",
                "helper must not report exceptions",
            )
            self.assertFalse(self.state.exists())

    def test_notes_argument_failures_are_redacted_before_input_or_effects(self):
        sentinel = "SENTINEL_ARGV"
        bad = [
            ["notes"],
            ["notes", sentinel],
            ["notes", "configure"],
            ["notes", "configure", "--state-dir"],
            ["notes", "configure", "--state-dir", str(self.state), "--token", sentinel],
            ["notes", "configure", "--state-dir", str(self.state), sentinel],
            [
                "--config",
                str(self.root / "absent"),
                "notes",
                "configure",
                "--unknown",
                sentinel,
            ],
        ]

        class Unreadable:
            @property
            def buffer(self):
                raise AssertionError("stdin accessed before argument validation")

        for arguments in bad:
            output, error = io.StringIO(), io.StringIO()
            with (
                mock.patch("sys.stdin", Unreadable()),
                mock.patch.object(notes, "get_scope") as get,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                result = main(arguments)
            self.assertTrue(
                (result, output.getvalue(), error.getvalue())
                == (2, '{"error_code":"invalid_setup"}\n', ""),
                "argument redaction required",
            )
            get.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_registered_notes_surface_and_non_notes_literal_arguments_keep_routing(
        self,
    ):
        for command in (
            "configure",
            "create",
            "update",
            "delete",
            "status",
            "retry",
            "discard",
        ):
            selected = parser().parse_args(
                ["notes", command, "--state-dir", str(self.state)]
            )
            self.assertEqual(selected.notes_operation, command)
            self.assert_error(self.cli(arguments=["notes", command]), "invalid_setup")
        self.assert_error(self.cli(arguments=["notes", "import"]), "invalid_setup")
        for arguments in (
            ["--config", "notes", "doctor"],
            ["run", "--actor", "notes", "--", "notes"],
        ):
            output, error = io.StringIO(), io.StringIO()
            with (
                mock.patch(
                    "devdiary.cli.load_registry",
                    side_effect=NoteError("binding_required"),
                ) as load,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
                self.assertRaises(NoteError),
            ):
                main(arguments)
            load.assert_called_once()
            self.assertTrue(output.getvalue() == "")


class NotesPlatformTest(unittest.TestCase):
    def test_non_posix_outbox_refuses_all_commands_before_input(self):
        class Unreadable(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise AssertionError("outbox input read before platform admission")

        for command in ("create", "update", "delete", "status", "retry", "discard"):
            with (
                mock.patch.object(profile, "os", SimpleNamespace(name="nt")),
                self.assertRaises(NoteError) as raised,
            ):
                notes.local_command("/synthetic/state", command, Unreadable())
            self.assertEqual(raised.exception.code, "platform_unsupported")

    def test_non_posix_integration_skips_before_temporary_fixture(self):
        result = unittest.TestResult()
        with (
            mock.patch(f"{__name__}.os", SimpleNamespace(name="nt")),
            mock.patch(
                f"{__name__}.tempfile.TemporaryDirectory",
                side_effect=AssertionError("POSIX fixture entered"),
            ) as temporary,
        ):
            NotesTest(
                "test_configure_matches_metadata_and_never_uses_registry_or_runner"
            ).run(result)
        self.assertTrue(
            result.wasSuccessful() and len(result.skipped) == 1,
            "non-POSIX integration must skip before fixtures",
        )
        temporary.assert_not_called()

    def test_non_posix_configure_fails_before_input_key_network_or_state(self):
        class Unreadable(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                raise AssertionError("configure read input on unsupported platform")

        with (
            mock.patch.object(profile, "os", SimpleNamespace(name="nt")),
            mock.patch.object(notes, "check_profile_available") as check,
            mock.patch.object(notes, "read_key_file") as read,
            mock.patch.object(notes, "get_scope") as get,
            mock.patch.object(notes, "create_profile") as create,
            self.assertRaises(NoteError) as raised,
        ):
            notes.configure("/synthetic/state", Unreadable())
        self.assertTrue(raised.exception.code == "platform_unsupported")
        for effect in (check, read, get, create):
            effect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
