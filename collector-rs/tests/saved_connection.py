"""A connection saved the way pairing leaves it, so `devdiary setup` reuses it."""

import json
import os
import uuid
from pathlib import Path

COLLECTOR_REF = "urn:devdiary:collector:11111111-1111-4111-8111-111111111111"
TOKEN = "dc_live_" + "s" * 43
FAKE_SYSTEMCTL = """#!/bin/sh
case "$*" in
  *is-enabled*) echo enabled ;;
  *is-active*) echo active ;;
esac
"""


def save(
    home,
    origin,
    refs,
    versions=(1, 2),
    opt_in=False,
    collector_ref=COLLECTOR_REF,
    installation_id=None,
):
    """Write installation.json and connection.json; return the connection folder."""
    data = Path(home) / ".local/share/devdiary/cli/claude"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.mkdir(mode=0o700)
    connection = data / "connection"
    connection.mkdir(mode=0o700)
    installation = {
        "installation_id": installation_id or str(uuid.uuid4()),
        "origin": origin,
        "scope_kind": "installation",
        "record_orchestrated_outside_repos": opt_in,
    }
    scope = {
        "endpoint": origin + "/ingest/v1/observations",
        "collector_ref": collector_ref,
        "installation_id": installation["installation_id"],
        "scope_kind": "installation",
        "observation_schema_versions": list(versions),
        "record_orchestrated_outside_repos": opt_in,
    }
    saved = {"scope": scope, "token": TOKEN, "repository_refs": list(refs)}
    for name, value in (
        ("installation.json", installation),
        ("connection.json", saved),
    ):
        (connection / name).write_text(json.dumps(value))
        (connection / name).chmod(0o600)
    return connection


def environment(root, home, base=None):
    """HOME, a unit folder and a fake systemctl under `root`, and no wait for a first session."""
    systemctl = Path(root) / "bin/systemctl"
    systemctl.parent.mkdir(exist_ok=True)
    systemctl.write_text(FAKE_SYSTEMCTL)
    systemctl.chmod(0o700)
    base = os.environ if base is None else base
    return {
        **{k: v for k, v in base.items() if k != "CODEX_HOME"},
        "HOME": str(home),
        "DEVDIARY_DEBUG_FIRST_SESSION_WAIT": "0",
        "XDG_CONFIG_HOME": str(Path(root) / "config"),
        "DEVDIARY_COLLECTOR_SYSTEMCTL": str(systemctl),
    }
