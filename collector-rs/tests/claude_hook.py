"""Disposable Linux CLI/shell fixtures, NOT stock Claude runtime qualification."""

import concurrent.futures
import fcntl
import io
import json
import os
import pty
import re
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

import saved_connection
import terminal

from devdiary import observer_hook

ROOT = Path(__file__).resolve().parents[1]
EVENTS = observer_hook.EVENTS


AGENT = "22c99d01-c3ee-4041-9066-0a83f34bd2e2"
RUN = "66666666-6666-4666-8666-666666666666"
TASK = "77777777-7777-4777-8777-777777777777"
DECLARED = "urn:devdiary:run:88888888-8888-4888-8888-888888888888"
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
CRON = {
    "key": "cron",
    "env": {"agent_ref": "CRONTOOL_AGENT", "run_ref": "CRONTOOL_RUN"},
}
EVIL = {"key": "evil", "env": {"agent_ref": "AWS_SECRET_ACCESS_KEY"}}
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
        shutil.copyfile(ROOT / "target/debug/devdiary", self.exe)
        self.exe.chmod(0o700)
        self.main = self.repo("main", "git@github.com:acme/demo.git")
        self.refs = ["https://github.com/acme/demo"]
        self.received = []
        self.served_refs = list(self.refs)
        self.served_orchestrators = None
        self.served_declared_run = None
        self.served_versions = [1, 2]
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
                reply = {
                    "schema_version": 1,
                    "collector_ref": owner.collector_ref,
                    "scope_kind": "installation",
                    "repository_refs": owner.served_refs,
                    "observation_schema_versions": owner.served_versions,
                }
                if owner.served_orchestrators is not None:
                    reply["orchestrators"] = owner.served_orchestrators
                declared = self.headers.get("DevDiary-Scope-Keys")
                if (
                    owner.served_declared_run is not None
                    and declared == "accepts_declared_run_ref"
                ):
                    reply["accepts_declared_run_ref"] = owner.served_declared_run
                self.reply(200, reply)

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

    def connect(
        self, opt_in=False, versions=(1, 2), approved=(PAPERCLIP,), settings=None
    ):
        # A saved connection, as pairing leaves it; setup then reuses it.
        self.home = self.root / getattr(self, "home_name", "home")
        self.connection = saved_connection.save(
            self.home,
            self.origin,
            self.refs,
            versions,
            opt_in,
            collector_ref=self.collector_ref,
        )
        self.data = self.connection.parent
        self.settings = self.root / "settings.json"
        self.write(self.settings, {} if settings is None else settings)
        self.setup_env = saved_connection.environment(self.root, self.home, self.env())
        self.opt_in = opt_in
        # Setup refreshes the scope once. Serve what the connection was approved
        # with, so a test's offer still arrives with the first sync.
        served = self.served_refs, self.served_orchestrators, self.served_declared_run
        self.served_refs = self.refs
        self.served_orchestrators = self.served_declared_run = None
        self.install()
        self.served_refs, self.served_orchestrators, self.served_declared_run = served
        self.outbox = self.connection / "outbox"
        if approved is not None:
            self.approve(list(approved))

    def install(self, code=0, settings=None, answers=(b"y\n",), on_prompt=None):
        """Run `devdiary setup` on a terminal, reusing the saved connection."""
        args = [self.exe, "setup", "--settings", settings or self.settings]
        args += ["--origin", self.origin, "--trust-origin", "--no-browser"]
        if self.opt_in:
            args.append("--record-orchestrated-outside-repos")
        status, out, err, _ = terminal.answering(
            args, self.setup_env, answers, on_prompt=on_prompt
        )
        self.assertEqual(status, code, err)
        if code == 0:
            record = json.loads((self.data / "install.json").read_bytes())
            self.plan = Path(record["plan"])
            self.pinned = Path(record["executable"])
        return out, err

    def uninstall(self, code=0):
        return self.run_collector("uninstall", env=self.setup_env, code=code)

    def newest_plan(self):
        plans = (self.data / "plans").iterdir()
        made = [p for p in plans if re.fullmatch(r"claude-\d+\.json", p.name)]
        return max(made, key=lambda p: int(p.stem.split("-")[1]))

    def approve(self, entries):
        self.write(self.outbox / "orchestrators-approved.json", entries)

    def offered(self):
        return json.loads((self.outbox / "orchestrators-offered.json").read_bytes())

    def saved_refs(self):
        return json.loads((self.outbox / "repository-refs.json").read_bytes())

    def doctor(self):
        result = self.run_collector("doctor", env=self.setup_env)
        return json.loads(result.stdout)

    def status(self):
        return self.doctor()["orchestrators"]

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
            if not k.startswith(
                ("PAPERCLIP_", "TRUSTGROWTH_", "WRIGHTBOT_", "TG_", "DEVDIARY_RUN_REF")
            )
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
            "hook",
            "claude",
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
        return self.doctor()["drops"]

    def sync(self):
        self.run_collector("sync", self.connection, env=self.setup_env)
        return self.doctor()


class ClaudeHookTest(InstallationFixture, unittest.TestCase):
    """One installed Claude Code registration, run through the real hook command."""

    home_name = "home ' quoted"

    def setUp(self):
        super().setUp()
        self.repo = self.main
        self.original = {
            "permissions": {"allow": ["Read"]},
            "env": {"PRIVATE": "CONFIG_SECRET"},
            "hooks": {"Stop": [], "Unrelated": [{"keep": True}]},
        }
        self.connect(approved=None, settings=self.original)
        self.installation_id = json.loads(
            (self.connection / "installation.json").read_bytes()
        )["installation_id"]
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

    def hook(self, data=None, raw=None, plan=None):
        result = subprocess.run(
            [str(self.exe), "hook", "claude", str(plan or self.plan)],
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

    def files(self, skip=(), units=True):
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
            and p.name not in skip
            and (units or not p.is_relative_to(self.root / "config"))
        }

    def refused_while_asked(self, edit, code=2):
        """Edit the settings while setup asks; nothing more may change."""
        seen = {}

        def on_prompt(_):
            edit()
            seen["before"] = self.files()

        self.install(code=code, on_prompt=on_prompt)
        self.assertEqual(self.files(), seen["before"])

    def test_roundtrip_quoted_shell_private_and_removed_hook(self):
        config = json.loads(self.settings.read_bytes())
        command = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        self.assertIn("home '\"'\"' quoted", command)
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
        self.assertNotIn("actor_ref", self.rows()[0])
        for file in self.outbox.iterdir():
            self.assertNotIn(b"PRIVATE_SENTINEL", file.read_bytes())
            self.assertEqual(file.stat().st_mode & 0o077, 0)
        self.assertNotIn(b"CONFIG_SECRET", self.plan.read_bytes())
        self.uninstall()
        self.assertEqual(json.loads(self.settings.read_bytes()), self.original)
        self.hook(dict(self.raw, tool_use_id="after-remove"))
        self.assertEqual(len(self.rows()), 1)
        self.pinned.unlink()
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
        other = "22222222-2222-4222-8222-222222222222"
        cases = [
            lambda p: dict(p, absent_events="invalid"),
            lambda p: dict(p, absent_events=list(EVENTS)),
            lambda p: dict(p, absent_events=p["absent_events"] + ["Stop"]),
            lambda p: dict(p, absent_events=p["absent_events"][1:]),
            lambda p: dict(p, absent_events=["Unknown"]),
            lambda p: dict(p, absent_events=[1]),
            lambda p: dict(p, absent_events=p["absent_events"] * 2),
            lambda p: dict(p, absent_hooks="false"),
            lambda p: dict(p, absent_hooks=True),
            lambda p: dict(p, unknown=True),
            lambda p: dict(p, scope=dict(p["scope"], installation_id=other)),
            lambda p: dict(p, scope=dict(p["scope"], unknown=True)),
            lambda p: dict(p, scope=dict(p["scope"], collector_ref="changed")),
        ]
        fields = list(json.loads(self.plan.read_bytes()))
        cases += [
            lambda p, f=field: {k: v for k, v in p.items() if k != f}
            for field in fields
        ]
        raw_cases = [
            lambda raw: raw.replace(b'"version": 1', b'"version": 1, "version": 1'),
            lambda raw: raw.replace(b'"timeout": 2', b'"timeout": 2, "timeout": 2'),
        ]

        def rewrite(change):
            return lambda path: path.write_bytes(
                json.dumps(change(json.loads(path.read_bytes()))).encode()
            )

        def rewrite_raw(change):
            def edit(path):
                raw = json.dumps(json.loads(path.read_bytes())).encode()
                path.write_bytes(change(raw))

            return edit

        saved = self.plan.read_bytes()
        for index, edit in enumerate(
            [rewrite(c) for c in cases] + [rewrite_raw(c) for c in raw_cases]
        ):
            with self.subTest(case=index):
                edit(self.plan)
                # Uninstall disables the timer before it reads the plan.
                before = self.files(skip={"drops.log"}, units=False)
                self.run_collector("upgrade", "--yes", env=self.setup_env, code=2)
                self.uninstall(code=2)
                self.hook()
                self.assertEqual(self.files(skip={"drops.log"}, units=False), before)
                self.plan.write_bytes(saved)
        self.uninstall()
        self.uninstall()
        self.assertEqual(json.loads(self.settings.read_bytes()), self.original)

    def test_python_reference_parity_and_dedup(self):
        py = self.root / "python"
        py.mkdir(mode=0o700)
        self.save(py / "registration.lock", {})
        self.save(
            py / "manifest.json",
            {
                "status": "installed",
                "installation_id": self.installation_id,
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
                    self.installation_id,
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
            # Installation rows are schema v2; the Python observer writes v1.
            for key in ("observation_id", "observed_at", "repository_ref"):
                a.pop(key, None)
            self.assertEqual((a.pop("schema_version"), b.pop("schema_version")), (2, 1))
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
        self.uninstall()
        self.save(self.settings, {})
        self.install()
        installed = self.settings.read_bytes()
        doc = json.loads(installed)
        doc["hooks"]["Stop"].append(doc["hooks"]["Stop"][0])
        self.save(self.settings, doc)
        self.uninstall(code=2)
        self.hook()
        self.assertEqual(self.rows(), [])
        self.settings.write_bytes(installed)
        self.uninstall()
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
        for file in self.outbox.iterdir():
            if file.is_file():
                self.assertNotIn(b"PRIVATE_SENTINEL", file.read_bytes())

    def test_fixture_exception_cleanup_disables_later_lifecycle(self):
        try:
            self.hook(dict(self.raw, hook_event_name="SessionStart", source="startup"))
            raise RuntimeError("synthetic host failure")
        except RuntimeError:
            pass
        finally:
            self.uninstall()
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
        self.assertEqual(
            self.drops(),
            {
                "outside_scope": 2,
                "invalid": 3,
                "invalid_json": 4,
                "invalid_prompt_id": 1,
                "invalid_tool_use_id": 1,
            },
        )
        child = self.repo / "sub"
        child.mkdir()
        self.hook(dict(self.raw, cwd=str(child)))
        self.assertEqual(len(self.rows()), 1)

    def test_a_permission_prompt_records_its_moment_and_nothing_private(self):
        self.hook(
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "session-1",
                "cwd": str(self.repo),
                "tool_name": "Bash",
                "tool_input": {"command": "PRIVATE_SENTINEL"},
            }
        )
        self.assertEqual([row["event"] for row in self.rows()], ["PermissionRequest"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(self.rows()))

    def test_a_bad_agent_type_costs_the_label_and_not_the_row(self):
        for tool, value in enumerate(("has space", None, "")):
            self.hook(dict(self.raw, tool_use_id=f"tool-{tool}", agent_type=value))
        self.assertEqual(len(self.rows()), 3)
        self.assertFalse(any("agent_type" in row for row in self.rows()))
        self.assertEqual(self.drops(), {})

    def test_open_stdin_deadline_and_locked_spool(self):
        start = time.monotonic()
        process = subprocess.Popen(
            [str(self.exe), "hook", "claude", str(self.plan)],
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
        with sqlite3.connect(self.outbox / "collector-rust-v1.sqlite3") as db:
            db.execute("BEGIN EXCLUSIVE")
            self.hook()
        self.assertEqual(self.rows(), [])
        self.hook()
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_dedup_and_registration_lock(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: self.hook(), range(12)))
        self.assertEqual(len(self.rows()), 1)
        with (self.outbox / "claude-settings.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.hook(dict(self.raw, tool_use_id="locked"))
            self.uninstall(code=2)
        self.assertEqual(len(self.rows()), 1)
        drops = self.drops()
        self.assertGreaterEqual(drops.pop("busy"), 1)
        self.assertLessEqual(drops.keys(), {"observation_conflict", "busy"})

    def test_consent_stale_plan_owned_entry_and_unrelated_edits(self):
        config = json.loads(self.settings.read_bytes())
        config["new_customer_setting"] = True
        extra = {"hooks": [{"type": "command", "command": "true"}]}
        config["hooks"]["Stop"].append(extra)
        self.save(self.settings, config)
        self.uninstall()
        expected = dict(self.original, new_customer_setting=True)
        expected["hooks"]["Stop"].append(extra)
        self.assertEqual(json.loads(self.settings.read_bytes()), expected)
        before = self.settings.read_bytes()
        _, err = self.install(code=2, answers=(b"n\n",))
        self.assertIn(b"consent_declined", err)
        self.assertEqual(self.settings.read_bytes(), before)
        self.install()
        config = json.loads(self.settings.read_bytes())
        config["hooks"]["Stop"][-1]["hooks"][0]["timeout"] = 10
        self.save(self.settings, config)
        before = self.settings.read_bytes()
        self.uninstall(code=2)
        self.assertEqual(self.settings.read_bytes(), before)
        self.hook()
        self.assertEqual(self.rows(), [])

    def test_changed_executable_scope_and_permissions(self):
        self.pinned.chmod(0o755)
        self.hook()
        self.assertEqual(self.rows(), [])
        self.pinned.chmod(0o700)
        with self.pinned.open("ab") as f:
            f.write(b"changed")
        self.hook()
        self.assertEqual(self.rows(), [])
        self.uninstall()
        self.assertEqual(json.loads(self.settings.read_bytes()), self.original)

    def test_setup_crash_and_retry_keeps_atomic_settings(self):
        # Real process death at varying points, never production fault switches.
        self.uninstall()
        for delay in (0, 0.01, 0.04, 0.08):
            with self.subTest(delay=delay):
                self.setup_then_kill(delay)
                current = json.loads(self.settings.read_bytes())
                installed = (self.data / "install.json").exists()
                backup = self.settings.with_name("settings.json.devdiary-backup")
                left = backup.exists()
                if current != self.original:
                    entry = json.loads(self.newest_plan().read_bytes())["entry"]
                    self.assertTrue(
                        all(current["hooks"][e].count(entry) == 1 for e in EVENTS)
                    )
                    # Hooks without an install record could be removed by
                    # neither setup nor uninstall, unless the backup is left.
                    self.assertTrue(installed or left, "hooks written, no record")
                if left:
                    self.assertEqual(json.loads(backup.read_bytes()), self.original)
                # A retry works both before and after the atomic rename.
                if not installed:
                    self.install(answers=(b"y\n", b"y\n") if left else (b"y\n",))
                    self.assertFalse(backup.exists())
                self.hook()
                self.uninstall()
                self.assertEqual(json.loads(self.settings.read_bytes()), self.original)
        with sqlite3.connect(self.outbox / "collector-rust-v1.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def setup_then_kill(self, delay):
        """Answer setup's question on a terminal, then kill it `delay` seconds later."""
        args = [self.exe, "setup", "--settings", self.settings, "--origin"]
        args += [self.origin, "--trust-origin", "--no-browser"]
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        process = subprocess.Popen(
            [str(a) for a in args],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.setup_env,
        )
        os.close(slave)
        seen = b""
        deadline = time.monotonic() + 20
        while terminal.PROMPT not in seen and time.monotonic() < deadline:
            seen += os.read(process.stdout.fileno(), 4096)
        self.assertIn(terminal.PROMPT, seen)
        os.write(master, b"y\n")
        time.sleep(delay)
        process.kill()
        process.communicate(timeout=10)

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
        self.uninstall()
        original = self.settings.read_bytes()
        plans = sorted((self.data / "plans").iterdir())
        self.settings.write_bytes(
            b'{"hooks":{},"permissions":{"allow":[],"allow":["Write"]}}'
        )
        self.install(code=2)
        self.assertEqual(sorted((self.data / "plans").iterdir()), plans)
        self.settings.write_bytes(original)
        edited = dict(self.original, edit_after_plan=True)
        self.refused_while_asked(lambda: self.save(self.settings, edited))
        self.assertEqual(json.loads(self.settings.read_bytes()), edited)

    def test_full_spool_neutral_without_mutation(self):
        # Exercise the real conservative admission boundary through the hook.
        for i in range(900):
            self.hook(dict(self.raw, tool_use_id=f"fill-{i}"))
            if i % 100 == 99 and self.drops().get("spool_full"):
                break
        else:
            self.fail("expected conservative capacity refusal")
        before = self.rows()
        self.assertGreater(len(before), 100)
        self.hook(dict(self.raw, tool_use_id="after-full"))
        self.assertEqual(self.rows(), before)
        self.assertLessEqual(
            (self.outbox / "collector-rust-v1.sqlite3").stat().st_size,
            16 * 1024 * 1024,
        )

    def test_outside_scope_event_counts_a_drop_without_a_row(self):
        outside = self.root / "repo-other"
        outside.mkdir()
        self.hook(dict(self.raw, cwd=str(outside)))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.drops(), {"outside_scope": 1})

    def test_settings_readable_by_others_but_writable_only_by_owner(self):
        self.uninstall()
        home = self.root / "other-home"
        home.mkdir(mode=0o700)
        claude = home / ".claude"
        claude.mkdir()
        claude.chmod(0o755)
        settings = claude / "settings.json"
        settings.write_text(json.dumps(self.original))
        settings.chmod(0o644)
        self.install(settings=settings)
        plan = self.plan
        self.assertEqual(settings.stat().st_mode & 0o777, 0o644)
        self.hook(plan=plan)
        self.assertEqual(len(self.rows()), 1)
        for path, mode in ((settings, 0o664), (settings, 0o777), (claude, 0o777)):
            with self.subTest(path=path.name, mode=oct(mode)):
                original = path.stat().st_mode & 0o777
                path.chmod(mode)
                self.hook(dict(self.raw, tool_use_id=f"mode-{mode}"), plan=plan)
                self.uninstall(code=2)
                path.chmod(original)
        alias = home / "settings-alias.json"
        os.link(settings, alias)
        self.hook(dict(self.raw, tool_use_id="hardlinked"), plan=plan)
        self.uninstall(code=2)
        alias.unlink()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.drops(), {"permission": 4})
        self.uninstall()
        self.assertEqual(json.loads(settings.read_bytes()), self.original)
        self.assertEqual(settings.stat().st_mode & 0o777, 0o644)
        linked = self.root / "linked-home"
        linked.symlink_to(home, target_is_directory=True)
        _, err = self.install(settings=linked / ".claude/settings.json", code=2)
        self.assertNotIn(b"already_installed", err)
        self.assertFalse((self.data / "install.json").exists())

    def test_broken_settings_count_as_stale_plan_not_invalid_events(self):
        text = self.settings.read_text()
        for broken in ('{"permissions": {}, ' + text[1:], text[:-1]):
            with self.subTest(broken=broken[:20]):
                self.settings.write_text(broken)
                self.hook()
        self.assertEqual(self.rows(), [])
        self.settings.write_text(text)  # doctor reads the settings too
        self.assertEqual(self.drops(), {"stale_plan": 2})

    def test_python_observer_registration_is_refused(self):
        self.uninstall()
        plans = sorted((self.data / "plans").iterdir())
        observer = {"type": "command", "command": "python3 /opt/observer_hook.py"}
        self.save(
            self.settings,
            dict(self.original, hooks={"Stop": [{"hooks": [observer]}]}),
        )
        _, err = self.install(code=2)
        self.assertIn(b"python_observer_registered", err)
        self.assertEqual(sorted((self.data / "plans").iterdir()), plans)

    def test_replaced_binary_is_rehashed_within_the_hook_deadline(self):
        self.assertGreater(self.pinned.stat().st_size, 32 * 1024 * 1024)
        fingerprint = self.outbox / "executable-fingerprint"
        self.hook()
        before = fingerprint.read_bytes()
        replacement = self.root / "replacement"
        shutil.copyfile(ROOT / "target/debug/devdiary", replacement)
        replacement.chmod(0o700)
        os.replace(replacement, self.pinned)
        self.hook(dict(self.raw, tool_use_id="after-replace"))
        self.assertEqual(len(self.rows()), 2)
        self.assertNotEqual(fingerprint.read_bytes(), before)
        with self.pinned.open("ab") as f:
            f.write(b"changed")
        self.hook(dict(self.raw, tool_use_id="after-change"))
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.drops(), {"stale_plan": 1})

    def test_large_tool_payload_is_skipped_up_to_sixteen_mebibytes(self):
        self.hook(dict(self.raw, tool_response="PRIVATE_SENTINEL" * 200_000))
        self.assertEqual(len(self.rows()), 1)
        self.hook(raw=b'{"pad":"' + b"x" * (16 * 1024 * 1024) + b'"}')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.drops(), {"input_too_large": 1})
        for file in self.outbox.iterdir():
            self.assertNotIn(b"PRIVATE_SENTINEL", file.read_bytes())

    def test_hook_to_real_cli_http_delivery(self):
        self.hook()
        spooled = self.rows()
        (self.outbox / "executable-fingerprint").unlink()
        doctor = self.sync()
        self.assertTrue((self.outbox / "executable-fingerprint").exists())
        self.assertEqual([json.loads(r) for r in self.received], spooled)
        self.assertEqual(self.rows(), [])
        self.assertEqual(
            (doctor["pending"], doctor["delivered"], doctor["drops"]), (0, 1, {})
        )


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
            self.assertNotIn("orchestrator", row)
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

    def test_unapproved_names_are_never_read(self):
        self.served_orchestrators = [PAPERCLIP]
        self.connect(approved=None)
        self.sync()
        self.assertEqual(self.offered()["orchestrators"], [PAPERCLIP])
        self.assertFalse((self.outbox / "orchestrators-approved.json").exists())
        self.hook(self.main, PAPERCLIP_AGENT_ID=AGENT)
        (row,) = self.rows()
        self.assertEqual(row["orchestrator"], "unapproved")
        self.assertNotIn("orchestrator_agent_ref", row)
        self.assertEqual(
            self.status(),
            {
                "approved": [],
                "approved_list_unreadable": False,
                "offered": ["paperclip"],
                "offered_list_unreadable": False,
                "waiting_for_approval": True,
                "refused": 0,
                "notice": "Runs of paperclip stay unknown until approved. "
                "In a terminal run: devdiary orchestrators approve",
            },
        )

    def test_an_unanswered_offer_marks_every_row_even_with_no_variable_set(self):
        self.served_orchestrators = [PAPERCLIP]
        self.connect(approved=None)
        self.sync()
        self.hook(self.main, tool_use_id="plain")
        self.hook(self.main, tool_use_id="run", PAPERCLIP_RUN_ID=RUN)
        rows = self.rows()
        self.assertEqual([r["orchestrator"] for r in rows], ["unapproved"] * 2)
        self.assertNotIn("orchestrator_run_ref", rows[1])
        self.sync()
        self.assertNotIn(RUN.encode(), b"".join(self.received))

    def test_the_marker_uses_no_checkout_hint_and_records_nothing_outside_a_repo(self):
        plain = self.root / "plain"
        plain.mkdir()
        tree = self.root / "hint-tree"
        git("-C", self.main, "worktree", "add", "-q", tree)
        self.served_orchestrators = [PAPERCLIP]
        self.connect(opt_in=True, approved=None)
        self.sync()
        self.hook(plain, PAPERCLIP_AGENT_ID=AGENT, PAPERCLIP_WORKSPACE_CWD=str(tree))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.drops(), {"outside_scope": 1})

    def test_an_empty_approved_list_with_an_offer_marks_rows(self):
        self.served_orchestrators = [PAPERCLIP]
        self.connect(approved=[])
        self.sync()
        self.hook(self.main)
        self.assertEqual(self.rows()[0]["orchestrator"], "unapproved")

    def test_an_unreadable_approved_list_with_an_offer_marks_rows(self):
        self.served_orchestrators = [PAPERCLIP]
        self.connect()
        self.sync()
        (self.outbox / "orchestrators-approved.json").write_text("not json")
        self.hook(self.main, PAPERCLIP_AGENT_ID=AGENT)
        (row,) = self.rows()
        self.assertEqual(row["orchestrator"], "unapproved")
        self.assertEqual(self.drops(), {})

    def test_an_offer_that_was_all_refused_marks_rows(self):
        self.served_orchestrators = [
            {"key": "evil", "env": {"agent_ref": "AWS_SECRET_ACCESS_KEY"}}
        ]
        self.connect(approved=None)
        self.sync()
        self.hook(self.main, AWS_SECRET_ACCESS_KEY=AGENT)
        (row,) = self.rows()
        self.assertEqual(row["orchestrator"], "unapproved")
        self.assertNotIn("orchestrator_agent_ref", row)

    def test_an_unreadable_offer_with_nothing_approved_marks_rows(self):
        self.connect(approved=None)
        offered = self.outbox / "orchestrators-offered.json"
        offered.write_text("not json")
        offered.chmod(0o600)
        self.hook(self.main)
        self.assertEqual(self.rows()[0]["orchestrator"], "unapproved")

    def test_with_nothing_offered_rows_stay_plain(self):
        self.served_orchestrators = []
        self.connect(approved=None)
        self.hook(self.main, tool_use_id="before")
        self.sync()
        self.hook(self.main, tool_use_id="after")
        for row in self.rows():
            self.assertNotIn("orchestrator", row)

    def test_approving_ends_the_marker(self):
        self.served_orchestrators = [PAPERCLIP]
        self.connect(approved=None)
        self.sync()
        self.approve([PAPERCLIP])
        self.hook(self.main, tool_use_id="plain")
        self.hook(self.main, tool_use_id="run", PAPERCLIP_AGENT_ID=AGENT)
        rows = self.rows()
        self.assertNotIn("orchestrator", rows[0])
        self.assertEqual(rows[1]["orchestrator"], "paperclip")

    def test_the_offered_list_is_stored_privately(self):
        self.served_orchestrators = [PAPERCLIP]
        self.connect()
        self.sync()
        mode = (self.outbox / "orchestrators-offered.json").stat().st_mode
        self.assertEqual(mode & 0o777, 0o600)
        self.assertFalse(self.status()["waiting_for_approval"])

    def test_a_changed_offer_keeps_the_approved_list_and_says_so(self):
        self.served_orchestrators = [CRON]
        self.connect()
        self.sync()
        self.assertEqual(self.offered()["orchestrators"], [CRON])
        self.hook(self.main, tool_use_id="old", PAPERCLIP_AGENT_ID=AGENT)
        self.hook(self.main, tool_use_id="new", CRONTOOL_AGENT=AGENT)
        rows = self.rows()
        self.assertEqual(rows[0]["orchestrator"], "paperclip")
        self.assertNotIn("orchestrator", rows[1])
        self.assertTrue(self.status()["waiting_for_approval"])
        self.assertIn("may look interactive", self.status()["notice"])

    def test_a_server_that_drops_the_offer_cannot_narrow_the_approved_list(self):
        self.served_orchestrators = []
        self.connect()
        self.sync()
        self.hook(self.main, PAPERCLIP_AGENT_ID=AGENT)
        self.assertEqual(self.rows()[0]["orchestrator"], "paperclip")
        self.assertTrue(self.status()["waiting_for_approval"])

    def test_a_bad_offered_file_is_reported_and_replaced_by_the_next_offer(self):
        self.connect()
        offered = self.outbox / "orchestrators-offered.json"
        for bad in ("not json", "[1]", '{"orchestrators": 3, "refused": 0}'):
            offered.write_text(bad)
            offered.chmod(0o600)
            self.assertTrue(self.status()["offered_list_unreadable"], bad)
            self.assertFalse(self.status()["waiting_for_approval"], bad)
        self.hook(self.main, PAPERCLIP_AGENT_ID=AGENT)
        self.assertEqual(self.rows()[0]["orchestrator"], "paperclip")
        self.served_orchestrators = [PAPERCLIP]
        self.sync()
        self.assertFalse(self.status()["offered_list_unreadable"])

    def test_a_refused_entry_left_in_an_old_offer_counts_as_refused(self):
        self.connect()
        self.write(
            self.outbox / "orchestrators-offered.json",
            {
                "orchestrators": [{"key": "old", "env": {"run_ref": "SENTRY_DSN"}}],
                "refused": 1,
            },
        )
        status = self.status()
        self.assertEqual((status["offered"], status["refused"]), ([], 2))

    def test_a_scope_reply_without_the_field_keeps_the_stored_offer(self):
        self.served_orchestrators = [PAPERCLIP]
        self.connect()
        self.sync()
        self.served_orchestrators = None
        self.sync()
        self.assertEqual(self.offered()["orchestrators"], [PAPERCLIP])

    def test_a_server_offering_a_secret_name_cannot_cause_it_to_be_read(self):
        evil = {"key": "evil", "env": {"agent_ref": "AWS_SECRET_ACCESS_KEY"}}
        self.served_orchestrators = [evil, PAPERCLIP]
        self.connect(approved=None)
        self.sync()
        self.assertEqual(self.offered(), {"orchestrators": [PAPERCLIP], "refused": 1})
        self.approve(self.offered()["orchestrators"])
        self.hook(self.main, AWS_SECRET_ACCESS_KEY=AGENT)
        (row,) = self.rows()
        self.assertNotIn("orchestrator", row)
        self.assertEqual(self.status()["refused"], 1)
        self.sync()
        self.assertNotIn(AGENT.encode(), b"".join(self.received))

    def test_a_refused_entry_keeps_the_last_good_offer_but_applies_refs(self):
        self.served_orchestrators = [PAPERCLIP]
        self.connect()
        self.sync()
        offer = self.offered()
        approved = (self.outbox / "orchestrators-approved.json").read_bytes()
        self.hook(self.main, PAPERCLIP_AGENT_ID=AGENT)
        self.served_orchestrators = [EVIL, CRON]
        self.served_refs = ["https://github.com/acme/other"]
        self.assertEqual(
            self.sync()["last_scope_refresh_error"],
            "scope_refresh_offer_refused",
        )
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.offered(), offer)
        self.assertEqual(
            (self.outbox / "orchestrators-approved.json").read_bytes(), approved
        )
        self.assertEqual(self.saved_refs(), self.served_refs)
        self.assertEqual(
            self.doctor()["last_scope_refresh_error"],
            "scope_refresh_offer_refused",
        )

    def test_a_refused_entry_does_not_freeze_the_repository_refs(self):
        self.served_orchestrators = [PAPERCLIP]
        self.connect()
        self.sync()
        added = "https://github.com/acme/added"
        added_repo = self.repo("added", "git@github.com:acme/added.git")
        self.served_orchestrators = [EVIL]
        self.served_refs = [added]
        self.sync()
        self.hook(added_repo)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.drops(), {})
        self.hook(self.main, tool_use_id="tool-2")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.drops(), {"unknown_repository": 1})

    def test_a_refused_entry_over_an_empty_offer_offers_the_clean_part(self):
        self.served_orchestrators = []
        self.connect(approved=None)
        self.sync()
        self.assertEqual(self.offered(), {"orchestrators": [], "refused": 0})
        self.served_orchestrators = [PAPERCLIP, EVIL]
        self.sync()
        self.assertEqual(self.offered(), {"orchestrators": [PAPERCLIP], "refused": 1})
        self.assertTrue(self.status()["waiting_for_approval"])
        self.hook(self.main, PAPERCLIP_AGENT_ID=AGENT)
        (row,) = self.rows()
        self.assertEqual(row["orchestrator"], "unapproved")

    def test_a_first_offer_with_a_refused_entry_is_saved_clean_and_reported(self):
        self.served_orchestrators = [EVIL, PAPERCLIP]
        self.connect(approved=None)
        self.assertEqual(
            self.sync()["last_scope_refresh_error"],
            "scope_refresh_offer_refused",
        )
        self.assertEqual(self.offered(), {"orchestrators": [PAPERCLIP], "refused": 1})

    def test_a_refused_name_in_the_approved_file_stops_the_row_unread(self):
        self.connect()
        self.approve([{"key": "evil", "env": {"agent_ref": "AWS_SECRET_ACCESS_KEY"}}])
        self.hook(self.main, AWS_SECRET_ACCESS_KEY=AGENT)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.drops(), {"error": 1})
        status = self.status()
        self.assertTrue(status["approved_list_unreadable"])
        self.assertTrue(status["waiting_for_approval"])

    def test_the_first_approved_entry_that_matches_wins(self):
        self.connect(approved=[CRON, PAPERCLIP])
        self.hook(self.main, CRONTOOL_RUN=RUN, PAPERCLIP_AGENT_ID=AGENT)
        (row,) = self.rows()
        self.assertEqual(row["orchestrator"], "cron")
        self.assertEqual(row["orchestrator_run_ref"], RUN)
        self.assertNotIn("orchestrator_agent_ref", row)

    def test_a_new_preset_works_with_no_collector_change(self):
        plain = self.root / "plain"
        plain.mkdir()
        tree = self.root / "other-tree"
        git("-C", self.main, "worktree", "add", "-q", tree)
        tool = {
            "key": "acme-run",
            "env": {"run_ref": "ACME_RUN", "checkout_path": ["ACME_DIR"]},
        }
        self.connect(approved=[tool])
        self.hook(plain, ACME_RUN="malformed", ACME_DIR=str(tree))
        (row,) = self.rows()
        self.assertEqual(row["orchestrator"], "acme-run")
        self.assertEqual(row["repository_ref"], "https://github.com/acme/demo")
        self.assertNotIn("orchestrator_run_ref", row)

    def test_only_the_matched_entry_gives_checkout_hints(self):
        plain = self.root / "plain"
        plain.mkdir()
        tree = self.root / "unused-tree"
        git("-C", self.main, "worktree", "add", "-q", tree)
        self.connect(approved=[CRON, PAPERCLIP])
        self.hook(plain, CRONTOOL_AGENT=AGENT, PAPERCLIP_WORKSPACE_CWD=str(tree))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.drops(), {"outside_scope": 1})

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
        release = ROOT / "target/release/devdiary"
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


class DeclaredRunHookTest(InstallationFixture, unittest.TestCase):
    def accepted(self, **connect):
        self.served_declared_run = True
        self.connect(**connect)
        self.sync()

    def declared(self):
        return [row.get("declared_run_ref") for row in self.rows()]

    def test_every_event_carries_the_run_once_the_scope_accepts_it(self):
        self.accepted()
        for number, event in enumerate(EVENTS):
            self.hook(
                self.main,
                tool_use_id=f"tool-{number}",
                event=event,
                DEVDIARY_RUN_REF=DECLARED,
            )
        self.assertEqual(self.declared(), [DECLARED] * len(EVENTS))

    def test_the_wire_carries_the_run(self):
        self.accepted()
        self.hook(self.main, DEVDIARY_RUN_REF=DECLARED)
        self.sync()
        self.assertEqual(json.loads(self.received[0])["declared_run_ref"], DECLARED)

    def test_no_row_carries_the_run_until_a_refresh_says_true(self):
        self.connect()
        self.hook(self.main, "before-sync", DEVDIARY_RUN_REF=DECLARED)
        self.sync()
        self.hook(self.main, "no-key", DEVDIARY_RUN_REF=DECLARED)
        self.served_declared_run = False
        self.sync()
        self.hook(self.main, "false", DEVDIARY_RUN_REF=DECLARED)
        self.assertEqual(self.declared(), [None])
        self.assertEqual(len(self.received), 2)
        self.assertNotIn(b"declared_run_ref", b"".join(self.received))

    def test_a_later_reply_without_the_key_stops_the_field(self):
        self.accepted()
        self.served_declared_run = None
        self.sync()
        self.hook(self.main, DEVDIARY_RUN_REF=DECLARED)
        self.assertEqual(self.declared(), [None])

    def test_a_later_false_stops_the_field(self):
        self.accepted()
        self.served_declared_run = False
        self.sync()
        self.hook(self.main, DEVDIARY_RUN_REF=DECLARED)
        self.assertEqual(self.declared(), [None])

    def test_a_refused_reply_keeps_the_last_good_answer(self):
        self.accepted()
        self.served_declared_run = "true"
        self.assertEqual(
            self.sync()["last_scope_refresh_error"],
            "scope_refresh_invalid_response",
        )
        self.hook(self.main, DEVDIARY_RUN_REF=DECLARED)
        self.assertEqual(self.declared(), [DECLARED])

    def test_a_refused_offer_keeps_the_last_good_answer(self):
        self.served_orchestrators = []
        self.accepted(approved=())
        self.served_declared_run = False
        self.served_orchestrators = [
            {"key": "evil", "env": {"agent_ref": "AWS_SECRET_ACCESS_KEY"}}
        ]
        self.sync()
        self.hook(self.main, DEVDIARY_RUN_REF=DECLARED)
        self.assertEqual(self.declared(), [DECLARED])

    def test_a_bad_value_is_left_out_and_the_row_stays(self):
        bad = [
            "",
            DECLARED.upper(),
            DECLARED + "\n",
            " " + DECLARED,
            "urn:devdiary:run:fixed-id",
            "urn:devdiary:event:" + DECLARED.rsplit(":", 1)[1],
            DECLARED.rsplit(":", 1)[1],
            b"urn:devdiary:run:\xff",
        ]
        self.accepted()
        for number, value in enumerate(bad):
            self.hook(self.main, f"tool-{number}", DEVDIARY_RUN_REF=value)
        self.assertEqual(self.declared(), [None] * len(bad))
        self.assertEqual(self.drops(), {})

    def test_no_variable_means_no_field(self):
        self.accepted()
        self.hook(self.main)
        self.assertEqual(self.declared(), [None])

    def test_the_fixed_name_needs_no_local_approval(self):
        self.served_orchestrators = [PAPERCLIP]
        self.accepted(approved=())
        self.hook(self.main, DEVDIARY_RUN_REF=DECLARED, PAPERCLIP_AGENT_ID=AGENT)
        (row,) = self.rows()
        self.assertEqual(row["declared_run_ref"], DECLARED)
        self.assertEqual(row["orchestrator"], "unapproved")

    def test_an_orchestrated_row_carries_both(self):
        self.accepted()
        self.hook(self.main, DEVDIARY_RUN_REF=DECLARED, PAPERCLIP_AGENT_ID=AGENT)
        (row,) = self.rows()
        self.assertEqual(row["declared_run_ref"], DECLARED)
        self.assertEqual(row["orchestrator"], "paperclip")

    def test_a_v1_connection_never_carries_the_run(self):
        self.accepted(versions=(1,))
        self.hook(self.main, DEVDIARY_RUN_REF=DECLARED)
        self.assertEqual(self.declared(), [None])

    def test_a_reply_without_v2_is_not_an_acceptance(self):
        self.served_declared_run = True
        self.served_versions = [1]
        self.connect()
        self.sync()
        self.hook(self.main, DEVDIARY_RUN_REF=DECLARED)
        self.assertEqual(self.declared(), [None])


class OutsideReposHookTest(InstallationFixture, unittest.TestCase):
    def consent(self):
        consent = self.outbox / "record-outside-repos"
        consent.write_bytes(b"")
        consent.chmod(0o600)

    def plain_folder(self):
        folder = Path.home() / f"devdiary-outside-{os.getpid()}"
        folder.mkdir(exist_ok=True)
        self.addCleanup(folder.rmdir)
        return folder

    def test_an_owner_row_outside_a_repository_needs_consent(self):
        self.connect()
        folder = self.plain_folder()
        self.hook(folder)
        self.assertEqual(self.rows(), [])
        self.consent()
        self.hook(folder)
        row = self.rows()[0]
        self.assertNotIn("repository_ref", row)
        self.assertNotIn(str(folder), json.dumps(row))

    def test_owner_consent_does_not_admit_a_named_run(self):
        self.connect(opt_in=False)
        self.consent()
        self.hook(self.plain_folder(), PAPERCLIP_AGENT_ID=str(uuid.uuid4()))
        self.assertEqual(self.rows(), [])

    def test_temp_folders_stay_out_even_with_consent(self):
        self.connect()
        self.consent()
        self.hook(tempfile.gettempdir())
        self.assertEqual(self.rows(), [])


class HookLossTest(InstallationFixture, unittest.TestCase):
    def test_a_hook_waits_out_a_short_write_lock(self):
        self.connect()
        db = sqlite3.connect(
            self.outbox / "collector-rust-v1.sqlite3", isolation_level=None
        )
        self.addCleanup(db.close)
        db.execute("BEGIN IMMEDIATE")
        locked_at = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            running = pool.submit(self.hook, self.main)
            try:
                # The process has 700 ms total, not the connection's 1400 ms.
                # The longer storage wait is covered below the process deadline.
                time.sleep(0.1)
                completed_while_locked = running.done()
            finally:
                db.execute("COMMIT")
            held_seconds = time.monotonic() - locked_at
            running.result()
        self.assertFalse(
            completed_while_locked,
            f"hook completed while writer lock was held for {held_seconds:.3f}s",
        )
        self.assertEqual(
            len(self.rows()), 1, {"held_seconds": held_seconds, "drops": self.drops()}
        )
        self.assertNotIn("busy", self.drops())

    def test_a_large_tool_result_is_still_a_row(self):
        self.connect()
        event = {
            "hook_event_name": "PostToolUse",
            "session_id": "session-1",
            "cwd": str(self.main),
            "tool_use_id": "tool-1",
            "tool_name": "Read",
            "tool_response": "x" * (6 * 1024 * 1024),
        }
        self.run_collector(
            "hook",
            "claude",
            self.plan,
            data=json.dumps(event).encode(),
            env=self.env(),
        )
        self.assertEqual(len(self.rows()), 1)

    def test_each_kind_of_bad_input_has_its_own_count(self):
        self.connect()
        self.run_collector("hook", "claude", self.plan, data=b"{", env=self.env())
        self.assertIn("invalid_json", self.drops())


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
            "hook", "claude", self.plan, data=json.dumps(event).encode(), env=self.env()
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
        self.assertEqual(len(self.rows()), 2 * len(sessions), self.drops())
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
