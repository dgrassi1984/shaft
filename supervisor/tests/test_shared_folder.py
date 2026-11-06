"""The operator's folder an instance may read (shaft share): read-only, never SHAFT's own state, and kept when
other per-instance settings change (the operator, 2026-10-13: shaft asked for "a clearly scoped folder")."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, sandbox

PROJECT = Path(__file__).resolve().parents[2]
TEMPLATE = PROJECT / "template"
HOME = Path(os.path.expanduser("~")).resolve()


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-sup-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        self.folder = Path(tempfile.mkdtemp(prefix="shaft-shared-")).resolve()
        self.addCleanup(shutil.rmtree, self.folder, True)
        p = mock.patch.object(core, "SUP_DIR", self.sup)
        p.start()
        self.addCleanup(p.stop)

    def test_other_settings_do_not_wipe_the_grant(self):
        # Regression: _save_tools rewrote tools/<name>.json with only its own keys, so switching a tool would
        # silently drop the shared folder.
        core.set_shared_folder("shaft", self.folder)
        core.set_tool("shaft", "delegate_coding", on=False)
        self.assertEqual(core.shared_folder("shaft"), self.folder)
        self.assertEqual(core.disabled_tools("shaft"), {"delegate_coding"})
        core.set_shared_folder("shaft", None)
        self.assertIsNone(core.shared_folder("shaft"))
        self.assertEqual(core.disabled_tools("shaft"), {"delegate_coding"})   # and removing it keeps the rest
        self.assertNotIn("shared", json.loads((self.sup / "tools" / "shaft.json").read_text()))

    def test_other_settings_do_not_wipe_the_mail_grant(self):
        # The same class of bug for the mail grant (docs/plans/PLAN-mail.md): every setter must keep the keys it does not own.
        grant = [{"account": "work", "label": "AssistantAccess"}]
        core.set_mail_grants("shaft", grant)
        core.set_tool("shaft", "delegate_coding", on=False)
        core.set_shared_folder("shaft", self.folder)
        core.set_shared_folder("shaft", None)
        core.set_tool("shaft", "delegate_coding", on=True)
        self.assertEqual(core.mail_grants("shaft"), grant)
        core.set_mail_grants("shaft", [])
        self.assertEqual(core.disabled_tools("shaft"), set())
        self.assertNotIn("mail", json.loads((self.sup / "tools" / "shaft.json").read_text()))
        core.set_mail_grants("shaft", grant)
        core.set_shared_folder("shaft", self.folder)
        core.set_mail_grants("shaft", [])
        self.assertEqual(core.shared_folder("shaft"), self.folder)   # and clearing mail keeps the folder

    def test_per_instance(self):
        core.set_shared_folder("shaft", self.folder)
        self.assertIsNone(core.shared_folder("wren"))

    def test_shaft_state_is_never_shared(self):
        for bad in (core.PROJECT, core.PROJECT / "instances", core.PROJECT / "supervisor", core.SUP_DIR, HOME,
                    HOME / "Library" / "Application Support" / "SHAFT"):
            if bad.is_dir():
                with self.assertRaises(ValueError, msg=str(bad)):
                    core.set_shared_folder("shaft", bad)
        with self.assertRaises(ValueError):
            core.set_shared_folder("shaft", self.folder / "missing")
        self.assertIsNone(core.shared_folder("shaft"))

    def test_broker_carries_it_for_the_instance_and_replays_never_for_the_battery(self):
        from supervisor.broker import Broker
        core.set_shared_folder("shaft", self.folder)
        self.assertEqual(Broker("shaft", self.sup, {}, self.sup / "b.log").shared, self.folder)
        battery = Broker("shaft-battery", self.sup, {}, self.sup / "b.log", root=self.sup / "ctrl")
        self.assertIsNone(battery.shared)
        replay = Broker("shaft", self.sup, {}, self.sup / "b.log", root=self.sup / "copy", share=True)
        self.assertEqual(replay.shared, self.folder)

    def test_a_folder_gone_since_the_grant_is_left_out_and_logged(self):
        from supervisor.broker import Broker
        core.set_shared_folder("shaft", self.folder)
        shutil.rmtree(self.folder)
        with mock.patch.object(core, "log") as log:
            self.assertIsNone(Broker("shaft", self.sup, {}, self.sup / "b.log").shared)
        self.assertIn("left out of this run", log.call_args[0][0])

    def test_agent_command_passes_it_to_sandbox_and_environment(self):
        from supervisor import runner
        from supervisor.broker import Broker
        core.set_shared_folder("shaft", self.folder)
        b = Broker("shaft", self.sup, {}, self.sup / "b.log")
        b.start()
        self.addCleanup(b.stop)
        root = self.sup / "inst"
        root.mkdir()
        _, env = runner.agent_command(root, b, ["live"], self.sup / "p.sb", [8780], {})
        self.assertEqual(env["SHAFT_SHARED"], str(self.folder))
        self.assertIn(f'(allow file-read* (subpath "{self.folder}"))', (self.sup / "p.sb").read_text())
        core.set_shared_folder("shaft", None)
        b = Broker("shaft", self.sup, {}, self.sup / "b.log")
        b.start()
        self.addCleanup(b.stop)
        _, env = runner.agent_command(root, b, ["live"], self.sup / "p.sb", [8780], {})
        self.assertNotIn("SHAFT_SHARED", env)
        self.assertNotIn(str(self.folder), (self.sup / "p.sb").read_text())


class SandboxTest(unittest.TestCase):
    """Run real sandboxed processes: the folder is readable, never writable, and secrets/ stays out of reach."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-share-", dir=PROJECT / ".supervisor")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root, self.shared = self.tmp / "inst", self.tmp / "operator"
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "notes").mkdir()
        (self.root / "secrets" / "secrets.json").write_text("TOPSECRET")
        self.shared.mkdir()
        (self.shared / "plan.md").write_text("the operator's plan")

    def sandboxed(self, prof: str, script: str) -> str:
        path = self.tmp / "p.sb"
        path.write_text(prof)
        r = subprocess.run(sandbox.wrap(path, [core.PYTHON, "-E", "-s", "-c", script]), cwd=self.root,
                           capture_output=True, text=True)
        return r.stdout + r.stderr

    def probe(self) -> str:
        return ("import pathlib\n"
                f"S = pathlib.Path({str(self.shared)!r})\n"
                "for label, f in (('READ', lambda: (S / 'plan.md').read_text()),\n"
                "                 ('WRITE', lambda: (S / 'new.md').write_text('x')),\n"
                "                 ('SECRET', lambda: open('secrets/secrets.json').read())):\n"
                "    try: print(label, 'OK', f())\n"
                "    except PermissionError: print(label, 'DENIED')\n")

    def test_agent_profile(self):
        out = self.sandboxed(sandbox.profile(self.root, HOME, [8780], shared=self.shared), self.probe())
        self.assertIn("READ OK the operator's plan", out)
        self.assertIn("WRITE DENIED", out)
        self.assertIn("SECRET DENIED", out)
        self.assertFalse((self.shared / "new.md").exists())

    def test_run_profile(self):
        out = self.sandboxed(sandbox.run_profile(self.root, HOME, shared=self.shared), self.probe())
        self.assertIn("READ OK the operator's plan", out)
        self.assertIn("WRITE DENIED", out)
        self.assertIn("SECRET DENIED", out)

    def test_without_a_grant_nothing_changes(self):
        self.assertEqual(sandbox.profile(self.root, HOME, [8780]), sandbox.profile(self.root, HOME, [8780], shared=None))
        out = self.sandboxed(sandbox.profile(self.root, HOME, [8780]), self.probe())
        self.assertIn("READ DENIED", out)
        out = self.sandboxed(sandbox.run_profile(self.root, HOME), self.probe())
        self.assertIn("READ DENIED", out)


class InstanceToolsTest(unittest.TestCase):
    """The instance's file tools see the folder as shared/: readable, listable, never writable, no way out."""

    def setUp(self):
        sys.path.insert(0, str(TEMPLATE))
        self.addCleanup(sys.path.remove, str(TEMPLATE))
        self.root = Path(tempfile.mkdtemp(prefix="shaft-inst-")).resolve()
        self.shared = Path(tempfile.mkdtemp(prefix="shaft-op-")).resolve()
        self.outside = Path(tempfile.mkdtemp(prefix="shaft-out-")).resolve()
        for d in (self.root, self.shared, self.outside):
            self.addCleanup(shutil.rmtree, d, True)
        (self.root / "notes").mkdir()
        (self.shared / "docs").mkdir()
        (self.shared / "docs" / "plan.md").write_text("the operator's plan")
        (self.outside / "private.md").write_text("PRIVATE")
        (self.shared / "escape").symlink_to(self.outside)

    def box(self, shared: Path | None):
        from shaft.tools import Toolbox
        env = {"SHAFT_SHARED": str(shared)} if shared else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if not shared:
                os.environ.pop("SHAFT_SHARED", None)
            return Toolbox(self.root)

    def test_read_and_list(self):
        box = self.box(self.shared)
        self.assertEqual(box.run("read_file", json.dumps({"path": "shared/docs/plan.md"})), "the operator's plan")
        listing = box.run("list_files", json.dumps({"path": "shared"}))
        self.assertIn("shared/docs/plan.md", listing)
        self.assertIn("shared/ (the operator's folder, read-only", box.run("list_files", "{}"))

    def test_never_written(self):
        box = self.box(self.shared)
        for tool in ("write_file", "append_file"):
            out = box.run(tool, json.dumps({"path": "shared/docs/plan.md", "content": "overwritten"}))
            self.assertIn("read-only", out)
        self.assertEqual((self.shared / "docs" / "plan.md").read_text(), "the operator's plan")

    def test_no_way_out(self):
        box = self.box(self.shared)
        self.assertIn("outside the shared folder", box.run("read_file", json.dumps({"path": "shared/../../etc/hosts"})))
        self.assertIn("outside the shared folder", box.run("read_file", json.dumps({"path": "shared/escape/private.md"})))

    def test_without_a_grant_it_says_there_is_none(self):
        # Regression: with the grant taken back, shared/ listed "(empty)" and read_file said "No such file" in my
        # own folder; shaft could not tell why and repeated the same read until the loop guard stopped it.
        box = self.box(None)
        for tool, args in (("list_files", {"path": "shared/"}), ("read_file", {"path": "shared/docs/plan.md"}),
                           ("write_file", {"path": "shared/x.md", "content": "mine"})):
            self.assertIn("no shared folder now", box.run(tool, json.dumps(args)), tool)
        self.assertFalse((self.root / "shared").exists())
        self.assertNotIn("operator's folder", box.run("list_files", "{}"))

    def test_without_a_grant_my_own_shared_dir_is_mine(self):
        (self.root / "shared").mkdir()
        box = self.box(None)
        self.assertIn("wrote", box.run("write_file", json.dumps({"path": "shared/x.md", "content": "mine"})))
        self.assertEqual(box.run("read_file", json.dumps({"path": "shared/x.md"})), "mine")

    def test_the_prompt_mentions_it_only_when_granted(self):
        code = ("from shaft import prompts, config; from shaft.state import State; from pathlib import Path; import os\n"
                "root = Path(os.environ['SHAFT_ROOT']); cfg = config.load(root, environ={}); s = State(root)\n"
                "print('SHARED' if 'shared/ is a folder' in prompts.system_prompt(s, cfg, 'm', 'built-in: read_file, run') "
                "else 'NONE')")
        (self.root / "config.env").write_text('LLM_BASE_URL="http://127.0.0.1:1/v1"\nLLM_MODEL="m"\n')
        env = {"PATH": "/usr/bin:/bin", "SHAFT_ROOT": str(self.root)}
        run = lambda e: subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=TEMPLATE, capture_output=True,
                                       text=True, env=e)
        with_it = run({**env, "SHAFT_SHARED": str(self.shared)})
        self.assertEqual(with_it.stdout.strip(), "SHARED", with_it.stderr)
        self.assertEqual(run(env).stdout.strip(), "NONE")


if __name__ == "__main__":
    unittest.main()
