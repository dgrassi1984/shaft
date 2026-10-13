"""self/personality.md keeps exactly one Core and one Surface block, and the core's slow pace cannot be
bypassed by a second Core block (wren, 2026-10-13: "update_personality left a trailing duplicate ## Core")."""
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


class CorePaceTest(unittest.TestCase):
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
            (sup / "core-changed-wren").write_text(datetime.now().isoformat())   # changed just now
            p.write_text(FILE + "\n## Core\n*(values and temperament — change rarely)*\n\n- new values [2026-10-12 17:24]\n")
            self.assertNotEqual(runner._core(root), before)          # the second block counts as a change
            note = runner.enforce_core("wren", root, before)
        self.assertIsNotNone(note)
        text = p.read_text()
        self.assertEqual(text.count("## Core"), 1)
        self.assertNotIn("new values", text)
        self.assertIn("- old core trait", text)
        self.assertIn("- old surface trait", text)


if __name__ == "__main__":
    unittest.main()
