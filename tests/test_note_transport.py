from __future__ import annotations

import io
import json
import os
import signal
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

from test_note_validation import scope

from devdiary import note_transport as transport
from devdiary.note_validation import NoteError

KEY = "dn_live_" + "a" * 43


class Response(io.BytesIO):
    def __init__(
        self,
        raw=None,
        status=200,
        media="application/json",
        cache="no-store",
        encoding=None,
    ):
        super().__init__(json.dumps(scope()).encode() if raw is None else raw)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = media
        self.headers["Cache-Control"] = cache
        self.headers["Content-Length"] = "1"
        if encoding:
            self.headers["Content-Encoding"] = encoding
        self.read_sizes: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


class NotePostTransportTest(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("POSIX notes writes are unsupported")
        self.request = {
            "action": "create",
            "operation_id": "op:post",
            "note_ref": "ref:post",
            "attributes": {"content": "SYNTHETIC_POST"},
        }
        self.raw = json.dumps(self.request, separators=(",", ":")).encode()
        self.receipt = {
            "operation_id": "op:post",
            "note_ref": "ref:post",
            "note_id": "note_post",
            "revision": 1,
            "outcome": "applied",
        }

    def call(self, response):
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            transport.urllib.request, "build_opener", return_value=opener
        ) as build:
            result = transport.post_operation(
                "https://example.test",
                KEY,
                self.raw,
                scope()["subject_ref"],
                scope()["workspace_ref"],
            )
        return result, opener, build

    def test_exact_post_framing_and_resources(self):
        response = Response(raw=json.dumps(self.receipt).encode(), status=201)
        result, opener, build = self.call(response)
        self.assertEqual(result, (self.receipt, 201))
        request = opener.open.call_args.args[0]
        self.assertEqual(request.data, self.raw)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://example.test/ingest/v1/notes")
        headers = dict(request.header_items())
        self.assertEqual(headers["Devdiary-notes-subject"], scope()["subject_ref"])
        self.assertEqual(headers["Devdiary-notes-workspace"], scope()["workspace_ref"])
        self.assertEqual(headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 10})
        self.assertTrue(
            any(
                isinstance(handler, transport.NoRedirectHandler)
                for handler in build.call_args.args
            )
        )
        self.assertEqual(response.read_sizes, [4097])
        self.assertTrue(response.closed)

    def test_error_body_read_stays_inside_total_wall_deadline(self):
        class Slow(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                time.sleep(0.1)
                return super().read(size)

        error = urllib.error.HTTPError(
            "https://example.test/private",
            409,
            "private",
            Message(),
            Slow(b'{"error":"binding_mismatch"}'),
        )
        opener = mock.Mock()
        opener.open.side_effect = error
        with (
            mock.patch.object(transport, "_WALL_SECONDS", 0.02),
            mock.patch.object(
                transport.urllib.request, "build_opener", return_value=opener
            ),
            self.assertRaises(transport.DeliveryError) as raised,
        ):
            transport.post_operation(
                "https://example.test",
                KEY,
                self.raw,
                scope()["subject_ref"],
                scope()["workspace_ref"],
            )
        self.assertEqual(raised.exception.code, "network_failure")

    def test_wrong_status_invalid_ack_and_http_classification(self):
        for response in (
            Response(raw=json.dumps(self.receipt).encode(), status=200),
            Response(raw=b"{}", status=201),
            Response(raw=b"x" * 4097, status=201),
            Response(
                raw=json.dumps(self.receipt).encode(), status=201, cache="private"
            ),
        ):
            with self.assertRaises(transport.DeliveryError) as raised:
                self.call(response)
            self.assertEqual(raised.exception.code, "invalid_ack")
            self.assertTrue(raised.exception.retryable)
            self.assertTrue(response.closed)
        for status, code, retryable in (
            (401, "unauthorized", False),
            (403, "forbidden", False),
            (404, "not_found", False),
            (409, "conflict", False),
            (422, "invalid_request", False),
            (429, "rate_limited", True),
            (500, "delivery_failed", True),
            (302, "delivery_failed", False),
        ):
            headers = Message()
            headers["Retry-After"] = "31"
            error = urllib.error.HTTPError(
                "https://example.test/PRIVATE",
                status,
                "PRIVATE_REASON",
                headers,
                io.BytesIO(b'{"error":"conflict","private":"SECRET"}'),
            )
            opener = mock.Mock()
            opener.open.side_effect = error
            with (
                mock.patch.object(
                    transport.urllib.request, "build_opener", return_value=opener
                ),
                self.assertRaises(transport.DeliveryError) as raised,
            ):
                transport.post_operation(
                    "https://example.test",
                    KEY,
                    self.raw,
                    scope()["subject_ref"],
                    scope()["workspace_ref"],
                )
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.retryable, retryable)
            self.assertNotIn("PRIVATE", str(raised.exception))
            if status == 429:
                self.assertEqual(raised.exception.retry_after, -1)


class NoteTransportTest(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("notes scope integration requires POSIX")

    def rejects(self, action, code):
        with self.assertRaises(NoteError) as raised:
            action()
        self.assertTrue(
            str(raised.exception) == code, "stable transport error required"
        )
        self.assertIsNone(raised.exception.__cause__)

    def call_fake(self, response):
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            transport.urllib.request, "build_opener", return_value=opener
        ) as build:
            actual = transport.get_scope("https://example.test", KEY)
        return actual, opener, build

    def test_fixed_get_headers_no_proxy_no_redirect_and_default_tls(self):
        response = Response()
        handler = signal.getsignal(signal.SIGALRM)
        proxy_type = urllib.request.ProxyHandler
        created_proxies: list[urllib.request.ProxyHandler] = []

        def create_proxy(proxies: dict[str, str]) -> urllib.request.ProxyHandler:
            proxy = proxy_type(proxies)
            created_proxies.append(proxy)
            return proxy

        with mock.patch.object(
            transport.urllib.request, "ProxyHandler", side_effect=create_proxy
        ) as proxy_factory:
            actual, opener, build = self.call_fake(response)
        proxy_factory.assert_called_once_with({})
        self.assertTrue(actual == scope())
        request = opener.open.call_args.args[0]
        self.assertTrue(
            request.full_url == "https://example.test/ingest/v1/notes/scope"
        )
        self.assertTrue(request.get_method() == "GET" and request.data is None)
        self.assertTrue(
            dict(request.header_items())
            == {"Authorization": "Bearer " + KEY, "Accept": "application/json"}
        )
        self.assertEqual(10, opener.open.call_args.kwargs["timeout"])
        handlers = build.call_args.args
        self.assertTrue(len(created_proxies) == 1, "one real proxy handler required")
        self.assertTrue(isinstance(created_proxies[0], proxy_type))
        self.assertTrue(any(h is created_proxies[0] for h in handlers))
        self.assertTrue(
            any(isinstance(h, transport.NoRedirectHandler) for h in handlers)
        )
        self.assertFalse(build.call_args.kwargs, "no custom TLS context")
        self.assertTrue(response.closed)
        self.assertTrue(response.read_sizes == [4097])
        self.assertTrue(signal.getsignal(signal.SIGALRM) == handler)
        self.assertTrue(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))

    def test_success_requires_status_media_no_store_encoding_and_exact_scope(self):
        cases = [
            (Response(status=201), "scope_failed"),
            (Response(media="text/json"), "invalid_scope"),
            (Response(media="application/json; charset=latin1"), "invalid_scope"),
            (Response(cache="private"), "invalid_scope"),
            (Response(encoding="gzip"), "invalid_scope"),
            (Response(raw=b"x" * 4097), "invalid_scope"),
            (Response(raw=b"{}"), "invalid_scope"),
            (Response(raw=b""), "invalid_scope"),
            (Response(raw=b'{"a":1,"a":2}'), "invalid_scope"),
        ]
        for response, code in cases:
            self.rejects(lambda response=response: self.call_fake(response), code)
            self.assertTrue(response.closed)
            self.assertTrue(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))
        for media in (
            "application/json; charset=utf-8",
            'application/json; charset="UTF-8"',
        ):
            self.assertTrue(self.call_fake(Response(media=media))[0] == scope())

    def test_http_errors_are_closed_once_without_body_reads_or_retry(self):
        for status, code in (
            (301, "scope_failed"),
            (401, "unauthorized"),
            (403, "forbidden"),
            (500, "scope_failed"),
        ):
            body = Response(raw=b"SENTINEL_BODY")
            error = urllib.error.HTTPError(
                "https://SENTINEL_URL", status, "SENTINEL_REASON", Message(), body
            )
            opener = mock.Mock()
            opener.open.side_effect = error
            with mock.patch.object(
                transport.urllib.request, "build_opener", return_value=opener
            ):
                self.rejects(
                    lambda: transport.get_scope("https://example.test", KEY), code
                )
            self.assertTrue(body.closed)
            self.assertFalse(body.read_sizes)
            self.assertEqual(1, opener.open.call_count)
            self.assertTrue(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))

    def test_network_errors_restore_timer_and_handler_and_suppress_exception_text(self):
        handler = signal.getsignal(signal.SIGALRM)
        opener = mock.Mock()
        opener.open.side_effect = OSError("SENTINEL_KEY_PATH_PRINCIPAL")
        with mock.patch.object(
            transport.urllib.request, "build_opener", return_value=opener
        ):
            self.rejects(
                lambda: transport.get_scope("https://example.test", KEY),
                "network_failure",
            )
        self.assertTrue(signal.getsignal(signal.SIGALRM) == handler)
        self.assertTrue(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))

    def test_timer_unavailable_active_alarm_and_off_main_thread_fail_before_open(self):
        with (
            mock.patch.object(transport.signal, "setitimer", None),
            mock.patch.object(transport.urllib.request, "build_opener") as build,
        ):
            self.rejects(
                lambda: transport.get_scope("https://example.test", KEY),
                "platform_unsupported",
            )
            build.assert_not_called()
        with (
            mock.patch.object(transport.signal, "getitimer", return_value=(1.0, 0.0)),
            mock.patch.object(transport.signal, "setitimer") as timer,
        ):
            self.rejects(
                lambda: transport.get_scope("https://example.test", KEY),
                "platform_unsupported",
            )
            timer.assert_not_called()
        errors = []

        def off_thread():
            try:
                transport.get_scope("https://example.test", KEY)
            except NoteError as error:
                errors.append(error.code)

        thread = threading.Thread(target=off_thread)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors == ["platform_unsupported"])

    def test_invalid_origins_and_injected_key_fail_without_network(self):
        for origin in (
            "http://127.0.0.1:12345",
            "https://example.test/",
            "http://localhost:12345",
        ):
            with mock.patch.object(transport.urllib.request, "build_opener") as build:
                self.rejects(
                    lambda origin=origin: transport.get_scope(origin, KEY),
                    "invalid_origin",
                )
                build.assert_not_called()
        self.rejects(
            lambda: transport.get_scope("https://example.test", KEY + "\r\nX: a"),
            "invalid_key",
        )

    def test_total_deadline_covers_open_and_trickled_reads_with_restoration(self):
        handler = signal.getsignal(signal.SIGALRM)
        opener = mock.Mock()

        def slow_open(*args, **kwargs):
            time.sleep(0.2)

        opener.open.side_effect = slow_open
        started = time.monotonic()
        with (
            mock.patch.object(transport, "_WALL_SECONDS", 0.05),
            mock.patch.object(
                transport.urllib.request, "build_opener", return_value=opener
            ),
        ):
            self.rejects(
                lambda: transport.get_scope("https://example.test", KEY),
                "network_failure",
            )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(signal.getsignal(signal.SIGALRM) == handler)
        self.assertTrue(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))

    def test_literal_loopback_get_redirects_oversize_and_trickle_cleanup(self):
        requests = []
        route = {"status": 200, "raw": json.dumps(scope()).encode(), "trickle": False}
        stop = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(route["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Location", f"http://127.0.0.1:{port}/target")
                self.end_headers()
                try:
                    if route["trickle"]:
                        for byte in route["raw"]:
                            if stop.wait(0.01):
                                break
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                    else:
                        self.wfile.write(route["raw"])
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_port
        server.daemon_threads = False
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        origin = f"http://127.0.0.1:{port}"
        try:
            with mock.patch.dict(
                "os.environ",
                {
                    "http_proxy": "http://127.0.0.1:1",
                    "https_proxy": "http://127.0.0.1:1",
                    "no_proxy": "",
                },
            ):
                self.assertTrue(
                    transport.get_scope(origin, KEY, _loopback_port=port) == scope()
                )
                for code in (301, 302, 303, 307, 308):
                    route["status"] = code
                    self.rejects(
                        lambda: transport.get_scope(origin, KEY, _loopback_port=port),
                        "scope_failed",
                    )
                self.assertTrue(
                    all(path == "/ingest/v1/notes/scope" for path, _ in requests),
                    "redirect target reached",
                )
                self.assertEqual(6, len(requests))
                route.update(status=200, raw=b"x" * 4097)
                self.rejects(
                    lambda: transport.get_scope(origin, KEY, _loopback_port=port),
                    "invalid_scope",
                )
                route.update(raw=json.dumps(scope()).encode(), trickle=True)
                started = time.monotonic()
                with mock.patch.object(transport, "_WALL_SECONDS", 0.1):
                    self.rejects(
                        lambda: transport.get_scope(origin, KEY, _loopback_port=port),
                        "network_failure",
                    )
                self.assertLess(time.monotonic() - started, 1)
        finally:
            stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "loopback server cleanup required")
        self.assertTrue(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0))
        for bad in (
            f"http://localhost:{port}",
            f"http://127.0.0.2:{port}",
            f"http://127.0.0.1:{port + 1}",
        ):
            self.rejects(
                lambda bad=bad: transport.get_scope(bad, KEY, _loopback_port=port),
                "invalid_origin",
            )


class NoteTransportPlatformTest(unittest.TestCase):
    def test_response_read_preserves_bytesio_size_contract(self):
        with Response(raw=b"fixture") as response:
            self.assertTrue(response.read(None) == b"fixture")
            response.seek(0)
            self.assertTrue(response.read() == b"fixture")
            response.seek(0)
            self.assertTrue(response.read(3) == b"fix")
            self.assertTrue(response.read_sizes == [None, -1, 3])
        self.assertTrue(response.closed)

    def test_non_posix_integration_skips_before_scope_get(self):
        result = unittest.TestResult()
        with (
            mock.patch(f"{__name__}.os", SimpleNamespace(name="nt")),
            mock.patch.object(
                transport,
                "get_scope",
                side_effect=AssertionError("POSIX scope fixture entered"),
            ) as get,
        ):
            NoteTransportTest(
                "test_fixed_get_headers_no_proxy_no_redirect_and_default_tls"
            ).run(result)
        self.assertTrue(
            result.wasSuccessful() and len(result.skipped) == 1,
            "non-POSIX integration must skip before fixtures",
        )
        get.assert_not_called()

    def test_non_posix_scope_get_fails_closed_before_opener_or_timer(self):
        with (
            mock.patch.object(transport, "os", SimpleNamespace(name="nt")),
            mock.patch.object(transport.urllib.request, "build_opener") as build,
            mock.patch.object(transport, "signal", SimpleNamespace()),
            self.assertRaises(NoteError) as raised,
        ):
            transport.get_scope("https://example.test", KEY)
        self.assertTrue(raised.exception.code == "platform_unsupported")
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
