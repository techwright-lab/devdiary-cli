from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = "1.0"


def new_reference(kind: str) -> str:
    return f"urn:devdiary:{kind}:{uuid4()}"


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def execution_chain(
    registry: dict[str, Any], adapter_name: str | None, environment: dict[str, str]
) -> list[dict[str, str]]:
    adapter = registry.get("adapters", {}).get(adapter_name or "", {})
    mapped = _mapped_values(adapter.get("map", {}), environment)
    executor: dict[str, str] = {
        "kind": adapter.get("execution_role", "executor"),
        "system": adapter.get("runtime", adapter_name or "generic-process"),
    }
    if mapped.get("session_ref"):
        executor["session_ref"] = mapped["session_ref"]
    chain = [executor]
    if mapped.get("provider") and mapped.get("model"):
        chain.append(
            {"kind": "model", "provider": mapped["provider"], "model": mapped["model"]}
        )
    return chain


def context(
    registry: dict[str, Any],
    actor: dict[str, Any],
    command: list[str],
    cwd: Path,
    adapter_name: str | None,
    environment: dict[str, str],
    task_ref: str | None,
    parent_ref: str | None,
) -> dict[str, Any]:
    run_ref = new_reference("run")
    principal_ref = actor.get("principal_ref", registry["principal_ref"])
    return {
        "schema_version": SCHEMA_VERSION,
        "actor": {
            "ref": actor["actor_ref"],
            "kind": actor.get("kind", "agent"),
            "display_name": actor["display_name"],
            "humanized_name": actor.get("humanized_name"),
        },
        "principal": {"ref": principal_ref},
        "run": {"ref": run_ref, "parent_ref": parent_ref, "task_ref": task_ref},
        "started_at": timestamp(),
        "execution_chain": execution_chain(registry, adapter_name, environment),
        "command": {
            "classification": "wrapped-process",
            "argument_count": max(0, len(command) - 1),
        },
        "working_directory": str(cwd.resolve()),
    }


def terminal_envelope(
    run_context: dict[str, Any],
    actor: dict[str, Any],
    event_type: str,
    ended_at: str,
    work: dict[str, list[str]],
) -> dict[str, Any]:
    ended_at = _ordered_end_time(run_context["started_at"], ended_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": new_reference("event"),
        "event_type": event_type,
        "actor": {
            "ref": run_context["actor"]["ref"],
            "kind": run_context["actor"]["kind"],
            "display_name": run_context["actor"]["display_name"],
            "humanized_name": run_context["actor"].get("humanized_name"),
        },
        "lane_ref": actor.get("lane"),
        "principal_ref": run_context["principal"]["ref"],
        "run_ref": run_context["run"]["ref"],
        "parent_run_ref": run_context["run"].get("parent_ref"),
        "task_refs": [run_context["run"]["task_ref"]]
        if run_context["run"].get("task_ref")
        else [],
        "session_type": "coding",
        "started_at": run_context["started_at"],
        "ended_at": ended_at,
        "execution_chain": run_context["execution_chain"],
        "work": work,
        "outcome": {"status": event_type.removeprefix("run.")},
        "attribution": {
            "source": "declared",
            "attester_ref": actor["attester_ref"],
            "confidence": 1.0,
        },
    }


def _mapped_values(
    mapping: dict[str, str], environment: dict[str, str]
) -> dict[str, str]:
    values: dict[str, str] = {}
    for target, source in mapping.items():
        if source.startswith("env:"):
            value = environment.get(source.removeprefix("env:"))
            if value:
                values[target] = value
    return values


def _ordered_end_time(started_at: str, ended_at: str) -> str:
    start = datetime.fromisoformat(started_at)
    finish = datetime.fromisoformat(ended_at)
    if finish <= start:
        finish = start + timedelta(microseconds=1)
    return finish.isoformat().replace("+00:00", "Z")
