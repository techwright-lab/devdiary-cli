"""Opt-in Linux stock-Claude -> Rust -> Rails qualification (not a CI test).

Default invocation is network-inert: no subprocess, auth probe or model call.
Raw host output is bounded in memory, never written to the report or disk.
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

HERE = Path(__file__).resolve().parent
CLI = HERE.parents[1]
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
    return {"one_read": True, "isolation_verified": True, "model": init.get("model")}


def inspect_spool(state):
    with contextlib.closing(sqlite3.connect(state / "collector-rust-v1.sqlite3")) as db:
        require(
            db.execute("pragma integrity_check").fetchone() == ("ok",),
            "spool_integrity",
        )
        rows = [
            json.loads(r[0])
            for r in db.execute("SELECT payload FROM outbox ORDER BY seq")
        ]
    counts = collections.Counter(r["event"] for r in rows)
    require(counts == {e: 1 for e in EVENTS}, "runtime_event_coverage")
    require(len({r["session_id"] for r in rows}) == 1, "runtime_session_correlation")
    tools = [r for r in rows if r["event"] in {"PreToolUse", "PostToolUse"}]
    require(
        len({r.get("tool_use_id") for r in tools}) == 1 and tools[0].get("tool_use_id"),
        "runtime_tool_correlation",
    )
    require(
        all(
            r.get("attribution_basis") == "unknown" and "actor_ref" not in r
            for r in rows
        ),
        "unknown_actor",
    )
    for file in state.iterdir():
        if file.is_file():
            require(
                not any(s.encode() in file.read_bytes() for s in SENTINELS),
                "spool_privacy",
            )
    require(
        not any("/home/" in json.dumps(r) or "/tmp/" in json.dumps(r) for r in rows),
        "wire_privacy",
    )
    return rows, dict(counts)


def execute(args, report):
    env = clean_env()
    rails = args.rails_checkout.resolve()
    require(
        not list(rails.glob(".env*"))
        and not (rails / "config/master.key").exists()
        and not list((rails / "config/credentials").glob("*.key")),
        "rails_checkout_contains_credentials",
    )
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
    # Managed policy cannot be disabled by --setting-sources. Refuse, don't bypass.
    home = Path(env["HOME"])
    managed = [
        Path("/etc/claude-code/managed-settings.json"),
        Path("/etc/claude-code/managed-mcp.json"),
        home / ".claude/managed-settings.json",
    ]
    require(
        not any(p.exists() for p in managed)
        and not list((home / ".claude").glob("*managed*")),
        "managed_policy_requires_review",
    )
    tracked = [home / ".claude/settings.json", home / ".claude/settings.local.json"]
    before = {p: sha(p) if p.exists() else None for p in tracked}
    with tempfile.TemporaryDirectory(prefix="claude-qualification-") as directory:
        root = Path(directory)
        report["stage"] = "rails_preflight"
        run(["bundle", "check"], cwd=rails, env={**env, "HOME": str(root)})
        report["stage"] = "setup"
        repo = root / "repo"
        repo.mkdir(mode=0o700)
        run(["git", "init", "--quiet", repo], env=env)
        (repo / "fixture.txt").write_text(SENTINELS[1])
        state = root / "state"
        state.mkdir(mode=0o700)
        collector = root / "collector"
        shutil.copyfile(args.collector.resolve(), collector)
        collector.chmod(0o700)
        report["collector_sha256"] = sha(collector)
        settings, plan = root / "settings.json", root / "plan.json"
        original = {"permissions": {"allow": ["Read(./fixture.txt)"]}}
        save(settings, original)
        save(root / "mcp.json", {"mcpServers": {}})
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["DISABLE_AUTOUPDATER"] = "1"
        base = [selected, "--setting-sources", "", "--settings", settings]
        report["claude"]["version"] = (
            run([*base, "--version"], cwd=repo, env=env).decode().strip()
        )
        require(
            report["claude"]["version"] == args.claude_version + " (Claude Code)",
            "claude_version_pin",
        )
        auth = json.loads(run([*base, "auth", "status"], cwd=repo, env=env))
        report["auth"] = {
            k: auth.get(k) for k in ("loggedIn", "authMethod", "apiProvider")
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
        # Reserve immutable endpoint while collecting; no server/provider retargeting.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            scope = {
                "endpoint": f"http://127.0.0.1:{reservation.getsockname()[1]}/ingest/v1/observations",
                "collector_ref": "qualification-" + uuid.uuid4().hex,
                "repository": str(repo),
                "repository_ref": "https://github.com/fixture/claude-qualification",
                "installation_id": str(uuid.uuid4()),
            }
            save(root / "scope.json", scope)
            run(
                [collector, "init", state, "--consent"],
                data=json.dumps(scope).encode(),
                env=env,
            )
            run([collector, "claude-plan", state, settings, plan], env=env)
            try:
                run([collector, "claude-apply", plan, "--consent"], env=env)
                report["stage"] = "stock_runtime"
                require(
                    sha(selected) == report["claude"]["sha256"], "claude_pin_changed"
                )
                report["model_runs_started"] = (
                    1  # No retry, including uncertain failures.
                )
                raw = run(
                    [
                        *base,
                        "--print",
                        "--no-session-persistence",
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
                    ],
                    cwd=repo,
                    env=env,
                    timeout=100,
                )
                report["runtime"] = validate_host(raw)
                rows, report["events"] = inspect_spool(state)
                save(root / "observations.json", rows)
            finally:
                run([collector, "claude-remove", plan, "--consent"], env=env)
                report["registration_removed"] = (
                    json.loads(settings.read_text()) == original
                )
                require(report["registration_removed"], "settings_restore")
                report["live_settings_unchanged"] = all(
                    (sha(p) if p.exists() else None) == digest
                    for p, digest in before.items()
                )
                require(report["live_settings_unchanged"], "live_settings_changed")
        report["stage"] = "rails"
        rails_interop(args, root, env, report)


def rails_interop(args, root, env, report):
    name = "devdiary_rust_collector_interop_" + uuid.uuid4().hex
    pg = ["-h", "127.0.0.1", "-p", str(args.pg_port), "-U", args.pg_user]
    # No .pgpass, Rails dotenv, production credentials, or inherited DB URL.
    renv = {
        **env,
        "HOME": str(root),
        "PGPASSFILE": str(root / "no-pgpass"),
        "RAILS_ENV": "test",
        "CI": "true",
        "SECRET_KEY_BASE": "qualification-test-only",
        "DATABASE_URL": f"postgresql://{args.pg_user}@127.0.0.1:{args.pg_port}/{name}",
        "QUALIFICATION_ROOT": str(root),
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
    try:
        require(
            run(count_command, env=renv).strip() == b"0", "database_name_already_exists"
        )
        # Reserve this unpredictable, verified-absent name before CREATE dispatch:
        # its server commit can precede an interrupt/timeout at the client.
        created = True
        run(["createdb", *pg, name], env=renv)
        run(
            ["bundle", "exec", "rails", "db:schema:load"],
            cwd=args.rails_checkout,
            env=renv,
            timeout=120,
        )
        run(
            ["bundle", "exec", "ruby", HERE / "qualify_rails.rb"],
            cwd=args.rails_checkout,
            env=renv,
            timeout=120,
        )
        report["rails"] = json.loads((root / "rails-result.json").read_text())
        require(
            report["rails"]["passed"] and report["rails"]["collector_revoked"],
            "rails_completion",
        )
        expected = json.loads((root / "expected-receipts.json").read_text())
        with contextlib.closing(
            sqlite3.connect(root / "state/collector-rust-v1.sqlite3")
        ) as db:
            receipts = [
                json.loads(r[0])
                for r in db.execute("SELECT receipt FROM outbox WHERE delivered=1")
            ]
        require(
            all(
                type(r.get("record_id")) is int and r["record_id"] > 0 for r in receipts
            ),
            "receipt_record_ids",
        )
        require(
            sorted(receipts, key=lambda r: r["observation_id"])
            == sorted(expected, key=lambda r: r["observation_id"]),
            "exact_rails_receipts",
        )
        report["exact_rails_receipts"] = True
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
    parser.add_argument("--collector", type=Path)
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
                        "--collector BINARY",
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
        and args.collector
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
