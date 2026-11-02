"""Opt-in pinned stock Codex qualification. Default invocation is network-inert."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path

import install_test
import qualify_claude

VERSION = "0.166.0"
COMPONENTS = (
    "bin/codex",
    "bin/codex-exec-server",
    "bin/codex-code-mode-host",
    "codex-resources/bwrap",
)


class GateError(RuntimeError):
    """Only fixed diagnostic codes leave the qualification process."""


def package_identity(package, expected):
    try:
        root = package.resolve(strict=True)
        manifest = json.loads((root / "codex-package.json").read_bytes())
        if manifest.get("version") != VERSION:
            raise GateError("package_version_mismatch")
        hashes = {}
        for name in COMPONENTS:
            path = (root / name).resolve(strict=True)
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or not path.stat().st_mode & 0o100
            ):
                raise GateError("package_incomplete")
            with path.open("rb") as file:
                if file.read(4) != b"\x7fELF":
                    raise GateError("package_incomplete")
                file.seek(0)
                hashes[name] = hashlib.file_digest(file, "sha256").hexdigest()
        if hashes["bin/codex"] != expected:
            raise GateError("package_hash_mismatch")
        return hashes
    except (OSError, ValueError, TypeError) as error:
        raise GateError("package_incomplete") from error


def require(condition, code):
    if not condition:
        raise GateError(code)


class AppServer:
    """Ordinary stock app-server requests; hook input is never synthesized."""

    def __init__(self, harness):
        self.harness = harness
        self.counter, self.buffer, self.total = 0, [], 0

    def __enter__(self):
        self.process = subprocess.Popen(
            self.harness.command("app-server", live=True),
            env=self.harness.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            start_new_session=True,
        )
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "devdiary-qualification", "version": "0.1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.send({"method": "initialized"})
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *_):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()

    def send(self, value):
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        self.process.stdin.flush()

    def read(self, deadline):
        while time.monotonic() < deadline:
            if select.select([self.process.stdout], [], [], 0.1)[0]:
                raw = self.process.stdout.readline(262145)
                require(raw and len(raw) <= 262144, "rpc_output_limit")
                self.total += len(raw)
                require(self.total <= 2_000_000, "rpc_output_limit")
                return json.loads(raw)
        raise GateError("rpc_timeout")

    def request(self, method, params):
        self.counter += 1
        number = self.counter
        self.send({"id": number, "method": method, "params": params})
        deadline = time.monotonic() + 45
        while True:
            value = self.read(deadline)
            if value.get("id") == number and "method" not in value:
                if "error" in value:
                    self.harness.report["rpc_failure"] = {
                        "method": method,
                        "code": value["error"].get("code"),
                    }
                    raise GateError("rpc_request_failed")
                return value["result"]
            self.buffer.append(value)

    def event(self, method):
        deadline = time.monotonic() + 45
        while True:
            for i, value in enumerate(self.buffer):
                if value.get("method") == method:
                    return self.buffer.pop(i)
            self.buffer.append(self.read(deadline))


class Harness:
    def __init__(self, args, report):
        self.args, self.report = args, report
        self.fixture = install_test.InstallTest()
        self.seen = 0

    def __enter__(self):
        try:
            self.prepare()
            return self
        except BaseException:
            self.fixture.doCleanups()
            raise

    def __exit__(self, *_):
        self.fixture.doCleanups()

    def prepare(self):
        scratch = Path.home() / ".cache/devdiary-codex-qualification"
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        original = tempfile.tempdir
        try:
            tempfile.tempdir = str(scratch)
            self.fixture.setUp()
        finally:
            tempfile.tempdir = original
        f = self.fixture
        f.runtime = "codex"
        f.data /= "codex"
        f.settings = f.home / ".codex/hooks.json"
        env = {
            **f.env(),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(f.root / "no-user-bus"),
        }
        result = subprocess.run(
            f.install_args("--vendor", "codex", "--yes"),
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        require(result.returncode == 0, "collector_install_failed")
        self.repo = f.root / "demo"
        subprocess.run(
            ["git", "init", "-q", str(self.repo)], check=True, capture_output=True
        )
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
            capture_output=True,
        )
        self.native = f.root / "stock-package/bin/codex"
        self.prefix = ["bwrap", "--tmpfs", "/"]
        for path in ("/usr", "/bin", "/lib", "/lib64", "/etc"):
            self.prefix += ["--ro-bind", path, path]
        # resolv.conf may point outside /etc. Mount the target, not unrelated /run data.
        resolver = Path("/etc/resolv.conf").resolve(strict=True)
        self.prefix += [
            "--ro-bind",
            str(resolver),
            str(resolver),
            "--dev",
            "/dev",
            "--unshare-pid",
            "--die-with-parent",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(f.root),
            str(f.root),
            "--ro-bind",
            str(self.args.package.resolve()),
            str(f.root / "stock-package"),
            "--chdir",
            str(self.repo),
        ]
        self.env = {
            "HOME": str(f.home),
            "PATH": str(f.root / "stock-package/codex-path") + ":/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
        }
        cache = Path.home() / ".codex/models_cache.json"
        if cache.is_file():
            shutil.copyfile(cache, f.home / ".codex/models_cache.json")
        (f.home / ".codex/auth.json").write_text(
            json.dumps({"OPENAI_API_KEY": "qualification-dummy-no-provider"})
        )
        (f.home / ".codex/auth.json").chmod(0o600)
        self.trust_hooks()
        self.report["ordinary_trust"] = True

    def command(self, *args, live=False):
        mounts = (
            [
                "--ro-bind",
                str(self.args.auth_file.resolve()),
                str(self.fixture.home / ".codex/auth.json"),
            ]
            if live
            else ["--unshare-net"]
        )
        prefix = (
            self.prefix if live else [p for p in self.prefix if p != "--unshare-pid"]
        )
        return [*prefix, *mounts, "--", str(self.native), *args]

    def trust_hooks(self):
        pid, terminal = pty.fork()
        if pid == 0:
            os.execvpe(
                "bwrap", self.command("--no-daemon", "--no-alt-screen"), self.env
            )
        fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))

        def drain(seconds):
            seen = bytearray()
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if not select.select([terminal], [], [], 0.1)[0]:
                    continue
                try:
                    chunk = os.read(terminal, 65536)
                except OSError as error:
                    raise GateError("trust_ui_closed") from error
                seen.extend(chunk)
                require(len(seen) < 2_000_000, "trust_ui_output_limit")
                if b"\x1b]10;?" in chunk or b"\x1b]11;?" in chunk:
                    os.write(
                        terminal,
                        b"\x1b]10;rgb:ffff/ffff/ffff\x1b\\\x1b]11;rgb:0000/0000/0000\x1b\\\x1b[1;1R",
                    )
            return re.sub(
                r"\x1b\[[0-?]*[ -/]*[@-~]", "", bytes(seen).decode(errors="replace")
            )

        try:
            require("Trust this folder?" in drain(3), "folder_trust_ui_missing")
            os.write(terminal, b"\r")
            drain(3)
            os.write(terminal, b"/hooks\r")
            require("Trust all and continue" in drain(3), "hook_trust_ui_missing")
            os.write(terminal, b"2\r")
            drain(3)
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)
            os.close(terminal)
        hooks = self.hooks()
        require(
            len(hooks) == 10 and all(h["trustStatus"] == "trusted" for h in hooks),
            "hook_trust_not_saved",
        )

    def hooks(self):
        process = subprocess.Popen(
            self.command("app-server"),
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def request(number, method, params):
            process.stdin.write(
                (
                    json.dumps({"id": number, "method": method, "params": params})
                    + "\n"
                ).encode()
            )
            process.stdin.flush()
            end = time.monotonic() + 15
            while time.monotonic() < end:
                if select.select([process.stdout], [], [], 0.1)[0]:
                    raw = process.stdout.readline(262145)
                    require(raw and len(raw) <= 262144, "hook_list_output_limit")
                    reply = json.loads(raw)
                    if reply.get("id") == number:
                        require("error" not in reply, "hook_list_failed")
                        return reply["result"]
            raise GateError("hook_list_timeout")

        try:
            request(
                1,
                "initialize",
                {
                    "clientInfo": {"name": "devdiary-qualification", "version": "0.1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            return request(2, "hooks/list", {"cwds": [str(self.repo)]})["data"][0][
                "hooks"
            ]
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()

    def exec(self, prompt, resume=None):
        require(
            self.report["model_runs_started"] < self.args.max_model_runs,
            "model_run_limit",
        )
        self.report["model_runs_started"] += 1
        args = ["--no-daemon", "exec"]
        if resume:
            args += ["resume", resume]
        args += ["--json", prompt]
        try:
            raw = qualify_claude.run(
                self.command(*args, live=True), env=self.env, timeout=45
            )
        except qualify_claude.GateError as error:
            raise GateError("stock_exec_failed") from error
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def rows(self):
        f = self.fixture
        f.run_collector("timer-sync", f.data / "connection")
        rows = f.received[self.seen :]
        self.seen = len(f.received)
        require(
            all(
                r.get("runtime") == "codex"
                and r.get("attribution_basis") == "unknown"
                and "actor_ref" not in r
                for r in rows
            ),
            "runtime_or_actor_mismatch",
        )
        require("PRIVATE_C1_QUALIFY" not in json.dumps(rows), "wire_privacy_failed")
        for path in (f.data / "connection/outbox").iterdir():
            if path.is_file():
                require(
                    b"PRIVATE_C1_QUALIFY" not in path.read_bytes(),
                    "spool_privacy_failed",
                )
        return rows

    def tool(self):
        protocol = self.exec(
            "Run exactly one shell command: printf PRIVATE_C1_QUALIFY. Then reply OK."
        )
        rows = self.rows()
        events = {r["event"] for r in rows}
        require(
            {
                "SessionStart",
                "UserPromptSubmit",
                "PreToolUse",
                "PostToolUse",
                "Stop",
                "SessionEnd",
            }
            <= events,
            "stock_tool_events_missing",
        )
        tools = [r for r in rows if r["event"] in ("PreToolUse", "PostToolUse")]
        require(
            len({r.get("tool_use_id") for r in tools}) == 1
            and tools[0].get("tool_use_id"),
            "tool_correlation_failed",
        )
        turns = [
            r
            for r in rows
            if r["event"] in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop")
        ]
        require(
            len({r.get("turn_id") for r in turns}) == 1 and turns[0].get("turn_id"),
            "turn_correlation_failed",
        )
        thread = next(
            (r.get("thread_id") for r in protocol if r.get("type") == "thread.started"),
            None,
        )
        require(thread, "thread_id_missing")
        return {
            "passed": True,
            "events": [r["event"] for r in rows],
            "observations": rows,
        }, thread

    def resume(self, thread, previous):
        self.exec(
            "Run exactly one shell command: printf PRIVATE_C1_QUALIFY. Then reply OK.",
            resume=thread,
        )
        rows = self.rows()
        require(
            any(
                r["event"] == "SessionStart" and r.get("source") == "resume"
                for r in rows
            ),
            "resume_start_missing",
        )
        require(
            all(r["session_id"] == previous[0]["session_id"] for r in rows),
            "resume_session_changed",
        )
        previous_turns = {r["turn_id"] for r in previous if "turn_id" in r}
        require(
            any(
                r["event"] == "UserPromptSubmit"
                and r.get("turn_id") not in previous_turns
                for r in rows
            ),
            "resume_turn_reused",
        )
        require(
            {"PreToolUse", "PostToolUse", "Stop", "SessionEnd"}
            <= {r["event"] for r in rows},
            "resume_tool_events_missing",
        )
        return {"passed": True, "observations": rows}

    def permission(self):
        require(
            self.report["model_runs_started"] < self.args.max_model_runs,
            "model_run_limit",
        )
        self.report["model_runs_started"] += 1
        rules = self.fixture.home / ".codex/rules"
        rules.mkdir(exist_ok=True)
        policy = rules / "qualification.rules"
        policy.write_text('prefix_rule(pattern=["python3"], decision="prompt")\n')
        try:
            with AppServer(self) as server:
                thread = server.request(
                    "thread/start",
                    {
                        "cwd": str(self.repo),
                        "approvalPolicy": "on-request",
                        "sandbox": "read-only",
                    },
                )["thread"]["id"]
                server.request(
                    "turn/start",
                    {
                        "threadId": thread,
                        "input": [
                            {
                                "type": "text",
                                "text": "Run exactly one shell command: python3 -c 'print(\"PRIVATE_C1_QUALIFY\")'. Then reply OK.",
                            }
                        ],
                    },
                )
                approval = server.event("item/commandExecution/requestApproval")
                before = self.rows()
                require(
                    any(r["event"] == "PermissionRequest" for r in before),
                    "permission_event_missing",
                )
                started = time.monotonic()
                time.sleep(2)
                waited = time.monotonic() - started
                server.send({"id": approval["id"], "result": {"decision": "accept"}})
                server.event("turn/completed")
                server.request("thread/archive", {"threadId": thread})
                after = self.rows()
                require(
                    any(r["event"] == "PostToolUse" for r in after),
                    "permission_tool_finish_missing",
                )
                return {
                    "passed": True,
                    "wait_seconds": waited,
                    "observations": before + after,
                }
        finally:
            policy.unlink(missing_ok=True)

    def interrupt(self):
        require(
            self.report["model_runs_started"] < self.args.max_model_runs,
            "model_run_limit",
        )
        self.report["model_runs_started"] += 1
        with AppServer(self) as server:
            thread = server.request(
                "thread/start",
                {
                    "cwd": str(self.repo),
                    "approvalPolicy": "on-request",
                    "sandbox": "read-only",
                },
            )["thread"]["id"]
            turn = server.request(
                "turn/start",
                {
                    "threadId": thread,
                    "input": [
                        {
                            "type": "text",
                            "text": "Run exactly one shell command: sleep 20. Use yield_time_ms 10000. Then reply OK.",
                        }
                    ],
                },
            )["turn"]["id"]
            while (
                server.event("item/started")["params"].get("item", {}).get("type")
                != "commandExecution"
            ):
                pass
            before = self.rows()
            require(
                any(r["event"] == "PreToolUse" for r in before),
                "interrupt_tool_open_missing",
            )
            server.request("turn/interrupt", {"threadId": thread, "turnId": turn})
            completed = server.event("turn/completed")
            require(
                completed["params"]["turn"]["status"] == "interrupted",
                "turn_not_interrupted",
            )
            server.request("thread/archive", {"threadId": thread})
            rows = before + self.rows()
            require(
                any(
                    r["event"] == "Interrupt" and r.get("turn_id") == turn for r in rows
                ),
                "interrupt_event_missing",
            )
            return {"passed": True, "observations": rows}

    def missing_terminal(self):
        require(
            self.report["model_runs_started"] < self.args.max_model_runs,
            "model_run_limit",
        )
        self.report["model_runs_started"] += 1
        with AppServer(self) as server:
            thread = server.request(
                "thread/start",
                {
                    "cwd": str(self.repo),
                    "approvalPolicy": "on-request",
                    "sandbox": "read-only",
                },
            )["thread"]["id"]
            server.request(
                "turn/start",
                {
                    "threadId": thread,
                    "input": [
                        {
                            "type": "text",
                            "text": "Run exactly one shell command: sleep 20. Use yield_time_ms 10000. Then reply OK.",
                        }
                    ],
                },
            )
            while (
                server.event("item/started")["params"].get("item", {}).get("type")
                != "commandExecution"
            ):
                pass
            rows = self.rows()
            require(
                any(r["event"] == "PreToolUse" for r in rows),
                "missing_terminal_tool_open_missing",
            )
            os.killpg(server.process.pid, signal.SIGKILL)
            server.process.wait(timeout=5)
        rows += self.rows()
        require(
            not any(r["event"] in ("SessionEnd", "PostToolUse", "Stop") for r in rows),
            "terminal_evidence_fabricated",
        )
        return {"passed": True, "observations": rows}

    def control_plane(self):
        f = self.fixture
        f.orchestrators = [install_test.PAPERCLIP]
        f.run_collector("timer-sync", f.data / "connection")
        code, _, _ = f.converse(
            [str(install_test.BIN), "orchestrators", "approve", "--vendor", "codex"],
            [b"y\n"],
        )
        require(code == 0, "orchestrator_consent_failed")
        old = self.env.copy()
        identifiers = {
            "PAPERCLIP_AGENT_ID": "11111111-1111-4111-8111-111111111111",
            "PAPERCLIP_RUN_ID": "22222222-2222-4222-8222-222222222222",
            "PAPERCLIP_TASK_ID": "33333333-3333-4333-8333-333333333333",
        }
        self.env.update(identifiers)
        try:
            self.exec(
                "Run exactly one shell command: printf PRIVATE_C1_QUALIFY. Then reply OK."
            )
        finally:
            self.env = old
        f.observation_status = 503
        before = len(f.received)
        f.run_collector("timer-sync", f.data / "connection")
        require(len(f.received) == before + 1, "delivery_failure_not_exercised")
        first = f.received_raw[-1]
        status = json.loads(f.run_collector("doctor", "--vendor", "codex").stdout)
        require(status["pending"] > 0, "failed_delivery_lost_pending")
        f.run_collector("uninstall", "--vendor", "codex")
        require(self.hooks() == [], "stock_hooks_survived_removal")
        self.seen = len(f.received)
        f.observation_status = 201
        rows = self.rows()
        require(first in f.received_raw[before + 1 :], "replay_bytes_changed")
        require(
            rows
            and all(
                r.get("orchestrator") == "paperclip"
                and r.get("orchestrator_agent_ref") == identifiers["PAPERCLIP_AGENT_ID"]
                and r.get("orchestrator_run_ref") == identifiers["PAPERCLIP_RUN_ID"]
                and r.get("orchestrator_task_ref") == identifiers["PAPERCLIP_TASK_ID"]
                for r in rows
            ),
            "launcher_environment_not_captured",
        )
        require(
            {"PreToolUse", "PostToolUse", "SessionEnd"} <= {r["event"] for r in rows},
            "launcher_tool_events_missing",
        )
        return (
            {
                "passed": True,
                "launcher": "qualification fixture with locally approved Paperclip environment names",
                "paperclip_service_e2e": False,
                "observations": rows,
            },
            {
                "passed": True,
                "exact_replay": True,
                "pending_survived_uninstall": True,
                "stock_hooks_removed": True,
            },
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect-package", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--consent-provider-use", action="store_true")
    parser.add_argument("--package", type=Path)
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--max-model-runs", type=int, default=0)
    parser.add_argument("--cases", default="tool")
    args = parser.parse_args()
    report = {"runtime_qualified": False, "model_runs_started": 0}
    try:
        if args.inspect_package:
            report["package_components"] = package_identity(
                args.inspect_package, args.sha256
            )
        if args.consent_provider_use:
            require(
                args.package and args.auth_file and args.max_model_runs > 0,
                "live_arguments_required",
            )
            report["package_components"] = package_identity(args.package, args.sha256)
            require(
                args.cases
                in (
                    "tool",
                    "tool,resume",
                    "tool,resume,permission",
                    "tool,resume,permission,interrupt",
                    "tool,resume,permission,interrupt,missing_terminal",
                    "tool,resume,permission,interrupt,missing_terminal,control_plane",
                ),
                "unsupported_case",
            )
            report["pinned_codex_version"] = VERSION
            report["server_minutes_verified"] = False
            report["gates"] = {}
            with Harness(args, report) as harness:
                report["gates"]["tool"], thread = harness.tool()
                if "resume" in args.cases.split(","):
                    report["gates"]["resume"] = harness.resume(
                        thread, report["gates"]["tool"]["observations"]
                    )
                if "permission" in args.cases.split(","):
                    report["gates"]["permission"] = harness.permission()
                if "interrupt" in args.cases.split(","):
                    report["gates"]["interrupt"] = harness.interrupt()
                if "missing_terminal" in args.cases.split(","):
                    report["gates"]["missing_terminal"] = harness.missing_terminal()
                if "control_plane" in args.cases.split(","):
                    (
                        report["gates"]["control_plane"],
                        report["gates"]["delivery_removal"],
                    ) = harness.control_plane()

        if report.get("gates"):
            report["selected_capture_gates_passed"] = all(
                gate["passed"] for gate in report["gates"].values()
            )
        print(json.dumps(report))
        return 0
    except GateError as error:
        report["error"] = str(error)
        print(json.dumps(report))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
