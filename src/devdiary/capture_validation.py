"""Strict, bounded wire input: never accept runtime configuration or environment."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, BinaryIO
from urllib.parse import urlparse

MAX_BYTES = 1_048_576
WORK_KINDS = {"repositories", "commits", "pull_requests", "issues", "artifacts"}
SOURCE_FIELDS = {"system", "company_id", "agent_id", "run_id"}
STATES = {"open", "pending", "delivered"}


class CaptureError(ValueError):
    """Only stable, non-sensitive error codes cross the CLI boundary."""


def fail(code: str = "invalid_input") -> None:
    raise CaptureError(code)


def text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        fail()
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail()
    return value


def obj(value: Any, allowed: set[str], required: set[str] | None = None) -> dict:
    if (
        not isinstance(value, dict)
        or set(value) - allowed
        or (required or set()) - set(value)
    ):
        fail()
    return value


def strings(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 10000:
        fail()
    return list(dict.fromkeys(text(v) for v in value))


def source(value: Any) -> dict:
    value = obj(value, SOURCE_FIELDS)
    return {k: text(v) for k, v in value.items()}


def chain(value: Any) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        fail()
    result = []
    for entry in value:
        if not isinstance(entry, dict):
            fail()
        kind = entry.get("kind")
        if kind in ("orchestrator", "executor"):
            obj(entry, {"kind", "system", "session_ref"}, {"kind", "system"})
        elif kind == "model":
            obj(entry, {"kind", "provider", "model"}, {"kind", "provider", "model"})
        else:
            fail()
        result.append({k: text(v) for k, v in entry.items()})
    if not any(e["kind"] in ("executor", "orchestrator") for e in result):
        fail()
    return result


def work(value: Any) -> dict[str, list[str]]:
    value = obj(value, WORK_KINDS)
    result = {k: strings(value.get(k, [])) for k in sorted(WORK_KINDS)}
    for values in result.values():
        for ref in values:
            parsed = urlparse(ref)
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                fail()
    return result


def end_time(value: Any) -> str:
    value = text(value)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        fail()
    return value


def dumps(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > MAX_BYTES:
        fail("payload_too_large")
    return encoded


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for k, v in pairs:
        if k in result:
            fail()
        result[k] = v
    return result


def read_input(stream: BinaryIO) -> dict:
    raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        fail("payload_too_large")
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: fail())
    if not isinstance(value, dict):
        fail()
    return value
