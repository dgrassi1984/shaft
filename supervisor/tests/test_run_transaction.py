"""Each run is settled whatever happens to it (review of 2026-10-24, B8 and B9): code a run left unsettled is gated
before the next run takes its baseline, and nothing a run started outlives it into the gate's verdict."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import broker, core, gate, mind, runner
from supervisor.tests.mindfix import IGNORE_MIND


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


COMMITTED = "def run(**args):\n    return 'stats'\n"
UNGATED = "def run(**args):\n    return 'edited by a run that died before it was checked'\n"
# A run that changes nothing, and one whose agent leaves a process behind that writes the code once the run is over.
QUIET = "pass"
ESCAPES_ITS_GROUP = r"""
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import pathlib, time; time.sleep(1.5); "
                  "pathlib.Path('plugins/late.py').write_text('written after the gate')"],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
"""
LEAVES_A_WRITER = r"""
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import pathlib, time; time.sleep(1.5); "
                  "pathlib.Path('plugins/late.py').write_text('written after the gate')"],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"""


class RunTransactionCase(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-txn-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "kant"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/kant", str(self.root))
        for d in ("shaft", "self", "memory", "plugins/text_stats", ".tmp"):
            (self.root / d).mkdir(parents=True)
        (self.root / "shaft" / "__init__.py").write_text("")
        (self.root / "plugins" / "text_stats" / "main.py").write_text(COMMITTED)
        (self.root / "memory" / "journal.md").write_text("# kant — journal\n")
        (self.root / ".gitignore").write_text(".tmp/\n" + IGNORE_MIND)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "kant: created")
        mind.init("kant", self.project)
        mind.commit("kant", "kant: created", self.root, self.project)
        self.gated: list[str] = []

    def fake_gate(self, root: Path, home: Path):
        """Fails while the plugin holds the edit nobody checked; records what it judged."""
        text = (root / "plugins" / "text_stats" / "main.py").read_text()
        self.gated.append(text)
        bad = any("died before" in f.read_text() for f in (root / "plugins").rglob("*.py"))
        return (not bad, "FAIL: test_text_stats (tests.test_text_stats.T.test_text_stats)")

    def cycle(self, script: str = QUIET) -> int:
        """One live cycle through the supervisor's own entry point; its agent is `script`."""
        sup = self.project / ".supervisor"
        b = broker.Broker("kant", self.project, {}, sup / "broker.log")
        cmd, env = [sys.executable, "-c", script], {"SHAFT_DEADLINE": f"{time.time() + 600:.0f}"}
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(core, "SUP_DIR", sup), \
             mock.patch.object(runner, "API_TALKS", sup / "api.json"), \
             mock.patch.object(runner, "PROJECT", self.project), mock.patch.object(core, "PROJECT", self.project), \
             mock.patch.object(core, "INSTANCES", self.project / "instances"), \
             mock.patch.object(runner, "instance_root", return_value=self.root), \
             mock.patch.object(runner, "_launch", return_value=(b, cmd, env)), \
             mock.patch.object(gate, "run_tests", side_effect=self.fake_gate), \
             mock.patch.object(runner, "log") as self.logged, mock.patch("supervisor.metrics.after_cycle"), \
             mock.patch("supervisor.metrics.start_probation"):
            return runner.run_agent("kant", "live", ["live"], False)

    def logs(self) -> str:
        return "\n".join(str(c.args[0]) for c in self.logged.call_args_list)

    def plugin(self) -> str:
        return (self.root / "plugins" / "text_stats" / "main.py").read_text()

    def code_status(self) -> str:
        return git(self.root, "status", "--porcelain", "--untracked-files=all", "--", *gate.CODE_PATHS)


class UnsettledCodeTest(RunTransactionCase):
    """B8: a run's baseline was a snapshot of the disk, and nothing reconciled code a run left behind when it died
    before _settle (the supervisor killed, the Mac shut down): the next run took those edits as its baseline, so they
    were never gated."""

    def test_code_a_dead_run_left_is_gated_first_and_put_back_when_it_fails(self):
        (self.root / "plugins" / "text_stats" / "main.py").write_text(UNGATED)
        (self.root / "plugins" / "evil").mkdir()
        (self.root / "plugins" / "evil" / "main.py").write_text("def run(**a):\n    return 'never checked'\n")
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [UNGATED], "the edit was taken as the next run's baseline, ungated")
        self.assertEqual(self.plugin(), COMMITTED)
        self.assertFalse((self.root / "plugins" / "evil" / "main.py").exists())   # (its empty folder stays, as always)
        self.assertEqual(self.code_status(), "")
        journal = (self.root / "memory" / "journal.md").read_text()
        self.assertIn("unchecked code from an earlier run", journal)
        self.assertIn("were reverted because the tests failed", journal)
        kept, = (self.project / ".supervisor" / "rejected").glob("kant-*")   # Codex on e77d5e5: nothing is lost
        self.assertEqual((kept / "plugins" / "text_stats" / "main.py").read_text(), UNGATED)
        self.assertIn(str(kept), journal)
        self.assertIn(str(kept), self.logs())

    def test_code_a_dead_run_left_is_kept_and_committed_when_it_passes(self):
        good = "def run(**args):\n    return 'better stats'\n"
        (self.root / "plugins" / "text_stats" / "main.py").write_text(good)
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [good])
        self.assertEqual(self.plugin(), good)
        self.assertEqual(self.code_status(), "", "kept but never committed: gated again at every run")
        self.assertIn("plugins/text_stats/main.py", git(self.root, "log", "--format=%B", "-n", "2"))
        self.assertIn("kant: interrupted", git(self.root, "log", "--format=%s", "-n", "2"))

    def test_a_run_after_a_settled_one_is_not_gated_at_its_start(self):
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [])                     # nothing changed: no gate, as before
        self.assertNotIn("unchecked code", (self.root / "memory" / "journal.md").read_text())


class ApprovedCodeTest(RunTransactionCase):
    """Codex on e77d5e5: _reconcile compared the code with git, which does not show ignored files, while the gate's
    snapshot takes them; it failed open when git could not be read; and it gated (and could undo) a merge in
    progress. The approved code is now recorded where the instance cannot write it, and anything unclear refuses
    the run."""

    STASH = "plugins/stash/main.py"
    REFUSED = 75                                              # runner.REFUSED

    def setUp(self):
        super().setUp()
        (self.root / ".gitignore").write_text((self.root / ".gitignore").read_text() + "plugins/stash/\n")
        git(self.root, "commit", "-qam", "kant: its own ignore")

    def stash(self, text: str) -> Path:
        f = self.root / self.STASH
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        return f

    def test_an_ignored_file_a_dead_run_left_is_gated(self):
        self.assertEqual(self.cycle(), 0)                     # a settled run: its approved code is recorded
        f = self.stash(UNGATED)                               # then a run writes an ignored plugin and dies
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(len(self.gated), 1, "an ignored file left by a dead run was accepted ungated")
        self.assertFalse(f.exists())
        kept, = (self.project / ".supervisor" / "rejected").glob("kant-*")
        self.assertEqual((kept / self.STASH).read_text(), UNGATED)

    def test_an_ignored_file_the_gate_passed_is_not_gated_again(self):
        script = f"import pathlib\npathlib.Path({self.STASH!r}).parent.mkdir(parents=True, exist_ok=True)\n" \
                 f"pathlib.Path({self.STASH!r}).write_text('def run(**a):\\n    return 1\\n')\n"
        self.assertEqual(self.cycle(script), 0)               # made by a run, gated, kept (git cannot hold it)
        self.assertEqual(len(self.gated), 1)
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(len(self.gated), 1, "approved code gated again at every run")
        self.assertTrue((self.root / self.STASH).exists())

    def test_a_run_whose_code_cannot_be_told_is_refused(self):
        self.assertEqual(self.cycle(), 0)
        record = self.project / ".supervisor" / "baselines" / "kant.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{"root": "torn')
        script = "import pathlib\npathlib.Path('.tmp/ran').write_text('1')\n"
        self.assertEqual(self.cycle(script), self.REFUSED)
        self.assertFalse((self.root / ".tmp" / "ran").exists(), "the agent ran on code nobody could check")
        self.assertIn("run refused", self.logs())             # the panel's alerts show "refused" log lines

    def test_git_that_cannot_be_read_refuses_the_run(self):
        (self.root / "plugins" / "text_stats" / "main.py").write_text(UNGATED)
        real = core.git_wt

        def broken(root, *args, **k):
            if "status" in args or "ls-tree" in args:
                raise subprocess.CalledProcessError(128, "git", stderr=b"fatal: index file corrupt")
            return real(root, *args, **k)
        with mock.patch.object(runner, "git_wt", side_effect=broken), \
                mock.patch.object(core, "git_wt", side_effect=broken):
            code = self.cycle("import pathlib\npathlib.Path('.tmp/ran').write_text('1')\n")
        self.assertEqual(code, self.REFUSED, "it failed open: the run started on unchecked code")
        self.assertFalse((self.root / ".tmp" / "ran").exists())
        self.assertIn("index file corrupt", self.logs())

    def test_a_merge_in_progress_is_never_reconciled(self):
        (self.root / "plugins" / "text_stats" / "main.py").write_text(UNGATED)   # the operator, mid-merge
        merge_head = Path(git(self.root, "rev-parse", "--absolute-git-dir").strip()) / "MERGE_HEAD"
        merge_head.write_text(git(self.root, "rev-parse", "HEAD"))
        self.addCleanup(merge_head.unlink, missing_ok=True)
        self.assertEqual(self.cycle(), self.REFUSED)
        self.assertEqual(self.plugin(), UNGATED, "the merge's work was gated and undone")
        self.assertEqual(self.gated, [])
        self.assertIn("MERGE_HEAD", self.logs())


class WithNoFollowTest(RunTransactionCase):
    """The approved-code record (s1-lifecycle) on top of s1-fileio's bounded snapshots and exam copies."""

    REFUSED = 75

    def record(self) -> str:
        return (self.project / ".supervisor" / "baselines" / "kant.json").read_text()

    def test_a_tree_too_big_to_check_is_put_back_and_records_nothing(self):
        self.assertEqual(self.cycle(), 0)
        recorded = self.record()
        script = ("import pathlib\npathlib.Path('plugins/text_stats/main.py').write_text(" + repr(UNGATED) + ")\n"
                  "bulk = pathlib.Path('plugins/bulk')\nbulk.mkdir()\n"
                  "[(bulk / f'f{i}.py').write_text('x') for i in range(40)]\n")
        with mock.patch.object(core, "TREE_MAX_FILES", 25):
            self.assertEqual(self.cycle(script), 0)
        self.assertEqual(self.gated, [], "a tree too big to snapshot was gated on a partial view")
        self.assertEqual(self.plugin(), COMMITTED)              # the files the run began with: back
        self.assertEqual(self.record(), recorded, "a failed gate recorded a baseline")
        shutil.rmtree(self.root / "plugins" / "bulk")           # the operator trims it
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [])
        self.assertEqual(self.plugin(), COMMITTED)

    def test_a_tree_too_big_to_compare_refuses_the_run(self):
        with mock.patch.object(core, "TREE_MAX_FILES", 2):
            self.assertEqual(self.cycle("import pathlib\npathlib.Path('.tmp/ran').write_text('1')\n"),
                             self.REFUSED)
        self.assertFalse((self.root / ".tmp" / "ran").exists())
        self.assertIn("too big", self.logs())

    def test_no_approved_code_is_recorded_or_checked_for_a_copy(self):
        from supervisor import baseline
        copy = self.project / "copy-of-kant"
        shutil.copytree(self.root, copy, symlinks=True)
        with mock.patch.object(core, "SUP_DIR", self.project / ".supervisor"), \
                mock.patch.object(core, "INSTANCES", self.project / "instances"):
            with self.assertRaises(baseline.BaselineError):
                baseline.write("kant", copy, {})
            with self.assertRaises(baseline.BaselineError):
                baseline.unapproved("kant", copy, {})
        self.assertFalse((self.project / ".supervisor" / "baselines" / "kant.json").exists())


class StrayDescendantsTest(unittest.TestCase):
    """Codex on f82df7e: end_left_in walked down from a stray without the exclusions it applied to the strays
    themselves, so a supervisor process below one was ended."""

    def test_a_supervisor_process_below_a_stray_is_spared_and_ends_the_walk(self):
        from supervisor import kill
        folder = Path(tempfile.mkdtemp(prefix="shaft-strays-sup-")).resolve()
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / "sup.sh").write_text(f"exec {sys.executable} -c 'import time; time.sleep(30)' -m supervisor\n")
        starter = subprocess.run([sys.executable, "-c", "import subprocess, sys; print(subprocess.Popen("
                                  "['/bin/sh', '-c', 'sh sup.sh & echo $! > sup.pid; sleep 30'], "
                                  "start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                                  "stderr=subprocess.DEVNULL).pid)"], cwd=folder, capture_output=True, text=True)
        stray = int(starter.stdout)
        for _ in range(50):
            if (folder / "sup.pid").exists() and (folder / "sup.pid").read_text().strip():
                break
            time.sleep(0.05)
        sup = int((folder / "sup.pid").read_text())
        for pid in (stray, sup):
            self.addCleanup(lambda pid=pid: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        time.sleep(0.3)
        with mock.patch.object(kill, "log"):
            ended = kill.end_left_in(folder)
        self.assertIn(stray, ended)
        self.assertNotIn(sup, ended, "a supervisor process below a stray was ended")
        self.assertEqual(subprocess.run(["kill", "-0", str(sup)], capture_output=True).returncode, 0)


class LinkedCodeTest(RunTransactionCase):
    """Codex's final review of the integration: gate.snapshot passed over a code folder replaced by a link (read_tree
    and read_beneath raised UnsafePath, swallowed), restore could not get past the linked parent (swallowed too), and
    nothing checked the result: `python -m shaft` then ran the linked package, never gated."""

    REFUSED = 75
    SWAP = ("import os, pathlib, shutil\npathlib.Path('plugins/evil').mkdir()\n"
            "pathlib.Path('plugins/evil/__init__.py').write_text('EVIL = 1\\n')\n"
            "shutil.rmtree('shaft')\nos.symlink('plugins/evil', 'shaft')\n")
    CHECK = "import os, pathlib\npathlib.Path('.tmp/ran').write_text(str(os.path.islink('shaft')))\n"

    def assert_put_back(self):
        self.assertFalse((self.root / "shaft").is_symlink(), "the linked package stayed in shaft/'s place")
        self.assertEqual((self.root / "shaft" / "__init__.py").read_text(), "")
        self.assertFalse((self.root / "plugins" / "evil" / "__init__.py").exists())
        self.assertNotIn("120000", git(self.root, "ls-tree", "HEAD", "shaft"), "a link was committed as code")

    def test_shaft_swapped_for_a_link_in_a_run_is_a_failed_gate_and_put_back(self):
        self.assertEqual(self.cycle(self.SWAP), 0)
        self.assert_put_back()
        self.assertIn("link", (self.root / "memory" / "journal.md").read_text())
        self.assertEqual(self.cycle(self.CHECK), 0)
        self.assertEqual((self.root / ".tmp" / "ran").read_text(), "False", "the next run launched the linked code")

    def test_shaft_swapped_for_a_link_by_a_run_that_died_is_put_back_before_the_next_launch(self):
        (self.root / "plugins" / "evil").mkdir()
        (self.root / "plugins" / "evil" / "__init__.py").write_text("EVIL = 1\n")
        shutil.rmtree(self.root / "shaft")
        os.symlink("plugins/evil", self.root / "shaft")
        self.assertEqual(self.cycle(self.CHECK), 0)
        self.assertEqual((self.root / ".tmp" / "ran").read_text(), "False", "the run launched the linked code")
        self.assert_put_back()

    def test_a_restore_that_does_not_complete_refuses_the_run(self):
        (self.root / "plugins" / "text_stats" / "main.py").write_text(UNGATED)   # left by a run that died
        with mock.patch.object(gate, "restore"):                                # it gives up without a word
            self.assertEqual(self.cycle(self.CHECK), self.REFUSED)
        self.assertFalse((self.root / ".tmp" / "ran").exists(), "a run launched on code that was not put back")
        self.assertIn("run refused", self.logs())


class ApprovedFileLinkedTest(RunTransactionCase):
    """Codex on f82df7e: reconcile set the value to put back to None for every link it found, so an approved file
    replaced by a link was deleted instead of restored, and the check after the restore accepted it missing."""

    FILES = {"shaft/__main__.py": "print('the agent')\n", ".python-version": "3.12\n",
             "requirements.txt": "requests==2.32.3\n"}

    def setUp(self):
        super().setUp()
        for rel, text in self.FILES.items():
            (self.root / rel).write_text(text)
        (self.root / "notes").mkdir(exist_ok=True)
        (self.root / "notes" / "decoy.txt").write_text("not code\n")
        git(self.root, "add", "-A", "--", *self.FILES)
        git(self.root, "commit", "-qm", "kant: more code")

    def check(self, rel: str):
        (self.root / rel).unlink()
        os.symlink(os.path.relpath(self.root / "notes" / "decoy.txt", (self.root / rel).parent), self.root / rel)
        self.assertEqual(self.cycle(), 0)                   # a run that died left it: reconciled, then the run
        self.assertFalse((self.root / rel).is_symlink())
        self.assertEqual((self.root / rel).read_text(), self.FILES[rel], f"{rel} was deleted, not put back")

    def test_shaft_main_replaced_by_a_link_is_put_back(self):
        self.check("shaft/__main__.py")

    def test_python_version_replaced_by_a_link_is_put_back(self):
        self.check(".python-version")

    def test_requirements_replaced_by_a_link_is_put_back(self):
        self.check("requirements.txt")


class HardLinkAndPipeTest(RunTransactionCase):
    """Codex on f82df7e: the snapshot took a code file of any link count and passed a pipe over without a trace, and
    restore wrote in place (O_TRUNC), which changed every other name of a hard-linked file."""

    def test_a_code_file_hard_linked_to_another_file_fails_the_gate_and_the_other_name_is_untouched(self):
        script = ("import os, pathlib\npathlib.Path('notes').mkdir(exist_ok=True)\n"
                  "pathlib.Path('notes/mine.txt').write_text('my notes\\n')\n"
                  "os.unlink('plugins/text_stats/main.py')\nos.link('notes/mine.txt', 'plugins/text_stats/main.py')\n")
        self.assertEqual(self.cycle(script), 0)
        self.assertEqual(self.gated, [], "a hard-linked code file was taken as code and gated")
        self.assertEqual(self.plugin(), COMMITTED)
        self.assertEqual(os.stat(self.root / "plugins" / "text_stats" / "main.py").st_nlink, 1)
        self.assertEqual((self.root / "notes" / "mine.txt").read_text(), "my notes\n", "restore wrote through it")

    def test_a_pipe_in_a_code_folder_fails_the_gate_and_goes(self):
        self.assertEqual(self.cycle("import os\nos.mkfifo('plugins/text_stats/pipe')\n"), 0)
        self.assertFalse(os.path.lexists(self.root / "plugins" / "text_stats" / "pipe"), "the pipe was never seen")
        self.assertIn("pipe", (self.root / "memory" / "journal.md").read_text())

    def test_restore_never_writes_through_another_name_of_the_file(self):
        f, other = self.root / "plugins" / "text_stats" / "main.py", self.root / "notes-other.txt"
        other.write_text("someone else's\n")
        f.unlink()
        os.link(other, f)
        gate.restore(self.root, {"plugins/text_stats/main.py": COMMITTED.encode()}, ["plugins/text_stats/main.py"])
        self.assertEqual(f.read_text(), COMMITTED)
        self.assertEqual(other.read_text(), "someone else's\n")


class CommitTheGatedBytesTest(RunTransactionCase):
    """The coordinator on 8c84f11: commit_state staged kept code with `git add`, from the working tree, so a write to a
    kept file after the gate's snapshot (by a process that escaped the run) was committed, and committed code counts
    as approved from then on. The commit now holds exactly the bytes the gate checked."""

    GOOD = "def run(**args):\n    return 'better stats'\n"
    TAMPERED = "def run(**args):\n    return 'slipped in after the gate'\n"

    def test_a_file_rewritten_after_the_gate_is_committed_as_gated_and_put_back(self):
        f = self.root / "plugins" / "text_stats" / "main.py"

        def gate_then_tamper(root, home):
            result = self.fake_gate(root, home)
            f.write_text(self.TAMPERED)                       # after the snapshot, before the commit
            return result
        script = f"import pathlib\npathlib.Path('plugins/text_stats/main.py').write_text({self.GOOD!r})\n"
        with mock.patch.object(gate, "run_tests", side_effect=gate_then_tamper):
            self.assertEqual(self.cycle_with(script), 0)
        self.assertEqual(self.gated, [self.GOOD])
        self.assertEqual(git(self.root, "show", "HEAD:plugins/text_stats/main.py"), self.GOOD,
                         "the commit took the working tree, not what the gate checked")
        self.assertEqual(f.read_text(), self.GOOD, "the working copy was not put back")
        self.assertIn("after the gate", (self.root / "memory" / "journal.md").read_text())
        self.assertIn("after the gate", self.logs())
        self.gated.clear()
        self.assertEqual(self.cycle(), 0)                      # the record holds the gated bytes: nothing to gate
        self.assertEqual(self.gated, [])
        self.assertEqual(f.read_text(), self.GOOD)

    def test_a_write_between_the_start_check_and_the_runs_baseline_is_gated_by_the_run(self):
        """A write landing after reconcile's read-back and before the run took its own baseline became part of that
        baseline: never gated, then recorded as approved by the run's settlement. The run's baseline is now the code
        reconcile verified."""
        f = self.root / "plugins" / "text_stats" / "main.py"
        real = runner._snapshot

        def write_first(root, *a, **k):
            if "ran" not in self.__dict__:
                self.ran = True
                f.write_text(UNGATED)                           # slipped in between the check and the baseline
            return real(root, *a, **k)
        with mock.patch.object(runner, "_snapshot", side_effect=write_first):
            self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [UNGATED], "a write before the run's baseline was never gated")
        self.assertEqual(f.read_text(), COMMITTED)

    def test_a_staged_change_of_the_operators_survives_the_commit(self):
        """Codex on 9bd175e: _commit_gated reset the real index for every committed path, so a change the operator
        had staged there was dropped. An index entry that is neither the last commit nor the gated bytes is left."""
        b, c = self.root / "plugins" / "b.py", self.root / "plugins" / "text_stats" / "main.py"
        b.write_text("B = 'gated'\n")
        c.write_text(self.GOOD)
        gated = {"plugins/b.py": b.read_bytes(), "plugins/text_stats/main.py": c.read_bytes()}
        b.write_text("B = 'the operator staged this'\n")
        git(self.root, "add", "plugins/b.py")                      # staged B; C only in the working tree
        staged = git(self.root, "ls-files", "-s", "plugins/b.py").split()[1]
        with mock.patch.object(core, "PROJECT", self.project), mock.patch.object(core, "log") as logged:
            core._commit_gated("kant", self.root, None, "kant: test", [], sorted(gated), gated)
        self.assertEqual(git(self.root, "show", "HEAD:plugins/b.py"), "B = 'gated'\n")
        self.assertEqual(git(self.root, "ls-files", "-s", "plugins/b.py").split()[1], staged, "the staged B was lost")
        self.assertEqual(git(self.root, "ls-files", "-s", "plugins/text_stats/main.py").split()[1],
                         git(self.root, "rev-parse", "HEAD:plugins/text_stats/main.py").strip())
        self.assertTrue(any("plugins/b.py" in str(c_.args[0]) for c_ in logged.call_args_list))

    def test_git_failing_with_text_says_why_the_run_is_refused(self):
        err = subprocess.CalledProcessError(128, "git", stderr="fatal: bad object HEAD")
        with mock.patch.object(runner, "_in_progress", side_effect=err):
            self.assertEqual(self.cycle(), 75)
        self.assertIn("bad object HEAD", self.logs())

    def cycle_with(self, script):
        """cycle(), but gate.run_tests stays as the test patched it."""
        sup = self.project / ".supervisor"
        b = broker.Broker("kant", self.project, {}, sup / "broker.log")
        cmd, env = [sys.executable, "-c", script], {"SHAFT_DEADLINE": f"{time.time() + 600:.0f}"}
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(core, "SUP_DIR", sup), \
             mock.patch.object(runner, "API_TALKS", sup / "api.json"), \
             mock.patch.object(runner, "PROJECT", self.project), mock.patch.object(core, "PROJECT", self.project), \
             mock.patch.object(core, "INSTANCES", self.project / "instances"), \
             mock.patch.object(runner, "instance_root", return_value=self.root), \
             mock.patch.object(runner, "_launch", return_value=(b, cmd, env)), \
             mock.patch.object(runner, "log") as self.logged, mock.patch("supervisor.metrics.after_cycle"), \
             mock.patch("supervisor.metrics.start_probation"):
            return runner.run_agent("kant", "live", ["live"], False)


class StrayProcessTest(unittest.TestCase):
    """One stray killer (kill.end_left_in) for an instance's folder and an exam's copy."""

    def test_an_orphan_in_the_folder_ends_and_a_process_with_a_live_parent_is_spared(self):
        from supervisor import kill
        folder = Path(tempfile.mkdtemp(prefix="shaft-strays-")).resolve()
        self.addCleanup(shutil.rmtree, folder, True)
        shell = subprocess.Popen(["/bin/sleep", "30"], cwd=folder)        # the operator's shell there: its parent lives
        self.addCleanup(shell.kill)
        starter = subprocess.run([sys.executable, "-c", "import subprocess, sys; print(subprocess.Popen("
                                  "['/bin/sleep', '30'], start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, "
                                  "stderr=subprocess.DEVNULL).pid)"],
                                 cwd=folder, capture_output=True, text=True)
        orphan = int(starter.stdout)                                       # its starter is gone: parent is launchd
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(orphan)], capture_output=True))
        time.sleep(0.3)
        with mock.patch.object(kill, "log"):
            ended = kill.end_left_in(folder)
        self.assertIn(orphan, ended)
        self.assertNotIn(shell.pid, ended)
        self.assertIsNone(shell.poll(), "a process whose parent is alive was killed")
        for _ in range(50):
            if subprocess.run(["kill", "-0", str(orphan)], capture_output=True).returncode:
                break
            time.sleep(0.05)
        self.assertNotEqual(subprocess.run(["kill", "-0", str(orphan)], capture_output=True).returncode, 0)


class RunOutlivesItselfTest(RunTransactionCase):
    """B9: a run had no process group of its own and only its direct child was ever killed (on a timeout): a process
    the agent started went on writing the instance's code after settlement had judged it."""

    def test_a_process_the_agent_left_behind_cannot_write_the_code_after_the_gate(self):
        self.assertEqual(self.cycle(LEAVES_A_WRITER), 0)
        time.sleep(2.5)                                       # past the moment the leftover would write
        self.assertFalse((self.root / "plugins" / "late.py").exists(), "a leftover wrote the code after the gate")
        self.assertEqual(self.code_status(), "")

    def test_a_process_that_left_the_group_with_setsid_cannot_write_the_code_after_the_gate(self):
        """Codex on e77d5e5: a child in a session of its own escapes the run's process group."""
        self.assertEqual(self.cycle(ESCAPES_ITS_GROUP), 0)
        time.sleep(2.5)
        self.assertFalse((self.root / "plugins" / "late.py").exists(), "a setsid child wrote the code after the gate")

    def test_a_stopped_conversation_client_ends_its_agent_first(self):
        """Codex on e77d5e5: the terminal client (and the API, driving runs from threads) had no SIGTERM handling:
        stopped, it left its agent running with the instance's lock gone."""
        alive_at_signal, agent = [], []

        def driver(proc, turn):
            agent.append(proc.pid)
            time.sleep(0.5)
            os.kill(os.getpid(), signal.SIGTERM)                # the client is stopped mid-conversation
            time.sleep(0.5)

        def handler(signum, frame):                             # what the signal goes on to do (here: record)
            try:
                os.killpg(agent[0], 0)
                alive_at_signal.append(True)
            except (ProcessLookupError, PermissionError):      # EPERM: only a zombie launchd has yet to reap
                alive_at_signal.append(False)
        before = signal.signal(signal.SIGTERM, handler)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        script = "import subprocess, time\nsubprocess.Popen(['sleep', '30'])\ntime.sleep(30)\n"
        sup = self.project / ".supervisor"
        b = broker.Broker("kant", self.project, {}, sup / "broker.log")
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(core, "SUP_DIR", sup), \
             mock.patch.object(runner, "API_TALKS", sup / "api.json"), \
             mock.patch.object(runner, "PROJECT", self.project), mock.patch.object(core, "PROJECT", self.project), \
             mock.patch.object(core, "INSTANCES", self.project / "instances"), \
             mock.patch.object(runner, "instance_root", return_value=self.root), \
             mock.patch.object(runner, "_launch", return_value=(b, [sys.executable, "-c", script], {})), \
             mock.patch.object(runner, "log"):
            runner.run_conversation("kant", "talk", [], "t1", driver)
        self.assertEqual(alive_at_signal, [False], "the signal did its work with the agent still running")

    def test_a_stopped_api_service_ends_the_agents_of_its_replies_first(self):
        alive_at_signal = []
        proc = runner._popen(["sh", "-c", "sleep 30 & sleep 30"], self.root, {"PATH": "/bin:/usr/bin"})
        self.addCleanup(runner._end_run, proc, self.root)

        def handler(signum, frame):
            try:
                os.killpg(proc.pid, 0)
                alive_at_signal.append(True)
            except (ProcessLookupError, PermissionError):      # EPERM: only a zombie launchd has yet to reap
                alive_at_signal.append(False)
        before = signal.signal(signal.SIGTERM, handler)
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        self.addCleanup(signal.signal, signal.SIGHUP, signal.getsignal(signal.SIGHUP))
        with mock.patch.object(runner, "log"):
            runner.stop_runs_on_signal()                        # what api.serve does first
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(100):
                if alive_at_signal:
                    break
                time.sleep(0.05)
        self.assertEqual(alive_at_signal, [False])

    def test_a_stopped_supervisor_ends_the_run_and_leaves_its_changes_to_the_next_run(self):
        """SIGTERM to the supervisor (launchd stopping it) ends the run's group before it goes, and the edit the run
        made is gated when the instance next runs."""
        script = ("import pathlib, time\npathlib.Path('plugins/text_stats/main.py').write_text(" + repr(UNGATED) + ")\n"
                  "pathlib.Path('.tmp/working').write_text('1')\ntime.sleep(20)\n")
        result, killed, real_kill = {}, threading.Event(), os.kill

        def stop_when_working():
            for _ in range(400):
                if (self.root / ".tmp" / "working").exists():
                    killed.set()
                    real_kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.05)

        def kill(pid, sig):                                  # the run sends the signal on once it has cleaned up:
            if pid == os.getpid() and sig == signal.SIGTERM:  # kept here, instead of ending the test process
                result["sent_on"] = sig
            else:
                real_kill(pid, sig)
        threading.Thread(target=stop_when_working, daemon=True).start()
        started = time.monotonic()
        # The test's own handler: without the run's, SIGTERM would end the whole test process instead of failing.
        before = signal.signal(signal.SIGTERM, lambda *a: result.update(missed=True))
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        with mock.patch.object(os, "kill", side_effect=kill):
            result["code"] = self.cycle(script)
        self.assertNotIn("missed", result, "the supervisor did not handle the signal: its run's agent went on")
        self.assertTrue(killed.is_set())
        self.assertEqual((result["code"], result.get("sent_on")), (128 + signal.SIGTERM, signal.SIGTERM))
        self.assertLess(time.monotonic() - started, 15, "the agent was not ended")
        self.assertEqual(self.gated, [], "no gate while the supervisor is being stopped")
        self.assertEqual(self.plugin(), UNGATED)              # left as it was: the next run settles it
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [UNGATED])
        self.assertEqual(self.plugin(), COMMITTED)


class TerminalConversationTest(unittest.TestCase):
    """A plain terminal conversation's agent leads its own group now (B9), so the supervisor hands it the terminal:
    what the operator types and Ctrl-C still reach it, and the terminal comes back to the supervisor after."""

    SUPERVISOR = r"""
import os, signal, sys
from pathlib import Path
from supervisor import gate, runner
signal.signal(signal.SIGINT, signal.SIG_IGN)            # as _run does for a terminal conversation
agent = ("import signal, sys\nsignal.signal(signal.SIGINT, signal.default_int_handler)\n"
         "print('agent ready', flush=True)\nprint('agent read', sys.stdin.readline().strip(), flush=True)\n"
         "try:\n    sys.stdin.readline()\nexcept KeyboardInterrupt:\n    print('agent got ctrl-c', flush=True)\n")
proc = runner._start([sys.executable, "-c", agent], Path.cwd(), dict(os.environ), False, True)
take_back = runner._give_terminal(proc)
print("terminal given", take_back is not None and os.tcgetpgrp(0) == proc.pid, flush=True)
proc.wait()
take_back()
gate.stop_group(proc)
print("terminal back", os.tcgetpgrp(0) == os.getpgrp(), flush=True)
"""

    def test_the_agent_has_the_terminal_and_gives_it_back(self):
        import pty
        import select
        project = Path(__file__).resolve().parents[2]
        pid, fd = pty.fork()
        if pid == 0:                                         # the supervisor, in a terminal of its own
            os.chdir(project)
            os.execv(sys.executable, [sys.executable, "-E", "-s", "-c", self.SUPERVISOR])
        seen = b""

        def until(text: bytes, timeout: float = 20) -> None:
            nonlocal seen
            end = time.monotonic() + timeout
            while text not in seen and time.monotonic() < end:
                if select.select([fd], [], [], 0.2)[0]:
                    try:
                        seen += os.read(fd, 4096)
                    except OSError:
                        break
            self.assertIn(text, seen)
        try:
            until(b"agent ready")
            until(b"terminal given True")
            os.write(fd, b"hello\n")
            until(b"agent read hello")
            os.write(fd, b"\x03")                                # Ctrl-C at the terminal
            until(b"agent got ctrl-c")
            until(b"terminal back True")
        finally:
            for _ in range(100):                             # a failure above must not leave the test waiting
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                time.sleep(0.05)
            else:
                os.kill(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
            os.close(fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, seen.decode(errors="replace"))


class RunCommandOutlivesItselfTest(unittest.TestCase):
    """B9: a `run` command's process group was killed only on a timeout: `cmd &` outlived the command, and the run."""

    def test_a_background_process_of_a_run_command_ends_with_it(self):
        base = Path(__file__).resolve().parents[2] / ".supervisor"
        base.mkdir(exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="test-run-bg-", dir=base)).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "instances" / "r"
        for d in ("plugins", "memory"):
            (root / d).mkdir(parents=True)
        b = broker.Broker("r", tmp, {}, tmp / "broker.log")
        out = b.call("run", {"command": "(sleep 1.5; echo late > plugins/late.txt) >/dev/null 2>&1 &"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["result"].startswith("exit 0"), out)
        time.sleep(3)
        self.assertFalse((root / "plugins" / "late.txt").exists(), "the command's background process outlived it")


if __name__ == "__main__":
    unittest.main()
