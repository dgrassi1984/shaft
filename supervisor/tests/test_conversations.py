"""Open conversations: registered while open, holding the instance only while replying."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from supervisor import runner

TEMPLATE = Path(__file__).resolve().parents[2] / "template"


class OpenConversationTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-sup-"))
        for target, value in ((runner, "SUP_DIR"), (runner, "API_TALKS")):
            p = mock.patch.object(target, value, self.sup if value == "SUP_DIR" else self.sup / "api_talks.json")
            p.start()
            self.addCleanup(p.stop)

    def test_quiet_talk_does_not_hold_the_instance(self):
        # Regression: an open `shaft talk` waiting at the prompt held the run lock for its whole length,
        # so the API (and journal write-ups) could not reach the instance until it was closed.
        seen = {}

        def driver(proc, turn):
            with turn():                                    # start-up: held
                seen["startup"] = runner.is_busy("shaft")
            seen["quiet"] = runner.is_busy("shaft")          # at the prompt: free
            seen["open"] = runner.open_conversations("shaft")
            seen["talking"] = runner.talking()
            with turn():                                    # a reply: held
                seen["reply"] = runner.busy_with("shaft")

        fake_proc = mock.Mock(returncode=0)
        fake_proc.poll.return_value = 0
        broker = mock.Mock(handoffs=[], counts={})
        with mock.patch.object(runner, "instance_root", return_value=self.sup), \
             mock.patch.object(runner, "PROJECT", Path(__file__).resolve().parents[2]), \
             mock.patch.object(runner, "_launch", return_value=(broker, [], {})), \
             mock.patch.object(runner, "_popen", return_value=fake_proc), \
             mock.patch.object(runner, "_snapshot", return_value={}), \
             mock.patch.object(runner, "_settle", return_value=True) as settle, \
             mock.patch("supervisor.core.instance_names", return_value=["shaft"]):
            runner.run_conversation("shaft", "talk", [], "t123", driver)
        self.assertTrue(seen["startup"])
        self.assertFalse(seen["quiet"])
        self.assertEqual(seen["open"], {"t123": "talk"})
        self.assertEqual(seen["talking"], ["shaft"])
        self.assertTrue(seen["reply"].startswith("talk"))
        self.assertEqual(settle.call_count, 2)               # each held stretch is checked and committed
        self.assertEqual(runner.open_conversations("shaft"), {})   # closed: no longer open
        self.assertFalse(runner.is_busy("shaft"))

    def test_crashed_conversation_stops_counting(self):
        folder = self.sup / "conversations" / "shaft"
        folder.mkdir(parents=True)
        (folder / "dead.talk").write_text("")                # nobody holds its lock
        self.assertEqual(runner.open_conversations("shaft"), {})
        self.assertFalse((folder / "dead.talk").exists())

    def test_api_conversation_is_open_until_quiet_for_the_idle_time(self):
        now = time.time()
        (self.sup / "api_talks.json").write_text(json.dumps({
            "a": {"talk_id": "api-recent", "model": "shaft", "last": now - 60, "written": False},
            "b": {"talk_id": "api-quiet", "model": "shaft", "last": now - runner.IDLE_MINUTES * 60 - 5,
                  "written": False},
            "c": {"talk_id": "api-done", "model": "shaft", "last": now, "written": True},
            "d": {"talk_id": "api-wren", "model": "wren", "last": now, "written": False}}))
        self.assertEqual(runner.open_conversations("shaft"), {"api-recent": "api"})


class WriteUpTest(unittest.TestCase):
    """The template's journal write-up (recover) against conversations still going on."""

    def run_template(self, code: str, root: Path, open_talks: str = "") -> str:
        env = {"PATH": "/usr/bin:/bin", "SHAFT_OPEN_TALKS": open_talks, "SHAFT_ROOT": str(root)}
        r = subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=TEMPLATE, env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        ep = self.root / "memory" / "episodes"
        ep.mkdir(parents=True)
        recs = [{"kind": "talk", "talk_id": "open1", "role": "user", "content": "hi", "ts": "2026-10-12T10:00"},
                {"kind": "talk", "talk_id": "open1", "role": "assistant", "content": "hello", "ts": "2026-10-12T10:01"},
                {"kind": "talk", "talk_id": "old", "role": "user", "content": "a", "ts": "2026-10-12T09:00"},
                {"kind": "talk", "talk_id": "old", "role": "assistant", "content": "b", "ts": "2026-10-12T09:01"},
                {"kind": "talk_recovered", "talk_id": "old", "content": "entry"},
                {"kind": "talk", "talk_id": "old", "role": "user", "content": "back again", "ts": "2026-10-12T11:00"},
                {"kind": "talk", "talk_id": "old", "role": "assistant", "content": "yes", "ts": "2026-10-12T11:01"}]
        (ep / "2026-10-12.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))

    CODE = ("from shaft.state import State; from shaft import recovery; from pathlib import Path; import os\n"
            "s = State(Path(os.environ['SHAFT_ROOT']))\n")

    def test_open_conversation_is_not_written_up_and_a_resumed_one_is_not_lost(self):
        # Regression: a write-up closed conversations still going on, and turns after that were never journaled.
        out = self.run_template(self.CODE + "print([(t, [x['content'] for x in turns]) "
                                "for t, turns in recovery.orphan_talks(s)])", self.root, open_talks="open1")
        self.assertEqual(out, "[('old', ['back again', 'yes'])]")

    def test_api_reply_does_not_get_its_own_conversation_twice(self):
        out = self.run_template(self.CODE + "print(recovery.unwritten_context(s, exclude='open1'))", self.root)
        self.assertNotIn("hello", out)
        self.assertIn("back again", out)


if __name__ == "__main__":
    unittest.main()
