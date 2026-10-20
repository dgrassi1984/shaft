"""Instances browse with a shared Chromium and a private profile each (docs/plans/PLAN-browser.md, the operator 2026-10-18: "provide it
a profile on a shared chromium, and leave all the rest to it").

What the supervisor guarantees, and these tests pin:
- `run` and jobs read the shared install and never write it; the agent's own sandbox (which its plugins inherit:
  broker token, model ports) and the gate's do not read it at all (D1);
- `run` and jobs write the instance's profile, and nothing new besides;
- every sandboxed process carries the same browser environment (before, `run` built its own and would have missed it);
- an orphaned Chromium on a profile is killed when the run ends, a live one (a job's) is not;
- the install is swapped in whole, so a failed download leaves the old one working.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import broker, browser, core, sandbox
from supervisor.core import HOME, PROJECT, PYTHON

NAME = "wren"
HAS_SANDBOX = Path(sandbox.SANDBOX_EXEC).exists()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-browser-", dir=PROJECT / ".supervisor")).resolve()   # in HOME
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "project"
        self.root = self.project / "instances" / NAME
        for d in ("notes", ".tmp"):
            (self.root / d).mkdir(parents=True)
        self.browsers = self.tmp / ".browsers"
        (self.browsers / "chromium-1248").mkdir(parents=True)
        (self.browsers / "chromium-1248" / "chrome").write_text("the shared browser")
        (self.browsers / "VERSION").write_text("1.64.0\n")
        for p in (mock.patch.object(browser, "BROWSERS", self.browsers),
                  mock.patch.object(core, "SUP_DIR", self.tmp / "sup")):
            p.start()
            self.addCleanup(p.stop)


@unittest.skipUnless(HAS_SANDBOX, "macOS sandbox")
class SandboxTest(Fixture):
    def sandboxed(self, prof: str) -> str:
        path = self.tmp / "p.sb"
        path.write_text(prof)
        probe = ("import pathlib\n"
                 f"B = pathlib.Path({str(self.browsers)!r})\n"
                 f"P = pathlib.Path({str(self.root / browser.PROFILE)!r})\n"
                 "for label, f in (('READ', lambda: (B / 'chromium-1248' / 'chrome').read_text()),\n"
                 "                 ('WRITE_SHARED', lambda: (B / 'planted').write_text('x')),\n"
                 "                 ('PROFILE', lambda: (P.mkdir(exist_ok=True), (P / 'Cookies').write_text('c'))),\n"
                 "                 ('ELSEWHERE', lambda: pathlib.Path('elsewhere.txt').write_text('x'))):\n"
                 "    try: f(); print(label, 'OK')\n"
                 "    except PermissionError: print(label, 'DENIED')\n")
        r = subprocess.run(sandbox.wrap(path, [PYTHON, "-E", "-s", "-c", probe]), cwd=self.root,
                           capture_output=True, text=True)
        return r.stdout + r.stderr

    def test_run_reads_the_shared_install_and_writes_only_its_profile(self):
        out = self.sandboxed(sandbox.run_profile(self.root, HOME, egress_port=1))
        self.assertIn("READ OK", out)
        self.assertIn("WRITE_SHARED DENIED", out)
        self.assertIn("PROFILE OK", out)
        self.assertIn("ELSEWHERE DENIED", out)
        self.assertFalse((self.browsers / "planted").exists())

    def test_a_job_reads_the_shared_install_and_writes_its_profile(self):
        state = self.root / ".jobs" / "p"
        state.mkdir(parents=True)
        out = self.sandboxed(sandbox.job_profile(self.root, HOME, state, egress_port=1, broker_port=2))
        self.assertIn("READ OK", out)
        self.assertIn("WRITE_SHARED DENIED", out)
        self.assertIn("PROFILE OK", out)
        self.assertIn("ELSEWHERE DENIED", out)

    def test_the_agents_own_sandbox_cannot_read_it(self):
        # D1: plugins run in the agent's sandbox, which carries the broker token and reaches the model ports. A
        # browser started there must fail to launch, not quietly run with that reach.
        out = self.sandboxed(sandbox.profile(self.root, HOME, [8780, 2, 1]))
        self.assertIn("READ DENIED", out)
        self.assertIn("WRITE_SHARED DENIED", out)

    def test_the_gates_sandbox_cannot_read_it(self):
        out = self.sandboxed(sandbox.profile(self.root, HOME, []))   # what gate.run_tests builds
        self.assertIn("READ DENIED", out)


class EnvironmentTest(Fixture):
    def test_every_sandboxed_process_gets_the_same_browser_environment(self):
        b = broker.Broker(NAME, self.project, {}, self.tmp / "broker.log")
        env = b.sandbox_env()
        self.assertEqual(env["PLAYWRIGHT_BROWSERS_PATH"], str(self.browsers))
        self.assertEqual(env["SHAFT_PLAYWRIGHT"], "1.64.0")
        self.assertEqual(env["SHAFT_BROWSER_PROFILE"], str(self.root / ".browser"))
        self.assertIn("HTTPS_PROXY", env)

    @unittest.skipUnless(HAS_SANDBOX, "macOS sandbox")
    def test_run_carries_it(self):
        # Regression: `run` built its environment from the proxy alone, so anything added to sandbox_env missed it.
        b = broker.Broker(NAME, self.project, {}, self.tmp / "broker.log")
        b.start()
        self.addCleanup(b.stop)
        out = b.call("run", {"command": "echo $PLAYWRIGHT_BROWSERS_PATH $SHAFT_PLAYWRIGHT $SHAFT_BROWSER_PROFILE"})
        self.assertIn(f"{self.browsers} 1.64.0 {self.root / '.browser'}", out["result"])

    def test_without_an_install_there_is_no_version_to_pin(self):
        (self.browsers / "VERSION").unlink()
        env = browser.env(self.root)
        self.assertNotIn("SHAFT_PLAYWRIGHT", env)
        self.assertIn("shaft browser install", browser.startup_note())
        self.assertEqual(browser.describe(self.root), "not installed (shaft browser install)")

    def test_the_panel_line(self):
        self.assertIsNone(browser.startup_note())
        (self.browsers / ".links").mkdir()                          # Playwright's own bookkeeping, not a revision
        self.assertEqual(browser.describe_install(), "Playwright 1.64.0 · chromium-1248")
        self.assertEqual(browser.describe(self.root), "Playwright 1.64.0 · no profile yet")
        (self.root / ".browser").mkdir()
        (self.root / ".browser" / "Cookies").write_bytes(b"x" * 3000)
        self.assertEqual(browser.describe(self.root), "Playwright 1.64.0 · profile 3 KB")


class SweepTest(Fixture):
    def spawn(self, orphan: bool) -> int:
        flag = f"--user-data-dir={self.root / '.browser'}"
        sleeper = f"{PYTHON} -c 'import time; time.sleep(60)' {flag}"
        if not orphan:
            p = subprocess.Popen(["/bin/sh", "-c", f"exec {sleeper}"])
            self.addCleanup(lambda: (p.kill(), p.wait()))
            return p.pid
        # The shell exits at once: its child is re-parented to launchd, as a Chromium whose launcher died would be.
        out = subprocess.run(["/bin/sh", "-c", f"{sleeper} >/dev/null 2>&1 & echo $!"], capture_output=True,
                             text=True).stdout
        pid = int(out.strip())
        self.addCleanup(lambda: _kill(pid))
        return pid

    def alive(self, pid: int) -> bool:
        return any(p == pid for p, _, _ in browser._processes())

    def test_an_orphaned_browser_is_killed_a_live_one_is_not(self):
        orphan, owned = self.spawn(orphan=True), self.spawn(orphan=False)
        for _ in range(50):
            if any(p == orphan and pp == 1 for p, pp, _ in browser._processes()):
                break
            time.sleep(0.1)
        with mock.patch.object(core, "log"):
            killed = browser.sweep(self.root)
        self.assertEqual(killed, [orphan])
        time.sleep(0.2)
        self.assertFalse(self.alive(orphan))
        self.assertTrue(self.alive(owned))

    def test_another_instances_profile_is_not_touched(self):
        other = self.project / "instances" / "shaft"
        orphan = self.spawn(orphan=True)
        time.sleep(0.3)
        self.assertEqual(browser.sweep(other), [])
        self.assertTrue(self.alive(orphan))

    def test_the_broker_sweeps_when_it_stops(self):
        b = broker.Broker(NAME, self.project, {}, self.tmp / "broker.log")
        b.start()
        with mock.patch.object(browser, "sweep") as sweep:
            b.stop()
        sweep.assert_called_once_with(b.root)


class NeverTravelsTest(Fixture):
    """The profile holds the instance's sessions and the passwords of its own accounts. The copies made from the live
    folder — the coder's scratch copy and the trial gate's — and the coder model's own file tools never see it, and
    the judge's diff of a replay leaves it out. (Replays are rebuilt from git and the mind store, so it never reaches
    one in the first place.)"""

    def setUp(self):
        super().setUp()
        (self.root / ".browser" / "Default").mkdir(parents=True)
        (self.root / ".browser" / "Default" / "Cookies").write_text("session=SECRET-COOKIE")
        (self.root / ".browser" / "passwords.json").write_text('{"site": "SECRET-PASSWORD"}')
        (self.root / "plugins").mkdir()
        (self.root / "plugins" / "x.py").write_text("x = 1\n")

    def capture_gate(self):
        seen = []

        def run_tests(scratch, home, env_root=None):
            seen.append(sorted(p.relative_to(scratch).as_posix() for p in scratch.rglob("*")))
            return True, ""
        return seen, mock.patch("supervisor.gate.run_tests", side_effect=run_tests)

    def test_the_coders_scratch_copy_has_no_profile(self):
        from supervisor import coder
        seen, patch = self.capture_gate()
        with patch:
            coder.test_in_scratch(self.root, [])
        (files,) = seen
        self.assertIn("plugins/x.py", files)                                     # the copy did happen
        self.assertFalse([f for f in files if f.startswith(".browser")], files)

    def test_the_trial_gate_copy_has_no_profile(self):
        from supervisor import trial
        seen, patch = self.capture_gate()
        with patch, mock.patch.object(trial, "_files", return_value={}):
            trial.gate_check(self.project, NAME, {"id": "t1"})
        (files,) = seen
        self.assertIn("notes", files)
        self.assertFalse([f for f in files if f.startswith(".browser")], files)

    def test_the_coder_model_cannot_list_or_read_it(self):
        from supervisor import coder
        listing = coder._read_only_tool(self.root, "list_files", {})
        self.assertIn("plugins/x.py", listing)
        self.assertNotIn(".browser", listing)
        for path in (".browser/passwords.json", ".browser/Default/Cookies", "notes/../.browser/passwords.json"):
            out = coder._read_only_tool(self.root, "read_file", {"path": path})
            self.assertTrue(out.startswith("ERROR"), out)
            self.assertNotIn("SECRET", out)
        self.assertTrue(coder._read_only_tool(self.root, "list_files", {"path": ".browser"}).startswith("ERROR"))

    def test_the_judges_diff_leaves_it_out(self):
        from supervisor import trial
        files = trial._files_now(self.root)
        self.assertIn("plugins/x.py", files)
        self.assertFalse([f for f in files if f.startswith(".browser")])


@unittest.skipUnless(HAS_SANDBOX, "macOS sandbox")
class FileSizeTest(Fixture):
    def test_run_writes_a_file_as_big_as_playwrights_node(self):
        # Regression: `uv pip install playwright` died in `run` with exit 153 and no message — its bundled node is
        # 122 MB and `run` allowed 100 MB per file. A sparse file: no data is written, the limit still applies.
        b = broker.Broker(NAME, self.project, {}, self.tmp / "broker.log")
        b.start()
        self.addCleanup(b.stop)
        # dd, like uv, does not ignore SIGXFSZ (Python does, and raises "File too large" itself); seek writes no data.
        big = "dd if=/dev/zero of=.tmp/big bs=1 count=0 seek={n} 2>/dev/null && echo WROTE"
        out = b.call("run", {"command": big.format(n=150 * 1024 ** 2)})["result"]
        self.assertIn("WROTE", out)
        out = b.call("run", {"command": big.format(n=broker.RUN_MAX_FILE_BYTES + 1)})["result"]
        self.assertNotIn("WROTE", out)
        self.assertIn("per-file limit: SIGXFSZ", out)                         # said, not a bare exit 153


class ListFilesTest(unittest.TestCase):
    def test_my_root_listing_is_not_drowned_by_the_profile(self):
        # A profile holds hundreds of cache files after a few pages; walked, they filled list_files' 400 rows.
        import sys
        from supervisor.core import TEMPLATE
        sys.path.insert(0, str(TEMPLATE))
        self.addCleanup(sys.path.remove, str(TEMPLATE))
        from shaft.tools import Toolbox
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-")).resolve()
        self.addCleanup(shutil.rmtree, root, True)
        (root / "notes").mkdir()
        (root / "notes" / "plan.md").write_text("x")
        cache = root / ".browser" / "Default" / "Cache"
        cache.mkdir(parents=True)
        for i in range(500):
            (cache / f"f{i}").write_text("")
        listing = Toolbox(root).run("list_files", "{}")
        self.assertIn("notes/plan.md", listing)
        self.assertIn(".browser/ (not listed: list_files .browser)", listing)
        self.assertNotIn("Cache/f1", listing)
        self.assertIn("Default/", Toolbox(root).run("list_files", '{"path": ".browser"}'))   # still reachable


class ReplayTest(Fixture):
    def test_a_replays_browser_gets_the_copys_empty_profile_and_the_refusing_proxy(self):
        # A replay never sends (test_egress.ReplayBrokerTest: its proxy refuses and records every connection). It must
        # not log in as the instance either: its browser's profile is the copy's, never the real one with its cookies.
        import threading
        from datetime import datetime
        state = self.tmp / "copy"
        state.mkdir()
        b = broker.ReplayBroker(NAME, self.project, {}, self.tmp / "broker.log", state_root=state, start_commit="0",
                                as_of=datetime.now(), cache={}, cache_lock=threading.Lock(), billed="x@trial",
                                tools_log=self.tmp / "tools.log")
        env = b.sandbox_env()
        self.assertEqual(env["SHAFT_BROWSER_PROFILE"], str(state / ".browser"))
        self.assertNotIn(str(self.root), env["SHAFT_BROWSER_PROFILE"])
        self.assertEqual(env["HTTPS_PROXY"], b.egress.url)
        self.assertIsNotNone(b.egress.refuse("example.com", 443))   # every destination refused


def _kill(pid: int) -> None:
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass


class InstallTest(Fixture):
    def fake_install(self, code: int):
        def run(cmd, env):
            Path(env["PLAYWRIGHT_BROWSERS_PATH"], "chromium-1300").mkdir(parents=True)
            return subprocess.CompletedProcess(cmd, code)
        return run

    def test_a_finished_install_replaces_the_old_one_whole(self):
        with mock.patch.object(browser.subprocess, "run", side_effect=self.fake_install(0)) as run, \
                mock.patch("supervisor.pyenv.uv_binary", return_value="/x/uv"), \
                mock.patch.object(core, "log"), mock.patch("builtins.print"):
            self.assertEqual(browser.install("1.70.0"), 0)
        self.assertIn("playwright==1.70.0", run.call_args[0][0])
        self.assertEqual(browser.version(), "1.70.0")
        self.assertEqual(sorted(p.name for p in self.browsers.iterdir()), ["VERSION", "chromium-1300"])   # no 1248
        self.assertFalse(self.browsers.with_name(".browsers.new").exists())
        self.assertFalse(self.browsers.with_name(".browsers.old").exists())

    def test_a_failed_install_leaves_the_old_one_working(self):
        with mock.patch.object(browser.subprocess, "run", side_effect=self.fake_install(1)), \
                mock.patch("supervisor.pyenv.uv_binary", return_value="/x/uv"), mock.patch("builtins.print"):
            self.assertEqual(browser.install("1.70.0"), 1)
        self.assertEqual(browser.version(), "1.64.0")
        self.assertTrue((self.browsers / "chromium-1248" / "chrome").exists())
        self.assertFalse(self.browsers.with_name(".browsers.new").exists())


if __name__ == "__main__":
    unittest.main()
