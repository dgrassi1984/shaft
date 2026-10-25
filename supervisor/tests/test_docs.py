"""An instance's documents (docs/plans/PLAN-webui-chat.md §3, D0): what `shaft docs` reads and writes, and what it refuses."""
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
        # In KB, rounded up (it said "262145 bytes ... (at most 262144)", unreadable in the panel)
        with self.assertRaisesRegex(docs.Refused, "would be 257 KB: at most 256 KB$"):
            docs.write("wren", "big.md", "x" * (docs.MAX_BYTES + 1), "none")
        self.doc("huge.md", "x" * (docs.MAX_BYTES + 1))
        with self.assertRaisesRegex(docs.Refused, r"is 257 KB: too big to show or edit here \(at most 256 KB\)$"):
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

    def test_remove_requires_the_existing_documents_sha(self):
        p = self.doc("sub/plan.md", "one")
        with self.assertRaisesRegex(docs.Refused, "changed since you looked"):
            docs.remove("wren", "sub/plan.md", docs.sha(b"old"))
        self.assertEqual(p.read_text(), "one")
        result = docs.remove("wren", "notes/docs/sub/plan.md", docs.sha(b"one"))
        self.assertEqual(result, {"path": "sub/plan.md", "old": "one", "sha": "none"})
        self.assertFalse(p.exists())
        docs.log.assert_called_with("wren: notes/docs/sub/plan.md deleted by the operator")
        for sha in ("none", docs.sha(b"one")):
            with self.assertRaisesRegex(docs.Refused, "not there"):
                docs.remove("wren", "sub/plan.md", sha)

    def test_remove_refuses_traversal_and_links(self):
        outside = self.outside / "x.md"
        outside.write_text("outside")
        (self.root / "notes/docs/link.md").symlink_to(outside)
        (self.root / "notes/docs/sub").symlink_to(self.outside, target_is_directory=True)
        for path in ("../x", "../x.md", "/x.md", "link.md", "sub/x.md"):
            with self.assertRaises(docs.Refused, msg=path):
                docs.remove("wren", path, docs.sha(b"outside"))
        self.assertEqual(outside.read_text(), "outside")

    def test_remove_command_needs_sha_and_reports_deletion(self):
        p = self.doc("plan.md", "one")
        with mock.patch("builtins.print") as out:
            self.assertEqual(docs.command("wren", "rm", "plan.md", None, None), 2)
            self.assertEqual(docs.command("wren", "rm", "plan.md", None, "stale"), 1)
            self.assertEqual(docs.command("wren", "rm", "plan.md", None, docs.sha(b"one")), 0)
            self.assertIn("notes/docs/plan.md deleted", out.call_args.args[0])
        self.assertFalse(p.exists())


if __name__ == "__main__":
    unittest.main()


class NoLinkFollowedAfterTheCheckTest(DocsTest):
    """Codex review: the path was checked, then opened again by name — a link swapped in between was followed."""

    def test_a_folder_swapped_for_a_link_after_the_check_takes_no_write(self):
        from supervisor import core
        (self.root / "notes/docs/sub").mkdir()
        real = core.contained

        def check_then_swap(root, *parts):              # the check passes; then the instance swaps the folder
            out = real(root, *parts)
            sub = self.root / "notes/docs/sub"
            if sub.is_dir() and not sub.is_symlink():
                sub.rmdir()
                sub.symlink_to(self.outside, target_is_directory=True)
            return out
        with mock.patch.object(core, "contained", side_effect=check_then_swap):   # the write's own, last check
            with self.assertRaises(docs.Refused):
                docs.write("wren", "sub/x.md", "operator text", "none")
        self.assertEqual(list(self.outside.iterdir()), [])

    def test_a_file_swapped_for_a_link_after_the_check_is_not_read(self):
        from supervisor import core
        (self.outside / "secret.md").write_text("not the instance's")
        p = self.doc("plan.md", "mine")
        real_read = core.read_beneath

        def swap_then_read(root, parts, limit=None):
            p.unlink()
            p.symlink_to(self.outside / "secret.md")
            return real_read(root, parts, limit)
        with mock.patch.object(docs, "read_beneath", side_effect=swap_then_read):
            self.assertEqual(docs.read("wren", "plan.md"), (None, "none"))

    def test_open_beneath_refuses_links_at_every_step(self):
        from supervisor import core
        (self.root / "notes/docs/lnk").symlink_to(self.outside, target_is_directory=True)
        (self.outside / "f.md").write_text("x")
        for parts in (["notes", "docs", "lnk", "f.md"], ["notes", "..", "x"], ["notes", "docs", "lnk"]):
            with self.assertRaises(core.UnsafePath, msg=parts):
                core.open_beneath(self.root, parts, os.O_RDONLY)

    def test_remove_never_follows_a_parent_swapped_after_read(self):
        p = self.doc("sub/x.md", "inside")
        outside = self.outside / "x.md"
        outside.write_text("outside")
        real_read = docs.read

        def read_then_swap(name, path):
            result = real_read(name, path)
            p.parent.rename(p.parent.with_name("was-sub"))
            p.parent.symlink_to(self.outside, target_is_directory=True)
            return result
        with mock.patch.object(docs, "read", side_effect=read_then_swap):
            with self.assertRaises(docs.Refused):
                docs.remove("wren", "sub/x.md", docs.sha(b"inside"))
        self.assertEqual(outside.read_text(), "outside")
