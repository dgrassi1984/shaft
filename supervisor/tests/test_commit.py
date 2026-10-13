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


class IgnoredFileTest(unittest.TestCase):
    def test_an_ignored_file_among_the_kept_code_does_not_sink_the_commit(self):
        # Regression (2026-10-13): shaft's delegated system plugin passed the tests, but the kept files included
        # plugins/.DS_Store (Finder's, ignored by git): `git add` refused it and the whole commit failed, leaving
        # the plugin staged and every later template update blocked.
        project = Path(tempfile.mkdtemp(prefix="shaft-commit-")).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        git(project, "init", "-q")
        git(project, "commit", "-q", "--allow-empty", "-m", "main")
        root = project / "instances" / "x"
        git(project, "worktree", "add", "-q", "--orphan", "-b", "instance/x", str(root))
        (root / ".gitignore").write_text(".DS_Store\n")
        (root / "plugins" / "system").mkdir(parents=True)
        (root / "plugins" / "system" / "main.py").write_text("def run(): return 'ok'\n")
        (root / "plugins" / ".DS_Store").write_bytes(b"finder")
        kept = ["plugins/.DS_Store", "plugins/system/main.py"]
        self.assertTrue(commit_state("x", "talk", kept, root=root, project=project))
        self.assertIn("plugins/system/main.py", git(root, "show", "--name-only", "--format=", "HEAD"))
        self.assertNotIn(".DS_Store", git(root, "show", "--name-only", "--format=", "HEAD"))

    def test_the_gate_does_not_count_finder_files_as_code(self):
        from supervisor import gate
        root = Path(tempfile.mkdtemp(prefix="shaft-snap-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "plugins").mkdir()
        (root / "plugins" / ".DS_Store").write_bytes(b"finder")
        self.assertEqual(gate.snapshot(root), {})


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


class UpdateCase(unittest.TestCase):
    """An instance worktree x branched from a template, with `shaft update` run against them."""

    def setUp(self):
        from unittest import mock
        from supervisor import __main__ as cli, core
        self.cli = cli
        self.project = Path(tempfile.mkdtemp(prefix="shaft-upd-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.tpl = self.project / "template"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "template", str(self.tpl))
        (self.tpl / "shaft").mkdir()
        (self.tpl / "shaft" / "agent.py").write_text("v = 1\n")
        git(self.tpl, "add", "-A")
        git(self.tpl, "commit", "-q", "-m", "template: first")
        self.root = self.project / "instances" / "x"
        git(self.project, "worktree", "add", "-q", "-b", "instance/x", str(self.root), "template")
        (self.root / "memory").mkdir()
        (self.root / "memory" / "journal.md").write_text("# x — journal\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "x: created")
        for target, value in ((core, "PROJECT"), (core, "INSTANCES")):
            p = mock.patch.object(target, value, self.project if value == "PROJECT" else self.project / "instances")
            p.start()
            self.addCleanup(p.stop)
        self.gate = mock.patch.object(cli.gate, "run_tests", return_value=(True, "ok"))
        self.gate.start()
        self.addCleanup(self.gate.stop)

    def template_change(self, *subjects):
        for i, s in enumerate(subjects):
            (self.tpl / "shaft" / "agent.py").write_text(f"v = {s!r}\n")
            git(self.tpl, "commit", "-q", "-am", s)

    def notes(self):
        return (self.root / "memory" / "journal.md").read_text().count("My code was updated")

    def update(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.cli.cmd_update(["x"])
        return code, out.getvalue()


class UpdateNoteTest(UpdateCase):
    """Regression (2026-10-13): `shaft update` merged template changes without a word to the instance, so it
    never learned its own code had changed — shaft's self-model still said "no embeddings, no vector DB" and
    "wakes me every 2 hours" after hybrid search was merged and its wake interval was 10 minutes."""

    def test_an_update_leaves_one_note_naming_the_merged_commits(self):
        self.template_change("template: hybrid embedding search", "template: wake every 10 minutes")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        journal = (self.root / "memory" / "journal.md").read_text()
        self.assertEqual(self.notes(), 1, journal)
        self.assertIn("template: hybrid embedding search", journal)
        self.assertIn("template: wake every 10 minutes", journal)
        self.assertNotIn("template: first", journal)                  # already in the instance before
        self.assertNotIn("merge template", journal)                   # the merge commit itself is not news
        self.assertEqual(git(self.root, "status", "--porcelain").strip(), "")   # the note is committed

    def test_no_note_when_already_up_to_date(self):
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertIn("already up to date", out)
        self.assertEqual(self.notes(), 0)

    def test_no_note_when_the_merge_fails_the_gate(self):
        self.template_change("template: broken change")
        head = git(self.root, "rev-parse", "HEAD")
        self.gate.stop()
        from unittest import mock
        with mock.patch.object(self.cli.gate, "run_tests", return_value=(False, "boom")):
            code, out = self.update()
        self.gate.start()
        self.assertEqual(code, 1, out)
        self.assertIn("rolled back", out)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual(self.notes(), 0)


class UpdateKeepsUncommittedWorkTest(UpdateCase):
    """Regression (2026-10-13): a merged template that failed the gate was rolled back with `reset --hard`,
    which also threw away every uncommitted edit in the instance's worktree — wren and kant both had an
    uncommitted config.env while updates ran (` M config.env`, WAKE_INTERVAL_MINUTES 120 -> 10)."""

    def setUp(self):
        super().setUp()
        (self.root / "config.env").write_text("WAKE_INTERVAL_MINUTES=120\n")
        git(self.root, "add", "config.env")
        git(self.root, "commit", "-q", "-m", "x: config")
        (self.root / "config.env").write_text("WAKE_INTERVAL_MINUTES=10\n")      # the operator's uncommitted edit

    def failing_update(self, side_effect=None):
        from unittest import mock
        self.gate.stop()
        with mock.patch.object(self.cli.gate, "run_tests", return_value=(False, "boom"),
                               side_effect=side_effect) as run:
            code, out = self.update()
        self.gate.start()
        return code, out, run

    def test_an_uncommitted_edit_survives_a_template_change_that_fails_the_gate(self):
        self.template_change("template: broken change")
        head = git(self.root, "rev-parse", "HEAD")
        code, out, run = self.failing_update()
        self.assertEqual((self.root / "config.env").read_text(), "WAKE_INTERVAL_MINUTES=10\n", out)
        self.assertEqual(code, 1, out)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 1\n")    # the merge never landed

    def test_update_refuses_and_names_the_uncommitted_files(self):
        self.template_change("template: fine change")
        self.gate.stop()
        from unittest import mock
        with mock.patch.object(self.cli.gate, "run_tests", return_value=(True, "ok")) as run:
            code, out = self.update()
        self.gate.start()
        self.assertEqual(code, 1, out)
        self.assertIn("config.env", out)
        self.assertIn("uncommitted", out)
        run.assert_not_called()                                    # nothing merged, nothing tested
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 1\n")
        self.assertEqual((self.root / "config.env").read_text(), "WAKE_INTERVAL_MINUTES=10\n")

    def test_untracked_files_do_not_block_an_update(self):
        # An untracked file can be lost by neither the merge (git refuses to overwrite it) nor the rollback.
        git(self.root, "checkout", "--", "config.env")
        (self.root / "scratch.txt").write_text("mine\n")
        self.template_change("template: fine change")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 'template: fine change'\n")
        self.assertEqual((self.root / "scratch.txt").read_text(), "mine\n")

    def test_rollback_keeps_a_file_written_while_the_gate_ran(self):
        # The rollback must undo only the merge: an edit made to a merged file during the gate is kept (git
        # refuses `reset --keep`) and the operator is told, rather than silently discarded by `reset --hard`.
        git(self.root, "checkout", "--", "config.env")
        self.template_change("template: broken change")

        def edit_during_gate(*_):
            (self.root / "shaft" / "agent.py").write_text("v = 'edited during the gate'\n")
            return False, "boom"
        code, out, _ = self.failing_update(edit_during_gate)
        self.assertEqual(code, 1, out)
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 'edited during the gate'\n")
        self.assertIn("could not roll back", out)
