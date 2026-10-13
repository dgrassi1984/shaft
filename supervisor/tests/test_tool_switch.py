"""Supervisor tools switched on/off per instance by the operator (Daniele: wren without delegate_coding)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from supervisor import core

TEMPLATE = Path(__file__).resolve().parents[2] / "template"


class ToolSwitchTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-sup-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        p = mock.patch.object(core, "SUP_DIR", self.sup)
        p.start()
        self.addCleanup(p.stop)

    def broker(self, name):
        from supervisor.broker import Broker
        b = Broker(name, self.sup, {}, self.sup / "broker.log")
        base = b.start()
        self.addCleanup(b.stop)
        req = urllib.request.Request(base + "/tools", headers={"X-Shaft-Token": b.token})
        return b, [t["function"]["name"] for t in json.load(urllib.request.urlopen(req, timeout=5))["tools"]]

    def test_switched_off_is_neither_offered_nor_callable(self):
        core.set_tool("wren", "delegate_coding", on=False)
        b, offered = self.broker("wren")
        self.assertNotIn("delegate_coding", offered)
        self.assertIn("web_search", offered)
        out = b.call("delegate_coding", {"task": "add a plugin"})
        self.assertFalse(out["ok"])
        self.assertIn("switched it off", out["error"])
        _, others = self.broker("shaft")              # per instance: shaft keeps it
        self.assertIn("delegate_coding", others)

    def test_switched_back_on(self):
        core.set_tool("wren", "delegate_coding", on=False)
        core.set_tool("wren", "delegate_coding", on=True)
        self.assertEqual(core.disabled_tools("wren"), set())
        self.assertIn("delegate_coding", self.broker("wren")[1])

    def test_the_prompt_urges_delegation_only_when_the_tool_is_there(self):
        code = ("from shaft import prompts, config; from shaft.state import State; from pathlib import Path; import os\n"
                "root = Path(os.environ['SHAFT_ROOT']); cfg = config.load(root, environ={}); s = State(root)\n"
                "print('WITH', 'use delegate_coding' in prompts.system_prompt(s, cfg, 'm', 'supervisor: run, delegate_coding'))\n"
                "print('WITHOUT', 'use delegate_coding' in prompts.system_prompt(s, cfg, 'm', 'supervisor: run, web_search'))")
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        (root / "config.env").write_text('LLM_BASE_URL="http://127.0.0.1:1/v1"\nLLM_MODEL="m"\n')
        r = subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=TEMPLATE, capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "SHAFT_ROOT": str(root)})
        self.assertEqual(r.stdout.split(), ["WITH", "True", "WITHOUT", "False"], r.stderr)


if __name__ == "__main__":
    unittest.main()
