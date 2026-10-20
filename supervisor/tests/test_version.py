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

    def test_issue_61_describe_and_cache_follow_commit(self):
        """#61: reverting loses tagged labels, dirty markers, or caches HEAD after it moves."""
        version.commit_describe.cache_clear()
        self.addCleanup(version.commit_describe.cache_clear)
        with mock.patch.object(version, "git", side_effect=[done("v0.2.0-60-gabc1234\n"),
                                                           done("40f64d7\n")]) as git:
            self.assertEqual(version.commit_describe("abc1234+changes"), "v0.2.0-60-gabc1234+changes")
            self.assertEqual(version.commit_describe("abc1234+changes"), "v0.2.0-60-gabc1234+changes")
            self.assertEqual(version.commit_describe("40f64d7"), "40f64d7")
        self.assertEqual(git.call_args_list, [
            mock.call("describe", "--tags", "--always", "abc1234", check=False),
            mock.call("describe", "--tags", "--always", "40f64d7", check=False)])

    def test_issue_61_record_keeps_startup_description(self):
        """#61: reverting drops the saved version or changes it when a later knob is read."""
        from supervisor import core
        with mock.patch.object(version, "supervisor_commit", return_value="abc1234+changes"), \
             mock.patch.object(version, "commit_describe", return_value="v0.2.0-60-gabc1234+changes"), \
             mock.patch.object(core, "_on_knob", None):
            version.record_service("web", {})
            before = version.service("web", stale=False)
            self.assertEqual(before["describe"], "v0.2.0-60-gabc1234+changes")
            with mock.patch.object(version, "supervisor_commit", return_value="def5678"):
                core._on_knob()
            self.assertEqual(version.service("web", stale=False), before)

    def test_issue_61_service_versions_include_scheduler_and_legacy_records(self):
        """#61: reverting hides scheduler details, its stale flag, and versions for old or absent records."""
        from supervisor import dashboard, panel
        rec = {"pid": os.getpid(), "commit": "abc1234+changes", "started": "2026-10-20T15:25:00"}
        def record(name, stale=True):
            return {**rec, "stale": True, **({"describe": "v0.1.0"} if name == "api" else {})} \
                if name in ("api", "scheduler") else None
        with mock.patch.object(version, "service", side_effect=record), \
             mock.patch.object(version, "commit_describe", return_value="v0.2.0-16-gabc1234+changes"), \
             mock.patch.object(dashboard, "_scheduler_running", return_value=True), \
             mock.patch.object(panel, "model_down_problem", return_value=""):
            rows = {s["name"]: s for s in panel.services_detail()}
        self.assertEqual(rows["scheduler"]["version"], "v0.2.0-16-gabc1234+changes")
        self.assertEqual(rows["scheduler"]["detail"], "abc1234+changes since 15:25")
        self.assertTrue(rows["scheduler"]["stale"])
        self.assertTrue(rows["scheduler"]["running"])
        self.assertEqual(rows["api"]["version"], "v0.1.0")
        self.assertEqual(rows["embed"]["version"], "unknown — restart to record")

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
        from supervisor import core
        with mock.patch.object(core, "instance_root", return_value=root), \
             mock.patch.object(version, "instance", return_value={"commit": "x", "template_behind": 0}), \
             mock.patch.object(version, "supervisor_commit", return_value="abc1234"):
            line = version.describe("wren")
        self.assertIn("claude-haiku-5-5 via the broker", line)
        self.assertNotIn("model server", line)


if __name__ == "__main__":
    unittest.main()
