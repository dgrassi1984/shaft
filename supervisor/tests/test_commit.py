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


class ConfigMergeTest(unittest.TestCase):
    def test_instance_settings_survive_a_conflicting_template_change(self):
        # Regression: wren (on Haiku) conflicted with every template change near its model lines.
        from unittest import mock
        from supervisor import __main__ as cli, core
        project = Path(tempfile.mkdtemp(prefix="shaft-cfg-")).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        git(project, "init", "-q")
        git(project, "commit", "-q", "--allow-empty", "-m", "main")
        tpl = project / "template"
        git(project, "worktree", "add", "-q", "--orphan", "-b", "template", str(tpl))
        (tpl / "config.env").write_text('LLM_BASE_URL="local"\nLLM_MODEL="bonsai"\nLLM_API_KEY=""\n')
        git(tpl, "add", "-A")
        git(tpl, "commit", "-q", "-m", "t1")
        root = project / "instances" / "w"
        git(project, "worktree", "add", "-q", "-b", "instance/w", str(root), "template")
        (root / "config.env").write_text('LLM_BASE_URL="broker"\nLLM_MODEL="claude-haiku-5-5"\nLLM_API_KEY=""\n')
        git(root, "commit", "-q", "-am", "w uses haiku")
        (tpl / "config.env").write_text('LLM_BASE_URL="local:8780"\nLLM_MODEL="bonsai-mlx"\nLLM_API_KEY=""\n'
                                        'LLM_PRESENCE_PENALTY=15\n')
        git(tpl, "commit", "-q", "-am", "t2")
        r = subprocess.run(["git", "-C", str(root), "merge", "--no-edit", "template"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)                                   # it really conflicts
        wt = lambda root_, *a, check=True, project=None: core.git_wt(root_, *a, check=check, project=project_)  # noqa: E731
        project_ = project
        with mock.patch.object(cli, "git_wt", wt):
            self.assertTrue(cli._resolve_config_conflict(root))
        merged = (root / "config.env").read_text()
        self.assertIn('LLM_BASE_URL="broker"', merged)
        self.assertIn('LLM_MODEL="claude-haiku-5-5"', merged)
        self.assertIn("LLM_PRESENCE_PENALTY=15", merged)
        self.assertNotIn("<<<<<<<", merged)


if __name__ == "__main__":
    unittest.main()
