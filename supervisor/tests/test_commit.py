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


class HistoryToolTest(unittest.TestCase):
    """`git` cannot run in an instance's sandbox (its repository lives outside its folder); shaft retried
    `git log` for 15 minutes. The broker's history tool shows its own branch — and only that."""

    def setUp(self):
        from supervisor.broker import Broker
        self.project = Path(tempfile.mkdtemp(prefix="shaft-hist-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main: supervisor secret")
        self.main = git(self.project, "rev-parse", "--short", "HEAD").strip()
        for name in ("x", "y"):
            root = self.project / "instances" / name
            git(self.project, "worktree", "add", "-q", "--orphan", "-b", f"instance/{name}", str(root))
            (root / "self").mkdir()
            (root / "self" / "self.md").write_text(f"I am {name}\n")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", f"{name}: reflect")
        self.other = git(self.project / "instances" / "y", "rev-parse", "--short", "HEAD").strip()
        self.broker = Broker("x", self.project, {}, self.project / "broker.log")

    def test_own_log_and_changes(self):
        log = self.broker.call("history", {"path": "self/self.md"})
        self.assertTrue(log["ok"], log)
        self.assertIn("x: reflect", log["result"])
        commit = log["result"].split()[0]
        self.assertIn("+I am x", self.broker.call("history", {"commit": commit})["result"])

    def test_nothing_outside_its_branch(self):
        for args in ({"commit": self.main}, {"commit": self.other}, {"path": "../y/self/self.md"},
                     {"commit": "--output=/tmp/pwn"}):
            with self.subTest(args=args):
                self.assertFalse(self.broker.call("history", args)["ok"])


class RecordToolTest(unittest.TestCase):
    """shaft asked for the supervisor's record of what it did (to check its own claims) — its own only."""

    def test_own_record_only(self):
        import json
        from unittest import mock
        from supervisor import core
        from supervisor.broker import Broker
        sup = Path(tempfile.mkdtemp(prefix="shaft-rec-"))
        self.addCleanup(shutil.rmtree, sup, True)
        (sup / "judgments").mkdir()
        (sup / "judgments" / "x.jsonl").write_text(json.dumps(
            {"episode_ts": "2026-10-12T20:34:45", "goal": "g", "claimed": "yes", "confidence": 75,
             "criterion_met": "partial", "reasoning": "only half done"}) + "\n")
        (sup / "judgments" / "y.jsonl").write_text(json.dumps({"reasoning": "Y SECRET"}) + "\n")
        (sup / "supervisor.log").write_text("[t] x: start live\n[t] y: start live Y SECRET\n[t] tick: y busy\n")
        log = sup / "broker.log"
        log.write_text("[t] x run {} -> ok\n[t] y fetch_page {Y SECRET} -> ok\n")
        with mock.patch.object(core, "SUP_DIR", sup):
            b = Broker("x", sup, {}, log)
            out = {k: b.call("record", {"kind": k})["result"] for k in ("judgments", "runs", "tools")}
        self.assertIn("only half done", out["judgments"])
        self.assertIn("x: start live", out["runs"])
        self.assertIn("x run", out["tools"])
        self.assertNotIn("SECRET", "".join(out.values()))
        self.assertNotIn("tick: y", out["runs"])
