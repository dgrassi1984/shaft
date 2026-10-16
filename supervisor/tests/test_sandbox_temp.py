"""The operator's per-user folder (/private/var/folders/<xx>/<id>/: every app's temp files and caches), /private/tmp,
/Users/Shared and /Volumes are closed to instances, while Apple's `sips` still works in their sandbox — so an instance can turn a PDF page into an image for
its own PDF tools (Daniele, 2026-10-16). Run under the real sandbox-exec."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from supervisor import sandbox
from supervisor.core import HOME

SIPS = Path("/usr/bin/sips")


def tiny_pdf() -> bytes:
    """A one-page PDF with a line of text, written by hand."""
    content = b"BT /F1 24 Tf 72 700 Td (INVOICE 2291) Tj ET"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)


@unittest.skipUnless(sys.platform == "darwin" and SIPS.exists() and Path(sandbox.SANDBOX_EXEC).exists()
                     and sandbox.user_folder(), "macOS sandbox-exec, sips and a per-user folder")
class UserFolderTest(unittest.TestCase):
    def setUp(self):
        # The instance's own folder in the home, as a real one is (instances/<name>/), not in the temp folder.
        self.root = Path(tempfile.mkdtemp(prefix="shaft-sbx-", dir=HOME / "Library" / "Caches")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".tmp").mkdir()
        (self.root / "notes").mkdir()
        (self.root / "notes" / "doc.pdf").write_bytes(tiny_pdf())
        temp = sandbox.user_folder() / "T"
        self.canary = temp / f"shaft-canary-{uuid.uuid4().hex}.txt"
        self.canary.write_text("the operator's temp file")
        self.lookalike = temp / f"{str(uuid.uuid4()).upper()}.other"          # a UUID name with more after it
        self.lookalike.write_text("another app's file")
        for f in (self.canary, self.lookalike):
            self.addCleanup(f.unlink, True)

    def run_in(self, profile: str, cmd: str) -> subprocess.CompletedProcess:
        prof = self.root / ".tmp" / "p.sb"
        prof.write_text(profile)
        return subprocess.run([sandbox.SANDBOX_EXEC, "-f", str(prof), "/bin/sh", "-c", cmd], cwd=self.root,
                              capture_output=True, text=True, timeout=120,
                              env={"PATH": "/usr/bin:/bin", "TMPDIR": str(self.root / ".tmp"),
                                   "HOME": str(self.root / ".tmp")})

    def profiles(self):
        return {"run": sandbox.run_profile(self.root, HOME), "agent": sandbox.profile(self.root, HOME, [])}

    def test_sips_renders_a_pdf_page_in_both_sandboxes(self):
        # Regression (2026-10-16): sips ignores TMPDIR and writes .<UUID>-<6> then <UUID> in the per-user temp
        # folder; with writes there denied it failed with "Cannot write to file …/T/<UUID>".
        for name, prof in self.profiles().items():
            with self.subTest(profile=name):
                r = self.run_in(prof, "rm -f .tmp/p1.png; sips -s format png notes/doc.pdf --out .tmp/p1.png")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue((self.root / ".tmp" / "p1.png").read_bytes().startswith(b"\x89PNG"))
                r = self.run_in(prof, "sips -Z 200 -s format jpeg .tmp/p1.png --out .tmp/p1.jpg")
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_operator_s_temp_folder_is_neither_listed_nor_read(self):
        # Until 2026-10-16 only the home was denied: any sandboxed command could list and read every app's temp files.
        temp = self.canary.parent
        for name, prof in self.profiles().items():
            for cmd in (f"ls {temp}", f"cat {self.canary}", f"cat {self.lookalike}", f"ls {temp.parent / 'C'}",
                        f"echo x > {temp / 'planted.txt'}"):
                with self.subTest(profile=name, cmd=cmd):
                    r = self.run_in(prof, cmd)
                    self.assertNotEqual(r.returncode, 0, r.stdout)
                    self.assertIn("Operation not permitted", r.stderr)
        self.assertFalse((temp / "planted.txt").exists())

    def test_the_operator_s_other_places_outside_home_are_closed(self):
        # /private/tmp held Claude Code's working files for every project of the operator's (2026-10-16).
        tmp_file = Path(tempfile.mkdtemp(prefix="shaft-sbx-tmp-", dir="/private/tmp")) / "notes.txt"
        tmp_file.write_text("the operator's")
        self.addCleanup(shutil.rmtree, tmp_file.parent, True)
        for name, prof in self.profiles().items():
            for cmd in (f"cat {tmp_file}", "ls /private/tmp", "ls /Users/Shared", "ls /Volumes"):
                with self.subTest(profile=name, cmd=cmd):
                    r = self.run_in(prof, cmd)
                    self.assertNotEqual(r.returncode, 0, r.stdout)
                    self.assertIn("Operation not permitted", r.stderr)

    def test_an_instance_in_a_temp_folder_still_uses_its_own_files(self):
        # The closing rule comes first: an instance folder inside the per-user folder (every test's) keeps working.
        inside = Path(tempfile.mkdtemp(prefix="shaft-sbx-in-")).resolve()
        self.addCleanup(shutil.rmtree, inside, True)
        self.assertIn(sandbox.user_folder(), inside.parents)
        (inside / "notes").mkdir()
        prof = inside / "p.sb"
        prof.write_text(sandbox.run_profile(inside, HOME))
        r = subprocess.run([sandbox.SANDBOX_EXEC, "-f", str(prof), "/bin/sh", "-c",
                            "echo kept > notes/n.txt && cat notes/n.txt"], cwd=inside, capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(r.stdout.strip(), "kept", r.stderr)

    def test_the_temp_name_pattern_has_no_counts(self):
        # SBPL regexes have no {n}: a pattern with them silently never matches.
        rule = "".join(sandbox._image_temp_names())
        self.assertNotIn("{", rule)
        self.assertIn(str(sandbox.user_folder() / "T"), rule)


if __name__ == "__main__":
    unittest.main()
