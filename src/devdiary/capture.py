"""Portable begin/freeze/deliver lifecycle, independent of any executor CLI."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from devdiary import capture_git, contract
from devdiary import capture_validation as v
from devdiary.capture_store import PUBLIC_FIELDS, Store, dumps_record, private_file
from devdiary.config import endpoint, find_actor, key_environment, validate_registry
from devdiary.secure_paths import UnsafePathError
from devdiary.transport import TransportError, post_envelope


def begin(store: Store, registry: dict, data: dict) -> dict:
    v.obj(
        data,
        {
            "actor_ref",
            "run_ref",
            "cwd",
            "execution_chain",
            "task_refs",
            "parent_run_ref",
            "key_file",
            "source",
        },
        {"actor_ref", "run_ref", "cwd", "execution_chain"},
    )
    request = {
        "actor_ref": v.text(data["actor_ref"]),
        "run_ref": v.text(data["run_ref"]),
        "cwd": v.text(data["cwd"]),
        "execution_chain": v.chain(data["execution_chain"]),
        "task_refs": v.strings(data.get("task_refs", [])),
        "parent_run_ref": v.text(data["parent_run_ref"])
        if data.get("parent_run_ref") is not None
        else None,
        "source": v.source(data.get("source", {})),
        "key_file": v.text(data["key_file"]) if "key_file" in data else None,
    }
    cwd = Path(request["cwd"])
    if not cwd.is_absolute() or not cwd.is_dir():
        v.fail("invalid_cwd")
    if request["key_file"] and not Path(request["key_file"]).is_absolute():
        v.fail("invalid_key_file")
    with store.transaction():
        existing = store.db.execute(
            "SELECT record FROM captures WHERE run_ref=?", (request["run_ref"],)
        ).fetchone()
        if existing:
            record = json.loads(existing["record"])
            if record["request"] != request:
                v.fail("begin_conflict")
            return _opened(record)
        validate_registry(registry)
        actor = find_actor(registry, request["actor_ref"])
        locator = (
            {"key_file": request["key_file"]}
            if request["key_file"]
            else {"key_env": key_environment(registry)}
        )
        if request["key_file"]:
            os.close(private_file(Path(request["key_file"]), readonly=True))
        context = contract.context(
            registry,
            actor,
            [],
            cwd,
            None,
            {},
            next(iter(request["task_refs"]), None),
            request["parent_run_ref"],
        )
        context["run"]["ref"] = request["run_ref"]
        context["execution_chain"] = request["execution_chain"]
        capture_id = str(uuid4())
        before = capture_git.snapshot(cwd)
        path = store.context_file(capture_id, context)
        environment = {
            "DEVDIARY_ATTRIBUTION_CONTEXT": str(path),
            "AGENT_ATTRIBUTION_CONTEXT": str(path),
            "DEVDIARY_CAPTURE_ID": capture_id,
        }
        identities = actor.get("identities", {})
        for field, suffix in (("git_name", "NAME"), ("git_email", "EMAIL")):
            if identities.get(field):
                for role in ("AUTHOR", "COMMITTER"):
                    environment[f"GIT_{role}_{suffix}"] = identities[field]
        record = {
            "capture_id": capture_id,
            "actor_ref": actor["actor_ref"],
            "run_ref": request["run_ref"],
            "source": request["source"],
            "state": "open",
            "started_at": context["started_at"],
            "request": request,
            "actor": actor,
            "endpoint": endpoint(registry),
            "credential": locator,
            "context": context,
            "before": before,
            "environment": environment,
        }
        try:
            store.db.execute(
                "INSERT INTO captures(capture_id,run_ref,state,record) VALUES (?,?,?,?)",
                (capture_id, record["run_ref"], "open", dumps_record(record)),
            )
        except BaseException:
            path.chmod(0o600)
            path.unlink(missing_ok=True)
            raise
    return _opened(record)


def _opened(record: dict) -> dict:
    return {
        k: record[k] for k in ("capture_id", "actor_ref", "run_ref", "environment")
    } | {"state": "open"}


def freeze(store: Store, data: dict) -> dict:
    v.obj(
        data, {"capture_id", "outcome", "ended_at", "work"}, {"capture_id", "outcome"}
    )
    capture_id = v.text(data["capture_id"])
    if data["outcome"] not in ("completed", "failed", "cancelled"):
        v.fail()
    claim = {"outcome": data["outcome"], "work": v.work(data.get("work", {}))}
    if "ended_at" in data:
        claim["ended_at"] = v.end_time(data["ended_at"])
    with store.transaction():
        record = store.get(capture_id)
        if record["state"] != "open":
            if (
                record["claim"]["outcome"] != claim["outcome"]
                or ("work" in data and record["claim"]["work"] != claim["work"])
                or (
                    "ended_at" in data
                    and datetime.fromisoformat(record["ended_at"])
                    != datetime.fromisoformat(claim["ended_at"])
                )
            ):
                v.fail("finish_conflict")
            return record
        actor = record["actor"]
        work, unavailable = capture_git.collect(
            Path(record["request"]["cwd"]),
            record["before"],
            actor.get("identities", {}).get("git_email"),
            delayed="ended_at" in data,
            started_at=record["started_at"],
            ended_at=claim.get("ended_at"),
        )
        for kind, values in claim["work"].items():
            work[kind] = list(dict.fromkeys([*work[kind], *values]))
        ended = claim.get("ended_at", contract.timestamp())
        if "ended_at" in data and datetime.fromisoformat(
            ended
        ) <= datetime.fromisoformat(record["started_at"]):
            v.fail("invalid_end_time")
        envelope = contract.terminal_envelope(
            record["context"], actor, "run." + claim["outcome"], ended, work
        )
        envelope["task_refs"] = record["request"]["task_refs"]
        envelope["extensions"] = {"devdiary_capture": {"source": record["source"]}}
        if unavailable:
            envelope["extensions"]["devdiary_capture"]["git_collection"] = unavailable
        v.dumps(envelope)  # refuse oversized evidence BEFORE committing state
        record.update(
            state="pending",
            ended_at=envelope["ended_at"],
            claim=claim,
            envelope=envelope,
        )
        store.save(record)
    return record


def _credential(record: dict, key_override: str | None = None) -> str:
    if key_override is not None:
        key = key_override
    elif "key_file" in record["credential"]:
        fd = private_file(Path(record["credential"]["key_file"]), readonly=True)
        with os.fdopen(fd, "r", encoding="utf-8") as file:
            key = file.read(16385)
        key = key.rstrip("\r\n")
    else:
        key = os.environ.get(record["credential"]["key_env"], "")
    if not key or len(key) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        v.fail("credential_unavailable")
    return key


def deliver(
    store: Store,
    capture_id: str,
    *,
    replay: bool = False,
    key_override: str | None = None,
) -> dict:
    # Serial delivery holds a reserved SQLite lock, not an uncommitted envelope.
    # Readers can see the durable pending record throughout HTTP. Process death
    # releases the lock; recovery simply replays the already-frozen event ID.
    with store.transaction():
        record = store.get(capture_id)
        if record["state"] == "open":
            v.fail("capture_not_finished")
        if record["state"] == "delivered" and not replay:
            return public(record)
        record["last_attempt_at"] = contract.timestamp()
        try:
            if not record["endpoint"]:
                v.fail("endpoint_unavailable")
            key = _credential(record, key_override)
            receipt = post_envelope(record["endpoint"], key, record["envelope"])
            record.update(state="delivered", receipt=receipt)
            record.pop("error_code", None)
        except (OSError, UnicodeError, v.CaptureError, TransportError, UnsafePathError):
            # Never copy transport bodies, paths, URLs, key material or raw errors.
            record["error_code"] = "delivery_failed"
        store.save(record)
    return public(record)


def public(record: dict) -> dict:
    return {k: record[k] for k in PUBLIC_FIELDS if k in record}


def status(store: Store, data: dict) -> dict:
    v.obj(data, {"states", "after", "limit", "source"})
    states = v.strings(data.get("states", sorted(v.STATES)))
    if set(states) - v.STATES:
        v.fail()
    source = v.source(data.get("source", {}))
    limit = data.get("limit", 100)
    if type(limit) is not int or not 1 <= limit <= 500:
        v.fail()
    cursor = data.get("after")
    if cursor is not None and (
        not isinstance(cursor, str)
        or not cursor.isascii()
        or not cursor.isdecimal()
        or len(cursor) > 18
    ):
        v.fail("invalid_cursor")
    rows = store.status_rows(states, source, int(cursor or 0), limit + 1)
    matches = []
    for row in rows:
        projection = json.loads(row["projection"])
        item = projection["public"]
        git_identity = {
            field: value
            for field, value in zip(("name", "email"), projection["git_identity"])
            if value
        }
        if git_identity:
            item["git_identity"] = git_identity
        matches.append((row["seq"], item))
    return {
        "captures": [r for _, r in matches[:limit]],
        "next_cursor": str(matches[limit - 1][0]) if len(matches) > limit else None,
    }
