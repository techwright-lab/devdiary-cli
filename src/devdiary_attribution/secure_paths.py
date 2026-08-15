from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafePathError(RuntimeError):
    """Raised when a control-plane path traverses a symbolic link."""


def reject_symlink_components(path: Path) -> None:
    current = Path(os.path.abspath(path))
    while True:
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(mode):
                raise UnsafePathError(
                    f"refusing symbolic link path component: {current}"
                )
        if current == current.parent:
            return
        current = current.parent
