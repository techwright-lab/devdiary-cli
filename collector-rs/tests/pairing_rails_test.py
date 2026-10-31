"""Parent pairing harness cleanup, without PostgreSQL/Rails/vendor execution."""

import contextlib
import email.utils
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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

    def test_sync_resumes_once_retry_after_has_passed(self):
        self.hook("first")
        self.status, self.headers = 429, {"Retry-After": "1"}
        self.run_collector("connection-sync", self.connection, "10", code=1)
        self.status, self.headers = 201, {}
        self.run_collector("connection-sync", self.connection, "10", code=2)
        time.sleep(1.1)
        self.run_collector("connection-sync", self.connection, "10")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.health()["local_pending"], 0)

    def test_http_date_retry_after_blocks_the_next_sync(self):
        self.hook("first")
        later = email.utils.formatdate(time.time() + 3600, usegmt=True)
        self.status, self.headers = 429, {"Retry-After": later}
        self.run_collector("connection-sync", self.connection, "10", code=1)
        self.status, self.headers = 201, {}
        self.run_collector("connection-sync", self.connection, "10", code=2)
        self.assertEqual(len(self.requests), 1)

    def test_forbidden_is_treated_as_revocation(self):
        self.hook("first")
        self.status = 403
        self.run_collector("connection-sync", self.connection, "10", code=1)
        self.assertTrue((self.connection / "revoked").exists())


COLLECTOR_REF = "urn:devdiary:collector:" + "1" * 8 + "-1111-4111-8111-" + "1" * 12


class InstallationPairingTest(unittest.TestCase):
    """Real binary pairs an installation scope against a fake pairing server."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="installation-pairing-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.exe = self.root / "collector"
        shutil.copyfile(BIN, self.exe)
        self.exe.chmod(0o700)
        self.starts, self.scope_status = [], 200
        self.connection_shape = {}
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
                self.reply(
                    owner.scope_status,
                    {
                        "schema_version": 1,
                        "collector_ref": COLLECTOR_REF,
                        "scope_kind": "installation",
                        "repository_refs": ["https://github.com/acme/later"],
                        "observation_schema_versions": [1, 2],
                        "orchestrators": [
                            {"key": "paperclip", "env": {"run_ref": "PAPERCLIP_RUN_ID"}}
                        ],
                    },
                )

            def do_POST(self):
                sent = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/exchange"):
                    connection = {
                        "collector_ref": COLLECTOR_REF,
                        "token": "dc_live_" + "s" * 43,
                        "installation_id": owner.starts[-1]["installation_id"],
                        "runtime": "claude-code",
                        "endpoint_path": "/ingest/v1/observations",
                        "scope_kind": "installation",
                        "repository_refs": ["https://github.com/acme/demo"],
                        "scope_path": "/ingest/v1/collector/scope",
                        "observation_schema_versions": [1, 2],
                        **owner.connection_shape,
                    }
                    connection = {k: v for k, v in connection.items() if v is not None}
                    return self.reply(
                        200,
                        {
                            "schema_version": 1,
                            "status": "consumed",
                            "connection": connection,
                        },
                    )
                owner.starts.append(sent)
                self.reply(
                    201,
                    {
                        "schema_version": 1,
                        "pairing_token": "dp_pair_" + "p" * 43,
                        "user_code": "a" * 32,
                        "verification_path": "/phoenix/settings/collector",
                        "interval": 5,
                        "expires_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60)
                        ),
                    },
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.origin = f"http://127.0.0.1:{server.server_port}"
        self.connection = self.root / "connection"

    def run_collector(self, *args, code=0):
        result = subprocess.run(
            [str(self.exe), *map(str, args)],
            capture_output=True,
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn(b"dc_live_", result.stdout + result.stderr)
        return result

    def setup_collector(self, *extra, code=0):
        return self.run_collector(
            "setup",
            self.connection,
            "--origin",
            self.origin,
            "--trust-origin",
            "--no-browser",
            *extra,
            code=code,
        )

    def test_setup_without_repository_pairs_installation_scope(self):
        out = self.setup_collector("--record-orchestrated-outside-repos").stdout
        self.assertIn(b"All active repositories", out)
        (start,) = self.starts
        self.assertEqual(start["scope_kind"], "installation")
        self.assertEqual(
            set(start), {"schema_version", "installation_id", "runtime", "scope_kind"}
        )
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
        health = json.loads(
            self.run_collector("connection-status", self.connection).stdout
        )
        self.assertEqual(health["local_repository_refs"], 1)
        self.setup_collector("--record-orchestrated-outside-repos")
        self.setup_collector(code=2)
        settings, plan = self.root / "settings.json", self.root / "plan.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        self.run_collector("setup-plan", self.connection, settings, plan)

    def test_sync_refreshes_refs_and_a_revoked_scope_read_stops(self):
        self.setup_collector()
        self.run_collector("connection-sync", self.connection, "10")
        refs = json.loads(
            (self.connection / "outbox/repository-refs.json").read_bytes()
        )
        self.assertEqual(refs, ["https://github.com/acme/later"])
        offered = json.loads(
            (self.connection / "outbox/orchestrators-offered.json").read_bytes()
        )
        self.assertEqual(offered["orchestrators"][0]["key"], "paperclip")
        self.scope_status = 401
        self.run_collector("connection-sync", self.connection, "10", code=2)
        self.assertTrue((self.connection / "revoked").exists())

    def test_a_failed_refresh_is_reported_until_a_good_one_clears_it(self):
        self.setup_collector()
        self.run_collector("connection-sync", self.connection, "10")
        refs = self.connection / "outbox/repository-refs.json"
        before = refs.read_bytes()
        self.scope_status = 503
        result = self.run_collector("connection-sync", self.connection, "10")
        (line,) = result.stdout.splitlines()
        self.assertEqual(
            json.loads(line),
            {
                "pending": 0,
                "delivered": 0,
                "scope_refresh_error": "scope_refresh_http_failure",
            },
        )
        self.assertIn(b"The server refused scope refresh", result.stderr)
        self.assertEqual(refs.read_bytes(), before)
        health = json.loads(
            self.run_collector("connection-status", self.connection).stdout
        )
        self.assertEqual(
            health["local_scope_refresh_error"], "scope_refresh_http_failure"
        )
        self.scope_status = 200
        result = self.run_collector("connection-sync", self.connection, "10")
        self.assertEqual(json.loads(result.stdout), {"pending": 0, "delivered": 0})
        self.assertEqual(result.stderr, b"")
        health = json.loads(
            self.run_collector("connection-status", self.connection).stdout
        )
        self.assertIsNone(health["local_scope_refresh_error"])

    def test_a_server_without_v2_pairs_a_v1_installation(self):
        self.connection_shape = {"observation_schema_versions": [1]}
        self.setup_collector()
        saved = json.loads((self.connection / "connection.json").read_bytes())
        self.assertEqual(saved["scope"]["observation_schema_versions"], [1])

    def test_the_outside_repo_opt_in_is_not_saved_without_v2(self):
        self.connection_shape = {"observation_schema_versions": [1]}
        err = self.setup_collector("--record-orchestrated-outside-repos").stderr
        self.assertIn(b"--record-orchestrated-outside-repos", err)
        saved = json.loads((self.connection / "connection.json").read_bytes())
        self.assertFalse(saved["scope"]["record_orchestrated_outside_repos"])
        self.run_collector("connection-status", self.connection)
        self.run_collector("connection-sync", self.connection, "10")
        self.setup_collector("--record-orchestrated-outside-repos")
        settings, plan = self.root / "settings.json", self.root / "plan.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        self.run_collector("setup-plan", self.connection, settings, plan)

    def test_an_outside_repo_opt_in_cannot_be_widened_after_pairing(self):
        self.connection_shape = {"observation_schema_versions": [1]}
        self.setup_collector("--record-orchestrated-outside-repos")
        path = self.connection / "connection.json"
        saved = json.loads(path.read_bytes())
        saved["scope"]["record_orchestrated_outside_repos"] = True
        path.write_text(json.dumps(saved))
        self.run_collector("connection-status", self.connection, code=2)
        saved["scope"]["observation_schema_versions"] = [1, 2]
        path.write_text(json.dumps(saved))
        self.run_collector("connection-status", self.connection, code=2)

    def test_an_exchange_for_another_scope_is_refused(self):
        self.connection_shape = {
            "scope_kind": None,
            "scope_path": None,
            "observation_schema_versions": None,
            "repository_refs": None,
            "repository_ref": "https://github.com/acme/demo",
        }
        self.setup_collector(code=2)
        self.assertFalse((self.connection / "connection.json").exists())

    def test_outside_repo_opt_in_is_installation_only(self):
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.run_collector(
            "setup",
            self.connection,
            repo,
            "--record-orchestrated-outside-repos",
            code=2,
        )
        self.assertEqual(self.starts, [])


@unittest.skipUnless(
    os.environ.get("DEVDIARY_RAILS_CHECKOUT"),
    "set DEVDIARY_RAILS_CHECKOUT to a reviewed DevDiary checkout and run on its Ruby PATH",
)
class LocalRailsInteropTest(unittest.TestCase):
    """Repository scope sends v1 and installation scope v2 to a real local server."""

    def test_schema_version_follows_what_the_server_advertises(self):
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("pairing_rails.py")),
                "--rails-checkout",
                os.environ["DEVDIARY_RAILS_CHECKOUT"],
                "--collector",
                str(BIN),
            ],
            capture_output=True,
            check=False,
            timeout=600,
        )
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual(result.returncode, 0, report)
        self.assertTrue(report["repository_scope_v1"])
        self.assertTrue(report["installation_scope_v2"])
        self.assertTrue(report["database_dropped"])


if __name__ == "__main__":
    unittest.main()
