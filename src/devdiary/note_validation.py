from __future__ import annotations

import ipaddress
import json
import math
import re
from datetime import datetime
from typing import BinaryIO
from urllib.parse import parse_qsl, urlsplit

SETUP_LIMIT = 262_144
METADATA_LIMIT = 4096
SCOPE_PATH = "/ingest/v1/notes/scope"
ENDPOINT_PATH = "/ingest/v1/notes"
SCOPE_FIELDS = frozenset(
    {
        "schema_version",
        "subject_ref",
        "workspace_ref",
        "capability",
        "endpoint_path",
        "scope_path",
    }
)
ERROR_CODES = frozenset(
    {
        "invalid_setup",
        "invalid_origin",
        "invalid_scope",
        "invalid_key",
        "unsafe_path",
        "platform_unsupported",
        "binding_required",
        "binding_mismatch",
        "profile_exists",
        "profile_failed",
        "unauthorized",
        "forbidden",
        "network_failure",
        "scope_failed",
        "configure_failed",
    }
)


# ERROR_CODES remains the configure boundary's allowlist.
OUTBOX_ERROR_CODES = frozenset(
    {
        "invalid_note",
        "invalid_ack",
        "store_failed",
        "store_corrupt",
        "store_busy",
        "purge_pending",
        "queue_full",
        "note_terminal",
        "note_not_found",
        "operation_blocked",
        "not_found",
        "conflict",
        "invalid_request",
        "delivery_failed",
        "rate_limited",
    }
)


class NoteError(ValueError):
    def __init__(self, code: str):
        self.code = (
            code if code in ERROR_CODES | OUTBOX_ERROR_CODES else "configure_failed"
        )
        super().__init__(self.code)


def encode_object(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError


def _check_depth(text: str):
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 8:
                raise ValueError
        elif char in "]}":
            depth -= 1


def _check_values(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_values(key)
            _check_values(item)
    elif isinstance(value, list):
        for item in value:
            _check_values(item)


def decode_object(raw: bytes, limit: int, code: str) -> dict:
    try:
        if not isinstance(raw, bytes) or len(raw) > limit:
            raise ValueError
        text = raw.decode("utf-8", errors="strict")
        _check_depth(text)
        result = json.loads(text, object_pairs_hook=_pairs, parse_constant=_nonfinite)
        if not isinstance(result, dict):
            raise TypeError
        _check_values(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NoteError(code) from None


def validate_locator(value: str, code="invalid_setup") -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or any(part in {"", ".", ".."} for part in value[1:].split("/"))
        or any(ord(char) < 32 or ord(char) == 127 or char in "~$" for char in value)
    ):
        raise NoteError(code)
    try:
        if len(value.encode("utf-8")) > METADATA_LIMIT:
            raise ValueError
    except (ValueError, UnicodeError):
        raise NoteError(code) from None
    return value


def validate_origin(value: str) -> str:
    try:
        if (
            not isinstance(value, str)
            or not value.isascii()
            or not value.startswith("https://")
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError
        authority = value[8:]
        if any(char in authority for char in "/?#@\\%"):
            raise ValueError
        port = None
        if authority.startswith("["):
            match = re.fullmatch(r"\[([^]]+)\](?::([0-9]+))?", authority, re.ASCII)
            if not match:
                raise ValueError
            host, port = match.groups()
            if str(ipaddress.IPv6Address(host)) != host:
                raise ValueError
        else:
            match = re.fullmatch(r"([^:]+)(?::([0-9]+))?", authority, re.ASCII)
            if not match:
                raise ValueError
            host, port = match.groups()
            try:
                address = ipaddress.IPv4Address(host)
            except ValueError:
                numeric_host = re.fullmatch(
                    r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host
                )
                if (
                    len(host) > 253
                    or numeric_host
                    or any(
                        not re.fullmatch(
                            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label, re.ASCII
                        )
                        for label in host.split(".")
                    )
                ):
                    raise ValueError
            else:
                if str(address) != host:
                    raise ValueError
        if port is not None and (
            not 1 <= int(port) <= 65535 or str(int(port)) != port or port == "443"
        ):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise NoteError("invalid_origin") from None


def validate_scope(value: dict) -> dict:
    try:
        if not isinstance(value, dict) or set(value) != SCOPE_FIELDS:
            raise ValueError
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError
        if any(
            not isinstance(value[key], str) for key in SCOPE_FIELDS - {"schema_version"}
        ):
            raise ValueError
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}",
            value["subject_ref"],
            re.ASCII,
        ):
            raise ValueError
        if not re.fullmatch(
            r"workspace_[A-Za-z0-9]+", value["workspace_ref"], re.ASCII
        ):
            raise ValueError
        if (
            value["capability"] != "notes:write"
            or value["endpoint_path"] != ENDPOINT_PATH
            or value["scope_path"] != SCOPE_PATH
            or len(encode_object(value)) > METADATA_LIMIT
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise NoteError("invalid_scope") from None
    return dict(value)


def validate_setup(value: dict) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "origin", "key_file", "metadata"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise NoteError("invalid_setup")
    validate_origin(value["origin"])
    validate_locator(value["key_file"])
    validate_scope(value["metadata"])
    return value


def read_setup(stream: BinaryIO) -> dict:
    return validate_setup(
        decode_object(stream.read(SETUP_LIMIT + 1), SETUP_LIMIT, "invalid_setup")
    )


def require_matching_scope(expected: dict, actual: dict) -> None:
    if validate_scope(expected) != validate_scope(actual):
        raise NoteError("binding_mismatch")


def _identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.isascii()
        and 1 <= len(value) <= 200
        and all(32 < ord(char) < 127 for char in value)
    )


def _write_shape(value: dict) -> dict:
    if (
        not isinstance(value, dict)
        or value.get("action") not in ("create", "update", "delete")
        or not {"action", "operation_id", "note_ref"} <= value.keys()
        or value.keys()
        - {
            "action",
            "operation_id",
            "note_ref",
            "attributes",
            "context",
            "references",
            "expected_revision",
        }
        or not _identity(value["operation_id"])
        or not _identity(value["note_ref"])
    ):
        raise NoteError("invalid_note")
    action = value["action"]
    attributes = value.get("attributes", {})
    if (
        not isinstance(attributes, dict)
        or attributes.keys()
        - {"content", "next_step", "source", "recorded_at", "occurred_at"}
        or any(
            item is not None and not isinstance(item, str)
            for item in attributes.values()
        )
    ):
        raise NoteError("invalid_note")
    if action == "create":
        if (
            "expected_revision" in value
            or (
                "context" in value and value["context"] not in ("workspace", "personal")
            )
            or not isinstance(attributes.get("content"), str)
            or not attributes["content"].strip()
        ):
            raise NoteError("invalid_note")
    elif (
        "context" in value
        or "recorded_at" in attributes
        or type(value.get("expected_revision")) is not int
        or value["expected_revision"] <= 0
        or (action == "delete" and (attributes or "references" in value))
    ):
        raise NoteError("invalid_note")
    for field, limit in (("content", 16384), ("next_step", 4096), ("source", 200)):
        item = attributes.get(field)
        if item is not None and len(item) > limit:
            raise NoteError("invalid_note")
    for field in ("recorded_at", "occurred_at"):
        item = attributes.get(field)
        if item is not None:
            if not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})",
                item,
                re.ASCII,
            ):
                raise NoteError("invalid_note")
            datetime.fromisoformat(item)
    if "references" in value:
        _references(value["references"])
    return value


def _references(values: object) -> None:
    if not isinstance(values, list) or len(values) > 32:
        raise NoteError("invalid_note")
    for value in values:
        if (
            not isinstance(value, dict)
            or not {"kind", "ref"} <= value.keys()
            or value.keys() - {"kind", "ref", "relation"}
            or not all(isinstance(item, str) for item in value.values())
        ):
            raise NoteError("invalid_note")
        kind, ref, relation = (
            value["kind"],
            value["ref"],
            value.get("relation", "context"),
        )
        if (
            kind
            not in (
                "work",
                "work_session",
                "stream",
                "note",
                "external",
                "document",
                "run",
            )
            or relation not in ("context", "supersedes", "follows_from")
            or (relation != "context" and kind != "note")
            or not 1 <= len(ref) <= 2048
            or ref != ref.strip()
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in ref)
        ):
            raise NoteError("invalid_note")
        if kind in ("work", "work_session", "stream", "note"):
            safe = all(char.isalnum() or char in "_.:-" for char in ref)
        elif kind == "run" and all(char.isalnum() or char in "_.-" for char in ref):
            safe = True
        elif kind == "document" and not re.match(r"https?:", ref, re.IGNORECASE):
            path = ref.removeprefix("vault:")
            safe = (
                bool(path)
                and not path.startswith(("/", "\\", "~"))
                and not any(char in path for char in ":%")
                and not any(
                    part in ("", ".", "..") for part in re.split(r"[\\/]", path)
                )
            )
        else:
            safe = _external_reference(ref)
        if not safe:
            raise NoteError("invalid_note")


def _external_reference(ref: str) -> bool:
    if (
        not ref.isascii()
        or re.search(r'[\s<>"{}|\\^`]', ref)
        or re.search(r"%(?![0-9a-fA-F]{2})", ref)
        or not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", ref)
    ):
        return False
    uri = urlsplit(ref)
    if (
        uri.scheme.lower() in ("javascript", "data", "file", "vbscript", "mailto")
        or uri.username is not None
    ):
        return False
    if uri.scheme.lower() in ("http", "https") and not uri.hostname:
        return False
    parts = [uri.query]
    if "=" in uri.fragment:
        parts.append(uri.fragment)
    return not any(
        re.search(
            r"token|password|secret|signature|api.?key|authorization",
            key,
            re.IGNORECASE,
        )
        for part in parts
        for key, _ in parse_qsl(part, keep_blank_values=True)
    )


def validate_write(value: dict) -> dict:
    try:
        _write_shape(value)
        _check_values(value)
        raw = encode_object(value)
        decode_object(raw, SETUP_LIMIT, "invalid_note")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise NoteError("invalid_note") from None


def freeze_write(value: dict) -> bytes:
    return encode_object(validate_write(value))


def read_write(stream: BinaryIO) -> dict:
    return validate_write(
        decode_object(stream.read(SETUP_LIMIT + 1), SETUP_LIMIT, "invalid_note")
    )


def validate_intent(action: str, value: dict) -> dict:
    fields = {
        "create": ({"attributes"}, {"attributes", "context", "references"}),
        "update": (
            {"note_ref", "attributes"},
            {"note_ref", "attributes", "references", "expected_revision"},
        ),
        "delete": ({"note_ref"}, {"note_ref", "expected_revision"}),
    }
    if action not in fields or not isinstance(value, dict):
        raise NoteError("invalid_note")
    required, allowed = fields[action]
    if not required <= value.keys() or value.keys() - allowed:
        raise NoteError("invalid_note")
    wire = {
        **value,
        "action": action,
        "operation_id": "op:validation",
        "note_ref": value.get("note_ref", "ref:validation"),
    }
    if action != "create" and "expected_revision" not in wire:
        wire["expected_revision"] = 1
    validate_write(wire)
    return value


def validate_receipt(
    raw: bytes, status: int, request: dict, note_id: str | None = None
) -> dict:
    value = decode_object(raw, METADATA_LIMIT, "invalid_ack")
    if (
        set(value) != {"operation_id", "note_ref", "note_id", "revision", "outcome"}
        or value["operation_id"] != request["operation_id"]
        or value["note_ref"] != request["note_ref"]
        or not _identity(value["note_id"])
        or not value["note_id"].startswith("note_")
        or len(value["note_id"]) <= 5
        or (note_id is not None and value["note_id"] != note_id)
        or type(value["revision"]) is not int
        or value["revision"] <= 0
        or type(status) is not int
        or status not in (200, 201)
        or value["outcome"] not in ("applied", "replayed", "deleted")
    ):
        raise NoteError("invalid_ack")
    action, outcome, revision = request["action"], value["outcome"], value["revision"]
    if status == 201:
        valid = action == "create" and outcome == "applied" and revision == 1
    elif outcome == "deleted":
        # A tombstone's current revision can differ from a stale request's base.
        valid = True
    elif action == "create":
        valid = outcome == "replayed" and revision == 1
    elif action == "update":
        valid = revision == request["expected_revision"] + 1
    else:
        valid = False
    if not valid:
        raise NoteError("invalid_ack")
    return value
