from __future__ import annotations

import hmac
import json
import os
import stat
from hashlib import sha256
from pathlib import Path
from typing import Any

from devdiary_attribution.secure_paths import UnsafePathError, reject_symlink_components
from devdiary_attribution.transport import TransportError, post_envelope


class SpoolError(RuntimeError):
    pass


MAX_ENVELOPE_BYTES = 1_048_576
QUEUE_VERSION = 1
INTEGRITY_ALGORITHM = "hmac-sha256"
SIGNATURE_DOMAIN = b"devdiary-attribution-spool-v1\0"


def enqueue(directory: Path, envelope: dict[str, Any], key: str) -> Path:
    if not key:
        raise SpoolError("an ingest key is required to authenticate a queue entry")
    _reject_symlinks(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _reject_symlinks(directory)
    directory.chmod(0o700)
    event_id = envelope["event_id"]
    queued = {
        "version": QUEUE_VERSION,
        "envelope": envelope,
        "integrity": {
            "algorithm": INTEGRITY_ALGORITHM,
            "value": _signature(envelope, key),
        },
    }
    payload = json.dumps(queued, indent=2, sort_keys=True) + "\n"
    if len(payload.encode("utf-8")) > MAX_ENVELOPE_BYTES:
        raise SpoolError(f"envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
    filename = sha256(event_id.encode("utf-8")).hexdigest()
    path = directory / f"{filename}.json"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(payload)
        file.flush()
        os.fsync(file.fileno())
    path.chmod(0o600)
    return path


def pending(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    _reject_symlinks(directory)
    return sorted(
        path
        for path in directory.glob("*.json")
        if path.is_file() and not path.is_symlink()
    )


def flush(directory: Path, url: str, key: str) -> tuple[int, list[str]]:
    delivered = 0
    errors: list[str] = []
    for path in pending(directory):
        try:
            envelope = _read_verified(path, key)
            post_envelope(url, key, envelope)
            path.unlink()
            delivered += 1
        except (
            OSError,
            json.JSONDecodeError,
            TransportError,
            SpoolError,
            KeyError,
            TypeError,
            UnicodeError,
        ) as error:
            errors.append(f"{path.name}: {error}")
    return delivered, errors


def _read_verified(path: Path, key: str) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as file:
        metadata = os.fstat(file.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise SpoolError("queued entry is not a regular file")
        if metadata.st_size > MAX_ENVELOPE_BYTES:
            raise SpoolError(f"queued envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
        queued = json.load(file)
    if not isinstance(queued, dict) or set(queued) != {
        "version",
        "envelope",
        "integrity",
    }:
        raise SpoolError("queued entry has an invalid authenticated format")
    if queued["version"] != QUEUE_VERSION:
        raise SpoolError("queued entry version is unsupported")
    envelope = queued["envelope"]
    integrity = queued["integrity"]
    if not isinstance(envelope, dict) or not isinstance(integrity, dict):
        raise SpoolError("queued entry has an invalid authenticated format")
    if integrity.get("algorithm") != INTEGRITY_ALGORITHM or not isinstance(
        integrity.get("value"), str
    ):
        raise SpoolError("queued entry integrity metadata is invalid")
    expected = _signature(envelope, key)
    if not hmac.compare_digest(expected, integrity["value"]):
        raise SpoolError("queued entry integrity check failed")
    event_id = envelope.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        raise SpoolError("queued envelope has no event identity")
    expected_filename = f"{sha256(event_id.encode('utf-8')).hexdigest()}.json"
    if path.name != expected_filename:
        raise SpoolError("queued entry filename does not match its event identity")
    return envelope


def _signature(envelope: dict[str, Any], key: str) -> str:
    canonical = json.dumps(
        envelope, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hmac.new(
        key.encode("utf-8"), SIGNATURE_DOMAIN + canonical, sha256
    ).hexdigest()


def _reject_symlinks(path: Path) -> None:
    try:
        reject_symlink_components(path)
    except UnsafePathError as error:
        raise SpoolError(str(error)) from error
