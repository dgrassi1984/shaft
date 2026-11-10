"""self/personality.md keeps exactly one Core and one Surface block, and the core's slow pace cannot be
bypassed by a second Core block (wisp, 2026-10-13: "update_personality left a trailing duplicate ## Core")."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import runner

TEMPLATE = Path(__file__).resolve().parents[2] / "template"
JOURNAL = "\n---\n## 2026-10-12 17:24 — A\n\nx\n\n---\n## 2026-10-12 18:37 — B\n\ny\n"
FILE = ("# Personality\n\n*Grown from experience.*\n\n## Core\n*(values and temperament — change rarely)*\n\n"
        "- old core trait [2026-10-12 17:24]\n\n## Surface\n*(current interests, opinions, moods)*\n\n"
        "- old surface trait [2026-10-12 18:37]\n")

# instances/shaft/self/personality.md as it was on 2026-10-13: a stray `# Personality` and caption inside
# Core, and a second, empty Surface block. All trait text is synthetic.
SHAFT_FILE = (
    '# Personality\n'
    '\n'
    '*Grown from experience. Every trait cites the journal entry that supports it.*\n'
    '\n'
    '## Core\n'
    '*(values and temperament — change rarely)*\n'
    '\n'
    '# Personality\n'
    '\n'
    '*Grown from experience. Every trait cites the journal entry that supports it.*\n'
    '\n'
    '\n'
    '- **Careful measurements.** Synthetic parser fixture trait. [2026-10-12 18:28]\n'
    '- **Small experiments.** Synthetic parser fixture trait. [2026-10-12 18:28]\n'
    '\n'
    '## Surface\n'
    '*(current interests, opinions, moods)*\n'
    '\n'
    '- **Check assumptions.** Synthetic parser fixture trait. [2026-10-12 18:28]\n'
    '- **Explore examples.** Synthetic parser fixture trait. [2026-10-12 18:28]\n'
    '- **Report errors.** Synthetic parser fixture trait. [2026-10-12 18:28]\n'
    '- **Compare results.** Synthetic parser fixture trait. [2026-10-12 18:28]\n'
    '\n'
    '## Surface\n'
    '*(current interests, opinions, moods)*\n'
    '\n'
    '(nothing yet)\n'
)


class LayerWriterTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "memory").mkdir()
        (self.root / "memory" / "journal.md").write_text(JOURNAL)
        (self.root / "self").mkdir()

    def update(self, layer, text):
        code = ("import sys; from pathlib import Path; from shaft import personality\n"
                "print(personality.update(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))")
        r = subprocess.run([sys.executable, "-E", "-s", "-c", code, str(self.root), layer, text], cwd=TEMPLATE,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return (self.root / "self" / "personality.md").read_text()

    def test_a_pasted_core_section_cannot_land_inside_surface(self):
        (self.root / "self" / "personality.md").write_text(FILE)
        out = self.update("surface", "# Personality\n\n## Core\n*(values and temperament — change rarely)*\n"
                                     "- smuggled core [2026-10-12 17:24]\n\n- new surface [2026-10-12 18:37]")
        self.assertEqual(out.count("## Core"), 1)
        self.assertEqual(out.count("## Surface"), 1)
        self.assertIn("- old core trait", out)

    def test_a_file_with_two_cores_is_healed_by_the_next_update(self):
        (self.root / "self" / "personality.md").write_text(
            FILE + "\n## Core\n*(values and temperament — change rarely)*\n\n- stray core [2026-10-12 18:37]\n")
        out = self.update("core", "- new core trait [2026-10-12 17:24]")
        self.assertEqual(out.count("## Core"), 1)
        self.assertIn("- new core trait", out)
        self.assertNotIn("stray core", out)
        self.assertIn("- old surface trait", out)

    def test_stray_headings_left_inside_an_existing_block_are_cleaned(self):
        # Regression (shaft, 2026-10-13): an older write pasted `# Personality` and its caption inside the Core
        # block and left a second, empty Surface block. Cleaning only the text being written left them there.
        (self.root / "self" / "personality.md").write_text(SHAFT_FILE)
        out = self.update("surface", "- new surface [2026-10-12 18:37]")
        self.assertEqual(out.count("## Core"), 1)
        self.assertEqual(out.count("## Surface"), 1)
        self.assertEqual(out.count("# Personality"), 1)               # the preamble's only
        self.assertEqual(out.count("*Grown from experience"), 1)
        core = out.split("## Core", 1)[1].split("## Surface", 1)[0]
        self.assertNotIn("# ", core.replace("## Core", ""))
        self.assertIn("- **Careful measurements.**", core)
        self.assertIn("- **Small experiments.**", core)
        self.assertEqual(core.count("*(values and temperament — change rarely)*"), 1)
        self.assertIn("- new surface", out)


class CorePaceTest(unittest.TestCase):
    def test_deleting_core_preserves_template_preamble(self):
        restored = self.check_restoration(
            "# Personality\n\n*Grown from experience.*\n\n## Surface\n- retained interest\n")
        self.assertTrue(restored.startswith("# Personality\n\n*Grown from experience.*\n\n## Core\n"))

    def check_restoration(self, changed):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "self").mkdir()
            p = root / "self/personality.md"
            p.write_text(changed)
            (root / "core-changed-example").write_text(datetime.now().isoformat())
            before = "- previous value\n* previous temperament"
            with mock.patch.object(runner, "SUP_DIR", root):
                note = runner.enforce_core("example", root, before)
            restored = p.read_text()
            self.assertIn("## Core\n*(values and temperament — change rarely)*\n\n" + before, restored)
            self.assertEqual(runner._core(root), before)
            self.assertEqual(restored.splitlines().count("## Core"), 1)
            self.assertTrue(restored.startswith("# Personality\n"))
            self.assertIn("## Surface\n- retained interest", restored)
            self.assertIn("supervisor restored it", note)
            return restored

    def test_deleting_core_section_is_restored_within_limit(self):
        self.check_restoration("# Personality\n\n## Surface\n- retained interest\n")

    def test_renamed_core_heading_is_restored_within_limit(self):
        for heading in ("## Values", "### Core", "## Core "):
            with self.subTest(heading=heading):
                self.check_restoration(f"# Personality\n\n{heading}\n- changed value\n\n"
                                       "## Surface\n- retained interest\n")

    def test_first_core_is_not_rate_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "self").mkdir()
            p = root / "self/personality.md"
            p.write_text(FILE)
            (root / "core-changed-example").write_text(datetime.now().isoformat())
            with mock.patch.object(runner, "SUP_DIR", root), mock.patch.object(runner, "log"):
                self.assertIsNone(runner.enforce_core("example", root, ""))
            self.assertEqual(p.read_text(), FILE)

    def test_a_core_change_hidden_in_a_second_block_is_refused_and_removed(self):
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        sup = Path(tempfile.mkdtemp(prefix="shaft-sup-"))
        self.addCleanup(shutil.rmtree, root, True)
        self.addCleanup(shutil.rmtree, sup, True)
        (root / "self").mkdir()
        p = root / "self" / "personality.md"
        p.write_text(FILE)
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(runner, "log"):
            before = runner._core(root)
            (sup / "core-changed-wisp").write_text(datetime.now().isoformat())   # changed just now
            p.write_text(FILE + "\n## Core\n*(values and temperament — change rarely)*\n\n- new values [2026-10-12 17:24]\n")
            self.assertNotEqual(runner._core(root), before)          # the second block counts as a change
            note = runner.enforce_core("wisp", root, before)
        self.assertIsNotNone(note)
        text = p.read_text()
        self.assertEqual(text.count("## Core"), 1)
        self.assertNotIn("new values", text)
        self.assertIn("- old core trait", text)
        self.assertIn("- old surface trait", text)


if __name__ == "__main__":
    unittest.main()
