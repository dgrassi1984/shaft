"""SHAFT's own docs in each instance folder (shaft-docs/, runner.stage_docs): the operator, 2026-10-20, wants an
instance to read the docs and tell him how to configure SHAFT (template/skills/helping-the-operator-configure.md).
The copy is fresh at every start, read-only to the instance, never followed through a link, never committed."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import battery, core, runner, sandbox
from supervisor.tests import test_commit

PROJECT = Path(__file__).resolve().parents[2]


class StageDocsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-docs-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project, self.root = self.tmp / "project", self.tmp / "inst"
        (self.project / "docs" / "plans").mkdir(parents=True)
        (self.project / "docs" / "codemap").mkdir()
        (self.project / "README.md").write_text("# SHAFT\n")
        (self.project / "docs" / "cli.md").write_text("shaft mail <name> grant <alias> <label>\n")
        (self.project / "docs" / "plans" / "PLAN-mail.md").write_text("how it was built\n")
        (self.project / "docs" / "codemap" / "python.md").write_text("code map\n")
        self.root.mkdir()

    def stage(self):
        runner.stage_docs(self.root, self.project)

    def listing(self) -> list[str]:
        return sorted(p.relative_to(self.root / core.DOCS_COPY).as_posix()
                      for p in (self.root / core.DOCS_COPY).rglob("*"))

    def test_readme_and_the_user_docs_only_not_the_plans(self):
        self.stage()
        self.assertEqual(self.listing(), ["README.md", "cli.md"])
        self.assertEqual((self.root / "shaft-docs" / "cli.md").read_text(), "shaft mail <name> grant <alias> <label>\n")

    def test_a_changed_renamed_or_deleted_doc_is_current_at_the_next_start(self):
        self.stage()
        (self.project / "docs" / "cli.md").write_text("changed\n")
        (self.project / "README.md").rename(self.project / "docs" / "overview.md")
        self.stage()
        self.assertEqual(self.listing(), ["cli.md", "overview.md"])   # README.md gone: no stale doc left behind
        self.assertEqual((self.root / "shaft-docs" / "cli.md").read_text(), "changed\n")

    def test_anything_that_is_not_a_doc_is_removed(self):
        self.stage()
        (self.root / "shaft-docs" / "stray.md").write_text("x")
        (self.root / "shaft-docs" / "sub").mkdir()
        (self.root / "shaft-docs" / "sub" / "f").write_text("x")
        self.stage()
        self.assertEqual(self.listing(), ["README.md", "cli.md"])

    def test_an_unchanged_doc_is_not_rewritten(self):
        self.stage()
        doc = self.root / "shaft-docs" / "cli.md"
        os.utime(doc, (1, 1))
        self.stage()
        self.assertEqual(doc.stat().st_mtime, 1)

    def test_a_link_planted_as_the_folder_is_removed_never_written_through(self):
        # A link made before the sandbox denied the path: following it would let the supervisor write anywhere.
        target = self.tmp / "operator-files"
        target.mkdir()
        (target / "keep.md").write_text("the operator's")
        (self.root / "shaft-docs").symlink_to(target)
        self.stage()
        self.assertFalse((self.root / "shaft-docs").is_symlink())
        self.assertEqual(self.listing(), ["README.md", "cli.md"])
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["keep.md"])   # its target untouched
        self.assertEqual((target / "keep.md").read_text(), "the operator's")

    def test_a_link_planted_as_a_doc_is_removed_never_written_through(self):
        outside = self.tmp / "outside.md"
        outside.write_text("the operator's")
        (self.root / "shaft-docs").mkdir()
        (self.root / "shaft-docs" / "cli.md").symlink_to(outside)
        self.stage()
        self.assertFalse((self.root / "shaft-docs" / "cli.md").is_symlink())
        self.assertEqual((self.root / "shaft-docs" / "cli.md").read_text(), "shaft mail <name> grant <alias> <label>\n")
        self.assertEqual(outside.read_text(), "the operator's")

    def test_every_agent_start_stages_the_project_s_real_docs(self):
        from supervisor.broker import Broker
        sup = self.tmp / "sup"
        sup.mkdir()
        with mock.patch.object(core, "SUP_DIR", sup):
            b = Broker("x", sup, {}, sup / "b.log")
            b.start()
            self.addCleanup(b.stop)
            runner.agent_command(self.root, b, ["live"], sup / "p.sb", [8780], {})
        self.assertIn("cli.md", self.listing())
        self.assertEqual((self.root / "shaft-docs" / "cli.md").read_bytes(), (PROJECT / "docs" / "cli.md").read_bytes())
        self.assertIn('(deny file-write* (subpath "' + str(self.root / "shaft-docs") + '"))',
                      (sup / "p.sb").read_text())

    def test_the_template_skill_points_at_the_folder_and_the_docs_it_names_exist(self):
        skill = (PROJECT / "template" / "skills" / "helping-the-operator-configure.md").read_text()
        self.assertIn(f"{core.DOCS_COPY}/", skill)
        named = {w.split("/")[1].rstrip("`.,:)") for w in skill.split() if w.strip("`").startswith("shaft-docs/")}
        named = {n for n in named if n.endswith(".md") and "*" not in n}
        self.assertTrue(named)
        staged = {p.name for pattern in runner.DOCS_SOURCES for p in PROJECT.glob(pattern)}
        self.assertLessEqual(named, staged, "the skill names a doc that is not copied")


@unittest.skipUnless(Path(sandbox.SANDBOX_EXEC).exists(), "macOS sandbox")
class DocsSandboxTest(unittest.TestCase):
    """Real sandboxed processes: the instance reads shaft-docs/ and cannot change it by any path."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-sbdocs-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "shaft-docs").mkdir()
        (self.root / "shaft-docs" / "cli.md").write_text("shaft mail\n")
        self.prof = self.root / ".p.sb"
        self.prof.write_text(sandbox.profile(self.root, Path.home(), []))

    def attempt(self, code: str, prof: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(sandbox.wrap(prof or self.prof, [sys.executable, "-c", "import os\n" + code]),
                              cwd=self.root, capture_output=True, text=True)

    def test_it_reads_the_docs(self):
        r = self.attempt("print(open('shaft-docs/cli.md').read(), end='')")
        self.assertEqual((r.returncode, r.stdout), (0, "shaft mail\n"), r.stderr)

    def test_it_cannot_change_them_by_any_path(self):
        for code in ["open('shaft-docs/cli.md', 'w').write('x')", "open('shaft-docs/cli.md', 'a').write('x')",
                     "open('shaft-docs/new.md', 'w').write('x')", "os.unlink('shaft-docs/cli.md')",
                     "os.rename('shaft-docs/cli.md', 'moved.md')", "os.rename('shaft-docs', 'moved')",
                     "os.mkdir('shaft-docs/sub')",
                     "open('t', 'w').write('x'); os.replace('t', 'shaft-docs/cli.md')",
                     "os.link('shaft-docs/cli.md', 'h'); open('h', 'a').write('x')"]:
            self.assertNotEqual(self.attempt(code).returncode, 0, code)
        self.assertEqual(sorted(p.name for p in (self.root / "shaft-docs").iterdir()), ["cli.md"])
        self.assertEqual((self.root / "shaft-docs" / "cli.md").read_text(), "shaft mail\n")
        self.assertEqual(self.attempt("open('notes.md', 'w').write('fine')").returncode, 0)   # the rest still is

    def test_nor_plant_one_where_it_is_missing(self):
        shutil.rmtree(self.root / "shaft-docs")
        for code in ["os.mkdir('shaft-docs')", "os.symlink('/etc', 'shaft-docs')",
                     "os.mkdir('d'); os.rename('d', 'shaft-docs')"]:
            self.assertNotEqual(self.attempt(code).returncode, 0, code)
        self.assertFalse(os.path.lexists(self.root / "shaft-docs"))

    def test_run_greps_them(self):
        # The skill sends the instance to `run` grep for a section's offset: the run sandbox reads its own folder.
        prof = self.root / ".run.sb"
        prof.write_text(sandbox.run_profile(self.root, Path.home()))
        r = subprocess.run(sandbox.wrap(prof, ["/usr/bin/grep", "-nb", "shaft", "shaft-docs/cli.md"]), cwd=self.root,
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, "1:0:shaft mail\n"), r.stderr)


class DocsNeverCommittedTest(test_commit.UpdateCase):
    """The copy is the supervisor's, refreshed at every start: never staged on the instance's branch, never
    snapshotted (and restored) around an exam."""

    def setUp(self):
        super().setUp()
        (self.root / "shaft-docs").mkdir()
        (self.root / "shaft-docs" / "cli.md").write_text("shaft mail\n")
        (self.root / "plugins").mkdir()
        (self.root / "plugins" / "p.py").write_text("x = 1\n")   # an ordinary untracked file, staged beside it

    def test_add_all_but_mind_leaves_it_out(self):
        core.add_all_but_mind(self.root, self.project)
        staged = core.git_wt(self.root, "diff", "--cached", "--name-only", project=self.project).stdout.split()
        self.assertIn("plugins/p.py", staged)
        self.assertFalse([p for p in staged if p.startswith("shaft-docs")], staged)

    def test_a_trial_replay_s_diff_leaves_it_out(self):
        # Regression: the replay stages the docs at its start, and the judge's diff of the copy showed every doc as
        # the replay's new work (test_trial's venv check failed on ".venv" in README.md, 2026-10-20).
        from supervisor import trial
        files = trial._files_now(self.root)
        self.assertIn("plugins/p.py", files)
        self.assertFalse([p for p in files if p.startswith("shaft-docs")], sorted(files))

    def test_the_battery_snapshot_leaves_it_out(self):
        files = battery._files(self.root)
        self.assertIn("plugins/p.py", files)
        self.assertFalse([p for p in files if p.startswith("shaft-docs")], files)


if __name__ == "__main__":
    unittest.main()
