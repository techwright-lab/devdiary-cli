from __future__ import annotations

import ipaddress
import json
import math
import re
from typing import BinaryIO

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


class NoteError(ValueError):
    def __init__(self, code: str):
        self.code = code if code in ERROR_CODES else "configure_failed"
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
