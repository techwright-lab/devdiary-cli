"""Native Codex adapter fixtures. These do not qualify stock Codex."""

import json
import subprocess
import unittest

import install_test


class CodexHookTest(unittest.TestCase):
    def setUp(self):
        self.fixture = install_test.InstallTest()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.runtime = "codex"
        self.fixture.data /= "codex"
        self.fixture.settings = self.fixture.home / ".codex/hooks.json"
        self.fixture.install("--vendor", "codex", "--yes")
        self.repo = self.fixture.root / "demo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "remote",
                "add",
                "origin",
                "https://github.com/acme/demo",
            ],
            check=True,
        )

    def fire(self, event, **metadata):
        record = self.fixture.record()
        result = subprocess.run(
            [record["executable"], "codex-hook", record["plan"]],
            input=json.dumps(
                {
                    "hook_event_name": event,
                    "session_id": "session-1",
                    "cwd": str(self.repo),
                    **metadata,
                }
            ).encode(),
            capture_output=True,
            env=self.fixture.env(),
            timeout=5,
            check=True,
        )
        self.assertEqual(result.stdout + result.stderr, b"")

    def sync(self):
        self.fixture.run_collector("timer-sync", self.fixture.data / "connection")
        return self.fixture.received

    def test_hook_delivers_codex_correlation_without_private_content(self):
        self.fire(
            "UserPromptSubmit",
            turn_id="turn-1",
            prompt_id="not-codex",
            prompt="PRIVATE_CODEX_SENTINEL",
            transcript_path="PRIVATE_CODEX_SENTINEL",
        )
        rows = self.sync()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["runtime"], "codex")
        self.assertEqual(rows[0]["turn_id"], "turn-1")
        self.assertNotIn("prompt_id", rows[0])
        self.assertNotIn("PRIVATE_CODEX_SENTINEL", json.dumps(rows))

    def test_permission_wait_retains_tool_and_turn_correlation(self):
        self.fire(
            "PermissionRequest",
            turn_id="turn-1",
            tool_use_id="call-1",
            tool_name="Bash",
            tool_input={"command": "PRIVATE_CODEX_SENTINEL"},
        )
        rows = self.sync()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool_name"], "Bash")
        self.assertEqual(rows[0]["tool_use_id"], "call-1")
        self.assertEqual(rows[0]["turn_id"], "turn-1")
        self.assertNotIn("tool_input", rows[0])

    def test_resumed_children_deduplicate_per_turn_and_retry_keeps_bytes(self):
        for turn in ("one", "one", "two"):
            self.fire("SubagentStop", agent_id="child", turn_id=turn)
        self.fire("Interrupt", turn_id="two")
        self.fire("PostToolUseFailure", tool_use_id="not-a-codex-event")
        self.fixture.observation_status = 503
        self.sync()
        first = self.fixture.received[0]
        self.fixture.observation_status = 201
        self.sync()
        self.assertEqual(self.fixture.received[-1], first)
        self.assertEqual(self.fixture.received_raw[0], self.fixture.received_raw[-1])
        delivered = self.fixture.received[1:]
        self.assertEqual(len(delivered), 3)
        self.assertCountEqual(
            [(r["event"], r["turn_id"]) for r in delivered],
            [("SubagentStop", "one"), ("SubagentStop", "two"), ("Interrupt", "two")],
        )
        self.assertNotIn("SessionEnd", [r["event"] for r in delivered])

    def test_interruption_does_not_link_a_later_commit_to_the_cancelled_call(self):
        self.fire(
            "PreToolUse", turn_id="turn-1", tool_use_id="call-1", tool_name="Bash"
        )
        self.fire("Interrupt", turn_id="turn-1")
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "--allow-empty",
                "-qm",
                "after interruption",
            ],
            check=True,
        )
        self.fire(
            "PostToolUse", turn_id="turn-1", tool_use_id="call-1", tool_name="Bash"
        )
        rows = self.sync()
        self.assertEqual(len(rows), 3)
        self.assertNotIn("commits", rows[-1])

    def test_upgrade_and_remove_keep_unrelated_hooks_and_pending_evidence(self):
        self.fixture.run_collector(
            "hook-apply", self.fixture.record()["plan"], "--consent"
        )
        settings = self.fixture.settings
        doc = json.loads(settings.read_bytes())
        unrelated = {"hooks": [{"type": "command", "command": "other-tool"}]}
        doc["hooks"]["Stop"].append(unrelated)
        settings.write_text(json.dumps(doc))
        self.fire("Stop", turn_id="turn-1")
        self.fixture.run_collector("upgrade", "--vendor", "codex", "--yes")
        self.fixture.run_collector("uninstall", "--vendor", "codex")
        self.assertEqual(
            json.loads(settings.read_bytes()), {"hooks": {"Stop": [unrelated]}}
        )
        self.assertFalse(
            (
                self.fixture.config / "systemd/user/devdiary-collector-codex-sync.timer"
            ).exists()
        )
        rows = self.sync()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["runtime"], "codex")


if __name__ == "__main__":
    unittest.main()
