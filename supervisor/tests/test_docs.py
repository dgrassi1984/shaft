"""An instance's documents (PLAN-webui-chat.md §3, D0): what `shaft docs` reads and writes, and what it refuses."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import docs


class DocsTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="shaft-docs-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "instances" / "wren"
        (self.root / "notes" / "docs").mkdir(parents=True)
        self.outside = self.base / "outside"
        self.outside.mkdir()
        for p in (mock.patch.object(docs, "instance_root", lambda n: self.root),
                  mock.patch("supervisor.core.SUP_DIR", self.base / "sup"), mock.patch.object(docs, "log")):
            p.start()
            self.addCleanup(p.stop)

    def doc(self, rel: str, text: str) -> Path:
        p = self.root / "notes" / "docs" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_a_new_document_and_an_edit_keep_the_bytes_exactly(self):
        r = docs.write("wren", "plan.md", "# Plan\n\n- one — two\n", "none")
        self.assertIsNone(r["old"])
        self.assertEqual((self.root / "notes/docs/plan.md").read_bytes(), "# Plan\n\n- one — two\n".encode())
        text, sha = docs.read("wren", "plan.md")
        self.assertEqual(sha, r["sha"])
        r2 = docs.write("wren", "notes/docs/plan.md", text + "- three", sha)      # the folder may be named or not
        self.assertEqual(r2["old"], "# Plan\n\n- one — two\n")
        self.assertTrue((self.root / "notes/docs/plan.md").read_text().endswith("- three"))

    def test_an_edit_on_a_document_that_changed_since_is_refused_and_writes_nothing(self):
        self.doc("plan.md", "v1")
        _, seen = docs.read("wren", "plan.md")
        self.doc("plan.md", "v2 by the instance")
        with self.assertRaisesRegex(docs.Refused, "changed since you looked"):
            docs.write("wren", "plan.md", "mine", seen)
        self.assertEqual((self.root / "notes/docs/plan.md").read_text(), "v2 by the instance")
        with self.assertRaisesRegex(docs.Refused, "exists already"):
            docs.write("wren", "plan.md", "mine", "none")

    def test_paths_that_leave_the_folder_or_are_not_markdown_are_refused(self):
        for bad in ("../x.md", "a/../../x.md", "/etc/x.md", "x.txt", ".hidden.md", "a//b.md", "", "notes.md/..",
                    "a/b/c/d/e.md"):
            with self.assertRaises(docs.Refused, msg=bad):
                docs.write("wren", bad, "x", "none")
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_a_link_anywhere_on_the_way_is_never_followed(self):
        (self.root / "notes/docs/out").symlink_to(self.outside, target_is_directory=True)
        self.doc("real.md", "ok")
        (self.outside / "secret.md").write_text("theirs")
        (self.root / "notes/docs/link.md").symlink_to(self.outside / "secret.md")
        with self.assertRaises(docs.Refused):
            docs.write("wren", "out/x.md", "x", "none")
        with self.assertRaises(docs.Refused):
            docs.write("wren", "link.md", "x", docs.sha(b"theirs"))
        with self.assertRaises(docs.Refused):
            docs.read("wren", "out/secret.md")
        self.assertEqual((self.outside / "secret.md").read_text(), "theirs")
        self.assertEqual([d["path"] for d in docs.listing("wren")], ["real.md"])   # links are not documents

    def test_a_document_too_big_is_refused_to_write_and_to_show(self):
        with self.assertRaisesRegex(docs.Refused, "at most"):
            docs.write("wren", "big.md", "x" * (docs.MAX_BYTES + 1), "none")
        self.doc("huge.md", "x" * (docs.MAX_BYTES + 1))
        with self.assertRaisesRegex(docs.Refused, "too big"):
            docs.read("wren", "huge.md")
        self.assertEqual(docs.listing("wren")[0]["sha"], "too-big")              # listed, never hashed each refresh

    def test_the_listing_is_newest_first_and_a_snapshot_says_what_changed(self):
        a = self.doc("a.md", "a")
        self.doc("sub/b.md", "b")
        os.utime(a, (time.time() - 60, time.time() - 60))
        self.assertEqual([d["path"] for d in docs.listing("wren")], ["sub/b.md", "a.md"])
        before = docs.snapshot("wren")
        self.doc("a.md", "a2")
        self.doc("c.md", "c")
        self.assertEqual(docs.changed(before, docs.snapshot("wren")),
                         [{"path": "a.md", "created": False}, {"path": "c.md", "created": True}])
        self.assertEqual(docs.changed(before, before), [])

    def test_an_instance_without_a_docs_folder_has_none(self):
        shutil.rmtree(self.root / "notes")
        self.assertEqual(docs.listing("wren"), [])
        self.assertEqual(docs.read("wren", "x.md"), (None, "none"))

    def test_the_command_lists_shows_and_sets(self):
        f = self.base / "new.md"
        f.write_text("hello\n")
        with mock.patch("builtins.print") as out:
            self.assertEqual(docs.command("wren", "set", "hi.md", str(f), "none"), 0)
            self.assertEqual(docs.command("wren", "set", "hi.md", str(f), "none"), 1)        # exists: refused
            self.assertEqual(docs.command("wren", "set", "hi.md", str(f), None), 2)          # no sha: usage
            self.assertEqual(docs.command("wren", "show", "hi.md", None, None), 0)
            self.assertEqual(docs.command("wren", None, None, None, None), 0)
        said = "\n".join(str(c.args[0]) for c in out.call_args_list if c.args)
        self.assertIn("notes/docs/hi.md created", said)
        self.assertIn("refused: notes/docs/hi.md exists already", said)
        self.assertIn(f"sha256 {docs.sha(b'hello' + bytes([10]))}", said)


if __name__ == "__main__":
    unittest.main()
