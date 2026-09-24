"""Parent pairing harness cleanup, without PostgreSQL/Rails/vendor execution."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pairing_rails as p


class PairingCleanupTest(unittest.TestCase):
    def exercise(self, fault):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            binary = root / "collector"
            binary.write_bytes(b"fixture")
            calls = []
            queries = 0

            def run(argv, **kwargs):
                nonlocal queries
                calls.append(list(map(str, argv)))
                if argv[0] == "psql":
                    queries += 1
                    return b"1" if fault == "preexisting" else b"0"
                if argv[0] == "createdb" and fault == "create_interrupt":
                    raise KeyboardInterrupt
                if argv[0] == "bundle":
                    raise RuntimeError("rails_failed")
                if argv[0] == "dropdb" and fault == "drop_timeout":
                    raise RuntimeError("drop_failed")
                return b"fixture-sha"

            output = io.StringIO()
            with (
                patch.object(p, "run", side_effect=run),
                patch.object(p.signal, "signal"),
                patch(
                    "sys.argv",
                    [
                        "pairing_rails.py",
                        "--rails-checkout",
                        str(root),
                        "--collector",
                        str(binary),
                    ],
                ),
                contextlib.redirect_stdout(output),
                self.assertRaises((Exception, KeyboardInterrupt)),
            ):
                p.main()
            report = json.loads(output.getvalue())
            self.assertFalse(report.get("passed", False))
            drops = [c for c in calls if c[0] == "dropdb"]
            if fault == "preexisting":
                self.assertEqual(drops, [])
                self.assertEqual(queries, 1)
            else:
                self.assertEqual(len(drops), 1)
                self.assertEqual(drops[0][-1], report["database_name"])
                self.assertEqual(queries, 2)
                self.assertTrue(report["database_dropped"])

    def test_verified_absent_name_reserved_before_interrupted_create(self):
        self.exercise("create_interrupt")

    def test_failed_rails_still_drops(self):
        self.exercise("rails")

    def test_drop_error_still_checks_absence_and_prints_owned_name(self):
        self.exercise("drop_timeout")

    def test_preexisting_database_is_never_dropped(self):
        self.exercise("preexisting")


if __name__ == "__main__":
    unittest.main()
