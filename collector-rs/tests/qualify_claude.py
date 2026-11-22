"""Opt-in Linux stock-Claude -> Rust -> Rails qualification (not a CI test).

Default invocation is network-inert: no subprocess, auth probe or model call.
Raw host output is bounded in memory, never written to the report or disk.
Setup and stock Claude get a temporary HOME and a fake scheduler, so a run never
enables a timer or launch agent in the person's session or writes their ~/.claude.
"""

import argparse
import collections
import contextlib
import hashlib
import json
import os
import selectors
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import install_test

HERE = Path(__file__).resolve().parent
CLI = HERE.parents[1]
ORIGINAL_SETTINGS = {"permissions": {"allow": ["Read(./fixture.txt)"]}}
# The Rails fixture tracks this repository, so the session's rows are in scope.
REPOSITORY = "https://github.com/fixture/claude-qualification"
EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
}
SENTINELS = (
    "PROMPT_PRIVATE_qualify",
    "FILE_PRIVATE_qualify",
    "RESPONSE_PRIVATE_qualify",
    "fixture.txt",
    ".claude/projects",
)


class GateError(RuntimeError):
    """Only fixed, harness-owned codes are safe for the report."""


def require(condition, gate):
    if not condition:
        raise GateError(gate)


def run(argv, *, cwd=None, env=None, data=b"", timeout=30, expected=0):
    """Bound bytes/time, including descendants retaining pipes; reap owned group."""
    proc = subprocess.Popen(
        list(map(str, argv)),
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = [bytearray(), bytearray()]
    try:
        proc.stdin.write(data)
        proc.stdin.close()
        with selectors.DefaultSelector() as selector:
            for index, pipe in enumerate((proc.stdout, proc.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, index)
            deadline = time.monotonic() + timeout
            while selector.get_map():
                require(time.monotonic() < deadline, "subprocess_timeout")
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(chunk)
                        require(
                            sum(map(len, output)) <= 2_000_000,
                            "subprocess_output_limit",
                        )
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        require(proc.returncode == expected, "subprocess_exit")
        return bytes(output[0])
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            pipe.close()


def clean_env():
    # Auth remains vendor-owned in normal HOME; no ambient provider keys/proxies.
    return {
        k: os.environ[k]
        for k in (
            "HOME",
            "USER",
            "LOGNAME",
            "PATH",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
        )
        if k in os.environ
    }


def sha(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def source(path, env):
    return {
        "sha": run(["git", "rev-parse", "HEAD"], cwd=path, env=env).decode().strip(),
        "dirty": bool(run(["git", "status", "--porcelain"], cwd=path, env=env)),
    }


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")
    path.chmod(0o600)


def validate_host(raw):
    messages = [json.loads(line) for line in raw.splitlines() if line.strip()]
    init = [
        m for m in messages if m.get("type") == "system" and m.get("subtype") == "init"
    ]
    require(len(init) == 1, "host_init")
    init = init[0]
    require(
        init.get("tools") == ["Read"]
        and init.get("mcp_servers") == []
        and init.get("plugins") == [],
        "host_isolation",
    )
    require(init.get("apiKeySource") == "none", "host_subscription")
    tools = [
        b
        for m in messages
        for b in m.get("message", {}).get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    require(len(tools) == 1 and tools[0].get("name") == "Read", "host_tool_count")
    results = [m for m in messages if m.get("type") == "result"]
    require(
        len(results) == 1
        and results[0].get("subtype") == "success"
        and results[0].get("is_error") is False
        and not results[0].get("permission_denials"),
        "host_result",
    )
    require(SENTINELS[2].encode() in raw, "host_response")
    return {
        "one_read": True,
        "init_tool_surface_verified": True,
        "model": init.get("model"),
    }


def check_managed_policy(home, system=Path("/etc/claude-code")):
    # Official managed-settings and server-managed-settings docs: CLI setting
    # sources cannot disable endpoint drop-ins or cached/current remote policy.
    # Reject even empty/unreadable drop-in directories (no policy contents read).
    managed = [
        system / "managed-settings.json",
        system / "managed-settings.d",
        system / "managed-mcp.json",
    ]
    require(
        not any(p.exists() or p.is_symlink() for p in managed)
        and not list((home / ".claude").glob("*managed*")),
        "managed_policy_requires_review",
    )
    # No supported pre-launch proof of remote absence exists in this harness.
    # auth status/init tool lists don't prove it, and doctor starts the vendor.
    # Fail closed, without a bypass flag or reading/copying the auth/cache files.
    raise GateError("remote_managed_policy_unverified")


class Machine:
    """A temporary HOME whose scheduler is a fake: setup writes the hooks, the
    connection and the job definition here and never reaches a real timer."""

    def __init__(self, root, base, repository=REPOSITORY):
        self.root = root
        self.home = root / "home"
        self.config = root / "config"
        (self.home / ".claude").mkdir(mode=0o700, parents=True)
        self.config.mkdir(mode=0o700)
        self.scheduler = install_test.FakeScheduler(root, self.home, self.config)
        self.settings = self.home / ".claude/settings.json"
        save(self.settings, ORIGINAL_SETTINGS)
        self.connection = self.home / ".local/share/devdiary/cli/claude/connection"
        self.env = {
            **{k: v for k, v in base.items() if not k.startswith("XDG_")},
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.config),
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(root / "no-user-bus"),
            # Step 4 of setup returns at once; delivery is the Rails stage's job.
            "DEVDIARY_DEBUG_FIRST_SESSION_WAIT": "0",
            **self.scheduler.env(),
        }
        self.repo = root / "repo"
        self.repo.mkdir(mode=0o700)
        (self.repo / "fixture.txt").write_text(SENTINELS[1])
        run(["git", "init", "--quiet", self.repo], env=self.env)
        run(
            ["git", "-C", self.repo, "remote", "add", "origin", repository],
            env=self.env,
        )


def debug_collector(path=install_test.BIN):
    """The fake scheduler is honoured only by a debug build. A release build would
    enable a real timer or launch agent, so nothing else may run setup here."""
    require(path == install_test.BIN and path.is_file(), "debug_build_required")
    return path


def claude_command(machine, claude, auth, args):
    """Stock Claude in a sandbox where only the run's folder is writable. Its
    vendor login is the one file bound in, read-only, from the real HOME."""
    root = machine.root
    command = ["bwrap", "--tmpfs", "/"]
    for path in ("/usr", "/bin", "/lib", "/etc"):
        command += ["--ro-bind", path, path]
    command += ["--ro-bind-try", "/lib64", "/lib64"]
    # resolv.conf may point outside /etc. Mount the target, not unrelated /run data.
    resolver = str(Path("/etc/resolv.conf").resolve())
    command += ["--ro-bind-try", resolver, resolver]
    command += ["--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    command += ["--unshare-pid", "--die-with-parent"]
    command += ["--bind", str(root), str(root)]
    command += ["--ro-bind", str(claude), str(root / "stock/claude")]
    command += ["--ro-bind", str(auth), str(machine.home / ".claude/.credentials.json")]
    command += ["--chdir", str(machine.repo), "--", str(root / "stock/claude")]
    return command + [str(a) for a in args]


def claude_env(machine):
    return {
        "HOME": str(machine.home),
        "PATH": str(machine.root / "stock") + ":/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def inspect_outbox(connection, root):
    """The rows the session's hooks wrote, checked before anything is sent."""
    outbox = connection / "outbox"
    with contextlib.closing(
        sqlite3.connect(outbox / "collector-rust-v1.sqlite3")
    ) as db:
        require(
            db.execute("pragma integrity_check").fetchone() == ("ok",),
            "outbox_integrity",
        )
        rows = [
            json.loads(r[0])
            for r in db.execute("SELECT payload FROM outbox ORDER BY seq")
        ]
    counts = collections.Counter(r["event"] for r in rows)
    require(counts == dict.fromkeys(EVENTS, 1), "runtime_event_coverage")
    require(len({r["session_id"] for r in rows}) == 1, "runtime_session_correlation")
    tools = [r for r in rows if r["event"] in {"PreToolUse", "PostToolUse"}]
    require(
        len({r.get("tool_use_id") for r in tools}) == 1 and tools[0].get("tool_use_id"),
        "runtime_tool_correlation",
    )
    require(
        all(
            r.get("runtime") == "claude-code"
            and r.get("attribution_basis") == "unknown"
            and "actor_ref" not in r
            for r in rows
        ),
        "unknown_actor",
    )
    for file in outbox.iterdir():
        if file.is_file():
            require(
                not any(s.encode() in file.read_bytes() for s in SENTINELS),
                "outbox_privacy",
            )
    wire = json.dumps(rows)
    require(not any(p in wire for p in (str(root), "/home/", "/tmp/")), "wire_privacy")
    return rows, dict(counts)


def private_file(path):
    require(
        path is not None
        and not path.is_symlink()
        and path.is_file()
        and path.stat().st_mode & 0o077 == 0,
        "auth_file_private_regular_file",
    )
    return path.resolve()


def execute(args, report):
    env = clean_env()
    report["stage"] = "managed_policy_preflight"
    check_managed_policy(Path(env["HOME"]))
    rails = args.rails_checkout.resolve()
    require(
        not list(rails.glob(".env*"))
        and not (rails / "config/master.key").exists()
        and not list((rails / "config/credentials").glob("*.key")),
        "rails_checkout_contains_credentials",
    )
    collector = debug_collector()
    auth = private_file(args.auth_file)
    report["sources"] = {"cli": source(CLI, env), "rails": source(rails, env)}
    ruby = Path(
        run(["mise", "which", "ruby"], cwd=rails, env=env).decode().strip()
    ).resolve(strict=True)
    env["PATH"] = str(ruby.parent) + os.pathsep + env["PATH"]
    report["ruby_version"] = run([ruby, "--version"], env=env).decode().strip()
    selected = Path(
        run(["mise", "which", "claude"], cwd=CLI, env=env).decode().strip()
    ).resolve(strict=True)
    with selected.open("rb") as executable:
        require(executable.read(4) == b"\x7fELF", "native_claude_required")
    report["claude"] = {"path": str(selected), "sha256": sha(selected)}
    # Nothing in the run may write the person's own settings.
    home = Path(env["HOME"])
    tracked = [
        home / ".claude/settings.json",
        home / ".claude/settings.local.json",
        home / ".claude.json",
    ]
    before = {p: sha(p) if p.exists() else None for p in tracked}
    with tempfile.TemporaryDirectory(prefix="claude-qualification-") as directory:
        root = Path(directory).resolve()
        report["stage"] = "rails_preflight"
        run(["bundle", "check"], cwd=rails, env={**env, "HOME": str(root)})
        report["stage"] = "setup"
        machine = Machine(root, env)
        exe = root / "collector"
        shutil.copyfile(collector, exe)
        exe.chmod(0o700)
        report["collector_sha256"] = sha(exe)

        def claude(*arguments, timeout=30):
            return run(
                claude_command(machine, selected, auth, arguments),
                env=claude_env(machine),
                timeout=timeout,
            )

        report["claude"]["version"] = claude("--version").decode().strip()
        require(
            report["claude"]["version"] == args.claude_version + " (Claude Code)",
            "claude_version_pin",
        )
        auth_status = json.loads(claude("auth", "status"))
        report["auth"] = {
            k: auth_status.get(k) for k in ("loggedIn", "authMethod", "apiProvider")
        }
        require(
            report["auth"]
            == {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
            },
            "subscription_required",
        )
        save(root / "mcp.json", {"mcpServers": {}})

        def session():
            # Setup paired against Rails and wrote the hooks into the temporary
            # HOME's user settings, which this stock session reads.
            report["stage"] = "stock_runtime"
            require(sha(selected) == report["claude"]["sha256"], "claude_pin_changed")
            report["model_runs_started"] = 1  # No retry, including uncertain failures.
            raw = claude(
                "--print",
                "--no-session-persistence",
                "--setting-sources",
                "user",
                "--strict-mcp-config",
                "--mcp-config",
                root / "mcp.json",
                "--disable-slash-commands",
                "--no-chrome",
                "--tools",
                "Read",
                "--allowedTools",
                "Read(./fixture.txt)",
                "--permission-mode",
                "dontAsk",
                "--permission-prompts",
                "none",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-hook-events",
                "--model",
                "sonnet",
                "--effort",
                "low",
                "--max-turns",
                "2",
                "--system-prompt",
                "Use only Read once on fixture.txt; access no other files or tools.",
                "--",
                f"{SENTINELS[0]} Read fixture.txt once then respond exactly {SENTINELS[2]}.",
                timeout=100,
            )
            report["runtime"] = validate_host(raw)
            rows, report["events"] = inspect_outbox(machine.connection, root)
            save(root / "observations.json", rows)
            report["stage"] = "rails_deliver"

        try:
            # machine.env holds the Ruby PATH and none of the person's XDG folders.
            rails_interop(args, root, machine.env, report, session)
            doctor = json.loads(run([exe, "doctor", "--json"], env=machine.env))
            report["doctor"] = {k: doctor.get(k) for k in ("pending", "delivered")}
            expected = json.loads((root / "expected-receipts.json").read_text())
            require(
                doctor.get("pending") == 0
                and doctor.get("delivered") == len(expected)
                and doctor.get("last_receipt") in expected,
                "exact_rails_receipts",
            )
            report["exact_rails_receipts"] = True
        finally:
            # Setup may have written hooks even when a later stage failed.
            run([exe, "uninstall"], env=machine.env)
            report["registration_removed"] = (
                json.loads(machine.settings.read_text()) == ORIGINAL_SETTINGS
            )
            require(report["registration_removed"], "settings_restore")
    report["live_settings_unchanged"] = all(
        (sha(p) if p.exists() else None) == digest for p, digest in before.items()
    )
    require(report["live_settings_unchanged"], "live_settings_changed")


def free_port():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def rails_interop(args, root, env, report, session):
    """Pair setup against Rails, run `session`, then deliver its rows to Rails.

    Both Rails boots listen on one port, the origin setup saved, and share one
    owned database that is dropped whatever happens.
    """
    name = "devdiary_qualification_" + uuid.uuid4().hex
    pg = ["-h", "127.0.0.1", "-p", str(args.pg_port), "-U", args.pg_user]
    # No .pgpass, Rails dotenv, production credentials, or inherited DB URL.
    renv = {
        **env,
        "PGPASSFILE": str(root / "no-pgpass"),
        "RAILS_ENV": "test",
        "CI": "true",
        "SECRET_KEY_BASE": "qualification-test-only",
        "DATABASE_URL": f"postgresql://{args.pg_user}@127.0.0.1:{args.pg_port}/{name}",
        "QUALIFICATION_ROOT": str(root),
        "QUALIFICATION_PORT": str(free_port()),
    }
    report["database_name"] = name
    count_command = [
        "psql",
        *pg,
        "-d",
        "postgres",
        "-Atc",
        f"SELECT count(*) FROM pg_database WHERE datname='{name}'",
    ]
    created = False

    def rails(stage):
        run(
            ["bundle", "exec", "ruby", HERE / "qualify_rails.rb", stage],
            cwd=args.rails_checkout,
            env=renv,
            timeout=120,
        )

    try:
        require(
            run(count_command, env=renv).strip() == b"0", "database_name_already_exists"
        )
        # Reserve this unpredictable, verified-absent name before CREATE dispatch:
        # its server commit can precede an interrupt/timeout at the client.
        created = True
        run(["createdb", *pg, name], env=renv)
        rails("pair")
        session()
        rails("deliver")
        report["rails"] = json.loads((root / "rails-result.json").read_text())
        require(
            report["rails"]["passed"] and report["rails"]["collector_revoked"],
            "rails_completion",
        )
    finally:
        if (root / "rails-result.json").exists():
            # A killed Ruby writer can leave a partial report. Never let parsing
            # prevent dropping the owned DB (which invalidates any surviving key).
            try:
                report["rails"] = json.loads((root / "rails-result.json").read_text())
            except (ValueError, OSError):
                report["rails_report_unreadable"] = True
        if created:
            # Force only our randomly named owned database, never a caller's DB.
            run(["dropdb", *pg, "--if-exists", "--force", name], env=renv)
            absent = run(count_command, env=renv)
            report["database_dropped"] = absent.strip() == b"0"
            require(report["database_dropped"], "database_cleanup")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consent-provider-use", action="store_true")
    parser.add_argument("--max-model-runs", type=int, default=0)
    parser.add_argument("--claude-version")
    parser.add_argument("--auth-file", type=Path)
    parser.add_argument("--rails-checkout", type=Path)
    parser.add_argument("--pg-user", default=os.environ.get("USER", ""))
    parser.add_argument("--pg-port", type=int, default=5432)
    args = parser.parse_args(argv)
    if not args.consent_provider_use:
        print(
            json.dumps(
                {
                    "mode": "preflight",
                    "network": False,
                    "model_runs_started": 0,
                    "required": [
                        "--consent-provider-use",
                        "--max-model-runs 1",
                        "--claude-version VERSION",
                        "--auth-file ~/.claude/.credentials.json",
                        "--rails-checkout CHECKOUT",
                    ],
                    "scope": "one subscription print session; no interactive/resume/child qualification",
                }
            )
        )
        return 0
    require(
        args.max_model_runs == 1
        and args.claude_version
        and args.auth_file
        and args.rails_checkout,
        "explicit_bounded_arguments_required",
    )
    require(
        not os.environ.get("CI") and not os.environ.get("GITHUB_ACTIONS"),
        "live_run_forbidden_in_ci",
    )
    require(args.pg_user.isalnum() and 1 <= args.pg_port <= 65535, "local_pg_arguments")
    report = {
        "evidence": "actual-stock-runtime",
        "model_runs_started": 0,
        "passed": False,
    }
    old = signal.signal(
        signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    mask = os.umask(0o077)
    try:
        execute(args, report)
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 — sanitize all failures
        # Exception text may contain raw vendor output or filesystem paths.
        report["failure_type"] = type(exc).__name__
        if isinstance(exc, GateError):
            report["failure_gate"] = str(exc)
    finally:
        signal.signal(signal.SIGTERM, old)
        os.umask(mask)
        print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
