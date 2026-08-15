from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

from devdiary_attribution import spool


class ReplayHandler(BaseHTTPRequestHandler):
    payloads: ClassVar[list[dict]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.__class__.payloads.append(json.loads(self.rfile.read(length)))
        body = b'{"status":"created"}'
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class SpoolTest(unittest.TestCase):
    KEY = "test-only-not-a-live-key"

    def setUp(self) -> None:
        ReplayHandler.payloads = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ReplayHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_pending_terminal_declaration_replays_with_the_same_event_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "events"
            envelope = {
                "event_id": "urn:devdiary:event:fixed-id",
                "event_type": "run.completed",
                "actor": {"ref": "urn:acme:actor:reviewer"},
            }
            path = spool.enqueue(queue, envelope, self.KEY)
            self.assertNotIn(self.KEY, path.read_text())

            delivered, errors = spool.flush(
                queue,
                f"http://127.0.0.1:{self.server.server_port}/ingest/v1/sessions",
                self.KEY,
            )

            self.assertEqual(1, delivered)
            self.assertEqual([], errors)
            self.assertFalse(path.exists())
            self.assertEqual(
                "urn:devdiary:event:fixed-id", ReplayHandler.payloads[0]["event_id"]
            )

    def test_event_identity_cannot_escape_the_private_spool_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "events"
            path = spool.enqueue(queue, {"event_id": "../../outside"}, self.KEY)

            self.assertEqual(queue, path.parent)
            self.assertRegex(path.name, re.compile(r"\A[0-9a-f]{64}\.json\Z"))

    def test_symbolic_link_spool_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            queue = Path(directory) / "events"
            try:
                queue.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("symbolic links are unavailable")

            with self.assertRaisesRegex(spool.SpoolError, "symbolic link"):
                spool.enqueue(queue, {"event_id": "urn:devdiary:event:test"}, self.KEY)

    def test_symbolic_link_parent_of_spool_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.mkdir()
            parent = Path(directory) / "linked-parent"
            try:
                parent.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("symbolic links are unavailable")

            with self.assertRaisesRegex(spool.SpoolError, "symbolic link"):
                spool.enqueue(
                    parent / "events",
                    {"event_id": "urn:devdiary:event:test"},
                    self.KEY,
                )

    def test_pending_ignores_symbolic_linked_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "events"
            queue.mkdir()
            target = Path(directory) / "outside.json"
            target.write_text(json.dumps({"event_id": "outside"}), encoding="utf-8")
            link = queue / "injected.json"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symbolic links are unavailable")

            self.assertEqual([], spool.pending(queue))

    def test_modified_queue_entry_is_never_sent_with_the_actor_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "events"
            path = spool.enqueue(
                queue,
                {
                    "event_id": "urn:devdiary:event:fixed-id",
                    "event_type": "run.completed",
                    "actor": {"ref": "urn:acme:actor:reviewer"},
                },
                self.KEY,
            )
            queued = json.loads(path.read_text())
            queued["envelope"]["actor"]["ref"] = "urn:acme:actor:forged"
            path.write_text(json.dumps(queued), encoding="utf-8")

            delivered, errors = spool.flush(
                queue,
                f"http://127.0.0.1:{self.server.server_port}/ingest/v1/sessions",
                self.KEY,
            )

            self.assertEqual(0, delivered)
            self.assertEqual([], ReplayHandler.payloads)
            self.assertEqual(1, len(errors))
            self.assertIn("integrity", errors[0])
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
