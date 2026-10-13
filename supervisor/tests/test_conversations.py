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
        broker = mock.Mock(handoffs=[], config_writes=[], counts={})
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

    def test_ctrl_c_cannot_abort_the_checks_after_a_reply(self):
        # Regression (2026-10-13): a Ctrl-C while the gate tested shaft's new plugin aborted the settle halfway —
        # no revert, no commit — and the broken files then blocked every template update.
        import os
        import signal
        done = []

        def settle(*a, **k):
            os.kill(os.getpid(), signal.SIGINT)   # the operator presses Ctrl-C during the checks
            done.append(True)
            return True

        before = signal.getsignal(signal.SIGINT)
        turn = runner.Turn("shaft", self.sup, "talk")
        with mock.patch.object(runner, "_snapshot", return_value={}), mock.patch.object(runner, "_settle", settle):
            turn.begin()
            turn.end()
        self.assertEqual(done, [True])
        self.assertIs(signal.getsignal(signal.SIGINT), before)   # Ctrl-C works again afterwards
        self.assertFalse(runner.is_busy("shaft"))

    def test_crashed_conversation_stops_counting(self):
        folder = self.sup / "conversations" / "shaft"
        folder.mkdir(parents=True)
        (folder / "dead.talk").write_text("")                # nobody holds its lock
        self.assertEqual(runner.open_conversations("shaft"), {})
        self.assertFalse((folder / "dead.talk").exists())

    def test_api_conversation_is_open_until_the_api_closes_it(self):
        # The API alone decides when its conversation ended (its idle time); a reader with another
        # SHAFT_IDLE_MINUTES (a test shell's 2) once wrote up a conversation still going on.
        now = time.time()
        (self.sup / "api_talks.json").write_text(json.dumps({
            "a": {"talk_id": "api-recent", "model": "shaft", "last": now - 60, "written": False},
            "b": {"talk_id": "api-long-quiet", "model": "shaft", "last": now - 10 * 3600, "written": False},
            "c": {"talk_id": "api-done", "model": "shaft", "last": now, "written": True},
            "d": {"talk_id": "api-wren", "model": "wren", "last": now, "written": False}}))
        with mock.patch("supervisor.version.service", return_value={"pid": 1}):
            self.assertEqual(runner.open_conversations("shaft"), {"api-recent": "api", "api-long-quiet": "api"})
        with mock.patch("supervisor.version.service", return_value=None):   # API not running: nobody to close them
            self.assertEqual(runner.open_conversations("shaft"), {})


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
        out = self.run_template(self.CODE + "print(recovery.unwritten_context(s, 'Daniele', exclude='open1'))", self.root)
        self.assertNotIn("hello", out)
        self.assertIn("back again", out)


class SpeakerTest(unittest.TestCase):
    def test_conversation_with_someone_else_is_not_remembered_as_the_operator(self):
        # Regression: Claude Code talked to shaft for Daniele (saying so); the write-up was titled
        # "Conversation with Daniele" and filed "what I learned about Daniele" from what Claude Code did.
        from supervisor.tests.fakellm import FakeLLM
        fake = FakeLLM([{"content": "## 2026-10-13 10:09 — Conversation with Claude Code (2026-10-13 10:09–10:10)\n\n"
                                    "Claude Code added a history tool."}])
        self.addCleanup(fake.close)
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        (root / "memory" / "episodes").mkdir(parents=True)
        (root / "config.env").write_text(f'LLM_BASE_URL="{fake.url}"\nLLM_MODEL="m"\n')
        recs = [{"kind": "talk", "talk_id": "t1", "role": "user", "content": "I added a history tool for you.",
                 "speaker": "Claude Code", "ts": "2026-10-12T23:38:00"},
                {"kind": "talk", "talk_id": "t1", "role": "assistant", "content": "thanks", "ts": "2026-10-12T23:44:00"}]
        (root / "memory" / "episodes" / "2026-10-12.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        r = subprocess.run([sys.executable, "-E", "-s", "-m", "shaft", "recover"], cwd=TEMPLATE,
                           env={"PATH": "/usr/bin:/bin", "SHAFT_ROOT": str(root)}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        journal = (root / "memory" / "journal.md").read_text()
        self.assertIn("Conversation with Claude Code (for Daniele)", journal)
        self.assertNotIn("Conversation with Daniele", journal)
        self.assertEqual(journal.count("## "), 1)   # MiMo opened its text with its own dated heading (wren, 10-13)
        sent = json.dumps(fake.requests[-1]["messages"])
        self.assertIn("do not attribute what Claude Code said or did to Daniele", sent)
        self.assertIn("what you learned about Claude Code", sent)


class JournalHeadingTest(unittest.TestCase):
    def test_section_headings_stay_dated_copies_go(self):
        code = ("from shaft.state import State; from pathlib import Path; import os\n"
                "s = State(Path(os.environ['SHAFT_ROOT']))\n"
                "s.append_journal('Reflection', '## What happened since my last reflection\\n\\nA lot.')\n"
                "s.append_journal('Sleep', '### 2026-10-13T04:40 Sleep cycle\\n\\nConsolidated.')\n"
                "print(s.journal.read_text())")
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        r = subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=TEMPLATE, capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "SHAFT_ROOT": str(root)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("## What happened since my last reflection", r.stdout)
        self.assertNotIn("Sleep cycle", r.stdout)
        self.assertIn("— Sleep\n\nConsolidated.", r.stdout)


if __name__ == "__main__":
    unittest.main()
