from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from devdiary import capture
from devdiary.capture_store import Store


class CaptureStatusTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "state"
        self.store = Store(self.directory)
        self.addCleanup(self.store.close)

    def insert(self, run, state="open", source=None):
        record = {
            "capture_id": run,
            "run_ref": run,
            "state": state,
            "source": source or {},
            "actor_ref": "actor:one",
            "actor": {},
        }
        self.store.db.execute(
            "INSERT INTO captures(capture_id,run_ref,state,record) VALUES (?,?,?,?)",
            (run, run, state, json.dumps(record)),
        )

    def test_large_unrelated_and_delivered_history_is_not_scanned_or_decoded(self):
        with self.store.transaction():
            for i in range(12000):
                self.insert(f"other:{i}", source={"system": "other"})
                self.insert(f"delivered:{i}", "delivered", {"system": "paperclip"})
            for i in range(6):
                self.insert(
                    f"wanted:{i}",
                    "open" if i % 2 else "pending",
                    {"system": "paperclip"},
                )
        steps = 0

        def progress():
            nonlocal steps
            steps += 100
            return int(steps > 10000)

        self.store.db.set_progress_handler(progress, 100)
        try:
            with mock.patch("devdiary.capture.json.loads", wraps=json.loads) as loads:
                page = capture.status(
                    self.store,
                    {
                        "states": ["open", "pending"],
                        "source": {"system": "paperclip"},
                        "limit": 2,
                    },
                )
                self.assertLessEqual(loads.call_count, 3)
            self.assertEqual(
                ["wanted:0", "wanted:1"], [r["run_ref"] for r in page["captures"]]
            )
            second = capture.status(
                self.store,
                {
                    "states": ["open", "pending"],
                    "source": {"system": "paperclip"},
                    "limit": 4,
                    "after": page["next_cursor"],
                },
            )
            self.assertEqual(
                [f"wanted:{i}" for i in range(2, 6)],
                [r["run_ref"] for r in second["captures"]],
            )
            self.assertIsNone(second["next_cursor"])
            self.assertLess(steps, 10000)
        finally:
            self.store.db.set_progress_handler(None, 0)

    def test_company_scope_seeks_past_other_companies_in_same_system(self):
        wanted = {"system": "paperclip", "company_id": "wanted"}
        with self.store.transaction():
            for i in range(12000):
                self.insert(
                    f"other:{i}", source={"system": "paperclip", "company_id": "other"}
                )
            self.insert("wanted", source=wanted)
        # A scan of the system bucket (even in SQLite) exceeds this VM budget.
        calls = 0

        def progress():
            nonlocal calls
            calls += 1
            return int(calls > 10)

        self.store.db.set_progress_handler(progress, 100)
        try:
            page = capture.status(self.store, {"source": wanted, "states": ["open"]})
            self.assertEqual(["wanted"], [r["run_ref"] for r in page["captures"]])
        finally:
            self.store.db.set_progress_handler(None, 0)

    def test_exact_partial_filters_empty_states_and_state_updates(self):
        source = {
            "system": "Paperclip",
            "company_id": "a%b_",
            "agent_id": "o'ne",
            "run_id": "r:1",
        }
        self.insert("one", source=source)
        self.insert("two", source={**source, "company_id": "aXbY"})
        self.insert("missing")
        for fields in (
            {"company_id": "a%b_"},
            source,
            {"agent_id": "o'ne", "company_id": "a%b_"},
        ):
            page = capture.status(self.store, {"source": fields})
            self.assertEqual(["one"], [r["run_ref"] for r in page["captures"]])
        self.assertEqual(
            [],
            capture.status(self.store, {"source": {"system": "paperclip"}})["captures"],
        )
        self.assertEqual([], capture.status(self.store, {"states": []})["captures"])
        record = self.store.get("one")
        record["state"] = "pending"
        self.store.save(record)
        self.assertEqual(
            ["one"],
            [
                r["run_ref"]
                for r in capture.status(self.store, {"states": ["pending"]})["captures"]
            ],
        )
        self.assertEqual(
            [], capture.status(self.store, {"after": "999999999999999999"})["captures"]
        )

    def test_projection_preserves_public_shape_and_snapshot_identity(self):
        for index, identities in enumerate(
            (
                {},
                {"git_name": ""},
                {"git_email": "é@example.test"},
                {"git_name": "Frozen", "git_email": "frozen@example.test"},
            )
        ):
            run = f"shape:{index}"
            self.insert(run)
            record = self.store.get(run)
            record.update(
                actor={"identities": {**identities, "private_identity": "PRIVATE"}},
                started_at="2024-01-01T00:00:00Z",
                ended_at=None,
                receipt={"session_id": 1, "nested": [True, None]},
                before={"private": "PRIVATE"},
                credential={"key_file": "PRIVATE"},
                environment={"PRIVATE": "PRIVATE"},
                envelope={"PRIVATE": "PRIVATE"},
            )
            self.store.save(record)
            expected = capture.public(record)
            git_identity = {
                field: identities[key]
                for field, key in (("name", "git_name"), ("email", "git_email"))
                if identities.get(key)
            }
            if git_identity:
                expected["git_identity"] = git_identity
            with mock.patch("devdiary.capture.json.loads", wraps=json.loads) as loads:
                actual = capture.status(self.store, {"after": str(index), "limit": 1})
            self.assertEqual([expected], actual["captures"])
            for call in loads.call_args_list:
                self.assertNotIn("PRIVATE", call.args[0])
            self.assertNotIn("last_attempt_at", actual["captures"][0])
            self.assertIsNone(actual["captures"][0]["ended_at"])

    @unittest.skipUnless(sys.platform == "linux", "requires Linux RLIMIT_AS")
    def test_default_status_page_large_private_records_has_bounded_memory(self):
        # Build valid sibling-worktree snapshot records in the child, then
        # exercise the actual CLI under a hard address-space ceiling. No mocks
        # of the SQL/fetch/JSON path; the parent never changes its memory limit.
        script = textwrap.dedent("""
            import resource, sys
            from pathlib import Path
            from devdiary import capture, cli, git_refs
            from devdiary.capture_store import Store
            from devdiary.config import default_registry
            from unittest import mock

            resource.setrlimit(resource.RLIMIT_AS, (256 * 1048576, 256 * 1048576))
            root = Path(sys.argv[1])
            store = Store(root / 'large')
            registry = default_registry('urn:test', 'urn:test:human:owner', None)
            registry['actors'] = [{
                'actor_ref': 'urn:test:actor:one', 'kind': 'agent',
                'display_name': 'One', 'attester_ref': 'urn:test:attester:one',
                'identities': {'git_name': 'N' * 4096, 'git_email': 'e' * 4083 + '@example.test'},
            }]
            entries = frozenset((f'{i:040x}', 'commit: private subject') for i in range(git_refs.REFLOG_LIMIT))
            timed = frozenset((*entry, 1704067200) for entry in entries)
            snapshot = git_refs.GitSnapshot(
                root, 'test/repo', '0' * 40, entries,
                {str(root / f'sibling-{i}'): entries for i in range(4)},
                {str(root / f'sibling-{i}'): timed for i in range(4)},
            )
            with mock.patch.object(git_refs, 'capture', return_value=snapshot):
                opened = capture.begin(store, registry, {
                    'actor_ref': 'urn:test:actor:one', 'run_ref': 'run:0',
                    'cwd': str(root), 'execution_chain': [{'kind': 'executor', 'system': 'test'}],
                    'source': {'system': 'paperclip', 'company_id': 'c' * 4096},
                })
            assert store.db.execute('SELECT length(record) FROM captures').fetchone()[0] > 5 * 1048576
            # Clone an actual begin record inside SQLite without accumulating
            # private records in Python; each remains an open capture.
            with store.transaction():
                for i in range(1, 101):
                    state = 'open'
                    store.db.execute(
                        "INSERT INTO captures(capture_id,run_ref,state,record) "
                        "SELECT ?,?,?,json_set(record,'$.capture_id',?,'$.run_ref',?,'$.state',?) "
                        "FROM captures WHERE capture_id=?",
                        (f'capture:{i}', f'run:{i}', state, f'capture:{i}', f'run:{i}', state, opened['capture_id']),
                    )
            store.close()
            del snapshot, entries, timed
            store = Store(root / 'large')
            last = capture.status(store, {'limit': 500, 'after': '100', 'states': ['open'], 'source': {'system': 'paperclip'}})
            assert [r['run_ref'] for r in last['captures']] == ['run:100']
            assert last['next_cursor'] is None
            store.close()
            raise SystemExit(cli.main(['--config', str(root / 'absent-registry'), 'capture', 'status', '--state-dir', str(root / 'large'), '--json']))
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.directory)],
            input="{}",
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        page = json.loads(result.stdout)
        self.assertEqual(100, len(page["captures"]))
        self.assertEqual("100", page["next_cursor"])
        self.assertEqual(
            [f"run:{i}" for i in range(100)], [r["run_ref"] for r in page["captures"]]
        )
        self.assertTrue(
            all(
                r["git_identity"]
                == {"name": "N" * 4096, "email": "e" * 4083 + "@example.test"}
                for r in page["captures"]
            )
        )
        self.assertNotIn("private subject", result.stdout)
        # The 1 MiB input/envelope ceiling is not an output-page ceiling:
        # bounded public fields plus snapshot Git identities can exceed it.
        self.assertGreater(len(result.stdout.encode()), 1048576)

    def test_existing_database_gains_indexes_without_rewriting_records(self):
        directory = self.directory / "legacy"
        directory.mkdir(mode=0o700)
        path = directory / "captures.sqlite3"
        path.touch(mode=0o600)
        db = sqlite3.connect(path)
        db.execute(
            "CREATE TABLE captures (seq INTEGER PRIMARY KEY AUTOINCREMENT, capture_id TEXT NOT NULL UNIQUE, run_ref TEXT NOT NULL UNIQUE, state TEXT NOT NULL, record TEXT NOT NULL)"
        )
        raw = '{"capture_id":"old","run_ref":"old","state":"open","source":{"system":"paperclip"},"actor":{}}'
        db.execute("INSERT INTO captures VALUES (17,'old','old','open',?)", (raw,))
        db.commit()
        db.close()
        store = Store(directory)
        self.addCleanup(store.close)
        self.assertEqual(
            raw, store.db.execute("SELECT record FROM captures").fetchone()[0]
        )
        self.assertEqual(
            "old",
            capture.status(store, {"source": {"system": "paperclip"}})["captures"][0][
                "run_ref"
            ],
        )
        indexes = {r[1] for r in store.db.execute("PRAGMA index_list(captures)")}
        self.assertIn("captures_state_seq", indexes)
        self.assertIn("captures_source_system_state_seq", indexes)
