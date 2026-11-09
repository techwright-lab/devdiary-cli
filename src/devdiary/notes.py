from __future__ import annotations

import time
from typing import BinaryIO

from devdiary.note_profile import (
    BoundProfile,
    check_profile_available,
    create_profile,
    read_key_file,
    require_posix,
)
from devdiary.note_store import Store
from devdiary.note_transport import DeliveryError, get_scope, post_operation
from devdiary.note_validation import (
    SETUP_LIMIT,
    NoteError,
    decode_object,
    read_setup,
    require_matching_scope,
    validate_intent,
)


def configure(directory, stream: BinaryIO) -> dict:
    require_posix()
    setup = read_setup(stream)
    profile = BoundProfile(
        origin=setup["origin"], key_file=setup["key_file"], **setup["metadata"]
    )
    check_profile_available(directory)
    key = read_key_file(profile.key_file)
    actual = get_scope(profile.origin, key)
    require_matching_scope(setup["metadata"], actual)
    create_profile(directory, profile)
    return {"profile": "configured"}


def _selector(command: str, value: dict) -> dict:
    allowed = {
        "status": {"after", "limit"},
        "retry": {"note_ref", "limit"},
        "discard": {"note_ref"},
    }
    if value.keys() - allowed[command] or (
        command == "discard" and set(value) != {"note_ref"}
    ):
        raise NoteError("invalid_note")
    if "note_ref" in value:
        # Reuse the exact opaque identity boundary without accepting body fields.
        validate_intent("delete", {"note_ref": value["note_ref"]})
    if "limit" in value and (
        type(value["limit"]) is not int or not 1 <= value["limit"] <= 100
    ):
        raise NoteError("invalid_note")
    if "after" in value and (type(value["after"]) is not int or value["after"] < 0):
        raise NoteError("invalid_note")
    return value


def flush(store: Store, note_ref: str | None = None, limit: int = 100) -> dict:
    selected: set[int] = set()
    delivered = 0
    deleted = 0
    failures: dict[int, dict] = {}
    while len(selected) < limit:
        heads = [seq for seq in store.heads(note_ref, 100) if seq not in selected]
        if not heads:
            break
        seq = heads[0]
        selected.add(seq)
        existing = store.connection.execute(
            "SELECT state,error_code FROM note_operations WHERE seq=?", (seq,)
        ).fetchone()
        if existing["state"] == "blocked" and existing["error_code"] not in (
            "unauthorized",
            "forbidden",
            "binding_mismatch",
        ):
            failures[seq] = {
                "state": "blocked",
                "error_code": existing["error_code"] or "operation_blocked",
            }
            continue
        operation = store.freeze(seq)
        if operation is None:
            continue
        for attempt in range(3):
            store.record_attempt(seq)
            try:
                key = read_key_file(operation["key_file"])
                mapped = store.connection.execute(
                    "SELECT note_id FROM notes WHERE note_ref=?",
                    (operation["note_ref"],),
                ).fetchone()[0]
                receipt, status = post_operation(
                    operation["origin"],
                    key,
                    operation["wire"],
                    operation["subject_ref"],
                    operation["workspace_ref"],
                    mapped,
                )
            except DeliveryError as error:
                store.record_failure(seq, error.code, not error.retryable)
                pending = {
                    "state": "pending" if error.retryable else "blocked",
                    "error_code": error.code,
                }
                failures[seq] = pending
                if not error.retryable or attempt == 2 or error.retry_after == -1:
                    if error.code in ("unauthorized", "forbidden", "binding_mismatch"):
                        return {**pending, "operations": delivered}
                    break
                time.sleep(max((0.2, 0.4)[attempt], error.retry_after or 0))
            else:
                store.acknowledge(seq, receipt, status)
                delivered += 1
                deleted += int(receipt["outcome"] == "deleted")
                failures.pop(seq, None)
                break
    if failures:
        return {**next(iter(failures.values())), "operations": delivered}
    remaining = store.heads(note_ref, 1)
    return {
        "state": "pending"
        if remaining
        else ("deleted" if delivered and deleted == delivered else "acknowledged"),
        "operations": delivered,
    }


def local_command(directory, command: str, stream: BinaryIO) -> dict:
    with Store.open(directory) as store:
        value = decode_object(stream.read(SETUP_LIMIT + 1), SETUP_LIMIT, "invalid_note")
        if command in ("create", "update", "delete"):
            return store.enqueue(command, value)
        if command not in ("status", "retry", "discard"):
            raise NoteError("invalid_note")
        _selector(command, value)
        if command == "status":
            return {
                "operations": store.status(
                    value.get("after", 0), value.get("limit", 100)
                )
            }
        if command == "discard":
            return store.discard(value["note_ref"])
        return flush(store, value.get("note_ref"), value.get("limit", 100))
