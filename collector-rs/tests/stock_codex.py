"""Opt-in, network-isolated discovery smoke for stock Codex 0.166.0.

This checks real config discovery and untrusted status, not model/tool lifecycle
or permission-wait/minute qualification. No provider credential is loaded.
Set DEVDIARY_CODEX_BINARY and DEVDIARY_CODEX_SHA256 to pin the executable.
"""

import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import time
import unittest

import codex_hook


@unittest.skipUnless(
    os.environ.get("DEVDIARY_CODEX_BINARY"),
    "set DEVDIARY_CODEX_BINARY to stock Codex 0.166.0",
)
class StockCodexSmoke(unittest.TestCase):
    def test_stock_codex_discovers_all_generated_hooks_as_untrusted(self):
        fixture = codex_hook.CodexHookTest()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        root = fixture.fixture.root
        native = root / "stock-codex"
        shutil.copyfile(os.environ["DEVDIARY_CODEX_BINARY"], native)
        native.chmod(0o700)
        digest = hashlib.sha256(native.read_bytes()).hexdigest()
        self.assertEqual(digest, os.environ["DEVDIARY_CODEX_SHA256"])
        prefix = ["bwrap", "--tmpfs", "/"]
        for path in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
            prefix += ["--ro-bind", path, path]
        prefix += [
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(root),
            str(root),
            "--chdir",
            str(fixture.repo),
            "--unshare-net",
            "--",
            str(native),
        ]
        env = {
            "HOME": str(fixture.fixture.home),
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
        }
        version = subprocess.run(
            [*prefix, "--version"], env=env, capture_output=True, check=True, timeout=10
        )
        self.assertEqual(version.stdout.strip(), b"codex-cli 0.166.0")
        process = subprocess.Popen(
            [*prefix, "app-server"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:

            def request(number, method, params):
                process.stdin.write(
                    (
                        json.dumps({"id": number, "method": method, "params": params})
                        + "\n"
                    ).encode()
                )
                process.stdin.flush()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if not select.select([process.stdout], [], [], 0.1)[0]:
                        continue
                    raw = process.stdout.readline(262145)
                    self.assertTrue(raw)
                    self.assertLessEqual(len(raw), 262144)
                    reply = json.loads(raw)
                    if reply.get("id") == number:
                        self.assertNotIn("error", reply)
                        return reply["result"]
                self.fail("stock Codex response timeout")

            request(
                1,
                "initialize",
                {
                    "clientInfo": {"name": "devdiary-smoke", "version": "0.1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            rows = request(2, "hooks/list", {"cwds": [str(fixture.repo)]})["data"]
            self.assertEqual(rows[0]["errors"], [])
            hooks = rows[0]["hooks"]
            self.assertEqual(
                {hook["eventName"] for hook in hooks},
                {
                    "sessionStart",
                    "userPromptSubmit",
                    "preToolUse",
                    "postToolUse",
                    "permissionRequest",
                    "stop",
                    "subagentStart",
                    "subagentStop",
                    "sessionEnd",
                    "interrupt",
                },
            )
            self.assertEqual(len(hooks), 10)
            self.assertEqual({hook["trustStatus"] for hook in hooks}, {"untrusted"})
            self.assertEqual(fixture.sync(), [])
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()


if __name__ == "__main__":
    unittest.main()
