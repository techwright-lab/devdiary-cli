from __future__ import annotations

import http.client
import os
import re
import signal
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

from devdiary.note_validation import (
    METADATA_LIMIT,
    SCOPE_PATH,
    NoteError,
    decode_object,
    validate_origin,
    validate_scope,
)
from devdiary.transport import NoRedirectHandler

_WALL_SECONDS = 10


def _expired(signum, frame):
    raise NoteError("network_failure")


@contextmanager
def _deadline():
    if (
        os.name != "posix"
        or threading.current_thread() is not threading.main_thread()
        or not all(
            callable(getattr(signal, name, None))
            for name in ("setitimer", "getitimer", "signal", "getsignal")
        )
        or not all(hasattr(signal, name) for name in ("SIGALRM", "ITIMER_REAL"))
    ):
        raise NoteError("platform_unsupported")
    try:
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        if previous_timer != (0.0, 0.0):
            raise NoteError("platform_unsupported")
        previous_handler = signal.getsignal(signal.SIGALRM)
    except (OSError, ValueError):
        raise NoteError("platform_unsupported") from None
    installed = False
    try:
        signal.signal(signal.SIGALRM, _expired)
        installed = True
        signal.setitimer(signal.ITIMER_REAL, min(_WALL_SECONDS, 10))
        yield
    finally:
        if installed:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                signal.signal(signal.SIGALRM, previous_handler)


def _origin(origin: str, loopback_port: int | None):
    if (
        type(loopback_port) is int
        and 1024 <= loopback_port <= 65535
        and origin
        in {f"http://127.0.0.1:{loopback_port}", f"http://[::1]:{loopback_port}"}
    ):
        return origin
    return validate_origin(origin)


def _response_scope(response):
    if response.status != 200:
        raise NoteError("scope_failed")
    media = response.headers.get("Content-Type", "")
    cache = response.headers.get("Cache-Control", "")
    json_media = re.fullmatch(
        r'application/json(?:\s*;\s*charset=(?:utf-8|"utf-8"))?',
        media,
        re.IGNORECASE | re.ASCII,
    )
    if (
        not json_media
        or "no-store" not in {part.strip().lower() for part in cache.split(",")}
        or response.headers.get("Content-Encoding", "identity").lower() != "identity"
    ):
        raise NoteError("invalid_scope")
    raw = response.read(METADATA_LIMIT + 1)
    return validate_scope(decode_object(raw, METADATA_LIMIT, "invalid_scope"))


def get_scope(origin: str, key: str, *, _loopback_port: int | None = None) -> dict:
    origin = _origin(origin, _loopback_port)
    if not isinstance(key, str) or not re.fullmatch(
        r"dn_live_[A-Za-z0-9_-]{43}", key, re.ASCII
    ):
        raise NoteError("invalid_key")
    response = None
    try:
        with _deadline():
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirectHandler()
            )
            request = urllib.request.Request(
                origin + SCOPE_PATH,
                method="GET",
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer " + key,
                },
            )
            try:
                response = opener.open(request, timeout=10)
                return _response_scope(response)
            finally:
                if response is not None:
                    response.close()
    except urllib.error.HTTPError as error:
        code = {401: "unauthorized", 403: "forbidden"}.get(error.code, "scope_failed")
        error.close()
        raise NoteError(code) from None
    except NoteError:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise NoteError("network_failure") from None
