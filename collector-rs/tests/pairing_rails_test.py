"""Parent pairing harness cleanup, without PostgreSQL/Rails/vendor execution."""

import contextlib
import email.utils
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pairing_rails as p
import pairing_server
import saved_connection
import terminal

BIN = Path(__file__).resolve().parents[1] / "target/debug/devdiary"


class PairingCleanupTest(unittest.TestCase):
    def exercise(self, fault):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
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
                    ["pairing_rails.py", "--rails-checkout", str(root)],
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
    """Real binary against a fake ingest server; the connection is saved locally."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="connection-sync-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
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

            def do_GET(self):
                body = json.dumps(
                    {
                        "schema_version": 1,
                        "collector_ref": COLLECTOR_REF,
                        "scope_kind": "installation",
                        "repository_refs": ["https://github.com/acme/demo"],
                        "observation_schema_versions": [1, 2],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(sent)
                body = json.dumps(
                    {
                        "collector_ref": COLLECTOR_REF,
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
        origin = f"http://127.0.0.1:{server.server_port}"
        home = self.root / "home"
        self.connection = saved_connection.save(
            home, origin, ["https://github.com/acme/demo"]
        )
        self.env = saved_connection.environment(self.root, home)
        settings = self.root / "settings.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        args = [BIN, "setup", "--settings", settings, "--origin", origin]
        code, _, err, _ = terminal.answering(
            [*args, "--trust-origin", "--no-browser"], self.env
        )
        self.assertEqual(code, 0, err)
        record = json.loads((self.connection.parent / "install.json").read_bytes())
        self.exe, self.plan = record["executable"], record["plan"]

    def run_collector(self, *args, code=0, data=None):
        result = subprocess.run(
            [str(self.exe), *map(str, args)],
            input=data,
            capture_output=True,
            check=False,
            timeout=10,
            env=self.env,
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
        self.run_collector("hook", "claude", self.plan, data=json.dumps(event).encode())

    def sync(self):
        self.run_collector("sync", self.connection)

    def health(self):
        return json.loads(self.run_collector("doctor", "--json").stdout)

    def test_revoked_credential_stops_spooling_and_syncing(self):
        self.hook("before-revoke")
        self.status = 401
        self.sync()
        self.assertTrue((self.connection / "revoked").exists())
        self.hook("after-revoke")
        health = self.health()
        self.assertEqual(health["pending"], 1)
        self.assertEqual(health["drops"], {"revoked": 1})
        self.assertTrue(health["revoked"])
        self.status = 201
        self.sync()
        self.assertEqual(len(self.requests), 1)

    def test_retry_after_is_respected_on_the_next_sync(self):
        self.hook("first")
        self.status, self.headers = 429, {"Retry-After": "120"}
        self.sync()
        self.status, self.headers = 201, {}
        self.sync()
        self.assertEqual(self.health()["last_sync_error"], "sync_rate_limited")
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((self.connection / "revoked").exists())

    def test_sync_resumes_once_retry_after_has_passed(self):
        self.hook("first")
        self.status, self.headers = 429, {"Retry-After": "1"}
        self.sync()
        self.status, self.headers = 201, {}
        self.sync()
        self.assertEqual(len(self.requests), 1)
        time.sleep(1.1)
        self.sync()
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.health()["pending"], 0)

    def test_http_date_retry_after_blocks_the_next_sync(self):
        self.hook("first")
        later = email.utils.formatdate(time.time() + 3600, usegmt=True)
        self.status, self.headers = 429, {"Retry-After": later}
        self.sync()
        self.status, self.headers = 201, {}
        self.sync()
        self.assertEqual(len(self.requests), 1)

    def test_forbidden_is_treated_as_revocation(self):
        self.hook("first")
        self.status = 403
        self.sync()
        self.assertTrue((self.connection / "revoked").exists())


COLLECTOR_REF = saved_connection.COLLECTOR_REF


class InstallationPairingTest(unittest.TestCase):
    """`devdiary setup` pairs an installation scope against a fake pairing server."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="installation-pairing-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.scope_status = 200
        self.served_refs = ["https://github.com/acme/demo"]
        self.served_orchestrators = None
        self.contract = pairing_server.Contract()
        contract = self.contract
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                reply = {
                    "schema_version": 1,
                    "collector_ref": COLLECTOR_REF,
                    "scope_kind": "installation",
                    "repository_refs": owner.served_refs,
                    "observation_schema_versions": [1, 2],
                }
                if owner.served_orchestrators is not None:
                    reply["orchestrators"] = owner.served_orchestrators
                self.reply(owner.scope_status, reply)

            def do_POST(self):
                if not contract.handle(self):
                    self.reply(404, {})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.origin = f"http://127.0.0.1:{server.server_port}"
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/settings.json").write_text("{}")
        self.env = saved_connection.environment(self.root, home)
        self.connection = home / ".local/share/devdiary/cli/claude/connection"

    def run_collector(self, *args, code=0):
        result = subprocess.run(
            [str(BIN), *map(str, args)],
            capture_output=True,
            check=False,
            timeout=20,
            env=self.env,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn(b"dc_live_", result.stdout + result.stderr)
        return result

    def setup_collector(self, *extra, code=0):
        args = [BIN, "setup", "--origin", self.origin, "--trust-origin"]
        status, out, err, _ = terminal.answering(
            [*args, "--no-browser", *extra], self.env
        )
        self.assertEqual(status, code, err)
        self.assertNotIn(b"dc_live_", out + err)
        return subprocess.CompletedProcess(args, status, out, err)

    def test_setup_pairs_installation_scope(self):
        self.setup_collector("--record-orchestrated-outside-repos")
        (start,) = self.contract.starts()
        saved = json.loads((self.connection / "connection.json").read_bytes())
        self.assertEqual(saved["repository_refs"], ["https://github.com/acme/demo"])
        self.assertEqual(
            saved["scope"],
            {
                "endpoint": self.origin + "/ingest/v1/observations",
                "collector_ref": COLLECTOR_REF,
                "installation_id": start["installation_id"],
                "scope_kind": "installation",
                "observation_schema_versions": [1, 2],
                "record_orchestrated_outside_repos": True,
            },
        )
        refs = self.connection / "outbox/repository-refs.json"
        self.assertEqual(
            json.loads(refs.read_bytes()), ["https://github.com/acme/demo"]
        )
        self.assertEqual(refs.stat().st_mode & 0o777, 0o600)
        self.run_collector("uninstall")
        self.setup_collector("--record-orchestrated-outside-repos")
        self.assertEqual(len(self.contract.starts()), 1)
        self.run_collector("uninstall")
        self.setup_collector(code=2)

    def test_sync_refreshes_refs_and_a_revoked_scope_read_stops(self):
        self.setup_collector()
        self.served_refs = ["https://github.com/acme/later"]
        self.served_orchestrators = [
            {"key": "paperclip", "env": {"run_ref": "PAPERCLIP_RUN_ID"}}
        ]
        self.run_collector("sync", self.connection)
        refs = json.loads(
            (self.connection / "outbox/repository-refs.json").read_bytes()
        )
        self.assertEqual(refs, ["https://github.com/acme/later"])
        offered = json.loads(
            (self.connection / "outbox/orchestrators-offered.json").read_bytes()
        )
        self.assertEqual(offered["orchestrators"][0]["key"], "paperclip")
        self.scope_status = 401
        self.run_collector("sync", self.connection)
        self.assertTrue((self.connection / "revoked").exists())

    def test_a_failed_refresh_is_reported_until_a_good_one_clears_it(self):
        self.setup_collector()
        self.run_collector("sync", self.connection)
        refs = self.connection / "outbox/repository-refs.json"
        before = refs.read_bytes()
        self.scope_status = 503
        self.run_collector("sync", self.connection)
        self.assertEqual(refs.read_bytes(), before)
        doctor = json.loads(self.run_collector("doctor", "--json").stdout)
        self.assertEqual(
            doctor["last_scope_refresh_error"], "scope_refresh_http_failure"
        )
        self.scope_status = 200
        self.run_collector("sync", self.connection)
        doctor = json.loads(self.run_collector("doctor", "--json").stdout)
        self.assertIsNone(doctor["last_scope_refresh_error"])

    def test_a_server_without_v2_pairs_a_v1_installation(self):
        self.contract.connection_shape = {"observation_schema_versions": [1]}
        self.setup_collector()
        saved = json.loads((self.connection / "connection.json").read_bytes())
        self.assertEqual(saved["scope"]["observation_schema_versions"], [1])

    def test_the_outside_repo_opt_in_is_not_saved_without_v2(self):
        self.contract.connection_shape = {"observation_schema_versions": [1]}
        err = self.setup_collector("--record-orchestrated-outside-repos").stderr
        self.assertIn(b"--record-orchestrated-outside-repos", err)
        saved = json.loads((self.connection / "connection.json").read_bytes())
        self.assertFalse(saved["scope"]["record_orchestrated_outside_repos"])
        self.run_collector("doctor", "--json")
        self.run_collector("sync", self.connection)
        self.run_collector("uninstall")
        self.setup_collector("--record-orchestrated-outside-repos")

    def test_an_outside_repo_opt_in_cannot_be_widened_after_pairing(self):
        self.contract.connection_shape = {"observation_schema_versions": [1]}
        self.setup_collector("--record-orchestrated-outside-repos")
        path = self.connection / "connection.json"
        saved = json.loads(path.read_bytes())
        saved["scope"]["record_orchestrated_outside_repos"] = True
        path.write_text(json.dumps(saved))
        self.run_collector("sync", self.connection, code=2)
        saved["scope"]["observation_schema_versions"] = [1, 2]
        path.write_text(json.dumps(saved))
        self.run_collector("sync", self.connection, code=2)

    def test_an_exchange_for_another_scope_is_refused(self):
        self.contract.connection_shape = {
            "scope_kind": None,
            "scope_path": None,
            "observation_schema_versions": None,
            "repository_refs": None,
            "repository_ref": "https://github.com/acme/demo",
        }
        self.setup_collector(code=2)
        self.assertFalse((self.connection / "connection.json").exists())


@unittest.skipUnless(
    os.environ.get("DEVDIARY_RAILS_CHECKOUT"),
    "set DEVDIARY_RAILS_CHECKOUT to a reviewed DevDiary checkout and run on its Ruby PATH",
)
class LocalRailsInteropTest(unittest.TestCase):
    """`devdiary setup` pairs installation scope against a real local server."""

    def test_setup_pairs_delivers_and_uninstalls_against_rails(self):
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("pairing_rails.py")),
                "--rails-checkout",
                os.environ["DEVDIARY_RAILS_CHECKOUT"],
            ],
            capture_output=True,
            check=False,
            timeout=600,
        )
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(result.returncode, 0, report)
        self.assertTrue(report["installation_scope_v2"])
        self.assertTrue(report["exact_rails_receipt"])
        self.assertTrue(report["fake_scheduler_only"])
        self.assertTrue(report["database_dropped"])


if __name__ == "__main__":
    unittest.main()
