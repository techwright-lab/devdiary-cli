"""Subprocess transport fixtures, not Rails or stock-runtime qualification."""

import http.server
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "target/debug/devdiary-collector"


class PairingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "remote",
                "add",
                "origin",
                "git@github.com:acme/demo.git",
            ],
            check=True,
        )
        self.calls = []
        self.mode = "ok"
        self.installation = ""
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.calls.append(
                    (
                        self.path,
                        data,
                        self.headers.get("Authorization"),
                        time.monotonic(),
                    )
                )
                if self.path.endswith("/exchange"):
                    if (
                        owner.mode == "pending"
                        and sum(x[0].endswith("/exchange") for x in owner.calls) == 1
                    ):
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"schema_version":1,"status":"pending"}')
                        return
                    if owner.mode == "lost":
                        self.connection.close()
                        return
                    if owner.mode == "consumed":
                        self.send_response(410)
                        self.end_headers()
                        self.wfile.write(b'{"error":"consumed"}')
                        return
                    if owner.mode == "redirect":
                        self.send_response(307)
                        self.send_header("Location", "/leak")
                        self.end_headers()
                        return
                    if (
                        owner.mode == "rate"
                        and sum(x[0].endswith("/exchange") for x in owner.calls) == 1
                    ):
                        self.send_response(429)
                        self.send_header("Retry-After", "6")
                        self.end_headers()
                        return
                    doc = {
                        "schema_version": 1,
                        "status": "consumed",
                        "connection": {
                            "collector_ref": "urn:devdiary:collector:11111111-1111-4111-8111-111111111111",
                            "token": "dc_live_" + "s" * 43,
                            "installation_id": owner.installation,
                            "runtime": "claude-code",
                            "repository_ref": "https://github.com/acme/"
                            + ("other" if owner.mode == "mismatch" else "demo"),
                            "endpoint_path": "/ingest/v1/observations",
                        },
                    }
                    status = 200
                else:
                    if owner.mode == "start-rate":
                        self.send_response(429)
                        self.send_header("Retry-After", "60")
                        self.end_headers()
                        return
                    owner.installation = data["installation_id"]
                    doc = {
                        "schema_version": 1,
                        "pairing_token": "dp_pair_" + "p" * 43,
                        "user_code": "a" * 32,
                        "verification_path": "/phoenix/settings/collector",
                        "interval": 5,
                        "expires_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60)
                        ),
                    }
                    status = 201
                    if owner.mode == "expired":
                        doc["expires_at"] = "2000-01-01T00:00:00Z"
                    if owner.mode == "bad-path":
                        doc["verification_path"] = (
                            "/phoenix/settings/collector?secret=bad"
                        )
                body = json.dumps(doc).encode()
                if owner.mode == "duplicate" and self.path.endswith("/exchange"):
                    body = body.replace(
                        b'"runtime": "claude-code"',
                        b'"runtime": "evil", "runtime": "claude-code"',
                    )
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def command(self, *extra):
        return [
            str(BIN),
            "setup",
            str(self.root / "connection"),
            str(self.repo),
            "--origin",
            self.origin,
            "--trust-origin",
            "--no-browser",
            *extra,
        ]

    def run_setup(self, *extra):
        p = subprocess.run(
            self.command(*extra),
            capture_output=True,
            check=False,
            timeout=25,
            env={
                **os.environ,
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "",
            },
        )
        self.assertNotIn(b"dp_pair_", p.stdout + p.stderr)
        self.assertNotIn(b"dc_live_", p.stdout + p.stderr)
        return p

    def test_pair_and_pin_without_hooks(self):
        p = self.run_setup()
        self.assertEqual(p.returncode, 0, p.stderr)
        c = self.root / "connection/connection.json"
        self.assertEqual(c.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(c.read_text())["scope"]["repository_ref"],
            "https://github.com/acme/demo",
        )
        self.assertFalse((self.repo / ".claude").exists())
        self.assertEqual(self.calls[0][1]["runtime"], "claude-code")
        self.assertNotIn(str(self.repo), json.dumps([x[1] for x in self.calls]))
        self.assertEqual(self.calls[1][2], "Bearer dp_pair_" + "p" * 43)
        self.assertGreaterEqual(self.calls[1][3] - self.calls[0][3], 5)
        before = c.read_bytes()
        self.assertEqual(self.run_setup().returncode, 0)
        self.assertEqual(c.read_bytes(), before)
        self.assertEqual(len(self.calls), 2)

    def test_ambiguous_remotes_require_choice(self):
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "remote",
                "add",
                "upstream",
                "https://github.com/other/demo.git",
            ],
            check=True,
        )
        self.assertNotEqual(self.run_setup().returncode, 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_setup("--remote", "origin").returncode, 0)

    def test_lost_response_requires_explicit_new_pair(self):
        self.mode = "lost"
        self.assertNotEqual(self.run_setup().returncode, 0)
        count = len(self.calls)
        p = self.run_setup()
        self.assertIn(b"new-pair", p.stderr)
        self.assertEqual(len(self.calls), count)
        installation = self.installation
        self.mode = "ok"
        self.assertEqual(self.run_setup("--new-pair").returncode, 0)
        self.assertEqual(self.installation, installation)

    def test_scope_mismatch_consumed_redirect_fail_closed(self):
        for mode in ["mismatch", "consumed", "redirect"]:
            self.mode = mode
            p = self.run_setup(*(["--new-pair"] if self.calls else []))
            self.assertNotEqual(p.returncode, 0)
            self.assertFalse((self.root / "connection/connection.json").exists())
        self.assertNotIn("/leak", [x[0] for x in self.calls])

    def test_retry_after(self):
        self.mode = "rate"
        self.assertEqual(self.run_setup().returncode, 0)
        self.assertGreaterEqual(self.calls[2][3] - self.calls[1][3], 6)

    def test_start_retry_after_survives_restart(self):
        self.mode = "start-rate"
        self.assertNotEqual(self.run_setup().returncode, 0)
        self.mode = "ok"
        self.assertNotEqual(self.run_setup("--new-pair").returncode, 0)
        self.assertEqual(len(self.calls), 1)

    def test_interrupt_pins_identity_and_requires_repair(self):
        with subprocess.Popen(
            self.command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as p:
            assert p.stdout is not None
            self.assertTrue(p.stdout.readline())
            p.send_signal(__import__("signal").SIGINT)
            p.communicate(timeout=5)
        self.assertTrue((self.root / "connection/installation.json").exists())
        self.assertNotEqual(self.run_setup().returncode, 0)

    def test_pending_obeys_interval(self):
        self.mode = "pending"
        self.assertEqual(self.run_setup().returncode, 0)
        self.assertGreaterEqual(self.calls[2][3] - self.calls[1][3], 5)

    def test_expiry_bad_path_duplicate_fail_closed(self):
        for mode in ["expired", "bad-path", "duplicate"]:
            self.mode = mode
            self.assertNotEqual(
                self.run_setup(*(["--new-pair"] if self.calls else [])).returncode, 0
            )
            self.assertFalse((self.root / "connection/connection.json").exists())
        self.assertEqual(sum(c[0].endswith("/exchange") for c in self.calls), 1)

    def test_plan_consent_health_and_frozen_scope(self):
        import shutil
        import sqlite3

        exe = self.root / "collector"
        shutil.copyfile(BIN, exe)
        exe.chmod(0o700)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "remote",
                "add",
                "upstream",
                "https://github.com/other/demo.git",
            ],
            check=True,
        )
        self.assertEqual(self.run_setup("--remote", "origin").returncode, 0)
        root = self.root / "connection"
        settings, plan = self.root / "settings.json", self.root / "plan.json"
        original = {
            "env": {"KEEP": "private-value"},
            "hooks": {"Stop": []},
            "disableAllHooks": True,
        }
        settings.write_text(json.dumps(original))
        settings.chmod(0o600)

        def command(*args, ok=True):
            p = subprocess.run(
                [str(exe), *map(str, args)], capture_output=True, timeout=5, check=False
            )
            self.assertEqual(p.returncode == 0, ok, p.stderr)
            self.assertNotIn(b"dc_live_", p.stdout + p.stderr)
            return p.stdout

        command("setup-plan", root, settings, plan)
        self.assertEqual(json.loads(settings.read_text()), original)
        command("claude-apply", plan, ok=False)
        command("claude-apply", plan, "--consent")
        health = json.loads(command("connection-status", root, plan))
        self.assertEqual(health["local_registration"]["registration"], "installed")
        self.assertTrue(health["local_registration"]["target_settings_block"])
        self.assertEqual(health["host_trust"], "unknown")
        self.assertEqual(health["local_observed"], 0)
        command("claude-remove", plan, "--consent")
        self.assertEqual(json.loads(settings.read_text()), original)
        self.assertNotIn("private-value", plan.read_text())
        self.assertNotIn("dc_live_", plan.read_text())
        data = json.loads((root / "connection.json").read_text())
        observation = {
            "schema_version": 1,
            "observation_id": "22222222-2222-4222-8222-222222222222",
            "installation_id": data["scope"]["installation_id"],
            "repository": str(self.repo),
            "runtime": "claude-code",
            "session_id": "fixture",
            "event": "SessionStart",
            "observed_at": time.time(),
            "attribution_basis": "unknown",
        }
        subprocess.run(
            [str(exe), "collect", str(root / "outbox")],
            input=json.dumps(observation).encode(),
            capture_output=True,
            check=True,
        )
        with sqlite3.connect(root / "outbox/collector-rust-v1.sqlite3") as db:
            frozen = db.execute("SELECT payload FROM outbox").fetchone()[0]
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "remote",
                "set-url",
                "origin",
                "https://github.com/acme/other",
            ],
            check=True,
        )
        self.assertNotEqual(
            self.run_setup("--remote", "origin", "--new-pair").returncode, 0
        )
        command("setup-plan", root, settings, self.root / "new-plan.json", ok=False)
        with sqlite3.connect(root / "outbox/collector-rust-v1.sqlite3") as db:
            self.assertEqual(
                db.execute("SELECT payload FROM outbox").fetchone()[0], frozen
            )

    def test_insecure_state_and_untrusted_origin_never_connect(self):
        connection = self.root / "connection"
        connection.mkdir(mode=0o755)
        self.assertNotEqual(self.run_setup().returncode, 0)
        connection.rmdir()
        connection.symlink_to(self.repo, target_is_directory=True)
        self.assertNotEqual(self.run_setup().returncode, 0)
        connection.unlink()
        p = subprocess.run(
            [
                str(BIN),
                "setup",
                str(connection),
                str(self.repo),
                "--origin",
                self.origin,
                "--no-browser",
            ],
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
