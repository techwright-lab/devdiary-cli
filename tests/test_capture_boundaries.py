from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from unittest import mock

from test_capture import CaptureFixture

from devdiary import capture, capture_git
from devdiary.capture_store import Store


class CaptureBoundaryTest(CaptureFixture):
    START = "2024-01-01T00:00:00.250000+00:00"
    END = "2024-01-01T00:00:10.750000+00:00"

    def begin_at(self):
        store = Store(self.root / "state")
        self.addCleanup(store.close)
        with mock.patch("devdiary.contract.timestamp", return_value=self.START):
            opened = capture.begin(store, self.registry, self.begin_data())
        return store, opened

    def dated_commit(self, opened, second, *, path=None, human=False):
        date = f"2024-01-01T00:00:{second:02d}+00:00"
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        if not human:
            env.update(opened["environment"])
        env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_AUTHOR_DATE="2020-01-01T00:00:00+00:00",
            GIT_COMMITTER_DATE=date,
        )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-qm", f"private message {second}"],
            cwd=path or self.repo,
            env=env,
            check=True,
            capture_output=True,
        )
        return self.git("-C", str(path or self.repo), "rev-parse", "HEAD")

    def finish_at(self, opened):
        return self.call(
            "finish",
            {
                "capture_id": opened["capture_id"],
                "outcome": "completed",
                "ended_at": self.END,
            },
        )

    def test_authoritative_end_automatically_collects_only_proven_window_and_replays(
        self,
    ):
        store, opened = self.begin_at()
        self.dated_commit(opened, 0)  # begin second is not provably inside
        expected = self.dated_commit(opened, 2)
        self.dated_commit(opened, 3, human=True)
        self.dated_commit(opened, 10)  # terminal second is ambiguous
        self.dated_commit(opened, 11)  # later same-actor work
        self.ack = "invalid"
        self.assertEqual("pending", self.finish_at(opened)["state"])
        envelope = self.payloads[-1]
        self.assertEqual([expected], envelope["work"]["commits"])
        self.assertEqual(
            datetime.fromisoformat(self.END),
            datetime.fromisoformat(envelope["ended_at"]),
        )
        self.assertEqual(
            "timestamp_qualified_terminal_second_excluded",
            envelope["extensions"]["devdiary_capture"]["git_collection"],
        )
        persisted = store.get(opened["capture_id"])
        self.assertNotIn("private message", json.dumps(persisted["before"]))
        self.dated_commit(opened, 4)  # even new backdated evidence cannot alter replay
        self.ack = "valid"
        with mock.patch(
            "devdiary.capture_git.collect", side_effect=AssertionError("recollected")
        ):
            capture.freeze(
                store, {"capture_id": opened["capture_id"], "outcome": "completed"}
            )
        self.call("retry", {"capture_id": opened["capture_id"]})
        self.assertEqual(envelope, self.payloads[-1])
        self.finish_at(opened)
        self.assertEqual(envelope, self.payloads[-1])

    def test_authoritative_end_existing_sibling_but_not_new_worktree_or_wrong_author(
        self,
    ):
        sibling = self.root / "sibling"
        self.git("worktree", "add", "-q", "-b", "sibling", str(sibling))
        _, opened = self.begin_at()
        expected = self.dated_commit(opened, 2, path=sibling)
        self.dated_commit(opened, 3, path=sibling, human=True)
        self.dated_commit(opened, 11, path=sibling)
        new = self.root / "new"
        self.git("worktree", "add", "-q", "-b", "new", str(new))
        self.dated_commit(opened, 4, path=new)
        self.finish_at(opened)
        self.assertEqual([expected], self.payloads[-1]["work"]["commits"])

    def test_timestamp_snapshots_persist_real_reflog_dates_without_messages(self):
        _, opened = self.begin_at()
        sha = self.dated_commit(opened, 2)
        snapshot = capture_git.snapshot(self.repo)
        stamp = int(datetime(2024, 1, 1, 0, 0, 2, tzinfo=UTC).timestamp())
        self.assertIn(
            stamp, [entry[1] for entry in snapshot["timed_entries"][str(self.repo)]]
        )
        self.assertNotIn("private message", json.dumps(snapshot))
        self.assertNotIn(sha, json.dumps(snapshot))

    def test_reflog_event_time_not_commit_time_controls_historic_collection(self):
        _, opened = self.begin_at()
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(opened["environment"])
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

        def event(second):
            # Both commit objects have in-window author AND committer dates.
            # Only the actual reflog event distinguishes later work.
            env.update(
                GIT_AUTHOR_DATE="2024-01-01T00:00:02+00:00",
                GIT_COMMITTER_DATE="2024-01-01T00:00:02+00:00",
            )
            sha = subprocess.check_output(
                [
                    "git",
                    "commit-tree",
                    "HEAD^{tree}",
                    "-p",
                    "HEAD",
                    "-m",
                    "private object",
                ],
                cwd=self.repo,
                env=env,
                text=True,
            ).strip()
            env["GIT_COMMITTER_DATE"] = f"2024-01-01T00:00:{second:02d}+00:00"
            subprocess.run(
                ["git", "update-ref", "-m", "commit: private event", "HEAD", sha],
                cwd=self.repo,
                env=env,
                check=True,
                capture_output=True,
            )
            return sha

        inside = event(3)
        event(11)
        self.finish_at(opened)
        self.assertEqual([inside], self.payloads[-1]["work"]["commits"])

    def test_prior_snapshot_without_timestamp_evidence_stays_conservative(self):
        store, opened = self.begin_at()
        record = store.get(opened["capture_id"])
        record["before"].pop("timed_entries", None)
        store.save(record)
        self.dated_commit(opened, 2)
        self.finish_at(opened)
        self.assertEqual([], self.payloads[-1]["work"]["commits"])
        self.assertEqual(
            "unavailable_authoritative_end_time",
            self.payloads[-1]["extensions"]["devdiary_capture"]["git_collection"],
        )

    def test_changed_root_and_preexisting_entries_are_not_collected(self):
        _, opened = self.begin_at()
        self.dated_commit(opened, 2)
        before = capture_git.snapshot(self.repo)
        with mock.patch("devdiary.contract.timestamp", return_value=self.START):
            store = Store(self.root / "second-state")
            self.addCleanup(store.close)
            second = capture.begin(store, self.registry, self.begin_data("run:second"))
        record = capture.freeze(
            store,
            {
                "capture_id": second["capture_id"],
                "outcome": "completed",
                "ended_at": self.END,
            },
        )
        self.assertEqual([], record["envelope"]["work"]["commits"])
        self.assertEqual(before["root"], str(self.repo))
        record = store.get(second["capture_id"])
        record["state"] = "open"
        record["before"]["root"] = str(self.root / "other-repo")
        store.save(record)
        record = capture.freeze(
            store,
            {
                "capture_id": second["capture_id"],
                "outcome": "completed",
                "ended_at": self.END,
            },
        )
        self.assertEqual([], record["envelope"]["work"]["commits"])
        self.assertEqual(
            "unavailable_author_identity_or_repository",
            record["envelope"]["extensions"]["devdiary_capture"]["git_collection"],
        )
