"""Each run is settled whatever happens to it (review of 2026-10-24, B8 and B9): code a run left unsettled is gated
before the next run takes its baseline, and nothing a run started outlives it into the gate's verdict."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import broker, core, gate, mind, runner
from supervisor.tests.mindfix import IGNORE_MIND


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


COMMITTED = "def run(**args):\n    return 'stats'\n"
UNGATED = "def run(**args):\n    return 'edited by a run that died before it was checked'\n"
# A run that changes nothing, and one whose agent leaves a process behind that writes the code once the run is over.
QUIET = "pass"
LEAVES_A_WRITER = r"""
import subprocess, sys
subprocess.Popen([sys.executable, "-c", "import pathlib, time; time.sleep(1.5); "
                  "pathlib.Path('plugins/late.py').write_text('written after the gate')"],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"""


class RunTransactionCase(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-txn-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "kant"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/kant", str(self.root))
        for d in ("shaft", "self", "memory", "plugins/text_stats", ".tmp"):
            (self.root / d).mkdir(parents=True)
        (self.root / "shaft" / "__init__.py").write_text("")
        (self.root / "plugins" / "text_stats" / "main.py").write_text(COMMITTED)
        (self.root / "memory" / "journal.md").write_text("# kant — journal\n")
        (self.root / ".gitignore").write_text(".tmp/\n" + IGNORE_MIND)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "kant: created")
        mind.init("kant", self.project)
        mind.commit("kant", "kant: created", self.root, self.project)
        self.gated: list[str] = []

    def fake_gate(self, root: Path, home: Path):
        """Fails while the plugin holds the edit nobody checked; records what it judged."""
        text = (root / "plugins" / "text_stats" / "main.py").read_text()
        self.gated.append(text)
        return ("died before" not in text, "FAIL: test_text_stats (tests.test_text_stats.T.test_text_stats)")

    def cycle(self, script: str = QUIET) -> int:
        """One live cycle through the supervisor's own entry point; its agent is `script`."""
        sup = self.project / ".supervisor"
        b = broker.Broker("kant", self.project, {}, sup / "broker.log")
        cmd, env = [sys.executable, "-c", script], {"SHAFT_DEADLINE": f"{time.time() + 600:.0f}"}
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(core, "SUP_DIR", sup), \
             mock.patch.object(runner, "API_TALKS", sup / "api.json"), \
             mock.patch.object(runner, "PROJECT", self.project), mock.patch.object(core, "PROJECT", self.project), \
             mock.patch.object(core, "INSTANCES", self.project / "instances"), \
             mock.patch.object(runner, "instance_root", return_value=self.root), \
             mock.patch.object(runner, "_launch", return_value=(b, cmd, env)), \
             mock.patch.object(gate, "run_tests", side_effect=self.fake_gate), \
             mock.patch.object(runner, "log"), mock.patch("supervisor.metrics.after_cycle"), \
             mock.patch("supervisor.metrics.start_probation"):
            return runner.run_agent("kant", "live", ["live"], False)

    def plugin(self) -> str:
        return (self.root / "plugins" / "text_stats" / "main.py").read_text()

    def code_status(self) -> str:
        return git(self.root, "status", "--porcelain", "--untracked-files=all", "--", *gate.CODE_PATHS)


class UnsettledCodeTest(RunTransactionCase):
    """B8: a run's baseline was a snapshot of the disk, and nothing reconciled code a run left behind when it died
    before _settle (the supervisor killed, the Mac shut down): the next run took those edits as its baseline, so they
    were never gated."""

    def test_code_a_dead_run_left_is_gated_first_and_put_back_when_it_fails(self):
        (self.root / "plugins" / "text_stats" / "main.py").write_text(UNGATED)
        (self.root / "plugins" / "evil").mkdir()
        (self.root / "plugins" / "evil" / "main.py").write_text("def run(**a):\n    return 'never checked'\n")
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [UNGATED], "the edit was taken as the next run's baseline, ungated")
        self.assertEqual(self.plugin(), COMMITTED)
        self.assertFalse((self.root / "plugins" / "evil" / "main.py").exists())   # (its empty folder stays, as always)
        self.assertEqual(self.code_status(), "")
        journal = (self.root / "memory" / "journal.md").read_text()
        self.assertIn("unchecked code from an earlier run", journal)
        self.assertIn("were reverted because the tests failed", journal)

    def test_code_a_dead_run_left_is_kept_and_committed_when_it_passes(self):
        good = "def run(**args):\n    return 'better stats'\n"
        (self.root / "plugins" / "text_stats" / "main.py").write_text(good)
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [good])
        self.assertEqual(self.plugin(), good)
        self.assertEqual(self.code_status(), "", "kept but never committed: gated again at every run")
        self.assertIn("plugins/text_stats/main.py", git(self.root, "log", "--format=%B", "-n", "2"))
        self.assertIn("kant: interrupted", git(self.root, "log", "--format=%s", "-n", "2"))

    def test_a_run_after_a_settled_one_is_not_gated_at_its_start(self):
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [])                     # nothing changed: no gate, as before
        self.assertNotIn("unchecked code", (self.root / "memory" / "journal.md").read_text())


class RunOutlivesItselfTest(RunTransactionCase):
    """B9: a run had no process group of its own and only its direct child was ever killed (on a timeout): a process
    the agent started went on writing the instance's code after settlement had judged it."""

    def test_a_process_the_agent_left_behind_cannot_write_the_code_after_the_gate(self):
        self.assertEqual(self.cycle(LEAVES_A_WRITER), 0)
        time.sleep(2.5)                                       # past the moment the leftover would write
        self.assertFalse((self.root / "plugins" / "late.py").exists(), "a leftover wrote the code after the gate")
        self.assertEqual(self.code_status(), "")

    def test_a_stopped_supervisor_ends_the_run_and_leaves_its_changes_to_the_next_run(self):
        """SIGTERM to the supervisor (launchd stopping it) ends the run's group before it goes, and the edit the run
        made is gated when the instance next runs."""
        script = ("import pathlib, time\npathlib.Path('plugins/text_stats/main.py').write_text(" + repr(UNGATED) + ")\n"
                  "pathlib.Path('.tmp/working').write_text('1')\ntime.sleep(20)\n")
        result, killed, real_kill = {}, threading.Event(), os.kill

        def stop_when_working():
            for _ in range(400):
                if (self.root / ".tmp" / "working").exists():
                    killed.set()
                    real_kill(os.getpid(), signal.SIGTERM)
                    return
                time.sleep(0.05)

        def kill(pid, sig):                                  # the run sends the signal on once it has cleaned up:
            if pid == os.getpid() and sig == signal.SIGTERM:  # kept here, instead of ending the test process
                result["sent_on"] = sig
            else:
                real_kill(pid, sig)
        threading.Thread(target=stop_when_working, daemon=True).start()
        started = time.monotonic()
        # The test's own handler: without the run's, SIGTERM would end the whole test process instead of failing.
        before = signal.signal(signal.SIGTERM, lambda *a: result.update(missed=True))
        self.addCleanup(signal.signal, signal.SIGTERM, before)
        with mock.patch.object(os, "kill", side_effect=kill):
            result["code"] = self.cycle(script)
        self.assertNotIn("missed", result, "the supervisor did not handle the signal: its run's agent went on")
        self.assertTrue(killed.is_set())
        self.assertEqual((result["code"], result.get("sent_on")), (128 + signal.SIGTERM, signal.SIGTERM))
        self.assertLess(time.monotonic() - started, 15, "the agent was not ended")
        self.assertEqual(self.gated, [], "no gate while the supervisor is being stopped")
        self.assertEqual(self.plugin(), UNGATED)              # left as it was: the next run settles it
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.gated, [UNGATED])
        self.assertEqual(self.plugin(), COMMITTED)


class TerminalConversationTest(unittest.TestCase):
    """A plain terminal conversation's agent leads its own group now (B9), so the supervisor hands it the terminal:
    what the operator types and Ctrl-C still reach it, and the terminal comes back to the supervisor after."""

    SUPERVISOR = r"""
import os, signal, sys
from pathlib import Path
from supervisor import gate, runner
signal.signal(signal.SIGINT, signal.SIG_IGN)            # as _run does for a terminal conversation
agent = ("import signal, sys\nsignal.signal(signal.SIGINT, signal.default_int_handler)\n"
         "print('agent ready', flush=True)\nprint('agent read', sys.stdin.readline().strip(), flush=True)\n"
         "try:\n    sys.stdin.readline()\nexcept KeyboardInterrupt:\n    print('agent got ctrl-c', flush=True)\n")
proc = runner._start([sys.executable, "-c", agent], Path.cwd(), dict(os.environ), False, True)
take_back = runner._give_terminal(proc)
print("terminal given", take_back is not None and os.tcgetpgrp(0) == proc.pid, flush=True)
proc.wait()
take_back()
gate.stop_group(proc)
print("terminal back", os.tcgetpgrp(0) == os.getpgrp(), flush=True)
"""

    def test_the_agent_has_the_terminal_and_gives_it_back(self):
        import pty
        import select
        project = Path(__file__).resolve().parents[2]
        pid, fd = pty.fork()
        if pid == 0:                                         # the supervisor, in a terminal of its own
            os.chdir(project)
            os.execv(sys.executable, [sys.executable, "-E", "-s", "-c", self.SUPERVISOR])
        seen = b""

        def until(text: bytes, timeout: float = 20) -> None:
            nonlocal seen
            end = time.monotonic() + timeout
            while text not in seen and time.monotonic() < end:
                if select.select([fd], [], [], 0.2)[0]:
                    try:
                        seen += os.read(fd, 4096)
                    except OSError:
                        break
            self.assertIn(text, seen)
        try:
            until(b"agent ready")
            until(b"terminal given True")
            os.write(fd, b"hello\n")
            until(b"agent read hello")
            os.write(fd, b"\x03")                                # Ctrl-C at the terminal
            until(b"agent got ctrl-c")
            until(b"terminal back True")
        finally:
            for _ in range(100):                             # a failure above must not leave the test waiting
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                time.sleep(0.05)
            else:
                os.kill(pid, signal.SIGKILL)
                _, status = os.waitpid(pid, 0)
            os.close(fd)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, seen.decode(errors="replace"))


class RunCommandOutlivesItselfTest(unittest.TestCase):
    """B9: a `run` command's process group was killed only on a timeout: `cmd &` outlived the command, and the run."""

    def test_a_background_process_of_a_run_command_ends_with_it(self):
        base = Path(__file__).resolve().parents[2] / ".supervisor"
        base.mkdir(exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="test-run-bg-", dir=base)).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / "instances" / "r"
        for d in ("plugins", "memory"):
            (root / d).mkdir(parents=True)
        b = broker.Broker("r", tmp, {}, tmp / "broker.log")
        out = b.call("run", {"command": "(sleep 1.5; echo late > plugins/late.txt) >/dev/null 2>&1 &"})
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["result"].startswith("exit 0"), out)
        time.sleep(3)
        self.assertFalse((root / "plugins" / "late.txt").exists(), "the command's background process outlived it")


if __name__ == "__main__":
    unittest.main()
