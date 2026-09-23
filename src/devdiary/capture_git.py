"""Conservative author-matched collection without retaining reflog messages."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime
from pathlib import Path

from devdiary import git_refs
from devdiary.capture_validation import WORK_KINDS


def _entry(sha: str, action: str) -> str:
    return hashlib.sha256((sha + "\0" + action).encode()).hexdigest()


def snapshot(cwd: Path) -> dict:
    current = git_refs.capture(cwd)
    return {
        "root": str(current.root) if current.root else None,
        "timed_entries": {
            path: sorted((_entry(sha, action), stamp) for sha, action, stamp in entries)
            for path, entries in current.timed_worktree_reflogs.items()
        },
        "entries": {
            path: sorted(_entry(sha, action) for sha, action in entries)
            for path, entries in current.worktree_reflogs.items()
        },
    }


def collect(
    cwd: Path,
    before: dict,
    email: str | None,
    *,
    delayed: bool,
    started_at: str | None = None,
    ended_at: str | None = None,
) -> tuple[dict, str | None]:
    work: dict[str, list[str]] = {k: [] for k in sorted(WORK_KINDS)}
    current = git_refs.capture(cwd)
    if current.repository:
        work["repositories"] = [current.repository]
    if not email or current.root is None or str(current.root) != before["root"]:
        return work, "unavailable_author_identity_or_repository"
    if delayed and (not started_at or not ended_at or "timed_entries" not in before):
        return work, "unavailable_authoritative_end_time"
    # Whole boundary seconds are ambiguous even with fractional caller times.
    lower = (
        math.floor(datetime.fromisoformat(started_at).timestamp()) if started_at else 0
    )
    upper = math.floor(datetime.fromisoformat(ended_at).timestamp()) if ended_at else 0
    for path, entries in current.worktree_reflogs.items():
        # Worktrees first seen at finish have no trustworthy begin boundary.
        if path not in before["entries"]:
            continue
        previous = set(before["entries"][path])
        qualified = set()
        if delayed:
            if path not in before["timed_entries"]:
                continue
            previous_timed = {tuple(entry) for entry in before["timed_entries"][path]}
            qualified = {
                (sha, action)
                for sha, action, stamp in current.timed_worktree_reflogs.get(path, ())
                if lower < stamp < upper
                and (_entry(sha, action), stamp) not in previous_timed
            }
        for sha, action in sorted(entries):
            if _entry(sha, action) in previous or not git_refs._creates_commit(action):
                continue
            if delayed and (sha, action) not in qualified:
                continue
            if git_refs._commit_author_email(Path(path), sha) == email.strip().lower():
                work["commits"].append(sha)
    work["commits"] = list(dict.fromkeys(work["commits"]))
    return work, "timestamp_qualified_terminal_second_excluded" if delayed else None
