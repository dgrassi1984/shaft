"""config.env has two writers — the operator, at any time, and the instance through the broker's set_config —
and the gate reverts only the instance's lines.

Regression (2026-10-13): Daniele switched kant to claude-opus-5-5 at about 15:00, during a live cycle. The gate
counted every change to config.env as the instance's; kant's new plugin failed its tests, so at 15:20 the gate
"reverted: ['config.env', 'plugins/text_stats/...']", told kant that *its* change was reverted, and the next
reflection ran on Bonsai."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import gate, mind, sandbox
from supervisor.tests.mindfix import IGNORE_MIND

OPERATOR = 'LLM_MODEL="claude-opus-5-5"   # Daniele, 2026-10-13'
CONFIG = 'LLM_BASE_URL="broker"\nLLM_MODEL="bonsai"\nWAKE_INTERVAL_MINUTES=120\n'
FAILING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail('broken')\n"
PASSING = "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(True)\n"

# The instance's cycle: it writes a plugin, a test and a journal line, then keeps working until the test says it is done.
CYCLE = r"""
import sys, time
from pathlib import Path
Path("plugins/text_stats").mkdir(parents=True, exist_ok=True)
Path("plugins/text_stats/main.py").write_text("def run(**args):\n    return 'stats'\n")
Path("tests").mkdir(exist_ok=True)
Path("tests/test_text_stats.py").write_text(sys.argv[1])
with Path("memory/journal.md").open("a") as f:
    f.write("made text_stats\n")
Path(".tmp/working").write_text("1")
end = time.time() + 30
while not Path(".tmp/done").exists() and time.time() < end:
    time.sleep(0.05)
"""


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout


REAL_RUN_TESTS = gate.run_tests


def instance_tests_only(root: Path, home: Path):
    """The gate's tests minus the supervisor invariants (they would run this whole suite again, inside itself):
    the instance's own tests, in its sandbox, for real."""
    fd, prof = tempfile.mkstemp(suffix=".sb")
    os.close(fd)
    try:
        Path(prof).write_text(sandbox.profile(root, home, []))
        return gate.run_instance_tests(root, Path(prof), sandbox.agent_env(os.environ, TMPDIR=str(root / ".tmp")),
                                       sys.executable)
    finally:
        os.unlink(prof)


class ConfigWritersTest(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-cfgw-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "kant"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/kant", str(self.root))
        for d in ("shaft", "self", "memory", "plugins", ".tmp"):
            (self.root / d).mkdir()
        (self.root / "shaft" / "__init__.py").write_text("")
        (self.root / "memory" / "journal.md").write_text("# kant — journal\n")
        (self.root / "config.env").write_text(CONFIG)
        (self.root / ".gitignore").write_text(".tmp/\n" + IGNORE_MIND)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "kant: created")
        mind.init("kant", self.project)
        mind.commit("kant", "kant: created", self.root, self.project)

    def cycle(self, test_file: str, during, gate_tests=instance_tests_only):
        """One live cycle through the supervisor's own entry point; `during(broker)` runs while the instance
        is mid-cycle, then the cycle ends. `gate_tests` stands in for gate.run_tests."""
        from supervisor import core, runner
        from supervisor.broker import Broker
        sup = self.project / ".supervisor"
        broker = Broker("kant", self.project, {}, sup / "broker.log")
        cmd = [sys.executable, "-c", CYCLE, test_file]
        env = {"SHAFT_DEADLINE": f"{time.time() + 600:.0f}"}
        result = {}
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(core, "SUP_DIR", sup), \
             mock.patch.object(runner, "API_TALKS", sup / "api.json"), \
             mock.patch.object(runner, "PROJECT", self.project), mock.patch.object(core, "PROJECT", self.project), \
             mock.patch.object(core, "INSTANCES", self.project / "instances"), \
             mock.patch.object(runner, "instance_root", return_value=self.root), \
             mock.patch.object(runner, "_launch", return_value=(broker, cmd, env)), \
             mock.patch.object(gate, "run_tests", side_effect=gate_tests), \
             mock.patch.object(runner, "log"), mock.patch("supervisor.metrics.after_cycle"), \
             mock.patch("supervisor.metrics.start_probation") as probation:
            t = threading.Thread(target=lambda: result.update(code=runner.run_agent("kant", "live", ["live"], False)))
            t.start()
            for _ in range(200):
                if (self.root / ".tmp" / "working").exists():
                    break
                time.sleep(0.05)
            self.assertTrue((self.root / ".tmp" / "working").exists(), "the fake cycle never started")
            during(broker)
            (self.root / ".tmp" / "done").write_text("1")
            t.join(timeout=60)
        self.assertEqual(result.get("code"), 0)
        self.probation = probation
        return broker

    def operator_edits(self, *lines: str):
        """Daniele, in his editor, outside the sandbox."""
        text = (self.root / "config.env").read_text()
        for line in lines:
            key = line.split("=", 1)[0]
            text = "\n".join(line if l.startswith(key + "=") else l for l in text.splitlines()) + "\n"
        (self.root / "config.env").write_text(text)

    def journal_entry(self, title: str) -> str:
        entries = (self.root / "memory" / "journal.md").read_text().split("\n---\n")
        found = [e for e in entries if title in e.split("\n", 1)[0]]
        self.assertEqual(len(found), 1, f"expected one {title!r} entry:\n" + "\n---\n".join(entries))
        return found[0]

    def commits(self) -> list[str]:
        return git(self.root, "log", "--format=%an|%B%x00").split("\0")

    def test_an_operator_edit_mid_cycle_survives_the_instance_failing_its_tests(self):
        self.cycle(FAILING, lambda broker: self.operator_edits(OPERATOR))
        config = (self.root / "config.env").read_text()
        self.assertIn(OPERATOR, config)                                     # the bug put LLM_MODEL="bonsai" back
        self.assertNotIn('LLM_MODEL="bonsai"', config)
        self.assertFalse((self.root / "plugins" / "text_stats" / "main.py").exists())   # its broken change is gone
        self.assertFalse((self.root / "tests" / "test_text_stats.py").exists())
        reverted = self.journal_entry("Supervisor: code change reverted")
        self.assertIn("plugins/text_stats/main.py", reverted)
        self.assertIn("tests/test_text_stats.py", reverted)
        self.assertNotIn("config.env", reverted)                           # not "my changes to config.env"
        # Committed as the operator's change, not as the instance's: nothing is left uncommitted.
        self.assertEqual(git(self.root, "status", "--porcelain", "--", "config.env"), "")
        self.assertIn(OPERATOR, git(self.root, "show", "HEAD:config.env"))
        outside = [c for c in self.commits() if "config.env changed from outside the instance" in c]
        self.assertEqual(len(outside), 1)
        self.assertTrue(outside[0].strip().startswith("SHAFT supervisor|"))
        self.assertIn('LLM_MODEL: bonsai -> claude-opus-5-5', outside[0])
        self.assertFalse([c for c in self.commits() if "made by the instance" in c and "config.env" in c])

    def test_a_gate_stage_that_times_out_reverts_the_change_instead_of_leaving_it_ungated_20261016(self):
        # Regression (found 2026-10-16): a gate stage past its timeout raised TimeoutExpired out of runner._settle.
        # The change stayed on disk, neither kept nor reverted, and the next run's snapshot took it as its "before":
        # it passed without ever being gated. The real gate.run_tests runs here, every stage; the instance's test hangs.
        tree = self.project.parent / (self.project.name + "-tree")                 # a tiny committed supervisor
        self.addCleanup(shutil.rmtree, tree, True)
        (tree / "supervisor" / "tests").mkdir(parents=True)
        for rel in ("supervisor/__init__.py", "supervisor/tests/__init__.py"):
            (tree / rel).write_text("")
        (tree / "supervisor" / "tests" / "test_ok.py").write_text(PASSING)
        (self.root / "shaft" / "plugin_runner.py").write_text("")                # its plugins' check passes
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "kant: plugin runner")
        hangs = ("import sys, time, unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n"
                 "        sys.stderr.write('test_x started\\n'); sys.stderr.flush(); time.sleep(120)\n")
        with mock.patch.object(gate, "committed_tree", return_value=(tree, "tree")):
            self.cycle(hangs, lambda broker: None, lambda root, home: REAL_RUN_TESTS(root, home, timeout_s=5))
        self.assertFalse((self.root / "plugins" / "text_stats" / "main.py").exists())   # reverted, not left on disk
        self.assertFalse((self.root / "tests" / "test_text_stats.py").exists())
        unsettled = git(self.root, "status", "--porcelain", "--untracked-files=all", "--", *gate.CODE_PATHS)
        self.assertEqual([l for l in unsettled.splitlines() if "__pycache__" not in l], [])     # the gate ignores those
        self.assertFalse([c for c in self.commits() if "text_stats" in c])
        reverted = self.journal_entry("Supervisor: code change reverted")           # the instance is told why
        self.assertIn("plugins/text_stats/main.py", reverted)
        self.assertIn("instance tests: timed out after 5s, counted as failed", reverted)
        self.assertIn("test_x started", reverted)
        logged = (self.project / ".supervisor" / "supervisor.log").read_text()
        self.assertIn("instance tests: timed out after 5s", logged)                  # and the operator, which stage
        self.probation.assert_not_called()

    def test_an_operator_edit_mid_cycle_is_not_the_instance_s_change_when_its_tests_pass(self):
        self.cycle(PASSING, lambda broker: self.operator_edits(OPERATOR))
        self.assertIn(OPERATOR, (self.root / "config.env").read_text())
        mine = [c for c in self.commits() if "made by the instance" in c]
        self.assertEqual(len(mine), 1)
        self.assertNotIn("config.env", mine[0])                             # and not on probation as the instance's
        # The run's journal went to its mind store, under the same message; the branch commit holds no mind path.
        run = git(self.root, "log", "-1", "--format=%H", "--grep=made by the instance").strip()
        self.assertFalse([p for p in git(self.root, "show", "--name-only", "--format=", run).split()
                          if p.split("/")[0] in ("self", "memory", "notes", "inbox")])
        self.assertEqual(mind.git("kant", "log", "-1", "--format=%B", project=self.project).stdout.strip(),
                         git(self.root, "log", "-1", "--format=%B", run).strip())
        self.assertIn("made text_stats", mind.show("kant", "mind", "memory/journal.md", self.project) or "")
        self.assertNotIn("config.env", self.probation.call_args.args[2])
        self.assertEqual(len([c for c in self.commits() if "changed from outside the instance" in c]), 1)

    def test_the_instance_s_own_config_change_is_reverted_with_its_code_and_named(self):
        def both(broker):
            r = broker.call("set_config", {"key": "WAKE_INTERVAL_MINUTES", "value": "15"})
            self.assertTrue(r["ok"], r)
            self.operator_edits(OPERATOR)
        self.cycle(FAILING, both)
        config = (self.root / "config.env").read_text()
        self.assertIn("WAKE_INTERVAL_MINUTES=120", config)                 # the instance's line, undone
        self.assertIn(OPERATOR, config)                                     # the operator's line, kept
        reverted = self.journal_entry("Supervisor: code change reverted")
        self.assertIn("config.env (WAKE_INTERVAL_MINUTES)", reverted)
        self.assertEqual(git(self.root, "status", "--porcelain", "--", "config.env"), "")

    def test_the_instance_s_passing_config_change_is_committed_as_its_own_and_the_operator_s_apart(self):
        def both(broker):
            self.assertTrue(broker.call("set_config", {"key": "WAKE_INTERVAL_MINUTES", "value": "15"})["ok"])
            self.operator_edits(OPERATOR)
        self.cycle(PASSING, both)
        config = (self.root / "config.env").read_text()
        self.assertIn('WAKE_INTERVAL_MINUTES="15"', config)
        self.assertIn(OPERATOR, config)
        log = git(self.root, "log", "--format=%H %s", "-n", "4").splitlines()
        by_subject = {line.split(" ", 1)[1]: line.split(" ", 1)[0] for line in log}
        outside = next(h for s, h in by_subject.items() if "changed from outside" in s)
        mine = next(h for s, h in by_subject.items() if s.startswith("kant: live"))
        outside_diff = git(self.root, "show", "--format=", outside, "--", "config.env")
        mine_diff = git(self.root, "show", "--format=", mine, "--", "config.env")
        self.assertIn("+" + OPERATOR, outside_diff)
        self.assertNotIn("+WAKE_INTERVAL_MINUTES", outside_diff)
        self.assertIn('+WAKE_INTERVAL_MINUTES="15"', mine_diff)
        self.assertNotIn("-LLM_MODEL", mine_diff)                           # a probation revert cannot undo his
        self.assertIn("config.env", self.probation.call_args.args[2])
        self.assertEqual(git(self.root, "status", "--porcelain", "--", "config.env"), "")


class SetConfigTest(unittest.TestCase):
    def setUp(self):
        from supervisor.broker import Broker
        self.project = Path(tempfile.mkdtemp(prefix="shaft-setcfg-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.root = self.project / "instances" / "wren"
        self.root.mkdir(parents=True)
        (self.root / "config.env").write_text('# my model\nLLM_MODEL="claude-haiku-5-5"  # wren\nX=1\nX=2\n')
        self.broker = Broker("wren", self.project, {}, self.project / "broker.log")

    def test_sets_the_line_that_counts_and_records_it(self):
        self.assertTrue(self.broker.call("set_config", {"key": "X", "value": "3"})["ok"])
        self.assertTrue(self.broker.call("set_config", {"key": "NEW_KEY", "value": "a b"})["ok"])
        self.assertEqual((self.root / "config.env").read_text(),
                         '# my model\nLLM_MODEL="claude-haiku-5-5"  # wren\nX=1\nX="3"\nNEW_KEY="a b"\n')
        self.assertEqual(self.broker.config_writes, [{"key": "X", "old": "X=2", "new": 'X="3"'},
                                                     {"key": "NEW_KEY", "old": None, "new": 'NEW_KEY="a b"'}])
        from supervisor.core import instance_config
        with mock.patch("supervisor.core.instance_root", return_value=self.root):
            self.assertEqual(instance_config("wren"), {"LLM_MODEL": "claude-haiku-5-5", "X": "3", "NEW_KEY": "a b"})

    def test_refuses_what_would_not_read_back_as_one_value(self):
        before = (self.root / "config.env").read_text()
        for key, value in (("x", "1"), ("A=B", "1"), ("# A", "1"), ("A", 'say "hi"'), ("A", "1\nLLM_MODEL=x"),
                           ("A", "x" * 501)):
            r = self.broker.call("set_config", {"key": key, "value": value})
            self.assertFalse(r["ok"], (key, value))
        self.assertEqual((self.root / "config.env").read_text(), before)
        self.assertEqual(self.broker.config_writes, [])

    def test_undoing_the_instance_s_writes_keeps_a_later_operator_edit_of_the_same_key(self):
        self.broker.call("set_config", {"key": "X", "value": "3"})
        self.broker.call("set_config", {"key": "X", "value": "4"})
        text, back = gate.config_without((self.root / "config.env").read_text(), self.broker.config_writes)
        self.assertEqual(back, ["X"])
        self.assertIn("\nX=2\n", text)
        later = (self.root / "config.env").read_text().replace('X="4"', "X=9")   # Daniele, after the instance
        text, back = gate.config_without(later, self.broker.config_writes)
        self.assertEqual(back, [])
        self.assertIn("\nX=9\n", text)


@unittest.skipUnless(Path(sandbox.SANDBOX_EXEC).exists(), "macOS sandbox")
class SandboxTest(unittest.TestCase):
    def test_the_instance_cannot_write_its_config_env_by_any_path(self):
        # Writing it directly would make its changes indistinguishable from the operator's.
        root = Path(tempfile.mkdtemp(prefix="shaft-sbcfg-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "config.env").write_text("A=1\n")
        prof = root / ".p.sb"
        prof.write_text(sandbox.profile(root, Path.home(), []))
        attempts = ["open('config.env', 'w').write('A=2')", "os.unlink('config.env')",
                    "os.rename('config.env', 'x')", "open('t', 'w').write('A=2'); os.replace('t', 'config.env')",
                    "os.link('config.env', 'h'); open('h', 'a').write('A=2')"]
        for code in attempts:
            r = subprocess.run(sandbox.wrap(prof, [sys.executable, "-c", "import os\n" + code]), cwd=root,
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, code)
        self.assertEqual((root / "config.env").read_text(), "A=1\n")
        ok = subprocess.run(sandbox.wrap(prof, [sys.executable, "-c", "open('notes.md', 'w').write('fine')"]),
                            cwd=root, capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stderr)                        # the rest of its folder still is


if __name__ == "__main__":
    unittest.main()
