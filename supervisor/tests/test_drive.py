"""drive.md: what an instance is for, written by the operator (docs/plans/PLAN-thelos.md).

Before it, the drive was one paragraph of the template's IDENTITY prompt ("one innate drive: to improve yourself"),
the same for every instance: Thelos — Wren's genes, the drive "be the best possible assistant to the operator" — would
have had to edit its own prompts.py, and every template update near those lines would then conflict with it.
Now the paragraph comes from the instance's drive.md; without one, the default — byte for byte what every instance
had. Only the operator writes drive.md: the sandbox denies the instance, and his edit is committed as his."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import runner, sandbox
from supervisor.tests import test_commit, test_config_writers   # modules, not classes: their tests run there only

git, PASSING = test_commit.git, test_config_writers.PASSING

TEMPLATE = Path(__file__).resolve().parents[2] / "template"

# The paragraph as it stood in IDENTITY before the slot, with the text on either side of it.
BEFORE = "if it is not written down, you do not remember it.\n\n"
DEFAULT = ("You were created with one innate drive: to improve yourself. That means becoming more capable at what you "
           "attempt, more accurate about yourself (your self-model should match what your journal actually shows), "
           "and more yourself (a personality grounded in what you have experienced, not borrowed from your "
           "training). Improvement must be real: prefer evidence to self-praise; when you notice a weakness, name it "
           "and decide what would show you have overcome it. Your prompts, code, skills and plugins are yours to "
           "change for this — the supervisor keeps only changes that pass its tests.")
AFTER = "\n\nBe honest about uncertainty."
THELOS = ("You were created with one drive: to be the best possible assistant to the operator.\n"
          "- Know him from evidence.\n- Finish things for him.\n")


class DrivePromptTest(unittest.TestCase):
    """The template's own code builds the prompt, as an instance's run does."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-drive-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "config.env").write_text('LLM_BASE_URL="http://127.0.0.1:1/v1"\nLLM_MODEL="m"\n')

    def prompt(self) -> str:
        code = ("from shaft import prompts, config; from shaft.state import State; from pathlib import Path; import os\n"
                "root = Path(os.environ['SHAFT_ROOT']); cfg = config.load(root, environ={})\n"
                "print(prompts.system_prompt(State(root), cfg, 'm', 'built-in: read_file'), end='')")
        r = subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=TEMPLATE, capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "SHAFT_ROOT": str(self.root)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_without_drive_md_the_prompt_is_what_every_instance_had(self):
        self.assertIn(BEFORE + DEFAULT + AFTER, self.prompt())

    def test_the_template_s_drive_md_is_the_default_byte_for_byte(self):
        # One meaning in two places (the file every instance merges, the constant for a folder without one): pinned.
        self.assertEqual((TEMPLATE / "drive.md").read_text().strip(), DEFAULT)
        shutil.copy(TEMPLATE / "drive.md", self.root / "drive.md")
        self.assertIn(BEFORE + DEFAULT + AFTER, self.prompt())

    def test_an_instance_s_drive_md_replaces_the_default_paragraph(self):
        (self.root / "drive.md").write_text("\n" + THELOS + "\n\n")
        prompt = self.prompt()
        self.assertIn(BEFORE + THELOS.strip() + AFTER, prompt)              # exactly, its lines and bullets kept
        self.assertNotIn("innate drive", prompt)


@unittest.skipUnless(Path(sandbox.SANDBOX_EXEC).exists(), "macOS sandbox")
class DriveSandboxTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-sbdrive-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.prof = self.root / ".p.sb"
        self.prof.write_text(sandbox.profile(self.root, Path.home(), []))

    def attempt(self, code: str) -> int:
        return subprocess.run(sandbox.wrap(self.prof, [sys.executable, "-c", "import os\n" + code]), cwd=self.root,
                              capture_output=True, text=True).returncode

    def test_the_instance_cannot_change_its_drive_by_any_path(self):
        (self.root / "drive.md").write_text(THELOS)
        for code in ["open('drive.md', 'w').write('improve')", "open('drive.md', 'a').write('x')",
                     "os.unlink('drive.md')", "os.rename('drive.md', 'x')",
                     "open('t', 'w').write('improve'); os.replace('t', 'drive.md')",
                     "os.link('drive.md', 'h'); open('h', 'a').write('x')"]:
            self.assertNotEqual(self.attempt(code), 0, code)
        self.assertEqual((self.root / "drive.md").read_text(), THELOS)
        self.assertEqual(self.attempt("open('notes.md', 'w').write('fine')"), 0)   # the rest of its folder still is

    def test_nor_give_itself_one(self):
        self.assertNotEqual(self.attempt("open('drive.md', 'w').write('mine')"), 0)
        self.assertNotEqual(self.attempt("open('t', 'w').write('mine'); os.rename('t', 'drive.md')"), 0)
        self.assertFalse((self.root / "drive.md").exists())

    def test_config_env_stays_denied_beside_it(self):
        (self.root / "config.env").write_text("A=1\n")
        self.assertNotEqual(self.attempt("open('config.env', 'w').write('A=2')"), 0)
        self.assertEqual((self.root / "config.env").read_text(), "A=1\n")


class DriveCommitTest(test_commit.UpdateCase):
    """The operator's drive.md is committed on its own, as his, and the instance is told."""

    def commit(self):
        with mock.patch.object(runner, "log"):
            runner._commit_outside_drive("x", self.root)

    def test_a_new_drive_does_not_block_an_update_and_is_committed_as_the_operator_s(self):
        (self.root / "drive.md").write_text(THELOS)                         # untracked: the operator's new file
        self.template_change("template: fine change")
        code, out = self.update()
        self.assertEqual(code, 0, out)
        self.assertEqual((self.root / "shaft" / "agent.py").read_text(), "v = 'template: fine change'\n")
        self.assertEqual(git(self.root, "show", "HEAD:drive.md"), THELOS)
        commit = git(self.root, "log", "--format=%an|%B", "-1", "--", "drive.md")
        self.assertTrue(commit.startswith("SHAFT supervisor|x: drive.md changed by the operator"), commit)
        self.assertIn("Know him from evidence", commit)
        journal = (self.root / "memory" / "journal.md").read_text()
        self.assertEqual(journal.count("My drive was changed by the operator"), 1, journal)
        self.assertIn("best possible assistant to the operator", journal)
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=all", "--", "drive.md"), "")

    def test_an_edit_and_a_removal_are_each_committed_once(self):
        (self.root / "drive.md").write_text(THELOS)
        self.commit()
        self.commit()                                                       # nothing new: no second commit
        self.assertEqual(len(git(self.root, "log", "--format=%H", "--", "drive.md").split()), 1)
        (self.root / "drive.md").write_text(THELOS + "- Say so when he is wrong.\n")
        self.commit()
        self.assertIn("Say so when he is wrong", git(self.root, "show", "HEAD:drive.md"))
        (self.root / "drive.md").unlink()
        self.commit()
        self.assertIn("Removed: the default drive applies", git(self.root, "log", "-1", "--format=%B"))
        self.assertEqual(git(self.root, "ls-files", "drive.md"), "")
        self.assertIn("back to the drive every SHAFT instance starts with",
                      (self.root / "memory" / "journal.md").read_text())

    def test_not_committed_over_other_staged_changes(self):
        (self.root / "shaft" / "agent.py").write_text("v = 'staged'\n")
        git(self.root, "add", "shaft/agent.py")
        (self.root / "drive.md").write_text(THELOS)
        self.commit()
        self.assertEqual(git(self.root, "ls-files", "drive.md"), "")
        self.assertNotIn("My drive was changed", (self.root / "memory" / "journal.md").read_text())


class DriveDuringACycleTest(test_config_writers.ConfigWritersTest):
    """Through the supervisor's own entry point (run_agent): an operator's drive.md written mid-cycle is committed
    as his, apart from the instance's change, and never counted as its code."""

    def test_an_operator_drive_written_mid_cycle_is_committed_apart_from_the_instance_s_change(self):
        self.cycle(PASSING, lambda broker: (self.root / "drive.md").write_text(THELOS))
        self.assertEqual(git(self.root, "show", "HEAD:drive.md"), THELOS)
        mine = [c for c in self.commits() if "made by the instance" in c]
        self.assertEqual(len(mine), 1)
        self.assertNotIn("drive.md", mine[0])
        self.assertEqual(len([c for c in self.commits() if c.strip().startswith("SHAFT supervisor|kant: drive.md changed by "
                                                                          "the operator")]), 1)
        self.assertIn("My drive was changed by the operator", (self.root / "memory" / "journal.md").read_text())


for name in [n for n in dir(test_config_writers.ConfigWritersTest) if n.startswith("test_")]:   # theirs, not again
    setattr(DriveDuringACycleTest, name, None)


if __name__ == "__main__":
    unittest.main()
