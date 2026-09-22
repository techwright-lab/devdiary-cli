from __future__ import annotations

import http.client
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest import mock

from devdiary.transport import TransportError, post_envelope

DUMMY_KEY = "test-only-not-a-live-key"


class RedirectTarget(BaseHTTPRequestHandler):
    requests: ClassVar[int] = 0

    def do_POST(self) -> None:
        self.__class__.requests += 1
        self.send_response(201)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


class RedirectSource(BaseHTTPRequestHandler):
    target: ClassVar[str]

    def do_POST(self) -> None:
        self.send_response(307)
        self.send_header("Location", self.__class__.target)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


class TransportTest(unittest.TestCase):
    def test_http_error_response_is_closed_before_raising(self) -> None:
        response = mock.Mock()
        error = urllib.error.HTTPError(
            "http://127.0.0.1/ingest", 503, "unavailable", mock.Mock(), response
        )
        with (
            mock.patch("devdiary.transport.OPENER.open", side_effect=error),
            self.assertRaises(TransportError),
        ):
            post_envelope("http://127.0.0.1/ingest", DUMMY_KEY, {}, attempts=1)
        response.close.assert_called_once()

    def test_excessive_response_nesting_is_safe_transport_failure(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"[" * 10000 + b"]" * 10000
        with (
            mock.patch("devdiary.transport.OPENER.open", return_value=response),
            self.assertRaises(TransportError),
        ):
            post_envelope("http://127.0.0.1/ingest", DUMMY_KEY, {}, attempts=1)

    def setUp(self) -> None:
        RedirectTarget.requests = 0
        self.target = ThreadingHTTPServer(("127.0.0.1", 0), RedirectTarget)
        RedirectSource.target = f"http://127.0.0.1:{self.target.server_port}/capture"
        self.source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectSource)
        self.threads = [
            threading.Thread(target=self.target.serve_forever, daemon=True),
            threading.Thread(target=self.source.serve_forever, daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def tearDown(self) -> None:
        for server in (self.source, self.target):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def test_authorization_is_never_forwarded_across_redirects(self) -> None:
        envelope = {"event_id": "urn:devdiary:event:test"}
        url = f"http://127.0.0.1:{self.source.server_port}/ingest"

        with self.assertRaisesRegex(TransportError, "HTTP 307") as raised:
            post_envelope(url, DUMMY_KEY, envelope, attempts=1)

        self.assertEqual(0, RedirectTarget.requests)
        self.assertNotIn(DUMMY_KEY, str(raised.exception))
        self.assertNotIn(DUMMY_KEY, json.dumps(envelope))

    def test_malformed_endpoint_is_wrapped_as_a_safe_transport_error(self) -> None:
        with self.assertRaisesRegex(TransportError, "invalid") as raised:
            post_envelope(
                "https://example.test:not-a-port/ingest",
                DUMMY_KEY,
                {"event_id": "urn:devdiary:event:test"},
                attempts=1,
            )

        self.assertNotIn(DUMMY_KEY, str(raised.exception))

    def test_http_success_without_bound_receipt_is_not_delivery(self) -> None:
        envelope = {
            "event_id": "urn:devdiary:event:test",
            "actor": {"ref": "actor:one"},
            "run_ref": "run:one",
        }
        with self.assertRaisesRegex(TransportError, "receipt mismatch"):
            post_envelope(
                f"http://127.0.0.1:{self.target.server_port}/ingest",
                DUMMY_KEY,
                envelope,
                attempts=1,
            )

    def test_response_phase_disconnect_is_retried_then_wrapped_safely(self) -> None:
        disconnect = http.client.RemoteDisconnected("peer closed connection")
        with (
            mock.patch(
                "devdiary.transport.OPENER.open",
                side_effect=disconnect,
            ) as open_request,
            mock.patch("devdiary.transport.time.sleep"),
            self.assertRaisesRegex(TransportError, "could not be reached") as raised,
        ):
            post_envelope(
                "https://example.test/ingest",
                DUMMY_KEY,
                {"event_id": "urn:devdiary:event:test"},
                attempts=2,
            )

        self.assertEqual(2, open_request.call_count)
        self.assertNotIn(DUMMY_KEY, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
