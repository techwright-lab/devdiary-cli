from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from devdiary.config import endpoint, find_actor, key_environment


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_checks(
    registry: dict[str, Any],
    actor_ref: str | None,
    environment: dict[str, str] | None = None,
    registry_path: Path | None = None,
) -> list[Check]:
    current_environment = dict(os.environ if environment is None else environment)
    checks = [
        Check("registry", True, "syntax and portable identity invariants are valid"),
        _schema_check(),
        _git_check(),
        _context_permissions_check(),
        _endpoint_check(registry),
        _key_check(registry, current_environment),
    ]
    if registry_path is not None:
        checks.append(_registry_integrity_check(registry_path))
    if actor_ref:
        actor = find_actor(registry, actor_ref)
        checks.append(
            Check("actor", True, f"resolved {actor['actor_ref']} ({actor['kind']})")
        )
    return checks


def report(checks: list[Check], json_output: bool = False) -> int:
    if json_output:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        for check in checks:
            marker = "PASS" if check.ok else "FAIL"
            print(f"{marker:4} {check.name}: {check.detail}")
    return 0 if all(check.ok for check in checks) else 1


def _schema_check() -> Check:
    try:
        schema_package = resources.files("devdiary.schemas")
        names = ("registry.schema.json", "context.schema.json", "envelope.schema.json")
        for name in names:
            json.loads(schema_package.joinpath(name).read_text(encoding="utf-8"))
        return Check("schemas", True, "three bundled JSON Schemas are readable")
    except (OSError, json.JSONDecodeError) as error:
        return Check("schemas", False, f"bundled schema is invalid: {error}")


def _git_check() -> Check:
    path = shutil.which("git")
    return Check(
        "git", bool(path), "git is available" if path else "git is not installed"
    )


def _context_permissions_check() -> Check:
    with tempfile.TemporaryDirectory(prefix="devdiary-doctor-") as directory:
        path = Path(directory) / "context.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        path.chmod(0o400)
        permissions = stat.S_IMODE(path.stat().st_mode)
    read_only = _context_permissions_are_safe(permissions, os.name)
    return Check(
        "context-permissions",
        read_only,
        f"temporary context mode is {permissions:04o}",
    )


def _context_permissions_are_safe(permissions: int, platform: str) -> bool:
    if platform == "nt":
        return not bool(permissions & stat.S_IWRITE)
    return permissions == 0o400


def _registry_integrity_check(path: Path) -> Check:
    if os.name == "nt":
        ok = path.is_file() and not path.is_symlink()
        detail = (
            "regular file using inherited Windows ACLs"
            if ok
            else "registry is not a regular file"
        )
        return Check("registry-integrity", ok, detail)

    metadata = path.stat()
    mode = stat.S_IMODE(metadata.st_mode)
    writable_by_others = bool(mode & 0o022)
    wrong_owner = hasattr(os, "getuid") and metadata.st_uid != os.getuid()
    ok = not writable_by_others and not wrong_owner
    detail = "owner-controlled" if ok else "group/world writable or foreign-owned"
    return Check("registry-integrity", ok, detail)


def _endpoint_check(registry: dict[str, Any]) -> Check:
    value = endpoint(registry)
    if not value:
        return Check("ingest-url", False, "ingest.url is not configured")
    return Check("ingest-url", True, "ingest URL is configured with an accepted scheme")


def _key_check(registry: dict[str, Any], environment: dict[str, str]) -> Check:
    name = key_environment(registry)
    present = bool(environment.get(name))
    detail = f"{name} is present" if present else f"{name} is missing"
    return Check("ingest-key", present, detail)
