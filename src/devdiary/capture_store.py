"""Private transactional capture records. No credentials are persisted here."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

from devdiary.capture_validation import SOURCE_FIELDS, CaptureError, dumps
from devdiary.secure_paths import reject_symlink_components

# Private evidence is not a wire envelope. Keep the complete hash/timestamp
# boundary across sibling worktrees, but refuse excessive local state rather
# than silently dropping evidence. Reserve room at begin for the frozen claim,
# 1 MiB envelope, bounded receipt, and delivery bookkeeping.
MAX_PRIVATE_RECORD_BYTES = 64 * 1_048_576
TERMINAL_RESERVE_BYTES = 4 * 1_048_576
PUBLIC_FIELDS = (
    "capture_id",
    "source",
    "actor_ref",
    "run_ref",
    "state",
    "started_at",
    "ended_at",
    "last_attempt_at",
    "error_code",
    "receipt",
)
# Aggregate only allowlisted top-level members, preserving absent vs null and
# nested JSON. Extract only two actor identity fields, never the actor object.
# Use one member walk and a two-path extraction rather than repeating
# full-record extraction for every public field of a potentially 64 MiB record.
_STATUS_SELECT = """
SELECT seq, json_object(
    'public', (SELECT json_group_object(key,
        CASE WHEN type IN ('object', 'array') THEN json(value)
             WHEN type IN ('true', 'false') THEN json(type)
             ELSE value END)
        FROM json_each(captures.record)
        WHERE key IN (SELECT value FROM json_each(?))),
    'git_identity', json_extract(record,
        '$.actor.identities.git_name', '$.actor.identities.git_email')
) AS projection FROM captures WHERE
"""


def dumps_record(record: dict) -> str:
    limit = MAX_PRIVATE_RECORD_BYTES
    if record["state"] == "open":
        limit -= TERMINAL_RESERVE_BYTES
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
    size = 0
    with io.StringIO() as output:
        for chunk in encoder.iterencode(record):
            size += len(chunk.encode("utf-8"))
            if size > limit:
                raise CaptureError("private_record_too_large")
            output.write(chunk)
        return output.getvalue()


def private_file(path: Path, *, readonly: bool = False) -> int:
    reject_symlink_components(path)
    flags = os.O_RDONLY if readonly else os.O_RDWR | os.O_CREAT
    fd = os.open(
        path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600
    )
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077))
    ):
        os.close(fd)
        raise CaptureError("unsafe_private_file")
    return fd


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory.absolute()
        reject_symlink_components(self.directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        reject_symlink_components(self.directory)
        info = self.directory.stat()
        if os.name == "posix" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise CaptureError("unsafe_state_directory")
        path = self.directory / "captures.sqlite3"
        os.close(private_file(path))
        # SQLite sidecars must not be redirected through pre-created links.
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            reject_symlink_components(sidecar)
            if sidecar.exists():
                try:
                    os.close(private_file(sidecar, readonly=True))
                except FileNotFoundError:
                    # A concurrent SQLite commit can remove its rollback journal.
                    pass
        self.db = sqlite3.connect(path, timeout=60, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS captures (seq INTEGER PRIMARY KEY AUTOINCREMENT, capture_id TEXT NOT NULL UNIQUE, run_ref TEXT NOT NULL UNIQUE, state TEXT NOT NULL, record TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS captures_state_seq ON captures(state,seq)"
        )
        # Expression indexes cover old records without rewriting frozen JSON.
        for key in sorted(SOURCE_FIELDS):
            self.db.execute(
                f"CREATE INDEX IF NOT EXISTS captures_source_{key}_state_seq "
                f"ON captures(json_extract(record,'$.source.{key}'),state,seq)"
            )
        # Common orchestrator scopes need multi-field seeks: a system-only
        # index would still walk every unrelated company in that system.
        scope = ["system", "company_id", "agent_id", "run_id"]
        for size in range(2, len(scope) + 1):
            expressions = ",".join(
                f"json_extract(record,'$.source.{key}')" for key in scope[:size]
            )
            self.db.execute(
                f"CREATE INDEX IF NOT EXISTS captures_source_scope_{size}_state_seq "
                f"ON captures({expressions},state,seq)"
            )
        sync_directory(self.directory)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def status_rows(
        self, states: list[str], source: dict, after: int, limit: int
    ) -> list[sqlite3.Row]:
        if set(source) - SOURCE_FIELDS:
            raise CaptureError("invalid_input")
        clauses = ["state=?", "seq>?"]
        values = []
        for key, value in sorted(source.items()):
            # Only allowlisted identifiers are interpolated; values are bound
            # and use SQLite's default binary (exact, case-sensitive) equality.
            clauses.append(f"json_extract(record,'$.source.{key}')=?")
            values.append(value)
        # Clauses contain only SOURCE_FIELDS paths; every data value is bound.
        sql = _STATUS_SELECT + " AND ".join(clauses) + " ORDER BY seq LIMIT ?"
        # Seek each state separately: IN (...) followed by ORDER BY seq can
        # sort an entire state's history before LIMIT. One UNION statement
        # merges at most 3 bounded pages in a consistent SQLite read snapshot.
        if not states:
            return []
        queries = []
        parameters = []
        for state in states:
            # The nested query is built exclusively from fixed/allowlisted SQL.
            queries.append(f"SELECT seq,projection FROM ({sql})")  # nosec B608
            parameters.extend((json.dumps(PUBLIC_FIELDS), state, after, *values, limit))
        parameters.append(limit)
        return self.db.execute(
            " UNION ALL ".join(queries) + " ORDER BY seq LIMIT ?", parameters
        ).fetchall()

    def get(self, capture_id: str) -> dict:
        row = self.db.execute(
            "SELECT record FROM captures WHERE capture_id=?", (capture_id,)
        ).fetchone()
        if row is None:
            raise CaptureError("capture_not_found")
        return json.loads(row["record"])

    def save(self, record: dict) -> None:
        self.db.execute(
            "UPDATE captures SET state=?, record=? WHERE capture_id=?",
            (record["state"], dumps_record(record), record["capture_id"]),
        )

    def context_file(self, capture_id: str, context: dict) -> Path:
        path = self.directory / (capture_id + ".context.json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(dumps(context))
            file.flush()
            os.fsync(file.fileno())
        path.chmod(0o400)
        sync_directory(self.directory)
        return path
