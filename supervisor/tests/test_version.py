"""Which code is running: a service is 'older' only if its own files changed since it started."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import version


def done(out: str = "", code: int = 0):
    return subprocess.CompletedProcess([], code, stdout=out, stderr="")


class VersionTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shaft-svc-"))
        p = mock.patch.object(version, "SERVICES", self.dir)
        p.start()
        self.addCleanup(p.stop)
        version.SERVICES.mkdir(exist_ok=True)
        (self.dir / "model.json").write_text(json.dumps(
            {"pid": os.getpid(), "commit": "abc1234", "started": "2026-10-12T22:49:02",
             "settings": {"temp": 0.1, "think_budget": 4096}}))

    def test_unrelated_commits_do_not_flag_the_model_server(self):
        with mock.patch.object(version, "git", return_value=done("")) as git:
            self.assertFalse(version.service("model")["stale"])
        self.assertIn("supervisor/bonsai_server.py", git.call_args.args)

    def test_a_change_to_its_code_flags_it(self):
        with mock.patch.object(version, "git", return_value=done("supervisor/bonsai_server.py\n")):
            self.assertTrue(version.service("model")["stale"])

    def test_dead_service_is_not_running(self):
        (self.dir / "api.json").write_text(json.dumps({"pid": 2 ** 22 + 12345, "commit": "abc1234"}))
        self.assertIsNone(version.service("api"))

    def test_cloud_instance_shows_its_cloud_model(self):
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        (root / "config.env").write_text('LLM_BASE_URL="broker"\nLLM_MODEL="claude-haiku-5-5"  # wren\n')
        with mock.patch.object(version, "instance_root", return_value=root), \
             mock.patch.object(version, "instance", return_value={"commit": "x", "template_behind": 0}), \
             mock.patch.object(version, "supervisor_commit", return_value="abc1234"):
            line = version.describe("wren")
        self.assertIn("claude-haiku-5-5 via the broker", line)
        self.assertNotIn("model server", line)


if __name__ == "__main__":
    unittest.main()
