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
import uuid
from contextlib import closing
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
PAPERCLIP = {
    "key": "paperclip",
    "env": {
        "agent_ref": "PAPERCLIP_AGENT_ID",
        "run_ref": "PAPERCLIP_RUN_ID",
        "task_ref": "PAPERCLIP_TASK_ID",
        "checkout_path": [
            "PAPERCLIP_WORKSPACE_WORKTREE_PATH",
            "PAPERCLIP_WORKSPACE_CWD",
        ],
    },
}
CODEGRAPH = {"hooks": [{"type": "command", "command": "codegraph hook prompt"}]}
FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/systemctl.log"
case "$*" in
  *is-enabled*) echo enabled ;;
  *is-active*) echo active ;;
esac
"""
NO_USER_BUS = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/systemctl.log"
exit 1
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
        self.observation_status = 201
        self.orchestrators = None
        self.scope_status = 200
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
                if owner.scope_status != 200:
                    return self.reply(owner.scope_status, {"error": "response-secret"})
                reply = {
                    "schema_version": 1,
                    "collector_ref": COLLECTOR_REF,
                    "scope_kind": "installation",
                    "repository_refs": ["https://github.com/acme/demo"],
                    "observation_schema_versions": [1, 2],
                }
                if owner.orchestrators is not None:
                    reply["orchestrators"] = owner.orchestrators
                self.reply(200, reply)

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
                if owner.observation_status != 201:
                    return self.reply(owner.observation_status, {"error": "refused"})
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
            "--trust-origin",
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

    def record(self):
        return json.loads((self.data / "install.json").read_text())

    def spool(self, count):
        repo = self.root / "demo"
        if not repo.exists():
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "remote",
                    "add",
                    "origin",
                    "https://github.com/acme/demo.git",
                ],
                check=True,
            )
        record = self.record()

        def hook(_):
            event = {
                "hook_event_name": "SessionStart",
                "session_id": str(uuid.uuid4()),
                "cwd": str(repo),
            }
            subprocess.run(
                [record["executable"], "claude-hook", record["plan"]],
                input=json.dumps(event).encode(),
                env=self.env(),
                timeout=10,
                check=True,
            )

        for i in range(count):
            hook(i)
        self.assertEqual(self.pending(), count)

    def pending(self):
        return self.count("outbox")

    def count(self, table):
        outbox = self.data / "connection/outbox/collector-rust-v1.sqlite3"
        with closing(sqlite3.connect(outbox)) as db:
            return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def rebuilt(self):
        path = self.root / "rebuild" / BIN.name
        path.parent.mkdir()
        path.write_bytes(BIN.read_bytes() + b"rebuilt")
        path.chmod(0o700)
        return path

    def pinned(self):
        return (self.data / VERSION / BIN.name).read_bytes()

    def revoke(self):
        self.spool(1)
        self.observation_status = 401
        self.assertTrue(self.timer_sync()["revoked"])
        self.observation_status = 201

    def timer_sync(self):
        self.run_collector(
            "timer-sync", self.data / "connection", exe=self.record()["executable"]
        )
        return json.loads(self.run_collector("doctor").stdout)

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
            [
                "--user daemon-reload",
                "--user enable --now devdiary-collector-sync.timer",
            ],
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
        self.assertEqual(
            (units / "devdiary-collector-sync.timer").read_text(),
            "[Unit]\nDescription=DevDiary collector sync every 2 minutes\n\n[Timer]\n"
            "OnActiveSec=30s\nOnUnitActiveSec=2min\nRandomizedDelaySec=30\nPersistent=true\n"
            "Unit=devdiary-collector-sync.service\n\n[Install]\nWantedBy=timers.target\n",
        )

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
            [
                "--user disable --now devdiary-collector-sync.timer",
                "--user daemon-reload",
            ],
        )
        self.assertFalse(
            (self.config / "systemd/user/devdiary-collector-sync.timer").exists()
        )
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
        self.assertEqual(
            json.loads(self.settings.read_bytes())["hooks"]["UserPromptSubmit"][0],
            CODEGRAPH,
        )
        self.assertEqual(
            self.systemctl_log()[-1],
            "--user enable --now devdiary-collector-sync.timer",
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

    def test_uninstall_without_a_user_bus_still_removes_the_hooks(self):
        self.fake.write_text(NO_USER_BUS)
        self.assertIn(b"timer_unavailable", self.install("--yes", code=2).stderr)
        result = self.run_collector("uninstall", code=2)
        self.assertIn(b"timer_unconfirmed", result.stderr)
        self.assertNotIn(b"Hooks are registered", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse(
            (self.config / "systemd/user/devdiary-collector-sync.timer").exists()
        )
        self.assertFalse((self.data / "install.json").exists())
        self.assertIn(b"Not installed", self.run_collector("uninstall").stdout)

    def test_upgrade_refused_by_the_settings_changes_nothing(self):
        self.install("--yes")
        doc = json.loads(self.settings.read_bytes())
        doc["env"] = {"X": "observer_hook"}
        edited = (json.dumps(doc, indent=2) + "\n").encode()
        self.settings.write_bytes(edited)
        log = self.systemctl_log()
        result = self.run_collector("upgrade", "--yes", code=2)
        self.assertIn(b"python_observer_registered", result.stderr)
        self.assertEqual(self.settings.read_bytes(), edited)
        self.assertEqual(self.systemctl_log(), log)
        self.assertTrue((self.data / "install.json").exists())

    def test_upgrade_failing_after_removal_can_still_be_uninstalled(self):
        self.install("--yes")
        (self.data / "connection/connection.json").write_bytes(b"{}")
        self.run_collector("upgrade", "--yes", code=2)
        self.assertEqual(sum(len(v) for v in self.owned_entries().values()), 0)
        self.assertTrue(json.loads(self.run_collector("doctor").stdout)["installed"])
        self.run_collector("uninstall")
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertIn(
            "--user disable --now devdiary-collector-sync.timer", self.systemctl_log()
        )
        self.assertFalse(
            (self.config / "systemd/user/devdiary-collector-sync.timer").exists()
        )

    def test_uninstall_without_a_record_still_disables_leftover_units(self):
        self.install("--yes")
        (self.data / "install.json").unlink()
        self.run_collector("uninstall")
        self.assertIn(
            "--user disable --now devdiary-collector-sync.timer", self.systemctl_log()
        )
        self.assertFalse(
            (self.config / "systemd/user/devdiary-collector-sync.timer").exists()
        )
        self.assertFalse(
            (self.config / "systemd/user/devdiary-collector-sync.service").exists()
        )

    def test_install_without_a_settings_file_says_so(self):
        self.settings.unlink()
        result = self.install("--yes", code=2)
        self.assertIn(b"settings_missing", result.stderr)
        self.assertNotIn(b"Setup incomplete", result.stderr)
        self.assertFalse(self.data.exists())

    def test_install_refuses_a_relative_settings_path(self):
        result = self.install("--yes", "--settings", "settings.json", code=2)
        self.assertIn(b"unsupported_path", result.stderr)
        self.assertNotIn(b"Setup incomplete", result.stderr)
        self.assertFalse(self.data.exists())

    def test_group_writable_settings_refuse_before_pairing(self):
        self.settings.chmod(0o664)
        result = self.install("--yes", code=2)
        self.assertIn(b"unsafe_settings", result.stderr)
        self.assertFalse(self.data.exists())

    def test_python_observer_settings_refuse_before_pairing(self):
        self.settings.write_bytes(
            self.original.replace(b"codegraph hook prompt", b"observer_hook")
        )
        result = self.install("--yes", code=2)
        self.assertIn(b"python_observer_registered", result.stderr)
        self.assertFalse(self.data.exists())

    def test_timer_sync_delivers_pending_rows_but_never_the_self_test(self):
        self.install("--yes")
        self.spool(3)
        doctor = self.timer_sync()
        self.assertEqual(len(self.received), 3)
        self.assertEqual(doctor["pending"], 0)
        self.assertIsNone(doctor["last_sync_error"])
        self.assertFalse(any("selftest" in json.dumps(r) for r in self.received))

    def test_timer_sync_sends_each_rejected_row_once_per_run(self):
        self.install("--yes")
        self.spool(120)
        self.observation_status = 422
        doctor = self.timer_sync()
        self.assertEqual(len(self.received), 120)
        self.assertEqual(doctor["pending"], 120)
        self.assertEqual(doctor["last_sync_error"], "http_422")

    def test_timer_sync_stops_on_the_first_server_error(self):
        self.install("--yes")
        self.spool(5)
        self.observation_status = 503
        doctor = self.timer_sync()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(doctor["last_sync_error"], "http_503")

    def test_timer_sync_stops_quietly_on_revocation(self):
        self.install("--yes")
        self.spool(5)
        self.observation_status = 401
        doctor = self.timer_sync()
        self.assertEqual(len(self.received), 1)
        self.assertTrue(doctor["revoked"])
        self.timer_sync()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(
            json.loads(self.run_collector("doctor").stdout)["last_sync_error"],
            "revoked",
        )

    def test_a_hook_event_with_the_self_test_prefix_never_reaches_the_outbox(self):
        self.install("--yes")
        self.spool(1)
        record = self.record()
        event = {
            "hook_event_name": "SessionStart",
            "session_id": "devdiary-selftest-x",
            "cwd": str(self.root / "demo"),
        }
        subprocess.run(
            [record["executable"], "claude-hook", record["plan"]],
            input=json.dumps(event).encode(),
            env=self.env(),
            timeout=10,
            check=True,
        )
        self.assertEqual((self.pending(), self.count("self_test")), (1, 2))

    def test_uninstall_restores_a_compact_settings_file_byte_for_byte(self):
        compact = json.dumps(
            {"model": "opus", "hooks": {"UserPromptSubmit": [CODEGRAPH]}},
            separators=(",", ":"),
        ).encode()
        self.settings.write_bytes(compact)
        self.install("--yes")
        self.run_collector("uninstall")
        self.assertEqual(self.settings.read_bytes(), compact)

    def interactive(self, answer, edit=None, args=None):
        log, before = self.systemctl_log(), self.settings.read_bytes()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen(
            args or self.install_args(),
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
        self.seen = seen
        self.assertEqual(self.systemctl_log(), log)
        self.assertEqual(self.settings.read_bytes(), before)
        if edit is not None:
            self.settings.write_bytes(edit)
        os.write(master, answer)
        _, err = process.communicate(timeout=30)
        return process.returncode, err

    def converse(self, args, answers):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen(
            args,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env(),
        )
        os.close(slave)
        seen = b""
        deadline = time.monotonic() + 30
        for prompt, answer in enumerate(answers, start=1):
            while seen.count(b"[y/N]") < prompt and time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if ready:
                    seen += os.read(process.stdout.fileno(), 4096)
            self.assertEqual(seen.count(b"[y/N]"), prompt, seen)
            os.write(master, answer)
        out, err = process.communicate(timeout=30)
        return process.returncode, seen + out, err

    def approved(self):
        path = self.data / "connection/outbox/orchestrators-approved.json"
        return json.loads(path.read_text()) if path.exists() else None

    def waiting(self):
        return json.loads(self.run_collector("doctor").stdout)["orchestrators"]

    def test_scope_compatibility_upgrade_reports_refresh_refusal(self):
        self.install("--yes")
        outbox = self.data / "connection/outbox"
        refs = (outbox / "repository-refs.json").read_bytes()
        offer = outbox / "orchestrators-offered.json"
        before_offer = offer.read_bytes() if offer.exists() else None
        self.scope_status = 503
        result = self.run_collector("upgrade", "--yes", code=2)
        self.assertIn(b"server refused scope refresh", result.stderr)
        self.assertIn(b"retry", result.stderr)
        self.assertNotIn(b"response-secret", result.stdout + result.stderr)
        self.assertEqual((outbox / "repository-refs.json").read_bytes(), refs)
        self.assertEqual(offer.read_bytes() if offer.exists() else None, before_offer)
        self.assertIsNone(self.approved())

    def test_install_shows_the_names_and_stores_them_only_on_yes(self):
        self.orchestrators = [PAPERCLIP]
        code, out, err = self.converse(self.install_args(), [b"y\n", b"y\n"])
        self.assertEqual(code, 0, err)
        for name in [*PAPERCLIP["env"].values()][:3]:
            self.assertIn(name.encode(), out)
        self.assertIn(
            b"refs: PAPERCLIP_AGENT_ID, PAPERCLIP_RUN_ID, PAPERCLIP_TASK_ID", out
        )
        self.assertIn(
            b"checkout paths (used here only): PAPERCLIP_WORKSPACE_WORKTREE_PATH, "
            b"PAPERCLIP_WORKSPACE_CWD",
            out,
        )
        self.assertNotIn(b"added ", out)
        self.assertEqual(self.approved(), [PAPERCLIP])
        self.assertFalse(self.waiting()["waiting_for_approval"])
        self.assertIsNone(self.waiting()["notice"])

    def test_declining_the_names_keeps_the_hooks_and_leaves_the_change_waiting(self):
        self.orchestrators = [PAPERCLIP]
        code, _, err = self.converse(self.install_args(), [b"y\n", b"n\n"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sum(len(v) for v in self.owned_entries().values()), 9)
        self.assertIsNone(self.approved())
        self.assertTrue(self.waiting()["waiting_for_approval"])

    def test_yes_never_approves_names_without_a_terminal(self):
        self.orchestrators = [PAPERCLIP]
        out = self.install("--yes").stdout
        self.assertIn(b"orchestrators approve", out)
        self.assertIsNone(self.approved())
        self.assertEqual(sum(len(v) for v in self.owned_entries().values()), 9)
        self.assertTrue(self.waiting()["waiting_for_approval"])

    def test_install_with_nothing_offered_asks_nothing_about_names(self):
        self.orchestrators = []
        out = self.install("--yes").stdout
        self.assertNotIn(b"orchestrators approve", out)
        self.assertFalse(self.waiting()["waiting_for_approval"])

    def test_orchestrators_approve_without_a_terminal_refuses(self):
        self.orchestrators = [PAPERCLIP]
        self.install("--yes")
        result = self.run_collector("orchestrators", "approve", code=2)
        self.assertIn(b"approval_required", result.stderr)
        self.assertIn(b"terminal", result.stderr)
        self.assertIsNone(self.approved())

    def test_orchestrators_approve_has_no_unattended_flag(self):
        self.orchestrators = [PAPERCLIP]
        self.install("--yes")
        self.run_collector("orchestrators", "approve", "--yes", code=2)
        self.assertIsNone(self.approved())

    def test_orchestrators_approve_on_a_terminal_shows_and_saves_the_offer(self):
        self.orchestrators = [PAPERCLIP]
        self.install("--yes")
        args = [str(BIN), "orchestrators", "approve"]
        code, out, err = self.converse(args, [b"y\n"])
        self.assertEqual(code, 0, err)
        self.assertIn(b"PAPERCLIP_TASK_ID", out)
        self.assertEqual(self.approved(), [PAPERCLIP])

    def test_declining_orchestrators_approve_keeps_the_old_list(self):
        self.orchestrators = [PAPERCLIP]
        self.converse(self.install_args(), [b"y\n", b"y\n"])
        self.orchestrators = [{"key": "cron", "env": {"run_ref": "CRON_RUN"}}]
        self.timer_sync()
        self.assertTrue(self.waiting()["waiting_for_approval"])
        args = [str(BIN), "orchestrators", "approve"]
        code, out, err = self.converse(args, [b"n\n"])
        self.assertEqual(code, 2)
        self.assertIn(b"added cron run_ref CRON_RUN", out)
        self.assertIn(b"removed paperclip agent_ref PAPERCLIP_AGENT_ID", out)
        self.assertIn(b"consent_declined", err)
        self.assertEqual(self.approved(), [PAPERCLIP])

    def test_doctor_warns_that_unapproved_runs_may_look_interactive(self):
        self.orchestrators = [PAPERCLIP]
        self.install("--yes")
        result = self.run_collector("doctor")
        self.assertIn(b"stay unknown until approved", result.stderr)
        self.assertIn("paperclip", json.loads(result.stdout)["orchestrators"]["notice"])

    def test_doctor_says_nothing_when_no_name_is_offered(self):
        self.install("--yes")
        result = self.run_collector("doctor")
        self.assertNotIn(b"until approved", result.stderr)
        self.assertIsNone(json.loads(result.stdout)["orchestrators"]["notice"])

    def spoil_offer(self):
        offered = self.data / "connection/outbox/orchestrators-offered.json"
        offered.write_text("not json")
        offered.chmod(0o600)

    def test_a_bad_offered_file_stops_neither_doctor_nor_upgrade(self):
        self.install("--yes")
        self.spoil_offer()
        self.assertTrue(self.waiting()["offered_list_unreadable"])
        out = self.run_collector("upgrade", "--yes").stdout
        self.assertIn(b"Self-test passed", out)
        self.assertIn(b"next sync replaces it", out)

    def test_orchestrators_approve_names_a_bad_offered_file(self):
        self.install("--yes")
        self.spoil_offer()
        args = [str(BIN), "orchestrators", "approve"]
        code, _, err = self.converse(args, [])
        self.assertEqual(code, 2)
        self.assertIn(b"offered_list_unreadable", err)
        self.assertIn(b"next sync replaces it", err)

    def test_orchestrators_approve_with_nothing_waiting_asks_nothing(self):
        self.install("--yes")
        args = [str(BIN), "orchestrators", "approve"]
        code, out, err = self.converse(args, [])
        self.assertEqual(code, 0, err)
        self.assertIn(b"No orchestrator change waits", out)

    def test_upgrade_offers_the_names_a_first_install_never_saw(self):
        self.install("--yes")
        self.orchestrators = [PAPERCLIP]
        code, out, err = self.converse([str(BIN), "upgrade"], [b"y\n", b"y\n"])
        self.assertEqual(code, 0, err)
        self.assertIn(b"PAPERCLIP_RUN_ID", out)
        self.assertEqual(self.approved(), [PAPERCLIP])

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
            self.systemctl_log()[-1],
            "--user enable --now devdiary-collector-sync.timer",
        )

    def test_upgrade_shows_the_new_entries_before_asking(self):
        self.install("--yes")
        before = self.settings.read_bytes()
        log = self.systemctl_log()
        code, err = self.interactive(b"n\n", args=[str(BIN), "upgrade"])
        self.assertEqual(code, 2)
        self.assertIn(b"consent_declined", err)
        self.assertEqual(self.seen.count(b" claude-hook "), 9)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.systemctl_log(), log)

    def test_declining_an_upgrade_from_a_rebuild_keeps_capture_working(self):
        self.install("--yes")
        rebuilt = self.rebuilt()
        code, err = self.interactive(b"n\n", args=[str(rebuilt), "upgrade"])
        self.assertEqual(code, 2)
        self.assertIn(b"consent_declined", err)
        self.assertEqual(self.pinned(), BIN.read_bytes())
        self.spool(1)
        self.assertEqual(json.loads(self.run_collector("doctor").stdout)["drops"], {})

    def test_upgrade_from_a_rebuild_pins_and_registers_the_new_build(self):
        self.install("--yes")
        rebuilt = self.rebuilt()
        self.run_collector("upgrade", "--yes", exe=rebuilt)
        self.assertEqual(self.pinned(), rebuilt.read_bytes())
        self.spool(1)

    def test_install_refuses_a_revoked_connection_before_registering(self):
        self.install("--yes")
        self.revoke()
        self.run_collector("uninstall")
        log = self.systemctl_log()
        result = self.install("--yes", code=2)
        self.assertIn(b"connection_revoked", result.stderr)
        self.assertIn(b"install --new-pair", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse((self.data / "install.json").exists())
        self.assertEqual(self.systemctl_log(), log)

    def test_install_new_pair_sets_a_revoked_connection_aside_and_pairs_again(self):
        self.install("--yes")
        self.revoke()
        self.run_collector("uninstall")
        self.install("--yes", "--new-pair")
        doctor = json.loads(self.run_collector("doctor").stdout)
        self.assertFalse(doctor["revoked"])
        self.assertEqual(doctor["registration"]["registration"], "installed")
        retired = [p for p in self.data.iterdir() if p.name.startswith("connection-")]
        self.assertEqual(len(retired), 1)
        self.assertTrue((retired[0] / "revoked").exists())
        self.spool(1)

    def test_install_new_pair_refuses_a_connection_that_still_works(self):
        self.install("--yes")
        self.run_collector("uninstall")
        result = self.install("--yes", "--new-pair", code=2)
        self.assertIn(b"connection_active", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)

    def test_upgrade_refuses_a_revoked_connection(self):
        self.install("--yes")
        self.revoke()
        before, log = self.settings.read_bytes(), self.systemctl_log()
        result = self.run_collector("upgrade", "--yes", code=2)
        self.assertIn(b"connection_revoked", result.stderr)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.systemctl_log(), log)

    def test_another_origin_needs_trust_origin(self):
        args = [a for a in self.install_args("--yes") if a != "--trust-origin"]
        result = self.run_collector(*args[1:], code=2)
        self.assertIn(b"--trust-origin", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse((self.data / "connection").exists())

    def test_the_binary_and_connection_live_under_home_whatever_xdg_data_home_says(
        self,
    ):
        elsewhere = self.root / "xdg-data"
        env = {**self.env(), "XDG_DATA_HOME": str(elsewhere)}
        subprocess.run(
            self.install_args("--yes"),
            env=env,
            check=True,
            capture_output=True,
            timeout=40,
        )
        self.assertFalse(elsewhere.exists())
        self.assertTrue((self.data / "install.json").exists())

    def test_install_output_names_no_manual_registration_steps(self):
        out = self.install("--yes").stdout
        for manual in [b"claude-apply", b"setup-plan", b"NEW_PLAN"]:
            self.assertNotIn(manual, out)

    def test_install_persists_the_outside_repos_choice(self):
        self.install("--yes", "--record-orchestrated-outside-repos")
        connection = json.loads((self.data / "connection/connection.json").read_text())
        self.assertTrue(connection["scope"]["record_orchestrated_outside_repos"])

    def test_doctor_shows_the_last_receipt_after_a_timer_sync(self):
        self.install("--yes")
        self.spool(1)
        self.assertIsNotNone(self.timer_sync()["last_receipt"])


if __name__ == "__main__":
    unittest.main()
