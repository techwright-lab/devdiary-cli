"""Real subprocess/HTTP delivery tests; only disposable state and synthetic credentials.

Rows come from the installed hook and leave through `sync CONNECTION_DIR`, the
command the timer runs. Frozen bytes and scope refresh are covered at the library
level in contract.rs and scope_compatibility.rs.
"""

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

import terminal

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "target/debug/devdiary"
FIXTURES = ROOT / "tests/fixtures"
TOKEN = "dc_live_" + "s" * 43
REF = "https://github.com/acme/demo"
FAKE_SYSTEMCTL = """#!/bin/sh
case "$*" in
  *is-enabled*) echo enabled ;;
  *is-active*) echo active ;;
esac
"""
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid"]


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.requests = []
        self.mode = "ok"
        self.entered = threading.Event()
        self.release = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = json.dumps(
                    {
                        "schema_version": 1,
                        "collector_ref": "collector-fixture",
                        "scope_kind": "installation",
                        "repository_refs": [REF],
                        "observation_schema_versions": [1, 2],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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
        self.install(f"http://127.0.0.1:{self.server.server_port}")

    def install(self, origin, code=0):
        """Save a connection as pairing leaves it, then run setup on a terminal."""
        self.home = self.root / "home"
        data = self.home / ".local/share/devdiary/cli/claude"
        data.parent.mkdir(parents=True)
        data.mkdir(mode=0o700)
        self.connection = data / "connection"
        self.connection.mkdir(mode=0o700)
        scope = json.loads((FIXTURES / "installation-scope.json").read_bytes())
        scope["endpoint"] = origin + "/ingest/v1/observations"
        installation = {
            "installation_id": scope["installation_id"],
            "origin": origin,
            "scope_kind": "installation",
            "record_orchestrated_outside_repos": False,
        }
        connection = {"scope": scope, "token": TOKEN, "repository_refs": [REF]}
        for name, value in (
            ("installation.json", installation),
            ("connection.json", connection),
        ):
            (self.connection / name).write_text(json.dumps(value))
            (self.connection / name).chmod(0o600)
        settings = self.root / "settings.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        systemctl = self.root / "bin/systemctl"
        systemctl.parent.mkdir()
        systemctl.write_text(FAKE_SYSTEMCTL)
        systemctl.chmod(0o700)
        self.env = {
            **{k: v for k, v in os.environ.items() if not k.startswith("PAPERCLIP_")},
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "DEVDIARY_COLLECTOR_SYSTEMCTL": str(systemctl),
            "HTTP_PROXY": "http://127.0.0.1:1",
        }
        args = [BIN, "setup", "--settings", settings, "--origin", origin]
        args += ["--trust-origin", "--no-browser"]
        status, _, self.setup_err, _ = terminal.answering(args, self.env)
        self.assertEqual(status, code, self.setup_err)
        record = json.loads((data / "install.json").read_bytes())
        self.exe, self.plan = record["executable"], record["plan"]
        self.outbox = self.connection / "outbox"
        self.repo = self.root / "demo"
        subprocess.run([*GIT, "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            [*GIT, "-C", str(self.repo), "remote", "add", "origin", REF + ".git"],
            check=True,
        )

    def stop_server(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def hook(self, session="session-1", **fields):
        # A tool ID makes the row's identity stable, so a repeat deduplicates.
        event = {
            "hook_event_name": "PostToolUse",
            "session_id": session,
            "cwd": str(self.repo),
            "tool_use_id": "tool-" + session,
            "tool_name": "Read",
            **fields,
        }
        subprocess.run(
            [self.exe, "hook", "claude", self.plan],
            input=json.dumps(event).encode(),
            capture_output=True,
            check=True,
            timeout=5,
            env=self.env,
        )

    def sync(self, code=0):
        result = subprocess.run(
            [self.exe, "sync", str(self.connection)],
            capture_output=True,
            check=False,
            timeout=30,
            env=self.env,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn(b"dc_live_", result.stdout + result.stderr)
        return result

    def doctor(self, code=0):
        result = subprocess.run(
            [str(BIN), "doctor"],
            capture_output=True,
            check=False,
            timeout=10,
            env=self.env,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        return json.loads(result.stdout) if code == 0 else None

    def db(self):
        db = sqlite3.connect(self.outbox / "collector-rust-v1.sqlite3")
        self.addCleanup(db.close)
        return db

    def pending(self):
        return self.db().execute("SELECT count(*) FROM outbox").fetchone()[0]

    def test_every_request_carries_the_token_and_no_private_file_leaks(self):
        self.hook(prompt="PRIVATE-SENTINEL")
        self.sync()
        self.assertEqual(len(self.requests), 1)
        for raw, auth in self.requests:
            self.assertEqual(auth, "Bearer " + TOKEN)
            self.assertNotIn(b"PRIVATE-SENTINEL", raw)
        for path in self.outbox.iterdir():
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertNotIn(b"PRIVATE-SENTINEL", path.read_bytes())
            self.assertNotIn(b"dc_live_", path.read_bytes())

    def test_byte_capacity_retains_and_delivers_every_accepted_row(self):
        # Real rows through the hook's admission, not injected blobs.
        for number in range(1200):
            self.hook(f"fill-{number}")
            if number % 100 == 99 and self.doctor()["drops"].get("spool_full"):
                break
        else:
            self.fail("expected conservative capacity refusal")
        count = self.pending()
        self.assertGreater(count, 100)
        db = self.db()
        self.assertLessEqual(db.execute("PRAGMA page_count").fetchone()[0], 4096)
        accepted = {
            json.loads(payload)["observation_id"]: payload
            for (payload,) in db.execute("SELECT payload FROM outbox")
        }
        # A global failure stops the run at its first row.
        self.mode = 503
        self.sync()
        self.assertEqual(len(self.requests), 1)
        # Row-specific refusals: each row is tried once per run, then kept.
        self.mode = "wrong"
        self.sync()
        self.assertEqual(
            db.execute(
                "SELECT count(*) FROM outbox WHERE failure='invalid_receipt'"
            ).fetchone()[0],
            count,
        )
        self.mode = "ok"
        self.sync()
        doctor = self.doctor()
        self.assertEqual((doctor["pending"], doctor["delivered"]), (0, count))
        self.hook("fill-0")  # a delivered row's identity is never spooled again
        self.assertEqual(self.pending(), 0)
        self.hook("after-capacity")  # reclaimed capacity admits a new row
        self.assertEqual(self.pending(), 1)
        self.assertLessEqual(
            (self.outbox / "collector-rust-v1.sqlite3").stat().st_size,
            16 * 1024 * 1024,
        )
        for raw, _ in self.requests:
            self.assertEqual(raw, accepted[json.loads(raw)["observation_id"]])

    def test_response_loss_exact_replay_and_dedup(self):
        self.hook()
        self.mode = "loss"
        self.sync()
        self.assertEqual(self.pending(), 1)
        self.mode = "ok"
        self.sync()
        self.assertEqual(self.requests[0][0], self.requests[-1][0])
        self.assertEqual(self.pending(), 0)
        self.assertEqual(self.doctor()["delivered"], 1)

    def test_poison_row_is_kept_and_the_next_row_still_goes(self):
        self.hook()
        self.hook("session-2")
        self.mode = 503
        self.sync()
        self.assertEqual(len(self.requests), 1)
        self.mode = "poison"
        self.sync()
        # The run starts after the row the global failure stopped at.
        sessions = [json.loads(raw)["session_id"] for raw, _ in self.requests[1:]]
        self.assertEqual(sessions, ["session-2", "session-1"])
        self.assertEqual(self.pending(), 1)
        (kept,) = self.db().execute("SELECT payload FROM outbox").fetchone()
        self.assertEqual(json.loads(kept)["session_id"], "session-1")
        self.assertEqual(self.doctor()["delivered"], 1)

    def test_crash_after_server_acceptance_keeps_bytes_and_cursor(self):
        self.hook()
        self.mode = "wait"
        process = subprocess.Popen(
            [self.exe, "sync", str(self.connection)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
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
        self.assertEqual(self.pending(), 1)
        self.mode = "ok"
        self.sync()
        self.assertEqual(self.requests[0][0], self.requests[-1][0])
        self.assertEqual(self.pending(), 0)

    def test_redirect_invalid_receipts_and_global_failures_keep_the_row(self):
        self.hook()
        for mode in [302, "wrong", "oversize", 503]:
            with self.subTest(mode=mode):
                self.mode = mode
                before = len(self.requests)
                self.sync()
                self.assertEqual(len(self.requests), before + 1)
                self.assertEqual(self.pending(), 1)
                self.assertIsNotNone(self.doctor()["last_sync_error"])
        self.mode = "ok"
        self.sync()
        self.assertEqual(self.pending(), 0)
        self.assertIsNone(self.doctor()["last_sync_error"])

    def test_concurrent_hooks_spool_every_row_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n: self.hook(f"session-{n}"), range(10)))
        self.assertEqual(self.pending(), 10)
        self.hook("session-3")  # same identity, new time: a conflict, not a row
        self.assertEqual(self.pending(), 10)
        # The row-count guard holds even when the bytes would still fit.
        db = self.db()
        with db:
            db.executemany(
                "INSERT INTO outbox(observation_id,payload) VALUES(?,?)",
                [(f"capacity-{i}", b"{}") for i in range(9990)],
            )
        self.hook("session-over")
        self.assertEqual(self.pending(), 10000)
        drops = self.doctor()["drops"]
        self.assertEqual(drops, {"observation_conflict": 1, "spool_full": 1})

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
            # A second installation saved against the TLS server. Setup registers,
            # then reports the scope refresh that the certificate refused.
            self.root = self.root / "tls"
            self.root.mkdir()
            self.install(f"https://localhost:{server.server_port}", code=2)
            self.assertIn(b"Scope refresh failed", self.setup_err)
            self.hook()
            self.sync()
            self.assertEqual(
                self.db().execute("SELECT failure FROM outbox").fetchone(),
                ("network_failure",),
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_a_foreign_schema_or_a_python_spool_is_refused(self):
        self.hook()
        db = self.db()
        with db:
            db.execute("PRAGMA user_version=3")
        self.sync(code=2)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
        with db:
            db.execute("PRAGMA user_version=0")
        (self.outbox / "observations.sqlite3").touch(mode=0o600)
        self.sync(code=2)
        self.assertEqual(self.requests, [])

    def test_collection_continues_during_exclusive_sync(self):
        self.hook()
        self.mode = "wait"
        process = subprocess.Popen(
            [self.exe, "sync", str(self.connection)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        try:
            self.assertTrue(self.entered.wait(3))
            self.hook("session-2")
            self.sync(code=2)
        finally:
            self.release.set()
            process.communicate(timeout=8)
        self.assertEqual(len(self.requests), 1)
        doctor = self.doctor()
        self.assertEqual((doctor["pending"], doctor["delivered"]), (1, 1))

    def test_a_stalled_server_is_abandoned_within_the_network_deadline(self):
        self.hook()
        self.mode = "wait"
        start = time.monotonic()
        self.sync()
        self.assertLess(time.monotonic() - start, 7)
        self.assertEqual(self.pending(), 1)


if __name__ == "__main__":
    unittest.main()
