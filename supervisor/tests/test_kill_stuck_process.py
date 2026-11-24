"""An ordinary sleeping process whose cwd lookup blocked in the kernel froze every run end."""
import json
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
    def setUp(self):
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(kill.core, "SUP_DIR", scratch))
        self.log = self.enterContext(mock.patch.object(kill, "log"))
        self.blockers = scratch / "cwd-blockers.json"
        self.commands = {11: "sleep 10", 12: "sleep 20", 13: "sleep 30", 14: "/bin/zsh /example/devicectl  list devices"}

    def fake_listing(self, *blockers):
        def run(argv, *, timeout=kill.LIST_TIMEOUT_S):
            if argv[0] == kill.PS:
                out = "".join(f"{pid} 1 S {command}\n" for pid, command in self.commands.items())
            else:
                self.assertEqual(argv[0], kill.LSOF)
                pids = [int(pid) for pid in argv[argv.index("-p") + 1].split(",")]
                if any(pid in blockers for pid in pids):
                    raise kill._ListTimeout(f"{kill.LSOF} timed out after {timeout:g}s")
                out = "".join(f"p{pid}\nfcwd\nn/folder/{pid}\n" for pid in pids)
            return subprocess.CompletedProcess(argv, 0, out, "")

        return self.enterContext(mock.patch.object(kill, "_list_command", side_effect=run))

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

    def test_sleeping_blocker_is_cached_and_skipped_on_the_next_listing(self):
        listing = self.fake_listing(14)
        procs = kill.processes()
        self.assertEqual({pid: p.cwd for pid, p in procs.items()}, {
            11: "/folder/11", 12: "/folder/12", 13: "/folder/13", 14: "",
        })
        self.assertEqual(json.loads(self.blockers.read_text()), {"14": self.commands[14]})
        lsof_calls = [call for call in listing.call_args_list if call.args[0][0] == kill.LSOF]
        self.assertEqual([call.args[0][5] for call in lsof_calls], ["11,12,13,14", "11,12", "13,14", "13", "14"])
        self.assertEqual(lsof_calls[0].kwargs, {})
        for call in lsof_calls[1:]:
            self.assertEqual(call.kwargs, {"timeout": kill.PROBE_TIMEOUT_S})

        listing.reset_mock()
        self.assertEqual(kill.processes(), procs)
        lsof_calls = [call for call in listing.call_args_list if call.args[0][0] == kill.LSOF]
        self.assertEqual(lsof_calls, [mock.call([kill.LSOF, "-a", "-d", "cwd", "-p", "11,12,13", "-Fpn"])])
        self.log.assert_called_once_with("skipping cwd lookup for pid 14 after lsof timed out")

    def test_two_blockers_in_the_same_list_are_found(self):
        self.blockers.write_text(json.dumps({"14": "sleep 40"}))
        self.fake_listing(11, 14)
        procs = kill.processes()
        self.assertEqual({pid: p.cwd for pid, p in procs.items()}, {
            11: "", 12: "/folder/12", 13: "/folder/13", 14: "",
        })
        self.assertEqual(json.loads(self.blockers.read_text()), {"11": self.commands[11], "14": self.commands[14]})
        self.assertEqual(self.log.call_args_list, [
            mock.call("skipping cwd lookup for pid 11 after lsof timed out"),
            mock.call("skipping cwd lookup for pid 14 after lsof timed out"),
        ])

    def test_a_pid_with_a_changed_command_is_probed_again(self):
        self.blockers.write_text(json.dumps({"14": "sleep 40", "15": "sleep 50"}))
        self.fake_listing()
        self.assertEqual(kill.processes()[14].cwd, "/folder/14")
        self.assertEqual(json.loads(self.blockers.read_text()), {})

    def test_corrupt_blocker_file_is_replaced(self):
        self.blockers.write_text("{")
        self.fake_listing()
        self.assertEqual(kill.processes()[14].cwd, "/folder/14")
        self.assertEqual(json.loads(self.blockers.read_text()), {})

    def test_unreadable_blocker_file_does_not_stop_listing(self):
        self.fake_listing()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            self.assertEqual(kill.processes()[14].cwd, "/folder/14")

    def test_probe_cap_raises_and_keeps_blockers_already_found(self):
        listing = self.fake_listing(11, 14)
        with mock.patch.object(kill, "MAX_PROBES", 3):
            with self.assertRaisesRegex(RuntimeError, "exceeded 3 cwd probes"):
                kill.processes()
        self.assertEqual(sum(call.args[0][0] == kill.LSOF for call in listing.call_args_list), 4)
        self.assertEqual(json.loads(self.blockers.read_text()), {"11": self.commands[11]})

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
