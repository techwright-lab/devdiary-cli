from __future__ import annotations

from typing import BinaryIO

from devdiary.note_profile import (
    BoundProfile, check_profile_available, create_profile, read_key_file, require_posix,
)
from devdiary.note_transport import get_scope
from devdiary.note_validation import read_setup, require_matching_scope


def configure(directory, stream: BinaryIO) -> dict:
    require_posix()
    setup = read_setup(stream)
    profile = BoundProfile(origin=setup["origin"], key_file=setup["key_file"], **setup["metadata"])
    check_profile_available(directory)
    key = read_key_file(profile.key_file)
    actual = get_scope(profile.origin, key)
    require_matching_scope(setup["metadata"], actual)
    create_profile(directory, profile)
    return {"profile": "configured"}
