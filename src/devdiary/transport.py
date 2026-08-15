from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from typing import Any


class TransportError(RuntimeError):
    """A safe ingest failure that never contains credential material."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirectHandler)
MAX_DOCUMENT_BYTES = 1_048_576


def post_envelope(
    url: str, key: str, envelope: dict[str, Any], attempts: int = 3
) -> dict[str, Any]:
    payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise TransportError(f"envelope exceeds {MAX_DOCUMENT_BYTES} bytes")
    try:
        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "devdiary/0.1",
            },
            method="POST",
        )
    except (ValueError, http.client.InvalidURL, UnicodeError) as error:
        raise TransportError("ingest endpoint is invalid") from error

    for attempt in range(1, attempts + 1):
        try:
            with OPENER.open(request, timeout=10) as response:
                body = response.read(MAX_DOCUMENT_BYTES + 1)
                if len(body) > MAX_DOCUMENT_BYTES:
                    raise TransportError(
                        f"ingest response exceeds {MAX_DOCUMENT_BYTES} bytes"
                    )
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            if error.code < 500 or attempt == attempts:
                raise TransportError(f"ingest returned HTTP {error.code}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            if attempt == attempts:
                raise TransportError(
                    "ingest could not be reached or returned invalid JSON"
                ) from error
        except (ValueError, http.client.InvalidURL, UnicodeError) as error:
            raise TransportError("ingest endpoint is invalid") from error
        except (http.client.HTTPException, OSError) as error:
            if attempt == attempts:
                raise TransportError(
                    "ingest could not be reached or returned invalid JSON"
                ) from error
        time.sleep(0.2 * attempt)

    raise TransportError("ingest failed")
