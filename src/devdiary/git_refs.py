from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

SHA_PATTERN = re.compile(r"\A[0-9a-f]{40}\Z")
GIT_EXECUTABLE = shutil.which("git")
REFLOG_LIMIT = 10_000
COMMIT_CREATION_ACTIONS = ("commit", "cherry-pick", "revert")
REBASE_CREATION_ACTIONS = (
    "rebase (pick)",
    "rebase (reword)",
    "rebase (edit)",
    "rebase (squash)",
    "rebase (fixup)",
    "rebase (continue)",
)


@dataclass(frozen=True)
class GitSnapshot:
    root: Path | None
    repository: str | None
    head: str | None
    reflog_entries: frozenset[tuple[str, str]]


def capture(cwd: Path) -> GitSnapshot:
    root_value = _git(cwd, "rev-parse", "--show-toplevel")
    if root_value is None:
        return GitSnapshot(
            root=None,
            repository=None,
            head=None,
            reflog_entries=frozenset(),
        )
    root = Path(root_value)
    head = _git(root, "rev-parse", "HEAD")
    remote = _git(root, "remote", "get-url", "origin")
    return GitSnapshot(
        root=root,
        repository=_repository_name(remote),
        head=head,
        reflog_entries=frozenset(_reflog_entries(root)),
    )


def commits_between(before: GitSnapshot, after: GitSnapshot) -> list[str]:
    if after.root is None:
        return []
    previous_entries = (
        before.reflog_entries if before.root == after.root else frozenset()
    )
    created: list[str] = []
    for entry in _reflog_entries(after.root):
        sha, action = entry
        if entry in previous_entries or not _creates_commit(action):
            continue
        if SHA_PATTERN.fullmatch(sha) and sha not in created:
            created.append(sha)
    return list(reversed(created))


def _creates_commit(action: str) -> bool:
    normalized = action.lower()
    if normalized.startswith(COMMIT_CREATION_ACTIONS):
        return True
    if normalized.startswith("merge"):
        return "fast-forward" not in normalized
    return normalized.startswith(REBASE_CREATION_ACTIONS)


def work_references(before: GitSnapshot, after: GitSnapshot) -> dict[str, list[str]]:
    repositories = [after.repository] if after.repository else []
    return {
        "repositories": repositories,
        "commits": commits_between(before, after),
        "pull_requests": [],
        "issues": [],
        "artifacts": [],
    }


def _reflog_entries(root: Path) -> list[tuple[str, str]]:
    output = _git(
        root,
        "reflog",
        "show",
        "HEAD",
        f"--max-count={REFLOG_LIMIT}",
        "--format=%H%x09%gs",
    )
    if output is None:
        return []
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        sha, separator, action = line.partition("\t")
        if separator and SHA_PATTERN.fullmatch(sha):
            entries.append((sha, action))
    return entries


def _git(cwd: Path, *arguments: str) -> str | None:
    if GIT_EXECUTABLE is None:
        return None
    try:
        result = subprocess.run(
            [GIT_EXECUTABLE, *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _repository_name(remote: str | None) -> str | None:
    if not remote:
        return None
    path = remote
    if "://" in remote:
        path = urlparse(remote).path
    elif ":" in remote and "@" in remote.split(":", 1)[0]:
        path = remote.split(":", 1)[1]
    parts = [part for part in path.removesuffix(".git").strip("/").split("/") if part]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:])
