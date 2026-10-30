"""Parent pairing harness cleanup, without PostgreSQL/Rails/vendor execution."""

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pairing_rails as p

BIN = Path(__file__).resolve().parents[1] / "target/debug/devdiary-collector"


class PairingCleanupTest(unittest.TestCase):
    def exercise(self, fault):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "collector"
            binary.write_bytes(b"fixture")
            calls = []
            queries = 0

            def run(argv, **kwargs):
                nonlocal queries
                calls.append(list(map(str, argv)))
                if argv[0] == "psql":
                    queries += 1
                    return b"1" if fault == "preexisting" else b"0"
                if argv[0] == "createdb" and fault == "create_interrupt":
                    raise KeyboardInterrupt
                if argv[0] == "bundle":
                    raise RuntimeError("rails_failed")
                if argv[0] == "dropdb" and fault == "drop_timeout":
                    raise RuntimeError("drop_failed")
                return b"fixture-sha"

            output = io.StringIO()
            with (
                patch.object(p, "run", side_effect=run),
                patch.object(p.signal, "signal"),
                patch(
                    "sys.argv",
                    [
                        "pairing_rails.py",
                        "--rails-checkout",
                        str(root),
                        "--collector",
                        str(binary),
                    ],
                ),
                contextlib.redirect_stdout(output),
                self.assertRaises((Exception, KeyboardInterrupt)),
            ):
                p.main()
            report = json.loads(output.getvalue())
            self.assertFalse(report.get("passed", False))
            drops = [c for c in calls if c[0] == "dropdb"]
            if fault == "preexisting":
                self.assertEqual(drops, [])
                self.assertEqual(queries, 1)
            else:
                self.assertEqual(len(drops), 1)
                self.assertEqual(drops[0][-1], report["database_name"])
                self.assertEqual(queries, 2)
                self.assertTrue(report["database_dropped"])

    def test_verified_absent_name_reserved_before_interrupted_create(self):
        self.exercise("create_interrupt")

    def test_failed_rails_still_drops(self):
        self.exercise("rails")

    def test_drop_error_still_checks_absence_and_prints_owned_name(self):
        self.exercise("drop_timeout")

    def test_preexisting_database_is_never_dropped(self):
        self.exercise("preexisting")


class ConnectionSyncRefusalTest(unittest.TestCase):
    """Real binary against a fake ingest server; the connection is built locally."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="connection-sync-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.exe = self.root / "collector"
        shutil.copyfile(BIN, self.exe)
        self.exe.chmod(0o700)
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin"]
            + ["git@github.com:acme/demo.git"],
            check=True,
        )
        self.status, self.headers, self.requests = 201, {}, []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(sent)
                body = json.dumps(
                    {
                        "collector_ref": "urn:devdiary:collector:"
                        + "1" * 8
                        + "-1111-4111-8111-"
                        + "1" * 12,
                        "observation_id": sent["observation_id"],
                        "installation_id": sent["installation_id"],
                        "record_id": 9,
                    }
                ).encode()
                self.send_response(owner.status)
                for name, value in owner.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.connection = self.root / "connection"
        self.connection.mkdir(mode=0o700)
        self.connection.chmod(0o700)
        origin = f"http://127.0.0.1:{server.server_port}"
        installation = {
            "installation_id": str(uuid.uuid4()),
            "origin": origin,
            "repository": str(self.repo.resolve()),
            "repository_ref": "https://github.com/acme/demo",
            "remote": None,
        }
        scope = {
            "endpoint": origin + "/ingest/v1/observations",
            "collector_ref": "urn:devdiary:collector:"
            + "1" * 8
            + "-1111-4111-8111-"
            + "1" * 12,
            **{
                k: installation[k]
                for k in ("installation_id", "repository", "repository_ref")
            },
        }
        self.write(self.connection / "installation.json", installation)
        self.write(
            self.connection / "connection.json",
            {"scope": scope, "token": "dc_live_" + "s" * 43},
        )
        self.settings = self.root / "settings.json"
        self.write(self.settings, {})
        self.plan = self.root / "plan.json"
        self.run_collector("setup-plan", self.connection, self.settings, self.plan)
        self.run_collector("claude-apply", self.plan, "--consent")

    def write(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def run_collector(self, *args, code=0, data=None):
        result = subprocess.run(
            [str(self.exe), *map(str, args)],
            input=data,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def hook(self, tool_use_id):
        event = {
            "hook_event_name": "PostToolUse",
            "session_id": "session-1",
            "cwd": str(self.repo),
            "tool_use_id": tool_use_id,
        }
        self.run_collector("claude-hook", self.plan, data=json.dumps(event).encode())

    def health(self):
        return json.loads(
            self.run_collector("connection-status", self.connection).stdout
        )

    def test_revoked_credential_stops_spooling_and_syncing(self):
        self.hook("before-revoke")
        self.status = 401
        self.run_collector("connection-sync", self.connection, "10", code=1)
        self.assertTrue((self.connection / "revoked").exists())
        self.hook("after-revoke")
        health = self.health()
        self.assertEqual(health["local_pending"], 1)
        self.assertEqual(health["local_drops"], {"revoked": 1})
        self.assertTrue(health["local_revoked"])
        self.status = 201
        self.run_collector("connection-sync", self.connection, "10", code=2)
        self.assertEqual(len(self.requests), 1)

    def test_retry_after_is_respected_on_the_next_sync(self):
        self.hook("first")
        self.status, self.headers = 429, {"Retry-After": "120"}
        self.run_collector("connection-sync", self.connection, "10", code=1)
        self.status, self.headers = 201, {}
        result = self.run_collector("connection-sync", self.connection, "10", code=2)
        self.assertIn(b"Retry-After", result.stderr)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((self.connection / "revoked").exists())


if __name__ == "__main__":
    unittest.main()
