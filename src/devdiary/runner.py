from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from devdiary import contract, git_refs, spool
from devdiary.config import (
    ConfigError,
    endpoint,
    enforcement,
    key_environment,
)
from devdiary.process_tree import ProcessTree
from devdiary.secure_environment import scrub_inherited_variable
from devdiary.transport import TransportError, post_envelope

CONTEXT_ENV = "DEVDIARY_ATTRIBUTION_CONTEXT"
ACTOR_ENV = "DEVDIARY_ACTOR_REF"
RUN_ENV = "DEVDIARY_RUN_REF"
TASK_ENV = "DEVDIARY_TASK_REF"
ENFORCEMENT_FAILURE = 70
MAX_CONTEXT_BYTES = 1_048_576
WINDOWS_CREATE_SUSPENDED = 0x00000004


class RunInterrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    envelope: dict[str, Any]
    emitted: bool
    pending_path: Path | None


def run_command(
    registry: dict[str, Any],
    actor: dict[str, Any],
    command: list[str],
    cwd: Path,
    adapter_name: str | None = None,
    task_ref: str | None = None,
    environment: dict[str, str] | None = None,
    spool_directory: Path | None = None,
    explicit_work: dict[str, list[str]] | None = None,
    state_directory: Path | None = None,
) -> RunResult:
    explicit_work = _validate_work_references(explicit_work)
    parent_environment = dict(os.environ if environment is None else environment)
    key_env = key_environment(registry)
    ingest_key = parent_environment.pop(key_env, None)
    try:
        scrub_inherited_variable(key_env)
    except (OSError, TypeError, ValueError) as error:
        raise ConfigError(
            "could not scrub the consumed ingest key from the native process environment"
        ) from error
    parent_ref = _parent_run_ref(parent_environment)
    safe_for_context = _without_secret_like_values(parent_environment, key_env)
    run_context = contract.context(
        registry,
        actor,
        command,
        cwd,
        adapter_name,
        safe_for_context,
        task_ref,
        parent_ref,
    )

    if state_directory is not None:
        return _run_durable(
            registry,
            actor,
            command,
            cwd,
            run_context,
            parent_environment,
            ingest_key,
            state_directory,
            spool_directory,
            explicit_work,
        )

    before = git_refs.capture(cwd)
    with tempfile.TemporaryDirectory(prefix="devdiary-") as directory:
        context_path = Path(directory) / "context.json"
        _write_context(context_path, run_context)
        child_environment = _child_environment(
            parent_environment, key_env, actor, run_context, context_path
        )
        exit_code, cancelled = _spawn(command, cwd, child_environment)
        after = git_refs.capture(cwd)

        event_type = (
            "run.cancelled"
            if cancelled
            else ("run.completed" if exit_code == 0 else "run.failed")
        )
        envelope = contract.terminal_envelope(
            run_context,
            actor,
            event_type,
            contract.timestamp(),
            _merge_work_references(
                git_refs.work_references(
                    before,
                    after,
                    author_email=(actor.get("identities") or {}).get("git_email"),
                ),
                explicit_work,
            ),
        )
        emitted, emission_failed = _emit(registry, ingest_key, envelope)

    pending_path = None
    if emission_failed and ingest_key:
        try:
            pending_path = spool.enqueue(
                spool_directory or (cwd / ".devdiary/attribution-events"),
                envelope,
                ingest_key,
            )
            print(
                f"devdiary: queued declaration at {pending_path}",
                file=sys.stderr,
            )
        except (OSError, spool.SpoolError, KeyError, TypeError) as error:
            print(
                f"devdiary: warning: could not queue declaration: {error}",
                file=sys.stderr,
            )
    if emission_failed and enforcement(registry) == "enforce" and exit_code == 0:
        exit_code = ENFORCEMENT_FAILURE
    return RunResult(
        exit_code=exit_code,
        envelope=envelope,
        emitted=emitted,
        pending_path=pending_path,
    )


def _run_durable(
    registry: dict,
    actor: dict,
    command: list[str],
    cwd: Path,
    context: dict,
    environment: dict,
    ingest_key: str | None,
    state_directory: Path,
    spool_directory: Path | None,
    explicit_work: dict,
) -> RunResult:
    import sqlite3

    from devdiary import capture
    from devdiary.capture_store import Store
    from devdiary.secure_paths import UnsafePathError

    store = None
    try:
        store = Store(state_directory)
        opened = capture.begin(
            store,
            registry,
            {
                "actor_ref": actor["actor_ref"],
                "run_ref": context["run"]["ref"],
                "cwd": str(cwd.resolve()),
                "execution_chain": context["execution_chain"],
                "task_refs": [context["run"]["task_ref"]]
                if context["run"].get("task_ref")
                else [],
                "parent_run_ref": context["run"].get("parent_ref"),
                "source": {"system": "devdiary-run"},
            },
        )
        child_environment = _child_environment(
            environment,
            key_environment(registry),
            actor,
            context,
            Path(opened["environment"][CONTEXT_ENV]),
        )
        child_environment.update(opened["environment"])
        exit_code, cancelled = _spawn(command, cwd, child_environment)
        record = capture.freeze(
            store,
            {
                "capture_id": opened["capture_id"],
                "outcome": "cancelled"
                if cancelled
                else "completed"
                if exit_code == 0
                else "failed",
                "work": explicit_work,
            },
        )
        emitted = False
        failed = False
        pending_path = None
        if enforcement(registry) != "off":
            # Empty means explicitly absent; never recover a secret from ambient
            # os.environ after run_command consumed a supplied environment.
            result = capture.deliver(
                store, opened["capture_id"], key_override=ingest_key or ""
            )
            emitted = result["state"] == "delivered"
            failed = not emitted
            if failed:
                print(
                    "devdiary: warning: declaration pending durable retry",
                    file=sys.stderr,
                )
            if failed and ingest_key:
                try:
                    pending_path = spool.enqueue(
                        spool_directory
                        or state_directory.parent / "attribution-events",
                        record["envelope"],
                        ingest_key,
                    )
                except (OSError, spool.SpoolError):
                    print(
                        "devdiary: warning: legacy spool unavailable; durable capture retained",
                        file=sys.stderr,
                    )
        if failed and enforcement(registry) == "enforce" and exit_code == 0:
            exit_code = ENFORCEMENT_FAILURE
        return RunResult(exit_code, record["envelope"], emitted, pending_path)
    except (OSError, ValueError, sqlite3.Error, UnsafePathError) as error:
        raise ConfigError(
            "durable capture failed; existing evidence is retained"
        ) from error
    finally:
        if store is not None:
            store.close()


def _write_context(path: Path, context: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(context, file, indent=2, sort_keys=True)
        file.write("\n")
    path.chmod(0o400)


def _child_environment(
    environment: dict[str, str],
    key_env: str,
    actor: dict[str, Any],
    run_context: dict[str, Any],
    context_path: Path,
) -> dict[str, str]:
    child = dict(environment)
    child.pop(key_env, None)
    child[CONTEXT_ENV] = str(context_path)
    child["AGENT_ATTRIBUTION_CONTEXT"] = str(context_path)
    child[ACTOR_ENV] = actor["actor_ref"]
    child[RUN_ENV] = run_context["run"]["ref"]
    if run_context["run"].get("task_ref"):
        child[TASK_ENV] = run_context["run"]["task_ref"]
    else:
        child.pop(TASK_ENV, None)

    identities = actor.get("identities", {})
    if identities.get("git_name"):
        child["GIT_AUTHOR_NAME"] = identities["git_name"]
        child["GIT_COMMITTER_NAME"] = identities["git_name"]
    if identities.get("git_email"):
        child["GIT_AUTHOR_EMAIL"] = identities["git_email"]
        child["GIT_COMMITTER_EMAIL"] = identities["git_email"]
    return child


def _spawn(
    command: list[str], cwd: Path, environment: dict[str, str]
) -> tuple[int, bool]:
    handled_signals = [
        candidate
        for candidate in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
        )
        if candidate
    ]
    previous_mask = _block_handled_signals(handled_signals)
    previous_handlers = {
        candidate: signal.getsignal(candidate) for candidate in handled_signals
    }
    installed_signals: list[int] = []
    pending_signal: int | None = None
    process: subprocess.Popen | None = None
    process_tree: ProcessTree | None = None
    tree_ready = False

    def interrupt(signum, _frame):
        nonlocal pending_signal
        pending_signal = signum
        if tree_ready:
            raise RunInterrupted(signum)

    try:
        for candidate in handled_signals:
            signal.signal(candidate, interrupt)
            installed_signals.append(candidate)
        _restore_signal_mask(previous_mask)

        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                start_new_session=os.name == "posix",
                creationflags=WINDOWS_CREATE_SUSPENDED if os.name == "nt" else 0,
            )
        except OSError as error:
            print(
                f"devdiary: could not start command: {error}",
                file=sys.stderr,
            )
            return 127, False

        try:
            process_tree = ProcessTree.attach(process)
            tree_ready = True
            if pending_signal is not None:
                tree_ready = False
                return _terminate(process, process_tree, pending_signal)
            process_tree.resume()
        except OSError as error:
            tree_ready = False
            _stop_unisolated_process(process, process_tree)
            print(
                f"devdiary: could not isolate command process tree: {error}",
                file=sys.stderr,
            )
            return 127, False

        return_code = process.wait()
        tree_ready = False
        process_tree.stop(grace_seconds=1.0)
        if return_code < 0:
            return 128 + abs(return_code), False
        return return_code, False
    except KeyboardInterrupt:
        tree_ready = False
        if process is None or process_tree is None:
            return 128 + signal.SIGINT, True
        return _terminate(process, process_tree, signal.SIGINT)
    except RunInterrupted as error:
        tree_ready = False
        if process is None or process_tree is None:
            return 128 + error.signum, True
        return _terminate(process, process_tree, error.signum)
    finally:
        tree_ready = False
        _block_handled_signals(handled_signals)
        if process_tree is not None:
            process_tree.close()
        for candidate in installed_signals:
            signal.signal(candidate, previous_handlers[candidate])
        _restore_signal_mask(previous_mask)


def _terminate(
    process: subprocess.Popen, process_tree: ProcessTree, signum: int
) -> tuple[int, bool]:
    process_tree.stop()
    process.wait()
    return 128 + signum, True


def _stop_unisolated_process(
    process: subprocess.Popen, process_tree: ProcessTree | None
) -> None:
    if process_tree is not None:
        try:
            process_tree.stop(grace_seconds=0.0)
        finally:
            process_tree.close()
    else:
        process.kill()
    process.wait()


def _block_handled_signals(handled_signals):
    if os.name != "posix" or not hasattr(signal, "pthread_sigmask"):
        return None
    return signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)


def _restore_signal_mask(previous_mask) -> None:
    if previous_mask is not None:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _emit(
    registry: dict[str, Any], key: str | None, envelope: dict[str, Any]
) -> tuple[bool, bool]:
    mode = enforcement(registry)
    if mode == "off":
        return False, False

    url = endpoint(registry)
    if not url or not key:
        missing = "ingest.url" if not url else key_environment(registry)
        disposition = (
            "declaration will be queued"
            if key
            else "declaration cannot be authenticated or queued"
        )
        print(
            f"devdiary: warning: {missing} is not configured; {disposition}",
            file=sys.stderr,
        )
        return False, True

    try:
        post_envelope(url, key, envelope)
        return True, False
    except TransportError as error:
        print(f"devdiary: warning: {error}", file=sys.stderr)
        return False, True


def _parent_run_ref(environment: dict[str, str]) -> str | None:
    context_path = environment.get(CONTEXT_ENV)
    if not context_path:
        return None
    try:
        path = Path(context_path)
        if path.stat().st_size > MAX_CONTEXT_BYTES:
            return None
        parent = json.loads(path.read_text(encoding="utf-8"))
        candidate = parent.get("run", {}).get("ref")
        return candidate if isinstance(candidate, str) and candidate else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _without_secret_like_values(
    environment: dict[str, str], key_env: str
) -> dict[str, str]:
    forbidden = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    return {
        name: value
        for name, value in environment.items()
        if name != key_env and not any(part in name.upper() for part in forbidden)
    }


def _merge_work_references(
    discovered: dict[str, list[str]], explicit: dict[str, list[str]] | None
) -> dict[str, list[str]]:
    explicit = explicit or {}
    return {
        kind: list(dict.fromkeys([*values, *explicit.get(kind, [])]))
        for kind, values in discovered.items()
    }


def _validate_work_references(
    explicit: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    explicit = explicit or {}
    allowed = {"repositories", "commits", "pull_requests", "issues", "artifacts"}
    unknown = sorted(set(explicit) - allowed)
    if unknown:
        raise ConfigError(f"unsupported work reference fields: {', '.join(unknown)}")
    for kind, values in explicit.items():
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ConfigError(f"{kind} must contain non-empty strings")
        for value in values:
            _reject_sensitive_url_components(kind, value)
    return explicit


def _reject_sensitive_url_components(kind: str, value: str) -> None:
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except ValueError as error:
        raise ConfigError(f"{kind} contains a malformed URL") from error
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError(f"{kind} URLs must not contain user information")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{kind} URLs must not contain a query or fragment")
