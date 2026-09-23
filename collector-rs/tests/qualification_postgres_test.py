"""Opt-in owned-PostgreSQL target/cleanup proof; no Rails app or model calls.

QUALIFICATION_PG_TEST=1 QUALIFICATION_RUBY=/absolute/ruby python3 ... -v
Uses only a random verified-absent DB on loopback, through the production parent
cleanup path. Requires installed ActiveRecord/pg; never uses a foreign database.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import qualify_claude as q


@unittest.skipUnless(
    os.environ.get("QUALIFICATION_PG_TEST") == "1", "opt-in local PG proof"
)
class OwnedPostgresTest(unittest.TestCase):
    def test_real_schema_connection_and_parent_drop_after_failure(self):
        with tempfile.TemporaryDirectory(prefix="qualification-pg-") as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "db").mkdir()
            (root / "config/boot.rb").write_text("")
            (root / "config/environment.rb").write_text(
                'ActiveRecord::Base.configurations = {"test" => {"url" => ENV.fetch("DATABASE_URL")}}\n'
                "ActiveRecord::Base.establish_connection(:test)\n"
            )
            (root / "db/schema.rb").write_text(
                "ActiveRecord::Schema.define { create_table(:qualification_probe, force: true) { |t| t.string :value } }\n"
                "ActiveRecord::Base.connection.execute(\"INSERT INTO qualification_probe(value) VALUES ('owned')\")\n"
            )
            code = (
                "file = ARGV.fetch(0); "
                'eval(File.read(file).split(%q{require "factory_bot_rails"}).first, TOPLEVEL_BINDING, file); '
                'raise "schema proof failed" unless ActiveRecord::Base.connection.select_value("SELECT value FROM qualification_probe") == "owned"; '
                'puts "owned-target-verified"'
            )
            real_run = q.run
            ran_schema = []

            def run(argv, **kwargs):
                if argv[0] == "bundle":
                    output = real_run(
                        [
                            os.environ["QUALIFICATION_RUBY"],
                            "-e",
                            "begin; "
                            + code
                            + "; rescue => e; puts e.full_message; end",
                            q.HERE / "qualify_rails.rb",
                        ],
                        **kwargs,
                    )
                    self.assertIn(b"owned-target-verified", output)
                    ran_schema.append(True)
                    raise RuntimeError("injected_after_real_schema")
                return real_run(argv, **kwargs)

            args = SimpleNamespace(
                pg_user=os.environ["USER"],
                pg_port=int(os.environ.get("QUALIFICATION_PG_PORT", "5432")),
                rails_checkout=root,
            )
            report = {}
            with (
                patch.object(q, "run", side_effect=run),
                self.assertRaisesRegex(RuntimeError, "injected_after_real_schema"),
            ):
                q.rails_interop(args, root, q.clean_env(), report)
            self.assertEqual(ran_schema, [True])
            self.assertTrue(report["database_dropped"])
            print("owned schema/application target verified; parent DROP verified")


if __name__ == "__main__":
    unittest.main()
