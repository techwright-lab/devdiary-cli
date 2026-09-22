from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

from test_capture import KEY, ROOT, CaptureFixture

from devdiary import capture
from devdiary.capture_store import Store
from devdiary.capture_validation import MAX_BYTES, CaptureError, read_input
from devdiary.transport import TransportError, post_envelope


class CaptureSafetyTest(CaptureFixture):
    def test_existing_sibling_worktree_collects_only_the_actor_author(self):
        sibling = self.root / "sibling"
        self.git("worktree", "add", "-q", "-b", "sibling", str(sibling))
        opened = self.call("begin", self.begin_data())
        self.git(
            "-C",
            str(sibling),
            "commit",
            "--allow-empty",
            "-qm",
            "actor sibling",
            environment={**os.environ, **opened["environment"]},
        )
        expected = self.git("-C", str(sibling), "rev-parse", "HEAD")
        self.git("-C", str(sibling), "commit", "--allow-empty", "-qm", "human sibling")
        self.call(
            "finish", {"capture_id": opened["capture_id"], "outcome": "completed"}
        )
        self.assertEqual([expected], self.payloads[-1]["work"]["commits"])

    def test_repeat_finish_preserves_an_explicit_offset_timestamp(self):
        from datetime import UTC, datetime

        opened = self.call("begin", self.begin_data())
        claim = {
            "capture_id": opened["capture_id"],
            "outcome": "completed",
            "ended_at": datetime.now(UTC).isoformat(),
        }
        self.call("finish", claim)
        self.assertEqual("delivered", self.call("finish", claim)["state"])

    def test_status_git_identity_is_an_immutable_allowlisted_snapshot(self):
        self.call("begin", self.begin_data())
        self.registry["actors"][0]["identities"] = {
            "git_name": "Different",
            "git_email": "different@example.test",
        }
        from devdiary.config import write_registry

        write_registry(self.config, self.registry, force=True)
        row = self.call(
            "status",
            {
                "source": {
                    "system": "paperclip",
                    "company_id": "company-one",
                    "agent_id": "agent-one",
                    "run_id": "run:one",
                }
            },
        )["captures"][0]
        self.assertEqual(
            {"name": "One", "email": "one@example.test"}, row["git_identity"]
        )
        self.assertNotIn("environment", row)
        self.assertNotIn("credential", row)
        self.registry["actors"][0]["identities"] = {}
        write_registry(self.config, self.registry, force=True)
        self.call("begin", self.begin_data("run:without-identity"))
        rows = self.call("status", {"source": {"run_id": "run:without-identity"}})[
            "captures"
        ]
        self.assertNotIn("git_identity", rows[0])

    @unittest.skipUnless(os.name == "posix", "requires POSIX signals")
    def test_real_run_cli_cancellation_persists_terminal_outcome(self):
        import signal
        import time

        self.state_path = self.root / "attribution-captures/captures.sqlite3"
        ready = self.root / "ready"
        script = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(60)"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "devdiary",
                "--config",
                str(self.config),
                "run",
                "--actor",
                self.actor["actor_ref"],
                "--cwd",
                str(self.repo),
                "--",
                sys.executable,
                "-c",
                script,
            ],
            env={
                **os.environ,
                "PYTHONPATH": str(ROOT / "src"),
                "DEVDIARY_INGEST_KEY": KEY,
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while (
                not ready.exists()
                and time.monotonic() < deadline
                and process.poll() is None
            ):
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            process.send_signal(signal.SIGTERM)
            output, error = process.communicate(timeout=15)
            self.assertNotIn(KEY.encode(), output + error)
            self.assertEqual(143, process.returncode)
            self.assertEqual("run.cancelled", self.payloads[-1]["event_type"])
            with sqlite3.connect(self.state_path) as db:
                self.assertEqual(
                    "delivered", db.execute("select state from captures").fetchone()[0]
                )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_omitted_optional_finish_fields_reuse_frozen_claims(self):
        from devdiary.contract import timestamp

        opened = self.call("begin", self.begin_data())
        full = {
            "capture_id": opened["capture_id"],
            "outcome": "completed",
            "ended_at": timestamp(),
            "work": {"issues": ["urn:issue:one"]},
        }
        self.call("finish", full)
        self.assertEqual(
            "delivered",
            self.call(
                "finish", {"capture_id": opened["capture_id"], "outcome": "completed"}
            )["state"],
        )
        self.assertEqual(1, len(self.payloads))

    # Reuse the isolated CLI/Git/HTTP fixtures, not inherited test methods.
    def test_crash_after_freeze_before_http_restarts_with_exact_envelope(self):
        opened = self.call("begin", self.begin_data())
        script = (
            "import os; from pathlib import Path; from devdiary import capture; "
            "from devdiary.capture_store import Store; "
            f"s=Store(Path({str(self.root / 'state')!r})); "
            f"capture.freeze(s, {{'capture_id':{opened['capture_id']!r},'outcome':'cancelled'}}); "
            "os._exit(9)"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            check=False,
        )
        self.assertEqual(9, result.returncode)
        self.assertEqual([], self.payloads)
        store = Store(self.root / "state")
        frozen = store.get(opened["capture_id"])["envelope"]
        store.close()
        self.assertEqual("pending", self.call("status", {})["captures"][0]["state"])
        self.call("retry", {"capture_id": opened["capture_id"]})
        self.assertEqual(frozen, self.payloads[-1])

    def test_concurrent_finish_and_replay_use_one_event_and_do_not_rewrite(self):
        from concurrent.futures import ThreadPoolExecutor

        opened = self.call("begin", self.begin_data())
        claim = {"capture_id": opened["capture_id"], "outcome": "completed"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.call("finish", claim), range(4)))
        self.assertTrue(all(r["state"] == "delivered" for r in results))
        self.assertEqual(1, len(self.payloads))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda _: self.call("retry", {"capture_id": opened["capture_id"]}),
                    range(4),
                )
            )
        self.assertEqual(5, len(self.payloads))
        self.assertEqual(1, len({json.dumps(p, sort_keys=True) for p in self.payloads}))

    def test_key_rotation_symlink_fails_pending_without_leaking(self):
        opened = self.call("begin", self.begin_data())
        self.key.unlink()
        try:
            self.key.symlink_to(self.config)
        except OSError:
            self.skipTest("symbolic links are unavailable")
        result = self.call(
            "finish", {"capture_id": opened["capture_id"], "outcome": "failed"}
        )
        self.assertEqual("pending", result["state"])
        self.assertEqual("delivery_failed", result["error_code"])
        self.assertEqual([], self.payloads)

    def test_failed_terminal_commit_never_sends_and_leaves_open(self):
        opened = self.call("begin", self.begin_data())
        store = Store(self.root / "state")
        try:
            with (
                mock.patch.object(
                    store,
                    "save",
                    side_effect=sqlite3.OperationalError("fake disk failure"),
                ),
                self.assertRaises(sqlite3.OperationalError),
            ):
                capture.freeze(
                    store, {"capture_id": opened["capture_id"], "outcome": "completed"}
                )
            self.assertEqual("open", store.get(opened["capture_id"])["state"])
            self.assertEqual([], self.payloads)
        finally:
            store.close()

    def test_actor_isolation_and_missing_key_can_retry_with_new_secret(self):
        other = {**self.actor, "actor_ref": "urn:test:actor:two"}
        self.registry["actors"].append(other)
        from devdiary.config import write_registry

        write_registry(self.config, self.registry, force=True)
        first = self.call("begin", self.begin_data())
        self.call(
            "begin", {**self.begin_data(), "actor_ref": other["actor_ref"]}, code=2
        )
        second = self.call(
            "begin", {**self.begin_data("run:two"), "actor_ref": other["actor_ref"]}
        )
        self.assertNotEqual(first["capture_id"], second["capture_id"])
        self.key.unlink()
        self.assertEqual(
            "pending",
            self.call(
                "finish", {"capture_id": first["capture_id"], "outcome": "completed"}
            )["state"],
        )
        self.key.write_text(KEY)
        self.key.chmod(0o600)
        self.assertEqual(
            "delivered",
            self.call("retry", {"capture_id": first["capture_id"]})["state"],
        )


class WireValidationTest(unittest.TestCase):
    def test_bounded_strict_json(self):
        invalid = [
            b"[]",
            b"null",
            b'{"x":1,"x":2}',
            b'{"x":NaN}',
            b"\xff",
            b"{" + b" " * MAX_BYTES,
        ]
        for data in invalid:
            with (
                self.subTest(data=data[:30]),
                self.assertRaises((ValueError, UnicodeError, CaptureError)),
            ):
                read_input(io.BytesIO(data))
        self.assertEqual({}, read_input(io.BytesIO(b"{}")))

    def test_transport_requires_exact_bound_receipt(self):
        envelope = {
            "actor": {"ref": "actor:one"},
            "event_id": "event:one",
            "run_ref": "run:one",
        }
        valid = {
            "actor_ref": "actor:one",
            "event_id": "event:one",
            "run_ref": "run:one",
            "session_id": 1,
        }
        receipts = [
            None,
            {},
            [],
            "ok",
            {"status": "created"},
            *[{**valid, k: "wrong"} for k in ("actor_ref", "run_ref", "event_id")],
            *[{**valid, "session_id": value} for value in (0, -1, True, "1", 1.5)],
        ]
        for receipt in receipts:
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                receipt
            ).encode()
            with (
                self.subTest(receipt=receipt),
                mock.patch("devdiary.transport.OPENER.open", return_value=response),
                self.assertRaises(TransportError),
            ):
                post_envelope("http://127.0.0.1/ingest", KEY, envelope, attempts=1)
        for body in (b"", b"x" * (MAX_BYTES + 1)):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = body
            with (
                mock.patch("devdiary.transport.OPENER.open", return_value=response),
                self.assertRaises(TransportError),
            ):
                post_envelope("http://127.0.0.1/ingest", KEY, envelope, attempts=1)
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {**valid, "untrusted": KEY}
        ).encode()
        with mock.patch("devdiary.transport.OPENER.open", return_value=response):
            self.assertEqual(
                valid,
                post_envelope("http://127.0.0.1/ingest", KEY, envelope, attempts=1),
            )


if __name__ == "__main__":
    unittest.main()
