from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from devdiary import git_refs


@unittest.skipIf(git_refs.GIT_EXECUTABLE is None, "git is not installed")
class GitRefsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_captures_the_first_commit_in_an_empty_repository(self) -> None:
        self._initialize_repository()
        before = git_refs.capture(self.root)

        sha = self._commit("first")
        after = git_refs.capture(self.root)

        self.assertEqual([sha], git_refs.commits_between(before, after))

    def test_switching_to_a_preexisting_branch_is_not_reported_as_new_work(
        self,
    ) -> None:
        self._initialize_repository()
        self._commit("main")
        self._git("checkout", "-b", "feature")
        self._commit("feature")
        self._git("checkout", "main")
        before = git_refs.capture(self.root)

        self._git("checkout", "feature")
        after = git_refs.capture(self.root)

        self.assertEqual([], git_refs.commits_between(before, after))

    def test_fast_forward_merge_does_not_claim_preexisting_commits(self) -> None:
        self._initialize_repository()
        self._commit("main")
        self._git("checkout", "-b", "feature")
        self._commit("feature")
        self._git("checkout", "main")
        before = git_refs.capture(self.root)

        self._git("merge", "--ff-only", "feature")
        after = git_refs.capture(self.root)

        self.assertEqual([], git_refs.commits_between(before, after))

    def test_non_fast_forward_merge_reports_only_the_new_merge_commit(self) -> None:
        self._initialize_repository()
        self._commit("base")
        self._git("checkout", "-b", "feature")
        self._commit("feature")
        self._git("checkout", "main")
        self._commit("main")
        before = git_refs.capture(self.root)

        self._git("merge", "--no-ff", "feature", "-m", "merge feature")
        merge_sha = self._git("rev-parse", "HEAD").stdout.strip()
        after = git_refs.capture(self.root)

        self.assertEqual([merge_sha], git_refs.commits_between(before, after))

    def test_rebase_reports_rewritten_commits_not_start_or_finish_entries(self) -> None:
        self._initialize_repository()
        self._commit("base")
        self._git("checkout", "-b", "feature")
        self._commit("feature")
        self._git("checkout", "main")
        self._commit("main")
        self._git("checkout", "feature")
        before = git_refs.capture(self.root)

        self._git("rebase", "main")
        rewritten_sha = self._git("rev-parse", "HEAD").stdout.strip()
        after = git_refs.capture(self.root)

        self.assertEqual([rewritten_sha], git_refs.commits_between(before, after))

    def test_commit_in_a_sibling_worktree_is_not_reported_as_this_run(self) -> None:
        self._initialize_repository()
        self._commit("main")
        sibling = self.root.parent / f"{self.root.name}-sibling"
        self._git("worktree", "add", "-b", "sibling", str(sibling))
        try:
            before = git_refs.capture(self.root)
            (sibling / "sibling.txt").write_text("sibling", encoding="utf-8")
            self._git_at(sibling, "add", "sibling.txt")
            self._git_at(sibling, "commit", "-m", "sibling")
            after = git_refs.capture(self.root)

            self.assertEqual([], git_refs.commits_between(before, after))
        finally:
            self._git("worktree", "remove", "--force", str(sibling))

    def test_captures_a_commit_even_when_the_command_resets_head(self) -> None:
        self._initialize_repository()
        original = self._commit("original")
        before = git_refs.capture(self.root)

        created = self._commit("temporary")
        self._git("reset", "--hard", original)
        after = git_refs.capture(self.root)

        self.assertEqual([created], git_refs.commits_between(before, after))

    def test_captures_work_when_the_command_initializes_the_repository(self) -> None:
        before = git_refs.capture(self.root)
        self._initialize_repository()

        sha = self._commit("initialized")
        after = git_refs.capture(self.root)

        self.assertEqual([sha], git_refs.commits_between(before, after))

    def _initialize_repository(self) -> None:
        self._git("init", "-b", "main")
        self._git("config", "user.name", "Test Runner")
        self._git("config", "user.email", "test@example.com")

    def _commit(self, name: str) -> str:
        (self.root / f"{name}.txt").write_text(name, encoding="utf-8")
        self._git("add", f"{name}.txt")
        self._git("commit", "-m", name)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self._git_at(self.root, *arguments)

    def _git_at(self, cwd: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        assert git_refs.GIT_EXECUTABLE is not None
        return subprocess.run(
            [git_refs.GIT_EXECUTABLE, *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
