from __future__ import annotations

import ctypes
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from devdiary import runner
from devdiary.config import ConfigError, default_registry

DUMMY_KEY = "test-only-not-a-live-key"


class CaptureHandler(BaseHTTPRequestHandler):
    authorization: str | None = None
    payload: dict | None = None

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        self.__class__.authorization = self.headers.get("Authorization")
        self.__class__.payload = json.loads(self.rfile.read(length))
        payload = self.__class__.payload
        assert payload is not None
        body = json.dumps(
            {
                "actor_ref": payload["actor"]["ref"],
                "event_id": payload["event_id"],
                "run_ref": payload["run_ref"],
                "session_id": 1,
            }
        ).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        CaptureHandler.authorization = None
        CaptureHandler.payload = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.registry = default_registry(
            "urn:acme",
            "urn:acme:human:owner",
            f"http://127.0.0.1:{self.server.server_port}/ingest/v1/sessions",
        )
        self.actor = {
            "actor_ref": "urn:acme:actor:reviewer",
            "kind": "agent",
            "display_name": "Reviewer",
            "humanized_name": "Alex",
            "attester_ref": "urn:acme:attester:launcher",
            "identities": {
                "git_name": "Reviewer Agent",
                "git_email": "reviewer@agents.example.test",
            },
        }
        self.registry["actors"] = [self.actor]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_launcher_hides_ingest_key_sets_private_context_and_emits_git_work(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            self._initialize_repository(repository)
            observed = root / "observed.json"
            child = (
                "import json,os,stat,subprocess; from pathlib import Path; "
                "context=Path(os.environ['DEVDIARY_ATTRIBUTION_CONTEXT']); "
                "Path('produced.txt').write_text('agent output'); "
                "subprocess.run(['git','add','produced.txt'],check=True); "
                "subprocess.run(['git','commit','-m','agent work'],check=True,stdout=subprocess.DEVNULL); "
                "Path(os.environ['OBSERVED']).write_text(json.dumps({"
                "'has_key': 'DEVDIARY_INGEST_KEY' in os.environ,"
                "'actor': os.environ['DEVDIARY_ACTOR_REF'],"
                "'run': os.environ['DEVDIARY_RUN_REF'],"
                "'context': json.loads(context.read_text()),"
                "'mode': stat.S_IMODE(context.stat().st_mode)}))"
            )
            environment = dict(os.environ)
            environment.update(
                {"DEVDIARY_INGEST_KEY": DUMMY_KEY, "OBSERVED": str(observed)}
            )

            result = runner.run_command(
                self.registry,
                self.actor,
                [sys.executable, "-c", child],
                repository,
                environment=environment,
                spool_directory=root / "pending",
            )

            child_state = json.loads(observed.read_text())
            self.assertEqual(0, result.exit_code)
            self.assertTrue(result.emitted)
            self.assertIsNone(result.pending_path)
            self.assertFalse(child_state["has_key"])
            if os.name == "nt":
                self.assertFalse(child_state["mode"] & stat.S_IWRITE)
            else:
                self.assertEqual(0o400, child_state["mode"])
            self.assertEqual(self.actor["actor_ref"], child_state["actor"])
            self.assertEqual(child_state["run"], child_state["context"]["run"]["ref"])
            self.assertEqual(f"Bearer {DUMMY_KEY}", CaptureHandler.authorization)
            self.assertEqual("run.completed", result.envelope["event_type"])
            self.assertEqual(["acme/repo"], result.envelope["work"]["repositories"])
            self.assertEqual(1, len(result.envelope["work"]["commits"]))
            self.assertNotIn(DUMMY_KEY, json.dumps(CaptureHandler.payload))
            author = subprocess.check_output(
                ["git", "show", "-s", "--format=%an <%ae>"], cwd=repository, text=True
            ).strip()
            self.assertEqual("Reviewer Agent <reviewer@agents.example.test>", author)

    def test_warn_mode_does_not_create_an_unauthenticated_queue_entry_when_key_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / "pending"
            result = runner.run_command(
                self.registry,
                self.actor,
                [sys.executable, "-c", "raise SystemExit(0)"],
                root,
                environment={},
                spool_directory=pending,
            )

            self.assertEqual(0, result.exit_code)
            self.assertFalse(result.emitted)
            self.assertIsNone(result.pending_path)
            self.assertEqual([], list(pending.glob("*.json")))

    def test_malformed_transport_url_is_queued_after_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = json.loads(json.dumps(self.registry))
            registry["ingest"]["url"] = "https://example.test:not-a-port/ingest"
            result = runner.run_command(
                registry,
                self.actor,
                [sys.executable, "-c", "raise SystemExit(0)"],
                Path(directory),
                environment={"DEVDIARY_INGEST_KEY": DUMMY_KEY},
                spool_directory=Path(directory) / "pending",
            )

            self.assertEqual(0, result.exit_code)
            self.assertFalse(result.emitted)
            self.assertIsNotNone(result.pending_path)

    def test_enforce_mode_fails_a_successful_command_when_declaration_cannot_be_sent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.registry["defaults"]["enforcement"] = "enforce"
            result = runner.run_command(
                self.registry,
                self.actor,
                [sys.executable, "-c", "raise SystemExit(0)"],
                Path(directory),
                environment={},
                spool_directory=Path(directory) / "pending",
            )
            self.assertEqual(runner.ENFORCEMENT_FAILURE, result.exit_code)

    def test_command_failure_is_preserved_in_enforce_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.registry["defaults"]["enforcement"] = "enforce"
            result = runner.run_command(
                self.registry,
                self.actor,
                [sys.executable, "-c", "raise SystemExit(17)"],
                Path(directory),
                environment={},
                spool_directory=Path(directory) / "pending",
            )
            self.assertEqual(17, result.exit_code)
            self.assertEqual("run.failed", result.envelope["event_type"])

    @unittest.skipUnless(
        hasattr(__import__("signal"), "SIGTERM"), "SIGTERM is not available"
    )
    def test_unexpected_signal_terminated_command_is_declared_failed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {"DEVDIARY_INGEST_KEY": DUMMY_KEY}
            result = runner.run_command(
                self.registry,
                self.actor,
                [
                    sys.executable,
                    "-c",
                    "import os,signal; os.kill(os.getpid(), signal.SIGTERM)",
                ],
                Path(directory),
                environment=environment,
                spool_directory=Path(directory) / "pending",
            )

            expected_exit_code = 15 if os.name == "nt" else 143
            self.assertEqual(expected_exit_code, result.exit_code)
            self.assertEqual("run.failed", result.envelope["event_type"])

    @unittest.skipUnless(
        os.name == "posix" and Path("/proc").exists(), "requires /proc"
    )
    def test_failed_command_cannot_leave_descendants_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "descendant.pid"
            script = (
                "import os,pathlib,signal,subprocess,sys,time;"
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']);"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid));"
                "time.sleep(0.2);os.kill(os.getpid(),signal.SIGTERM)"
            )
            result = runner.run_command(
                self.registry,
                self.actor,
                [sys.executable, "-c", script],
                root,
                environment={"DEVDIARY_INGEST_KEY": DUMMY_KEY},
                spool_directory=root / "pending",
            )

            descendant_pid = int(pid_path.read_text())
            for _ in range(50):
                if not self._process_is_running(descendant_pid):
                    break
                time.sleep(0.02)

            self.assertFalse(self._process_is_running(descendant_pid))
            self.assertEqual(143, result.exit_code)
            self.assertEqual("run.failed", result.envelope["event_type"])

    @unittest.skipUnless(
        os.name == "posix" and hasattr(signal, "SIGTERM"),
        "requires POSIX SIGTERM",
    )
    def test_launcher_interruption_is_cancelled_and_stops_the_process_tree(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready = root / "ready"

            def interrupt_launcher() -> None:
                for _ in range(100):
                    if ready.exists():
                        os.kill(os.getpid(), signal.SIGTERM)
                        return
                    time.sleep(0.01)

            interrupter = threading.Thread(target=interrupt_launcher)
            interrupter.start()
            try:
                result = runner.run_command(
                    self.registry,
                    self.actor,
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(60)",
                    ],
                    root,
                    environment={"DEVDIARY_INGEST_KEY": DUMMY_KEY},
                    spool_directory=root / "pending",
                )
            finally:
                interrupter.join(timeout=2)

            self.assertFalse(interrupter.is_alive())
            self.assertEqual(143, result.exit_code)
            self.assertEqual("run.cancelled", result.envelope["event_type"])

    @unittest.skipUnless(
        os.name == "posix" and Path("/proc").exists(), "requires Linux /proc"
    )
    def test_launcher_scrubs_the_inherited_key_from_native_parent_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observed = root / "observed"
            source = Path(__file__).parents[1] / "src"
            child = (
                "import os,pathlib;"
                f"pathlib.Path({str(observed)!r}).write_text(str("
                f"{DUMMY_KEY.encode()!r} in pathlib.Path(f'/proc/{{os.getppid()}}/environ').read_bytes()))"
            )
            launcher = (
                "from pathlib import Path; from devdiary import runner;"
                "from devdiary.config import default_registry;"
                "registry=default_registry('urn:acme','urn:acme:human:owner',None);"
                "registry['defaults']['enforcement']='off';"
                "actor={'actor_ref':'urn:acme:actor:reviewer','kind':'agent',"
                "'display_name':'Reviewer','attester_ref':'urn:acme:attester:launcher'};"
                f"result=runner.run_command(registry,actor,[{sys.executable!r},'-c',{child!r}],Path({str(root)!r}));"
                "raise SystemExit(result.exit_code)"
            )
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(source)
            environment["DEVDIARY_INGEST_KEY"] = DUMMY_KEY

            completed = subprocess.run(
                [sys.executable, "-c", launcher], env=environment, check=False
            )

            self.assertEqual(0, completed.returncode)
            self.assertEqual("False", observed.read_text())

    def test_nested_launcher_inherits_parent_run_without_allowing_it_to_override_actor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "parent-context.json"
            parent.write_text(
                json.dumps(
                    {
                        "actor": {"ref": "urn:acme:actor:some-other-actor"},
                        "run": {"ref": "urn:devdiary:run:parent"},
                    }
                )
            )
            environment = {
                "DEVDIARY_INGEST_KEY": DUMMY_KEY,
                "DEVDIARY_ATTRIBUTION_CONTEXT": str(parent),
            }

            result = runner.run_command(
                self.registry,
                self.actor,
                [sys.executable, "-c", "raise SystemExit(0)"],
                root,
                environment=environment,
                spool_directory=root / "pending",
            )

            self.assertEqual(
                "urn:devdiary:run:parent", result.envelope["parent_run_ref"]
            )
            self.assertEqual(self.actor["actor_ref"], result.envelope["actor"]["ref"])

    def test_explicit_work_references_extend_git_discovery_without_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = {"DEVDIARY_INGEST_KEY": DUMMY_KEY}
            result = runner.run_command(
                self.registry,
                self.actor,
                [sys.executable, "-c", "raise SystemExit(0)"],
                Path(directory),
                environment=environment,
                spool_directory=Path(directory) / "pending",
                explicit_work={
                    "repositories": ["acme/repo", "acme/repo"],
                    "commits": ["a" * 40],
                    "pull_requests": ["https://github.com/acme/repo/pull/7"],
                    "issues": ["https://github.com/acme/repo/issues/9"],
                    "artifacts": ["urn:artifact:build:1"],
                },
            )

            self.assertEqual(["acme/repo"], result.envelope["work"]["repositories"])
            self.assertEqual(["a" * 40], result.envelope["work"]["commits"])
            self.assertEqual(
                ["https://github.com/acme/repo/pull/7"],
                result.envelope["work"]["pull_requests"],
            )
            self.assertEqual(
                ["urn:artifact:build:1"], result.envelope["work"]["artifacts"]
            )

    def test_an_explicit_empty_environment_never_falls_back_to_parent_secrets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                os.environ, {"DEVDIARY_INGEST_KEY": DUMMY_KEY}, clear=True
            ):
                result = runner.run_command(
                    self.registry,
                    self.actor,
                    [sys.executable, "-c", "raise SystemExit(0)"],
                    Path(directory),
                    environment={},
                    spool_directory=Path(directory) / "pending",
                )

            self.assertFalse(result.emitted)
            self.assertIsNone(result.pending_path)

    def test_invalid_explicit_work_is_rejected_before_the_command_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started"
            with self.assertRaisesRegex(ConfigError, "non-empty strings"):
                runner.run_command(
                    self.registry,
                    self.actor,
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).touch()",
                    ],
                    Path(directory),
                    environment={},
                    explicit_work={"issues": [""]},
                )

            self.assertFalse(marker.exists())

    def test_native_environment_scrub_failure_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "started"
            with (
                mock.patch(
                    "devdiary.runner.scrub_inherited_variable",
                    side_effect=ValueError("simulated native scrub failure"),
                ),
                self.assertRaisesRegex(ConfigError, "could not scrub"),
            ):
                runner.run_command(
                    self.registry,
                    self.actor,
                    [
                        sys.executable,
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).touch()",
                    ],
                    Path(directory),
                    environment={"DEVDIARY_INGEST_KEY": DUMMY_KEY},
                )

            self.assertFalse(marker.exists())

    def test_signal_handlers_are_armed_before_the_process_is_started(self) -> None:
        events: list[str] = []
        process = mock.Mock(pid=123)
        process.wait.side_effect = lambda: events.append("wait") or 0
        process_tree = mock.Mock()
        process_tree.resume.side_effect = lambda: events.append("resume")

        def start_process(*_args, **_kwargs):
            events.append("start")
            return process

        def install_handler(*_args, **_kwargs):
            events.append("handler")

        with (
            mock.patch("devdiary.runner.subprocess.Popen", start_process),
            mock.patch(
                "devdiary.runner.ProcessTree.attach",
                return_value=process_tree,
            ),
            mock.patch(
                "devdiary.runner.signal.signal",
                side_effect=install_handler,
            ),
        ):
            runner._spawn(["runtime"], Path.cwd(), {})

        self.assertLess(events.index("handler"), events.index("start"))

    def test_windows_process_is_suspended_until_job_assignment(self) -> None:
        events: list[str] = []
        creation: dict[str, object] = {}
        cwd = Path.cwd()
        process = mock.Mock(pid=123)
        process.wait.side_effect = lambda: events.append("wait") or 0
        process_tree = mock.Mock()
        process_tree.resume.side_effect = lambda: events.append("resume")

        def start_process(*_args, **kwargs):
            creation.update(kwargs)
            events.append("start")
            return process

        def attach(_process):
            events.append("attach")
            return process_tree

        with (
            mock.patch.object(runner.os, "name", "nt"),
            mock.patch("devdiary.runner.subprocess.Popen", start_process),
            mock.patch("devdiary.runner.ProcessTree.attach", side_effect=attach),
        ):
            result = runner._spawn(["runtime"], cwd, {})

        self.assertEqual((0, False), result)
        self.assertEqual(0x00000004, creation["creationflags"])
        self.assertLess(events.index("attach"), events.index("resume"))
        self.assertLess(events.index("resume"), events.index("wait"))

    def test_signal_during_resume_interrupts_immediately_after_attachment(self) -> None:
        events: list[str] = []
        handlers: dict[int, object] = {}
        process = mock.Mock(pid=123)
        process.wait.return_value = 0
        process_tree = mock.Mock()

        def install_handler(signum, handler):
            if callable(handler):
                handlers[signum] = handler

        def resume():
            handler = handlers[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)
            events.append("resume-returned")

        process_tree.resume.side_effect = resume
        with (
            mock.patch("devdiary.runner.subprocess.Popen", return_value=process),
            mock.patch(
                "devdiary.runner.ProcessTree.attach",
                return_value=process_tree,
            ),
            mock.patch(
                "devdiary.runner.signal.signal",
                side_effect=install_handler,
            ),
        ):
            result = runner._spawn(["runtime"], Path.cwd(), {})

        self.assertEqual((128 + signal.SIGTERM, True), result)
        self.assertNotIn("resume-returned", events)
        process_tree.stop.assert_called_once()

    @unittest.skipUnless(os.name == "nt", "requires Windows Job Objects")
    def test_windows_job_contains_an_immediately_spawned_descendant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_path = root / "descendant.pid"
            script = (
                "import pathlib,subprocess,sys;"
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))"
            )

            result = runner.run_command(
                self.registry,
                self.actor,
                [sys.executable, "-c", script],
                root,
                environment={"DEVDIARY_INGEST_KEY": DUMMY_KEY},
                spool_directory=root / "pending",
            )

            descendant_pid = int(pid_path.read_text())
            for _ in range(100):
                if not self._windows_process_is_running(descendant_pid):
                    break
                time.sleep(0.02)

            self.assertEqual(0, result.exit_code)
            self.assertFalse(self._windows_process_is_running(descendant_pid))

    def test_sensitive_work_reference_url_components_are_rejected_before_start(
        self,
    ) -> None:
        references = (
            "https://artifacts.example.test/build/1?token=test-only-sensitive-value",
            "//artifacts.example.test/build/1?auth=test-only-sensitive-value",
            "urn:artifact:build:1#test-only-sensitive-value",
        )
        for reference in references:
            with (
                self.subTest(reference=reference),
                tempfile.TemporaryDirectory() as directory,
            ):
                marker = Path(directory) / "started"
                with self.assertRaisesRegex(ConfigError, "query or fragment"):
                    runner.run_command(
                        self.registry,
                        self.actor,
                        [
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; Path({str(marker)!r}).touch()",
                        ],
                        Path(directory),
                        environment={},
                        explicit_work={"artifacts": [reference]},
                    )

                self.assertFalse(marker.exists())

    def _process_is_running(self, pid: int) -> bool:
        stat_path = Path(f"/proc/{pid}/stat")
        if not stat_path.exists():
            return False
        fields = stat_path.read_text().split()
        return len(fields) > 2 and fields[2] != "Z"

    def _windows_process_is_running(self, pid: int) -> bool:
        kernel32 = vars(ctypes)["WinDLL"]("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            return (
                bool(get_exit_code(handle, ctypes.byref(exit_code)))
                and exit_code.value == 259
            )
        finally:
            close_handle(handle)

    def _initialize_repository(self, repository: Path) -> None:
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Human"], cwd=repository, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "human@example.test"],
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:acme/repo.git"],
            cwd=repository,
            check=True,
        )
        (repository / "initial.txt").write_text("initial")
        subprocess.run(["git", "add", "initial.txt"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repository, check=True)


if __name__ == "__main__":
    unittest.main()
