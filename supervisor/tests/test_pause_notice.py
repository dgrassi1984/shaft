"""Operator pause transitions leave a supervisor note in the affected journals."""
import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import __main__ as cli, core, kill


class PauseNoticeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-pause-notice-")
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.sup = self.project / ".supervisor"
        self.sup.mkdir()
        self.instances = self.project / "instances"
        for name in ("wisp", "tundra"):
            root = self.instances / name
            (root / "shaft").mkdir(parents=True)
            (root / ".git").touch()
        for module in (core, cli, kill):
            for attr, value in (("SUP_DIR", self.sup), ("INSTANCES", self.instances)):
                patch = mock.patch.object(module, attr, value)
                patch.start()
                self.addCleanup(patch.stop)

    def command(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(list(args)), 0)

    def journal(self, name="wisp"):
        return (self.instances / name / "memory/journal.md").read_text()

    def test_pause_and_repause_preserve_note_and_start_time(self):
        self.command("pause", "wisp")
        flag = self.sup / "paused-wisp"
        stamp, note = flag.read_text(), self.journal()
        self.assertIn("Supervisor: paused by the operator", note)
        self.assertIn(f"started at {datetime.fromisoformat(stamp):%Y-%m-%d %H:%M}", note)
        self.assertIn("No autonomous cycle, nightly sleep, job or battery starts while paused.", note)
        self.assertIn("Conversations still run.", note)
        self.command("pause", "wisp")
        self.assertEqual((flag.read_text(), self.journal()), (stamp, note))
        self.assertFalse((self.instances / "tundra/memory/journal.md").exists())

    def test_resume_reports_stored_duration_and_is_idempotent(self):
        self.command("pause", "wisp")
        flag = self.sup / "paused-wisp"
        flag.write_text("2026-10-25T12:42:00")                     # tundra's pause, as `shaft pause` stores it
        with mock.patch.object(core, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat("2026-10-26T14:36:00")
            self.command("resume", "wisp")
        note = self.journal()
        self.assertFalse(flag.exists())
        self.assertIn("Supervisor: resumed by the operator", note)
        self.assertIn("ended at 2026-10-26 14:36. The pause lasted 25 h 54 min (since 2026-10-25 12:42).", note)
        self.assertIn("Autonomous starts are allowed again.", note)
        self.command("resume", "wisp")
        self.assertEqual(self.journal(), note)

    def test_global_pause_and_resume_with_individual_pause(self):
        self.command("pause", "wisp")
        self.command("pause")
        for name in ("wisp", "tundra"):
            self.assertIn("Global pause started at", self.journal(name))
        notes = [self.journal(n) for n in ("wisp", "tundra")]
        self.command("pause")
        self.assertEqual([self.journal(n) for n in ("wisp", "tundra")], notes)
        self.command("resume")
        self.assertFalse((self.sup / "paused").exists())
        self.assertTrue(core.paused("wisp"))
        self.assertFalse(core.paused("tundra"))
        self.assertIn("The global pause ended, but this instance is still individually paused.", self.journal())
        self.assertIn("Autonomous starts are allowed again.", self.journal("tundra"))

    def test_individual_resume_under_global_pause(self):
        self.command("pause")
        self.command("pause", "wisp")
        self.command("resume", "wisp")
        self.assertTrue(core.paused("wisp"))
        self.assertIn("This instance is still paused by the global pause.", self.journal())

    def test_unreadable_stamps_do_not_prevent_resume(self):
        for stamp in (b"not a date", b"\xff", b"2026-10-25T12:42:00"):
            with self.subTest(stamp=stamp):
                flag = self.sup / "paused-wisp"
                flag.write_bytes(stamp)
                if stamp.startswith(b"2026"):
                    with mock.patch.object(Path, "read_text", side_effect=OSError("unreadable")):
                        self.command("resume", "wisp")
                else:
                    self.command("resume", "wisp")
                self.assertFalse(flag.exists())
                self.assertIn("The pause duration is unknown because its stored start time could not be read.",
                              self.journal())

    def test_note_failure_does_not_break_pause_resume_or_kill(self):
        with mock.patch.object(core, "journal_note", side_effect=OSError("journal unavailable")):
            self.command("pause", "wisp")
            self.assertTrue(core.paused("wisp"))
            self.command("resume", "wisp")
            self.assertFalse(core.paused("wisp"))
            with mock.patch.object(kill, "processes", return_value={}), \
                 mock.patch.object(kill, "runner_busy", return_value=False), \
                 mock.patch.object(kill, "running", return_value=[]):
                self.command("kill", "wisp")
            self.assertTrue(core.paused("wisp"))
        self.assertEqual((self.sup / "supervisor.log").read_text().count("NOT written: journal unavailable"), 3)

    def test_kill_explains_pause_and_does_not_repeat_it(self):
        with mock.patch.object(kill, "processes", return_value={}), \
             mock.patch.object(kill, "runner_busy", return_value=False), \
             mock.patch.object(kill, "running", return_value=[]):
            self.command("kill", "wisp")
            note = self.journal()
            self.assertTrue(core.paused("wisp"))
            self.assertIn("Supervisor: paused by a kill", note)
            self.command("kill", "wisp")
            self.assertEqual(self.journal(), note)

    def test_global_note_failure_does_not_skip_other_instances(self):
        (self.instances / "tundra/memory").symlink_to(self.project, target_is_directory=True)
        self.command("pause")
        self.assertTrue(core.paused("tundra"))
        self.assertIn("Global pause started at", self.journal())
        self.assertFalse((self.project / "journal.md").exists())
        self.assertIn("NOT written", (self.sup / "supervisor.log").read_text())
