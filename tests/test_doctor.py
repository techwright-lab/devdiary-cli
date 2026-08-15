from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devdiary_attribution import doctor


class DoctorTest(unittest.TestCase):
    def test_windows_context_permission_check_accepts_read_only_semantics(self) -> None:
        self.assertTrue(doctor._context_permissions_are_safe(0o444, "nt"))
        self.assertFalse(doctor._context_permissions_are_safe(0o666, "nt"))

    def test_windows_registry_integrity_requires_a_regular_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text("{}", encoding="utf-8")

            with mock.patch.object(doctor.os, "name", "nt"):
                check = doctor._registry_integrity_check(path)

        self.assertTrue(check.ok)
        self.assertIn("Windows ACLs", check.detail)


if __name__ == "__main__":
    unittest.main()
