from __future__ import annotations

import unittest
from unittest import mock

from devdiary.process_tree import ProcessTree


class ProcessTreeTest(unittest.TestCase):
    def test_suspended_windows_process_is_resumed_only_through_the_job_tree(
        self,
    ) -> None:
        process = mock.Mock()
        tree = ProcessTree(
            process=process,
            job_handle=99,
            windows_suspended=True,
        )

        with mock.patch("devdiary.process_tree._resume_windows_process") as resume:
            tree.resume()

        resume.assert_called_once_with(process)
        self.assertFalse(tree.windows_suspended)


if __name__ == "__main__":
    unittest.main()
