from __future__ import annotations

import ctypes
import os
import sys


def scrub_inherited_variable(name: str) -> None:
    """Remove a consumed secret from Python and the inherited Linux env block."""
    if name not in os.environ:
        return
    if sys.platform.startswith("linux"):
        _zero_linux_environment_entry(name)
    os.environ.pop(name, None)


def _zero_linux_environment_entry(name: str) -> None:
    libc = ctypes.CDLL(None)
    environment = ctypes.POINTER(ctypes.c_void_p).in_dll(libc, "environ")
    prefix = name.encode("utf-8") + b"="
    index = 0
    while address := environment[index]:
        entry = ctypes.string_at(address)
        if entry.startswith(prefix):
            ctypes.memset(address, 0, len(entry))
        index += 1
