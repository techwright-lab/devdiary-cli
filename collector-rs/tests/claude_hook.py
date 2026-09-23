"""Disposable Linux CLI/shell fixtures, NOT stock Claude runtime qualification."""

import concurrent.futures
import fcntl
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from devdiary import observer_hook

ROOT = Path(__file__).resolve().parents[1]
EVENTS = observer_hook.EVENTS


class ClaudeHookTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="claude-rust-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.exe = self.root / "collector ' quoted"
        shutil.copyfile(ROOT / "target/debug/devdiary-collector", self.exe)
        self.exe.chmod(0o700)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.repo = self.root / "repo"
        self.repo.mkdir(mode=0o700)
        self.settings = self.root / "settings.json"
        self.original = {
            "permissions": {"allow": ["Read"]},
            "env": {"PRIVATE": "CONFIG_SECRET"},
            "hooks": {"Stop": [], "Unrelated": [{"keep": True}]},
        }
        self.save(self.settings, self.original)
        self.plan = self.root / "plan ' quoted.json"
        self.scope = json.loads((ROOT / "tests/fixtures/scope.json").read_bytes())
        self.scope["repository"] = str(self.repo)
        self.command("init", self.state, "--consent", data=self.scope)
        self.command("claude-plan", self.state, self.settings, self.plan)
        self.command("claude-apply", self.plan, "--consent")
        self.raw = {
            "hook_event_name": "PostToolUse",
            "session_id": "session-1",
            "cwd": str(self.repo),
            "tool_use_id": "tool-1",
            "tool_name": "Read",
            "prompt": "PRIVATE_SENTINEL",
            "tool_input": {"path": "PRIVATE_SENTINEL"},
            "tool_response": "PRIVATE_SENTINEL",
            "transcript_path": "PRIVATE_SENTINEL",
            "actor_ref": "not-authoritative",
        }

    def save(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def command(self, *args, data=None, code=0):
        raw = json.dumps(data).encode() if data is not None else b""
        result = subprocess.run(
            [str(self.exe), *map(str, args)],
            input=raw,
            capture_output=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def hook(self, data=None, raw=None):
        result = subprocess.run(
            [str(self.exe), "claude-hook", str(self.plan)],
            input=raw
            if raw is not None
            else json.dumps(data if data is not None else self.raw).encode(),
            capture_output=True,
            check=False,
            timeout=2,
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (0, b"", b"")
        )
        return result

    def rows(self):
        with sqlite3.connect(self.state / "collector-rust-v1.sqlite3") as db:
            return [
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM outbox ORDER BY seq")
            ]

    def test_roundtrip_quoted_shell_private_and_removed_hook(self):
        config = json.loads(self.settings.read_bytes())
        command = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            input=json.dumps(self.raw).encode(),
            capture_output=True,
            check=False,
            timeout=2,
            env={"PATH": "/nonexistent"},
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (0, b"", b"")
        )
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["attribution_basis"], "unknown")
        self.assertNotIn("actor_ref", self.rows()[0])
        for file in self.state.iterdir():
            self.assertNotIn(b"PRIVATE_SENTINEL", file.read_bytes())
            self.assertEqual(file.stat().st_mode & 0o077, 0)
        self.assertNotIn(b"CONFIG_SECRET", self.plan.read_bytes())
        self.command("claude-remove", self.plan, "--consent")
        self.assertEqual(json.loads(self.settings.read_bytes()), self.original)
        self.hook(dict(self.raw, tool_use_id="after-remove"))
        self.assertEqual(len(self.rows()), 1)
        self.exe.unlink()
        result = subprocess.run(
            ["/bin/sh", "-c", command],
            input=b"{}",
            capture_output=True,
            check=False,
            timeout=2,
        )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (0, b"", b"")
        )

    def test_saved_plan_validation_precedes_all_mutations(self):
        self.command("claude-remove", self.plan, "--consent")
        self.plan.unlink()
        self.command("claude-plan", self.state, self.settings, self.plan)
        original = json.loads(self.plan.read_bytes())
        cases = [
            dict(original, absent_events="invalid"),
            dict(original, absent_events=original["absent_events"] + ["Stop"]),
            dict(original, absent_events=original["absent_events"][1:]),
            dict(original, absent_events=["Unknown"]),
            dict(original, absent_events=[1]),
            dict(original, absent_events=original["absent_events"] * 2),
            dict(original, absent_hooks="false"),
            dict(original, absent_hooks=True),
            dict(original, unknown=True),
            dict(
                original,
                scope=dict(
                    original["scope"],
                    installation_id="22222222-2222-4222-8222-222222222222",
                ),
            ),
            dict(original, scope=dict(original["scope"], unknown=True)),
            dict(original, scope=dict(original["scope"], collector_ref="changed")),
        ]
        cases += [
            {k: v for k, v in original.items() if k != field} for field in original
        ]
        raws = [json.dumps(value).encode() for value in cases]
        raws += [
            json.dumps(original)
            .replace('"version": 1', '"version": 1, "version": 1')
            .encode(),
            json.dumps(original)
            .replace('"timeout": 2', '"timeout": 2, "timeout": 2')
            .encode(),
        ]
        for raw in raws:
            with self.subTest(plan=raw):
                self.plan.write_bytes(raw)
                before = {
                    str(p.relative_to(self.root)): p.read_bytes()
                    for p in self.root.rglob("*")
                    if p.is_file()
                }
                self.command("claude-apply", self.plan, "--consent", code=2)
                after = {
                    str(p.relative_to(self.root)): p.read_bytes()
                    for p in self.root.rglob("*")
                    if p.is_file()
                }
                self.assertEqual(after, before)
        self.save(self.plan, original)
        self.command("claude-apply", self.plan, "--consent")
        # Type-valid receipt metadata and scope edits must also fail after install.
        for field, value in (
            ("absent_hooks", True),
            ("absent_events", list(EVENTS)),
            ("scope", dict(original["scope"], collector_ref="changed")),
            (
                "scope",
                dict(
                    original["scope"],
                    installation_id="22222222-2222-4222-8222-222222222222",
                ),
            ),
        ):
            self.save(self.plan, dict(original, **{field: value}))
            before = {
                str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            }
            self.command("claude-apply", self.plan, "--consent", code=2)
            self.command("claude-remove", self.plan, "--consent", code=2)
            self.hook()
            after = {
                str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            }
            self.assertEqual(after, before)
        self.save(self.plan, original)
        self.command("claude-remove", self.plan, "--consent")
        self.command("claude-remove", self.plan, "--consent")
        self.assertEqual(json.loads(self.settings.read_bytes()), self.original)

    def test_type_valid_original_presence_mismatch_is_atomic(self):
        self.command("claude-remove", self.plan, "--consent")
        for original, change in [
            (self.original, {"absent_events": list(EVENTS)}),
            ({}, {"absent_hooks": False}),
            ({"hooks": {}}, {"absent_hooks": True}),
        ]:
            with self.subTest(original=original, change=change):
                self.plan.unlink()
                self.save(self.settings, original)
                self.command("claude-plan", self.state, self.settings, self.plan)
                valid = json.loads(self.plan.read_bytes())
                self.save(self.plan, dict(valid, **change))
                before = {
                    str(p.relative_to(self.root)): p.read_bytes()
                    for p in self.root.rglob("*")
                    if p.is_file()
                }
                self.command("claude-apply", self.plan, "--consent", code=2)
                after = {
                    str(p.relative_to(self.root)): p.read_bytes()
                    for p in self.root.rglob("*")
                    if p.is_file()
                }
                self.assertEqual(before, after)
                self.save(self.plan, valid)
                self.command("claude-apply", self.plan, "--consent")
                self.command("claude-remove", self.plan, "--consent")
                self.assertEqual(json.loads(self.settings.read_bytes()), original)

    def test_python_reference_parity_and_dedup(self):
        py = self.root / "python"
        py.mkdir(mode=0o700)
        self.save(py / "registration.lock", {})
        self.save(
            py / "manifest.json",
            {
                "status": "installed",
                "installation_id": self.scope["installation_id"],
                "repository": str(self.repo),
                "vendor": "claude",
            },
        )
        for event in EVENTS:
            payload = dict(
                self.raw,
                hook_event_name=event,
                prompt_id="prompt-1",
                agent_id="child-1",
                agent_type="Explore",
                source="resume",
                model="model-fixture",
                reason="other",
            )
            for _ in range(2):
                self.hook(payload)
                observer_hook.collect(
                    py,
                    self.scope["installation_id"],
                    io.BytesIO(json.dumps(payload).encode()),
                )
        with sqlite3.connect(py / "observations.sqlite3") as db:
            reference = [
                json.loads(r[0])
                for r in db.execute("SELECT metadata FROM observations ORDER BY seq")
            ]
        actual = self.rows()
        self.assertEqual(len(actual), len(reference))
        for a, b in zip(actual, reference):
            for key in ("observation_id", "observed_at", "repository_ref"):
                a.pop(key, None)
            for key in (
                "observation_id",
                "observed_at",
                "repository",
                "source_tag",
                "actor_ref",
                "delivery",
                "deduplication",
            ):
                b.pop(key, None)
            self.assertEqual(a, b)
        self.assertEqual([r["event"] for r in actual].count("Stop"), 1)
        self.assertEqual([r["event"] for r in actual].count("SessionEnd"), 2)

    def test_empty_config_roundtrip_and_duplicate_owned_refusal(self):
        self.command("claude-remove", self.plan, "--consent")
        self.plan.unlink()
        self.save(self.settings, {})
        self.command("claude-plan", self.state, self.settings, self.plan)
        self.command("claude-apply", self.plan, "--consent")
        installed = self.settings.read_bytes()
        doc = json.loads(installed)
        doc["hooks"]["Stop"].append(doc["hooks"]["Stop"][0])
        self.save(self.settings, doc)
        self.command("claude-remove", self.plan, "--consent", code=2)
        self.hook()
        self.assertEqual(self.rows(), [])
        self.settings.write_bytes(installed)
        self.command("claude-remove", self.plan, "--consent")
        self.assertEqual(json.loads(self.settings.read_bytes()), {})

    def test_missing_ids_keep_distinct_firings_and_no_inferred_end(self):
        for event in ("SessionStart", "Stop", "SubagentStop", "SessionEnd"):
            for _ in range(2):
                self.hook(
                    {
                        "hook_event_name": event,
                        "session_id": "session-1",
                        "cwd": str(self.repo),
                        "stop_hook_active": True,
                        "reason": "other",
                    }
                )
        rows = self.rows()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({r["observation_id"] for r in rows}), 8)
        for event in ("SessionStart", "Stop", "SubagentStop", "SessionEnd"):
            self.assertEqual(sum(r["event"] == event for r in rows), 2)
        for row in rows:
            self.assertFalse(
                {"duration", "ended_at", "actor_ref", "stop_hook_active"} & row.keys()
            )
            if row["event"] != "SessionEnd":
                self.assertNotIn("reason", row)

    def test_fixture_resume_failure_child_correlation_and_repeated_starts(self):
        # Synthetic hook contract only: no claim about interactive Claude behavior.
        def emit(event, **fields):
            self.hook(
                dict(
                    hook_event_name=event,
                    session_id="parent-session",
                    cwd=str(self.repo),
                    **fields,
                )
            )

        for source in ("startup", "resume", "resume"):
            emit("SessionStart", source=source, model="fixture-model")
        for _ in range(2):
            emit("PreToolUse", tool_use_id="failed-read", tool_name="Read")
            emit(
                "PostToolUseFailure",
                tool_use_id="failed-read",
                tool_name="Read",
                error="PRIVATE_SENTINEL",
                tool_input={"path": "PRIVATE_SENTINEL"},
            )
            for child in ("child-a", "child-b"):
                emit("SubagentStart", agent_id=child, agent_type="Explore")
                emit(
                    "PreToolUse",
                    agent_id=child,
                    tool_use_id="same-tool",
                    tool_name="Read",
                )
                emit(
                    "PostToolUse",
                    agent_id=child,
                    tool_use_id="same-tool",
                    tool_name="Read",
                )
                emit("SubagentStop", agent_id=child, agent_type="Explore")
            emit("Stop", prompt_id="turn-one")
        rows = self.rows()
        self.assertEqual(len(rows), 14)
        self.assertEqual(
            [r["source"] for r in rows if r["event"] == "SessionStart"],
            ["startup", "resume", "resume"],
        )
        self.assertFalse(any(r["event"] == "SessionEnd" for r in rows))
        for child in ("child-a", "child-b"):
            child_rows = [r for r in rows if r.get("agent_id") == child]
            self.assertEqual(
                [r["event"] for r in child_rows],
                ["SubagentStart", "PreToolUse", "PostToolUse", "SubagentStop"],
            )
            self.assertTrue(
                all(r["session_id"] == "parent-session" for r in child_rows)
            )
        failed = [r for r in rows if r["event"] == "PostToolUseFailure"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["tool_use_id"], "failed-read")
        self.assertTrue(
            all(
                not {"duration", "ended_at", "actor_ref", "error"} & r.keys()
                for r in rows
            )
        )
        emit("SessionEnd", reason="other")
        self.assertEqual(len(self.rows()), 15)
        for file in self.state.iterdir():
            if file.is_file():
                self.assertNotIn(b"PRIVATE_SENTINEL", file.read_bytes())

    def test_fixture_exception_cleanup_disables_later_lifecycle(self):
        try:
            self.hook(dict(self.raw, hook_event_name="SessionStart", source="startup"))
            raise RuntimeError("synthetic host failure")
        except RuntimeError:
            pass
        finally:
            self.command("claude-remove", self.plan, "--consent")
        self.assertEqual(json.loads(self.settings.read_bytes()), self.original)
        before = self.rows()
        for event in EVENTS:
            self.hook(dict(self.raw, hook_event_name=event))
        self.assertEqual(self.rows(), before)
        self.assertEqual([r["event"] for r in before], ["SessionStart"])

    def test_invalid_scope_and_payload_are_neutral(self):
        outside = self.root / "repo-other"
        outside.mkdir()
        (self.repo / "escape").symlink_to(outside, target_is_directory=True)
        cases = [
            dict(self.raw, cwd=str(outside)),
            dict(self.raw, cwd=str(self.repo / "escape")),
            dict(self.raw, cwd="relative"),
            dict(self.raw, hook_event_name="Unknown"),
            dict(self.raw, session_id="bad token"),
            dict(self.raw, prompt_id=None),
            dict(self.raw, tool_use_id="x" * 201),
        ]
        for value in cases:
            self.hook(value)
        for raw in (
            b"{invalid",
            b"[]",
            b"x" * 65537,
            b'{"session_id":"one","session_id":"two"}',
        ):
            self.hook(raw=raw)
        self.assertEqual(self.rows(), [])
        child = self.repo / "sub"
        child.mkdir()
        self.hook(dict(self.raw, cwd=str(child)))
        self.assertEqual(len(self.rows()), 1)

    def test_open_stdin_deadline_and_locked_spool(self):
        start = time.monotonic()
        process = subprocess.Popen(
            [str(self.exe), "claude-hook", str(self.plan)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            self.assertEqual(process.wait(timeout=1.5), 0)
            self.assertLess(time.monotonic() - start, 1.3)
            self.assertEqual(process.stdout.read(), b"")
            self.assertEqual(process.stderr.read(), b"")
        finally:
            process.kill() if process.poll() is None else None
            process.communicate()
        with sqlite3.connect(self.state / "collector-rust-v1.sqlite3") as db:
            db.execute("BEGIN EXCLUSIVE")
            self.hook()
        self.assertEqual(self.rows(), [])
        self.hook()
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_dedup_and_registration_lock(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.hook(), range(12)))
        self.assertEqual(len(self.rows()), 1)
        with (self.root / ".devdiary-rust-claude.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.hook(dict(self.raw, tool_use_id="locked"))
            self.command("claude-remove", self.plan, "--consent", code=2)
        self.assertEqual(len(self.rows()), 1)

    def test_consent_stale_plan_owned_entry_and_unrelated_edits(self):
        self.command("claude-remove", self.plan, code=2)
        config = json.loads(self.settings.read_bytes())
        config["new_customer_setting"] = True
        extra = {"hooks": [{"type": "command", "command": "true"}]}
        config["hooks"]["Stop"].append(extra)
        self.save(self.settings, config)
        self.command("claude-remove", self.plan, "--consent")
        expected = dict(self.original, new_customer_setting=True)
        expected["hooks"]["Stop"].append(extra)
        self.assertEqual(json.loads(self.settings.read_bytes()), expected)
        self.command("claude-apply", self.plan, "--consent", code=2)
        self.plan.unlink()
        self.command("claude-plan", self.state, self.settings, self.plan)
        self.command("claude-apply", self.plan, code=2)
        self.command("claude-apply", self.plan, "--consent")
        config = json.loads(self.settings.read_bytes())
        config["hooks"]["Stop"][-1]["hooks"][0]["timeout"] = 10
        self.save(self.settings, config)
        before = self.settings.read_bytes()
        self.command("claude-remove", self.plan, "--consent", code=2)
        self.assertEqual(self.settings.read_bytes(), before)
        self.hook()
        self.assertEqual(self.rows(), [])

    def test_changed_executable_scope_and_permissions(self):
        self.exe.chmod(0o755)
        self.hook()
        self.assertEqual(self.rows(), [])
        self.exe.chmod(0o700)
        with self.exe.open("ab") as f:
            f.write(b"changed")
        self.hook()
        self.assertEqual(self.rows(), [])
        self.command("claude-remove", self.plan, "--consent")
        self.assertEqual(json.loads(self.settings.read_bytes()), self.original)
        self.command("claude-apply", self.plan, "--consent", code=2)

    def test_setup_crash_and_retry_keeps_atomic_settings(self):
        # Real process death at varying points, never production fault switches.
        self.command("claude-remove", self.plan, "--consent")
        for delay in (0, 0.01, 0.04, 0.08):
            self.plan.unlink()
            self.command("claude-plan", self.state, self.settings, self.plan)
            process = subprocess.Popen(
                [str(self.exe), "claude-apply", str(self.plan), "--consent"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            time.sleep(delay)
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=2)
            current = json.loads(self.settings.read_bytes())
            if current != self.original:
                entry = json.loads(self.plan.read_bytes())["entry"]
                self.assertTrue(
                    all(current["hooks"][event].count(entry) == 1 for event in EVENTS)
                )
            # Retry works both before and after the atomic rename.
            self.command("claude-apply", self.plan, "--consent")
            self.hook()
            self.command("claude-remove", self.plan, "--consent")
            self.assertEqual(json.loads(self.settings.read_bytes()), self.original)
        with sqlite3.connect(self.state / "collector-rust-v1.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_scope_changed_and_untrusted_registration_files(self):
        saved_plan = self.plan.read_bytes()
        proposal = json.loads(saved_plan)
        proposal["scope"]["collector_ref"] = "changed"
        self.save(self.plan, proposal)
        self.hook()
        self.assertEqual(self.rows(), [])
        self.plan.write_bytes(saved_plan)
        self.plan.chmod(0o644)
        self.hook()
        self.assertEqual(self.rows(), [])
        self.plan.chmod(0o600)
        alias = self.root / "hardlink"
        os.link(self.plan, alias)
        self.hook()
        self.assertEqual(self.rows(), [])
        alias.unlink()
        self.command("claude-remove", self.plan, "--consent")
        original = self.settings.read_bytes()
        self.plan.unlink()
        self.settings.write_bytes(
            b'{"hooks":{},"permissions":{"allow":[],"allow":["Write"]}}'
        )
        self.command("claude-plan", self.state, self.settings, self.plan, code=2)
        self.assertFalse(self.plan.exists())
        self.settings.write_bytes(original)
        self.command("claude-plan", self.state, self.settings, self.plan)
        self.save(self.settings, dict(self.original, edit_after_plan=True))
        before = self.settings.read_bytes()
        self.command("claude-apply", self.plan, "--consent", code=2)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_full_spool_neutral_without_mutation(self):
        # Exercise the real conservative admission boundary, not synthetic rows.
        row = json.loads((ROOT / "tests/fixtures/local-observation.json").read_bytes())
        row.update(
            repository=str(self.repo), installation_id=self.scope["installation_id"]
        )
        import uuid

        for _ in range(700):
            row["observation_id"] = str(uuid.uuid4())
            result = subprocess.run(
                [str(self.exe), "collect", str(self.state)],
                input=json.dumps(row).encode(),
                capture_output=True,
                check=False,
                timeout=3,
            )
            if result.returncode:
                break
        else:
            self.fail("expected conservative capacity refusal")
        before = self.rows()
        self.assertGreater(len(before), 100)
        self.hook()
        self.assertEqual(self.rows(), before)
        self.assertLessEqual(
            (self.state / "collector-rust-v1.sqlite3").stat().st_size, 16 * 1024 * 1024
        )

    def test_hook_to_real_cli_http_delivery(self):
        owner = self
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                received.append(payload)
                self.send_response(201)
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "collector_ref": owner.scope["collector_ref"],
                            "observation_id": payload["observation_id"],
                            "installation_id": payload["installation_id"],
                            "record_id": 1,
                        }
                    ).encode()
                )

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            # New isolated scope rather than retargeting an existing spool.
            self.command("claude-remove", self.plan, "--consent")
            self.plan.unlink()
            self.state = self.root / "http-state"
            self.state.mkdir(mode=0o700)
            self.scope["endpoint"] = (
                f"http://127.0.0.1:{server.server_port}/ingest/v1/observations"
            )
            self.command("init", self.state, "--consent", data=self.scope)
            self.command("claude-plan", self.state, self.settings, self.plan)
            self.command("claude-apply", self.plan, "--consent")
            self.hook()
            key = self.root / "synthetic-key"
            key.write_text("dc_live_SYNTHETIC_ONLY")
            key.chmod(0o600)
            self.command("sync", self.state, key, "10")
            self.assertEqual(received, self.rows())
            self.assertEqual(
                json.loads(self.command("status", self.state).stdout),
                {"pending": 0, "delivered": 1},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
