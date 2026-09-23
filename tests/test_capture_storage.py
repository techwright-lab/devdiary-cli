from __future__ import annotations

import json
import sys
import unittest
from unittest import mock

from test_capture import KEY, CaptureFixture

from devdiary import capture, capture_git, capture_store, git_refs, runner
from devdiary import capture_validation as v
from devdiary.capture_store import Store


class CaptureStorageTest(CaptureFixture):
    def full_snapshot(self, siblings=0):
        # Unique hash/action pairs at the real per-worktree collector ceiling.
        timed = {
            str(self.repo if index == 0 else self.root / f"sibling-{index}"): frozenset(
                (f"{entry:040x}", "commit: private subject", 1704067200)
                for entry in range(git_refs.REFLOG_LIMIT)
            )
            for index in range(siblings + 1)
        }
        entries = {
            path: frozenset((sha, action) for sha, action, _ in values)
            for path, values in timed.items()
        }
        return git_refs.GitSnapshot(
            self.repo,
            "test/repo",
            "0" * 40,
            entries[str(self.repo)],
            entries,
            timed,
        )

    def test_full_worktree_and_sibling_snapshots_survive_entire_lifecycle(self):
        for siblings in (0, 3):
            with self.subTest(siblings=siblings):
                before = self.full_snapshot(siblings)
                with mock.patch("devdiary.git_refs.capture", return_value=before):
                    expected = json.loads(json.dumps(capture_git.snapshot(self.repo)))
                    store = Store(self.root / "state")
                    opened = capture.begin(
                        store, self.registry, self.begin_data(f"run:large-{siblings}")
                    )
                self.assertGreater(len(json.dumps(expected)), v.MAX_BYTES)
                store.close()
                store = Store(self.root / "state")
                self.addCleanup(store.close)
                capture_id = opened["capture_id"]
                self.assertEqual(expected, store.get(capture_id)["before"])
                paths = list(before.worktree_reflogs)
                new_sha = "f" * 40
                after = git_refs.GitSnapshot(
                    before.root,
                    before.repository,
                    new_sha,
                    before.reflog_entries,
                    {
                        path: entries | {(new_sha, "commit: new")}
                        for path, entries in before.worktree_reflogs.items()
                    },
                    {
                        path: entries | {(new_sha, "commit: new", 1704067202)}
                        for path, entries in before.timed_worktree_reflogs.items()
                    },
                )
                with (
                    mock.patch("devdiary.git_refs.capture", return_value=after),
                    mock.patch(
                        "devdiary.git_refs._commit_author_email",
                        return_value="one@example.test",
                    ) as author,
                ):
                    frozen = capture.freeze(
                        store, {"capture_id": capture_id, "outcome": "completed"}
                    )
                self.assertEqual(len(paths), author.call_count)
                self.assertEqual([new_sha], frozen["envelope"]["work"]["commits"])
                self.assertEqual(expected, store.get(capture_id)["before"])
                self.assertLess(len(v.dumps(frozen["envelope"])), v.MAX_BYTES)
                self.ack = "invalid"
                self.assertEqual("pending", capture.deliver(store, capture_id)["state"])
                self.ack = "valid"
                self.assertEqual(
                    "delivered", capture.deliver(store, capture_id)["state"]
                )
                self.assertEqual(frozen["envelope"], self.payloads[-1])
                self.assertEqual(self.payloads[-2], self.payloads[-1])
                self.assertEqual(expected, store.get(capture_id)["before"])

    def test_real_git_full_reflog_survives_begin_finish_delivery(self):
        sha = self.git("rev-parse", "HEAD")
        reflog = self.repo / ".git/logs/HEAD"
        reflog.write_text(
            "".join(
                f"{sha} {sha} Human <human@example.test> 1704067200 +0000\tcommit: entry {i}\n"
                for i in range(git_refs.REFLOG_LIMIT)
            )
        )
        opened = self.call("begin", self.begin_data())
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        before = store.get(opened["capture_id"])["before"]
        self.assertEqual(10000, len(before["entries"][str(self.repo)]))
        self.assertEqual(10000, len(before["timed_entries"][str(self.repo)]))
        self.assertGreater(len(json.dumps(before).encode()), v.MAX_BYTES)
        self.git(
            "commit",
            "--allow-empty",
            "-qm",
            "new actor work",
            environment={**opened["environment"]},
        )
        new_sha = self.git("rev-parse", "HEAD")
        finished = self.call(
            "finish",
            {
                "capture_id": opened["capture_id"],
                "outcome": "completed",
            },
        )
        self.assertEqual("delivered", finished["state"])
        self.assertEqual([new_sha], self.payloads[-1]["work"]["commits"])
        self.assertEqual(before, store.get(opened["capture_id"])["before"])

    def test_private_overflow_rolls_back_begin_and_save(self):
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        with (
            mock.patch.object(capture_store, "MAX_PRIVATE_RECORD_BYTES", 1),
            self.assertRaisesRegex(v.CaptureError, "private_record_too_large"),
        ):
            capture.begin(store, self.registry, self.begin_data())
        self.assertEqual([], store.db.execute("SELECT * FROM captures").fetchall())
        self.assertEqual([], list(store.directory.glob("*.context.json")))
        opened = capture.begin(store, self.registry, self.begin_data())
        original = store.get(opened["capture_id"])
        with (
            mock.patch.object(capture_store, "MAX_PRIVATE_RECORD_BYTES", 1),
            self.assertRaisesRegex(v.CaptureError, "private_record_too_large"),
        ):
            capture.freeze(
                store,
                {"capture_id": opened["capture_id"], "outcome": "completed"},
            )
        self.assertEqual(original, store.get(opened["capture_id"]))
        self.assertEqual([], self.payloads)

    def test_direct_run_spawns_child_with_full_sibling_snapshot(self):
        observed = self.root / "spawned"
        child = f"from pathlib import Path; Path({str(observed)!r}).touch()"
        with mock.patch(
            "devdiary.git_refs.capture", return_value=self.full_snapshot(2)
        ):
            result = runner.run_command(
                self.registry,
                self.actor,
                [sys.executable, "-c", child],
                self.repo,
                environment={"DEVDIARY_INGEST_KEY": KEY},
                state_directory=self.root / "state",
            )
        self.assertTrue(observed.exists())
        self.assertEqual(0, result.exit_code)
        self.assertTrue(result.emitted)
        self.assertEqual([], self.payloads[-1]["work"]["commits"])

    def test_large_private_snapshot_does_not_relax_wire_or_freeze_limit(self):
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        with mock.patch("devdiary.git_refs.capture", return_value=self.full_snapshot()):
            opened = capture.begin(store, self.registry, self.begin_data())
        claim = {
            "capture_id": opened["capture_id"],
            "outcome": "completed",
            "work": {"artifacts": [f"urn:test:{i}:" + "x" * 4000 for i in range(300)]},
        }
        with self.assertRaisesRegex(v.CaptureError, "payload_too_large"):
            capture.freeze(store, claim)
        self.assertEqual("open", store.get(opened["capture_id"])["state"])
        self.assertNotIn("envelope", store.get(opened["capture_id"]))
        self.assertEqual([], self.payloads)
        with self.assertRaisesRegex(v.CaptureError, "payload_too_large"):
            v.dumps(claim)
        with self.assertRaisesRegex(v.CaptureError, "payload_too_large"):
            store.context_file("oversized", claim)


class PrivateEncodingTest(unittest.TestCase):
    def test_exact_byte_ceiling_and_terminal_reserve(self):
        # Scale budgets, not serialization: exact UTF-8/escaped JSON size still
        # controls acceptance, and terminal updates can consume the reserve.
        record = {"state": "open", "before": "é" * 100}
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        with (
            mock.patch.object(capture_store, "MAX_PRIVATE_RECORD_BYTES", size + 256),
            mock.patch.object(capture_store, "TERMINAL_RESERVE_BYTES", 256),
        ):
            self.assertEqual(encoded, capture_store.dumps_record(record))
            with self.assertRaisesRegex(v.CaptureError, "private_record_too_large"):
                capture_store.dumps_record({**record, "extra": "x"})
            terminal = {**record, "state": "pending", "envelope": "x" * 100}
            self.assertEqual(terminal, json.loads(capture_store.dumps_record(terminal)))
            with self.assertRaisesRegex(v.CaptureError, "private_record_too_large"):
                capture_store.dumps_record({**terminal, "receipt": "x" * 256})
        self.assertEqual(64 * 1_048_576, capture_store.MAX_PRIVATE_RECORD_BYTES)
        self.assertEqual(4 * 1_048_576, capture_store.TERMINAL_RESERVE_BYTES)
        with self.assertRaises(ValueError):
            capture_store.dumps_record({"state": "open", "invalid": float("nan")})
