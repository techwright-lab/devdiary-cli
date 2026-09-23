"""Explicit, consented observer delivery. Never imported by the vendor hook."""

from __future__ import annotations

import http.client
import json
import re
import signal
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from devdiary import observer, observer_hook
from devdiary.transport import NoRedirectHandler

FIELDS = (
    "schema_version",
    "observation_id",
    "installation_id",
    "runtime",
    "session_id",
    "event",
    "observed_at",
    "attribution_basis",
    "actor_ref",
    "prompt_id",
    "turn_id",
    "tool_use_id",
    "agent_id",
    "agent_type",
    "model",
    "tool_name",
    "source",
    "reason",
    "repository",
)
CONNECTION = "upload-connection.json"
MAX_BYTES = 16384
DEADLINE = 5


def strict_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("invalid_json")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_json")),
    )


def validate_endpoint(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/ingest/v1/observations"
        or not parsed.hostname
        or not (
            parsed.scheme == "https"
            or (
                parsed.scheme == "http"
                and parsed.hostname in ("127.0.0.1", "::1", "localhost")
            )
        )
    ):
        raise ValueError("invalid_endpoint")
    return value


def credential(path):
    path = observer.absolute(path)
    observer_hook.safe_path(path)
    if not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("invalid_credential_file")
    value = path.read_text().strip()
    if not re.fullmatch(r"dc_live_[A-Za-z0-9_-]+", value):
        raise ValueError("invalid_collector_credential")
    return value


def connection(state):
    path = state / CONNECTION
    observer_hook.safe_path(path)
    raw = observer.read(path)
    if raw is None:
        raise ValueError("connection_required")
    return strict_json(raw)


def scope(config):
    return observer.digest(
        observer.encode({k: v for k, v in config.items() if k != "key_file"})
    )


def database(state):
    db = observer_hook.connect(state)
    db.execute(
        "CREATE TABLE IF NOT EXISTS upload_cursor (singleton INTEGER PRIMARY KEY CHECK(singleton=1), seq INTEGER NOT NULL)"
    )
    db.execute("INSERT OR IGNORE INTO upload_cursor VALUES (1, 0)")
    # Separate from the freeze cursor; additive migration preserves old outboxes.
    db.execute(
        "CREATE TABLE IF NOT EXISTS upload_attempt_cursor (singleton INTEGER PRIMARY KEY CHECK(singleton=1), seq INTEGER NOT NULL)"
    )
    db.execute("INSERT OR IGNORE INTO upload_attempt_cursor VALUES (1, 0)")
    db.execute("""CREATE TABLE IF NOT EXISTS upload_outbox (
        seq INTEGER PRIMARY KEY, scope TEXT NOT NULL, endpoint TEXT NOT NULL,
        collector_ref TEXT NOT NULL, payload BLOB NOT NULL,
        delivered INTEGER NOT NULL DEFAULT 0, receipt TEXT, failure TEXT)""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS upload_pending ON upload_outbox(delivered, seq)"
    )
    db.commit()
    return db


def configure(
    state, *, endpoint, collector_ref, repository_ref, key_file, consent=False
):
    if not consent:
        raise ValueError("consent_required")
    state = observer.absolute(state)
    validate_endpoint(endpoint)
    if (
        not re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository_ref
        )
        or repository_ref.endswith((".git", "/.", "/.."))
        or not observer_hook.token(collector_ref)
    ):
        raise ValueError("invalid_connection")
    key_file = observer.absolute(key_file)
    credential(key_file)
    with observer.locked(state / "uploader"), observer.locked(state):
        m = observer.manifest(state)
        if not m or m["status"] != "installed":
            raise ValueError("installed_scope_required")
        config = {
            "schema_version": 1,
            "endpoint": endpoint,
            "collector_ref": collector_ref,
            "repository_ref": repository_ref,
            "repository": m["repository"],
            "installation_id": m["installation_id"],
            "key_file": str(key_file),
        }
        with closing(database(state)) as db:
            # Never silently retarget even an acknowledged installation. Rotating
            # the key locator is safe only with identical collector/config scope.
            if db.execute(
                "SELECT 1 FROM upload_outbox WHERE scope != ? LIMIT 1", (scope(config),)
            ).fetchone():
                raise ValueError("frozen_connection_conflict")
        path = state / CONNECTION
        observer_hook.safe_path(path)
        observer.atomic(
            path, observer.encode(config), observer.digest(observer.read(path))
        )
    return {"connection": "configured", "delivery": "explicit_sync_only"}


def envelope(row, config):
    if (
        row.pop("repository") != config["repository"]
        or row["installation_id"] != config["installation_id"]
    ):
        raise ValueError("installation_scope_conflict")
    row["repository_ref"] = config["repository_ref"]
    micros = int(
        (Decimal(str(row["observed_at"])) * 1000000).to_integral_value(
            rounding=ROUND_HALF_EVEN
        )
    )
    row["observed_at"] = (
        datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)
    ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    payload = json.dumps(
        row, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(payload) > MAX_BYTES:
        raise ValueError("observation_too_large")
    return payload


def freeze(db, config, limit):
    # SQL projects only public metadata. Cursor avoids rescanning delivered history.
    paths = tuple(f"$.{field}" for field in FIELDS)
    db.execute("BEGIN IMMEDIATE")
    try:
        cursor = db.execute(
            "SELECT seq FROM upload_cursor WHERE singleton=1"
        ).fetchone()[0]
        rows = db.execute(
            "SELECT seq,json_extract(metadata, ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "FROM observations WHERE seq > ? ORDER BY seq LIMIT ?",
            (*paths, cursor, limit),
        ).fetchall()
        for values in rows:
            row = {
                field: value
                for field, value in zip(FIELDS, json.loads(values[1]))
                if value is not None
            }
            payload = envelope(row, config)
            db.execute(
                "INSERT INTO upload_outbox(seq,scope,endpoint,collector_ref,payload) VALUES (?,?,?,?,?)",
                (
                    values[0],
                    scope(config),
                    config["endpoint"],
                    config["collector_ref"],
                    payload,
                ),
            )
            db.execute("UPDATE upload_cursor SET seq=? WHERE singleton=1", (values[0],))
        db.commit()
    except BaseException:
        db.rollback()
        raise


def receipt(raw, payload, collector_ref):
    value = strict_json(raw)
    data = strict_json(payload)
    expected = {k: data[k] for k in ("observation_id", "installation_id")}
    expected["collector_ref"] = collector_ref
    if (
        not isinstance(value, dict)
        or set(value) != {*expected, "record_id"}
        or any(value[k] != v for k, v in expected.items())
        or type(value["record_id"]) is not int
        or value["record_id"] <= 0
    ):
        raise ValueError("invalid_receipt")
    return value


def post(endpoint, key, payload, collector_ref):
    # Wall deadline includes DNS, TLS, and trickling response bodies, unlike the
    # socket timeout alone. Explicit CLI sync runs on the POSIX main thread.
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError()))
    signal.setitimer(signal.ITIMER_REAL, DEADLINE)
    try:
        request = urllib.request.Request(
            validate_endpoint(endpoint),
            data=payload,
            method="POST",
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirectHandler
        )
        with opener.open(request, timeout=4) as response:
            if response.status not in (200, 201):
                return None, "invalid_status"
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None, "invalid_receipt"
            return receipt(raw, payload, collector_ref), None
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        return None, f"http_{code}"
    except (OSError, http.client.HTTPException):
        return None, "network_failure"
    except (ValueError, UnicodeError, RecursionError):
        return None, "invalid_receipt"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def acknowledge(db, seq, value):
    db.execute(
        "UPDATE upload_outbox SET delivered=1, receipt=?, failure=NULL WHERE seq=?",
        (json.dumps(value, sort_keys=True), seq),
    )
    db.commit()


def pending_batch(db, limit):
    # Two indexed ranges bound both reads and memory; each pending row appears
    # at most once even when we wrap within this batch. Caller holds uploader lock.
    cursor = db.execute(
        "SELECT seq FROM upload_attempt_cursor WHERE singleton=1"
    ).fetchone()[0]
    query = "SELECT seq,endpoint,collector_ref,payload FROM upload_outbox WHERE delivered=0 AND "
    rows = db.execute(
        query + "seq > ? ORDER BY seq LIMIT ?", (cursor, limit)
    ).fetchall()
    if len(rows) < limit:
        rows += db.execute(
            query + "seq <= ? ORDER BY seq LIMIT ?", (cursor, limit - len(rows))
        ).fetchall()
    return rows


def sync(state, limit=100):
    state = observer.absolute(state)
    limit = min(max(int(limit), 1), 100)
    # Separate uploader lock: hooks keep collecting while HTTP is in flight.
    with observer.locked(state / "uploader"):
        config = connection(state)
        key = credential(config["key_file"])
        with closing(database(state)) as db:
            if db.execute(
                "SELECT 1 FROM upload_outbox WHERE scope != ? LIMIT 1", (scope(config),)
            ).fetchone():
                raise ValueError("frozen_connection_conflict")
            freeze(db, config, limit)
            rows = pending_batch(db, limit)
            for seq, endpoint, collector_ref, payload in rows:
                # Commit before HTTP so a crash or lost response cannot pin the
                # next process to a poison row. Frozen bytes remain retryable.
                db.execute(
                    "UPDATE upload_attempt_cursor SET seq=? WHERE singleton=1", (seq,)
                )
                db.commit()
                value, failure = post(endpoint, key, payload, collector_ref)
                if failure:
                    db.execute(
                        "UPDATE upload_outbox SET failure=? WHERE seq=?", (failure, seq)
                    )
                    db.commit()
                    # Shared outages should not hammer the endpoint. Row-specific
                    # failures still allow the rest of this bounded page through.
                    if failure in (
                        "http_401",
                        "http_403",
                        "http_429",
                        "network_failure",
                    ) or failure.startswith("http_5"):
                        break
                    continue
                acknowledge(db, seq, value)
    return health(state)


def health(state):
    state = observer.absolute(state)
    observer_hook.safe_path(state)
    path = state / "observations.sqlite3"
    observer_hook.safe_path(path)
    if not path.exists():
        return {"pending": 0, "delivered": 0, "last_failure": None}
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05)
    ) as db:
        total = db.execute("SELECT count(*) FROM observations").fetchone()[0]
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='upload_outbox'"
        ).fetchone():
            return {"pending": total, "delivered": 0, "last_failure": None}
        delivered = db.execute(
            "SELECT count(*) FROM upload_outbox WHERE delivered=1"
        ).fetchone()[0]
        failure = db.execute(
            "SELECT failure FROM upload_outbox WHERE failure IS NOT NULL ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return {
            "pending": total - delivered,
            "delivered": delivered,
            "last_failure": failure[0] if failure else None,
        }
