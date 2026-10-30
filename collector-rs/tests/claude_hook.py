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
import uuid
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

    def hook(self, data=None, raw=None, plan=None):
        result = subprocess.run(
            [str(self.exe), "claude-hook", str(plan or self.plan)],
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

    def drops(self):
        return json.loads(self.command("status", self.state).stdout)["drops"]

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
                if p.is_file() and p.name != "drops.log"
            }
            before.pop("state/drops.log", None)
            self.assertEqual(after.keys(), before.keys())
            self.assertTrue(all(after[k] == before[k] for k in before))
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
        self.assertEqual(self.drops(), {"outside_scope": 2, "invalid": 9})
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
        with (self.state / "claude-settings.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.hook(dict(self.raw, tool_use_id="locked"))
            self.command("claude-remove", self.plan, "--consent", code=2)
        self.assertEqual(len(self.rows()), 1)
        drops = self.drops()
        self.assertGreaterEqual(drops.pop("busy"), 1)
        self.assertLessEqual(drops.keys(), {"observation_conflict", "busy"})

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

        for _ in range(1200):
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

    def test_outside_scope_event_counts_a_drop_without_a_row(self):
        outside = self.root / "repo-other"
        outside.mkdir()
        self.hook(dict(self.raw, cwd=str(outside)))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.drops(), {"outside_scope": 1})

    def test_settings_readable_by_others_but_writable_only_by_owner(self):
        self.command("claude-remove", self.plan, "--consent")
        home = self.root / "home"
        home.mkdir(mode=0o700)
        claude = home / ".claude"
        claude.mkdir()
        claude.chmod(0o755)
        settings = claude / "settings.json"
        settings.write_text(json.dumps(self.original))
        settings.chmod(0o644)
        plan = self.root / "shared-plan.json"
        self.command("claude-plan", self.state, settings, plan)
        self.command("claude-apply", plan, "--consent")
        self.assertEqual(settings.stat().st_mode & 0o777, 0o644)
        self.hook(plan=plan)
        self.assertEqual(len(self.rows()), 1)
        for path, mode in ((settings, 0o664), (settings, 0o777), (claude, 0o777)):
            with self.subTest(path=path.name, mode=oct(mode)):
                original = path.stat().st_mode & 0o777
                path.chmod(mode)
                self.hook(dict(self.raw, tool_use_id=f"mode-{mode}"), plan=plan)
                self.command("claude-remove", plan, "--consent", code=2)
                path.chmod(original)
        alias = home / "settings-alias.json"
        os.link(settings, alias)
        self.hook(dict(self.raw, tool_use_id="hardlinked"), plan=plan)
        self.command("claude-remove", plan, "--consent", code=2)
        alias.unlink()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.drops(), {"permission": 4})
        linked = self.root / "linked-home"
        linked.symlink_to(home, target_is_directory=True)
        self.command(
            "claude-plan",
            self.state,
            linked / ".claude/settings.json",
            self.root / "linked-plan.json",
            code=2,
        )
        self.command("claude-remove", plan, "--consent")
        self.assertEqual(json.loads(settings.read_bytes()), self.original)
        self.assertEqual(settings.stat().st_mode & 0o777, 0o644)

    def test_broken_settings_count_as_stale_plan_not_invalid_events(self):
        text = self.settings.read_text()
        for broken in ('{"permissions": {}, ' + text[1:], text[:-1]):
            with self.subTest(broken=broken[:20]):
                self.settings.write_text(broken)
                self.hook()
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.drops(), {"stale_plan": 2})

    def test_python_observer_registration_is_refused(self):
        self.command("claude-remove", self.plan, "--consent")
        self.plan.unlink()
        observer = {"type": "command", "command": "python3 /opt/observer_hook.py"}
        self.save(
            self.settings,
            dict(self.original, hooks={"Stop": [{"hooks": [observer]}]}),
        )
        result = self.command(
            "claude-plan", self.state, self.settings, self.plan, code=2
        )
        self.assertIn(b"python_observer_registered", result.stderr)
        self.assertFalse(self.plan.exists())

    def test_replaced_binary_is_rehashed_within_the_hook_deadline(self):
        self.assertGreater(self.exe.stat().st_size, 32 * 1024 * 1024)
        fingerprint = self.state / "executable-fingerprint"
        self.hook()
        before = fingerprint.read_bytes()
        replacement = self.root / "replacement"
        shutil.copyfile(ROOT / "target/debug/devdiary-collector", replacement)
        replacement.chmod(0o700)
        os.replace(replacement, self.exe)
        self.hook(dict(self.raw, tool_use_id="after-replace"))
        self.assertEqual(len(self.rows()), 2)
        self.assertNotEqual(fingerprint.read_bytes(), before)
        with self.exe.open("ab") as f:
            f.write(b"changed")
        self.hook(dict(self.raw, tool_use_id="after-change"))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.drops(), {"stale_plan": 1})

    def test_unrelated_revoked_entry_beside_a_plain_state_is_ignored(self):
        (self.root / "revoked").write_text("unrelated")
        self.hook()
        (self.root / "revoked").unlink()
        (self.root / "revoked").mkdir()
        self.hook(dict(self.raw, tool_use_id="beside-directory"))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.drops(), {})

    def test_large_tool_payload_is_skipped_up_to_four_mebibytes(self):
        self.hook(dict(self.raw, tool_response="PRIVATE_SENTINEL" * 200_000))
        self.assertEqual(len(self.rows()), 1)
        self.hook(raw=b'{"pad":"' + b"x" * (4 * 1024 * 1024) + b'"}')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.drops(), {"invalid": 1})
        for file in self.state.iterdir():
            self.assertNotIn(b"PRIVATE_SENTINEL", file.read_bytes())

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
            spooled = self.rows()
            key = self.root / "synthetic-key"
            key.write_text("dc_live_SYNTHETIC_ONLY")
            key.chmod(0o600)
            (self.state / "executable-fingerprint").unlink()
            self.command("sync", self.state, key, "10")
            self.assertTrue((self.state / "executable-fingerprint").exists())
            self.assertEqual(received, spooled)
            self.assertEqual(self.rows(), [])
            self.assertEqual(
                json.loads(self.command("status", self.state).stdout),
                {"pending": 0, "delivered": 1, "drops": {}},
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


AGENT = "22c99d01-c3ee-4041-9066-0a83f34bd2e2"
RUN = "66666666-6666-4666-8666-666666666666"
TASK = "77777777-7777-4777-8777-777777777777"
CANARIES = (
    "PAPERCLIP_API_KEY",
    "PAPERCLIP_AGENT_PRIVATE_KEY",
    "TRUSTGROWTH_API_KEY",
    "WRIGHTBOT_REVIEW_SIGNOFF_TOKEN",
)


GIT_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@t.invalid")


def git(*args, identity=GIT_IDENTITY):
    return (
        subprocess.run(
            ["/usr/bin/git", "-c", "commit.gpgsign=false", *identity, *map(str, args)],
            check=True,
            capture_output=True,
            env={**os.environ, "GIT_EDITOR": "true", "GIT_CONFIG_GLOBAL": "/dev/null"},
        )
        .stdout.decode()
        .strip()
    )


class InstallationFixture:
    """An installation-scope connection built locally against a fake ingest server."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="installation-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.exe = self.root / "collector"
        shutil.copyfile(ROOT / "target/debug/devdiary-collector", self.exe)
        self.exe.chmod(0o700)
        self.main = self.repo("main", "git@github.com:acme/demo.git")
        self.refs = ["https://github.com/acme/demo"]
        self.received = []
        self.served_refs = list(self.refs)
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
                if self.path != "/ingest/v1/collector/scope":
                    return self.reply(404, {})
                self.reply(
                    200,
                    {
                        "schema_version": 1,
                        "collector_ref": owner.collector_ref,
                        "scope_kind": "installation",
                        "repository_refs": owner.served_refs,
                        "observation_schema_versions": [1, 2],
                    },
                )

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                owner.received.append(raw)
                sent = json.loads(raw)
                self.reply(
                    201,
                    {
                        "collector_ref": owner.collector_ref,
                        "observation_id": sent["observation_id"],
                        "installation_id": sent["installation_id"],
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
        self.collector_ref = (
            "urn:devdiary:collector:" + "1" * 8 + "-1111-4111-8111-" + "1" * 12
        )

    def repo(self, name, url):
        path = self.root / name
        git("init", "-q", path)
        git("-C", path, "remote", "add", "origin", url)
        git("-C", path, "commit", "-q", "--allow-empty", "-m", "init")
        return path

    def connect(self, opt_in=False, versions=(1, 2)):
        self.connection = self.root / "connection"
        self.connection.mkdir(mode=0o700)
        installation = {
            "installation_id": str(uuid.uuid4()),
            "origin": self.origin,
            "scope_kind": "installation",
            "record_orchestrated_outside_repos": opt_in,
        }
        scope = {
            "endpoint": self.origin + "/ingest/v1/observations",
            "collector_ref": self.collector_ref,
            "installation_id": installation["installation_id"],
            "scope_kind": "installation",
            "observation_schema_versions": list(versions),
            "record_orchestrated_outside_repos": opt_in,
        }
        self.write(self.connection / "installation.json", installation)
        self.write(
            self.connection / "connection.json",
            {
                "scope": scope,
                "token": "dc_live_" + "s" * 43,
                "repository_refs": self.refs,
            },
        )
        self.settings = self.root / "settings.json"
        self.write(self.settings, {})
        self.plan = self.root / "plan.json"
        self.run_collector("setup-plan", self.connection, self.settings, self.plan)
        self.run_collector("claude-apply", self.plan, "--consent")
        self.outbox = self.connection / "outbox"

    def write(self, path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def run_collector(self, *args, code=0, data=None, env=None, exe=None):
        result = subprocess.run(
            [str(exe or self.exe), *map(str, args)],
            input=data,
            capture_output=True,
            check=False,
            timeout=10,
            env=env,
        )
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def env(self, **extra):
        base = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PAPERCLIP_", "TRUSTGROWTH_", "WRIGHTBOT_", "TG_"))
        }
        return {**base, **extra}

    def hook(
        self,
        cwd,
        tool_use_id="tool-1",
        exe=None,
        event="PostToolUse",
        session="session-1",
        tool="Bash",
        **env,
    ):
        event = {
            "hook_event_name": event,
            "session_id": session,
            "cwd": str(cwd),
            "tool_use_id": tool_use_id,
            "tool_name": tool,
            "tool_input": {"command": "PRIVATE_SENTINEL"},
        }
        result = self.run_collector(
            "claude-hook",
            self.plan,
            data=json.dumps(event).encode(),
            env=self.env(**env),
            exe=exe,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))

    def rows(self):
        with sqlite3.connect(self.outbox / "collector-rust-v1.sqlite3") as db:
            return [
                json.loads(r[0])
                for r in db.execute("SELECT payload FROM outbox ORDER BY seq")
            ]

    def drops(self):
        status = self.run_collector("connection-status", self.connection)
        return json.loads(status.stdout)["local_drops"]

    def sync(self, code=0):
        return self.run_collector("connection-sync", self.connection, "100", code=code)


class InstallationScopeHookTest(InstallationFixture, unittest.TestCase):
    def test_sibling_worktrees_of_an_allowed_repository_are_captured(self):
        self.connect()
        nested = self.main / ".worktrees/x"
        external = self.root / "home/.t3/worktrees/demo/y"
        git("-C", self.main, "worktree", "add", "-q", nested)
        git("-C", self.main, "worktree", "add", "-q", external)
        for i, cwd in enumerate((self.main, nested, external / "sub")):
            cwd.mkdir(exist_ok=True)
            self.hook(cwd, tool_use_id=f"tool-{i}")
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["repository_ref"], "https://github.com/acme/demo")
            self.assertEqual(row["schema_version"], 2)
            self.assertEqual(row["orchestrator"], "none")
            self.assertNotIn("actor_ref", row)
            self.assertNotIn("repository", row)
        self.assertEqual(self.drops(), {})

    def test_unknown_repository_is_dropped_without_a_row(self):
        self.connect()
        other = self.repo("other", "https://github.com/acme/other.git")
        self.hook(other)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.drops(), {"unknown_repository": 1})

    def test_a_large_config_with_non_utf8_bytes_still_resolves(self):
        config = self.main / ".git/config"
        branches = "".join(
            f'[branch "f/{i}"]\n\tremote = origin\n\tmerge = refs/heads/f/{i}\n'
            for i in range(3000)
        )
        config.write_bytes(
            branches.encode() + config.read_bytes() + b"[user]\n\tname = Ren\xe9\n"
        )
        self.connect()
        self.hook(self.main)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.drops(), {})

    def test_repository_refs_match_the_server_spelling(self):
        self.refs = ["https://github.com/Acme/Demo"]
        self.connect()
        self.hook(self.main)
        self.assertEqual(
            self.rows()[0]["repository_ref"], "https://github.com/Acme/Demo"
        )

    def test_non_repository_cwd_needs_paperclip_and_the_opt_in(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.connect()
        self.hook(plain, tool_use_id="no-env")
        self.hook(plain, tool_use_id="paperclip", PAPERCLIP_AGENT_ID=AGENT)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.drops(), {"outside_scope": 2})

    def test_opted_in_paperclip_run_outside_a_repository_is_recorded_without_one(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.connect(opt_in=True)
        self.hook(plain, tool_use_id="no-env")
        self.hook(
            plain,
            PAPERCLIP_AGENT_ID=AGENT,
            PAPERCLIP_RUN_ID=RUN,
            PAPERCLIP_TASK_ID=TASK.upper(),
        )
        (row,) = self.rows()
        self.assertNotIn("repository_ref", row)
        self.assertEqual(row["orchestrator"], "paperclip")
        self.assertEqual(row["orchestrator_agent_ref"], AGENT)
        self.assertEqual(row["orchestrator_run_ref"], RUN)
        self.assertEqual(row["orchestrator_task_ref"], TASK)
        self.assertEqual(self.drops(), {"outside_scope": 1})

    def test_paperclip_workspace_path_is_a_local_checkout_hint_only(self):
        plain = self.root / "plain"
        plain.mkdir()
        tree = self.root / "paperclip-ws-HINTMARKER"
        git("-C", self.main, "worktree", "add", "-q", tree)
        self.connect()
        self.hook(
            plain, tool_use_id="hint-without-agent", PAPERCLIP_WORKSPACE_CWD=str(tree)
        )
        self.hook(
            plain,
            tool_use_id="hint",
            PAPERCLIP_AGENT_ID=AGENT,
            PAPERCLIP_WORKSPACE_WORKTREE_PATH=str(self.root / "missing"),
            PAPERCLIP_WORKSPACE_CWD=str(tree),
        )
        (row,) = self.rows()
        self.assertEqual(row["repository_ref"], "https://github.com/acme/demo")
        self.assertEqual(row["orchestrator_agent_ref"], AGENT)
        self.sync()
        self.assertNotIn(b"HINTMARKER", b"".join(self.received))

    def test_malformed_or_partial_paperclip_ids_never_read_as_interactive(self):
        self.connect()
        self.hook(self.main, tool_use_id="bad-agent", PAPERCLIP_AGENT_ID="agent-7")
        self.hook(self.main, tool_use_id="run-only", PAPERCLIP_RUN_ID=RUN)
        rows = self.rows()
        self.assertEqual([r["orchestrator"] for r in rows], ["paperclip", "paperclip"])
        self.assertNotIn("orchestrator_agent_ref", rows[0])
        self.assertEqual(rows[1]["orchestrator_run_ref"], RUN)

    def test_secret_environment_never_reaches_spool_or_wire(self):
        plain = self.root / "plain"
        plain.mkdir()
        self.connect(opt_in=True)
        secrets = {name: "SENTINELSECRET" for name in CANARIES}
        hints = {
            "PAPERCLIP_WORKSPACE_CWD": str(self.root / "ws-cwd-PATHCANARY"),
            "PAPERCLIP_WORKSPACE_WORKTREE_PATH": str(self.main),
        }
        self.hook(self.main, tool_use_id="repo", **secrets, **hints)
        self.hook(
            plain, tool_use_id="hinted", PAPERCLIP_AGENT_ID=AGENT, **secrets, **hints
        )
        self.hook(
            plain,
            tool_use_id="bare",
            PAPERCLIP_AGENT_ID=AGENT,
            **secrets,
            PAPERCLIP_WORKSPACE_CWD=str(self.root / "ws-cwd-PATHCANARY"),
        )
        self.assertEqual(len(self.rows()), 3)
        for file in self.outbox.iterdir():
            raw = file.read_bytes()
            self.assertNotIn(b"SENTINELSECRET", raw)
            self.assertNotIn(b"PRIVATE_SENTINEL", raw)
        self.sync()
        wire = b"".join(self.received)
        self.assertEqual(len(self.received), 3)
        for value in [
            "SENTINELSECRET",
            *hints.values(),
            str(plain),
            "PRIVATE_SENTINEL",
        ]:
            self.assertNotIn(value.encode(), wire)

    def test_sync_refreshes_repository_refs_from_the_scope_endpoint(self):
        self.connect()
        other = self.repo("other", "https://github.com/acme/other.git")
        self.hook(other, tool_use_id="before")
        self.served_refs = ["https://github.com/acme/other"]
        self.sync()
        self.hook(other, tool_use_id="after")
        self.hook(self.main, tool_use_id="removed")
        self.assertEqual(
            [r["repository_ref"] for r in self.rows()],
            ["https://github.com/acme/other"],
        )
        self.assertEqual(self.drops(), {"unknown_repository": 2})

    def test_server_without_v2_gets_v1_rows(self):
        self.connect(versions=(1,))
        self.hook(self.main, PAPERCLIP_AGENT_ID=AGENT)
        (row,) = self.rows()
        self.assertEqual(row["schema_version"], 1)
        self.assertEqual(row["repository_ref"], "https://github.com/acme/demo")
        self.assertFalse({"orchestrator", "orchestrator_agent_ref"} & row.keys())

    def test_a_v1_wire_never_marks_or_links_commits(self):
        self.connect(versions=(1,))
        self.hook(self.main, "commit", event="PreToolUse")
        Path(self.main / "file").write_text("x")
        git("-C", self.main, "add", "file")
        git("-C", self.main, "commit", "-q", "-m", "change")
        self.hook(self.main, "commit")
        for row in self.rows():
            self.assertFalse({"commits", "overlap_count", "commit_gap"} & row.keys())
        with sqlite3.connect(self.outbox / "collector-rust-v1.sqlite3") as db:
            (marks,) = db.execute("SELECT count(*) FROM bracket_marks").fetchone()
        self.assertEqual(marks, 0)

    def test_out_of_scope_hook_p99_on_the_release_build(self):
        release = ROOT / "target/release/devdiary-collector"
        if not release.exists():
            self.skipTest("cargo build --release first")
        exe = self.root / "release-collector"
        shutil.copyfile(release, exe)
        exe.chmod(0o700)
        self.exe = exe
        self.connect()
        other = self.repo("other", "https://github.com/acme/other.git")
        timings = []
        for i in range(200):
            start = time.perf_counter()
            self.hook(other, tool_use_id=f"t{i}")
            timings.append(time.perf_counter() - start)
        timings.sort()
        p99 = timings[int(len(timings) * 0.99) - 1]
        print(f"out-of-scope hook p99 {p99 * 1000:.1f} ms over {len(timings)} runs")
        self.assertLess(p99, 0.050)
        self.assertEqual(self.rows(), [])


BRACKET_FIELDS = {"commits", "overlap_count", "commit_gap"}
HOUR_NS = 3_600_000_000_000


class CommitBracketHookTest(InstallationFixture, unittest.TestCase):
    """Commits are linked only from one Bash tool call's own checkout reflog."""

    def setUp(self):
        super().setUp()
        self.connect()

    def post(self, tool_use_id, session="session-1", event="PostToolUse"):
        (row,) = [
            r
            for r in self.rows()
            if r["event"] == event
            and r["tool_use_id"] == tool_use_id
            and r["session_id"] == session
        ]
        return row

    def bracket(self, cwd, tool_use_id, action, session="session-1", **env):
        self.hook(cwd, tool_use_id, event="PreToolUse", session=session, **env)
        action()
        self.hook(cwd, tool_use_id, session=session, **env)
        return self.post(tool_use_id, session)

    def change(self, cwd=None, name="file", message="change"):
        cwd = cwd or self.main
        path = Path(cwd) / name
        path.write_text(path.read_text() + "x\n" if path.exists() else "x\n")
        git("-C", cwd, "add", name)
        git("-C", cwd, "commit", "-q", "-m", message)
        return self.head(cwd)

    def head(self, cwd=None):
        return git("-C", cwd or self.main, "rev-parse", "HEAD")

    def assert_captured(self, row, shas, overlap=0):
        self.assertEqual(row["commits"], shas)
        self.assertEqual(row["overlap_count"], overlap)
        self.assertNotIn("commit_gap", row)

    def assert_nothing(self, row):
        self.assertFalse(BRACKET_FIELDS & row.keys(), row)

    def marks(self):
        with sqlite3.connect(self.outbox / "collector-rust-v1.sqlite3") as db:
            return [r[0] for r in db.execute("SELECT tool_use_id FROM bracket_marks")]

    def test_commit_amend_cherry_pick_and_rebase_pick_emit_their_sha(self):
        row = self.bracket(self.main, "commit", lambda: self.change())
        self.assert_captured(row, [self.head()])

        amend = lambda: git("-C", self.main, "commit", "-q", "--amend", "-m", "amended")
        row = self.bracket(self.main, "amend", amend)
        self.assert_captured(row, [self.head()])

        git("-C", self.main, "checkout", "-q", "-b", "topic")
        picked = self.change(name="picked")
        git("-C", self.main, "checkout", "-q", "-")
        pick = lambda: git("-C", self.main, "cherry-pick", picked)
        row = self.bracket(self.main, "cherry-pick", pick)
        self.assert_captured(row, [self.head()])

        git("-C", self.main, "checkout", "-q", "-b", "rebased", "HEAD~1")
        self.change(name="rebased")
        main_branch = git("-C", self.main, "rev-parse", "--abbrev-ref", "@{-1}")
        rebase = lambda: git("-C", self.main, "rebase", "-q", main_branch)
        row = self.bracket(self.main, "rebase", rebase)
        self.assert_captured(row, [self.head()])
        self.assertEqual(self.drops(), {})

    def test_revert_and_a_non_fast_forward_merge_emit_their_sha(self):
        self.change()
        revert = lambda: git("-C", self.main, "revert", "--no-edit", "HEAD")
        self.assert_captured(self.bracket(self.main, "revert", revert), [self.head()])
        git("-C", self.main, "checkout", "-q", "-b", "side", "HEAD~1")
        self.change(name="side")
        git("-C", self.main, "checkout", "-q", "-")
        merge = lambda: git(
            "-C", self.main, "merge", "-q", "--no-ff", "--no-edit", "side"
        )
        self.assert_captured(self.bracket(self.main, "merge", merge), [self.head()])

    def test_a_failed_bash_call_still_closes_its_bracket(self):
        self.hook(self.main, "failed", event="PreToolUse")
        sha = self.change()
        self.hook(self.main, "failed", event="PostToolUseFailure")
        self.assert_captured(self.post("failed", event="PostToolUseFailure"), [sha])
        self.assertEqual(self.marks(), ["failed"])
        self.hook(self.main, "failed")
        self.assert_nothing(self.post("failed"))

    def test_checkout_reset_and_fast_forward_merge_emit_none(self):
        self.change()
        git("-C", self.main, "checkout", "-q", "-b", "ahead")
        self.change(name="ahead")
        git("-C", self.main, "checkout", "-q", "-")
        switch = lambda: (
            git("-C", self.main, "checkout", "-q", "ahead"),
            git("-C", self.main, "checkout", "-q", "-"),
        )
        self.assert_nothing(self.bracket(self.main, "checkout", switch))
        reset = lambda: git("-C", self.main, "reset", "-q", "--hard", "HEAD~1")
        self.assert_nothing(self.bracket(self.main, "reset", reset))
        merge = lambda: git("-C", self.main, "merge", "-q", "--ff-only", "ahead")
        self.assert_nothing(self.bracket(self.main, "merge", merge))
        self.assertEqual(self.head(), git("-C", self.main, "rev-parse", "ahead"))

    def test_a_commit_between_tool_calls_emits_nothing(self):
        self.change()
        self.assert_nothing(self.bracket(self.main, "after", lambda: None))
        self.change(name="unbracketed")
        self.hook(self.main, "post-without-pre")
        self.assert_nothing(self.post("post-without-pre"))

    def test_a_commit_in_another_checkout_during_the_bracket_emits_nothing(self):
        sibling = self.root / "sibling"
        git("-C", self.main, "worktree", "add", "-q", sibling)
        row = self.bracket(self.main, "elsewhere", lambda: self.change(sibling))
        self.assert_nothing(row)

    def test_overlapping_brackets_on_one_checkout_are_both_counted(self):
        self.hook(self.main, "a", event="PreToolUse", session="session-a")
        self.hook(self.main, "b", event="PreToolUse", session="session-b")
        first = self.change(name="a")
        self.hook(self.main, "a", session="session-a")
        second = self.change(name="b")
        self.hook(self.main, "b", session="session-b")
        a, b = self.post("a", "session-a"), self.post("b", "session-b")
        self.assertEqual(a["commits"], [first])
        self.assertIn(second, b["commits"])
        self.assertGreaterEqual(a["overlap_count"], 1)
        self.assertGreaterEqual(b["overlap_count"], 1)

    def test_parallel_calls_in_one_session_are_not_overlap(self):
        self.hook(self.main, "status", event="PreToolUse")
        row = self.bracket(self.main, "commit", lambda: self.change())
        self.hook(self.main, "status")
        self.assert_captured(row, [self.head()])

    def end_turn(self, session, event):
        event = {"hook_event_name": event, "session_id": session, "cwd": str(self.main)}
        if event["hook_event_name"] != "SessionEnd":
            event["prompt_id"] = f"{session}-prompt"
        result = self.run_collector(
            "claude-hook", self.plan, data=json.dumps(event).encode(), env=self.env()
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))

    def age(self, tool_use_id, nanos):
        with sqlite3.connect(self.outbox / "collector-rust-v1.sqlite3") as db:
            db.execute(
                "UPDATE bracket_marks SET opened_at = opened_at - ? WHERE tool_use_id = ?",
                (nanos, tool_use_id),
            )

    def test_a_call_waiting_long_for_permission_still_counts_as_overlap(self):
        self.hook(self.main, "waiting", event="PreToolUse", session="session-a")
        self.age("waiting", HOUR_NS // 3)
        self.hook(self.main, "b", event="PreToolUse", session="session-b")
        landed = self.change(name="waiting")
        self.hook(self.main, "b", session="session-b")
        self.hook(self.main, "waiting", session="session-a")
        self.assert_captured(self.post("b", "session-b"), [landed], overlap=1)
        self.assert_captured(self.post("waiting", "session-a"), [landed], overlap=1)

    def test_an_unclosed_call_stops_counting_when_its_session_ends_the_turn(self):
        for end in ("Stop", "UserPromptSubmit", "SessionEnd"):
            with self.subTest(end=end):
                other = f"session-{end}"
                self.hook(self.main, end, event="PreToolUse", session=other)
                row = self.bracket(
                    self.main, f"{end}-soon", lambda name=end: self.change(name=name)
                )
                self.assert_captured(row, [self.head()], overlap=1)
                self.end_turn(other, end)
                row = self.bracket(
                    self.main, f"{end}-later", lambda name=end: self.change(name=name)
                )
                self.assert_captured(row, [self.head()])
                self.hook(self.main, end, session=other)
                self.assert_nothing(self.post(end, other))

    def test_a_non_bash_tool_call_never_marks_or_links(self):
        for event in ("PreToolUse", "PostToolUse"):
            self.hook(self.main, "edit", event=event, tool="Edit")
            if event == "PreToolUse":
                self.change()
        self.assertEqual(self.marks(), [])
        self.assert_nothing(self.post("edit"))

    def test_concurrent_brackets_on_one_checkout_drop_nothing(self):
        sessions = [f"session-{i}" for i in range(24)]
        with concurrent.futures.ThreadPoolExecutor(len(sessions)) as pool:
            for event in ("PreToolUse", "PostToolUse"):
                calls = [
                    pool.submit(self.hook, self.main, "t", event=event, session=s)
                    for s in sessions
                ]
                for call in calls:
                    call.result()
        self.assertEqual(self.drops(), {})
        self.assertEqual(len(self.rows()), 2 * len(sessions))
        self.assertEqual(len(self.marks()), len(sessions))

    def test_sibling_worktrees_each_get_only_their_own_sha(self):
        left, right = self.root / "left", self.root / "right"
        git("-C", self.main, "worktree", "add", "-q", left)
        git("-C", self.main, "worktree", "add", "-q", right)
        self.hook(left, "l", event="PreToolUse", session="session-l")
        self.hook(right, "r", event="PreToolUse", session="session-r")
        left_sha = self.change(left, name="l")
        right_sha = self.change(right, name="r")
        self.hook(left, "l", session="session-l")
        self.hook(right, "r", session="session-r")
        self.assert_captured(self.post("l", "session-l"), [left_sha])
        self.assert_captured(self.post("r", "session-r"), [right_sha])

    def test_declared_paperclip_checkout_is_bracketed_when_cwd_has_none(self):
        plain = self.root / "plain"
        plain.mkdir()
        tree = self.root / "paperclip-tree"
        git("-C", self.main, "worktree", "add", "-q", tree)
        env = {"PAPERCLIP_AGENT_ID": AGENT, "PAPERCLIP_WORKSPACE_CWD": str(tree)}
        row = self.bracket(plain, "declared", lambda: self.change(tree), **env)
        self.assert_captured(row, [self.head(tree)])

    def test_truncated_or_rotated_reflog_is_a_rewrite_gap(self):
        for i in range(5):
            self.change(name=f"f{i}")
        log = self.main / ".git/logs/HEAD"

        def truncate():
            log.write_bytes(b"")
            self.change(name="after-truncate")

        row = self.bracket(self.main, "truncated", truncate)
        self.assertEqual(row["commit_gap"], "reflog_rewritten")
        self.assertFalse({"commits", "overlap_count"} & row.keys())

        def rotate():
            self.change(name="before-expire")
            git("-C", self.main, "reflog", "expire", "--expire=now", "--all")
            self.change(name="after-expire")

        row = self.bracket(self.main, "rotated", rotate)
        self.assertEqual(row["commit_gap"], "reflog_rewritten")
        self.assertEqual(self.drops(), {"reflog_rotated": 2})

    def test_more_than_64_kib_or_20_commits_is_an_overflow_gap(self):
        many = lambda: [self.change(name=f"n{i}") for i in range(21)]
        row = self.bracket(self.main, "many", many)
        self.assertEqual(row["commit_gap"], "reflog_overflow")
        self.assertNotIn("commits", row)
        huge = lambda: self.change(name="huge", message="s" * 70_000)
        row = self.bracket(self.main, "huge", huge)
        self.assertEqual(row["commit_gap"], "reflog_overflow")

    def test_a_missing_post_emits_nothing_and_its_mark_is_pruned_after_6h(self):
        self.hook(self.main, "lost", event="PreToolUse")
        self.change()
        self.assertEqual(self.marks(), ["lost"])
        with sqlite3.connect(self.outbox / "collector-rust-v1.sqlite3") as db:
            db.execute(
                "UPDATE bracket_marks SET opened_at = opened_at - ?", (7 * HOUR_NS,)
            )
        self.hook(self.main, "next", event="PreToolUse")
        self.assertEqual(self.marks(), ["next"])
        self.hook(self.main, "lost")
        self.assert_nothing(self.post("lost"))

    def test_reflog_messages_and_author_emails_never_reach_spool_or_wire(self):
        identity = (
            "-c",
            "user.name=NAMECANARY",
            "-c",
            "user.email=canary@author.invalid",
        )

        def commit():
            Path(self.main / "secret").write_text("x")
            git("-C", self.main, "add", "secret")
            git(
                "-C",
                self.main,
                "commit",
                "-q",
                "-m",
                "MESSAGECANARY",
                identity=identity,
            )

        row = self.bracket(self.main, "private", commit)
        self.assert_captured(row, [self.head()])
        canaries = [b"MESSAGECANARY", b"NAMECANARY", b"canary@author.invalid"]
        for file in self.outbox.iterdir():
            raw = file.read_bytes()
            for canary in canaries:
                self.assertNotIn(canary, raw, file.name)
        self.sync()
        wire = b"".join(self.received)
        self.assertIn(self.head().encode(), wire)
        for canary in canaries:
            self.assertNotIn(canary, wire)

    def test_a_10k_line_reflog_keeps_the_debug_hook_under_700_ms(self):
        # Real SHAs and a current date: git's auto maintenance rewrites a
        # reflog holding stale or dangling entries, which reads as a gap.
        head, now = self.head(), int(time.time())
        line = (
            f"{head} {head} t <t@t.invalid> {now} +0000\tcheckout: moving from a to b\n"
        )
        with open(self.main / ".git/logs/HEAD", "a") as log:
            log.write(line * 10_000)
        timings = []

        def timed(**kwargs):
            start = time.perf_counter()
            self.hook(self.main, "slow", **kwargs)
            timings.append(time.perf_counter() - start)

        timed(event="PreToolUse")
        sha = self.change()
        timed()
        self.assert_captured(self.post("slow"), [sha])
        print(
            f"bracket hook latency with a 10k-line reflog: {max(timings) * 1000:.1f} ms"
        )
        self.assertLess(max(timings), 0.700)

    def test_nearly_64_kib_appended_inside_the_bracket_stays_under_700_ms(self):
        head, now = self.head(), int(time.time())
        line = (
            f"{head} {head} t <t@t.invalid> {now} +0000\tcheckout: moving from a to b\n"
        )

        def busy():
            with open(self.main / ".git/logs/HEAD", "a") as log:
                log.write(line * ((63 * 1024) // len(line)))
            return self.change()

        self.hook(self.main, "busy", event="PreToolUse")
        sha = busy()
        before = time.perf_counter()
        self.hook(self.main, "busy")
        elapsed = time.perf_counter() - before
        self.assert_captured(self.post("busy"), [sha])
        print(f"bracket hook latency reading ~63 KiB appended: {elapsed * 1000:.1f} ms")
        self.assertLess(elapsed, 0.700)


if __name__ == "__main__":
    unittest.main()
