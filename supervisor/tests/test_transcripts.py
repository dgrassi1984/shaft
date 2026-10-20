"""`shaft top`'s transcripts tab (the operator, 2026-10-14: "look into each instance's transcripts, both operator-facing
and cycles"): every finished cycle, sleep and reflection, and every conversation with its turns, tools and write-up,
read from the instance's memory/episodes/*.jsonl."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import panel

try:
    from rich.console import Console

    from supervisor import top, top_views
except ModuleNotFoundError:   # rich lives in .venv-ui
    top = top_views = None

BIG = "x" * (panel.TRANSCRIPT_CUT + 500)
CYCLE = {"ts": "2026-10-14T13:19:08", "kind": "live", "ended_by": "control", "steps": 3, "inbox": [],
         "intent": {"goal": "Verify the template changes", "success_criterion": "notes/how-i-work.md names all three",
                    "confidence": 80, "origin": "self"},
         "tools": [{"tool": "read_file", "args": "{\"path\": \"shaft/tools.py\"}", "result": BIG},
                   {"tool": "run", "args": {"command": "ls"}, "result": "exit 0"}],
         "finish": {"what_happened": "verified all three", "criterion_met": "yes", "learned": "check the source"}}
TALK = [
    {"ts": "2026-10-14T12:56:01", "kind": "talk", "talk_id": "t1", "role": "user", "via": "Telegram",
     "content": "Share your findings with shaft"},
    {"ts": "2026-10-14T12:58:07", "kind": "talk", "talk_id": "t1", "role": "assistant", "via": "Telegram",
     "content": "Sent — one message to shaft.", "reasoning": "he is on a smaller model, keep it short",
     "tools": [{"tool": "send_message", "args": "{\"to\": \"shaft\"}", "result": "delivered to shaft"}]},
    {"ts": "2026-10-14T13:26:30", "kind": "talk_recovered", "talk_id": "t1",
     "content": "Conversation with the operator: shared my notes with shaft."},
]
SLEEP = {"ts": "2026-10-14T12:54:00", "kind": "sleep", "ended_by": "answer", "tools": [],
         "content": "Sleep journal — consolidated 23 facts into 18"}


class TranscriptData(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-tx-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        rows = [SLEEP, *TALK[:2], CYCLE, TALK[2]]
        ep = self.root / "memory" / "episodes" / "2026-10-14.jsonl"
        ep.parent.mkdir(parents=True)
        ep.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
        for p in (mock.patch.object(panel, "instance_root", lambda name: self.root),
                  mock.patch.object(panel, "instance_config", lambda name: {"OPERATOR_NAME": "the operator"})):
            p.start()
            self.addCleanup(p.stop)
        panel._memo.clear()

    def test_cycles_and_conversations_newest_first_turns_grouped(self):
        es = panel.transcripts("wren")
        self.assertEqual([e["kind"] for e in es], ["cycle", "conversation", "sleep"])
        talk = es[1]
        self.assertEqual(len(talk["turns"]), 2)
        self.assertEqual(len(talk["writeups"]), 1)
        self.assertEqual(talk["last"], "2026-10-14T12:58:07")   # its last turn, not the write-up at 13:26
        self.assertIn("2 turns · via Telegram → 12:58 · written up · Share your findings", panel.transcript_summary(talk))
        self.assertIn("Verify the template changes", panel.transcript_summary(es[0]))
        self.assertTrue(panel.transcript_summary(es[0]).startswith("yes"))

    def test_a_cycle_in_full(self):
        cycle = panel.transcripts("wren")[0]
        text = "\n".join(l for l, _ in panel.transcript_lines("wren", cycle))
        for part in ("Verify the template changes", "notes/how-i-work.md names all three", "▸ 1. read_file",
                     "\"path\": \"shaft/tools.py\"", "▸ 2. run", "exit 0", "Finish · what happened", "verified all three",
                     "Finish · criterion met", "check the source"):
            self.assertIn(part, text)
        self.assertIn(f"[… 500 more chars, in full at {cycle['where']}]", text)   # a cut says where the rest is
        self.assertNotIn(BIG, text)

    def test_a_conversation_in_full_with_thinking_on_request(self):
        talk = panel.transcripts("wren")[1]
        text = "\n".join(l for l, _ in panel.transcript_lines("wren", talk))
        self.assertIn("── 12:56:01 the operator via Telegram", text)
        self.assertIn("── 12:58:07 wren via Telegram", text)
        self.assertIn("▸ 1. send_message", text)
        self.assertIn("shared my notes with shaft", text)                   # the write-up
        self.assertNotIn("keep it short", text)
        thinking = "\n".join(l for l, _ in panel.transcript_lines("wren", talk, thinking=True))
        self.assertIn("(thinking) he is on a smaller model, keep it short", thinking)


@unittest.skipIf(top is None, "needs rich (.venv-ui/bin/python -m unittest supervisor.tests.test_transcripts)")
class TranscriptView(TranscriptData):
    def snap(self):
        return {"at": "15:00:00", "services": [], "events": [], "messages": [], "alerts": [], "spend": 0.0,
                "instances": [{"name": "wren", "state": "in a cycle", "since": "15:00"}]}

    def text(self, ui, width=100, height=30):
        c = Console(width=width, record=True, file=io.StringIO())
        c.print(top.render(self.snap(), ui, width, height))
        return c.export_text()

    def keys(self, ui, *ks):
        for k in ks:
            top.dispatch(ui, k, self.snap(), 10, lambda n: "")

    def test_zero_opens_the_tab_and_enter_reads_one_in_full(self):
        ui = top.UI()
        self.keys(ui, "0")
        out = self.text(ui)
        self.assertEqual(ui.tab, "transcripts")
        self.assertIn("3 transcripts", out)
        self.assertIn("the cycle running now (since 15:00) appears when it ends", out)
        self.keys(ui, "down", "enter")
        out = self.text(ui)
        self.assertIn("── 12:56:01 the operator via Telegram", out)
        self.assertIn("t thinking on", out)
        self.keys(ui, "t")
        self.assertIn("(thinking) he is on a smaller model", self.text(ui))
        self.keys(ui, "esc")
        self.assertFalse(ui.detail)

    def test_f_filters_cycles_then_conversations(self):
        ui = top.UI(tab="transcripts")
        self.keys(ui, "f")
        self.assertIn("2 cycles", self.text(ui))
        self.keys(ui, "f")
        out = self.text(ui)
        self.assertIn("1 conversations", out)
        self.assertNotIn("Verify the template", out)
        self.keys(ui, "f")
        self.assertIn("3 transcripts", self.text(ui))

    def test_instance_written_control_characters_never_reach_the_terminal(self):
        ep = self.root / "memory" / "episodes" / "2026-10-14.jsonl"
        evil = {**CYCLE, "ts": "2026-10-14T14:00:00", "intent": {"goal": "hi \x1b]0;pwned\x07 \x1b[2J there"}}
        ep.write_text(ep.read_text() + json.dumps(evil) + "\n")
        panel._memo.clear()
        ui = top.UI(tab="transcripts")
        listing = self.text(ui)
        self.keys(ui, "enter")
        for out in (listing, self.text(ui)):
            self.assertNotIn("\x1b", out)
            self.assertIn("there", out)

    def test_fits_80_columns_with_every_tab_reachable(self):
        out = self.text(top.UI(tab="transcripts"), width=80, height=20)
        self.assertTrue(all(len(l) <= 80 for l in out.splitlines()))
        self.assertIn("0 tx", out.splitlines()[0])


if __name__ == "__main__":
    unittest.main()
