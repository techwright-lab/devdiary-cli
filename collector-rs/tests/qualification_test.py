"""Harness control-flow fixtures ONLY; no stock runtime/provider claims or calls."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import install_test
import qualify_claude as q


class QualificationTest(unittest.TestCase):
    def test_managed_dropins_and_remote_unknown_fail_before_vendor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            system = root / "etc/claude-code"
            home = root / "home"
            system.mkdir(parents=True)
            home.mkdir()
            dropins = system / "managed-settings.d"
            dropins.mkdir()
            policy = dropins / "audit.json"
            policy.write_text('{"hooks":{"SessionStart":[]}}')
            with self.assertRaisesRegex(q.GateError, "managed_policy_requires_review"):
                q.check_managed_policy(home, system)
            policy.unlink()
            dropins.rmdir()
            # Even no on-disk policy does not prove current/cached remote absence.
            with self.assertRaisesRegex(
                q.GateError, "remote_managed_policy_unverified"
            ):
                q.check_managed_policy(home, system)
            with (
                patch.object(q, "clean_env", return_value={"HOME": str(home)}),
                patch.object(q, "run", side_effect=AssertionError("no subprocess")),
                self.assertRaises(q.GateError),
            ):
                q.execute(SimpleNamespace(), {})

    def test_dropin_prevents_vendor_dispatch_with_synthetic_system_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            system = home / "etc/claude-code"
            (system / "managed-settings.d").mkdir(parents=True)
            (system / "managed-settings.d/audit.json").write_text("{}")
            native = q.CLI / "collector-rs/target/debug/devdiary"

            def fake_run(argv, **kwargs):
                if list(map(str, argv))[:2] == ["mise", "which"]:
                    return str(native).encode()
                if "--version" in argv and "--settings" in argv:
                    self.fail("vendor dispatched with managed drop-in")
                return b"fixture"

            original_check = getattr(q, "check_managed_policy", None)
            with (
                patch.object(
                    q, "clean_env", return_value={"HOME": str(home), "PATH": "fixture"}
                ),
                patch.object(q, "run", side_effect=fake_run),
                patch.object(
                    q,
                    "Path",
                    side_effect=lambda value: (
                        system / str(value).removeprefix("/etc/claude-code/")
                        if str(value).startswith("/etc/claude-code/")
                        else Path(value)
                    ),
                ),
                contextlib.ExitStack() as stack,
            ):
                if original_check:
                    stack.enter_context(
                        patch.object(
                            q,
                            "check_managed_policy",
                            side_effect=lambda h: original_check(h, system),
                        )
                    )
                with self.assertRaisesRegex(
                    q.GateError, "managed_policy_requires_review"
                ):
                    q.execute(
                        SimpleNamespace(rails_checkout=q.CLI, collector=native), {}
                    )

    def test_default_is_inert_even_with_live_arguments(self):
        with (
            patch.object(q, "execute", side_effect=AssertionError("must not run")),
            patch.object(
                q.subprocess, "Popen", side_effect=AssertionError("must not spawn")
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(q.main(["--max-model-runs", "1"]), 0)
        self.assertEqual(json.loads(output.getvalue())["model_runs_started"], 0)

    def test_requires_exact_budget_and_ci_refusal(self):
        with patch.object(q, "execute", side_effect=AssertionError("must not run")):
            for budget in ("0", "2", "-1"):
                with self.assertRaises(RuntimeError):
                    q.main(["--consent-provider-use", "--max-model-runs", budget])
            with (
                patch.dict(os.environ, {"CI": "true"}),
                self.assertRaises(RuntimeError),
            ):
                q.main(
                    [
                        "--consent-provider-use",
                        "--max-model-runs",
                        "1",
                        "--claude-version",
                        "fixture",
                        "--auth-file",
                        "x",
                        "--rails-checkout",
                        "x",
                    ]
                )

    def test_environment_excludes_credentials_and_proxies(self):
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "secret",
                "DATABASE_URL": "secret",
                "HTTP_PROXY": "secret",
                "CLAUDE_CODE_OAUTH_TOKEN": "secret",
                "HOME": "/normal-home",
            },
        ):
            env = q.clean_env()
        self.assertEqual(env["HOME"], "/normal-home")
        self.assertFalse(any("secret" == value for value in env.values()))

    def test_bounded_child_timeout_overflow_and_exit(self):
        for code, timeout in (
            ("import time; time.sleep(20)", 0.1),
            ("print('x'*2000001)", 5),
            ("raise SystemExit(2)", 5),
        ):
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                q.run([sys.executable, "-c", code], timeout=timeout)
        self.assertEqual(q.run([sys.executable, "-c", "print('ok')"]), b"ok\n")

    def test_descendant_pipe_timeout_is_bounded(self):
        start = time.monotonic()
        code = "import os,time; pid=os.fork(); time.sleep(20) if pid==0 else None"
        with self.assertRaises(RuntimeError):
            q.run([sys.executable, "-c", code], timeout=0.2)
        self.assertLess(time.monotonic() - start, 3)

    def host(self):
        return [
            {
                "type": "system",
                "subtype": "init",
                "tools": ["Read"],
                "mcp_servers": [],
                "plugins": [],
                "apiKeySource": "none",
                "model": "fixture-not-real",
            },
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Read"}]},
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "permission_denials": [],
                "result": q.SENTINELS[2],
            },
        ]

    def test_host_validation_rejects_unexpected_tools_auth_and_denials(self):
        encode = lambda ms: b"\n".join(json.dumps(m).encode() for m in ms)
        self.assertTrue(q.validate_host(encode(self.host()))["one_read"])
        for key, value in (
            ("tools", ["Read", "Bash"]),
            ("mcp_servers", [{}]),
            ("plugins", [{}]),
            ("apiKeySource", "env"),
        ):
            messages = self.host()
            messages[0][key] = value
            with self.assertRaises(RuntimeError):
                q.validate_host(encode(messages))
        messages = self.host()
        messages[-1]["permission_denials"] = [{}]
        with self.assertRaises(RuntimeError):
            q.validate_host(encode(messages))

    def test_schema_is_not_dispatched_in_an_unguarded_separate_boot(self):
        calls = []

        def run(argv, **kwargs):
            if argv[0] == "bundle":
                calls.append(list(map(str, argv)))
                raise RuntimeError("stop before Rails fixture")
            return b"0\n"

        with patch.object(q, "run", side_effect=run), self.assertRaises(RuntimeError):
            q.rails_interop(
                SimpleNamespace(pg_user="fixture", pg_port=5432, rails_checkout=q.CLI),
                Path("/synthetic"),
                {},
                {},
                session=lambda: self.fail("no session before pairing"),
            )
        self.assertEqual(
            calls,
            [["bundle", "exec", "ruby", str(q.HERE / "qualify_rails.rb"), "pair"]],
        )

    def test_database_cleanup_on_schema_failure_or_interrupt(self):
        for failure in (RuntimeError("fixture"), KeyboardInterrupt()):
            calls = []

            def fixture_run(argv, failure=failure, calls=calls, **kwargs):
                calls.append(list(map(str, argv)))
                self.assertNotIn("DATABASE_URL", q.clean_env())
                if argv[0] == "bundle":
                    raise failure
                return b"0\n"

            report = {}
            args = SimpleNamespace(
                pg_user="fixture", pg_port=5432, rails_checkout=q.CLI
            )
            with (
                patch.object(q, "run", side_effect=fixture_run),
                self.assertRaises(type(failure)),
            ):
                q.rails_interop(args, Path("/synthetic"), {}, report, lambda: None)
            self.assertEqual(
                [c[0] for c in calls], ["psql", "createdb", "bundle", "dropdb", "psql"]
            )
            self.assertTrue(report["database_dropped"])
            self.assertEqual(calls[1][-1], calls[3][-1])
            self.assertLessEqual(len(calls[1][-1].encode()), 63)

    def test_a_failed_session_skips_delivery_and_still_drops_the_database(self):
        calls = []

        def fixture_run(argv, **kwargs):
            calls.append([str(a) for a in argv])
            return b"0\n"

        def session():
            raise q.GateError("host_result")

        report = {}
        args = SimpleNamespace(pg_user="fixture", pg_port=5432, rails_checkout=q.CLI)
        with (
            patch.object(q, "run", side_effect=fixture_run),
            self.assertRaisesRegex(q.GateError, "host_result"),
        ):
            q.rails_interop(args, Path("/synthetic"), {}, report, session)
        self.assertEqual(
            [c[0] for c in calls], ["psql", "createdb", "bundle", "dropdb", "psql"]
        )
        self.assertEqual(calls[2][-1], "pair")
        self.assertTrue(report["database_dropped"])

    def test_pairing_and_delivery_boot_rails_on_one_fixed_port(self):
        calls, ports = [], []

        def fixture_run(argv, env=None, **kwargs):
            calls.append([str(a) for a in argv])
            if argv[0] == "bundle":
                ports.append(env["QUALIFICATION_PORT"])
                raise_after = argv[-1] == "deliver"
                if raise_after:
                    raise RuntimeError("stop after delivery")
            return b"0\n"

        args = SimpleNamespace(pg_user="fixture", pg_port=5432, rails_checkout=q.CLI)
        with (
            patch.object(q, "run", side_effect=fixture_run),
            self.assertRaises(RuntimeError),
        ):
            q.rails_interop(args, Path("/synthetic"), {}, {}, lambda: None)
        self.assertEqual(
            [c[-1] for c in calls if c[0] == "bundle"], ["pair", "deliver"]
        )
        self.assertEqual(len(set(ports)), 1)
        self.assertTrue(1024 < int(ports[0]) < 65536)

    def test_partial_rails_report_cannot_prevent_database_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rails-result.json").write_text('{"passed":')
            calls = []

            def fixture_run(argv, **kwargs):
                calls.append(str(argv[0]))
                if argv[0] == "bundle":
                    raise KeyboardInterrupt()
                return b"0\n"

            report = {}
            args = SimpleNamespace(
                pg_user="fixture", pg_port=5432, rails_checkout=q.CLI
            )
            with (
                patch.object(q, "run", side_effect=fixture_run),
                self.assertRaises(KeyboardInterrupt),
            ):
                q.rails_interop(args, root, {}, report, lambda: None)
            self.assertEqual(calls, ["psql", "createdb", "bundle", "dropdb", "psql"])
            self.assertTrue(report["database_dropped"])
            self.assertTrue(report["rails_report_unreadable"])

    def test_interrupted_create_cleans_its_verified_absent_name(self):
        calls = []

        def fixture_run(argv, **kwargs):
            calls.append(str(argv[0]))
            if argv[0] == "createdb":
                raise KeyboardInterrupt()
            return b"0\n"

        report = {}
        args = SimpleNamespace(pg_user="fixture", pg_port=5432)
        with (
            patch.object(q, "run", side_effect=fixture_run),
            self.assertRaises(KeyboardInterrupt),
        ):
            q.rails_interop(args, Path("/synthetic"), {}, report, lambda: None)
        self.assertEqual(calls, ["psql", "createdb", "dropdb", "psql"])
        self.assertTrue(report["database_dropped"])

    def test_preexisting_database_refused_without_drop(self):
        with (
            patch.object(q, "run", return_value=b"1\n") as run,
            self.assertRaises(q.GateError),
        ):
            q.rails_interop(
                SimpleNamespace(pg_user="fixture", pg_port=5432),
                Path("/synthetic"),
                {},
                {},
                lambda: None,
            )
        self.assertEqual(run.call_count, 1)

    def test_failed_precheck_never_drops_unowned_database(self):
        with (
            patch.object(q, "run", side_effect=RuntimeError("exists")) as run,
            self.assertRaises(RuntimeError),
        ):
            q.rails_interop(
                SimpleNamespace(pg_user="fixture", pg_port=5432),
                Path("/synthetic"),
                {},
                {},
                lambda: None,
            )
        self.assertEqual(run.call_count, 1)


class IsolationTest(unittest.TestCase):
    """What a live run would give setup and stock Claude, built without running either."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="claude-isolation-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.real_home = self.root / "real-home"
        self.real_home.mkdir()
        base = {"HOME": str(self.real_home), "PATH": os.environ["PATH"]}
        with (
            patch.dict(os.environ, {"XDG_DATA_HOME": str(self.real_home / "data")}),
            patch.object(q, "run", return_value=b""),
        ):
            self.machine = q.Machine(self.root / "run", {**base, **q.clean_env()})

    def test_setup_gets_a_temporary_home_and_a_fake_scheduler(self):
        env, run = self.machine.env, self.root / "run"
        self.assertEqual(env["HOME"], str(run / "home"))
        self.assertEqual(env["XDG_CONFIG_HOME"], str(run / "config"))
        self.assertNotIn("XDG_DATA_HOME", env)
        self.assertEqual(env["DEVDIARY_DEBUG_FIRST_SESSION_WAIT"], "0")
        (name,) = [k for k in env if k.startswith("DEVDIARY_COLLECTOR_")]
        self.assertIn(
            name, ("DEVDIARY_COLLECTOR_SYSTEMCTL", "DEVDIARY_COLLECTOR_LAUNCHCTL")
        )
        self.assertTrue(Path(env[name]).is_relative_to(run))
        self.assertTrue(
            Path(
                env["DBUS_SESSION_BUS_ADDRESS"].removeprefix("unix:path=")
            ).is_relative_to(run)
        )
        self.assertEqual(
            json.loads(self.machine.settings.read_text()), q.ORIGINAL_SETTINGS
        )
        self.assertTrue(self.machine.settings.is_relative_to(run / "home/.claude"))

    def test_stock_claude_can_write_only_inside_the_run(self):
        auth = self.real_home / ".claude/.credentials.json"
        claude = self.root / "claude"
        command = q.claude_command(self.machine, claude, auth, ["--version"])
        pairs = [command[i : i + 3] for i in range(len(command) - 2)]
        root = str(self.machine.root)
        self.assertEqual(
            [p for p in pairs if p[0] == "--bind"], [["--bind", root, root]]
        )
        self.assertIn(
            [
                "--ro-bind",
                str(auth),
                str(self.machine.home / ".claude/.credentials.json"),
            ],
            pairs,
        )
        mentions = [a for a in command if str(self.real_home) in a]
        self.assertEqual(mentions, [str(auth)])
        self.assertEqual(
            command[-2:], [str(self.machine.root / "stock/claude"), "--version"]
        )
        env = q.claude_env(self.machine)
        self.assertEqual(env["HOME"], str(self.machine.home))
        self.assertFalse(any(str(self.real_home) in v for v in env.values()))

    def test_only_the_debug_build_may_run_setup(self):
        with self.assertRaisesRegex(q.GateError, "debug_build_required"):
            q.debug_collector(self.root / "target/release/devdiary")
        self.assertEqual(q.debug_collector(), install_test.BIN)


class OutboxInspectionTest(unittest.TestCase):
    """Real hook rows in a real outbox, from synthetic hook input: no Claude run."""

    def setUp(self):
        self.fixture = install_test.InstallTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.install()
        self.connection = self.fixture.data / "connection"
        self.session = str(uuid.uuid4())

    def hook(self, event, **extra):
        f = self.fixture
        record = f.record()
        data = {
            "hook_event_name": event,
            "session_id": self.session,
            "cwd": str(f.demo()),
        }
        subprocess.run(
            [record["executable"], "hook", "claude", record["plan"]],
            input=json.dumps({**data, **extra}).encode(),
            env=f.env(),
            timeout=10,
            check=True,
        )

    def test_one_session_with_every_event_once_passes(self):
        self.hook("SessionStart")
        self.hook("UserPromptSubmit")
        for event in ("PreToolUse", "PostToolUse"):
            self.hook(event, tool_use_id="toolu_fixture", tool_name="Read")
        self.hook("Stop")
        with self.assertRaisesRegex(q.GateError, "runtime_event_coverage"):
            q.inspect_outbox(self.connection, self.fixture.root)
        self.hook("SessionEnd")
        rows, counts = q.inspect_outbox(self.connection, self.fixture.root)
        self.assertEqual(counts, dict.fromkeys(q.EVENTS, 1))
        self.assertEqual({r["session_id"] for r in rows}, {self.session})
        (self.connection / "outbox/stray").write_bytes(q.SENTINELS[0].encode())
        with self.assertRaisesRegex(q.GateError, "outbox_privacy"):
            q.inspect_outbox(self.connection, self.fixture.root)

    def test_two_tool_calls_fail_correlation(self):
        for event in ("SessionStart", "UserPromptSubmit"):
            self.hook(event)
        self.hook("PreToolUse", tool_use_id="toolu_a", tool_name="Read")
        self.hook("PostToolUse", tool_use_id="toolu_b", tool_name="Read")
        for event in ("Stop", "SessionEnd"):
            self.hook(event)
        with self.assertRaisesRegex(q.GateError, "runtime_tool_correlation"):
            q.inspect_outbox(self.connection, self.fixture.root)


class LiveOrderTest(unittest.TestCase):
    """execute() with every subprocess faked, past the managed-policy gate."""

    def test_a_failed_model_run_still_uninstalls_and_drops_the_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            home, rails = tmp / "real-home", tmp / "rails"
            home.mkdir()
            rails.mkdir()
            claude = tmp / "claude"
            claude.write_bytes(b"\x7fELFfixture")
            auth = tmp / "credentials.json"
            auth.write_text("{}")
            auth.chmod(0o600)
            calls = []

            def fake_run(argv, env=None, **kwargs):
                argv = [str(a) for a in argv]
                calls.append((argv, env or {}))
                if argv[:2] == ["mise", "which"]:
                    return (
                        sys.executable if argv[2] == "ruby" else str(claude)
                    ).encode()
                if argv[0] == "bwrap" and argv[-1] == "--version":
                    return b"2.1.280 (Claude Code)\n"
                if argv[0] == "bwrap" and argv[-2:] == ["auth", "status"]:
                    return json.dumps(
                        {
                            "loggedIn": True,
                            "authMethod": "claude.ai",
                            "apiProvider": "firstParty",
                        }
                    ).encode()
                if argv[0] == "bwrap" and "--print" in argv:
                    raise q.GateError("subprocess_exit")
                if argv[0] == "psql":
                    return b"0\n"
                return b""

            args = SimpleNamespace(
                rails_checkout=rails,
                auth_file=auth,
                claude_version="2.1.280",
                pg_user="fixture",
                pg_port=5432,
            )
            report = {"model_runs_started": 0}
            with (
                patch.object(q, "check_managed_policy"),
                patch.object(
                    q,
                    "clean_env",
                    return_value={"HOME": str(home), "PATH": os.environ["PATH"]},
                ),
                patch.object(q, "run", side_effect=fake_run),
                self.assertRaisesRegex(q.GateError, "subprocess_exit"),
            ):
                q.execute(args, report)

        def step(argv):
            if argv[0] == "bwrap" and "--print" in argv:
                return "model"
            if argv[:3] == ["bundle", "exec", "ruby"]:
                return argv[-1]
            if argv[0] == "dropdb" or argv[-1] == "uninstall":
                return argv[-1] if argv[-1] == "uninstall" else "dropdb"
            return None

        steps = [step(argv) for argv, _ in calls if step(argv)]
        self.assertEqual(steps, ["pair", "model", "dropdb", "uninstall"])
        self.assertEqual(report["model_runs_started"], 1)
        self.assertTrue(report["registration_removed"])
        isolated = [
            env["HOME"]
            for argv, env in calls
            if argv[0] == "bwrap" or argv[-1] in ("pair", "uninstall")
        ]
        self.assertEqual(len(isolated), 5)
        self.assertEqual(len(set(isolated)), 1)
        self.assertEqual(Path(isolated[0]).name, "home")
        self.assertIn("claude-qualification-", isolated[0])


@unittest.skipUnless(
    os.environ.get("DEVDIARY_RAILS_CHECKOUT"),
    "set DEVDIARY_RAILS_CHECKOUT to a reviewed DevDiary checkout and run on its Ruby PATH",
)
class SyntheticRailsStagesTest(unittest.TestCase):
    """qualify_rails.rb's pair and deliver stages against real Rails, with six
    synthetic hook events in place of the stock session. No vendor or model."""

    def test_setup_pairs_against_rails_and_the_rows_are_delivered(self):
        with tempfile.TemporaryDirectory(prefix="claude-qualification-") as tmp:
            root = Path(tmp).resolve()
            machine = q.Machine(root, q.clean_env())
            exe = root / "collector"
            exe.write_bytes(q.debug_collector().read_bytes())
            exe.chmod(0o700)
            report = {}

            def session():
                record = json.loads(
                    (machine.connection.parent / "install.json").read_text()
                )
                for event in q.EVENTS:
                    data = {
                        "hook_event_name": event,
                        "session_id": "synthetic-session",
                        "cwd": str(machine.repo),
                        "tool_use_id": "toolu_synthetic",
                        "tool_name": "Read",
                    }
                    subprocess.run(
                        [record["executable"], "hook", "claude", record["plan"]],
                        input=json.dumps(data).encode(),
                        env=machine.env,
                        timeout=10,
                        check=True,
                    )
                rows, _ = q.inspect_outbox(machine.connection, root)
                q.save(root / "observations.json", rows)

            args = SimpleNamespace(
                rails_checkout=Path(os.environ["DEVDIARY_RAILS_CHECKOUT"]),
                pg_user=os.environ.get("USER", ""),
                pg_port=5432,
            )
            try:
                q.rails_interop(args, root, machine.env, report, session)
                doctor = json.loads(q.run([exe, "doctor", "--json"], env=machine.env))
                expected = json.loads((root / "expected-receipts.json").read_text())
                self.assertEqual(doctor["pending"], 0)
                self.assertEqual(doctor["delivered"], len(expected))
                self.assertIn(doctor["last_receipt"], expected)
            finally:
                q.run([exe, "uninstall"], env=machine.env)
            self.assertEqual(
                json.loads(machine.settings.read_text()), q.ORIGINAL_SETTINGS
            )
            self.assertTrue(report["rails"]["passed"], report)
            self.assertTrue(report["rails"]["collector_revoked"], report)
            self.assertTrue(report["database_dropped"])
            log = machine.scheduler.log()
            for call in machine.scheduler.started() + machine.scheduler.stopped():
                self.assertIn(call, log)


if __name__ == "__main__":
    unittest.main()
