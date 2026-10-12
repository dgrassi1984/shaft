"""Committing an instance's state on its branch."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from supervisor.core import commit_state


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


class CommitStateTest(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-commit-")).resolve()
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "x"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/x", str(self.root))

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)

    def test_commits_with_empty_state_dirs(self):
        # Regression: `git commit -- notes skills` failed because empty dirs are unknown to git,
        # which crashed the supervisor after a real reflection.
        for d in ("self", "memory", "notes", "skills"):
            (self.root / d).mkdir()
        (self.root / "self" / "self.md").write_text("# Self\n")
        (self.root / "memory" / "journal.md").write_text("j\n")
        self.assertTrue(commit_state("x", "reflect", [], root=self.root, project=self.project))
        files = git(self.root, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(files), ["memory/journal.md", "self/self.md"])
        self.assertIn("SHAFT/x", git(self.root, "log", "-1", "--format=%an"))
        self.assertTrue(commit_state("x", "reflect", [], root=self.root, project=self.project))  # nothing new: no-op


if __name__ == "__main__":
    unittest.main()
