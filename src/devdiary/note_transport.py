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
    ENDPOINT_PATH,
    METADATA_LIMIT,
    SCOPE_PATH,
    SETUP_LIMIT,
    NoteError,
    decode_object,
    validate_origin,
    validate_receipt,
    validate_scope,
    validate_write,
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


class DeliveryError(NoteError):
    def __init__(self, code: str, retryable: bool, retry_after: float | None = None):
        super().__init__(code)
        self.retryable = retryable
        self.retry_after = retry_after


def _post_response(response, request: dict, note_id: str | None):
    if response.status not in (200, 201):
        raise DeliveryError(
            "delivery_failed", response.status == 429 or response.status >= 500
        )
    media = response.headers.get("Content-Type", "")
    cache = response.headers.get("Cache-Control", "")
    if (
        not re.fullmatch(
            r'application/json(?:\s*;\s*charset=(?:utf-8|"utf-8"))?',
            media,
            re.IGNORECASE | re.ASCII,
        )
        or "no-store" not in {part.strip().lower() for part in cache.split(",")}
        or response.headers.get("Content-Encoding", "identity").lower() != "identity"
    ):
        raise DeliveryError("invalid_ack", True)
    try:
        receipt = validate_receipt(
            response.read(METADATA_LIMIT + 1), response.status, request, note_id
        )
    except NoteError:
        raise DeliveryError("invalid_ack", True) from None
    return receipt, response.status


def _http_failure(error: urllib.error.HTTPError) -> DeliveryError:
    code = {
        400: "invalid_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        413: "invalid_request",
        415: "invalid_request",
        422: "invalid_request",
        429: "rate_limited",
    }.get(error.code, "delivery_failed")
    retry_after = None
    header = error.headers.get("Retry-After")
    if header is not None:
        retry_after = (
            float(header)
            if re.fullmatch(r"[0-9]{1,2}", header, re.ASCII) and int(header) <= 30
            else -1
        )
    if error.code == 409:
        try:
            value = decode_object(
                error.read(METADATA_LIMIT + 1), METADATA_LIMIT, "invalid_ack"
            )
            if set(value) == {"error"} and value["error"] == "binding_mismatch":
                code = "binding_mismatch"
        except NoteError as failure:
            if failure.code == "network_failure":
                raise
            code = "conflict"
        except (OSError, ValueError, http.client.HTTPException):
            code = "conflict"
    return DeliveryError(
        code, error.code == 429 or 500 <= error.code <= 599, retry_after
    )


def post_operation(
    origin: str,
    key: str,
    wire: bytes,
    subject_ref: str,
    workspace_ref: str,
    note_id: str | None = None,
    *,
    _loopback_port: int | None = None,
) -> tuple[dict, int]:
    origin = _origin(origin, _loopback_port)
    request_value = validate_write(decode_object(wire, SETUP_LIMIT, "invalid_note"))
    if not isinstance(key, str) or not re.fullmatch(
        r"dn_live_[A-Za-z0-9_-]{43}", key, re.ASCII
    ):
        raise NoteError("invalid_key")
    if (
        not isinstance(subject_ref, str)
        or not re.fullmatch(
            r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}",
            subject_ref,
            re.ASCII,
        )
        or not isinstance(workspace_ref, str)
        or not re.fullmatch(r"workspace_[A-Za-z0-9]+", workspace_ref, re.ASCII)
    ):
        raise NoteError("binding_mismatch")
    response = None
    try:
        with _deadline():
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), NoRedirectHandler()
            )
            request = urllib.request.Request(
                origin + ENDPOINT_PATH,
                data=wire,
                method="POST",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + key,
                    "DevDiary-Notes-Subject": subject_ref,
                    "DevDiary-Notes-Workspace": workspace_ref,
                },
            )
            try:
                try:
                    response = opener.open(request, timeout=10)
                except urllib.error.HTTPError as error:
                    try:
                        failure = _http_failure(error)
                    finally:
                        error.close()
                    raise failure from None
                return _post_response(response, request_value, note_id)
            finally:
                if response is not None:
                    response.close()
    except DeliveryError:
        raise
    except NoteError as error:
        if error.code == "network_failure":
            raise DeliveryError("network_failure", True) from None
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise DeliveryError("network_failure", True) from None
