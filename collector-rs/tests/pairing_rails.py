"""Opt-in Rust pairing -> real Rails fixture. No vendor/model/live hooks.
Run under the Rails checkout's Ruby/bundle PATH:
 python3 pairing_rails.py --rails-checkout /reviewed/server --collector /built/binary
Only an unpredictable, verified-absent loopback database is created/dropped.
"""

import argparse
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import tempfile
import uuid
from pathlib import Path

from qualify_claude import clean_env, require, run

HERE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rails-checkout", type=Path, required=True)
    p.add_argument("--collector", type=Path, required=True)
    p.add_argument("--pg-user", default=os.environ.get("USER", ""))
    p.add_argument("--pg-port", type=int, default=5432)
    a = p.parse_args()
    require(a.pg_user.isalnum() and 1 <= a.pg_port <= 65535, "local_pg_required")
    rails = a.rails_checkout.resolve()
    require(
        not list(rails.glob(".env*"))
        and not (rails / "config/master.key").exists()
        and not list((rails / "config/credentials").glob("*.key")),
        "rails_checkout_contains_credentials",
    )

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    report = {
        "evidence": "fixture-driven-real-Rust-Rails",
        "model_calls": 0,
        "live_hooks": False,
    }
    with tempfile.TemporaryDirectory(prefix="pairing-rails-") as tmp:
        root = Path(tmp)
        shutil.copyfile(a.collector.resolve(), root / "collector")
        (root / "collector").chmod(0o700)
        run(["git", "init", "-q", root / "repo"])
        run(
            [
                "git",
                "-C",
                root / "repo",
                "remote",
                "add",
                "origin",
                "git@github.com:fixture/pairing.git",
            ]
        )
        name = "devdiary_qualification_" + uuid.uuid4().hex
        report["database_name"] = name
        report["rails_sha"] = (
            run(["git", "rev-parse", "HEAD"], cwd=rails).decode().strip()
        )
        pg = ["-h", "127.0.0.1", "-p", str(a.pg_port), "-U", a.pg_user]
        env = {
            **clean_env(),
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "PGPASSFILE": str(root / "no-pgpass"),
            "RAILS_ENV": "test",
            "CI": "true",
            "SECRET_KEY_BASE": "pairing-fixture-only",
            "DATABASE_URL": f"postgresql://{a.pg_user}@127.0.0.1:{a.pg_port}/{name}",
            "PAIRING_ROOT": str(root),
        }
        query = [
            "psql",
            *pg,
            "-d",
            "postgres",
            "-Atc",
            f"SELECT count(*) FROM pg_database WHERE datname='{name}'",
        ]
        reserved = False
        try:
            require(run(query, env=env).strip() == b"0", "database_exists")
            reserved = True
            run(["createdb", *pg, name], env=env)
            run(
                ["bundle", "exec", "ruby", HERE / "pairing_rails.rb"],
                cwd=a.rails_checkout.resolve(),
                env=env,
                timeout=120,
            )
            report.update(json.loads((root / "pairing-result.json").read_text()))
            require(report["passed"] and report["collector_revoked"], "rails_cleanup")
            with contextlib.closing(
                sqlite3.connect(root / "connection/outbox/collector-rust-v1.sqlite3")
            ) as db:
                row = db.execute(
                    "SELECT receipt,payload FROM outbox WHERE delivered=1"
                ).fetchone()
                receipt, payload = json.loads(row[0]), json.loads(row[1])
            config = json.loads((root / "connection/connection.json").read_text())
            require(
                receipt
                == {
                    "record_id": report["record_id"],
                    "collector_ref": config["scope"]["collector_ref"],
                    "installation_id": payload["installation_id"],
                    "observation_id": payload["observation_id"],
                },
                "exact_rails_receipt",
            )
            report["exact_rails_receipt"] = True
        except BaseException:
            report["passed"] = False
            raise
        finally:
            try:
                if reserved:
                    try:
                        run(
                            ["dropdb", *pg, "--if-exists", "--force", name],
                            env=env,
                            timeout=120,
                        )
                    finally:
                        report["database_dropped"] = run(query, env=env).strip() == b"0"
                    require(report["database_dropped"], "database_cleanup")
            except BaseException:
                report["passed"] = False
                raise
            finally:
                print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
