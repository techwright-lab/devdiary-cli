"""Harness control-flow fixtures ONLY; no stock runtime/provider claims or calls."""

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
            # Even no on-disk policy does not prove current/cached remote absence.
            with self.assertRaisesRegex(q.GateError, "remote_managed_policy_unverified"):
                q.check_managed_policy(home, system)
            with (
                patch.object(q, "clean_env", return_value={"HOME": str(home)}),
                patch.object(q, "run", side_effect=AssertionError("no subprocess")),
                self.assertRaises(q.GateError),
            ):
                q.execute(SimpleNamespace(), {})

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
                        "--collector",
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

    def test_registration_cleanup_on_fake_vendor_failure_and_interrupt(self):
        # Real collector registration; fake vendor never constitutes qualification.
        collector = q.CLI / "collector-rs/target/debug/devdiary-collector"
        real_run = q.run
        for failure in (RuntimeError("fixture-secret"), KeyboardInterrupt()):
            with tempfile.TemporaryDirectory() as home:
                env = {"HOME": home, "PATH": os.environ["PATH"]}
                report = {}
                args = SimpleNamespace(
                    rails_checkout=q.CLI, collector=collector, claude_version="fixture"
                )
                calls = []

                def fixture_run(argv, failure=failure, calls=calls, **kwargs):
                    strings = list(map(str, argv))
                    if strings[:2] == ["mise", "which"]:
                        return str(collector).encode()
                    if strings == ["bundle", "check"]:
                        return b""
                    if "--version" in strings:
                        return b"fixture (Claude Code)"
                    if "auth" in strings:
                        return b'{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","email":"PRIVATE"}'
                    if "--print" in strings:
                        calls.append("fake_vendor")
                        raise failure
                    return real_run(argv, **kwargs)

                with (
                    patch.object(q, "clean_env", return_value=env),
                    patch.object(q, "check_managed_policy"),
                    patch.object(q, "run", side_effect=fixture_run),
                    self.assertRaises(type(failure)),
                ):
                    q.execute(args, report)
                self.assertEqual(calls, ["fake_vendor"])
                self.assertTrue(report["registration_removed"])
                self.assertTrue(report["live_settings_unchanged"])
                self.assertNotIn("email", report["auth"])

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
                q.rails_interop(args, Path("/synthetic"), {}, report)
            self.assertEqual(
                [c[0] for c in calls], ["psql", "createdb", "bundle", "dropdb", "psql"]
            )
            self.assertTrue(report["database_dropped"])
            self.assertEqual(calls[1][-1], calls[3][-1])

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
                q.rails_interop(args, root, {}, report)
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
            q.rails_interop(args, Path("/synthetic"), {}, report)
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
            )
        self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
