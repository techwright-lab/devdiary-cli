"""Real subprocess/HTTP tests; only disposable state and synthetic credentials."""

import concurrent.futures
import json
import os
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from devdiary import observer_upload

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "target/debug/devdiary-collector"
FIXTURES = ROOT / "tests/fixtures"


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "rust-state"
        self.state.mkdir(mode=0o700)
        self.key = self.root / "key"
        self.key.write_text("dc_live_SYNTHETIC_ONLY")
        self.key.chmod(0o600)
        self.config = json.loads((FIXTURES / "scope.json").read_bytes())
        self.row = json.loads((FIXTURES / "local-observation.json").read_bytes())
        self.requests = []
        self.mode = "ok"
        self.entered = threading.Event()
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append((raw, self.headers.get("Authorization")))
                owner.entered.set()
                data = json.loads(raw)
                if owner.mode == "wait":
                    owner.release.wait(10)
                if owner.mode == "loss":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                status = 201
                if isinstance(owner.mode, int):
                    status = owner.mode
                if owner.mode == "poison" and data["session_id"] == "session-1":
                    status = 422
                body = json.dumps(
                    {
                        "observation_id": data["observation_id"],
                        "installation_id": data["installation_id"],
                        "collector_ref": "collector-fixture",
                        "record_id": 17,
                    }
                ).encode()
                if owner.mode == "wrong":
                    body = body.replace(b"collector-fixture", b"wrong-collector")
                if owner.mode == "oversize":
                    body = b" " * 16385
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Location", "/unexpected-redirect")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.config["endpoint"] = (
            f"http://127.0.0.1:{self.server.server_port}/ingest/v1/observations"
        )
        self.call("init", "--consent", data=self.config)

    def stop_server(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, command, *args, data=None, code=0):
        raw = json.dumps(data).encode() if isinstance(data, dict) else data
        result = subprocess.run(
            [str(BIN), command, str(self.state), *map(str, args)],
            input=raw,
            check=False,
            capture_output=True,
            timeout=9,
            env={**os.environ, "HTTP_PROXY": "http://127.0.0.1:1"},
        )
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn(b"PRIVATE-SENTINEL", result.stdout + result.stderr)
        self.assertNotIn(b"dc_live_", result.stdout + result.stderr)
        return result

    def db(self):
        db = sqlite3.connect(self.state / "collector-rust-v1.sqlite3")
        self.addCleanup(db.close)
        return db

    def collect(self, number=2, **changes):
        row = {**self.row, **changes}
        row["observation_id"] = f"{number:08d}-2222-4222-8222-222222222222"
        self.call("collect", data=row)
        return row

    def test_python_conformance_timestamp_null_and_vendor_ids(self):
        for i, timestamp in enumerate(
            [1720000000.123456, 0, -0.0000005, 0.0000015, 0.9999995, -1.1234565]
        ):
            row = self.collect(i, observed_at=timestamp, prompt_id=None)
            projected = {
                k: v
                for k, v in row.items()
                if k in observer_upload.FIELDS and v is not None
            }
            expected = observer_upload.envelope(projected, self.config)
            actual = (
                self.db()
                .execute(
                    "SELECT payload FROM outbox WHERE observation_id=?",
                    (row["observation_id"],),
                )
                .fetchone()[0]
            )
            self.assertEqual(actual, expected)
        self.call("sync", self.key, 100)
        for raw, auth in self.requests:
            self.assertEqual(auth, "Bearer dc_live_SYNTHETIC_ONLY")
            self.assertNotIn(b"PRIVATE-SENTINEL", raw)
            observer_upload.receipt(
                json.dumps(
                    {
                        "observation_id": json.loads(raw)["observation_id"],
                        "installation_id": self.config["installation_id"],
                        "collector_ref": self.config["collector_ref"],
                        "record_id": 17,
                    }
                ),
                raw,
                self.config["collector_ref"],
            )
        for path in self.state.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertNotIn(b"PRIVATE-SENTINEL", path.read_bytes())
            self.assertNotIn(b"dc_live_", path.read_bytes())

    def test_response_loss_exact_replay_and_dedup(self):
        self.collect()
        self.collect()
        self.mode = "loss"
        self.call("sync", self.key, 1, code=1)
        self.mode = "ok"
        self.call("sync", self.key, 1)
        self.assertEqual(self.requests[0][0], self.requests[1][0])
        self.assertEqual(
            self.db().execute("SELECT count(*) FROM outbox").fetchone()[0], 1
        )
        self.assertEqual(
            self.db().execute("SELECT delivered FROM outbox").fetchone()[0], 1
        )

    def test_poison_fairness_and_global_failure_cursor(self):
        self.collect()
        self.collect(3, session_id="session-2")
        self.mode = 401
        self.call("sync", self.key, 100, code=1)
        self.assertEqual(len(self.requests), 1)
        self.mode = "poison"
        self.call("sync", self.key, 1, code=1)
        self.assertEqual(json.loads(self.requests[-1][0])["session_id"], "session-2")
        self.call("sync", self.key, 100, code=1)
        self.assertEqual(
            self.db().execute("SELECT delivered FROM outbox ORDER BY seq").fetchall(),
            [(0,), (1,)],
        )

    def test_crash_after_server_acceptance_keeps_bytes_and_cursor(self):
        self.collect()
        self.collect(3, session_id="session-2")
        self.mode = "wait"
        process = subprocess.Popen(
            [str(BIN), "sync", str(self.state), str(self.key), "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertTrue(self.entered.wait(3))
            process.kill()
            process.communicate(timeout=3)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            self.release.set()
        self.assertEqual(self.db().execute("SELECT cursor FROM scope").fetchone()[0], 1)
        self.mode = "ok"
        self.call("sync", self.key, 1, code=1)
        self.call("sync", self.key, 1)
        self.assertEqual(self.requests[0][0], self.requests[-1][0])

    def test_redirect_invalid_receipts_and_global_failures(self):
        self.collect()
        for mode in [302, "wrong", "oversize", 403, 429, 503]:
            self.mode = mode
            before = len(self.requests)
            self.call("sync", self.key, 100, code=1)
            self.assertEqual(len(self.requests), before + 1)
            self.assertEqual(
                self.db().execute("SELECT delivered FROM outbox").fetchone()[0], 0
            )
        self.key.write_text("dc_live_ROTATED_SYNTHETIC")
        self.mode = "ok"
        self.call("sync", self.key, 1)
        self.assertEqual(self.requests[-1][1], "Bearer dc_live_ROTATED_SYNTHETIC")

    def test_scope_credential_permissions_and_python_spool_refusal(self):
        self.collect()
        for key, value in [
            ("endpoint", "https://other.test/ingest/v1/observations"),
            ("collector_ref", "other"),
            ("repository_ref", "https://github.com/acme/other"),
            ("installation_id", "33333333-3333-4333-8333-333333333333"),
        ]:
            self.call("init", "--consent", data={**self.config, key: value}, code=2)
        self.key.chmod(0o644)
        self.call("sync", self.key, 1, code=2)
        self.key.chmod(0o600)
        self.key.write_text("dd_live_actor_key")
        self.call("sync", self.key, 1, code=2)
        (self.state / "observations.sqlite3").touch(mode=0o600)
        self.call("status", code=2)
        self.assertEqual(self.requests, [])

    def test_capacity_conflicts_concurrent_writers_and_atomicity(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(self.collect, range(10)))
        self.assertEqual(
            self.db().execute("SELECT count(*) FROM outbox").fetchone()[0], 10
        )
        self.call(
            "collect",
            data={
                **self.row,
                "observation_id": "00000002-2222-4222-8222-222222222222",
                "session_id": "changed",
            },
            code=2,
        )
        db = self.db()
        with db:
            db.executemany(
                "INSERT INTO outbox(observation_id,payload) VALUES(?,?)",
                [(f"capacity-{i}", b"{}") for i in range(9990)],
            )
        self.call("collect", data=self.row, code=2)
        self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 10000)

    def test_untrusted_tls_certificate_is_not_accepted(self):
        cert, key = self.root / "cert.pem", self.root / "tls-key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.state = self.root / "tls-state"
            self.state.mkdir(mode=0o700)
            self.config["endpoint"] = (
                f"https://localhost:{server.server_port}/ingest/v1/observations"
            )
            self.call("init", "--consent", data=self.config)
            self.collect()
            self.call("sync", self.key, 1, code=1)
            self.assertEqual(
                self.db().execute("SELECT failure,delivered FROM outbox").fetchone(),
                ("network_failure", 0),
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_symlinks_hardlinks_foreign_schema_and_missing_consent(self):
        link = self.root / "linked-key"
        link.symlink_to(self.key)
        self.call("sync", link, 1, code=2)
        link.unlink()
        os.link(self.key, link)
        self.call("sync", self.key, 1, code=2)
        link.unlink()
        self.call("init", data=self.config, code=2)
        db = self.db()
        with db:
            db.execute("PRAGMA user_version=2")
        self.call("status", code=2)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.requests, [])

    def test_collection_continues_during_exclusive_sync(self):
        self.collect()
        self.mode = "wait"
        process = subprocess.Popen(
            [str(BIN), "sync", str(self.state), str(self.key), "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertTrue(self.entered.wait(3))
            self.collect(3)
            self.call("sync", self.key, 1, code=2)
        finally:
            self.release.set()
            process.communicate(timeout=8)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(
            self.db().execute("SELECT count(*) FROM outbox").fetchone()[0], 2
        )

    def test_blocked_and_oversize_stdin_and_network_deadlines(self):
        self.call("collect", data=b" " * 65537, code=2)
        process = subprocess.Popen(
            [str(BIN), "collect", str(self.state)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(process.wait(timeout=2), 2)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.collect()
        self.mode = "wait"
        start = time.monotonic()
        self.call("sync", self.key, 1, code=1)
        self.assertLess(time.monotonic() - start, 7)


if __name__ == "__main__":
    unittest.main()
