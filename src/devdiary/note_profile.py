from __future__ import annotations

import os
import re
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

from devdiary.note_validation import (
    METADATA_LIMIT, SCOPE_FIELDS, NoteError, decode_object, encode_object,
    validate_locator, validate_origin, validate_scope,
)

PROFILE_NAME = "notes-profile.json"


@dataclass(frozen=True)
class BoundProfile:
    schema_version: int
    origin: str
    subject_ref: str
    workspace_ref: str
    capability: str
    endpoint_path: str
    scope_path: str
    key_file: str = field(repr=False)

    def __post_init__(self):
        validate_origin(self.origin)
        validate_locator(self.key_file)
        validate_scope({key: getattr(self, key) for key in SCOPE_FIELDS})
        if len(encode_object(asdict(self))) > METADATA_LIMIT:
            raise NoteError("invalid_setup")


def require_posix() -> None:
    if (os.name != "posix" or not all(getattr(os, name, None) for name in
            ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
            or not all(callable(getattr(os, name, None)) for name in
                       ("open", "stat", "fstat", "mkdir", "read", "write", "close", "getuid", "fsync"))
            or not {os.open, os.stat, os.mkdir} <= getattr(os, "supports_dir_fd", set())
            or os.stat not in getattr(os, "supports_follow_symlinks", set())):
        raise NoteError("platform_unsupported")


def _parts(path) -> list[str]:
    return validate_locator(os.fspath(path), "unsafe_path")[1:].split("/")


def _same(left, right):
    if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
        raise NoteError("unsafe_path")


def _directory_check(info, private=False, private_seen=False):
    mode = stat.S_IMODE(info.st_mode)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.getuid()}:
        raise NoteError("unsafe_path")
    if private:
        if info.st_uid != os.getuid() or mode != 0o700:
            raise NoteError("unsafe_path")
    elif mode & 0o022 and (not mode & stat.S_ISVTX or private_seen):
        raise NoteError("unsafe_path")


def _open_directory(parent, name, *, private=False, private_seen=False):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise NoteError("unsafe_path")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        info = os.fstat(fd)
        _same(before, info)
        _directory_check(info, private, private_seen)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _parent(path):
    require_posix()
    parts = _parts(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _directory_check(os.fstat(fd))
        private_seen = False
        for component in parts[:-1]:
            next_fd = _open_directory(fd, component, private_seen=private_seen)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            private_seen |= info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700
        yield fd, parts[-1]
    finally:
        os.close(fd)


def _file_check(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise NoteError("unsafe_path")


def _read_file(directory, name):
    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    _file_check(before)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        _file_check(os.fstat(fd))
        _same(before, os.fstat(fd))
        chunks, remaining = [], METADATA_LIMIT + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        _file_check(os.fstat(fd))
        _same(os.fstat(fd), os.stat(name, dir_fd=directory, follow_symlinks=False))
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_key_file(locator: str) -> str:
    try:
        with _parent(locator) as (directory, name):
            _directory_check(os.fstat(directory), private=True)
            raw = _read_file(directory, name)
        if len(raw) > METADATA_LIMIT or not re.fullmatch(rb"dn_live_[A-Za-z0-9_-]{43}\n?", raw):
            raise NoteError("invalid_key")
        return raw.removesuffix(b"\n").decode("ascii")
    except OSError:
        raise NoteError("unsafe_path") from None


def _exists(directory, name):
    try:
        os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def check_profile_available(directory) -> None:
    try:
        with _parent(directory) as (parent, name):
            try:
                state = _open_directory(parent, name, private=True)
            except FileNotFoundError:
                return
            try:
                if _exists(state, PROFILE_NAME):
                    raise NoteError("profile_exists")
            finally:
                os.close(state)
    except OSError:
        raise NoteError("profile_failed") from None


def create_profile(directory, profile: BoundProfile) -> None:
    raw = encode_object(asdict(profile))
    try:
        with _parent(directory) as (parent, name):
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            state = _open_directory(parent, name, private=True)
            try:
                try:
                    fd = os.open(PROFILE_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=state)
                except FileExistsError:
                    raise NoteError("profile_exists") from None
                try:
                    _file_check(os.fstat(fd))
                    while raw:
                        written = os.write(fd, raw)
                        if written <= 0:
                            raise OSError
                        raw = raw[written:]
                    _file_check(os.fstat(fd))
                    _same(os.fstat(fd), os.stat(PROFILE_NAME, dir_fd=state, follow_symlinks=False))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                _directory_check(os.fstat(state), private=True)
                _same(os.fstat(state), os.stat(name, dir_fd=parent, follow_symlinks=False))
                os.fsync(state)
                # Another initializer may have created the leaf before our mkdir.
                os.fsync(parent)
            finally:
                os.close(state)
    except OSError:
        raise NoteError("profile_failed") from None


def require_bound_profile(directory) -> BoundProfile:
    try:
        with _parent(directory) as (parent, name):
            state = _open_directory(parent, name, private=True)
            try:
                raw = _read_file(state, PROFILE_NAME)
            finally:
                os.close(state)
        value = decode_object(raw, METADATA_LIMIT, "binding_required")
        if set(value) != SCOPE_FIELDS | {"origin", "key_file"}:
            raise NoteError("binding_required")
        return BoundProfile(**value)
    except NoteError as error:
        if error.code in {"unsafe_path", "platform_unsupported"}:
            raise
        raise NoteError("binding_required") from None
    except (OSError, TypeError):
        raise NoteError("binding_required") from None
