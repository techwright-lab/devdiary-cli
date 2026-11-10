from __future__ import annotations

import json
import os
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_note_validation import scope

from devdiary.note_profile import BoundProfile, create_profile
from devdiary.note_store import DB_NAME, LOCK_NAME, Store

FIXTURE = Path(__file__).with_name("note_outbox_process_fixture.py")
SOURCE = Path(__file__).parent.parent
SECONDS = 10


class NoteOutboxProcessTest(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("POSIX notes writes are unsupported")
        if os.environ.get("DEVDIARY_OUTBOX_QUALIFICATION") == "1":
            self.assertTrue(
                getattr(sys, "_devdiary_qualification_network_guard", False)
            )
            self.assertTrue(os.environ.get("DEVDIARY_OUTBOX_FIXTURE_PYTHON"))
        self.temp = tempfile.TemporaryDirectory(
            dir=Path(tempfile.gettempdir()).resolve()
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.children: list[subprocess.Popen[bytes]] = []

    def state(self, name):
        root = self.root / name
        root.mkdir(mode=0o700)
        state = root / "state"
        key = root / "key"
        fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(("dn_live_" + "a" * 43).encode())
        create_profile(
            state,
            BoundProfile(origin="https://example.test", key_file=str(key), **scope()),
        )
        return state

    def _timeout_cleanup(self, child: subprocess.Popen[bytes]) -> None:
        # Only this retained, still-owned unreaped child; never a crash killpoint.
        if child.returncode is None and child.poll() is None:
            child.kill()
        child.wait(timeout=SECONDS)

    @contextmanager
    def child(self, state, phase, port=None):
        self.assertLess(sum(child.returncode is None for child in self.children), 2)
        private = os.environ.get("DEVDIARY_OUTBOX_QUALIFICATION") == "1"
        executable = (
            os.environ["DEVDIARY_OUTBOX_FIXTURE_PYTHON"] if private else sys.executable
        )
        environment = {
            key: os.environ[key]
            for key in (
                "HOME",
                "TMPDIR",
                "PATH",
                "DEVDIARY_PROFILE_QUALIFICATION",
                "DEVDIARY_OUTBOX_QUALIFICATION",
                "DEVDIARY_OUTBOX_FIXTURE_PYTHON",
            )
            if key in os.environ
        }
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    (str(SOURCE / "src"), str(SOURCE / "tests"))
                ),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        argv = [executable, str(FIXTURE), "--state-dir", str(state), "--phase", phase]
        if port is not None:
            argv.extend(("--port", str(port)))
        child = subprocess.Popen(
            argv,
            cwd=SOURCE,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
        )
        self.children.append(child)
        try:
            yield child
        finally:
            try:
                if child.returncode is None:
                    try:
                        out, err = child.communicate(timeout=SECONDS)
                    except subprocess.TimeoutExpired:
                        self._timeout_cleanup(child)
                        raise RuntimeError("fixture_timeout_BLOCKED") from None
                    self.assertLessEqual(len(out), 2048)
                    self.assertLessEqual(len(err), 4096)
            finally:
                for pipe in (child.stdin, child.stdout, child.stderr):
                    if pipe is not None:
                        pipe.close()

    def metadata(self, child):
        if child.stdout is None:
            raise AssertionError("fixture stdout required")
        raw = bytearray()
        deadline = time.monotonic() + SECONDS
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while b"\n" not in raw:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    self._timeout_cleanup(child)
                    raise RuntimeError("fixture_barrier_timeout_BLOCKED")
                chunk = os.read(child.stdout.fileno(), 1)
                if not chunk:
                    raise RuntimeError("fixture_barrier_eof_BLOCKED")
                raw.extend(chunk)
                self.assertLessEqual(len(raw), 512)
        return json.loads(raw)

    def finish(self, child, data=None):
        try:
            out, err = child.communicate(input=data, timeout=SECONDS)
        except subprocess.TimeoutExpired:
            self._timeout_cleanup(child)
            raise RuntimeError("fixture_timeout_BLOCKED") from None
        self.assertLessEqual(len(out), 2048)
        self.assertEqual(err, b"", "fixture stderr must be empty")
        return out

    def test_real_http_lock_excludes_second_process_and_releases(self):
        state = self.state("http")
        reached, release = threading.Event(), threading.Event()
        observations = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers["Content-Length"])
                request = json.loads(self.rfile.read(size))
                with closing(sqlite3.connect(state / DB_NAME)) as connection:
                    observations.append(
                        connection.execute(
                            "SELECT state,attempts FROM note_operations WHERE operation_id=?",
                            (request["operation_id"],),
                        ).fetchone()
                    )
                reached.set()
                if not release.wait(SECONDS):
                    return
                receipt = {
                    "operation_id": request["operation_id"],
                    "note_ref": request["note_ref"],
                    "note_id": "note_http_process",
                    "revision": 1,
                    "outcome": "applied",
                }
                raw = json.dumps(receipt).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with self.child(state, "http", server.server_port) as holder:
                self.assertEqual(self.metadata(holder), {"phase": "http"})
                try:
                    self.assertTrue(
                        reached.wait(SECONDS), "HTTP barrier timeout BLOCKED"
                    )
                    inode = (state / LOCK_NAME).stat().st_ino
                    with self.child(state, "contend") as contender:
                        self.assertEqual(
                            self.metadata(contender), {"state": "store_busy"}
                        )
                        self.finish(contender)
                        self.assertEqual(contender.returncode, 0)
                finally:
                    release.set()
                self.assertEqual(
                    json.loads(self.finish(holder))["state"], "acknowledged"
                )
                self.assertEqual(holder.returncode, 0)
            self.assertEqual(observations, [("dispatched", 1)])
            with Store.open(state):
                pass
            self.assertEqual((state / LOCK_NAME).stat().st_ino, inode)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(SECONDS)
            self.assertFalse(
                thread.is_alive(), "owned server thread did not join BLOCKED"
            )
        self.assertTrue(all(child.returncode is not None for child in self.children))

    def test_named_abrupt_crashes_recover_exact_identity_and_physical_cleanup(self):
        phases = (
            "enqueue_before_commit",
            "enqueue_committed",
            "freeze_before_commit",
            "dispatch_committed",
            "ack_committed",
            "intent_published",
            "purge_closed_synced",
            "metadata_finalized",
            "wal_committed",
        )
        for phase in phases:
            with self.subTest(phase=phase):
                state = self.state(phase)
                with Store.open(state) as store:
                    other = store.enqueue(
                        "create",
                        {"attributes": {"content": "OTHER_PROCESS_PENDING_71c2"}},
                    )
                with self.child(state, phase) as child:
                    metadata = self.metadata(child)
                    self.assertEqual(metadata["phase"], phase)
                    if phase == "wal_committed":
                        self.assertGreater(metadata["wal_bytes"], 32)
                        self.assertTrue(metadata["shm"])
                    elif phase in ("enqueue_committed", "dispatch_committed"):
                        self.assertEqual(
                            set(metadata),
                            {"phase", "operation_id", "note_ref"}
                            | (
                                {"wire_sha256"}
                                if phase == "dispatch_committed"
                                else set()
                            ),
                        )
                    else:
                        self.assertEqual(metadata, {"phase": phase})
                    self.finish(child, b"crash\n")
                    self.assertEqual(child.returncode, 73)
                if phase == "wal_committed":
                    self.assertGreater((state / (DB_NAME + "-wal")).stat().st_size, 32)
                    self.assertTrue((state / (DB_NAME + "-shm")).is_file())
                with Store.open(state) as store:
                    self.assertEqual(
                        store.connection.execute("PRAGMA journal_mode").fetchone()[0],
                        "delete",
                    )
                    self.assertIn(
                        b"OTHER_PROCESS_PENDING_71c2",
                        store.connection.execute(
                            "SELECT draft FROM note_operations WHERE operation_id=?",
                            (other["operation_id"],),
                        ).fetchone()[0],
                    )
                    rows = [
                        row
                        for row in store.status()
                        if row["operation_id"] != other["operation_id"]
                    ]
                    if phase == "enqueue_before_commit":
                        self.assertEqual(rows, [])
                    else:
                        self.assertEqual(len(rows), 1)
                        operation_id = rows[0]["operation_id"]
                        if phase in ("enqueue_committed", "dispatch_committed"):
                            self.assertEqual(operation_id, metadata["operation_id"])
                            self.assertEqual(rows[0]["note_ref"], metadata["note_ref"])
                        seq = rows[0]["seq"]
                        if phase in (
                            "enqueue_committed",
                            "freeze_before_commit",
                            "dispatch_committed",
                        ):
                            frozen = store.freeze(seq)
                            self.assertIsNotNone(frozen)
                            if frozen is not None:
                                raw = frozen["wire"]
                                if phase == "dispatch_committed":
                                    self.assertEqual(
                                        frozen["wire_sha256"], metadata["wire_sha256"]
                                    )
                                self.assertEqual(
                                    json.loads(raw)["operation_id"], operation_id
                                )
                                replay = store.freeze(seq)
                                assert replay is not None
                                self.assertEqual(replay["wire"], raw)
                            store.discard(rows[0]["note_ref"])
                        else:
                            self.assertIsNone(store.freeze(seq))
                    self.assertIn(
                        b"OTHER_PROCESS_PENDING_71c2",
                        store.connection.execute(
                            "SELECT draft FROM note_operations WHERE operation_id=? AND note_ref=?",
                            (other["operation_id"], other["note_ref"]),
                        ).fetchone()[0],
                    )
                self.assertFalse((state / (DB_NAME + "-wal")).exists())
                self.assertFalse((state / (DB_NAME + "-shm")).exists())
                for path in state.iterdir():
                    if path.is_file():
                        self.assertNotIn(
                            b"PROCESS_SYNTHETIC_8c17", path.read_bytes(), phase
                        )
                print(
                    "process-evidence",
                    phase,
                    "exit73/reaped/recovered/sentinel-absent",
                    flush=True,
                )
        self.assertTrue(all(child.returncode is not None for child in self.children))
