"""A line of an instance's Markdown shown as text — the rail, a push, the Overview and Messages lists, a transcript's
summary, a journal heading — is the text the page would show, not the Markdown source (found in the smoke test of the
integrated build, 2026-10-25: the rail read 'Finished the **smoke** reply.'). And the documents drawer, once closed,
leaves the chat the whole width whatever the panel holds."""
from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import messages, panel, web_alerts, web_chat, web_md, web_pages, web_views

CSS = re.sub(r"/\*.*?\*/", "", (Path(__file__).resolve().parents[1] / "web_static" / "web.css").read_text(), flags=re.S)
REPLY = "```python\nx = 1\n```\n\n## The **plan**: `make` it\nmore"


class FirstLineTest(unittest.TestCase):
    def test_the_first_line_is_the_text_shown(self):
        self.assertEqual(web_md.first_line(REPLY), "x = 1")                 # a fence's marker is not its text
        self.assertEqual(web_md.first_line("\n---\n| a | b |\n|---|---|\nFinished the **smoke** reply."), "a\tb")
        self.assertEqual(web_md.first_line("> quoted\nmine **too**", quotes=False), "mine too")
        self.assertEqual(web_md.first_line("   \n"), "")


class SummariesTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-lines-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        self.rows = []
        history = mock.patch.object(messages, "history", side_effect=lambda: self.rows)
        history.start()
        self.addCleanup(history.stop)
        p = mock.patch.object(web_chat, "SUP_DIR", self.sup)
        p.start()
        self.addCleanup(p.stop)

    def chat(self, content: str) -> dict:
        """Since 2026-11-11 the instance row summarizes Main, so seed its own conversation."""
        chat = {"id": "web-0123456789abcdef", "instance": "wisp", "kind": "from", "created": "2026-10-25T10:00:00", "state": "idle",
                "turns": [{"role": "user", "content": "q", "ts": "2026-10-25T10:00:00"},
                          {"role": "assistant", "content": content, "ts": "2026-10-25T10:00:05"}], "reply": None}
        web_chat.save(chat)
        return chat

    def test_the_rail_shows_the_reply_as_text(self):
        self.chat("Finished the **smoke** reply, see [the plan](https://a.example) <b>")
        self.assertEqual(web_chat.summary("wisp")["line"], "Finished the smoke reply, see the plan <b>")
        rail = str(web_pages.rail(["wisp"], "", {"instances": []}, {"wisp": web_chat.summary("wisp")}))
        self.assertIn("Finished the smoke reply, see the plan &lt;b&gt;", rail)        # escaped once, as text
        sent = {"ts": "2026-10-25T11:00:00", "from": "wisp", "to": "operator", "file": "m.md", "read": False,
                "text": "## Question: **which** one?"}
        self.rows = [dict(sent, talk=None)]
        self.assertEqual(web_chat.summary("wisp", [sent])["line"], "Question: which one?")

    def test_rail_lines_cut_at_80_characters_end_in_an_ellipsis(self):
        for count in (79, 80, 81):
            with self.subTest(count=count):
                self.rows = []
                text = "x" * count
                expected = "x" * min(count, 80) + ("…" if count > 80 else "")
                chat = self.chat(text)
                self.assertEqual(web_chat.summary("wisp")["line"], expected)
                chat["turns"][-1].update(role="user", content=text)
                web_chat.save(chat)
                self.assertEqual(web_chat.summary("wisp")["line"], ("you: " + text)[:80] + "…")
                self.assertEqual(web_chat.excerpt({"turns": [{"role": "user", "content": text}]}), (expected, 1))
                sent = {"ts": "2026-10-25T11:00:00", "from": "wisp", "to": "operator", "file": "m.md", "read": False, "text": text}
                self.rows = [dict(sent, talk=None)]
                self.assertEqual(web_chat.summary("wisp", [sent])["line"], expected)
                with self.assertRaisesRegex(web_chat.Refused, "From is not cleared"):
                    web_chat.clear("wisp", "from")
                self.assertEqual(web_chat.summary("wisp", [sent])["line"], expected)
                with mock.patch.object(web_chat, "from_chat", return_value=None):
                    self.assertEqual(web_chat.summary("wisp", [sent])["line"], expected)

    def test_a_push_says_it_as_text(self):
        self.assertEqual(web_chat.reply_event(self.chat(REPLY))["body"], "x = 1")
        self.assertEqual(web_chat.reply_event(self.chat("**Done**: `3` files"))["body"], "Done: 3 files")
        self.assertEqual(web_alerts.event({"from": "wisp", "file": "m.md", "text": "- [ ] **check** this"})["body"],
                         "☐ check this")

    def test_the_overview_and_messages_lists_show_text(self):
        m = {"ts": "2026-10-25T11:00:00", "from": "wisp", "to": "operator", "file": "m.md", "read": True,
             "kind": "note", "text": "**Heads up**: `deploy` *now*"}
        self.assertIn(">Heads up: deploy now<", str(web_pages.message_line(m)))
        page = str(web_views.messages_page([m], "", ["wisp", "operator"], ["wisp"], "t"))
        self.assertIn("Heads up: deploy now", page)
        self.assertNotIn("**Heads up**", page)

    def test_a_transcript_summary_and_a_journal_heading_show_text(self):
        e = {"kind": "reflect", "record": {"steps": 0, "tools": [], "ended_by": "done",
                                            "content": "## Today\nI **fixed** the `gate`"}}
        self.assertEqual(panel.transcript_summary(e), "0 tools · done · Today I fixed the gate")
        self.assertEqual(panel.journal_heading("# 2026-10-25 — **fixed** the `gate`\nbody"), "2026-10-25 — fixed the gate")


class ClosedDrawerTest(unittest.TestCase):
    def test_closed_wins_over_every_rule_that_makes_two_columns(self):
        """The bug: .chatgrid:has(.doclist a) — a list in the drawer — outranked .chatgrid.hidedocs (a :has() takes
        the specificity of its argument), so a closed drawer showing its list kept the chat at half width. Every rule
        giving the grid a second column must leave a closed drawer out (:not(.hidedocs))."""
        rules = re.findall(r"([^{}]*\.chatgrid[^{}]*)\{([^}]*grid-template-columns:[^;}]*minmax\(18rem[^}]*)\}", CSS)
        self.assertTrue(rules)
        for selectors, _ in rules:
            for sel in selectors.split(","):
                sel = sel.strip()
                if ".showdocs" in sel:                     # opened on purpose: .showdocs and .hidedocs never coexist
                    continue
                self.assertIn(":not(.hidedocs)", sel, sel)
        for sel in re.findall(r"([^{}]*\.chatgrid[^{}]*)\{[^}]*display: none", CSS):
            for one in sel.split(","):
                if "aside.docs" in one and ":has(" in one:
                    self.assertIn(":not(.showdocs)", one, one)   # only ever hides: no :has() can show it when closed


if __name__ == "__main__":
    unittest.main()
