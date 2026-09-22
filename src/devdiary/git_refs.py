from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
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
    worktree_reflogs: dict[str, frozenset[tuple[str, str]]]
    timed_worktree_reflogs: dict[str, frozenset[tuple[str, str, int]]] = field(
        default_factory=dict
    )


def capture(cwd: Path) -> GitSnapshot:
    root_value = _git(cwd, "rev-parse", "--show-toplevel")
    if root_value is None:
        return GitSnapshot(
            root=None,
            repository=None,
            head=None,
            reflog_entries=frozenset(),
            worktree_reflogs={},
        )
    root = Path(root_value)
    head = _git(root, "rev-parse", "HEAD")
    remote = _git(root, "remote", "get-url", "origin")
    timed = {
        str(path): frozenset(_timed_reflog_entries(path))
        for path in _worktree_paths(root)
    }
    worktree_reflogs = {
        path: frozenset((sha, action) for sha, action, _ in entries)
        for path, entries in timed.items()
    }
    return GitSnapshot(
        root=root,
        repository=_repository_name(remote),
        head=head,
        reflog_entries=worktree_reflogs[str(root)],
        worktree_reflogs=worktree_reflogs,
        timed_worktree_reflogs=timed,
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


def work_references(
    before: GitSnapshot,
    after: GitSnapshot,
    author_email: str | None = None,
) -> dict[str, list[str]]:
    repositories = [after.repository] if after.repository else []
    return {
        "repositories": repositories,
        "commits": commits_between(before, after)
        + authored_worktree_commits(before, after, author_email),
        "pull_requests": [],
        "issues": [],
        "artifacts": [],
    }


def authored_worktree_commits(
    before: GitSnapshot,
    after: GitSnapshot,
    author_email: str | None,
) -> list[str]:
    expected = (author_email or "").strip().lower()
    if after.root is None or not expected:
        return []
    created: list[str] = []
    cwd_commits = set(commits_between(before, after))
    for path, entries in after.worktree_reflogs.items():
        if Path(path) == after.root:
            continue
        previous = before.worktree_reflogs.get(path, frozenset())
        for sha, action in entries:
            if (sha, action) in previous or not _creates_commit(action):
                continue
            if sha in cwd_commits or sha in created:
                continue
            if _commit_author_email(Path(path), sha) == expected:
                created.append(sha)
    return created


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


def _timed_reflog_entries(root: Path) -> list[tuple[str, str, int]]:
    # %gD with --date=raw is the reflog event timestamp, NOT the commit's
    # author/committer timestamp (%at/%ct). Messages remain memory-only.
    output = _git(
        root,
        "reflog",
        "show",
        "HEAD",
        f"--max-count={REFLOG_LIMIT}",
        "--date=raw",
        "--format=%H%x09%gD%x09%gs",
    )
    entries: list[tuple[str, str, int]] = []
    for line in (output or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not SHA_PATTERN.fullmatch(parts[0]):
            continue
        stamp = re.fullmatch(r"HEAD@\{(-?\d+) [+-]\d{4}\}", parts[1])
        if stamp:
            entries.append((parts[0], parts[2], int(stamp[1])))
    return entries


def _worktree_paths(root: Path) -> list[Path]:
    output = _git(root, "worktree", "list", "--porcelain")
    if output is None:
        return [root]
    paths = [root]
    for line in output.splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
            if path not in paths:
                paths.append(path)
    return paths


def _commit_author_email(cwd: Path, sha: str) -> str | None:
    value = _git(cwd, "log", "-1", "--format=%ae", sha)
    return value.strip().lower() if value else None


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
