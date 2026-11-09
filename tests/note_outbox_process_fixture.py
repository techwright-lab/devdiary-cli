from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from unittest import mock

from devdiary import note_erasure, notes
from devdiary.note_store import Store
from devdiary.note_transport import post_operation
from devdiary.note_validation import NoteError

PHASES = (
    "http",
    "contend",
    "enqueue_before_commit",
    "enqueue_committed",
    "freeze_before_commit",
    "dispatch_committed",
    "ack_committed",
    "intent_published",
    "purge_closed_synced",
    "metadata_finalized",
    "wal_committed",
)


def emit(value: dict) -> None:
    raw = json.dumps(value, separators=(",", ":"))
    if len(raw.encode()) > 512:
        raise RuntimeError("fixture_metadata_bound")
    print(raw, flush=True)


def crash(phase: str, metadata: dict | None = None) -> None:
    emit({"phase": phase, **(metadata or {})})
    if sys.stdin.buffer.readline(16) != b"crash\n":
        raise RuntimeError("fixture_barrier")
    os._exit(73)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--phase", required=True, choices=PHASES)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if os.environ.get("DEVDIARY_OUTBOX_QUALIFICATION") == "1" and not getattr(
        sys, "_devdiary_qualification_network_guard", False
    ):
        raise RuntimeError("fixture_guard_missing")
    if args.phase == "contend":
        try:
            with Store.open(args.state_dir):
                emit({"state": "entered"})
        except NoteError as error:
            emit({"state": error.code})
        return 0
    with Store.open(args.state_dir) as store:
        transaction = store.transaction

        @contextmanager
        def before_commit():
            with transaction():
                yield
                crash(args.phase)

        if args.phase == "enqueue_before_commit":
            with mock.patch.object(store, "transaction", before_commit):
                store.enqueue(
                    "create", {"attributes": {"content": "PROCESS_SYNTHETIC_8c17"}}
                )
        queued = store.enqueue(
            "create", {"attributes": {"content": "PROCESS_SYNTHETIC_8c17"}}
        )
        seq = queued["seq"]
        if args.phase == "enqueue_committed":
            crash(
                args.phase,
                {
                    "operation_id": queued["operation_id"],
                    "note_ref": queued["note_ref"],
                },
            )
        if args.phase == "freeze_before_commit":
            with mock.patch.object(store, "transaction", before_commit):
                store.freeze(seq)
        operation = store.freeze(seq)
        if operation is None:
            raise RuntimeError("fixture_operation_missing")
        if args.phase == "dispatch_committed":
            crash(
                args.phase,
                {
                    "operation_id": operation["operation_id"],
                    "note_ref": operation["note_ref"],
                    "wire_sha256": operation["wire_sha256"],
                },
            )
        if args.phase == "http":
            if args.port is None or not 1024 <= args.port <= 65535:
                raise RuntimeError("fixture_port")
            port = args.port

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

            emit({"phase": "http"})
            with mock.patch.object(notes, "post_operation", side_effect=loopback):
                result = notes.flush(store)
            emit(result)
            return 0
        receipt = {
            "operation_id": queued["operation_id"],
            "note_ref": queued["note_ref"],
            "note_id": "note_process",
            "revision": 1,
            "outcome": "applied",
        }
        if args.phase == "wal_committed":
            if (
                store.connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                != "wal"
            ):
                raise RuntimeError("fixture_wal_mode")
            store.connection.execute("PRAGMA wal_autocheckpoint=0")
        store._mark_ack(seq, receipt, 201)
        if args.phase == "wal_committed":
            wal_bytes = store.check_file("notes.sqlite3-wal").st_size
            if wal_bytes <= 32 or not store.exists("notes.sqlite3-shm"):
                raise RuntimeError("fixture_wal_missing")
            emit({"phase": args.phase, "wal_bytes": wal_bytes, "shm": True})
            if sys.stdin.buffer.readline(16) != b"crash\n":
                raise RuntimeError("fixture_barrier")
            os._exit(73)
        if args.phase == "ack_committed":
            crash(args.phase)
        if args.phase == "intent_published":
            with mock.patch.object(
                note_erasure, "_purge", side_effect=lambda *unused: crash(args.phase)
            ):
                note_erasure.recover(store)
        if args.phase == "purge_closed_synced":
            with mock.patch.object(
                note_erasure, "_finish", side_effect=lambda *unused: crash(args.phase)
            ):
                note_erasure.recover(store)
        if args.phase == "metadata_finalized":
            unlink = os.unlink

            def before_unlink(path, *, dir_fd=None):
                if path == store.intent_name:
                    crash(args.phase)
                return unlink(path, dir_fd=dir_fd)

            with mock.patch.object(
                note_erasure.os, "unlink", side_effect=before_unlink
            ):
                note_erasure.recover(store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
