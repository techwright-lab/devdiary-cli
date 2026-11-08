from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from devdiary import note_profile as profile
from devdiary import notes
from devdiary.cli import main
from devdiary.note_profile import require_bound_profile
from devdiary.note_validation import NoteError
from test_note_validation import scope


class NotesTest(unittest.TestCase):
    def setUp(self):
        if os.name != "posix":
            self.skipTest("notes configure integration requires POSIX")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.key = self.root / "key"
        self.secret = "dn_live_" + "a" * 43
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(self.secret.encode())

    def setup_value(self, metadata=None):
        return {
            "schema_version": 1,
            "origin": "https://example.test",
            "key_file": str(self.key),
            "metadata": scope() if metadata is None else metadata,
        }

    def cli(self, value=None, *, arguments=None, raw=None):
        output, error = io.StringIO(), io.StringIO()
        stream = SimpleNamespace(
            buffer=io.BytesIO(
                raw
                if raw is not None
                else json.dumps(value or self.setup_value()).encode()
            )
        )
        with (
            mock.patch("sys.stdin", stream),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            result = main(
                arguments or ["notes", "configure", "--state-dir", str(self.state)]
            )
        self.assertTrue(error.getvalue() == "", "stderr escaped")
        self.assertTrue(
            self.secret not in output.getvalue()
            and str(self.key) not in output.getvalue(),
            "private value escaped",
        )
        return result, output.getvalue()

    def assert_error(self, result, code):
        self.assertTrue(
            result
            == (2, json.dumps({"error_code": code}, separators=(",", ":")) + "\n"),
            "safe error required",
        )

    def test_configure_matches_metadata_and_never_uses_registry_or_runner(self):
        with (
            mock.patch(
                "devdiary.cli.load_registry",
                side_effect=AssertionError("registry accessed"),
            ),
            mock.patch(
                "devdiary.cli.runner.run_command",
                side_effect=AssertionError("runner called"),
            ),
            mock.patch.dict(
                os.environ,
                {
                    "DEVDIARY_INGEST_KEY": "SENTINEL",
                    "DEVDIARY_RUNTIME_SESSION_REF": "SENTINEL",
                },
            ),
            mock.patch.object(notes, "get_scope", return_value=scope()) as get,
        ):
            result = self.cli(
                arguments=[
                    "--config",
                    str(self.root / "absent"),
                    "notes",
                    "configure",
                    "--state-dir",
                    str(self.state),
                ]
            )
        self.assertTrue(result == (0, '{"profile":"configured"}\n'))
        self.assertTrue(get.call_args.args == ("https://example.test", self.secret))
        bound = require_bound_profile(self.state)
        self.assertTrue(bound.subject_ref == scope()["subject_ref"])
        self.assertTrue(
            list(self.state.iterdir()) == [self.state / "notes-profile.json"]
        )

    def test_expected_a_actual_b_is_rejected_without_alternative_identity_or_profile(
        self,
    ):
        with mock.patch.object(notes, "get_scope", return_value=scope("scope_b")):
            result = self.cli()
        self.assert_error(result, "binding_mismatch")
        self.assertFalse(self.state.exists())
        self.assertTrue(scope("scope_b")["subject_ref"] not in result[1])

    def test_invalid_setup_and_scope_fail_before_state_creation(self):
        for field in ("content", "token", "actor", "workspace_ref"):
            with mock.patch.object(notes, "get_scope") as get:
                self.assert_error(
                    self.cli({**self.setup_value(), field: "SENTINEL"}), "invalid_setup"
                )
                get.assert_not_called()
            self.assertFalse(self.state.exists())
        for code in ("unauthorized", "forbidden", "invalid_scope", "network_failure"):
            with mock.patch.object(notes, "get_scope", side_effect=NoteError(code)):
                self.assert_error(self.cli(), code)
            self.assertFalse(self.state.exists())
        self.assert_error(self.cli(raw=b'{"metadata":"SENTINEL"}'), "invalid_setup")

    def test_platform_and_unsafe_key_or_state_fail_before_network(self):
        with (
            mock.patch("devdiary.note_profile.os.O_NOFOLLOW", 0),
            mock.patch.object(notes, "get_scope") as get,
        ):
            self.assert_error(self.cli(), "platform_unsupported")
            get.assert_not_called()
        self.key.unlink()
        self.key.symlink_to(self.root / "missing")
        with mock.patch.object(notes, "get_scope") as get:
            self.assert_error(self.cli(), "unsafe_path")
            get.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_existing_a_cannot_be_retargeted_by_b_and_same_a_rotation_preserves_tuple(
        self,
    ):
        with mock.patch.object(notes, "get_scope", return_value=scope()):
            self.assertTrue(self.cli()[0] == 0)
        raw = (self.state / "notes-profile.json").read_bytes()
        replacement = "dn_live_" + "b" * 43
        self.key.write_bytes(replacement.encode())
        with mock.patch.object(
            notes, "get_scope", return_value=scope("scope_b")
        ) as get:
            self.assert_error(
                self.cli(self.setup_value(scope("scope_b"))), "profile_exists"
            )
            get.assert_not_called()
        self.assertTrue((self.state / "notes-profile.json").read_bytes() == raw)
        bound = require_bound_profile(self.state)
        from devdiary.note_profile import read_key_file
        from devdiary.note_validation import require_matching_scope

        with mock.patch.object(notes, "get_scope", return_value=scope("scope_b")):
            with self.assertRaises(NoteError) as rejected:
                require_matching_scope(
                    scope(),
                    notes.get_scope(bound.origin, read_key_file(bound.key_file)),
                )
        self.assertTrue(rejected.exception.code == "binding_mismatch")
        self.assertTrue(scope("scope_b")["subject_ref"] not in str(rejected.exception))
        self.assertTrue((self.state / "notes-profile.json").read_bytes() == raw)
        with mock.patch.object(notes, "get_scope", return_value=scope()) as get:
            require_matching_scope(
                scope(), notes.get_scope(bound.origin, read_key_file(bound.key_file))
            )
        self.assertTrue(get.call_args.args[1] == replacement)
        self.assertTrue(require_bound_profile(self.state) == bound)
        self.state = self.root / "separate_b"
        with mock.patch.object(notes, "get_scope", return_value=scope("scope_b")):
            self.assertTrue(self.cli(self.setup_value(scope("scope_b")))[0] == 0)
        self.assertTrue(
            require_bound_profile(self.state).subject_ref
            == scope("scope_b")["subject_ref"]
        )

    def test_sync_failure_has_no_configured_output_and_existing_file_is_preserved(self):
        with (
            mock.patch.object(notes, "get_scope", return_value=scope()),
            mock.patch(
                "devdiary.note_profile.os.fsync", side_effect=OSError("SENTINEL_SYNC")
            ),
        ):
            self.assert_error(self.cli(), "profile_failed")
        self.assertTrue(
            require_bound_profile(self.state).subject_ref == scope()["subject_ref"]
        )
        with mock.patch.object(notes, "get_scope") as get:
            self.assert_error(self.cli(), "profile_exists")
            get.assert_not_called()

    def test_unexpected_errors_are_content_free_and_cannot_inject_error_codes(self):
        altered = NoteError("invalid_scope")
        altered.code = "SENTINEL_CODE"
        for failure in (
            RuntimeError("SENTINEL_PATH_SECRET"),
            NoteError("SENTINEL_CODE"),
            altered,
        ):
            with mock.patch.object(notes, "get_scope", side_effect=failure):
                self.assert_error(self.cli(), "configure_failed")
            self.assertFalse(self.state.exists())

    def test_notes_argument_failures_are_redacted_before_input_or_effects(self):
        sentinel = "SENTINEL_ARGV"
        bad = [
            ["notes"],
            ["notes", sentinel],
            ["notes", "configure"],
            ["notes", "configure", "--state-dir"],
            ["notes", "configure", "--state-dir", str(self.state), "--token", sentinel],
            ["notes", "configure", "--state-dir", str(self.state), sentinel],
            [
                "--config",
                str(self.root / "absent"),
                "notes",
                "configure",
                "--unknown",
                sentinel,
            ],
        ]

        class Unreadable:
            @property
            def buffer(self):
                raise AssertionError("stdin accessed before argument validation")

        for arguments in bad:
            output, error = io.StringIO(), io.StringIO()
            with (
                mock.patch("sys.stdin", Unreadable()),
                mock.patch.object(notes, "get_scope") as get,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                result = main(arguments)
            self.assertTrue(
                (result, output.getvalue(), error.getvalue())
                == (2, '{"error_code":"invalid_setup"}\n', ""),
                "argument redaction required",
            )
            get.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_only_configure_is_exposed_and_non_notes_literal_arguments_keep_routing(
        self,
    ):
        for command in (
            "create",
            "update",
            "delete",
            "status",
            "retry",
            "discard",
            "import",
        ):
            self.assert_error(self.cli(arguments=["notes", command]), "invalid_setup")
        for arguments in (
            ["--config", "notes", "doctor"],
            ["run", "--actor", "notes", "--", "notes"],
        ):
            output, error = io.StringIO(), io.StringIO()
            with (
                mock.patch(
                    "devdiary.cli.load_registry",
                    side_effect=NoteError("binding_required"),
                ) as load,
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                with self.assertRaises(NoteError):
                    main(arguments)
            load.assert_called_once()
            self.assertTrue(output.getvalue() == "")


class NotesPlatformTest(unittest.TestCase):
    def test_non_posix_integration_skips_before_temporary_fixture(self):
        result = unittest.TestResult()
        with (
            mock.patch(f"{__name__}.os", SimpleNamespace(name="nt")),
            mock.patch(
                f"{__name__}.tempfile.TemporaryDirectory",
                side_effect=AssertionError("POSIX fixture entered"),
            ) as temporary,
        ):
            NotesTest(
                "test_configure_matches_metadata_and_never_uses_registry_or_runner"
            ).run(result)
        self.assertTrue(
            result.wasSuccessful() and len(result.skipped) == 1,
            "non-POSIX integration must skip before fixtures",
        )
        temporary.assert_not_called()

    def test_non_posix_configure_fails_before_input_key_network_or_state(self):
        class Unreadable:
            def read(self, size=-1):
                raise AssertionError("configure read input on unsupported platform")

        with (
            mock.patch.object(profile, "os", SimpleNamespace(name="nt")),
            mock.patch.object(notes, "check_profile_available") as check,
            mock.patch.object(notes, "read_key_file") as read,
            mock.patch.object(notes, "get_scope") as get,
            mock.patch.object(notes, "create_profile") as create,
            self.assertRaises(NoteError) as raised,
        ):
            notes.configure("/synthetic/state", Unreadable())
        self.assertTrue(raised.exception.code == "platform_unsupported")
        for effect in (check, read, get, create):
            effect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
