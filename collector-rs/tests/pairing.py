"""Pairing through `devdiary setup` against the local contract-v2 pairing server,
not Rails or stock-runtime qualification."""

import http.server
import json
import os
import platform
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path

import pairing_server
import terminal
from pairing_server import EXCHANGE, START, display

CRATE = Path(__file__).resolve().parents[1]
BIN = CRATE / "target/debug/devdiary"
VERSION = tomllib.loads((CRATE / "Cargo.toml").read_text())["package"]["version"]
FAKE_SYSTEMCTL = """#!/bin/sh
case "$*" in
  *is-enabled*) echo enabled ;;
  *is-active*) echo active ;;
esac
"""
FAKE_OPENER = """#!/bin/sh
printf '%s\\n' "$@" >> "$(dirname "$0")/opened"
"""
CONNECTED = "Connected. Return to the terminal."


def later(action):
    """Run `action` on its own thread, as a person in a browser would."""
    threading.Thread(target=action, daemon=True).start()


class PairingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.settings = self.home / ".claude/settings.json"
        self.settings.write_text("{}")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name, script in (("systemctl", FAKE_SYSTEMCTL), ("opener", FAKE_OPENER)):
            (bin_dir / name).write_text(script)
            (bin_dir / name).chmod(0o700)
        self.opened = bin_dir / "opened"
        self.env = {
            **{k: v for k, v in os.environ.items() if k != "CODEX_HOME"},
            "HOME": str(self.home),
            "DEVDIARY_DEBUG_FIRST_SESSION_WAIT": "0",
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "DEVDIARY_COLLECTOR_SYSTEMCTL": str(bin_dir / "systemctl"),
            "DEVDIARY_DEBUG_OPENER": str(bin_dir / "opener"),
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
        }
        self.data = self.home / ".local/share/devdiary/cli/claude"
        self.contract = pairing_server.Contract()
        contract = self.contract

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                body = json.dumps(
                    {
                        "schema_version": 1,
                        "collector_ref": contract.collector_ref,
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
                if not contract.handle(self):
                    self.send_response(404)
                    self.end_headers()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def command(self, *extra, browser=False):
        return [
            str(BIN),
            "setup",
            "--origin",
            self.origin,
            "--trust-origin",
            *([] if browser else ["--no-browser"]),
            *extra,
        ]

    def run_setup(
        self, *extra, answers=(b"y\n", b"y\n"), browser=False, typing=None, env=None
    ):
        args = self.command(*extra, browser=browser)
        code, out, err, _ = terminal.answering(
            args, env or self.env, answers, typing=typing
        )
        self.assertNotIn(b"dp_pair_", out + err)
        self.assertNotIn(b"dc_live_", out + err)
        return subprocess.CompletedProcess(args, code, out, err)

    def connection(self, data=None):
        return (data or self.data) / "connection"

    def set_aside(self, data=None):
        """The manual recovery: move the connection folder aside."""
        connection = self.connection(data)
        if connection.exists():
            connection.rename(connection.with_name(f"set-aside-{time.time_ns()}"))

    def uninstall(self, *extra):
        result = subprocess.run(
            [str(BIN), "uninstall", *extra],
            capture_output=True,
            env=self.env,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def polls(self):
        return [b for b in self.contract.exchanges() if "verifier" not in b]

    def assert_nothing_local(self):
        self.assertEqual(self.settings.read_text(), "{}")
        self.assertFalse((self.data / "install.json").exists())
        left = {p.name for p in self.connection().iterdir()}
        self.assertLessEqual(left, {"setup.lock", "start-retry-after"})

    def test_one_request_names_the_harness_and_this_machine_and_nothing_else(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        (start,) = self.contract.starts()
        self.assertEqual(
            set(start),
            {
                "schema_version",
                "installation_id",
                "harnesses",
                "scope_kind",
                "hostname",
                "os_name",
                "os_version",
                "arch",
                "cli_version",
                "callback_port",
            },
        )
        self.assertEqual(start["schema_version"], 2)
        self.assertEqual(start["harnesses"], ["claude-code"])
        self.assertEqual(start["scope_kind"], "installation")
        self.assertEqual(start["hostname"], socket.gethostname())
        self.assertEqual(start["arch"], platform.machine().replace("arm64", "aarch64"))
        self.assertEqual(start["cli_version"], VERSION)
        if platform.system() == "Darwin":
            self.assertEqual(start["os_name"], "macOS")
            self.assertEqual(start["os_version"], platform.mac_ver()[0])
        else:
            self.assertEqual(start["os_name"], "Linux")
            self.assertEqual(start["os_version"], platform.release())
        self.assertNotIn(str(self.home), json.dumps(start))
        saved = json.loads((self.connection() / "connection.json").read_bytes())
        self.assertEqual(saved["scope"]["installation_id"], start["installation_id"])

    def test_the_terminal_prints_the_code_and_opens_its_approval_link(self):
        result = self.run_setup(browser=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        code = display(self.contract.latest.code)
        link = f"{self.origin}/collector/approve?code={code}"
        self.assertIn(f"Approval code: {code}".encode(), result.stdout)
        self.assertIn(link.encode(), result.stdout)
        self.assertEqual(self.opened.read_text(), link + "\n")
        self.assertNotIn(b"Select:", result.stdout)
        self.assertIsNone(re.search(rb"\b[0-9a-f]{32}\b", result.stdout))

    def test_the_browser_callback_connects_and_names_the_workspace(self):
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.contract.pages, [(200, CONNECTED + "\n")])
        self.assertIn(b"Connected to the DevDiary workspace acme-diary.", result.stdout)
        request = self.contract.latest
        self.assertEqual(request.status, "consumed")
        (exchange,) = self.contract.exchanges()
        self.assertEqual(set(exchange), {"schema_version", "verifier"})
        self.assertEqual(self.contract.calls[-1][2], f"Bearer {request.token}")
        saved = self.connection() / "connection.json"
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        token = json.loads(saved.read_bytes())["token"]
        self.assertEqual(token, request.connections[0]["token"])
        before = saved.read_bytes()
        self.uninstall()
        self.assertEqual(self.run_setup().returncode, 0)
        self.assertEqual(saved.read_bytes(), before)
        self.assertEqual(len(self.contract.calls), 2)

    def test_a_callback_that_names_another_request_is_refused(self):
        refused = []

        def browser(contract, request):
            def visit():
                other = "BCDFGHJK" if request.code != "BCDFGHJK" else "CDFGHJKL"
                wrong = f"http://127.0.0.1:{request.port}/callback?code={other}&verifier=BCDFGHJK"
                refused.append(contract.browse(wrong)[0])
                refused.append(len(contract.exchanges()))
                contract.browse(contract.approve(request))

            later(visit)

        self.contract.on_start = browser
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(refused, [404, 0])
        self.assertEqual(self.contract.pages[-1], (200, CONNECTED + "\n"))
        self.assertEqual(len(self.contract.exchanges()), 1)

    def test_without_a_listener_the_request_has_no_port_and_a_typed_verifier_connects(
        self,
    ):
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        env = {**self.env, "DEVDIARY_DEBUG_CALLBACK_PORT": str(taken.getsockname()[1])}
        self.contract.on_start = lambda contract, request: contract.approve(request)
        typed = []

        def typing(out):
            if typed or b"Approval code" not in out:
                return None
            verifier = display(self.contract.latest.verifier).lower()
            typed.append(verifier)
            return f"  {verifier} \n".encode()

        result = self.run_setup(env=env, typing=typing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("callback_port", self.contract.starts()[0])
        self.assertEqual(len(typed), 1)
        self.assertTrue((self.connection() / "connection.json").exists())

    def test_five_wrong_verifiers_deny_and_change_nothing_locally(self):
        self.contract.on_start = pairing_server.nobody

        def typing(out):
            if b"Approval code" in out and not self.contract.exchanges():
                return b"BCDF-GHJK\n" * 5
            return None

        result = self.run_setup(typing=typing)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"pairing_denied", result.stderr)
        for left in ("4 attempts", "3 attempts", "2 attempts", "1 attempt"):
            self.assertIn(f"wrong. {left} left.".encode(), result.stdout)
        self.assertEqual(self.contract.latest.status, "denied")
        self.assert_nothing_local()
        self.contract.on_start = pairing_server.approve_in_browser
        self.assertEqual(self.run_setup().returncode, 0)

    def test_a_denial_in_the_browser_stops_with_pairing_denied(self):
        self.contract.on_start = lambda contract, request: contract.deny(request)
        result = self.run_setup()
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"pairing_denied", result.stderr)
        self.assert_nothing_local()

    def test_an_expired_request_starts_a_fresh_one_with_a_new_code(self):
        def first_expires(contract, request):
            if len(contract.requests) == 1:
                contract.expire(request)
            else:
                pairing_server.approve_in_browser(contract, request)

        self.contract.on_start = first_expires
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second = self.contract.requests
        self.assertEqual([s["schema_version"] for s in self.contract.starts()], [2, 2])
        out = result.stdout.decode()
        for request in (first, second):
            link = f"{self.origin}/collector/approve?code={display(request.code)}"
            self.assertIn(link, out)
        self.assertLess(out.index(display(first.code)), out.index("expired"))
        self.assertLess(out.index("expired"), out.index(display(second.code)))
        saved = json.loads((self.connection() / "connection.json").read_bytes())
        self.assertEqual(
            saved["scope"]["installation_id"], second.body["installation_id"]
        )

    def test_a_fresh_request_after_expiry_obeys_the_start_rate_limit(self):
        self.contract.start_limit = 1
        self.contract.on_start = lambda contract, request: contract.expire(request)
        result = self.run_setup()
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"rate limited", result.stderr)
        self.assertEqual(
            result.stderr.splitlines()[-1], b"error_code: start_rate_limited"
        )
        self.assertEqual(len(self.contract.starts()), 2)
        self.assertTrue((self.connection() / "start-retry-after").exists())

    def test_the_cli_never_falls_back_to_schema_version_1(self):
        old = json.dumps({"error": "unsupported_schema_version", "supported": [1]})
        self.contract.intercept = lambda path, body: (
            (422, {}, old.encode()) if path == START else None
        )
        result = self.run_setup()
        self.assertEqual(result.returncode, 2)
        self.assertEqual([s["schema_version"] for s in self.contract.starts()], [2])
        self.assert_nothing_local()

    def test_an_unreachable_server_changes_nothing(self):
        self.origin = "http://127.0.0.1:1"
        result = self.run_setup()
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"Nothing changed on this machine", result.stderr)
        self.assert_nothing_local()

    def test_polls_keep_the_interval_and_retry_after(self):
        self.contract.on_start = pairing_server.nobody

        def answer(path, body):
            if path == EXCHANGE and len(self.polls()) == 1:
                return 429, {"Retry-After": "6"}, b'{"error":"rate_limited"}'
            return None

        def approve_on_second_poll(request, body):
            if "verifier" not in body and len(self.polls()) == 2:
                location = self.contract.approve(request)
                later(lambda: self.contract.browse(location))

        self.contract.intercept = answer
        self.contract.on_exchange = approve_on_second_poll
        result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        times = [t for path, _, _, t in self.contract.calls]
        self.assertGreaterEqual(times[1] - times[0], 5)
        self.assertGreaterEqual(times[2] - times[1], 6)

    def test_start_retry_after_survives_restart(self):
        self.contract.intercept = lambda path, body: (
            (429, {"Retry-After": "60"}, b"{}") if path == START else None
        )
        self.assertNotEqual(self.run_setup().returncode, 0)
        self.contract.intercept = None
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"rate limited", result.stderr)
        self.assertEqual(len(self.contract.calls), 1)

    def test_a_lost_exchange_response_needs_the_connection_set_aside(self):
        self.contract.intercept = lambda path, body: (
            "close" if path == EXCHANGE and "verifier" in body else None
        )
        self.assertNotEqual(self.run_setup().returncode, 0)
        count = len(self.contract.calls)
        result = self.run_setup()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"move the connection folder", result.stderr)
        self.assertEqual(len(self.contract.calls), count)
        self.contract.intercept = None
        self.set_aside()
        self.assertEqual(self.run_setup().returncode, 0)

    def test_an_interrupt_before_the_exchange_changes_nothing(self):
        self.contract.on_start = pairing_server.nobody
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        with subprocess.Popen(
            self.command(),
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        ) as p:
            os.close(slave)
            assert p.stdout is not None
            seen = b""
            for wanted, answer in ((terminal.PROMPT, b"y\n"), (b"Approval code", None)):
                while wanted not in seen:
                    chunk = os.read(p.stdout.fileno(), 4096)
                    self.assertTrue(chunk, seen)
                    seen += chunk
                if answer:
                    os.write(master, answer)
            p.send_signal(signal.SIGINT)
            p.communicate(timeout=5)
        self.assertNotEqual(p.returncode, 0)
        self.assert_nothing_local()
        self.contract.on_start = pairing_server.approve_in_browser
        self.assertEqual(self.run_setup().returncode, 0)

    def test_unexpected_answers_fail_closed(self):
        def start_answer(change):
            def intercept(path, body):
                if path != START:
                    return None
                status, headers, doc = self.contract.start(
                    json.dumps(body).encode(), "application/json"
                )
                change(doc)
                return status, headers, json.dumps(doc).encode()

            return intercept

        def elsewhere(doc):
            doc["approval_path"] = "/settings/collector?code=" + doc["approval_code"]

        def past(doc):
            doc["expires_at"] = "2000-01-01T00:00:00Z"

        def duplicate(path, body):
            if path != EXCHANGE or "verifier" not in body:
                return None
            status, headers, doc = self.contract.exchange(
                f"Bearer {self.contract.latest.token}", json.dumps(body).encode()
            )
            raw = json.dumps(doc).encode()
            raw = raw.replace(
                b'"workspace_name"', b'"workspace_name": "x", "workspace_name"'
            )
            return status, headers, raw

        def redirect(path, body):
            if path == EXCHANGE and "verifier" in body:
                return 307, {"Location": "/leak"}, b""
            return None

        cases = {
            "approval page elsewhere": (start_answer(elsewhere), {}),
            "already expired": (start_answer(past), {}),
            "duplicate keys": (duplicate, {}),
            "redirected exchange": (redirect, {}),
            "another harness": (None, {"runtime": "codex"}),
            "another scope": (None, {"scope_kind": "repository"}),
            "an extra key": (None, {"label": "elsewhere"}),
        }
        for name, (intercept, shape) in cases.items():
            with self.subTest(name):
                self.contract.intercept = intercept
                self.contract.connection_shape = shape
                self.set_aside()
                self.assertNotEqual(self.run_setup().returncode, 0)
                self.assertFalse((self.connection() / "connection.json").exists())
        self.assertNotIn("/leak", [c[0] for c in self.contract.calls])

    def test_codex_connection_is_bound_to_its_requested_harness(self):
        result = self.run_setup("--vendor", "codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.contract.starts()[0]["harnesses"], ["codex"])
        codex = self.home / ".local/share/devdiary/cli/codex"
        path = self.connection(codex) / "connection.json"
        saved = path.read_bytes()
        self.assertEqual(json.loads(saved)["scope"]["runtime"], "codex")
        self.uninstall("--vendor", "codex")
        self.assertEqual(self.run_setup("--vendor", "codex").returncode, 0)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(len(self.contract.starts()), 1)

    def test_invalid_saved_harness_is_not_treated_as_legacy_claude(self):
        self.assertEqual(self.run_setup().returncode, 0)
        self.uninstall()
        saved = self.connection() / "installation.json"
        doc = json.loads(saved.read_bytes())
        doc["runtime"] = None
        saved.write_text(json.dumps(doc))
        self.assertEqual(self.run_setup().returncode, 2)

    def test_insecure_state_and_untrusted_origin_never_connect(self):
        self.data.mkdir(parents=True, mode=0o700)
        connection = self.connection()
        connection.mkdir()
        connection.chmod(0o755)
        self.assertNotEqual(self.run_setup().returncode, 0)
        connection.rmdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        connection.symlink_to(elsewhere, target_is_directory=True)
        self.assertNotEqual(self.run_setup().returncode, 0)
        connection.unlink()
        untrusted = [a for a in self.command() if a != "--trust-origin"]
        code, _, _, _ = terminal.answering(untrusted, self.env)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.contract.calls, [])


if __name__ == "__main__":
    unittest.main()
