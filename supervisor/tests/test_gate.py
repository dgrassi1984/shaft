"""The self-modification gate restores failing code changes byte-for-byte."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from supervisor import gate


class GateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-gate-"))
        (self.root / "shaft").mkdir()
        (self.root / "shaft" / "a.py").write_text("A = 1\n")
        (self.root / "config.env").write_text("X=1\n")
        (self.root / "memory").mkdir()
        (self.root / "memory" / "journal.md").write_text("j\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_detects_edit_add_delete_and_ignores_state(self):
        before = gate.snapshot(self.root)
        (self.root / "shaft" / "a.py").write_text("A = 2\n")
        (self.root / "shaft" / "new.py").write_text("B = 1\n")
        (self.root / "config.env").unlink()
        (self.root / "memory" / "journal.md").write_text("changed\n")
        (self.root / "shaft" / "__pycache__").mkdir()
        (self.root / "shaft" / "__pycache__" / "a.pyc").write_bytes(b"x")
        self.assertEqual(gate.changed(before, gate.snapshot(self.root)),
                         ["config.env", "shaft/a.py", "shaft/new.py"])

    def test_restore_undoes_everything(self):
        before = gate.snapshot(self.root)
        (self.root / "shaft" / "a.py").write_text("broken(\n")
        (self.root / "shaft" / "new.py").write_text("B = 1\n")
        (self.root / "config.env").unlink()
        touched = gate.changed(before, gate.snapshot(self.root))
        gate.restore(self.root, before, touched)
        self.assertEqual(gate.snapshot(self.root), before)
        self.assertFalse((self.root / "shaft" / "new.py").exists())


if __name__ == "__main__":
    unittest.main()
