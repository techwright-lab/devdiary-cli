"""One-command install against a mock pairing/ingest server, a temp HOME and a fake systemctl."""

import hashlib
import json
import os
import pty
import select
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "target/debug/devdiary-collector"
VERSION = "0.1.0"
EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "SessionEnd",
]
COLLECTOR_REF = "urn:devdiary:collector:11111111-1111-4111-8111-111111111111"
CODEGRAPH = {"hooks": [{"type": "command", "command": "codegraph hook prompt"}]}
FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/systemctl.log"
case "$*" in
  *is-enabled*) echo enabled ;;
  *is-active*) echo active ;;
esac
"""


class InstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="install-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.config = self.root / "config"
        self.config.mkdir()
        self.fake = self.root / "bin/systemctl"
        self.fake.parent.mkdir()
        self.fake.write_text(FAKE_SYSTEMCTL)
        self.fake.chmod(0o700)
        self.settings = self.home / ".claude/settings.json"
        self.original = (
            json.dumps(
                {"model": "opus", "hooks": {"UserPromptSubmit": [CODEGRAPH]}},
                indent=2,
            )
            + "\n"
        ).encode()
        self.settings.write_bytes(self.original)
        self.settings.chmod(0o644)
        self.data = self.home / ".local/share/devdiary/collector"
        self.received = []
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
                    200,
                    {
                        "schema_version": 1,
                        "collector_ref": COLLECTOR_REF,
                        "scope_kind": "installation",
                        "repository_refs": ["https://github.com/acme/demo"],
                        "observation_schema_versions": [1, 2],
                    },
                )

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/collector_pairings"):
                    owner.installation = data["installation_id"]
                    return self.reply(
                        201,
                        {
                            "schema_version": 1,
                            "pairing_token": "dp_pair_" + "p" * 43,
                            "user_code": "a" * 32,
                            "verification_path": "/settings/collector",
                            "interval": 5,
                            "expires_at": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60)
                            ),
                        },
                    )
                if self.path.endswith("/exchange"):
                    return self.reply(
                        200,
                        {
                            "schema_version": 1,
                            "status": "consumed",
                            "connection": {
                                "collector_ref": COLLECTOR_REF,
                                "token": "dc_live_" + "s" * 43,
                                "installation_id": owner.installation,
                                "runtime": "claude-code",
                                "endpoint_path": "/ingest/v1/observations",
                                "scope_kind": "installation",
                                "scope_path": "/ingest/v1/collector/scope",
                                "repository_refs": ["https://github.com/acme/demo"],
                                "observation_schema_versions": [1, 2],
                            },
                        },
                    )
                owner.received.append(data)
                self.reply(
                    201,
                    {
                        "collector_ref": COLLECTOR_REF,
                        "observation_id": data["observation_id"],
                        "installation_id": data["installation_id"],
                        "record_id": len(owner.received),
                    },
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.origin = f"http://127.0.0.1:{server.server_port}"

    def env(self):
        base = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PAPERCLIP_", "XDG_", "HOME"))
        }
        return {
            **base,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "DEVDIARY_COLLECTOR_SYSTEMCTL": str(self.fake),
        }

    def install_args(self, *extra):
        return [
            str(BIN),
            "install",
            "--origin",
            self.origin,
            "--no-browser",
            *extra,
        ]

    def run_collector(self, *args, code=0, exe=BIN):
        result = subprocess.run(
            [str(exe), *map(str, args)],
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=40,
            env=self.env(),
        )
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn(b"dc_live_", result.stdout + result.stderr)
        return result

    def install(self, *extra, code=0):
        return self.run_collector(*self.install_args(*extra)[1:], code=code)

    def systemctl_log(self):
        log = self.fake.parent / "systemctl.log"
        return log.read_text().splitlines() if log.exists() else []

    def owned_entries(self):
        doc = json.loads(self.settings.read_bytes())
        return {
            event: [e for e in doc["hooks"].get(event, []) if e != CODEGRAPH]
            for event in EVENTS
        }

    def test_install_yes_registers_nine_entries_beside_codegraph(self):
        out = self.install("--yes").stdout.decode()
        doc = json.loads(self.settings.read_bytes())
        self.assertEqual(doc["hooks"]["UserPromptSubmit"][0], CODEGRAPH)
        entries = self.owned_entries()
        self.assertEqual(sum(len(v) for v in entries.values()), 9)
        exe = self.data / VERSION / "devdiary-collector"
        for (entry,) in entries.values():
            self.assertIn(f"'{exe}' claude-hook ", entry["hooks"][0]["command"])
        self.assertEqual(exe.stat().st_mode & 0o777, 0o700)
        self.assertEqual(out.count(" claude-hook "), 9)
        self.assertIn("Self-test passed", out)
        self.assertEqual(
            self.systemctl_log(),
            ["--user daemon-reload", "--user enable --now devdiary-collector-sync.timer"],
        )

    def test_unit_files_snapshot(self):
        self.install("--yes")
        units = self.config / "systemd/user"
        exe = self.data / VERSION / "devdiary-collector"
        self.assertEqual(
            (units / "devdiary-collector-sync.service").read_text(),
            "[Unit]\nDescription=DevDiary collector sync\n\n[Service]\nType=oneshot\n"
            "Nice=10\nNoNewPrivileges=true\n"
            f'ExecStart="{exe}" timer-sync "{self.data / "connection"}"\n',
        )
        timer = (units / "devdiary-collector-sync.timer").read_text()
        self.assertIn("\nOnUnitActiveSec=2min\n", timer)
        self.assertIn("\nPersistent=true\n", timer)
        self.assertIn("\nRandomizedDelaySec=30\n", timer)

    def test_self_test_row_is_never_selected_by_sync(self):
        self.install("--yes")
        outbox = self.data / "connection/outbox/collector-rust-v1.sqlite3"
        with sqlite3.connect(outbox) as db:
            (selftests,) = db.execute("SELECT count(*) FROM self_test").fetchone()
            (pending,) = db.execute("SELECT count(*) FROM outbox").fetchone()
        self.assertEqual((selftests, pending), (1, 0))
        exe = self.data / VERSION / "devdiary-collector"
        self.run_collector("timer-sync", self.data / "connection", exe=exe)
        self.run_collector("connection-sync", self.data / "connection", "100")
        self.assertEqual(self.received, [])
        with sqlite3.connect(outbox) as db:
            (selftests,) = db.execute("SELECT count(*) FROM self_test").fetchone()
        self.assertEqual(selftests, 1)

    def test_uninstall_restores_the_original_sha_and_repeats_as_a_no_op(self):
        self.install("--yes")
        self.run_collector("uninstall")
        self.assertEqual(
            hashlib.sha256(self.settings.read_bytes()).hexdigest(),
            hashlib.sha256(self.original).hexdigest(),
        )
        self.assertEqual(
            self.systemctl_log()[2:],
            ["--user disable --now devdiary-collector-sync.timer", "--user daemon-reload"],
        )
        self.assertFalse((self.config / "systemd/user/devdiary-collector-sync.timer").exists())
        log = self.systemctl_log()
        self.run_collector("uninstall")
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(self.systemctl_log(), log)

    def test_doctor_reports_registration_timer_and_drops(self):
        self.install("--yes")
        doctor = json.loads(self.run_collector("doctor").stdout)
        self.assertEqual(doctor["registration"]["registration"], "installed")
        self.assertEqual(doctor["timer"], {"enabled": "enabled", "active": "active"})
        self.assertEqual(doctor["drops"], {})
        self.assertIsNone(doctor["last_receipt"])
        self.assertIsNone(doctor["last_sync_error"])

    def test_upgrade_replaces_the_registration_with_a_fresh_plan(self):
        self.install("--yes")
        before = self.owned_entries()["Stop"][0]["hooks"][0]["command"]
        self.install("--yes", code=2)
        out = self.run_collector("upgrade", "--yes").stdout.decode()
        self.assertIn("Self-test passed", out)
        entries = self.owned_entries()
        self.assertEqual(sum(len(v) for v in entries.values()), 9)
        self.assertNotEqual(entries["Stop"][0]["hooks"][0]["command"], before)
        self.assertEqual(json.loads(self.settings.read_bytes())["hooks"]["UserPromptSubmit"][0], CODEGRAPH)
        self.assertEqual(
            self.systemctl_log()[-1], "--user enable --now devdiary-collector-sync.timer"
        )
        self.run_collector("uninstall")
        self.assertEqual(self.settings.read_bytes(), self.original)

    def test_without_a_tty_or_yes_install_refuses_before_pairing(self):
        result = self.install(code=2)
        self.assertIn(b"consent_required", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.systemctl_log(), [])

    def test_invalid_settings_json_refuses(self):
        self.settings.write_bytes(b'{"hooks": ')
        result = self.install("--yes", code=2)
        self.assertIn(b"invalid_settings", result.stderr)
        self.assertEqual(self.settings.read_bytes(), b'{"hooks": ')
        self.assertFalse((self.data / "connection").exists())
        self.assertEqual(self.systemctl_log(), [])

    def interactive(self, answer, edit=None):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen(
            self.install_args(),
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
        )
        os.close(slave)
        seen = b""
        deadline = time.monotonic() + 30
        while b"[y/N]" not in seen and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.5)
            if ready:
                seen += os.read(process.stdout.fileno(), 4096)
        self.assertIn(b"[y/N]", seen)
        self.assertEqual(self.systemctl_log(), [])
        self.assertEqual(self.settings.read_bytes(), self.original)
        if edit is not None:
            self.settings.write_bytes(edit)
        os.write(master, answer)
        _, err = process.communicate(timeout=30)
        return process.returncode, err

    def test_declining_consent_changes_nothing_and_never_enables(self):
        code, err = self.interactive(b"n\n")
        self.assertEqual(code, 2)
        self.assertIn(b"consent_declined", err)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(self.systemctl_log(), [])

    def test_settings_edited_between_plan_and_apply_give_stale_plan(self):
        edited = self.original.replace(b'"opus"', b'"sonnet"')
        code, err = self.interactive(b"y\n", edit=edited)
        self.assertEqual(code, 2)
        self.assertIn(b"stale_plan", err)
        self.assertEqual(self.settings.read_bytes(), edited)
        self.assertEqual(self.systemctl_log(), [])

    def test_consent_on_a_tty_installs_then_enables(self):
        code, err = self.interactive(b"y\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(sum(len(v) for v in self.owned_entries().values()), 9)
        self.assertEqual(
            self.systemctl_log()[-1], "--user enable --now devdiary-collector-sync.timer"
        )


if __name__ == "__main__":
    unittest.main()
