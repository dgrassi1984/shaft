"""Later answers return to their thread, as the same conversation (the operator, 2026-11-10).

The case it comes from (2026-11-10): a reply out of budget queued "finish reading, then give your view" at 21:12;
a cycle sent the verdict at 21:15 — into the From feed, written blind to what the operator had said at 21:13, and the
thread's later replies never knew it had been sent. Now: send_message(conversation=…) puts the answer in its thread,
only after the cycle has read what was said there since (read_thread), and the thread's next reply reads it in its
dialogue. An ended thread's answer goes to From, linked to it. The operator's notes for the next cycle written in a
thread show in it and name it."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from supervisor import broker, messages, web, web_alerts, web_chat, web_views
from supervisor.tests.test_background_messages import Fixture
from supervisor.tests.test_web import ME, WebTest

CID = "web-c0ffee02"
TALK = "api-0a1b2c3d4e"


def turn(role: str, ts: str, content: str) -> dict:
    return {"role": role, "ts": ts, "content": content, **({"shown": content} if role == "user" else {})}


class ThreadRepliesTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.make(self)
        for p in (mock.patch("supervisor.channel.route", return_value=("web", "test")),
                  mock.patch("supervisor.feedback.by_target", return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def thread(self, cid=CID, state="idle", talks=(TALK,), title="Report review") -> dict:
        chat = {"id": cid, "instance": "wisp", "login": ME, "created": "2026-11-10T21:08:48", "state": state,
                "title": title, "reply": None, "talks": list(talks), "turns": [
                    turn("user", "2026-11-10T21:11:33", "What do you make of the report?"),
                    turn("assistant", "2026-11-10T21:12:22", "I still have to read the second half: I will, first."),
                    turn("user", "2026-11-10T21:13:47", "Actually, the deadline moved to Friday."),
                    turn("assistant", "2026-11-10T21:14:20", "Noted."),
                ]}
        web_chat.save(chat)
        return chat

    def cycle(self) -> broker.Broker:
        """A cycle's broker: no conversation of its own (talk_id None)."""
        return broker.Broker("wisp", self.f.project, {}, self.f.project / "broker.log")

    def send(self, b: broker.Broker, conversation: str, text: str = "I read the whole report: two sections need work.",
             **extra) -> dict:
        return b.call("send_message", {"to": "operator", "text": text, "conversation": conversation, **extra})

    def operator_writes(self, text: str, ts: str) -> None:
        chat = web_chat.load(CID)
        chat["turns"].append(turn("user", ts, text))
        web_chat.save(chat)

    # -- the case ---------------------------------------------------------------------------------------------------
    def test_the_queued_answer_case_an_answer_waits_for_a_read_then_joins_the_thread_as_its_next_turn(self):
        self.thread()
        b = self.cycle()
        refused = self.send(b, TALK)                       # queued at 21:12, sent blind at 21:15: refused now
        self.assertFalse(refused["ok"], refused)
        self.assertIn("read the thread «Report review» first: read_thread(id='web-c0ffee02')", refused["error"])
        self.assertIn("you have not read it in this run", refused["error"])
        self.assertEqual(messages.history(), [])          # nothing delivered, nothing in the operator's inbox
        self.assertFalse((self.f.sup / "operator-inbox").exists() and any((self.f.sup / "operator-inbox").iterdir()))

        read = b.call("read_thread", {"id": TALK})         # the conversation id of the queued note resolves
        self.assertTrue(read["ok"], read)
        self.assertIn("operator (2026-11-10T21:13:47): Actually, the deadline moved to Friday.", read["result"])

        self.operator_writes("So what can I do?", "2026-11-10T21:15:35")   # written after the read
        stale = self.send(b, TALK)
        self.assertFalse(stale["ok"])
        self.assertIn("the operator wrote there 1 more time(s) since you read it, last at 21:15", stale["error"])

        b.call("read_thread", {"id": CID})
        sent = self.send(b, TALK, "Having read it all, with Friday in mind: two sections need work.")
        self.assertTrue(sent["ok"], sent)
        self.assertIn("delivered to the operator (note) as part of the thread «Report review»", sent["result"])
        [row] = messages.history()
        self.assertEqual((row["thread"], row["talk"]), (CID, None))
        self.assertNotIn("re", row)

        chat = web_chat.load(CID)
        self.assertEqual([m["file"] for m in web_chat.sent_during("wisp", chat)], [row["file"]])
        self.assertEqual(web_chat.from_feed("wisp")["items"], [])            # not in From: no second place for it
        self.assertEqual(web_alerts._cid_of_message(row), CID)              # its push opens the thread
        item = web_chat.message_item("wisp", row, {})
        self.assertTrue(item["from_cycle"])
        self.assertIn("⏾ from its cycle", str(web_views._message_item("wisp", item, False)))

        # One voice: the thread's next reply reads the cycle's answer in its dialogue, after the turn it followed
        # (the operator's 21:15:35 message has no answer yet: the request still ends with it, the cycle's answer just
        # before it; the thread's own order keeps the answer last)
        api = web_chat._api_messages(chat)
        self.assertEqual([m["role"] for m in api], ["user", "assistant", "user", "assistant", "assistant", "user"])
        self.assertTrue(api[-2]["content"].startswith("[sent from my cycle at "))
        self.assertIn("with Friday in mind", api[-2]["content"])
        self.assertIn("with Friday in mind", web_chat.dialogue(chat)[-1][2])
        self.assertIn("with Friday in mind", b.call("read_thread", {"id": CID})["result"])

    def test_the_thread_marks_lights_its_unread_and_not_froms(self):
        chat = self.thread()
        b = self.cycle()
        b.call("read_thread", {"id": CID})
        self.assertTrue(self.send(b, CID)["ok"])
        [row] = messages.history()
        self.assertEqual(web_chat.last_from(chat, web_chat.sent_during("wisp", chat)), row["ts"])
        self.assertIsNone(web_chat.from_feed("wisp")["last"])

    def test_an_ended_threads_answer_goes_to_from_linked_to_it(self):
        self.thread(state="ended")
        sent = self.send(self.cycle(), TALK)               # no read needed: it is not written into the thread
        self.assertTrue(sent["ok"], sent)
        self.assertIn("in the From feed, linked to the thread «Report review»: it has ended", sent["result"])
        [row] = messages.history()
        self.assertEqual(row["re"], CID)
        self.assertNotIn("thread", row)
        [item] = web_chat.from_feed("wisp")["items"]
        self.assertEqual(item["re"], {"cid": CID, "title": "Report review"})
        self.assertFalse(item["from_cycle"])
        self.assertIn('href="/i/wisp/chat?id=web-c0ffee02"', str(web_views._message_item("wisp", item, False)))
        self.assertEqual(web_chat.sent_during("wisp", web_chat.load(CID), archived=True), [])

    def test_a_conversation_with_no_web_thread_goes_to_from_and_an_unknown_one_is_refused(self):
        self.thread()
        sent = self.send(self.cycle(), "api-term1")        # a terminal talk or Telegram: no web thread
        self.assertTrue(sent["ok"], sent)
        self.assertIn("in the From feed (that conversation was not a web thread)", sent["result"])
        for bad in ("web-0000dead", "../x", "a b"):
            r = self.send(self.cycle(), bad)
            self.assertFalse(r["ok"])
            self.assertIn("unknown conversation", r["error"])
        r = self.cycle().call("send_message", {"to": "wisp", "text": "x", "conversation": CID})
        self.assertFalse(r["ok"])
        self.assertIn("conversation is for a message to the operator", r["error"])
        self.assertEqual(len(messages.history()), 1)

    def test_a_replay_neither_reads_nor_looks_up_live_threads(self):
        """Review finding: a replay resolved today's thread (its new title, its ended state, a deleted file refused).
        It is simulated: nothing about the thread is looked up, whatever became of it."""
        b = self.cycle()
        b.as_of = datetime(2026, 11, 10, 21, 15)           # read_thread is not offered in a replay
        said = " to the conversation it names (a replay: not looked up)"
        self.assertEqual(b._conversation_target(CID), (None, None, said))     # no such thread file now
        self.thread(state="ended", title="Renamed since")
        self.assertEqual(b._conversation_target(TALK), (None, None, said))
        with self.assertRaises(ValueError):
            b._conversation_target("../x")

    # -- review findings ----------------------------------------------------------------------------------------------
    def test_a_reply_request_ends_with_the_operators_message_even_after_a_late_cycle_answer(self):
        """Review finding: a cycle answer stamped in the same second as the operator's pending message (or just after,
        before the cycle paused) sorted after it, and the API refuses a request that does not end with the operator."""
        self.thread()
        self.operator_writes("So what now?", "2026-11-10T21:15:35")
        for ts in ("2026-11-10T21:15:35", "2026-11-10T21:15:36"):
            messages._cache.update(key=None, rows=[])
            (self.f.sup / "messages.jsonl").write_text(
                '{"ts": "%s", "from": "wisp", "to": "operator", "kind": "note", "file": "f.md", "text": "late", '
                '"talk": null, "thread": "%s"}\n' % (ts, CID))
            api = web_chat._api_messages(web_chat.load(CID))
            self.assertEqual((api[-1]["role"], api[-1]["content"]), ("user", "So what now?"), ts)
            self.assertEqual(api[-2]["role"], "assistant")
            self.assertIn("late", api[-2]["content"])
            self.assertEqual(web_chat.dialogue(web_chat.load(CID))[-1][2].split("\n")[-1], "late")   # true order kept

    def test_what_the_operator_does_during_the_orders_check_counts(self):
        """Review finding: the read was checked before the orders check (a judge: seconds), then delivered blind."""
        self.thread()
        b = self.cycle()
        b.call("read_thread", {"id": CID})

        def judge(text, file, stamp):
            self.operator_writes("Wait, cancel that.", "2026-11-10T21:16:00")
            return None, ""
        with mock.patch.object(b, "_check_orders", side_effect=judge):
            r = self.send(b, CID)
        self.assertFalse(r["ok"], r)
        self.assertIn("wrote there 1 more time(s)", r["error"])
        self.assertEqual(messages.history(), [])

    def test_read_thread_always_returns_the_operators_latest_message_it_credits(self):
        """Review finding: a long answer after the operator's last message pushed it out of read_thread's 12,000
        characters, yet the read counted, so a cycle could answer without having seen it."""
        chat = self.thread()
        chat["turns"] += [turn("user", "2026-11-10T21:15:00", "Do not proceed. " + "x" * 9000),
                          turn("assistant", "2026-11-10T21:15:30", "y" * 13000),
                          turn("assistant", "2026-11-10T21:15:40", "z" * 13000)]
        web_chat.save(chat)
        b = self.cycle()
        out = b.call("read_thread", {"id": CID})["result"]
        self.assertIn("operator (2026-11-10T21:15:00): Do not proceed.", out)
        self.assertIn("[10000 more characters cut]", out)
        self.assertTrue(self.send(b, CID)["ok"])

    def test_an_ended_threads_answer_sent_from_another_conversation_is_in_from(self):
        """Review finding: sent during thread A's reply for ended thread B, the row (talk=A's, re=B) showed in A and
        its push opened A."""
        self.thread(state="ended")
        a = self.thread(cid="web-a0000001", talks=("api-aaaa",), title="A")
        b = self.cycle()
        b.talk_id = "api-aaaa"
        self.assertTrue(self.send(b, CID)["ok"])
        [row] = messages.history()
        self.assertEqual((row["talk"], row["re"]), ("api-aaaa", CID))
        self.assertEqual(web_chat.sent_during("wisp", a), [])
        self.assertEqual([i["re"]["cid"] for i in web_chat.from_feed("wisp")["items"]], [CID])
        self.assertEqual(web_alerts._cid_of_message(row), "from")

    # -- the operator's note for the next cycle, written in a thread -----------------------------------------------
    def test_a_note_for_the_next_cycle_shows_in_its_thread_names_it_and_needs_a_new_read(self):
        chat = self.thread()
        b = self.cycle()
        b.call("read_thread", {"id": CID})
        file, _ = web_chat.background("wisp", ME, "Look at the second half too.", [], mid="m1", cid=CID)
        text = (self.f.root / "inbox" / file).read_text()
        self.assertIn("Conversation: web-c0ffee02 («Report review»)\n", text)
        [row] = messages.history()
        self.assertEqual(row["thread"], CID)
        [later] = [web_chat.message_item("wisp", m, {}) for m in web_chat.sent_during("wisp", chat)]
        self.assertEqual(later["type"], "later")
        self.assertEqual(web_chat.from_feed("wisp")["items"], [])
        self.assertTrue(web_chat._api_messages(chat)[-1]["content"].startswith("[left for my next cycle at "))
        refused = self.send(b, CID)                        # the note is something the operator wrote there since
        self.assertIn("wrote there 1 more time(s)", refused["error"])

    def test_a_note_from_the_plain_page_joins_the_thread_it_follows(self):
        """The plain chat page's composer is drawn before its first message opens a thread: it names "current", so
        a note sent from it after that message still belongs to the thread (found in the browser check)."""
        self.thread()
        file, _ = web_chat.background("wisp", ME, "and the second half", [], mid="m1", cid="current")
        self.assertIn(f"Conversation: {CID} («Report review»)", (self.f.root / "inbox" / file).read_text())
        self.assertEqual(messages.history()[0]["thread"], CID)

    def test_a_note_from_from_or_for_an_ended_thread_stays_in_from(self):
        self.thread(state="ended")
        for cid in ("", "from", CID, "current"):
            file, _ = web_chat.background("wisp", ME, f"note {cid}", [], mid=f"m-{cid or 0}", cid=cid)
            self.assertNotIn("Conversation:", (self.f.root / "inbox" / file).read_text())
        self.assertEqual([m.get("thread") for m in messages.history()], [None, None, None, None])
        self.assertEqual(len(web_chat.from_feed("wisp")["items"]), 4)


class ThreadNoteRouteTest(WebTest):
    """The plain chat page names the thread it shows, so a note for the next cycle written there belongs to it."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(web, "_names", return_value=["wisp"])
        p.start()
        self.addCleanup(p.stop)
        self.cookie = self.paired()

    def test_the_plain_pages_composer_names_its_thread_and_the_route_passes_it(self):
        web_chat.save({"id": CID, "instance": "wisp", "login": ME, "created": "2026-11-10T21:08:48", "state": "idle",
                       "turns": [turn("user", "2026-11-10T21:11:33", "hi")], "reply": None, "talks": []})
        page = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        self.assertIn('<input type="hidden" name="thread" value="current">', page)
        pinned = self.req(f"/i/wisp/chat?id={CID}", cookie=self.cookie).body.decode()
        self.assertIn(f'<input type="hidden" name="cid" value="{CID}">', pinned)
        self.assertNotIn('name="thread"', pinned)
        self.assertNotIn('name="cid"', page.split('class="composer"', 1)[1].split("</form>", 1)[0])
        body = (b'--B\r\nContent-Disposition: form-data; name="text"\r\n\r\nlater\r\n'
                b'--B\r\nContent-Disposition: form-data; name="background"\r\n\r\n1\r\n'
                b'--B\r\nContent-Disposition: form-data; name="thread"\r\n\r\ncurrent\r\n'
                b'--B\r\nContent-Disposition: form-data; name="mid"\r\n\r\nm9\r\n--B--\r\n')
        headers = {"content-type": "multipart/form-data; boundary=B", "sec-fetch-site": "same-origin",
                   "x-csrf-token": self.csrf(self.cookie)}
        with mock.patch.object(web_chat, "background", return_value=("f.md", "for wisp's next cycle")) as later:
            r = self.req("/chat/wisp/send", "POST", cookie=self.cookie, body=body, headers=headers)
        self.assertEqual(r.status, 200, r.body)
        later.assert_called_once_with("wisp", ME, "later", [], mid="m9", cid="current")


if __name__ == "__main__":
    unittest.main()
