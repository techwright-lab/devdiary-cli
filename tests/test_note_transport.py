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
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


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
        actual, opener, build = self.call_fake(response)
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
        self.assertTrue(
            any(
                isinstance(h, urllib.request.ProxyHandler) and h.proxies == {}
                for h in handlers
            )
        )
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
                self.send_header(
                    "Location", f"http://127.0.0.1:{self.server.server_port}/target"
                )
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

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = False
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        thread.start()
        port = server.server_port
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
