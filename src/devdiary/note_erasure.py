from __future__ import annotations

import os
import sqlite3
from typing import TYPE_CHECKING

from devdiary.note_validation import NoteError, decode_object, encode_object

if TYPE_CHECKING:
    from devdiary.note_store import Store


def _intent(store: Store, note_ref: str) -> dict:
    profile = store.profile
    return {
        "version": 1,
        "origin": profile.origin,
        "subject_ref": profile.subject_ref,
        "workspace_ref": profile.workspace_ref,
        "endpoint_path": profile.endpoint_path,
        "note_ref": note_ref,
    }


def _read_marker(store: Store, name: str) -> dict:
    fd = store._open_file(name)
    try:
        chunks, remaining = [], 8193
        while remaining:
            raw = os.read(fd, remaining)
            if not raw:
                break
            chunks.append(raw)
            remaining -= len(raw)
        value = decode_object(b"".join(chunks), 8192, "purge_pending")
        store.verify_file(fd, name)
    finally:
        os.close(fd)
    return value


def _read_intent(store: Store) -> str:
    value = _read_marker(store, store.intent_name)
    ref = value.get("note_ref")
    if (
        not isinstance(ref, str)
        or value != _intent(store, ref)
        or type(value.get("version")) is not int
    ):
        raise NoteError("purge_pending")
    if (
        store.connection.execute(
            "SELECT 1 FROM notes WHERE note_ref=?", (ref,)
        ).fetchone()
        is None
    ):
        raise NoteError("purge_pending")
    store.check_paths()
    return ref


def _publish(store: Store, note_ref: str) -> None:
    if store.exists(store.intent_name):
        if _read_intent(store) != note_ref:
            raise NoteError("purge_pending")
        return
    if store.exists(store.intent_temp):
        try:
            previous = _read_marker(store, store.intent_temp)
        except NoteError as error:
            if error.code != "purge_pending":
                raise
            # An interrupted write can be rebuilt only from pending DB metadata.
            previous = None
        if previous is not None and (
            previous != _intent(store, note_ref)
            or type(previous.get("version")) is not int
        ):
            raise NoteError("purge_pending")
        os.unlink(store.intent_temp, dir_fd=store.directory_fd)
        os.fsync(store.directory_fd)
    fd = store._new_file(store.intent_temp)
    try:
        raw = encode_object(_intent(store, note_ref))
        while raw:
            count = os.write(fd, raw)
            if count <= 0:
                raise OSError
            raw = raw[count:]
        store.verify_file(fd, store.intent_temp)
        os.fsync(fd)
        store.check_paths()
    finally:
        os.close(fd)
    os.rename(
        store.intent_temp,
        store.intent_name,
        src_dir_fd=store.directory_fd,
        dst_dir_fd=store.directory_fd,
    )
    os.fsync(store.directory_fd)
    store.check_paths()


def _purge(store: Store, note_ref: str, cleanup: str) -> None:
    with store.transaction():
        if cleanup == "ack" or not cleanup:
            store.connection.execute(
                "UPDATE note_operations SET draft=NULL,wire=NULL WHERE note_ref=? AND (cleanup_pending=1 OR state='acknowledged')",
                (note_ref,),
            )
        else:
            store.connection.execute(
                "UPDATE note_operations SET draft=NULL,wire=NULL,state='purged' WHERE note_ref=?",
                (note_ref,),
            )
    # Full secure_delete covers ordinary-table deleted cells/overflow from inception.
    # A nonempty freelist can also contain historical OFF-mode data; rebuild then.
    if store.connection.execute("PRAGMA freelist_count").fetchone()[0]:
        store.connection.execute("VACUUM")
    store.close_connection()
    store.sync()
    for name in store.sidecars:
        if store.exists(name):
            raise NoteError("purge_pending")
    store._connect()
    store.sync()


def _finish(store: Store, note_ref: str, cleanup: str) -> None:
    with store.transaction():
        store.connection.execute(
            "UPDATE note_operations SET cleanup_pending=0 WHERE note_ref=?", (note_ref,)
        )
        if cleanup and cleanup != "ack":
            terminal = cleanup in ("deleted", "cancelled", "abandoned")
            store.connection.execute(
                "UPDATE notes SET cleanup='',disposition=?,terminal=MAX(terminal,?) WHERE note_ref=?",
                (cleanup, int(terminal), note_ref),
            )
        else:
            store.connection.execute(
                "UPDATE notes SET cleanup='' WHERE note_ref=?", (note_ref,)
            )
    store.sync()
    store.check_file(store.intent_name)
    os.unlink(store.intent_name, dir_fd=store.directory_fd)
    os.fsync(store.directory_fd)
    store.check_paths()


def _recover(store: Store) -> None:
    if store.lock_fd < 0:
        raise NoteError("store_failed")
    while True:
        pending = store.connection.execute(
            "SELECT note_ref,cleanup FROM notes WHERE cleanup<>'' ORDER BY note_ref LIMIT 1"
        ).fetchone()
        marker = store.exists(store.intent_name)
        if marker:
            ref = _read_intent(store)
            row = store.connection.execute(
                "SELECT cleanup FROM notes WHERE note_ref=?", (ref,)
            ).fetchone()
            cleanup = row[0]
        elif pending is not None:
            ref, cleanup = pending
        elif store.exists(store.intent_temp):
            raise NoteError("purge_pending")
        else:
            return
        store.cleanup_started = True
        store.cleanup_note_ref = ref
        try:
            _publish(store, ref)
            _purge(store, ref, cleanup)
            _finish(store, ref, cleanup)
        except (OSError, sqlite3.Error, NoteError):
            # Pending DB metadata or the published marker remains authoritative.
            # Restore the flag after a detectable late unlink/sync failure as well.
            try:
                if not store.connected:
                    store._connect()
                with store.transaction():
                    store.connection.execute(
                        "UPDATE notes SET cleanup=? WHERE note_ref=?",
                        (cleanup or "ack", ref),
                    )
            except (OSError, sqlite3.Error, NoteError):
                raise NoteError("purge_pending") from None
            raise NoteError("purge_pending") from None


def recover(store: Store) -> None:
    """Recover using public SQLite/OS results; no hidden-native-handle receipt."""
    try:
        _recover(store)
    except (OSError, sqlite3.Error, NoteError):
        store.cleanup_started = True
        raise NoteError("purge_pending") from None
