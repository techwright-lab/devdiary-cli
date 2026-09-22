from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from devdiary.config import default_registry, write_registry

ROOT = Path(__file__).resolve().parents[1]
KEY = "fake-capture-key-not-live"


class CaptureFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state/captures.sqlite3"
        self.payloads = []
        self.ack = "valid"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                owner.payloads.append(payload)
                # Terminal evidence must already be committed before HTTP.
                with sqlite3.connect(owner.state_path) as db:
                    owner.assertIn(
                        db.execute(
                            "select state from captures where run_ref=?",
                            (payload["run_ref"],),
                        ).fetchone()[0],
                        ("pending", "delivered"),
                    )
                body = json.dumps(
                    {
                        "actor_ref": payload["actor"]["ref"],
                        "event_id": payload["event_id"],
                        "run_ref": payload["run_ref"],
                        "session_id": 42,
                    }
                    if owner.ack == "valid"
                    else {"status": "created"}
                ).encode()
                self.send_response(201)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.registry = default_registry(
            "urn:test",
            "urn:test:human:owner",
            f"http://127.0.0.1:{self.server.server_port}/ingest/v1/sessions",
        )
        self.actor = {
            "actor_ref": "urn:test:actor:one",
            "kind": "agent",
            "display_name": "One",
            "attester_ref": "urn:test:attester:one",
            "identities": {"git_name": "One", "git_email": "one@example.test"},
        }
        self.registry["actors"] = [self.actor]
        self.config = self.root / "registry.json"
        write_registry(self.config, self.registry)
        self.key = self.root / "key"
        self.key.write_text(KEY)
        self.key.chmod(0o600)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Human")
        self.git("config", "user.email", "human@example.test")
        self.git("remote", "add", "origin", "git@github.com:test/repo.git")
        self.git("commit", "--allow-empty", "-qm", "initial")

    def git(self, *args, environment=None):
        explicit_identity = environment is not None
        supplied = os.environ if environment is None else environment
        environment = {
            k: value
            for k, value in supplied.items()
            if not k.startswith("GIT_")
            or explicit_identity
            and k
            in {
                "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL",
            }
        }
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        return subprocess.check_output(
            ["git", *args], cwd=self.repo, env=environment, text=True
        ).strip()

    def call(self, op, data, code=0):
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("DEVDIARY_", "GIT_"))
        }
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "devdiary",
                "--config",
                str(self.config),
                "capture",
                op,
                "--state-dir",
                str(self.root / "state"),
                "--json",
            ],
            input=json.dumps(data),
            capture_output=True,
            text=True,
            env=env,
            timeout=40,
            check=False,
        )
        self.assertEqual(code, result.returncode, result.stderr)
        self.assertNotIn(KEY, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def begin_data(self, run="run:one", runtime="codex"):
        return {
            "actor_ref": self.actor["actor_ref"],
            "run_ref": run,
            "cwd": str(self.repo),
            "key_file": str(self.key),
            "execution_chain": [
                {"kind": "orchestrator", "system": "paperclip"},
                {"kind": "executor", "system": runtime},
                {"kind": "model", "provider": "test", "model": "model-v1"},
            ],
            "source": {
                "system": "paperclip",
                "company_id": "company-one",
                "agent_id": "agent-one",
                "run_id": run,
            },
            "task_refs": ["task:one", "task:two"],
        }


class CaptureTest(CaptureFixture):
    def test_runtime_switches_same_actor_private_context_and_git(self):
        for runtime in ["codex", "claude", "hermes", "arbitrary-executable"]:
            data = self.begin_data("run:" + runtime, runtime)
            opened = self.call("begin", data)
            self.assertEqual(opened, self.call("begin", data))
            env = opened["environment"]
            self.assertEqual(
                env["DEVDIARY_ATTRIBUTION_CONTEXT"], env["AGENT_ATTRIBUTION_CONTEXT"]
            )
            context_path = Path(env["DEVDIARY_ATTRIBUTION_CONTEXT"])
            if os.name == "posix":
                self.assertEqual(0o400, context_path.stat().st_mode & 0o777)
            else:
                self.assertFalse(context_path.stat().st_mode & 0o200)
            context = json.loads(context_path.read_text())
            self.assertEqual(data["execution_chain"], context["execution_chain"])
            self.assertNotIn("key_file", context_path.read_text())
            agent_commit = self.git(
                "commit",
                "--allow-empty",
                "-qm",
                runtime,
                environment={**os.environ, **env},
            )
            del agent_commit
            sha = self.git("rev-parse", "HEAD")
            self.git("commit", "--allow-empty", "-qm", "unrelated human")
            finished = self.call(
                "finish", {"capture_id": opened["capture_id"], "outcome": "completed"}
            )
            self.assertEqual("delivered", finished["state"])
            self.assertEqual(42, finished["receipt"]["session_id"])
            self.assertEqual([sha], self.payloads[-1]["work"]["commits"])
            self.assertEqual(data["task_refs"], self.payloads[-1]["task_refs"])
            self.assertEqual(self.actor["actor_ref"], self.payloads[-1]["actor"]["ref"])

    def test_pending_restart_freezes_claims_and_snapshots_registry(self):
        opened = self.call("begin", self.begin_data())
        self.registry["actors"][0]["display_name"] = "Changed"
        write_registry(self.config, self.registry, force=True)
        self.ack = "invalid"
        claim = {
            "capture_id": opened["capture_id"],
            "outcome": "failed",
            "work": {"issues": ["https://github.com/test/repo/issues/1"]},
        }
        self.assertEqual("pending", self.call("finish", claim)["state"])
        original = self.payloads[-1]
        self.assertEqual("One", original["actor"]["display_name"])
        self.call("finish", {**claim, "outcome": "completed"}, code=2)
        self.call("finish", {**claim, "work": {"issues": ["changed"]}}, code=2)
        self.ack = "valid"
        self.config.unlink()  # retry/status must not need mutable registry
        self.assertEqual(
            "delivered",
            self.call("retry", {"capture_id": opened["capture_id"]})["state"],
        )
        self.assertEqual(original, self.payloads[-1])
        self.assertEqual("delivered", self.call("finish", claim)["state"])
        self.assertEqual(original, self.payloads[-1])

    def test_concurrent_begin_conflicts_status_filters_and_pagination(self):
        data = self.begin_data()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.call("begin", data), range(4)))
        self.assertEqual(1, len({r["capture_id"] for r in results}))
        changed = self.begin_data(runtime="other")
        self.call("begin", changed, code=2)
        self.call("begin", {**data, "source": {"system": "other"}}, code=2)
        self.call("begin", self.begin_data("run:two"))
        page = self.call(
            "status",
            {"source": {"system": "paperclip"}, "states": ["open"], "limit": 1},
        )
        self.assertEqual(1, len(page["captures"]))
        self.assertIsNotNone(page["next_cursor"])
        second = self.call("status", {"after": page["next_cursor"], "limit": 1})
        self.assertEqual(1, len(second["captures"]))
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(
            [], self.call("status", {"source": {"system": "other"}})["captures"]
        )
        self.assertNotIn("key_file", json.dumps(page))
        self.assertNotIn("execution_chain", json.dumps(page))

    def test_delayed_finish_excludes_later_git_and_retains_explicit(self):
        from devdiary.contract import timestamp

        opened = self.call("begin", self.begin_data())
        ended_at = timestamp()
        self.git(
            "commit",
            "--allow-empty",
            "-qm",
            "later work",
            environment={**os.environ, **opened["environment"]},
        )
        self.call(
            "finish",
            {
                "capture_id": opened["capture_id"],
                "outcome": "completed",
                "ended_at": ended_at,
                "work": {"commits": ["a" * 40]},
            },
        )
        self.assertEqual(["a" * 40], self.payloads[-1]["work"]["commits"])
        self.assertEqual(
            "timestamp_qualified_terminal_second_excluded",
            self.payloads[-1]["extensions"]["devdiary_capture"]["git_collection"],
        )

    def test_runner_uses_durable_store_before_child_and_preserves_exit(self):
        from devdiary import runner

        observed = self.root / "observed.json"
        child = (
            "import os,json,sqlite3; from pathlib import Path; "
            + f"db=sqlite3.connect({str(self.root / 'state/captures.sqlite3')!r}); "
            + "assert db.execute('select state from captures').fetchone()[0]=='open'; "
            + f"Path({str(observed)!r}).write_text(json.dumps(dict(os.environ))); raise SystemExit(7)"
        )
        result = runner.run_command(
            self.registry,
            self.actor,
            [sys.executable, "-c", child],
            self.repo,
            environment={"DEVDIARY_INGEST_KEY": KEY},
            state_directory=self.root / "state",
        )
        self.assertEqual(7, result.exit_code)
        self.assertTrue(result.emitted)
        env = json.loads(observed.read_text())
        self.assertNotIn("DEVDIARY_INGEST_KEY", env)
        self.assertEqual(
            env["AGENT_ATTRIBUTION_CONTEXT"], env["DEVDIARY_ATTRIBUTION_CONTEXT"]
        )
        self.assertEqual("delivered", self.call("status", {})["captures"][0]["state"])

    def test_validation_and_private_key_file(self):
        for update in [
            {"execution_chain": []},
            {
                "execution_chain": [
                    {"kind": "executor", "system": "x", "config": {"secret": KEY}}
                ]
            },
            {"source": {"prompt": KEY}},
            {"env": {"secret": KEY}},
            {"task_refs": "not-array"},
        ]:
            self.call("begin", {**self.begin_data(), **update}, code=2)
        if os.name == "posix":
            self.key.chmod(0o644)
            self.call("begin", self.begin_data(), code=2)
            self.key.chmod(0o600)
        link = self.root / "key-link"
        try:
            link.symlink_to(self.key)
        except OSError:
            self.skipTest("symbolic links are unavailable")
        self.call("begin", {**self.begin_data(), "key_file": str(link)}, code=2)
        self.assertEqual([], self.call("status", {})["captures"])


if __name__ == "__main__":
    unittest.main()
