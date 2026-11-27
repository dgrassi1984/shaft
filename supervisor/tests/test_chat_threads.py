"""Topic threads: several open web chats with one instance, each its own conversation, answered one reply at a time
(the operator, 2026-11-10: "different discussions on different topics in parallel … exactly the same assistant …
internally still sequential"). Before, every operation found "the" open chat by instance name (web_chat.open_chat), so
a second chat could only be opened by ending the first, and a message always landed in the newest one."""
from __future__ import annotations

import time

from supervisor import web_chat
from supervisor.tests.test_web_chat import ME, ChatTest


class ThreadsTest(ChatTest):
    def completions(self, cid: str) -> list[dict]:
        return [body for _, headers, body in self.api.chats() if headers["X-Shaft-Conversation"] == cid]

    def test_two_open_threads_are_each_answered_in_their_own_conversation(self):
        a = web_chat.new_thread("wisp", ME)
        b = web_chat.new_thread("wisp", ME)
        web_chat.send("wisp", ME, "alpha", [], cid=a["id"])
        self.wait_idle(a["id"])
        web_chat.send("wisp", ME, "beta", [], cid=b["id"])
        self.wait_idle(b["id"])
        shown = lambda cid: [t.get("shown") for t in web_chat.load(cid)["turns"] if t["role"] == "user"]  # noqa: E731
        self.assertEqual(shown(a["id"]), ["alpha"])
        self.assertEqual(shown(b["id"]), ["beta"])
        self.assertEqual(len(self.completions(a["id"])), 1)
        self.assertEqual(len(self.completions(b["id"])), 1)
        self.assertNotIn("alpha", str([m for m in self.completions(b["id"])[0]["messages"]
                                     if m["role"] != "system"]))
        self.assertEqual({c["id"] for c in web_chat.threads("wisp")}, {a["id"], b["id"]})

    def test_regression_a_message_sent_to_thread_b_never_lands_in_a_while_a_replies(self):
        a = web_chat.new_thread("wisp", ME)
        b = web_chat.new_thread("wisp", ME)
        self.api.hold.set()
        web_chat.send("wisp", ME, "long question", [], cid=a["id"])
        time.sleep(0.3)
        _, said = web_chat.send("wisp", ME, "other topic", [], cid=b["id"])
        self.assertEqual(said, "sent to wisp")                          # its own reply, not an interjection into A
        self.assertFalse([r for r in self.api.requests if r[0] == "/v1/chat/interject"])
        self.assertNotIn("other topic", str(web_chat.load(a["id"])["turns"]))
        self.api.hold.clear()
        self.wait_idle(a["id"], 15)
        self.wait_idle(b["id"], 15)
        self.assertEqual([t["role"] for t in web_chat.load(a["id"])["turns"]], ["user", "assistant"])
        self.assertEqual([t["role"] for t in web_chat.load(b["id"])["turns"]], ["user", "assistant"])

    def test_new_thread_ends_no_other_and_end_ends_only_its_own(self):
        a, _ = web_chat.send("wisp", ME, "first", [], cid=web_chat.new_thread("wisp", ME)["id"])
        self.wait_idle(a["id"])
        b = web_chat.new_thread("wisp", ME)
        self.assertNotEqual(web_chat.load(a["id"])["state"], "ended")
        self.assertFalse([r for r in self.api.requests if r[0] == "/v1/chat/end"])
        web_chat.end(a["id"])
        self.assertEqual(web_chat.load(a["id"])["state"], "ended")
        self.assertEqual([c["id"] for c in web_chat.threads("wisp")], [b["id"]])
        with self.assertRaisesRegex(web_chat.Refused, "ended"):
            web_chat.send("wisp", ME, "to a closed thread", [], cid=a["id"])
        self.assertEqual(len(web_chat.load(a["id"])["turns"]), 2)        # nothing was added to it

    def test_a_message_naming_no_thread_goes_to_main_whatever_the_threads_order(self):
        """2026-11-11: it went to the most recently active thread (current()), a target that moved with every reply;
        the instance's own conversation, Main, takes it — not the thread on top of the operator's order either."""
        a = web_chat.new_thread("wisp", ME)
        b = web_chat.new_thread("wisp", ME)
        web_chat.send("wisp", ME, "in a", [], cid=a["id"])
        self.wait_idle(a["id"])
        chat, _ = web_chat.send("wisp", ME, "no thread named", [])
        self.assertTrue(web_chat.is_from(chat))
        self.assertNotIn(chat["id"], (a["id"], b["id"]))
        self.wait_idle(chat["id"])

    def test_a_thread_of_another_instance_is_refused(self):
        (self.dir / "instances" / "kiln").mkdir()
        k = web_chat.new_thread("kiln", ME)
        with self.assertRaisesRegex(web_chat.Refused, "not a chat of wisp"):
            web_chat.send("wisp", ME, "x", [], cid=k["id"])
        with self.assertRaisesRegex(web_chat.Refused, "not a chat of wisp"):
            web_chat.rename("wisp", k["id"], "mine now")
        self.assertEqual(web_chat.load(k["id"])["turns"], [])

    def test_a_thread_is_titled_by_its_first_message_until_renamed(self):
        a = web_chat.new_thread("wisp", ME)
        web_chat.send("wisp", ME, "October close\nand the rest", [], cid=a["id"])
        self.wait_idle(a["id"])
        self.assertEqual(web_chat.title(web_chat.load(a["id"])), "October close")
        web_chat.rename("wisp", a["id"], "  Ledger  ")
        self.assertEqual(web_chat.title(web_chat.load(a["id"])), "Ledger")
        with self.assertRaisesRegex(web_chat.Refused, "title"):
            web_chat.rename("wisp", a["id"], "   ")
        b = web_chat.new_thread("wisp", ME, title="Mail")
        self.assertEqual(web_chat.title(b), "Mail")

    def test_queue_and_unqueue_act_on_the_named_thread(self):
        a = web_chat.new_thread("wisp", ME)
        b = web_chat.new_thread("wisp", ME)
        self.api.hold.set()
        web_chat.send("wisp", ME, "busy", [], cid=a["id"])
        time.sleep(0.3)
        _, said = web_chat.queue("wisp", ME, "after that", [], mid="m1", cid=a["id"])
        self.assertIn("queued", said)
        self.assertEqual([t["mid"] for t in web_chat.load(a["id"])["queued"]], ["m1"])
        self.assertFalse(web_chat.load(b["id"]).get("queued"))
        web_chat.unqueue("wisp", "m1", cid=a["id"])
        self.assertEqual(web_chat.load(a["id"])["queued"], [])
        self.api.hold.clear()
        self.wait_idle(a["id"], 15)

    def test_document_edits_go_with_the_thread_they_were_saved_beside(self):
        a = web_chat.new_thread("wisp", ME)
        b = web_chat.new_thread("wisp", ME)
        web_chat.doc_saved("wisp", ME, "plan.md", "old\n", "new\n", "sha1", cid=b["id"])
        self.assertIn("plan.md", web_chat.load(b["id"]).get("pending_edits") or {})
        web_chat.new_thread("wisp", ME)                                  # an open thread's edits are never taken
        self.assertIn("plan.md", web_chat.load(b["id"]).get("pending_edits") or {})
        web_chat.send("wisp", ME, "in a", [], cid=a["id"])
        self.wait_idle(a["id"])
        self.assertNotIn("plan.md", str(self.completions(a["id"])))
        web_chat.send("wisp", ME, "in b", [], cid=b["id"])
        self.wait_idle(b["id"])
        self.assertIn("plan.md", str(self.completions(b["id"])))

    def test_a_message_sent_again_is_found_in_the_thread_it_landed_in(self):
        a = web_chat.new_thread("wisp", ME)
        b = web_chat.new_thread("wisp", ME)
        web_chat.send("wisp", ME, "once", [], mid="m-once", cid=b["id"])
        self.wait_idle(b["id"])
        time.sleep(1.1)
        web_chat.send("wisp", ME, "a is newer now", [], cid=a["id"])
        self.wait_idle(a["id"])
        chat, said = web_chat.send("wisp", ME, "once", [], mid="m-once")  # its answer was lost: sent again
        self.assertEqual((chat["id"], said), (b["id"], "already sent to wisp"))
        self.assertEqual(len(self.completions(b["id"])), 1)

    def test_the_rail_says_replying_while_any_thread_replies(self):
        a = web_chat.new_thread("wisp", ME)
        self.api.hold.set()
        web_chat.send("wisp", ME, "busy", [], cid=a["id"])
        time.sleep(1.1)
        web_chat.new_thread("wisp", ME)                                  # newer, idle
        self.assertTrue(web_chat.summary("wisp").get("replying"))
        self.api.hold.clear()
        self.wait_idle(a["id"], 15)
        self.assertFalse(web_chat.summary("wisp").get("replying"))

    def test_clearing_one_thread_leaves_the_others(self):
        a, _ = web_chat.send("wisp", ME, "first", [], cid=web_chat.new_thread("wisp", ME)["id"])
        self.wait_idle(a["id"])
        b, _ = web_chat.send("wisp", ME, "second", [], cid=web_chat.new_thread("wisp", ME)["id"])
        self.wait_idle(b["id"])
        web_chat.clear("wisp", a["id"])
        self.assertEqual(len(web_chat.load(a["id"])["clears"]), 1)
        self.assertNotIn("clears", web_chat.load(b["id"]))
        self.assertEqual({c["id"] for c in web_chat.threads("wisp")}, {a["id"], b["id"]})   # both still open

    def test_feedback_on_a_chat_that_is_gone_is_delivered_not_a_crash(self):
        """Review of the threads lane: send_feedback looked its target up with thread_of, which raises Refused — the
        feedback route catches only ValueError, so a rating on a chat no longer there answered 500."""
        from unittest import mock
        from supervisor import feedback
        item = {"id": "f1", "sent": False, "thumb": "down", "target": {"kind": "chat", "chat": "web-00000000", "k": 1}}
        with mock.patch.object(feedback, "deliver") as deliver:
            self.assertEqual(web_chat.send_feedback("wisp", item, "why"), "Feedback delivered to its inbox.")
        deliver.assert_called_once_with(item, "why")

    def test_the_rail_reads_the_chat_the_page_shows(self):
        """Review of the threads lane: the rail took the newest chat not cleared — an older ended one after the newest
        was cleared — while the page (shown) showed none, so the apps' unread mark compared two different chats."""
        old, _ = web_chat.send("wisp", ME, "old topic", [], cid=web_chat.new_thread("wisp", ME)["id"])
        self.wait_idle(old["id"])
        web_chat.end(old["id"])
        time.sleep(1.1)
        new, _ = web_chat.send("wisp", ME, "new topic", [], cid=web_chat.new_thread("wisp", ME)["id"])
        self.wait_idle(new["id"])
        web_chat.clear("wisp", new["id"])
        self.assertIsNone(web_chat.shown("wisp"))
        self.assertNotIn("old topic", web_chat.summary("wisp").get("line", ""))
        self.assertNotIn("Answer", web_chat.summary("wisp").get("line", ""))

    def test_a_message_sent_again_to_a_thread_ended_since_is_found_not_refused(self):
        a = web_chat.new_thread("wisp", ME)
        b = web_chat.new_thread("wisp", ME)
        web_chat.send("wisp", ME, "once", [], mid="m-a", cid=a["id"])
        self.wait_idle(a["id"])
        chat, said = web_chat.send("wisp", ME, "once", [], mid="m-a", cid=a["id"])
        self.assertEqual((chat["id"], said), (a["id"], "already sent to wisp"))
        self.assertEqual(len(self.completions(a["id"])), 1)
        self.assertEqual(len(self.completions(b["id"])), 0)
