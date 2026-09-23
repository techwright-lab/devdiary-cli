from __future__ import annotations

import argparse
import json
import os
import sys
from importlib import resources
from pathlib import Path

from devdiary import __version__, doctor, runner, spool
from devdiary.config import (
    ConfigError,
    add_actor,
    add_adapter,
    default_config_path,
    default_registry,
    endpoint,
    find_actor,
    key_environment,
    load_registry,
    validate_registry,
    write_registry,
)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="devdiary")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", type=Path, default=default_config_path())
    commands = root.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init", help="create an editable Cast Registry")
    initialize.add_argument("--namespace", required=True)
    initialize.add_argument("--principal-ref", required=True)
    initialize.add_argument("--endpoint")
    initialize.add_argument("--force", action="store_true")

    actor = commands.add_parser("actor", help="manage actors")
    actor_commands = actor.add_subparsers(dest="actor_command", required=True)
    actor_add = actor_commands.add_parser(
        "add", help="add a stable agent or automation actor"
    )
    actor_add.add_argument("--ref", required=True)
    actor_add.add_argument("--kind", choices=("agent", "automation"), default="agent")
    actor_add.add_argument("--display-name", required=True)
    actor_add.add_argument("--humanized-name")
    actor_add.add_argument("--principal-ref")
    actor_add.add_argument("--attester-ref", required=True)
    actor_add.add_argument("--git-name")
    actor_add.add_argument("--git-email")
    actor_add.add_argument("--lane")
    actor_add.add_argument("--alias", action="append", default=[])
    actor_add.add_argument("--identity", action="append", default=[])

    adapter = commands.add_parser("adapter", help="manage declarative runtime adapters")
    adapter_commands = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_add = adapter_commands.add_parser("add", help="add environment mappings")
    adapter_add.add_argument("--name", required=True)
    adapter_add.add_argument("--runtime", required=True)
    adapter_add.add_argument(
        "--execution-role", choices=("orchestrator", "executor"), default="executor"
    )
    adapter_add.add_argument("--session-ref-env")
    adapter_add.add_argument("--provider-env")
    adapter_add.add_argument("--model-env")

    diagnosis = commands.add_parser(
        "doctor", help="validate local conformance without creating work"
    )
    diagnosis.add_argument("--actor")
    diagnosis.add_argument("--json", action="store_true")

    launch = commands.add_parser(
        "run", help="run a command inside an attribution context"
    )
    launch.add_argument("--actor", required=True)
    launch.add_argument("--adapter")
    launch.add_argument("--task-ref")
    launch.add_argument("--cwd", type=Path, default=Path.cwd())
    launch.add_argument("--repository", action="append", default=[])
    launch.add_argument("--commit", action="append", default=[])
    launch.add_argument("--pull-request", action="append", default=[])
    launch.add_argument("--issue", action="append", default=[])
    launch.add_argument("--artifact", action="append", default=[])
    launch.add_argument("runtime_command", nargs=argparse.REMAINDER)

    emit = commands.add_parser("emit", help="retry queued terminal declarations")
    emit_commands = emit.add_subparsers(dest="emit_command", required=True)
    emit_commands.add_parser(
        "pending", help="deliver all queued declarations idempotently"
    )

    capture = commands.add_parser(
        "capture", help="durable portable attribution lifecycle"
    )
    operations = capture.add_subparsers(dest="capture_operation", required=True)
    for operation in ("begin", "finish", "status", "retry"):
        command = operations.add_parser(operation)
        command.add_argument("--state-dir", type=Path, required=True)
        command.add_argument("--json", action="store_true")

    observer = commands.add_parser(
        "observer", help="local-only stock Claude/Codex metadata observer (POSIX pilot)"
    )
    observer_ops = observer.add_subparsers(dest="observer_operation", required=True)
    preview = observer_ops.add_parser(
        "plan", help="preview narrow hook registration; writes nothing"
    )
    preview.add_argument("--vendor", choices=("claude", "codex"), default="claude")
    discovery = observer_ops.add_parser(
        "discover",
        help="probe installed versions/help in isolation; never enable hooks",
    )
    discovery.add_argument("--vendor", choices=("claude", "codex"))
    preview.add_argument("--settings", type=Path, required=True)
    preview.add_argument("--state-dir", type=Path, required=True)
    preview.add_argument("--repository", type=Path, required=True)
    preview.add_argument(
        "--executable",
        type=Path,
        required=True,
        help="trusted absolute Python interpreter",
    )
    preview.add_argument("--binding-session")
    preview.add_argument("--binding-agent-id")
    preview.add_argument(
        "--actor-ref",
        help="exact known registry ref; never inferred from runtime/model",
    )
    install = observer_ops.add_parser("apply")
    install.add_argument("--plan", type=Path, required=True)
    install.add_argument("--consent", action="store_true")
    install.add_argument(
        "--vendor",
        choices=("claude", "codex"),
        help="optional assertion against saved plan",
    )
    for operation in ("remove", "health", "observations"):
        op = observer_ops.add_parser(operation)
        op.add_argument("--state-dir", type=Path, required=True)
        if operation == "remove":
            op.add_argument("--consent", action="store_true")
            op.add_argument(
                "--vendor",
                choices=("claude", "codex"),
                help="optional assertion against installation",
            )
        if operation == "observations":
            op.add_argument("--limit", type=int, default=100)

    connect = observer_ops.add_parser(
        "connect",
        help="consent to explicit metadata upload; never changes vendor hooks",
    )
    connect.add_argument("--state-dir", type=Path, required=True)
    connect.add_argument("--endpoint", required=True)
    connect.add_argument("--collector-ref", required=True)
    connect.add_argument("--repository-ref", required=True)
    connect.add_argument("--key-file", type=Path, required=True)
    connect.add_argument("--consent", action="store_true")
    sync = observer_ops.add_parser(
        "sync", help="upload a bounded batch outside vendor hooks"
    )
    sync.add_argument("--state-dir", type=Path, required=True)
    sync.add_argument("--limit", type=int, default=100)

    schema = commands.add_parser("schema", help="print a bundled public JSON Schema")
    schema.add_argument("name", choices=("registry", "context", "envelope"))
    return root


def main(arguments: list[str] | None = None) -> int:
    args = parser().parse_args(arguments)
    if args.command == "capture":
        return _capture(args)
    if args.command == "observer":
        return _observer(args)
    try:
        if args.command == "init":
            return _initialize(args)
        if args.command == "actor":
            return _actor(args)
        if args.command == "adapter":
            return _adapter(args)
        if args.command == "schema":
            return _schema(args.name)

        registry = load_registry(args.config)
        if args.command == "doctor":
            return doctor.report(
                doctor.run_checks(registry, args.actor, registry_path=args.config),
                json_output=args.json,
            )
        if args.command == "run":
            return _run(args, registry)
        if args.command == "emit":
            return _emit(args, registry)
    except (ConfigError, spool.SpoolError) as error:
        print(f"devdiary: {error}", file=sys.stderr)
        return 2
    return 2


def _observer(args: argparse.Namespace) -> int:
    import sqlite3

    from devdiary import observer
    from devdiary.secure_paths import UnsafePathError

    try:
        operation = args.observer_operation
        if not observer.supported_platform():
            print(
                json.dumps(
                    {
                        "error_code": "observer_platform_unsupported",
                        "platform_support": "POSIX_only",
                    }
                )
            )
            return 2
        if operation == "plan":
            binding = None
            if args.actor_ref or args.binding_session or args.binding_agent_id:
                if not args.actor_ref or not args.binding_session:
                    raise ValueError("actor_ref_and_exact_session_required")
                registry = load_registry(args.config)
                if not any(
                    actor["actor_ref"] == args.actor_ref for actor in registry["actors"]
                ):
                    raise ValueError("exact_known_actor_required")
                binding = {
                    "actor_ref": args.actor_ref,
                    "session_id": args.binding_session,
                    "agent_id": args.binding_agent_id,
                }
            result = observer.plan(
                args.settings,
                args.state_dir,
                args.repository,
                args.executable,
                binding,
                vendor=args.vendor,
            )
        elif operation == "apply":
            proposal_raw = observer.read(args.plan)
            if proposal_raw is None:
                raise ValueError("plan_missing")
            proposal = json.loads(proposal_raw)
            if proposal.get("binding"):
                registry = load_registry(args.config)
                if not any(
                    actor["actor_ref"] == proposal["binding"]["actor_ref"]
                    for actor in registry["actors"]
                ):
                    raise ValueError("exact_known_actor_required")
            result = observer.apply(proposal, consent=args.consent, vendor=args.vendor)
        elif operation == "remove":
            result = observer.remove(
                args.state_dir, consent=args.consent, vendor=args.vendor
            )
        elif operation == "discover":
            result = observer.discover(args.vendor)
        elif operation == "connect":
            from devdiary import observer_upload

            result = observer_upload.configure(
                args.state_dir,
                endpoint=args.endpoint,
                collector_ref=args.collector_ref,
                repository_ref=args.repository_ref,
                key_file=args.key_file,
                consent=args.consent,
            )
        elif operation == "sync":
            from devdiary import observer_upload

            result = observer_upload.sync(args.state_dir, args.limit)
            print(json.dumps(result, sort_keys=True))
            return 1 if result["last_failure"] else 0
        elif operation == "health":
            result = observer.health(args.state_dir)
        else:
            result = observer.observations(args.state_dir, args.limit)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        sqlite3.Error,
        ConfigError,
        UnsafePathError,
    ):
        # Do not echo settings values or paths from parser/database exceptions.
        print(
            json.dumps(
                {
                    "error_code": "observer_operation_failed",
                    "action": "inspect_health_and_replan_with_host_quiescent",
                }
            )
        )
        return 2


def _capture(args: argparse.Namespace) -> int:
    import sqlite3

    from devdiary import capture
    from devdiary import capture_validation as validation
    from devdiary.capture_store import Store
    from devdiary.secure_paths import UnsafePathError

    store = None
    try:
        data = validation.read_input(sys.stdin.buffer)
        store = Store(args.state_dir)
        if args.capture_operation == "begin":
            result = capture.begin(store, load_registry(args.config), data)
        elif args.capture_operation == "finish":
            record = capture.freeze(store, data)
            result = capture.deliver(store, record["capture_id"])
        elif args.capture_operation == "retry":
            validation.obj(data, {"capture_id"}, {"capture_id"})
            result = capture.deliver(
                store, validation.text(data["capture_id"]), replay=True
            )
        else:
            result = capture.status(store, data)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        RecursionError,
        sqlite3.Error,
        UnsafePathError,
    ) as error:
        code = (
            str(error)
            if isinstance(error, validation.CaptureError)
            else "capture_failed"
        )
        print(json.dumps({"error_code": code}))
        print(f"devdiary: {code}", file=sys.stderr)
        return 2
    finally:
        if store is not None:
            store.close()


def _initialize(args: argparse.Namespace) -> int:
    registry = default_registry(args.namespace, args.principal_ref, args.endpoint)
    validate_registry(registry)
    write_registry(args.config, registry, force=args.force)
    print(args.config)
    return 0


def _actor(args: argparse.Namespace) -> int:
    identities = {
        name: value
        for name, value in (("git_name", args.git_name), ("git_email", args.git_email))
        if value
    }
    custom_identities = _assignments(args.identity, "identity")
    duplicate_identities = sorted(set(identities) & set(custom_identities))
    if duplicate_identities:
        raise ConfigError(f"duplicate identity: {', '.join(duplicate_identities)}")
    identities.update(custom_identities)
    actor = {
        "actor_ref": args.ref,
        "kind": args.kind,
        "display_name": args.display_name,
        "humanized_name": args.humanized_name,
        "attester_ref": args.attester_ref,
        "identities": identities,
        "lane": args.lane,
        "aliases": args.alias,
    }
    if args.principal_ref:
        actor["principal_ref"] = args.principal_ref
    add_actor(args.config, actor)
    print(args.ref)
    return 0


def _adapter(args: argparse.Namespace) -> int:
    mapping = {
        target: f"env:{value}"
        for target, value in (
            ("session_ref", args.session_ref_env),
            ("provider", args.provider_env),
            ("model", args.model_env),
        )
        if value
    }
    adapter = {
        "execution_role": args.execution_role,
        "runtime": args.runtime,
        "map": mapping,
    }
    add_adapter(args.config, args.name, adapter)
    print(args.name)
    return 0


def _run(args: argparse.Namespace, registry: dict) -> int:
    command = list(args.runtime_command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise ConfigError("run requires a command after --")
    if args.adapter and args.adapter not in registry.get("adapters", {}):
        raise ConfigError(f"adapter not found: {args.adapter}")
    actor = find_actor(registry, args.actor)
    result = runner.run_command(
        registry,
        actor,
        command,
        args.cwd,
        adapter_name=args.adapter,
        task_ref=args.task_ref,
        spool_directory=args.config.parent / "attribution-events",
        state_directory=args.config.parent / "attribution-captures",
        explicit_work={
            "repositories": args.repository,
            "commits": args.commit,
            "pull_requests": args.pull_request,
            "issues": args.issue,
            "artifacts": args.artifact,
        },
    )
    return result.exit_code


def _emit(args: argparse.Namespace, registry: dict) -> int:
    if args.emit_command != "pending":
        return 2
    url = endpoint(registry)
    key_env = key_environment(registry)
    key = os.environ.get(key_env)
    if not url:
        raise ConfigError("ingest.url is not configured")
    if not key:
        raise ConfigError(f"{key_env} is not present")
    delivered, errors = spool.flush(args.config.parent / "attribution-events", url, key)
    print(f"delivered {delivered} queued declaration(s)")
    for error in errors:
        print(f"devdiary: warning: {error}", file=sys.stderr)
    return 1 if errors else 0


def _schema(name: str) -> int:
    package = resources.files("devdiary.schemas")
    schema = json.loads(
        package.joinpath(f"{name}.schema.json").read_text(encoding="utf-8")
    )
    print(json.dumps(schema, indent=2, sort_keys=True))
    return 0


def _assignments(values: list[str], label: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for value in values:
        name, separator, mapped = value.partition("=")
        if not separator or not name or not mapped:
            raise ConfigError(f"{label} must use NAME=VALUE")
        if name in assignments:
            raise ConfigError(f"duplicate {label}: {name}")
        assignments[name] = mapped
    return assignments
