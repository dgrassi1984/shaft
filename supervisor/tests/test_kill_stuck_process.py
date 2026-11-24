"""Process listing when macOS has a stuck process."""
import re
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from supervisor import kill


class StuckProcessTest(unittest.TestCase):
    def test_lsof_skips_exiting_processes_and_zombies(self):
        """A stuck exiting process hung every lsof -u and froze every run end."""
        ps = [kill.PS, "-x", "-o", "pid=,ppid=,stat=,command="]
        lsof = [kill.LSOF, "-a", "-d", "cwd", "-p", "11,14", "-Fpn"]
        with mock.patch.object(kill, "_list_command", side_effect=[
            subprocess.CompletedProcess(ps, 0, " 11 1 S python -m shaft live\n 12 1 ?E\n 13 12 Z (sleep)\n 14 1 S\n"),
            subprocess.CompletedProcess(lsof, 1, "p11\nfcwd\nn/working folder\n", "pid 14 vanished"),
        ]) as listing:
            self.assertEqual(kill.processes(), {
                11: kill.Proc(11, 1, "python -m shaft live", "/working folder"),
                12: kill.Proc(12, 1, ""),
                13: kill.Proc(13, 12, "(sleep)"),
                14: kill.Proc(14, 1, ""),
            })
        self.assertEqual(listing.call_args_list, [mock.call(ps), mock.call(lsof)])

    def test_slow_listing_times_out(self):
        with tempfile.TemporaryDirectory(dir=".") as scratch, ExitStack() as children:
            script = Path(scratch).resolve() / "slow-ps"
            script.write_text("#!/bin/sh\nexec /bin/sleep 8\n")
            script.chmod(0o700)
            popen = subprocess.Popen

            def launch(*args, **kwargs):
                return children.enter_context(popen(*args, **kwargs))

            with mock.patch.object(kill, "PS", str(script)), mock.patch.object(kill, "LIST_TIMEOUT_S", 0.05), \
                    mock.patch.object(kill.subprocess, "Popen", side_effect=launch):
                start = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, re.escape(f"{script} timed out after 0.05s")):
                    kill.processes()
                self.assertLess(time.monotonic() - start, 2)

    def test_timed_out_listing_is_killed_without_waiting_or_reading_again(self):
        argv = [kill.PS, "-x", "-o", "pid=,ppid=,stat=,command="]
        with mock.patch.object(kill.subprocess, "Popen") as popen:
            child = popen.return_value
            child.communicate.side_effect = subprocess.TimeoutExpired(argv, kill.LIST_TIMEOUT_S)
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                kill.processes()
        self.assertEqual(child.method_calls, [
            mock.call.communicate(timeout=kill.LIST_TIMEOUT_S),
            mock.call.kill(),
            mock.call.stdout.close(),
            mock.call.stderr.close(),
        ])
