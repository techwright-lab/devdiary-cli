from __future__ import annotations

import dataclasses
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devdiary import note_profile as profile
from devdiary.note_validation import NoteError
from test_note_validation import scope


class NoteProfileTest(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("notes profile integration requires POSIX")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.key = self.root / "key"
        self.secret = "dn_live_" + "a" * 43
        self.write_file(self.key, self.secret.encode())
        self.state = self.root / "state"
        self.bound = profile.BoundProfile(
            origin="https://example.test", key_file=str(self.key), **scope()
        )

    def write_file(self, path, raw):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)

    def rejects(self, action, code):
        with self.assertRaises(NoteError) as raised:
            action()
        self.assertTrue(str(raised.exception) == code, "stable error required")
        self.assertTrue(self.secret not in repr(raised.exception), "secret escaped")
        self.assertIsNone(raised.exception.__cause__)

    def test_bound_value_is_frozen_validated_and_locator_is_not_repr(self):
        self.assertTrue(str(self.key) not in repr(self.bound), "locator escaped")
        self.assertTrue(self.secret not in repr(self.bound), "secret escaped")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            setattr(self.bound, "subject_ref", "other")
        self.rejects(
            lambda: dataclasses.replace(self.bound, capability="offline"),
            "invalid_scope",
        )
        self.rejects(
            lambda: dataclasses.replace(self.bound, origin="http://example.test"),
            "invalid_origin",
        )

    def test_missing_guard_is_read_only_and_valid_profile_round_trips(self):
        self.rejects(
            lambda: profile.require_bound_profile(self.state), "binding_required"
        )
        self.assertFalse(self.state.exists())
        with mock.patch.object(
            profile, "read_key_file", side_effect=AssertionError("guard read key")
        ):
            profile.create_profile(self.state, self.bound)
            self.assertTrue(profile.require_bound_profile(self.state) == self.bound)
        raw = (self.state / "notes-profile.json").read_bytes()
        self.assertTrue(set(json.loads(raw)) == {"origin", "key_file", *scope()})
        self.assertTrue(len(raw) <= 4096)
        self.assertTrue(self.secret.encode() not in raw, "secret persisted")
        self.assertEqual(0o700, stat.S_IMODE(self.state.stat().st_mode))
        self.assertEqual(
            0o600, stat.S_IMODE((self.state / "notes-profile.json").stat().st_mode)
        )

    def test_owned_temporary_base_alias_uses_canonical_profile_fixture(self):
        base = self.root / "temporary-base"
        base.mkdir(mode=0o700)
        alias = self.root / "temporary-alias"
        alias.symlink_to(base, target_is_directory=True)
        temporary_directory = tempfile.TemporaryDirectory
        case = NoteProfileTest(
            "test_missing_guard_is_read_only_and_valid_profile_round_trips"
        )
        result = unittest.TestResult()
        with mock.patch.object(
            tempfile,
            "TemporaryDirectory",
            side_effect=lambda: temporary_directory(dir=alias),
        ):
            case.run(result)
        self.assertTrue(case.temp.name.startswith(str(alias) + os.sep))
        self.assertFalse(result.errors, "owned alias fixture setup must complete")
        self.assertFalse(result.skipped, "POSIX alias fixture must run")
        self.assertTrue(
            result.wasSuccessful(),
            "owned profile fixture must canonicalize its temporary root",
        )

    def test_key_family_bounded_read_and_optional_single_lf(self):
        self.assertTrue(profile.read_key_file(str(self.key)) == self.secret)
        self.key.unlink()
        for index, raw in enumerate(
            [
                self.secret.encode() + b"\n",
                b"x" * 4097,
                self.secret.encode() + b"\n\n",
                self.secret.encode() + b" ",
                b"\xff",
                b"dc_live_" + b"a" * 43,
            ]
        ):
            path = self.root / f"candidate{index}"
            self.write_file(path, raw)
            if index == 0:
                self.assertTrue(profile.read_key_file(str(path)) == self.secret)
            else:
                self.rejects(lambda: profile.read_key_file(str(path)), "invalid_key")

    def test_symlink_components_and_hardlinks_are_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.key)
        self.rejects(lambda: profile.read_key_file(str(link)), "unsafe_path")
        ancestor = self.root / "ancestor"
        ancestor.symlink_to(self.root, target_is_directory=True)
        self.rejects(
            lambda: profile.read_key_file(str(ancestor / "key")), "unsafe_path"
        )
        hard = self.root / "hard"
        os.link(self.key, hard)
        self.rejects(lambda: profile.read_key_file(str(self.key)), "unsafe_path")
        self.state.symlink_to(self.root, target_is_directory=True)
        self.rejects(
            lambda: profile.create_profile(self.state, self.bound), "unsafe_path"
        )

    def test_fifo_and_unsafe_file_and_directory_modes_fail_before_read(self):
        fifo = self.root / "fifo"
        os.mkfifo(fifo, 0o600)
        self.rejects(lambda: profile.read_key_file(str(fifo)), "unsafe_path")
        os.chmod(self.key, 0o640)
        self.rejects(lambda: profile.read_key_file(str(self.key)), "unsafe_path")
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o700)
        self.write_file(unsafe / "key", self.secret.encode())
        os.chmod(unsafe, 0o755)
        self.rejects(lambda: profile.read_key_file(str(unsafe / "key")), "unsafe_path")
        os.chmod(unsafe, 0o777)
        self.rejects(
            lambda: profile.create_profile(unsafe / "state", self.bound), "unsafe_path"
        )

    def test_platform_failure_precedes_file_io_and_nested_creation_is_absent(self):
        with mock.patch.object(profile.os, "O_NOFOLLOW", 0):
            self.rejects(
                lambda: profile.read_key_file(str(self.key)), "platform_unsupported"
            )
            self.rejects(
                lambda: profile.create_profile(self.state, self.bound),
                "platform_unsupported",
            )
        self.assertFalse(self.state.exists())
        with mock.patch.object(profile.os, "write", None):
            self.rejects(
                lambda: profile.create_profile(self.state, self.bound),
                "platform_unsupported",
            )
        self.assertFalse(self.state.exists())
        self.rejects(
            lambda: profile.create_profile(
                self.root / "missing" / "nested", self.bound
            ),
            "profile_failed",
        )
        self.assertFalse((self.root / "missing").exists())
        for path in ("relative/key", str(self.root) + "/../key"):
            self.rejects(lambda: profile.read_key_file(path), "unsafe_path")

    def test_guard_rejects_unqualified_partial_and_oversized_files(self):
        self.state.mkdir(mode=0o700)
        path = self.state / "notes-profile.json"
        for raw in (b"{", b"{}", b'{"origin":"https://example.test"}', b"x" * 4097):
            self.write_file(path, raw)
            self.rejects(
                lambda: profile.require_bound_profile(self.state), "binding_required"
            )
            self.rejects(
                lambda: profile.create_profile(self.state, self.bound), "profile_exists"
            )
            self.assertTrue(path.read_bytes() == raw)
            path.unlink()

    def test_sync_order_includes_parent_even_when_state_already_exists(self):
        events = []
        real_sync, real_close = os.fsync, os.close

        def sync(fd):
            events.append(
                "file_sync" if stat.S_ISREG(os.fstat(fd).st_mode) else "dir_sync"
            )
            return real_sync(fd)

        def close(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                events.append("file_close")
            return real_close(fd)

        with (
            mock.patch.object(profile.os, "fsync", side_effect=sync),
            mock.patch.object(profile.os, "close", side_effect=close),
        ):
            profile.create_profile(self.state, self.bound)
        self.assertTrue(
            events == ["file_sync", "file_close", "dir_sync", "dir_sync"],
            "durability order required",
        )
        events.clear()
        other = self.root / "existing"
        other.mkdir(mode=0o700)
        with mock.patch.object(profile.os, "fsync", side_effect=sync):
            profile.create_profile(other, self.bound)
        self.assertTrue(
            events == ["file_sync", "dir_sync", "dir_sync"],
            "existing state sync required",
        )

    def test_parent_sync_is_required_if_a_concurrent_initializer_created_the_state_leaf(
        self,
    ):
        real_mkdir, real_sync = os.mkdir, os.fsync
        parent_id = self.root.stat().st_ino
        synced = []

        def concurrent_mkdir(name, *args, **kwargs):
            real_mkdir(name, *args, **kwargs)
            raise FileExistsError

        def sync(fd):
            synced.append(os.fstat(fd).st_ino)
            real_sync(fd)

        with (
            mock.patch.object(profile.os, "mkdir", side_effect=concurrent_mkdir),
            mock.patch.object(
                profile.os, "supports_dir_fd", os.supports_dir_fd | {profile.os.mkdir}
            ),
            mock.patch.object(profile.os, "fsync", side_effect=sync),
        ):
            profile.create_profile(self.state, self.bound)
        self.assertTrue(parent_id in synced, "concurrent new leaf must be durable")

    def test_partial_write_disk_full_preserves_indeterminate_file_and_blocks_reopen(
        self,
    ):
        real_write = os.write
        calls = 0

        def fail(fd, data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_write(fd, data[:10])
            raise OSError("SENTINEL_DISK")

        with mock.patch.object(profile.os, "write", side_effect=fail):
            self.rejects(
                lambda: profile.create_profile(self.state, self.bound), "profile_failed"
            )
        self.assertTrue(
            (self.state / "notes-profile.json").read_bytes() == b'{"schema_v'
        )
        self.rejects(
            lambda: profile.require_bound_profile(self.state), "binding_required"
        )
        self.rejects(
            lambda: profile.create_profile(self.state, self.bound), "profile_exists"
        )

    def test_each_sync_failure_preserves_file_and_never_claims_success(self):
        for failing in (1, 2, 3):
            state = self.root / f"sync{failing}"
            real_sync = os.fsync
            calls = 0

            def sync(fd):
                nonlocal calls
                calls += 1
                if calls == failing:
                    raise OSError("SENTINEL_SYNC")
                return real_sync(fd)

            with mock.patch.object(profile.os, "fsync", side_effect=sync):
                self.rejects(
                    lambda: profile.create_profile(state, self.bound), "profile_failed"
                )
            self.assertTrue((state / "notes-profile.json").is_file())
            self.assertTrue(
                profile.require_bound_profile(state) == self.bound,
                "current valid bytes are not a configure durability receipt",
            )
            self.rejects(
                lambda: profile.create_profile(state, self.bound), "profile_exists"
            )

    def test_concurrent_exclusive_creates_have_exactly_one_winner(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def create():
            barrier.wait(timeout=2)
            try:
                profile.create_profile(self.state, self.bound)
                outcomes.append("configured")
            except NoteError as error:
                outcomes.append(error.code)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertFalse(
            any(thread.is_alive() for thread in threads), "thread cleanup required"
        )
        self.assertTrue(sorted(outcomes) == ["configured", "profile_exists"])
        self.assertTrue(profile.require_bound_profile(self.state) == self.bound)
        self.assertTrue(
            list(self.state.iterdir()) == [self.state / "notes-profile.json"]
        )

    def test_descriptor_identity_and_owner_checks_fail_closed(self):
        real_stat = os.stat

        def wrong_owner(*args, **kwargs):
            info = real_stat(*args, **kwargs)
            if args[0] == "key":
                values = list(info)
                values[4] = os.getuid() + 1
                return os.stat_result(values)
            return info

        with mock.patch.object(profile.os, "stat", side_effect=wrong_owner):
            # Preserve primitive capability detection while testing descriptor checks.
            with (
                mock.patch.object(
                    profile.os,
                    "supports_dir_fd",
                    os.supports_dir_fd | {profile.os.stat},
                ),
                mock.patch.object(
                    profile.os,
                    "supports_follow_symlinks",
                    os.supports_follow_symlinks | {profile.os.stat},
                ),
            ):
                self.rejects(
                    lambda: profile.read_key_file(str(self.key)), "unsafe_path"
                )
        real_open = os.open

        def swap(name, flags, *args, **kwargs):
            if name == "key":
                self.key.unlink()
                self.key.symlink_to(self.root / "absent")
            return real_open(name, flags, *args, **kwargs)

        with (
            mock.patch.object(profile.os, "open", side_effect=swap),
            mock.patch.object(
                profile.os, "supports_dir_fd", os.supports_dir_fd | {profile.os.open}
            ),
        ):
            self.rejects(lambda: profile.read_key_file(str(self.key)), "unsafe_path")

    def test_existing_valid_profile_is_never_reconfigured(self):
        profile.create_profile(self.state, self.bound)
        raw = (self.state / "notes-profile.json").read_bytes()
        replacement = profile.BoundProfile(
            origin=self.bound.origin, key_file=str(self.key), **scope("scope_b")
        )
        self.rejects(
            lambda: profile.create_profile(self.state, replacement), "profile_exists"
        )
        self.assertTrue((self.state / "notes-profile.json").read_bytes() == raw)


class NoteProfilePlatformTest(unittest.TestCase):
    def test_non_posix_integration_skips_before_temporary_fixture(self):
        result = unittest.TestResult()
        with (
            mock.patch(f"{__name__}.os", SimpleNamespace(name="nt")),
            mock.patch(
                f"{__name__}.tempfile.TemporaryDirectory",
                side_effect=AssertionError("POSIX fixture entered"),
            ) as temporary,
        ):
            NoteProfileTest(
                "test_bound_value_is_frozen_validated_and_locator_is_not_repr"
            ).run(result)
        self.assertTrue(
            result.wasSuccessful() and len(result.skipped) == 1,
            "non-POSIX integration must skip before fixtures",
        )
        temporary.assert_not_called()

    def test_non_posix_profile_operations_fail_closed_before_file_io(self):
        bound = profile.BoundProfile(
            origin="https://example.test",
            key_file="/synthetic/key",
            **scope(),
        )
        operations = (
            lambda: profile.read_key_file("/synthetic/key"),
            lambda: profile.create_profile("/synthetic/state", bound),
            lambda: profile.check_profile_available("/synthetic/state"),
            lambda: profile.require_bound_profile("/synthetic/state"),
        )
        with mock.patch.object(profile, "os", SimpleNamespace(name="nt")):
            for operation in operations:
                with self.assertRaises(NoteError) as raised:
                    operation()
                self.assertTrue(raised.exception.code == "platform_unsupported")


if __name__ == "__main__":
    unittest.main()
