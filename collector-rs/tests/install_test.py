"""One-command install against a mock pairing/ingest server, a temp HOME and a fake
scheduler: systemctl on Linux, launchctl on macOS."""

import calendar
import fcntl
import hashlib
import json
import os
import plistlib
import pty
import re
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tomllib
import unittest
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import isolation
import pairing_server
import terminal

CRATE = Path(__file__).resolve().parents[1]
BIN = CRATE / "target/debug/devdiary"
VERSION = tomllib.loads((CRATE / "Cargo.toml").read_text())["package"]["version"]
EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
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
DARWIN = sys.platform == "darwin"
UID = os.getuid()
FAKE_SYSTEMCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/scheduler.log"
case "$*" in
  *is-enabled*) echo enabled ;;
  *is-active*) echo active ;;
esac
"""
# Remembers which agents are loaded. Bootout of an agent that is not loaded
# fails, as launchctl's does.
FAKE_LAUNCHCTL = """#!/bin/sh
dir="$(dirname "$0")"
printf '%s\\n' "$*" >> "$dir/scheduler.log"
case "$1" in
  print)
    case "$2" in
      gui/*/*) test -e "$dir/loaded-${2##*/}" ;;
    esac ;;
  bootstrap) : > "$dir/loaded-$(basename "$3" .plist)" ;;
  bootout) rm "$dir/loaded-${2##*/}" 2>/dev/null || exit 3 ;;
esac
"""
# No user manager on Linux; no GUI login domain on macOS.
UNREACHABLE = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/scheduler.log"
exit 1
"""
# An agent that stays loaded whatever bootout says.
STUCK_LAUNCHCTL = """#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/scheduler.log"
test "$1" != bootout
"""


class FakeScheduler:
    """The fake systemctl or launchctl, and where its job definitions go."""

    PROBES = ("print",) if DARWIN else ("--user show-environment", "--user is-")

    def __init__(self, root, home, config):
        self.fake = root / "bin" / ("launchctl" if DARWIN else "systemctl")
        self.fake.parent.mkdir()
        self.write(FAKE_LAUNCHCTL if DARWIN else FAKE_SYSTEMCTL)
        self.dir = home / "Library/LaunchAgents" if DARWIN else config / "systemd/user"

    def write(self, script):
        self.fake.write_text(script)
        self.fake.chmod(0o700)

    def env(self):
        name = "LAUNCHCTL" if DARWIN else "SYSTEMCTL"
        return {f"DEVDIARY_COLLECTOR_{name}": str(self.fake)}

    def log(self):
        log = self.fake.parent / "scheduler.log"
        return log.read_text().splitlines() if log.exists() else []

    def changes(self):
        """Every call except the read-only probes."""
        return [line for line in self.log() if not line.startswith(self.PROBES)]

    def files(self, vendor="claude"):
        if DARWIN:
            return [self.dir / f"me.devdiary.{vendor}-sync.plist"]
        return [
            self.dir / f"devdiary-{vendor}-sync.service",
            self.dir / f"devdiary-{vendor}-sync.timer",
        ]

    def defined(self, vendor="claude"):
        return any(path.exists() for path in self.files(vendor))

    def started(self, vendor="claude"):
        if DARWIN:
            return [f"bootstrap gui/{UID} {self.files(vendor)[0]}"]
        return [
            "--user daemon-reload",
            f"--user enable --now devdiary-{vendor}-sync.timer",
        ]

    def restarted(self, vendor="claude"):
        if DARWIN:
            return [
                f"bootout gui/{UID}/me.devdiary.{vendor}-sync",
                *self.started(vendor),
            ]
        return self.started(vendor)

    def stopped(self, vendor="claude"):
        if DARWIN:
            return [f"bootout gui/{UID}/me.devdiary.{vendor}-sync"]
        return [
            f"--user disable --now devdiary-{vendor}-sync.timer",
            "--user daemon-reload",
        ]

    def running(self):
        if DARWIN:
            return {"loaded": True}
        return {"enabled": "enabled", "active": "active"}


class InstallTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="install-")
        self.addCleanup(tmp.cleanup)
        # The symlink-ancestor rule refuses macOS's /var symlink.
        self.root = Path(tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.config = self.root / "config"
        self.config.mkdir()
        self.scheduler = FakeScheduler(self.root, self.home, self.config)
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
        self.data = self.home / ".local/share/devdiary/cli/claude"
        self.codex_data = self.home / ".local/share/devdiary/cli/codex"
        self.received_raw = []
        self.received = []
        # The connection token each received observation carried.
        self.received_tokens = []
        self.observation_status = 201
        self.orchestrators = None
        self.scope_status = 200
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
                if contract.handle(self):
                    return
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                data = json.loads(raw)
                authorization = self.headers["Authorization"]
                owner.received_raw.append(raw)
                owner.received.append(data)
                owner.received_tokens.append(pairing_server.bearer(authorization))
                if contract.refuses(authorization):
                    return self.reply(401, {"error": "unauthorized"})
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

            def do_DELETE(self):
                if not contract.handle(self):
                    self.reply(404, {"error": "not_found"})

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
            for k, v in isolation.inherited(terminal.local_browser(os.environ)).items()
            if not k.startswith(("PAPERCLIP_", "XDG_"))
        }
        return {
            **base,
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            # Setup's wait for the first session ends at once unless a test sets it.
            "DEVDIARY_DEBUG_FIRST_SESSION_WAIT": "0",
            **self.scheduler.env(),
        }

    def install_args(self, *extra):
        return [
            str(BIN),
            "setup",
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

    def install(self, *extra, code=0, answers=(b"y\n",), env=None):
        args = self.install_args(*extra)
        status, out, err, _ = terminal.answering(args, env or self.env(), answers)
        self.assertEqual(status, code, err)
        self.assertNotIn(b"dc_live_", out + err)
        return subprocess.CompletedProcess(args, status, out, err)

    def test_codex_uses_its_active_configuration_home(self):
        custom = self.home / "control-plane-codex"
        env = {**self.env(), "CODEX_HOME": str(custom)}
        self.install("--vendor", "codex", env=env, answers=(b"y\n", b"y\n"))
        self.assertTrue((custom / "hooks.json").is_file())
        self.assertFalse((self.home / ".codex/hooks.json").exists())

    def test_codex_can_add_hooks_to_an_existing_owner_writable_config_directory(self):
        directory = self.home / ".codex"
        directory.mkdir(mode=0o755)
        (directory / "config.toml").write_text('model = "fixture"\n')
        self.install("--vendor", "codex", answers=(b"y\n", b"y\n"))
        self.assertTrue((directory / "hooks.json").is_file())
        self.assertEqual((directory / "config.toml").read_text(), 'model = "fixture"\n')

    def test_codex_creation_preserves_a_file_added_during_consent(self):
        self.settings = self.home / ".codex/hooks.json"
        self.settings.parent.mkdir(mode=0o700)
        added = b'{"hooks":{"Stop":[]},"description":"concurrent-owner-edit"}'
        code, _ = self.interactive(
            b"y\nn\n", edit=added, args=self.install_args("--vendor", "codex")
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.settings.read_bytes(), added)

    def test_declining_codex_setup_does_not_create_hooks_file(self):
        code, _, err = self.converse(self.install_args("--vendor", "codex"), [b"n\n"])
        self.assertEqual(code, 2, err)
        self.assertFalse((self.home / ".codex/hooks.json").exists())
        self.assertEqual(self.scheduler.changes(), [])

    def test_codex_installs_alongside_claude_without_replacing_it(self):
        self.install()
        claude_settings = self.settings.read_bytes()
        claude_service = self.scheduler.files("claude")[0]
        original_service = claude_service.read_bytes()
        settings = self.home / ".codex/hooks.json"
        settings.parent.mkdir()
        settings.write_text(
            '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"keep"}]}]}}'
        )
        self.install("--vendor", "codex", answers=(b"y\n", b"y\n"))
        self.assertEqual(self.contract.starts()[-1]["harnesses"], ["codex"])
        self.assertEqual(self.settings.read_bytes(), claude_settings)
        self.assertEqual(claude_service.read_bytes(), original_service)
        hooks = json.loads(settings.read_bytes())["hooks"]
        self.assertIn("Interrupt", hooks)
        self.assertIn("PermissionRequest", hooks)
        self.assertNotIn("PostToolUseFailure", hooks)
        self.assertIn(" hook codex ", hooks["Interrupt"][0]["hooks"][0]["command"])
        self.assertTrue((self.codex_data / "install.json").is_file())
        self.assertTrue(self.scheduler.defined("codex"))

    def owned_entries(self):
        doc = json.loads(self.settings.read_bytes())
        return {
            event: [e for e in doc["hooks"].get(event, []) if e != CODEGRAPH]
            for event in EVENTS
        }

    def record(self):
        return json.loads((self.data / "install.json").read_text())

    def demo(self):
        """A repository whose remote is the tracked one."""
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
        return repo

    def spool(self, count, vendor="claude"):
        repo = self.demo()
        record = self.record()

        def hook(_):
            event = {
                "hook_event_name": "SessionStart",
                "session_id": str(uuid.uuid4()),
                "cwd": str(repo),
            }
            subprocess.run(
                [record["executable"], "hook", vendor, record["plan"]],
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
            "sync", self.data / "connection", exe=self.record()["executable"]
        )
        return json.loads(self.run_collector("doctor", "--json").stdout)

    def test_install_yes_registers_nine_entries_beside_codegraph(self):
        out = self.install().stdout.decode()
        doc = json.loads(self.settings.read_bytes())
        self.assertEqual(doc["hooks"]["UserPromptSubmit"][0], CODEGRAPH)
        entries = self.owned_entries()
        self.assertEqual(sum(len(v) for v in entries.values()), len(EVENTS))
        exe = self.data / VERSION / "devdiary"
        for (entry,) in entries.values():
            self.assertIn(f"'{exe}' hook claude ", entry["hooks"][0]["command"])
        self.assertEqual(exe.stat().st_mode & 0o777, 0o700)
        self.assertIn("Claude Code   adds 10 hooks to ~/.claude/settings.json", out)
        self.assertNotIn(" hook claude ", out)
        self.assertIn("Self-test passed", out)
        self.assertEqual(self.scheduler.changes(), self.scheduler.started())

    def test_d_shows_the_exact_entries_then_asks_again(self):
        out = self.install(answers=(b"d\n", b"y\n")).stdout.decode()
        self.assertEqual(out.count("Set up Claude Code? [y/N/d]"), 2)
        shown = out.index(
            "Claude Code hook entries to append to ~/.claude/settings.json:"
        )
        self.assertLess(shown, out.rindex("[y/N/d]"))
        self.assertEqual(out.count(" hook claude "), len(EVENTS))
        commands = [e["hooks"][0]["command"] for (e,) in self.owned_entries().values()]
        for command in commands:
            self.assertIn(command, out)

    def test_a_plain_setup_is_four_numbered_steps_under_the_header(self):
        (self.home / ".codex").mkdir()
        answers = (b"both\n", b"y\n", b"y\n", b"y\n")
        status, out, err, _ = terminal.answering(
            self.install_args(), self.env(), answers
        )
        self.assertEqual(status, 0, err)
        self.assertNotIn(b"\x1b", out + err)
        self.assertNotIn(str(self.home).encode(), out)
        lines = out.decode().splitlines()
        self.assertEqual(lines[0], f"DevDiary {VERSION} · devdiary.me")
        self.assertEqual(
            [line for line in lines if line.startswith("[")],
            [
                "[1/4] Check this machine",
                "[2/4] Choose what DevDiary may change",
                "[3/4] Approve this machine in DevDiary",
                "[4/4] Wait for your first session",
            ],
        )
        for line in [
            "  ✓ Claude Code: ~/.claude/settings.json",
            "  Claude Code   adds 10 hooks to ~/.claude/settings.json",
            "  Approval code: "
            + self.contract.latest.code[:4]
            + "-"
            + self.contract.latest.code[4:],
            "  ✓ Connected to the DevDiary workspace acme-diary.",
            "✓ Claude Code records to the workspace acme-diary",
            "✓ Codex records to the workspace acme-diary",
            "! Codex: trust the DevDiary entries in /hooks",
            "✓ Sync runs every 2 minutes",
            "  Changed on this machine:",
            "  Waiting for your first session · 0:00 of 0:00",
        ]:
            self.assertIn(line, lines)
        self.assertEqual(lines[-2:], ["", "Next: devdiary doctor"])
        for jargon in ["host trust", "qualification", "verifier"]:
            self.assertNotIn(jargon, out.decode())

    def test_a_declined_setup_ends_with_what_happened_next_and_the_code(self):
        result = self.install(code=2, answers=(b"n\n",))
        rows = result.stderr.decode().splitlines()
        self.assertEqual(rows[0], "✗ Nothing was registered.")
        self.assertIn("  Changed on this machine: nothing.", rows)
        self.assertEqual(
            rows[-2:], ["Next: devdiary setup", "error_code: consent_declined"]
        )

    def test_on_a_terminal_setup_is_styled_and_says_the_same_words(self):
        plain = self.install().stdout.decode()
        self.run_collector("uninstall")
        # Pair again, so both runs take the same steps.
        connection = self.data / "connection"
        connection.rename(connection.with_name("set-aside"))
        env = {**self.env(), "TERM": "xterm-256color"}
        env.pop("COLORTERM", None)
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 100, 0, 0))
        process = subprocess.Popen(
            self.install_args(),
            stdin=slave,
            stdout=slave,
            stderr=subprocess.PIPE,
            env=env,
        )
        os.close(slave)
        seen, answered = b"", 0
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.5)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                seen += chunk
            if terminal.questions(seen) > answered:
                os.write(master, b"y\n")
                answered += 1
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, 0)
        styled = seen.decode()
        self.assertIn("\x1b[38;5;36m", styled)
        self.assertIn("╭", styled)

        def words(text):
            text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\r", "", text)
            return re.sub(r"\b[A-Z]{4}-[A-Z]{4}\b|127\.0\.0\.1:\d+", "X", text)

        self.assertIn("Records your coding-agent sessions", words(styled))
        for line in words(plain).splitlines()[1:]:
            if line and "[y/N" not in line:
                self.assertIn(line.strip(), words(styled))

    def test_version_answers_to_the_flag_and_the_command(self):
        for args in (["--version"], ["-V"], ["version"]):
            out = self.run_collector(*args).stdout
            self.assertEqual(out, f"devdiary {VERSION}\n".encode(), args)

    def test_doctor_reads_as_one_line_per_harness(self):
        self.install()
        out = self.run_collector("doctor").stdout.decode()
        lines = out.splitlines()
        self.assertEqual(lines[0], f"DevDiary {VERSION} · devdiary.me")
        self.assertIn(
            "✓ Claude Code records to the workspace acme-diary: 0 waiting to send, 0 sent,"
            + " last sync not yet.",
            lines,
        )
        self.assertIn("Codex: not found on this machine.", out)
        self.assertNotIn("{", out)
        self.assertNotIn("Next:", out)

    def event_from(self, cwd):
        """One SessionStart from a session in `cwd`, as Claude Code sends it."""
        record = self.record()
        event = {
            "hook_event_name": "SessionStart",
            "session_id": str(uuid.uuid4()),
            "cwd": str(cwd),
        }
        subprocess.run(
            [record["executable"], "hook", "claude", record["plan"]],
            input=json.dumps(event).encode(),
            env=self.env(),
            timeout=10,
            check=True,
        )

    def untracked_repository(self):
        repo = self.root / "other"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        remote = "https://github.com/acme/other.git"
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", remote], check=True
        )
        return repo

    def outside_any_repository(self):
        folder = self.root / "notes"
        folder.mkdir()
        return folder

    def test_doctor_explains_drops_when_nothing_was_sent(self):
        self.install()
        self.event_from(self.outside_any_repository())
        repo = self.untracked_repository()
        self.event_from(repo)
        self.event_from(repo)
        report = json.loads(self.run_collector("doctor", "--json").stdout)
        self.assertEqual(report["drops"], {"outside_scope": 1, "unknown_repository": 2})
        self.assertEqual(
            report["drops_by_event"],
            {
                "outside_scope": {"SessionStart": 1},
                "unknown_repository": {"SessionStart": 2},
            },
        )
        self.assertNotIn("workspace", report)
        out = self.run_collector("doctor").stdout.decode()
        flat = " ".join(out.split())
        self.assertIn(
            "! Claude Code records to the workspace acme-diary, but nothing was sent:"
            + " 3 events were dropped.",
            flat,
        )
        self.assertIn(
            "2 SessionStart events came from sessions in a repository this workspace"
            + " doesn't track.",
            flat,
        )
        self.assertIn(
            "1 SessionStart event came from sessions outside the recording scope", flat
        )
        self.assertNotIn("✓ Claude Code", out)
        self.assertEqual(
            out.splitlines()[-1],
            "Next: track the repository in DevDiary, or run devdiary outside-repos on",
        )

    def test_doctor_shows_drops_beside_deliveries_as_a_dim_line(self):
        self.install()
        self.spool(1)
        self.assertEqual(self.timer_sync()["delivered"], 1)
        self.event_from(self.outside_any_repository())
        out = self.run_collector("doctor").stdout.decode()
        lines = out.splitlines()
        (row,) = [i for i, line in enumerate(lines) if line.startswith("✓ Claude Code")]
        self.assertTrue(
            lines[row].startswith(
                "✓ Claude Code records to the workspace acme-diary: 0 waiting to send, 1 sent,"
            ),
            lines[row],
        )
        (dim,) = [i for i, line in enumerate(lines) if "1 event was dropped: " in line]
        (codex,) = [i for i, line in enumerate(lines) if line.startswith("Codex:")]
        self.assertTrue(row < dim < codex, lines)
        self.assertTrue(lines[dim].startswith("  1 event was dropped: "), lines)
        self.assertIn(
            "1 SessionStart event came from sessions outside the recording scope",
            " ".join(out.split()),
        )
        self.assertNotIn("Next:", out)

    def test_doctor_names_the_unreadable_file_where_and_what_next(self):
        self.install()
        self.settings.write_bytes(b'{\n  "model": "opus",\n  oops\n}\n')
        out = self.run_collector("doctor", code=1).stdout.decode()
        self.assertIn(
            "✗ Claude Code: ~/.claude/settings.json is not valid JSON: the error is at line 3,",
            out,
        )
        self.assertEqual(
            out.splitlines()[-1],
            "Next: devdiary doctor, after you fix the JSON in ~/.claude/settings.json",
        )

    def test_setup_refusals_name_the_file_and_what_to_fix(self):
        observer = {"hooks": [{"type": "command", "command": "python observer_hook"}]}
        cases = [
            (
                json.dumps({"hooks": {"Stop": [observer]}}).encode(),
                "python_observer_registered",
                "~/.claude/settings.json registers the Python observer hook",
                "Next: devdiary setup, after you remove the Python observer hook from "
                + "~/.claude/settings.json",
            ),
            (
                b'{\n  "model": "opus",\n  oops\n}\n',
                "invalid_settings",
                "~/.claude/settings.json is not valid JSON: the error is at line 3, column 3.",
                "Next: devdiary setup, after you fix the JSON in ~/.claude/settings.json",
            ),
        ]
        for raw, code, why, next_line in cases:
            with self.subTest(code=code):
                self.settings.write_bytes(raw)
                err = self.install(code=2).stderr.decode()
                self.assertIn(why, err)
                self.assertEqual(
                    err.splitlines()[-2:], [next_line, f"error_code: {code}"]
                )
        self.settings.write_bytes(self.original)
        self.settings.chmod(0o666)
        err = self.install(code=2).stderr.decode()
        self.assertIn(
            "~/.claude/settings.json can be written by group or others (mode 666)", err
        )
        self.assertEqual(err.splitlines()[-1], "error_code: unsafe_settings")

    def test_help_shows_the_overview_and_each_command_its_own(self):
        for args in (["--help"], ["help"], []):
            out = self.run_collector(*args).stdout
            self.assertTrue(
                out.startswith(f"DevDiary {VERSION} · records".encode()), (args, out)
            )
        setup = self.run_collector("setup", "--help").stdout
        self.assertIn(b"Usage:  devdiary setup [options]", setup)
        self.assertIn(b"Examples:", setup)

    @unittest.skipIf(DARWIN, "systemd units are Linux only")
    def test_unit_files_snapshot(self):
        self.install()
        units = self.config / "systemd/user"
        exe = self.data / VERSION / "devdiary"
        self.assertEqual(
            (units / "devdiary-claude-sync.service").read_text(),
            "[Unit]\nDescription=DevDiary collector sync\n\n[Service]\nType=oneshot\n"
            "Nice=10\nNoNewPrivileges=true\n"
            f'ExecStart="{exe}" sync "{self.data / "connection"}"\n',
        )
        self.assertEqual(
            (units / "devdiary-claude-sync.timer").read_text(),
            "[Unit]\nDescription=DevDiary collector sync every 2 minutes\n\n[Timer]\n"
            "OnActiveSec=30s\nOnUnitActiveSec=2min\nRandomizedDelaySec=30\nPersistent=true\n"
            "Unit=devdiary-claude-sync.service\n\n[Install]\nWantedBy=timers.target\n",
        )

    @unittest.skipUnless(DARWIN, "launchd agents are macOS only")
    def test_launch_agent_snapshot(self):
        self.install()
        (agent,) = self.scheduler.files()
        self.assertEqual(agent.parent, self.home / "Library/LaunchAgents")
        info = agent.stat()
        self.assertEqual(info.st_uid, UID)
        self.assertEqual(info.st_mode & 0o022, 0)
        exe = self.data / VERSION / "devdiary"
        self.assertEqual(
            plistlib.loads(agent.read_bytes()),
            {
                "Label": "me.devdiary.claude-sync",
                "ProgramArguments": [str(exe), "sync", str(self.data / "connection")],
                "StartInterval": 120,
                "RunAtLoad": True,
                "ProcessType": "Background",
                "Nice": 10,
                "LowPriorityIO": True,
            },
        )

    @unittest.skipUnless(DARWIN, "launchd agents are macOS only")
    def test_macos_consent_names_the_login_item_before_anything_loads(self):
        code, _ = self.interactive(b"n\n")
        self.assertEqual(code, 2)
        question = self.seen.decode()
        self.assertIn("background-item notification", question)
        self.assertIn("Login Items", question)
        self.assertIn("Switching it off there stops delivery", question)
        self.assertEqual(self.scheduler.changes(), [])

    @unittest.skipUnless(DARWIN, "launchd agents are macOS only")
    def test_an_agent_still_loaded_after_bootout_is_unconfirmed(self):
        self.install()
        self.scheduler.write(STUCK_LAUNCHCTL)
        result = self.run_collector("uninstall", code=2)
        self.assertIn(b"timer_unconfirmed", result.stderr)
        self.assertIn(b"launchctl bootout", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse(self.scheduler.defined())
        self.assertFalse((self.data / "install.json").exists())

    def test_setup_without_a_reachable_scheduler_refuses_before_pairing(self):
        self.scheduler.write(UNREACHABLE)
        result = self.install(code=2)
        self.assertIn(b"scheduler_unreachable", result.stderr)
        self.assertIn(b"Nothing was sent", result.stderr)
        self.assertNotIn(b"Hook entries", result.stdout)
        self.assertEqual(len(self.contract.starts()), 0)
        self.assertFalse(hasattr(self, "installation"))
        self.assertFalse(self.data.exists())
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(self.scheduler.changes(), [])

    def test_doctor_reports_the_last_sync_run_time(self):
        self.install()
        self.assertIsNone(
            json.loads(self.run_collector("doctor", "--json").stdout)["last_sync_at"]
        )
        before = time.time()
        at = self.timer_sync()["last_sync_at"]
        self.assertRegex(at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        seconds = calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))
        self.assertLessEqual(abs(seconds - before), 60)

    def test_self_test_row_is_never_selected_by_sync(self):
        self.install()
        outbox = self.data / "connection/outbox/collector-rust-v1.sqlite3"
        with sqlite3.connect(outbox) as db:
            (selftests,) = db.execute("SELECT count(*) FROM self_test").fetchone()
            (pending,) = db.execute("SELECT count(*) FROM outbox").fetchone()
        self.assertEqual((selftests, pending), (1, 0))
        exe = self.data / VERSION / "devdiary"
        self.run_collector("sync", self.data / "connection", exe=exe)
        self.assertEqual(self.received, [])
        with sqlite3.connect(outbox) as db:
            (selftests,) = db.execute("SELECT count(*) FROM self_test").fetchone()
        self.assertEqual(selftests, 1)

    def test_uninstall_restores_the_original_sha_and_repeats_as_a_no_op(self):
        self.install()
        self.run_collector("uninstall")
        self.assertEqual(
            hashlib.sha256(self.settings.read_bytes()).hexdigest(),
            hashlib.sha256(self.original).hexdigest(),
        )
        self.assertEqual(
            self.scheduler.changes(),
            [*self.scheduler.started(), *self.scheduler.stopped()],
        )
        self.assertFalse(self.scheduler.defined())
        log = self.scheduler.log()
        self.run_collector("uninstall")
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(self.scheduler.log(), log)

    def test_doctor_reports_registration_timer_and_drops(self):
        self.install()
        doctor = json.loads(self.run_collector("doctor", "--json").stdout)
        self.assertEqual(doctor["registration"]["registration"], "installed")
        self.assertEqual(doctor["timer"], self.scheduler.running())
        self.assertEqual(doctor["drops"], {})
        self.assertIsNone(doctor["last_receipt"])
        self.assertIsNone(doctor["last_sync_error"])

    def test_doctor_reports_settings_that_are_not_json_and_runs_the_other_checks(self):
        self.install()
        self.spool(1)
        # The comma after "opus" is missing; "hooks" starts at line 3, column 3.
        broken = b'{\n  "model": "opus"\n  "hooks": {}\n}\n'
        self.settings.write_bytes(broken)
        result = self.run_collector("doctor", "--json", code=1)
        doctor = json.loads(result.stdout)
        self.assertEqual(
            doctor["registration"],
            {
                "registration": "settings_unreadable",
                "settings": str(self.settings),
                "line": 3,
                "column": 3,
                "backup": None,
            },
        )
        self.assertEqual(doctor["timer"], self.scheduler.running())
        self.assertEqual(doctor["pending"], 1)
        self.assertNotIn(b"error_code", result.stderr)
        self.assertIn(f"{self.settings} is not valid JSON".encode(), result.stderr)
        self.assertIn(b"line 3, column 3", result.stderr)
        self.assertEqual(self.settings.read_bytes(), broken)

    def test_doctor_names_the_backup_setup_left_beside_unreadable_settings(self):
        self.install()
        self.settings.write_bytes(b"{")
        self.backup().write_bytes(self.original)
        result = self.run_collector("doctor", "--json", code=1)
        doctor = json.loads(result.stdout)
        self.assertEqual(doctor["registration"]["backup"], str(self.backup()))
        self.assertIn(str(self.backup()).encode(), result.stderr)
        self.assertEqual(self.backup().read_bytes(), self.original)

    def test_upgrade_replaces_the_registration_with_a_fresh_plan(self):
        self.install()
        before = self.owned_entries()["Stop"][0]["hooks"][0]["command"]
        out = self.run_collector("upgrade", "--yes").stdout.decode()
        self.assertIn("Self-test passed", out)
        entries = self.owned_entries()
        self.assertEqual(sum(len(v) for v in entries.values()), len(EVENTS))
        self.assertNotEqual(entries["Stop"][0]["hooks"][0]["command"], before)
        self.assertEqual(
            json.loads(self.settings.read_bytes())["hooks"]["UserPromptSubmit"][0],
            CODEGRAPH,
        )
        restarted = self.scheduler.restarted()
        self.assertEqual(self.scheduler.changes()[-len(restarted) :], restarted)
        self.run_collector("uninstall")
        self.assertEqual(self.settings.read_bytes(), self.original)

    def test_without_a_terminal_setup_refuses_before_pairing(self):
        result = self.run_collector(*self.install_args()[1:], code=2)
        self.assertIn(b"error_code: no_terminal", result.stderr)
        self.assertNotIn(b"--yes", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.scheduler.changes(), [])

    def test_invalid_settings_json_refuses(self):
        self.settings.write_bytes(b'{"hooks": ')
        result = self.install(code=2)
        self.assertIn(b"invalid_settings", result.stderr)
        self.assertEqual(self.settings.read_bytes(), b'{"hooks": ')
        self.assertFalse((self.data / "connection").exists())
        self.assertEqual(self.scheduler.changes(), [])

    @unittest.skipIf(DARWIN, "launchctl reads an absent domain as an unloaded agent")
    def test_uninstall_without_a_user_bus_still_removes_the_hooks(self):
        self.install()
        self.scheduler.write(UNREACHABLE)
        result = self.run_collector("uninstall", code=2)
        self.assertIn(b"timer_unconfirmed", result.stderr)
        self.assertNotIn(b"Hooks are registered", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse(self.scheduler.defined())
        self.assertFalse((self.data / "install.json").exists())
        self.assertIn(b"Not installed", self.run_collector("uninstall").stdout)

    def older_registration(self, event="PermissionRequest"):
        doc = json.loads(self.settings.read_bytes())
        del doc["hooks"][event]
        self.settings.write_text(json.dumps(doc))

    def test_upgrade_takes_over_a_registration_without_a_newer_hook(self):
        self.install()
        self.older_registration()
        self.assertEqual(
            json.loads(self.run_collector("doctor", "--json").stdout)["registration"][
                "registration"
            ],
            "installed",
        )
        self.run_collector("upgrade", "--yes")
        self.assertEqual(
            sum(len(v) for v in self.owned_entries().values()), len(EVENTS)
        )
        self.run_collector("uninstall")
        self.assertEqual(
            json.loads(self.settings.read_bytes()), json.loads(self.original)
        )

    def test_foreign_newer_hook_survives_upgrade_and_uninstall(self):
        self.install()
        doc = json.loads(self.settings.read_bytes())
        foreign = {"hooks": [{"type": "command", "command": "customer permission"}]}
        doc["hooks"]["PermissionRequest"] = [foreign]
        self.settings.write_text(json.dumps(doc))
        self.assertEqual(
            json.loads(self.run_collector("doctor", "--json").stdout)["registration"][
                "registration"
            ],
            "installed",
        )
        self.run_collector("upgrade", "--yes")
        self.assertEqual(
            sum(e != foreign for v in self.owned_entries().values() for e in v),
            len(EVENTS),
        )
        self.assertIn(
            foreign,
            json.loads(self.settings.read_bytes())["hooks"]["PermissionRequest"],
        )
        self.run_collector("uninstall")
        expected = json.loads(self.original)
        expected["hooks"]["PermissionRequest"] = [foreign]
        self.assertEqual(json.loads(self.settings.read_bytes()), expected)

    def test_uninstall_keeps_a_foreign_newer_hook_without_upgrade(self):
        self.install()
        doc = json.loads(self.settings.read_bytes())
        foreign = {"hooks": [{"type": "command", "command": "customer permission"}]}
        doc["hooks"]["PermissionRequest"] = [foreign]
        self.settings.write_text(json.dumps(doc))
        self.run_collector("uninstall")
        expected = json.loads(self.original)
        expected["hooks"]["PermissionRequest"] = [foreign]
        self.assertEqual(json.loads(self.settings.read_bytes()), expected)
        self.assertFalse((self.data / "install.json").exists())

    def test_uninstall_removes_a_registration_without_a_newer_hook(self):
        self.install()
        self.older_registration()
        self.run_collector("uninstall")
        self.assertEqual(sum(len(v) for v in self.owned_entries().values()), 0)

    def test_a_missing_original_hook_still_reads_as_changed(self):
        self.install()
        self.older_registration("Stop")
        self.assertEqual(
            json.loads(self.run_collector("doctor", "--json").stdout)["registration"][
                "registration"
            ],
            "changed",
        )

    def test_upgrade_refused_by_the_settings_changes_nothing(self):
        self.install()
        doc = json.loads(self.settings.read_bytes())
        doc["env"] = {"X": "observer_hook"}
        edited = (json.dumps(doc, indent=2) + "\n").encode()
        self.settings.write_bytes(edited)
        log = self.scheduler.changes()
        result = self.run_collector("upgrade", "--yes", code=2)
        self.assertIn(b"python_observer_registered", result.stderr)
        self.assertEqual(self.settings.read_bytes(), edited)
        self.assertEqual(self.scheduler.changes(), log)
        self.assertTrue((self.data / "install.json").exists())

    def test_upgrade_failing_after_removal_can_still_be_uninstalled(self):
        self.install()
        (self.data / "connection/connection.json").write_bytes(b"{}")
        self.run_collector("upgrade", "--yes", code=2)
        self.assertEqual(sum(len(v) for v in self.owned_entries().values()), 0)
        self.assertTrue(
            json.loads(self.run_collector("doctor", "--json").stdout)["installed"]
        )
        self.run_collector("uninstall")
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertIn(self.scheduler.stopped()[0], self.scheduler.changes())
        self.assertFalse(self.scheduler.defined())

    def test_uninstall_without_a_record_still_disables_leftover_units(self):
        self.install()
        (self.data / "install.json").unlink()
        self.run_collector("uninstall")
        self.assertIn(self.scheduler.stopped()[0], self.scheduler.changes())
        self.assertFalse(self.scheduler.defined())

    def test_a_named_harness_without_a_settings_file_says_so(self):
        self.settings.unlink()
        result = self.install("--vendor", "claude", code=2)
        self.assertIn(b"settings_missing", result.stderr)
        self.assertNotIn(b"Setup incomplete", result.stderr)
        self.assertFalse(self.data.exists())

    def test_install_refuses_a_relative_settings_path(self):
        result = self.install("--settings", "settings.json", code=2)
        self.assertIn(b"unsupported_path", result.stderr)
        self.assertNotIn(b"Setup incomplete", result.stderr)
        self.assertFalse(self.data.exists())

    def test_group_writable_settings_refuse_before_pairing(self):
        self.settings.chmod(0o664)
        result = self.install(code=2)
        self.assertIn(b"unsafe_settings", result.stderr)
        self.assertFalse(self.data.exists())

    def test_python_observer_settings_refuse_before_pairing(self):
        self.settings.write_bytes(
            self.original.replace(b"codegraph hook prompt", b"observer_hook")
        )
        result = self.install(code=2)
        self.assertIn(b"python_observer_registered", result.stderr)
        self.assertFalse(self.data.exists())

    def test_old_collector_hooks_refuse_before_pairing(self):
        old = b"'/home/u/.local/share/devdiary/collector/0.4.0/devdiary-collector' claude-hook '/home/u/plan.json' >/dev/null 2>&1 || :"
        self.settings.write_bytes(self.original.replace(b"codegraph hook prompt", old))
        result = self.install(code=2)
        self.assertIn(b"old_collector_registered", result.stderr)
        self.assertIn(b"devdiary-collector uninstall", result.stderr)
        self.assertFalse(hasattr(self, "installation"))
        self.assertFalse(self.data.exists())

    def setup_with_path(self, *dirs):
        path = os.pathsep.join([*map(str, dirs), os.environ["PATH"]])
        return self.install(env={**self.env(), "PATH": path})

    def test_setup_warns_when_another_devdiary_comes_first_on_path(self):
        other = self.root / "other-bin"
        other.mkdir()
        (other / "devdiary").write_text("#!/bin/sh\n")
        (other / "devdiary").chmod(0o755)
        result = self.setup_with_path(other)
        self.assertIn(str(other / "devdiary").encode(), result.stderr)
        self.assertTrue((self.data / "install.json").is_file())

    def test_setup_does_not_warn_when_it_is_first_on_path(self):
        result = self.setup_with_path(BIN.parent)
        self.assertNotIn(b"PATH", result.stderr)

    def test_sync_sends_every_installed_harness_now(self):
        self.install()
        self.spool(2)
        self.install("--vendor", "codex", answers=(b"y\n", b"y\n"))
        claude, self.data = self.data, self.codex_data
        self.spool(1, vendor="codex")
        self.data = claude
        self.run_collector("sync")
        self.assertEqual(len(self.received), 3)
        self.assertEqual(self.pending(), 0)

    def test_repository_scope_commands_and_old_setup_flags_are_gone(self):
        state = self.root / "state"
        state.mkdir(mode=0o700)
        plan = self.root / "plan.json"
        removed = [
            ["init", state, "--consent"],
            ["collect", state],
            ["sync", state, self.root / "key", "10"],
            ["status", state],
            ["repair", state],
            ["claude-plan", state, self.settings, plan],
            ["claude-apply", plan, "--consent"],
            ["claude-remove", plan, "--consent"],
            ["hook-apply", plan, "--consent"],
            ["hook-remove", plan, "--consent"],
            ["setup", state],
            ["setup-plan", state, self.settings, plan],
            ["connection-status", state],
            ["connection-sync", state, "10"],
            [*self.install_args("--yes")[1:]],
            [*self.install_args("--new-pair")[1:]],
            [*self.install_args("--record-outside-repos")[1:]],
        ]
        for args in removed:
            with self.subTest(args=args[:2]):
                result = self.run_collector(*args, code=2)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"error_code: invalid_command", result.stderr)
                self.assertEqual(list(state.iterdir()), [])
        self.assertFalse(hasattr(self, "installation"))
        self.assertFalse(self.data.exists())
        self.assertEqual(self.settings.read_bytes(), self.original)
        help = self.run_collector("--help").stdout
        for word in [b"init ", b"collect ", b"claude-plan", b"hook-apply", b"STATE"]:
            self.assertNotIn(word, help)
        for word in [b"setup-plan", b"connection-sync", b"--new-pair"]:
            self.assertNotIn(word, help)
        self.assertNotIn(b"--yes", self.run_collector("setup", "--help").stdout)

    def test_sync_with_nothing_installed_sends_nothing(self):
        out = self.run_collector("sync").stdout
        self.assertIn(b"Not installed", out)
        self.assertEqual(self.received, [])

    def test_timer_sync_delivers_pending_rows_but_never_the_self_test(self):
        self.install()
        self.spool(3)
        doctor = self.timer_sync()
        self.assertEqual(len(self.received), 3)
        self.assertEqual(doctor["pending"], 0)
        self.assertIsNone(doctor["last_sync_error"])
        self.assertFalse(any("selftest" in json.dumps(r) for r in self.received))

    def test_timer_sync_sends_each_rejected_row_once_per_run(self):
        self.install()
        self.spool(120)
        self.observation_status = 422
        doctor = self.timer_sync()
        self.assertEqual(len(self.received), 120)
        self.assertEqual(doctor["pending"], 120)
        self.assertEqual(doctor["last_sync_error"], "http_422")

    def test_timer_sync_stops_on_the_first_server_error(self):
        self.install()
        self.spool(5)
        self.observation_status = 503
        doctor = self.timer_sync()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(doctor["last_sync_error"], "http_503")

    def test_timer_sync_stops_quietly_on_revocation(self):
        self.install()
        self.spool(5)
        self.observation_status = 401
        doctor = self.timer_sync()
        self.assertEqual(len(self.received), 1)
        self.assertTrue(doctor["revoked"])
        self.timer_sync()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(
            json.loads(self.run_collector("doctor", "--json").stdout)[
                "last_sync_error"
            ],
            "revoked",
        )

    def test_a_hook_event_with_the_self_test_prefix_never_reaches_the_outbox(self):
        self.install()
        self.spool(1)
        record = self.record()
        event = {
            "hook_event_name": "SessionStart",
            "session_id": "devdiary-selftest-x",
            "cwd": str(self.root / "demo"),
        }
        subprocess.run(
            [record["executable"], "hook", "claude", record["plan"]],
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
        self.install()
        self.run_collector("uninstall")
        self.assertEqual(self.settings.read_bytes(), compact)

    def interactive(self, answer, edit=None, args=None):
        log = self.scheduler.changes()
        before = self.settings.read_bytes() if self.settings.exists() else None
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
        while not terminal.questions(seen) and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.5)
            if ready:
                seen += os.read(process.stdout.fileno(), 4096)
        self.assertTrue(terminal.questions(seen), seen)
        self.seen = seen
        self.assertEqual(self.scheduler.changes(), log)
        self.assertEqual(
            self.settings.read_bytes() if self.settings.exists() else None, before
        )
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
            while terminal.questions(seen) < prompt and time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if ready:
                    seen += os.read(process.stdout.fileno(), 4096)
            self.assertEqual(terminal.questions(seen), prompt, seen)
            os.write(master, answer)
        out, err = process.communicate(timeout=30)
        return process.returncode, seen + out, err

    def approved(self):
        path = self.data / "connection/outbox/orchestrators-approved.json"
        return json.loads(path.read_text()) if path.exists() else None

    def waiting(self):
        return json.loads(self.run_collector("doctor", "--json").stdout)[
            "orchestrators"
        ]

    def test_scope_compatibility_upgrade_reports_refresh_refusal(self):
        self.install()
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
        self.assertEqual(
            json.loads(self.run_collector("doctor", "--json").stdout)[
                "last_scope_refresh_error"
            ],
            "scope_refresh_http_failure",
        )

    def test_install_reports_a_refresh_refusal(self):
        self.scope_status = 503
        result = self.install(code=2)
        self.assertIn(b"server refused scope refresh", result.stderr)
        self.assertNotIn(b"response-secret", result.stdout + result.stderr)

    def test_timer_sync_delivers_through_a_failed_refresh_and_doctor_shows_it(self):
        self.install()
        self.spool(3)
        self.scope_status = 503
        doctor = self.timer_sync()
        self.assertEqual(len(self.received), 3)
        self.assertEqual(doctor["pending"], 0)
        self.assertIsNone(doctor["last_sync_error"])
        self.assertEqual(
            doctor["last_scope_refresh_error"], "scope_refresh_http_failure"
        )
        self.scope_status = 200
        self.assertIsNone(self.timer_sync()["last_scope_refresh_error"])

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
        self.assertEqual(
            sum(len(v) for v in self.owned_entries().values()), len(EVENTS)
        )
        self.assertIsNone(self.approved())
        self.assertTrue(self.waiting()["waiting_for_approval"])

    def test_upgrade_yes_never_approves_names_without_a_terminal(self):
        self.orchestrators = [PAPERCLIP]
        self.install()
        out = self.run_collector("upgrade", "--yes").stdout
        self.assertIn(b"orchestrators approve", out)
        self.assertIsNone(self.approved())
        self.assertEqual(
            sum(len(v) for v in self.owned_entries().values()), len(EVENTS)
        )
        self.assertTrue(self.waiting()["waiting_for_approval"])

    def test_install_with_nothing_offered_asks_nothing_about_names(self):
        self.orchestrators = []
        out = self.install().stdout
        self.assertNotIn(b"orchestrators approve", out)
        self.assertFalse(self.waiting()["waiting_for_approval"])

    def test_orchestrators_approve_without_a_terminal_refuses(self):
        self.orchestrators = [PAPERCLIP]
        self.install()
        result = self.run_collector("orchestrators", "approve", code=2)
        self.assertIn(b"approval_required", result.stderr)
        self.assertIn(b"terminal", result.stderr)
        self.assertIsNone(self.approved())

    def test_orchestrators_approve_has_no_unattended_flag(self):
        self.orchestrators = [PAPERCLIP]
        self.install()
        self.run_collector("orchestrators", "approve", "--yes", code=2)
        self.assertIsNone(self.approved())

    def test_orchestrators_approve_on_a_terminal_shows_and_saves_the_offer(self):
        self.orchestrators = [PAPERCLIP]
        self.install()
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
        self.install()
        result = self.run_collector("doctor", "--json")
        self.assertIn(b"stay unknown until approved", result.stderr)
        self.assertIn("paperclip", json.loads(result.stdout)["orchestrators"]["notice"])

    def test_doctor_says_nothing_when_no_name_is_offered(self):
        self.install()
        result = self.run_collector("doctor", "--json")
        self.assertNotIn(b"until approved", result.stderr)
        self.assertIsNone(json.loads(result.stdout)["orchestrators"]["notice"])

    def spoil_offer(self):
        offered = self.data / "connection/outbox/orchestrators-offered.json"
        offered.write_text("not json")
        offered.chmod(0o600)

    def test_a_bad_offered_file_stops_neither_doctor_nor_upgrade(self):
        self.install()
        self.spoil_offer()
        self.assertTrue(self.waiting()["offered_list_unreadable"])
        out = self.run_collector("upgrade", "--yes").stdout
        self.assertIn(b"Self-test passed", out)
        self.assertIn(b"next sync replaces it", out)

    def test_orchestrators_approve_names_a_bad_offered_file(self):
        self.install()
        self.spoil_offer()
        args = [str(BIN), "orchestrators", "approve"]
        code, _, err = self.converse(args, [])
        self.assertEqual(code, 2)
        self.assertIn(b"offered_list_unreadable", err)
        self.assertIn(b"next sync replaces it", err)

    def test_orchestrators_approve_with_nothing_waiting_asks_nothing(self):
        self.install()
        args = [str(BIN), "orchestrators", "approve"]
        code, out, err = self.converse(args, [])
        self.assertEqual(code, 0, err)
        self.assertIn(b"No orchestrator change waits", out)

    def test_upgrade_offers_the_names_a_first_install_never_saw(self):
        self.install()
        self.orchestrators = [PAPERCLIP]
        code, out, err = self.converse([str(BIN), "upgrade"], [b"y\n", b"y\n"])
        self.assertEqual(code, 0, err)
        self.assertIn(b"PAPERCLIP_RUN_ID", out)
        self.assertEqual(self.approved(), [PAPERCLIP])

    def backup(self):
        return self.settings.with_name(self.settings.name + ".devdiary-backup")

    def test_declining_consent_before_pairing_sends_and_changes_nothing(self):
        code, err = self.interactive(b"n\n")
        self.assertEqual(code, 2)
        self.assertIn(b"consent_declined", err)
        self.assertIn(b"adds 10 hooks to ~/.claude/settings.json", self.seen)
        self.assertEqual(len(self.contract.starts()), 0)
        self.assertFalse(self.data.exists())
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(self.scheduler.changes(), [])

    def test_settings_edited_after_consent_give_stale_plan_and_write_nothing(self):
        edited = self.original.replace(b'"opus"', b'"sonnet"')
        self.contract.on_exchange = lambda request, body: self.settings.write_bytes(
            edited
        )
        result = self.install(code=2)
        self.assertIn(b"stale_plan", result.stderr)
        self.assertEqual(self.settings.read_bytes(), edited)
        self.assertFalse(self.backup().exists())
        self.assertFalse((self.data / "install.json").exists())
        self.assertEqual(list((self.data / "plans").iterdir()), [])
        self.assertEqual(self.scheduler.changes(), [])

    def test_a_run_stopped_after_pairing_reuses_the_connection_and_asks_again(self):
        edited = self.original.replace(b'"opus"', b'"sonnet"')
        self.contract.on_exchange = lambda request, body: self.settings.write_bytes(
            edited
        )
        self.install(code=2)
        self.contract.on_exchange = None
        out = self.install(answers=(b"d\n", b"y\n")).stdout
        self.assertIn(
            "Claude Code: connected to the workspace acme-diary. To use another account, run `devdiary login`.",
            self.flat(out),
        )
        self.assertNotIn(b"not paired again", out)
        self.assertEqual(len(self.contract.starts()), 1)
        self.assertEqual(out.count(b" hook claude "), len(EVENTS))
        self.assertEqual(
            sum(len(v) for v in self.owned_entries().values()), len(EVENTS)
        )

    def test_a_left_backup_is_restored_on_yes_before_the_settings_are_read(self):
        before = b'{"model": "before-the-interrupted-run"}'
        self.settings.write_bytes(b"not json: an interrupted run")
        self.backup().write_bytes(before)
        self.backup().chmod(0o640)
        result = self.install(code=2, answers=(b"y\n", b"n\n"))
        self.assertIn(b"consent_declined", result.stderr)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o640)
        self.assertFalse(self.backup().exists())
        self.assertEqual(len(self.contract.starts()), 0)

    def test_a_left_backup_kept_on_no_stops_with_backup_kept(self):
        self.backup().write_bytes(b'{"model": "before"}')
        result = self.install(code=2, answers=(b"n\n",))
        self.assertIn(b"backup_kept", result.stderr)
        self.assertIn(b"~/.claude/settings.json.devdiary-backup", result.stdout)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(self.backup().read_bytes(), b'{"model": "before"}')
        self.assertEqual(len(self.contract.starts()), 0)
        self.assertFalse(self.data.exists())

    def test_consent_on_a_tty_installs_then_enables(self):
        code, err = self.interactive(b"y\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(
            sum(len(v) for v in self.owned_entries().values()), len(EVENTS)
        )
        self.assertEqual(self.scheduler.changes(), self.scheduler.started())
        self.assertFalse(self.backup().exists())

    def test_upgrade_summarises_the_change_and_shows_the_new_entries_on_d(self):
        self.install()
        before = self.settings.read_bytes()
        log = self.scheduler.changes()
        code, out, err = self.converse([str(BIN), "upgrade"], [b"d\n", b"n\n"])
        self.assertEqual(code, 2)
        self.assertIn(b"consent_declined", err)
        self.assertIn(f"replaces its hooks of version {VERSION}".encode(), out)
        self.assertLess(
            out.index(b"Upgrade Claude Code? [y/N/d]"), out.index(b" hook claude ")
        )
        self.assertEqual(out.count(b" hook claude "), len(EVENTS))
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.scheduler.changes(), log)

    def test_declining_an_upgrade_from_a_rebuild_keeps_capture_working(self):
        self.install()
        rebuilt = self.rebuilt()
        code, err = self.interactive(b"n\n", args=[str(rebuilt), "upgrade"])
        self.assertEqual(code, 2)
        self.assertIn(b"consent_declined", err)
        self.assertEqual(self.pinned(), BIN.read_bytes())
        self.spool(1)
        self.assertEqual(
            json.loads(self.run_collector("doctor", "--json").stdout)["drops"], {}
        )

    def test_upgrade_from_a_rebuild_pins_and_registers_the_new_build(self):
        self.install()
        rebuilt = self.rebuilt()
        self.run_collector("upgrade", "--yes", exe=rebuilt)
        self.assertEqual(self.pinned(), rebuilt.read_bytes())
        self.spool(1)

    def test_install_refuses_a_revoked_connection_before_registering(self):
        self.install()
        self.revoke()
        self.run_collector("uninstall")
        log = self.scheduler.changes()
        result = self.install(code=2)
        self.assertIn(b"connection_revoked", result.stderr)
        self.assertIn(b"move the connection folder", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse((self.data / "install.json").exists())
        self.assertEqual(self.scheduler.changes(), log)

    def test_a_revoked_connection_moved_aside_pairs_again(self):
        self.install()
        self.revoke()
        self.run_collector("uninstall")
        connection = self.data / "connection"
        connection.rename(self.data / "connection-revoked")
        self.install()
        doctor = json.loads(self.run_collector("doctor", "--json").stdout)
        self.assertFalse(doctor["revoked"])
        self.assertEqual(doctor["registration"]["registration"], "installed")
        self.assertTrue((self.data / "connection-revoked/revoked").exists())
        self.spool(1)

    def test_upgrade_refuses_a_revoked_connection(self):
        self.install()
        self.revoke()
        before, log = self.settings.read_bytes(), self.scheduler.changes()
        result = self.run_collector("upgrade", "--yes", code=2)
        self.assertIn(b"connection_revoked", result.stderr)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.scheduler.changes(), log)

    def test_another_origin_needs_trust_origin(self):
        args = [a for a in self.install_args() if a != "--trust-origin"]
        status, _, err, _ = terminal.answering(args, self.env())
        self.assertEqual(status, 2, err)
        result = subprocess.CompletedProcess(args, status, b"", err)
        self.assertIn(b"--trust-origin", result.stderr)
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertFalse((self.data / "connection").exists())

    def test_the_binary_and_connection_live_under_home_whatever_xdg_data_home_says(
        self,
    ):
        elsewhere = self.root / "xdg-data"
        env = {**self.env(), "XDG_DATA_HOME": str(elsewhere)}
        self.install(env=env)
        self.assertFalse(elsewhere.exists())
        self.assertTrue((self.data / "install.json").exists())

    def test_install_output_names_no_manual_registration_steps(self):
        out = self.install().stdout
        for manual in [b"claude-apply", b"setup-plan", b"NEW_PLAN"]:
            self.assertNotIn(manual, out)

    def test_install_persists_the_outside_repos_choice(self):
        self.install("--record-orchestrated-outside-repos")
        connection = json.loads((self.data / "connection/connection.json").read_text())
        self.assertTrue(connection["scope"]["record_orchestrated_outside_repos"])

    def test_doctor_shows_the_last_receipt_after_a_timer_sync(self):
        self.install()
        self.spool(1)
        self.assertIsNotNone(self.timer_sync()["last_receipt"])

    # Guided setup: every harness found, one approval, then the first session.

    BOTH = (b"both\n", b"y\n", b"y\n")

    def find_codex(self, raw=b'{"hooks":{}}'):
        settings = self.home / ".codex/hooks.json"
        settings.parent.mkdir(exist_ok=True)
        settings.write_bytes(raw)
        settings.chmod(0o600)
        return settings

    def waiting_for(self, seconds):
        return {**self.env(), "DEVDIARY_DEBUG_FIRST_SESSION_WAIT": str(seconds)}

    def hook_event(self, vendor):
        """One event from a session in the tracked repository, as the harness sends it."""
        data = self.codex_data if vendor == "codex" else self.data
        record = json.loads((data / "install.json").read_text())
        event = {
            "hook_event_name": "SessionStart",
            "session_id": str(uuid.uuid4()),
            "cwd": str(self.demo()),
        }
        subprocess.run(
            [record["executable"], "hook", vendor, record["plan"]],
            input=json.dumps(event).encode(),
            env=self.env(),
            timeout=10,
            check=True,
        )

    def assert_failed_exit(self, err, code):
        lines = err.decode().splitlines()
        self.assertEqual(lines[-1], f"error_code: {code}")
        self.assertTrue(lines[-2].startswith("Next: "), lines)

    def test_both_harnesses_found_are_set_up_after_one_approval(self):
        codex = self.find_codex()
        out = self.install(answers=self.BOTH).stdout.decode()
        steps = [
            "[1/4] Check this machine",
            "[2/4] Choose what DevDiary may change",
            "[3/4] Approve this machine in DevDiary",
            "[4/4] Wait for your first session",
        ]
        self.assertEqual(
            [out.index(step) for step in steps],
            sorted(out.index(step) for step in steps),
        )
        (start,) = self.contract.starts()
        self.assertEqual(start["harnesses"], ["claude-code", "codex"])
        self.assertEqual(
            sum(len(v) for v in self.owned_entries().values()), len(EVENTS)
        )
        self.assertIn(" hook codex ", codex.read_text())
        self.assertTrue((self.data / "install.json").is_file())
        self.assertTrue((self.codex_data / "install.json").is_file())
        self.assertEqual(
            self.scheduler.changes(),
            [*self.scheduler.started("claude"), *self.scheduler.started("codex")],
        )
        self.assertIn("/hooks", out)
        self.assertEqual(out.splitlines()[-1], "Next: devdiary doctor")
        # Setup reports no progress: no request reached the server beyond pairing and scope reads.
        self.assertEqual(self.received, [])

    def test_choosing_one_harness_asks_only_about_that_one(self):
        self.find_codex()
        status, _, err, asked = terminal.answering(
            self.install_args(), self.env(), (b"codex\n", b"y\n")
        )
        self.assertEqual(status, 0, err)
        self.assertEqual(asked, 2)
        self.assertEqual(self.contract.starts()[0]["harnesses"], ["codex"])
        self.assertEqual(self.settings.read_bytes(), self.original)

    def test_vendor_skips_the_choice_of_harnesses(self):
        self.find_codex()
        result = self.install("--vendor", "codex")
        self.assertNotIn(terminal.CHOICE, result.stdout)
        self.assertEqual(self.contract.starts()[0]["harnesses"], ["codex"])

    def test_a_declined_harness_is_left_as_it_was_and_the_other_is_set_up(self):
        codex = self.find_codex()
        self.install(answers=(b"both\n", b"y\n", b"n\n"))
        (start,) = self.contract.starts()
        self.assertEqual(start["harnesses"], ["claude-code"])
        self.assertTrue((self.data / "install.json").is_file())
        self.assertEqual(codex.read_bytes(), b'{"hooks":{}}')
        self.assertFalse(self.codex_data.exists())
        self.assertFalse(self.scheduler.defined("codex"))

    def test_declining_every_harness_sends_and_changes_nothing(self):
        codex = self.find_codex()
        result = self.install(code=2, answers=(b"both\n", b"n\n", b"n\n"))
        self.assert_failed_exit(result.stderr, "consent_declined")
        self.assertIn(b"Changed on this machine: nothing", result.stderr)
        self.assertIn(b"Sent to DevDiary: nothing", result.stderr)
        self.assertEqual(self.contract.starts(), [])
        self.assertEqual(self.settings.read_bytes(), self.original)
        self.assertEqual(codex.read_bytes(), b'{"hooks":{}}')
        self.assertFalse(self.data.exists())
        self.assertFalse(self.codex_data.exists())
        self.assertEqual(self.scheduler.changes(), [])

    def test_an_installed_harness_is_shown_and_not_offered_again(self):
        self.install()
        self.find_codex()
        status, out, err, asked = terminal.answering(
            self.install_args(), self.env(), (b"y\n",)
        )
        self.assertEqual(status, 0, err)
        self.assertEqual(asked, 1)
        self.assertIn(b"Claude Code: already set up", out)
        self.assertEqual(self.contract.starts()[-1]["harnesses"], ["codex"])

    def test_with_every_harness_set_up_setup_says_so_and_names_doctor(self):
        self.install()
        log = self.scheduler.changes()
        out = self.install(answers=()).stdout
        self.assertIn(b"already set up", out)
        self.assertEqual(out.splitlines()[-1], b"Next: devdiary doctor")
        self.assertEqual(len(self.contract.starts()), 1)
        self.assertEqual(self.scheduler.changes(), log)

    def test_with_no_harness_found_setup_names_the_paths_it_checked(self):
        self.settings.unlink()
        result = self.install(code=2)
        self.assert_failed_exit(result.stderr, "no_harness_found")
        shown = result.stdout + result.stderr
        self.assertIn(b"checked ~/.claude/settings.json", shown)
        self.assertIn(b"checked ~/.codex", shown)
        self.assertEqual(self.contract.starts(), [])
        self.assertFalse(self.data.exists())

    def test_a_self_test_failure_in_one_harness_lets_the_other_finish(self):
        self.find_codex()
        # Claude goes first. Its pinned copy changes as its job starts, just
        # before its self-test, so the hook refuses to run it.
        pinned = self.data / VERSION / "devdiary"
        self.scheduler.write(
            (FAKE_LAUNCHCTL if DARWIN else FAKE_SYSTEMCTL)
            + f"case \"$*\" in *enable*claude-sync*|bootstrap*claude-sync*) printf x >> '{pinned}' ;; esac\n"
        )
        result = self.install(code=2, answers=self.BOTH)
        self.assert_failed_exit(result.stderr, "self_test_failed")
        self.assertIn(b"Claude Code", result.stderr)
        codex = self.run_collector("doctor", "--json", "--vendor", "codex").stdout
        self.assertEqual(json.loads(codex)["registration"]["registration"], "installed")
        self.assertIn(self.scheduler.started("codex")[-1], self.scheduler.changes())

    def test_the_wait_ends_at_its_limit_with_recording_on(self):
        began = time.monotonic()
        out = self.install(env=self.waiting_for(2)).stdout
        self.assertGreaterEqual(time.monotonic() - began, 2)
        self.assertIn(b"Recording is on", out)
        self.assertEqual(out.splitlines()[-1], b"Next: devdiary doctor")
        self.assertTrue((self.data / "install.json").is_file())

    def test_the_first_receipt_ends_the_wait_and_names_the_harness_and_time(self):
        self.find_codex()
        started = []

        def start_a_codex_session(out):
            if b"[4/4] Wait for your first session" in out and not started:
                started.append(True)
                self.hook_event("codex")

        began = time.monotonic()
        status, out, err, _ = terminal.answering(
            self.install_args(),
            self.waiting_for(60),
            self.BOTH,
            typing=start_a_codex_session,
        )
        self.assertEqual(status, 0, err)
        self.assertLess(time.monotonic() - began, 40)
        self.assertEqual(len(self.received), 1)
        self.assertRegex(
            out.decode(), r"first session from Codex at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
        )
        self.assertEqual(out.splitlines()[-1], b"Next: devdiary doctor")

    def test_ctrl_c_stops_only_the_wait(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen(
            self.install_args(),
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.waiting_for(60),
        )
        os.close(slave)
        seen, answered = b"", 0
        deadline = time.monotonic() + 30
        while b"Ctrl+C" not in seen and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.5)
            if ready:
                seen += os.read(process.stdout.fileno(), 4096)
            if terminal.questions(seen) > answered:
                os.write(master, b"y\n")
                answered += 1
        self.assertIn(b"Ctrl+C", seen)
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=10)
        self.assertEqual(
            sum(len(v) for v in self.owned_entries().values()), len(EVENTS)
        )
        self.assertTrue((self.data / "install.json").is_file())
        self.assertTrue((self.data / "connection/connection.json").is_file())
        self.assertEqual(self.scheduler.changes(), self.scheduler.started())

    # Moving this machine to another account.

    RETIRED = r"\Aconnection\.retired-\d{8}T\d{6}Z(-\d+)?\Z"

    def login_args(self):
        return [
            str(BIN),
            "login",
            "--origin",
            self.origin,
            "--trust-origin",
            "--no-browser",
        ]

    def login(self, code=0, answers=(b"y\n",), env=None):
        status, out, err, asked = terminal.answering(
            self.login_args(), env or self.env(), answers
        )
        self.assertEqual(status, code, err)
        self.assertNotIn(b"dc_live_", out + err)
        self.asked = asked
        return subprocess.CompletedProcess(self.login_args(), status, out, err)

    def interrupted_login(self, until, env=None):
        """Answer yes until `until` shows on stdout or stderr, then press Ctrl+C."""
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen(
            self.login_args(),
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env or self.env(),
        )
        os.close(slave)
        seen, answered = b"", 0
        deadline = time.monotonic() + 30
        while until not in seen and time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout, process.stderr], [], [], 0.5)
            for stream in ready:
                seen += os.read(stream.fileno(), 4096)
            if terminal.questions(seen) > answered:
                os.write(master, b"y\n")
                answered += 1
        self.assertIn(until, seen)
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=10)
        self.assertNotIn(b"dc_live_", seen)

    def flat(self, raw):
        """Output with its wrapped lines joined, so a sentence reads as one line."""
        text = raw.decode() if isinstance(raw, bytes) else raw
        return " ".join(text.split())

    def saved_token(self, data):
        return json.loads((data / "connection/connection.json").read_text())["token"]

    def retired(self, data):
        folders = sorted(p for p in data.iterdir() if p.name.startswith("connection."))
        for folder in folders:
            self.assertRegex(folder.name, self.RETIRED)
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        return folders

    def test_login_moves_both_harnesses_to_another_account_with_one_approval(self):
        self.find_codex()
        self.install(answers=self.BOTH)
        self.hook_event("claude")
        old = self.contract.issued()
        log = self.scheduler.changes()
        self.contract.workspace_name = "other-diary"
        out = self.login().stdout.decode()
        self.assertEqual(self.asked, 1)
        before = out[: out.index("[2/4]")]
        self.assertIn("✓ Claude Code records to the workspace acme-diary", before)
        self.assertIn("✓ Codex records to the workspace acme-diary", before)
        self.assertIn("Sign this machine in to another account? [y/N/d]", out)
        self.assertLess(
            out.index(
                "Approve while signed in to the account this machine should record to."
            ),
            out.index("Approval code"),
        )
        # What waited went to the old account, before both old connections were revoked.
        self.assertEqual(len(self.received), 1)
        self.assertIn(self.received_tokens[0], old)
        self.assertEqual(self.contract.revoked, old)
        self.assertEqual(
            sorted(pairing_server.bearer(a) for a in self.contract.revocations()),
            sorted(old),
        )
        (_, start) = self.contract.starts()
        self.assertEqual(start["harnesses"], ["claude-code", "codex"])
        new = self.contract.issued() - old
        for data in (self.data, self.codex_data):
            self.assertIn(self.saved_token(data), new)
            self.assertEqual(len(self.retired(data)), 1)
        # The hooks and the sync jobs stay, and now record to the new account.
        self.assertEqual(self.scheduler.changes(), log)
        self.assertEqual(
            sum(len(v) for v in self.owned_entries().values()), len(EVENTS)
        )
        self.hook_event("claude")
        self.hook_event("codex")
        self.run_collector("sync")
        self.assertEqual(len(self.received), 3)
        self.assertLessEqual(set(self.received_tokens[1:]), new)
        after = out[out.index("[4/4]") :]
        self.assertIn("✓ Claude Code records to the workspace other-diary", after)
        self.assertIn("✓ Codex records to the workspace other-diary", after)
        self.assertIn("! Codex: trust the DevDiary entries in /hooks", after)
        self.assertNotIn("never received", self.flat(out))
        self.assertEqual(out.splitlines()[-1], "Next: devdiary doctor")

    def test_rows_still_waiting_stay_behind_and_never_reach_the_new_account(self):
        self.install()
        self.observation_status = 503
        self.spool(2)
        out = self.login().stdout.decode()
        self.assertRegex(
            self.flat(out),
            r"Claude Code: 2 events that were not sent stay in "
            r"~/\.local/share/devdiary/cli/claude/connection\.retired-\S+ "
            r"and are never sent\.",
        )
        (retired,) = self.retired(self.data)
        with closing(
            sqlite3.connect(retired / "outbox/collector-rust-v1.sqlite3")
        ) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 2)
        self.observation_status = 201
        self.hook_event("claude")
        self.run_collector("sync")
        # Only the new session reaches the new account.
        new = self.saved_token(self.data)
        self.assertEqual(self.received_tokens.count(new), 1)

    def test_a_connection_revoked_in_the_browser_is_retired_too(self):
        self.install()
        self.spool(1)
        self.contract.revoked.add(self.saved_token(self.data))
        self.assertTrue(self.timer_sync()["revoked"])
        out = self.login().stdout.decode()
        self.assertIn(
            "Claude Code: its connection to the workspace acme-diary was revoked",
            out[: out.index("[2/4]")],
        )
        self.assertEqual(len(self.contract.revocations()), 1)
        (retired,) = self.retired(self.data)
        self.assertTrue((retired / "revoked").exists())
        doctor = json.loads(self.run_collector("doctor", "--json").stdout)
        self.assertFalse(doctor["revoked"])
        self.assertEqual(doctor["registration"]["registration"], "installed")

    def assert_login_stops_before_anything_changes(self, answer, code):
        self.install()
        settings, token = self.settings.read_bytes(), self.saved_token(self.data)
        log = self.scheduler.changes()
        self.contract.intercept = lambda path, body: (
            answer if path == pairing_server.CONNECTION else None
        )
        result = self.login(code=2)
        self.assert_failed_exit(result.stderr, code)
        self.assertIn(b"Nothing changed on this machine.", result.stderr)
        self.assertEqual(len(self.contract.revocations()), 1)
        self.assertEqual(self.saved_token(self.data), token)
        self.assertEqual(self.retired(self.data), [])
        self.assertEqual(self.settings.read_bytes(), settings)
        self.assertEqual(len(self.contract.starts()), 1)
        self.assertEqual(self.scheduler.changes(), log)

    def test_an_unreachable_server_stops_login_before_anything_changes(self):
        self.assert_login_stops_before_anything_changes("close", "revoke_unreachable")

    def test_a_server_that_does_not_revoke_stops_login_before_anything_changes(self):
        missing = (404, {}, b'{"error":"not_found"}')
        self.assert_login_stops_before_anything_changes(missing, "revoke_refused")

    def test_an_interrupted_login_run_again_completes(self):
        self.find_codex()
        self.install(answers=self.BOTH)
        self.contract.on_start = pairing_server.nobody
        self.interrupted_login(b"Approval code")
        # Unpaired, with the hooks in place.
        self.assertEqual(
            sum(len(v) for v in self.owned_entries().values()), len(EVENTS)
        )
        for data in (self.data, self.codex_data):
            self.assertFalse((data / "connection/connection.json").exists())
            self.assertEqual(len(self.retired(data)), 1)
        # The sync job runs meanwhile and leaves the folder as pairing needs it.
        result = self.run_collector(
            "sync", self.data / "connection", exe=self.record()["executable"], code=2
        )
        self.assertEqual(
            result.stderr.decode().splitlines()[-1], "error_code: not_paired"
        )
        self.assertIn(b"Next: devdiary login", result.stderr)
        self.assertLessEqual(
            {p.name for p in (self.data / "connection").iterdir()}, {"setup.lock"}
        )
        self.contract.on_start = pairing_server.approve_in_browser
        self.contract.workspace_name = "other-diary"
        out = self.login().stdout.decode()
        self.assertIn("Claude Code: not paired", out[: out.index("[2/4]")])
        self.assertIn("Finish signing this machine in? [y/N/d]", out)
        starts = self.contract.starts()
        self.assertEqual(len(starts), 3)
        self.assertEqual(starts[-1]["harnesses"], ["claude-code", "codex"])
        for data in (self.data, self.codex_data):
            self.assertIn(self.saved_token(data), self.contract.requests[-1].tokens())
        self.hook_event("codex")
        self.run_collector("sync")
        self.assertIn(self.received_tokens[-1], self.contract.requests[-1].tokens())

    def test_a_login_whose_new_connection_never_arrived_finishes_on_the_next_run(self):
        self.install()
        self.contract.intercept = lambda path, body: (
            "close" if isinstance(body, dict) and "verifier" in body else None
        )
        result = self.login(code=2)
        self.assert_failed_exit(result.stderr, "login_incomplete")
        self.assertIn(b"Next: devdiary login", result.stderr)
        self.contract.intercept = None
        out = self.login().stdout.decode()
        self.assertIn(
            "may have left a connection in DevDiary that this machine never received",
            self.flat(out),
        )
        self.assertEqual(len(self.retired(self.data)), 2)
        self.assertIn(self.saved_token(self.data), self.contract.requests[-1].tokens())

    def test_a_login_stopped_after_the_approval_moves_the_hooks_on_the_next_run(self):
        self.install()
        self.contract.workspace_name = "other-diary"
        paused = {**self.env(), "DEVDIARY_DEBUG_PAUSE_AT": "login_paired"}
        self.interrupted_login(b"paused at login_paired", env=paused)
        out = self.login().stdout.decode()
        self.assertIn(
            "Claude Code: connected to the workspace other-diary, but its hook entries still name the old connection.",
            self.flat(out[: out.index("[2/4]")]),
        )
        self.assertEqual(len(self.contract.starts()), 2)
        self.assertEqual(len(self.retired(self.data)), 1)
        self.hook_event("claude")
        self.run_collector("sync")
        self.assertEqual(self.received_tokens, [self.saved_token(self.data)])
        self.assertIn("✓ Claude Code records to the workspace other-diary", out)

    def test_declining_login_sends_and_changes_nothing(self):
        self.install()
        self.spool(1)
        settings, token = self.settings.read_bytes(), self.saved_token(self.data)
        result = self.login(code=2, answers=(b"n\n",))
        self.assert_failed_exit(result.stderr, "consent_declined")
        self.assertIn(b"Sent to DevDiary: nothing", result.stderr)
        self.assertEqual(self.received, [])
        self.assertEqual(self.contract.revocations(), [])
        self.assertEqual(len(self.contract.starts()), 1)
        self.assertEqual(self.saved_token(self.data), token)
        self.assertEqual(self.settings.read_bytes(), settings)

    def test_login_without_a_terminal_refuses_before_anything_is_sent(self):
        self.install()
        result = self.run_collector("login", code=2)
        self.assert_failed_exit(result.stderr, "no_terminal")
        self.assertEqual(self.contract.revocations(), [])

    def test_login_with_nothing_set_up_says_to_run_setup(self):
        result = self.login(code=2, answers=())
        self.assert_failed_exit(result.stderr, "not_installed")
        self.assertIn(b"Next: devdiary setup", result.stderr)
        self.assertEqual(self.contract.starts(), [])


def setUpModule():
    isolation.guard()


if __name__ == "__main__":
    unittest.main()
