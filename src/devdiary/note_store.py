from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path

from devdiary.note_profile import BoundProfile, require_bound_profile, require_posix
from devdiary.note_validation import (
    SETUP_LIMIT,
    NoteError,
    decode_object,
    encode_object,
    freeze_write,
    validate_intent,
    validate_locator,
    validate_receipt,
)

if os.name == "posix":
    import fcntl

DB_NAME = "notes.sqlite3"
LOCK_NAME = "notes.lock"
INTENT_NAME = "notes-erasure-intent.json"
INTENT_TEMP = "notes-erasure-intent.tmp"
APPLICATION_ID = 0x44444E31
SIDECARS = (DB_NAME + "-journal", DB_NAME + "-wal", DB_NAME + "-shm")
MAX_OPERATIONS = 1000
MAX_BYTES = 64 * 1024 * 1024

_DDL = (
    """CREATE TABLE note_store_binding (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        version INTEGER NOT NULL CHECK(version=1),
        origin TEXT NOT NULL, subject_ref TEXT NOT NULL, workspace_ref TEXT NOT NULL,
        endpoint_path TEXT NOT NULL, key_file TEXT NOT NULL)""",
    """CREATE TABLE notes (
        note_ref TEXT PRIMARY KEY NOT NULL,
        note_id TEXT UNIQUE, revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
        terminal INTEGER NOT NULL DEFAULT 0 CHECK(terminal IN (0,1)),
        disposition TEXT NOT NULL DEFAULT '' CHECK(disposition IN ('','deleted','cancelled','discarded','abandoned')),
        cleanup TEXT NOT NULL DEFAULT '' CHECK(cleanup IN ('','ack','deleted','cancelled','discarded','abandoned')))""",
    """CREATE TABLE note_operations (
        seq INTEGER PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
        note_ref TEXT NOT NULL REFERENCES notes(note_ref),
        action TEXT NOT NULL CHECK(action IN ('create','update','delete')),
        state TEXT NOT NULL CHECK(state IN ('queued','dispatched','blocked','acknowledged','purged')),
        origin TEXT NOT NULL, subject_ref TEXT NOT NULL, workspace_ref TEXT NOT NULL,
        endpoint_path TEXT NOT NULL, key_file TEXT NOT NULL,
        predecessor INTEGER REFERENCES note_operations(seq),
        draft BLOB CHECK(draft IS NULL OR length(draft)<=262144),
        wire BLOB CHECK(wire IS NULL OR length(wire)<=262144), wire_sha256 TEXT,
        expected_revision INTEGER CHECK(expected_revision IS NULL OR expected_revision>0),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        error_code TEXT NOT NULL DEFAULT '',
        receipt_note_id TEXT, receipt_revision INTEGER CHECK(receipt_revision IS NULL OR receipt_revision>0),
        receipt_outcome TEXT CHECK(receipt_outcome IS NULL OR receipt_outcome IN ('applied','replayed','deleted')),
        cleanup_pending INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_pending IN (0,1)),
        CHECK(predecessor IS NULL OR predecessor<seq),
        CHECK(state NOT IN ('acknowledged','purged') OR cleanup_pending=1 OR (draft IS NULL AND wire IS NULL)))""",
    "CREATE INDEX note_operation_heads ON note_operations(note_ref,seq,state)",
    "CREATE INDEX note_operation_state ON note_operations(state,seq)",
)


def _require_memory_temp_policy(options: list[str], readback: object) -> None:
    # SQLite TEMP_STORE=0 and OMIT_MEMORYDB can override a MEMORY pragma.
    # Compile mode3 forces memory; modes1/2 require the effective runtime choice.
    diagnostics = [option for option in options if option.startswith("TEMP_STORE")]
    if (
        any(
            option.partition("=")[0] in ("OMIT_MEMORYDB", "SQLITE_OMIT_MEMORYDB")
            for option in options
        )
        or len(diagnostics) != 1
        or diagnostics[0] not in ("TEMP_STORE=1", "TEMP_STORE=2", "TEMP_STORE=3")
        or type(readback) is not int
        or readback not in (0, 1, 2)
        or (diagnostics[0] != "TEMP_STORE=3" and readback != 2)
    ):
        raise NoteError("platform_unsupported")


def _file(info) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise NoteError("unsafe_path")


def _same(left, right) -> None:
    if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
        raise NoteError("unsafe_path")


def _directory(path) -> int:
    parts = validate_locator(os.fspath(path), "unsafe_path")[1:].split("/")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    private_seen = False
    try:
        for index, part in enumerate(parts):
            before = os.stat(part, dir_fd=fd, follow_symlinks=False)
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            _same(before, info)
            mode = stat.S_IMODE(info.st_mode)
            if info.st_uid not in (0, os.getuid()) or (
                mode & 0o022 and (not mode & stat.S_ISVTX or private_seen)
            ):
                raise NoteError("unsafe_path")
            if index == len(parts) - 1 and (
                info.st_uid != os.getuid() or mode != 0o700
            ):
                raise NoteError("unsafe_path")
            private_seen |= info.st_uid == os.getuid() and mode == 0o700
        return fd
    except BaseException:
        os.close(fd)
        raise


class Store:
    intent_name = INTENT_NAME
    intent_temp = INTENT_TEMP
    sidecars = SIDECARS

    def __init__(self, directory, profile: BoundProfile):
        self.directory = Path(directory)
        self.profile = profile
        self.directory_fd = -1
        self.lock_fd = -1
        self.connection: sqlite3.Connection
        self.connected = False
        self.db_identity: os.stat_result | None = None
        self.lock_identity: os.stat_result | None = None
        self.cleanup_started = False
        self.cleanup_note_ref: str | None = None

    @classmethod
    @contextmanager
    def open(cls, directory):
        require_posix()
        profile = require_bound_profile(directory)
        store = cls(directory, profile)
        try:
            store._prepare()
            yield store
        except (OSError, sqlite3.Error):
            raise NoteError(
                "purge_pending" if store.cleanup_started else "store_failed"
            ) from None
        finally:
            store.close()

    def binding(self) -> tuple[str, str, str, str, str]:
        profile = self.profile
        return (
            profile.origin,
            profile.subject_ref,
            profile.workspace_ref,
            profile.endpoint_path,
            profile.key_file,
        )

    def exists(self, name: str) -> bool:
        try:
            self.check_file(name)
        except FileNotFoundError:
            return False
        return True

    def check_file(self, name: str):
        info = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        _file(info)
        return info

    def check_paths(self) -> None:
        anchor = _directory(self.directory)
        try:
            _same(os.fstat(anchor), os.fstat(self.directory_fd))
        finally:
            os.close(anchor)
        info = os.stat(self.directory, follow_symlinks=False)
        _same(info, os.fstat(self.directory_fd))
        if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
            raise NoteError("unsafe_path")
        if self.lock_identity is not None:
            _same(self.lock_identity, self.check_file(LOCK_NAME))
        if self.db_identity is not None:
            _same(self.db_identity, self.check_file(DB_NAME))
        for name in (*SIDECARS, INTENT_NAME, INTENT_TEMP):
            self.exists(name)

    def verify_file(self, fd: int, name: str) -> None:
        _file(os.fstat(fd))
        _same(os.fstat(fd), self.check_file(name))

    def _new_file(self, name: str) -> int:
        fd = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=self.directory_fd,
        )
        try:
            _file(os.fstat(fd))
            _same(os.fstat(fd), self.check_file(name))
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _open_file(self, name: str) -> int:
        before = self.check_file(name)
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.directory_fd
        )
        try:
            _file(os.fstat(fd))
            _same(before, os.fstat(fd))
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _prepare(self) -> None:
        self.directory_fd = _directory(self.directory)
        try:
            self.lock_fd = self._new_file(LOCK_NAME)
        except FileExistsError:
            self.lock_fd = self._open_file(LOCK_NAME)
        self.lock_identity = os.fstat(self.lock_fd)
        os.fsync(self.lock_fd)
        os.set_inheritable(self.lock_fd, False)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NoteError("store_busy") from None
        self.check_paths()
        new = not self.exists(DB_NAME)
        if new:
            if any(self.exists(name) for name in (*SIDECARS, INTENT_NAME, INTENT_TEMP)):
                raise NoteError("store_corrupt")
            fd = self._new_file(DB_NAME)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        self.db_identity = self.check_file(DB_NAME)
        self._connect(new)
        if new:
            with self.transaction():
                for statement in _DDL:
                    self.connection.execute(statement)
                self.connection.execute("PRAGMA application_id=1145327153")
                self.connection.execute("PRAGMA user_version=1")
                self.connection.execute(
                    "INSERT INTO note_store_binding VALUES (1,1,?,?,?,?,?)",
                    self.binding(),
                )
            self.sync()
        row = self.connection.execute(
            "SELECT origin,subject_ref,workspace_ref,endpoint_path,key_file FROM note_store_binding WHERE singleton=1 AND version=1"
        ).fetchone()
        if row is None or tuple(row) != self.binding():
            raise NoteError("binding_mismatch")
        self.cleanup_started = (
            self.exists(INTENT_NAME)
            or self.exists(INTENT_TEMP)
            or self.connection.execute(
                "SELECT 1 FROM notes WHERE cleanup<>'' LIMIT 1"
            ).fetchone()
            is not None
        )
        self.sync()
        from devdiary.note_erasure import recover

        recover(self)

    def _connect(self, new=False) -> None:
        self.check_paths()
        if not new:
            fd = self._open_file(DB_NAME)
            try:
                header = os.read(fd, 100)
                if (
                    len(header) != 100
                    or header[:16] != b"SQLite format 3\x00"
                    or int.from_bytes(header[68:72], "big") != APPLICATION_ID
                    or int.from_bytes(header[60:64], "big") != 1
                ):
                    raise NoteError("store_corrupt")
            finally:
                os.close(fd)
        wal = DB_NAME + "-wal"
        if self.exists(wal):
            fd = self._open_file(wal)
            try:
                header = os.read(fd, 32)
                if header and (
                    len(header) != 32
                    or header[:4] not in (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")
                    or int.from_bytes(header[4:8], "big") != 3007000
                ):
                    raise NoteError("store_corrupt")
            finally:
                os.close(fd)
        self.connection = sqlite3.connect(
            str(self.directory / DB_NAME), timeout=0, isolation_level=None
        )
        self.connected = True
        self.connection.row_factory = sqlite3.Row
        if not new:
            if (
                self.connection.execute("PRAGMA application_id").fetchone()[0]
                != APPLICATION_ID
                or self.connection.execute("PRAGMA user_version").fetchone()[0] != 1
            ):
                raise NoteError("store_corrupt")
        for statement in (
            "PRAGMA secure_delete=ON",
            "PRAGMA synchronous=FULL",
            "PRAGMA foreign_keys=ON",
            "PRAGMA temp_store=MEMORY",
        ):
            self.connection.execute(statement)
        for pragma, expected in (
            ("secure_delete", 1),
            ("synchronous", 2),
            ("foreign_keys", 1),
        ):
            if self.connection.execute("PRAGMA " + pragma).fetchone()[0] != expected:
                raise NoteError("store_failed")
        options = [
            row[0]
            for row in self.connection.execute("PRAGMA compile_options").fetchall()
        ]
        _require_memory_temp_policy(
            options, self.connection.execute("PRAGMA temp_store").fetchone()[0]
        )
        mode = self.connection.execute("PRAGMA journal_mode").fetchone()[0]
        if mode == "wal":
            try:
                result = self.connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
            except sqlite3.Error:
                raise NoteError("purge_pending") from None
            if result[0] != 0 or result[1] != 0 or result[2] != 0:
                raise NoteError("purge_pending")
        if (
            self.connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            != "delete"
        ):
            raise NoteError("store_failed")
        self.check_paths()

    @contextmanager
    def transaction(self):
        self.check_paths()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        self.check_paths()

    def _capacity(self, added: int, count: int = 0) -> None:
        row = self.connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(COALESCE(length(draft),0)+COALESCE(length(wire),0)),0) FROM note_operations WHERE state NOT IN ('acknowledged','purged') OR cleanup_pending=1"
        ).fetchone()
        if row[0] + count > MAX_OPERATIONS or row[1] + added > MAX_BYTES:
            raise NoteError("queue_full")

    def _ready(self) -> None:
        from devdiary.note_erasure import recover

        recover(self)

    def enqueue(self, action: str, intent: dict) -> dict:
        self._ready()
        validate_intent(action, intent)
        draft = encode_object(intent)
        ref = (
            "note-ref:" + uuid.uuid4().hex if action == "create" else intent["note_ref"]
        )
        operation_id = "note-op:" + uuid.uuid4().hex
        cancel = False
        with self.transaction():
            self._capacity(len(draft), 1)
            if action == "create":
                self.connection.execute(
                    "INSERT INTO notes(note_ref) VALUES (?)", (ref,)
                )
            note = self.connection.execute(
                "SELECT note_id,revision,terminal FROM notes WHERE note_ref=?", (ref,)
            ).fetchone()
            if note is None:
                raise NoteError("note_not_found")
            if note["terminal"]:
                raise NoteError("note_terminal")
            previous = self.connection.execute(
                "SELECT seq,state,cleanup_pending FROM note_operations WHERE note_ref=? ORDER BY seq DESC LIMIT 1",
                (ref,),
            ).fetchone()
            if "expected_revision" in intent and (
                note["note_id"] is None
                or (
                    previous is not None
                    and (
                        previous["state"] not in ("acknowledged", "purged")
                        or previous["cleanup_pending"]
                    )
                )
            ):
                raise NoteError("operation_blocked")
            cursor = self.connection.execute(
                "INSERT INTO note_operations(operation_id,note_ref,action,state,origin,subject_ref,workspace_ref,endpoint_path,key_file,predecessor,draft) VALUES (?,?,?,'queued',?,?,?,?,?,?,?)",
                (
                    operation_id,
                    ref,
                    action,
                    *self.binding(),
                    previous["seq"] if previous else None,
                    draft,
                ),
            )
            seq = cursor.lastrowid
            if action == "delete" and note["note_id"] is None:
                uncertain = self.connection.execute(
                    "SELECT 1 FROM note_operations WHERE note_ref=? AND (wire IS NOT NULL OR attempts>0) LIMIT 1",
                    (ref,),
                ).fetchone()
                if uncertain is None:
                    self.connection.execute(
                        "UPDATE notes SET cleanup='cancelled',terminal=1 WHERE note_ref=?",
                        (ref,),
                    )
                    cancel = True
        self.sync()
        if cancel:
            from devdiary.note_erasure import recover

            recover(self)
        return {
            "state": "cancelled" if cancel else "queued",
            "note_ref": ref,
            "operation_id": operation_id,
            "seq": seq,
        }

    def status(self, after: int = 0, limit: int = 100) -> list[dict]:
        self._ready()
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise NoteError("invalid_note")
        rows = self.connection.execute(
            "SELECT o.seq,o.operation_id,o.note_ref,o.action,o.state,o.attempts,o.error_code,o.cleanup_pending,n.note_id,n.revision,n.terminal,n.disposition FROM note_operations o JOIN notes n ON n.note_ref=o.note_ref WHERE o.seq>? ORDER BY o.seq LIMIT ?",
            (after, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def heads(self, note_ref: str | None = None, limit: int = 100) -> list[int]:
        self._ready()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise NoteError("invalid_note")
        if note_ref is not None:
            validate_intent("delete", {"note_ref": note_ref})
        rows = self.connection.execute(
            "SELECT o.seq FROM note_operations o JOIN notes n ON n.note_ref=o.note_ref WHERE n.terminal=0 AND o.state IN ('queued','dispatched','blocked') AND (? IS NULL OR o.note_ref=?) AND NOT EXISTS (SELECT 1 FROM note_operations p WHERE p.note_ref=o.note_ref AND p.seq<o.seq AND (p.state NOT IN ('acknowledged','purged') OR p.cleanup_pending=1)) ORDER BY o.seq LIMIT ?",
            (note_ref, note_ref, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def freeze(self, seq: int):
        self._ready()
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM note_operations WHERE seq=?", (seq,)
            ).fetchone()
            if row is None or row["state"] in ("acknowledged", "purged"):
                return None
            if (
                tuple(
                    row[key]
                    for key in (
                        "origin",
                        "subject_ref",
                        "workspace_ref",
                        "endpoint_path",
                        "key_file",
                    )
                )
                != self.binding()
            ):
                raise NoteError("binding_mismatch")
            note = self.connection.execute(
                "SELECT note_id,revision,terminal FROM notes WHERE note_ref=?",
                (row["note_ref"],),
            ).fetchone()
            if note["terminal"]:
                return None
            if row["predecessor"] is not None:
                previous = self.connection.execute(
                    "SELECT state,cleanup_pending FROM note_operations WHERE seq=?",
                    (row["predecessor"],),
                ).fetchone()
                if (
                    previous["state"] not in ("acknowledged", "purged")
                    or previous["cleanup_pending"]
                ):
                    return None
            if row["wire"] is not None:
                if hashlib.sha256(row["wire"]).hexdigest() != row["wire_sha256"]:
                    raise NoteError("store_corrupt")
                return dict(row)
            draft = decode_object(row["draft"], SETUP_LIMIT, "store_corrupt")
            wire = {
                **draft,
                "action": row["action"],
                "operation_id": row["operation_id"],
                "note_ref": row["note_ref"],
            }
            expected = None
            if row["action"] != "create":
                expected = draft.get("expected_revision", note["revision"])
                if not expected:
                    raise NoteError("operation_blocked")
                wire["expected_revision"] = expected
            raw = freeze_write(wire)
            self._capacity(len(raw) - len(row["draft"]))
            self.connection.execute(
                "UPDATE note_operations SET state='dispatched',draft=NULL,wire=?,wire_sha256=?,expected_revision=? WHERE seq=?",
                (raw, hashlib.sha256(raw).hexdigest(), expected, seq),
            )
        self.sync()
        return dict(
            self.connection.execute(
                "SELECT * FROM note_operations WHERE seq=?", (seq,)
            ).fetchone()
        )

    def record_attempt(self, seq: int) -> None:
        with self.transaction():
            self.connection.execute(
                "UPDATE note_operations SET attempts=attempts+1,state='dispatched',error_code='' WHERE seq=?",
                (seq,),
            )
        self.sync()

    def record_failure(self, seq: int, code: str, blocked: bool) -> None:
        from devdiary.note_validation import ERROR_CODES, OUTBOX_ERROR_CODES

        if code not in ERROR_CODES | OUTBOX_ERROR_CODES:
            code = "network_failure"
        with self.transaction():
            self.connection.execute(
                "UPDATE note_operations SET state=?,error_code=? WHERE seq=?",
                ("blocked" if blocked else "dispatched", code, seq),
            )

    def _mark_ack(self, seq: int, receipt: dict, status: int) -> None:
        with self.transaction():
            row = self.connection.execute(
                "SELECT note_ref,operation_id,wire,state FROM note_operations WHERE seq=?",
                (seq,),
            ).fetchone()
            if row is None or row["wire"] is None:
                raise NoteError("invalid_ack")
            note = self.connection.execute(
                "SELECT note_id,revision,terminal FROM notes WHERE note_ref=?",
                (row["note_ref"],),
            ).fetchone()
            if note["terminal"]:
                raise NoteError("note_terminal")
            request = decode_object(row["wire"], SETUP_LIMIT, "store_corrupt")
            validate_receipt(encode_object(receipt), status, request, note["note_id"])
            deleted = receipt["outcome"] == "deleted"
            self.connection.execute(
                "UPDATE notes SET note_id=?,revision=MAX(revision,?),terminal=?,cleanup=? WHERE note_ref=?",
                (
                    receipt["note_id"],
                    receipt["revision"],
                    int(deleted),
                    "deleted" if deleted else "ack",
                    row["note_ref"],
                ),
            )
            self.connection.execute(
                "UPDATE note_operations SET state='acknowledged',cleanup_pending=1,receipt_note_id=?,receipt_revision=?,receipt_outcome=?,error_code='' WHERE seq=?",
                (receipt["note_id"], receipt["revision"], receipt["outcome"], seq),
            )

    def acknowledge(self, seq: int, receipt: dict, status: int) -> None:
        self._mark_ack(seq, receipt, status)
        from devdiary.note_erasure import recover

        recover(self)

    def discard(self, note_ref: str) -> dict:
        self._ready()
        validate_intent("delete", {"note_ref": note_ref})
        with self.transaction():
            note = self.connection.execute(
                "SELECT note_id,terminal FROM notes WHERE note_ref=?", (note_ref,)
            ).fetchone()
            if note is None:
                raise NoteError("note_not_found")
            uncertain = self.connection.execute(
                "SELECT 1 FROM note_operations WHERE note_ref=? AND (wire IS NOT NULL OR attempts>0) LIMIT 1",
                (note_ref,),
            ).fetchone()
            disposition = (
                "abandoned" if note["note_id"] is None and uncertain else "discarded"
            )
            self.connection.execute(
                "UPDATE notes SET cleanup=?,terminal=MAX(terminal,?) WHERE note_ref=?",
                (disposition, int(note["note_id"] is None), note_ref),
            )
        from devdiary.note_erasure import recover

        recover(self)
        return {"state": disposition, "note_ref": note_ref, "remote_deletion": False}

    def sync(self) -> None:
        self.check_paths()
        for name in (DB_NAME, *SIDECARS):
            if self.exists(name):
                fd = self._open_file(name)
                try:
                    os.fsync(fd)
                    _same(os.fstat(fd), self.check_file(name))
                finally:
                    os.close(fd)
        os.fsync(self.directory_fd)
        self.check_paths()
        # Anchor the actual parent through the validated leaf, not a caller alias.
        # Syncing the child alone does not persist the entry naming that child.
        parent_fd = os.open(
            "..",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=self.directory_fd,
        )
        try:
            _same(
                os.fstat(parent_fd),
                os.stat(self.directory.parent, follow_symlinks=False),
            )
            _same(
                os.fstat(self.directory_fd),
                os.stat(self.directory.name, dir_fd=parent_fd, follow_symlinks=False),
            )
            self.check_paths()
            os.fsync(parent_fd)
            _same(
                os.fstat(parent_fd),
                os.stat(self.directory.parent, follow_symlinks=False),
            )
            _same(
                os.fstat(self.directory_fd),
                os.stat(self.directory.name, dir_fd=parent_fd, follow_symlinks=False),
            )
            self.check_paths()
        finally:
            os.close(parent_fd)

    def close_connection(self) -> None:
        if self.connected:
            self.connection.close()
            self.connected = False

    def close(self) -> None:
        try:
            try:
                try:
                    self.close_connection()
                except (OSError, sqlite3.Error):
                    if (
                        self.cleanup_started
                        and self.cleanup_note_ref is not None
                        and not self.exists(INTENT_NAME)
                    ):
                        from devdiary.note_erasure import _publish

                        # Final metadata-connection close is still a public result.
                        # Fence a detectable failure before releasing the writer lock.
                        _publish(self, self.cleanup_note_ref)
                    raise
            finally:
                try:
                    if self.lock_fd >= 0:
                        os.close(self.lock_fd)
                        self.lock_fd = -1
                finally:
                    if self.directory_fd >= 0:
                        os.close(self.directory_fd)
                        self.directory_fd = -1
        except (OSError, sqlite3.Error):
            raise NoteError(
                "purge_pending" if self.cleanup_started else "store_failed"
            ) from None
