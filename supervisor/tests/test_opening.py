"""The hidden opening of a new instance and the resume after the operator's first reply.

Setup ends by asking the new instance for its welcome (web_chat.opening): a user turn the operator never sees — the API
needs a request that ends with a user message — so Main's first visible message is the instance's own welcome. The
instance stays paused (`shaft new --paused-until-first-reply`) until the operator has answered it; the reply to that
answer then resumes it (or, with "stay", only clears the marker). The reply to the opening itself never does."""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from unittest import mock

from supervisor import actions, core, runner, web_chat, web_views
from supervisor.tests.test_web_chat import ME, ChatTest

OPENING = "SETUP-ONLY: introduce yourself to the operator and ask what they want from you first."


class OpeningTest(ChatTest):
    def setUp(self):
        super().setUp()
        self.sup = self.dir / "sup"
        self.sup.mkdir(parents=True, exist_ok=True)
        # Never the real CLI: a reply that ends after a test would otherwise run `shaft resume`
        self.run_now = self.patch(mock.patch.object(actions, "run_now", return_value=(True, "resumed wisp.")))

    def patch(self, p):
        """Started now, stopped only after every reply of the test has ended (cleanups run last-in, first-out)."""
        started = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.end_relays)
        return started

    def paused_until_first_reply(self, then: str) -> None:
        """What `shaft new --paused-until-first-reply <then>` leaves (Task 9)."""
        (self.sup / "paused-wisp").write_text("")
        (self.sup / "first-reply-wisp").write_text(json.dumps({"then": then, "created": "2026-11-12T10:00:00+01:00"}))

    def marker(self):
        return self.sup / "first-reply-wisp"

    def resumes(self) -> list:
        return [c for c in self.run_now.call_args_list if c.args[0][:1] == ["resume"]]

    def open_and_wait(self) -> dict:
        chat = web_chat.opening("wisp", ME, OPENING)
        return self.wait_idle(chat["id"])

    def test_the_opening_is_never_in_the_feed_nor_the_title(self):
        self.api.hold.set()
        chat = web_chat.opening("wisp", ME, OPENING)
        self.assertTrue(web_chat.is_from(chat))
        self.assertEqual(chat["state"], "replying")
        time.sleep(0.2)
        while_replying = web_chat.feed("wisp")
        self.assertEqual([i for i in while_replying["items"] if i["type"] == "user"], [])
        self.assertNotIn("SETUP-ONLY", json.dumps(while_replying))
        self.assertNotIn("line", web_chat.summary("wisp"))          # nothing to show on the rail yet: no "you: …"
        self.api.hold.clear()
        c = self.wait_idle(chat["id"], 15)
        model = web_chat.feed("wisp")
        self.assertEqual([i["type"] for i in model["items"]], ["assistant"])
        self.assertNotIn("SETUP-ONLY", json.dumps(model))
        self.assertNotIn("SETUP-ONLY", str(web_views.chat_feed("wisp", c, web_chat.from_rows("wisp"))))
        self.assertNotIn("SETUP-ONLY", json.dumps(web_chat.summary("wisp")))
        self.assertEqual(web_chat.excerpt(c), ("", 0))              # no operator message in it yet
        self.assertEqual(web_chat.title(c), "wisp")
        self.assertEqual(model["mids"], [])
        # The instance received it all the same — a user message, the request's last, with the docs note
        self.assertEqual(self.api.chats()[0][2]["messages"],
                         [{"role": "user", "content": OPENING + "\n\n" + web_chat.DOCS_NOTE}])
        self.assertEqual(self.api.chats()[0][1]["X-Shaft-Conversation"], chat["id"])

    def test_the_instance_reply_to_the_opening_is_shown(self):
        c = self.open_and_wait()
        model = web_chat.feed("wisp")
        self.assertEqual([(i["type"], i["content"]) for i in model["items"]], [("assistant", "Answer 1.")])
        self.assertIsNone(model["items"][0]["failure"])
        self.assertEqual(model["last"], c["turns"][-1]["ts"])          # unread for the operator: the instance spoke
        self.assertEqual(web_chat.summary("wisp")["line"], "Answer 1.")
        # The operator's answer continues the same conversation; the docs note is not said twice
        web_chat.send("wisp", ME, "hello, I am the operator", [])
        self.wait_idle(c["id"])
        self.assertEqual(self.api.chats()[1][2]["messages"], [
            {"role": "user", "content": OPENING + "\n\n" + web_chat.DOCS_NOTE},
            {"role": "assistant", "content": "Answer 1."},
            {"role": "user", "content": "hello, I am the operator"}])
        self.assertEqual([i["type"] for i in web_chat.feed("wisp")["items"]], ["assistant", "user", "assistant"])
        self.assertEqual(web_chat.excerpt(web_chat.load(c["id"])), ("hello, I am the operator", 1))

    def test_the_opening_is_only_for_a_new_instance_and_never_empty(self):
        with self.assertRaisesRegex(web_chat.Refused, "empty"):
            web_chat.opening("wisp", ME, "   ")
        c = self.open_and_wait()
        with self.assertRaisesRegex(web_chat.Refused, "already"):
            web_chat.opening("wisp", ME, OPENING)
        self.assertEqual(len(web_chat.load(c["id"])["turns"]), 2)
        self.assertEqual(len(self.api.chats()), 1)

    def test_the_first_operator_reply_resumes_once(self):
        self.paused_until_first_reply("resume")
        c = self.open_and_wait()
        self.assertEqual(self.resumes(), [])                         # the welcome alone never resumes it
        self.assertTrue(self.marker().exists())
        self.assertTrue(core.paused("wisp"))
        web_chat.send("wisp", ME, "hi", [])
        self.wait_idle(c["id"])
        self.assertEqual(self.resumes(), [mock.call(["resume", "wisp"], who="first reply")])
        self.assertFalse(self.marker().exists())
        web_chat.send("wisp", ME, "and again", [])                  # no marker: no second resume
        self.wait_idle(c["id"])
        self.assertEqual(len(self.resumes()), 1)

    def test_a_reply_in_a_thread_counts_as_the_first_reply_too(self):
        self.paused_until_first_reply("resume")
        self.open_and_wait()
        t = web_chat.new_thread("wisp", ME)
        web_chat.send("wisp", ME, "a topic of its own", [], cid=t["id"])
        self.wait_idle(t["id"])
        self.assertEqual(len(self.resumes()), 1)
        self.assertFalse(self.marker().exists())

    def test_two_replies_ending_together_resume_once(self):
        self.paused_until_first_reply("resume")
        slow = threading.Event()

        def run_now(args, timeout=60, who="top"):
            slow.wait(2)
            return True, "resumed wisp."
        self.run_now.side_effect = run_now
        workers = [threading.Thread(target=web_chat._first_reply, args=("wisp",)) for _ in range(2)]
        for w in workers:
            w.start()
        time.sleep(0.1)
        slow.set()
        for w in workers:
            w.join(5)
        self.assertEqual(len(self.resumes()), 1)
        self.assertFalse(self.marker().exists())

    def test_a_resume_that_fails_keeps_the_marker_for_the_next_reply(self):
        self.paused_until_first_reply("resume")
        c = self.open_and_wait()
        self.run_now.return_value = (False, "error: launchd refused")
        web_chat.send("wisp", ME, "hi", [])
        self.wait_idle(c["id"])
        self.assertEqual(len(self.resumes()), 1)
        self.assertTrue(self.marker().exists())                     # still waiting: the next reply tries again
        self.run_now.return_value = (True, "resumed wisp.")
        web_chat.send("wisp", ME, "hi again", [])
        self.wait_idle(c["id"])
        self.assertEqual(len(self.resumes()), 2)
        self.assertFalse(self.marker().exists())

    def test_stay_keeps_it_paused_and_clears_the_marker(self):
        self.paused_until_first_reply("stay")
        c = self.open_and_wait()
        self.assertTrue(self.marker().exists())
        web_chat.send("wisp", ME, "hi", [])
        self.wait_idle(c["id"])
        self.assertEqual(self.resumes(), [])
        self.assertFalse(self.marker().exists())
        self.assertTrue(core.paused("wisp"))

    def test_a_failed_opening_keeps_the_instance_paused(self):
        """Review Focus 3: the welcome reply fails (the API down here). The chat shows the failure with Retry; Retry
        asks for the welcome again, still hidden; the instance stays paused and the marker stays."""
        self.paused_until_first_reply("resume")
        port = self.api.server.server_address[1]
        with socket.socket() as s:                                    # a port nothing listens on: connection refused
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        with mock.patch.object(web_chat, "_api_conn",
                               lambda timeout: http.client.HTTPConnection("127.0.0.1", closed, timeout=timeout)):
            c = self.open_and_wait()
        self.assertTrue(web_chat.failed(c))
        model = web_chat.feed("wisp")
        self.assertEqual([i["type"] for i in model["items"]], ["assistant"])
        failure = model["items"][0]["failure"]
        self.assertEqual(failure["kind"], "connection")
        self.assertIn("No external actions ran", failure["action"])      # refused before it was posted: nothing ran
        retry = failure["retry"]
        self.assertIsNotNone(retry)
        self.assertEqual((retry["text"], retry["retry_of"], retry["confirm"]), (web_chat.OPENING_RETRY, 1, None))
        self.assertNotIn("SETUP-ONLY", json.dumps(model))
        rendered = str(web_views.chat_feed("wisp", c, web_chat.from_rows("wisp")))
        self.assertIn("data-retry-of=\"1\"", rendered)
        self.assertNotIn("SETUP-ONLY", rendered)
        self.assertEqual(self.resumes(), [])
        self.assertTrue(core.paused("wisp"))
        self.assertTrue(self.marker().exists())
        # Retry, as the page and the apps send it: the opening runs again, hidden; the failed try folds
        self.assertEqual(port, self.api.server.server_address[1])
        chat, said = web_chat.send("wisp", ME, retry["text"], [], mid="m-retry", retry_of=str(retry["retry_of"]))
        self.assertEqual(said, "sent to wisp")
        c = self.wait_idle(chat["id"])
        self.assertEqual(self.api.chats()[-1][2]["messages"],
                         [{"role": "user", "content": OPENING + "\n\n" + web_chat.DOCS_NOTE}])
        model = web_chat.feed("wisp")
        self.assertEqual([i["type"] for i in model["items"]], ["retried", "assistant"])
        self.assertNotIn(web_chat.OPENING_RETRY, json.dumps(model["items"]))
        self.assertIn("m-retry", model["mids"])                      # the client's "sending" copy has arrived
        self.assertEqual(self.resumes(), [])                         # the welcome, even retried, never resumes it
        self.assertTrue(self.marker().exists())
        self.assertTrue(core.paused("wisp"))

    def test_the_conversation_is_open_while_the_opening_replies(self):
        """The opening's reply is an API conversation like any (keyed by Main's id): the API registers it, so
        runner.open_conversations holds it and no write-up runs under it."""
        talks = self.sup / "api_talks.json"
        from supervisor import api
        for p in (mock.patch.object(api, "TALKS", talks), mock.patch.object(runner, "API_TALKS", talks),
                  mock.patch("supervisor.version.service", return_value={"pid": 1})):
            self.patch(p)
        self.api.hold.set()
        chat = web_chat.opening("wisp", ME, OPENING)
        end = time.monotonic() + 5
        while not self.api.chats() and time.monotonic() < end:
            time.sleep(0.05)
        _, headers, body = self.api.chats()[0]
        # What the real API does with this request (api.Handler._chat): its conversation, by X-Shaft-Conversation
        talk = api.Conversations().talk_id(body["model"], body["messages"], headers["X-Shaft-Conversation"],
                                           headers["X-Shaft-Channel"])
        self.assertEqual(web_chat.load(chat["id"])["state"], "replying")
        self.assertIn(chat["id"], [c["id"] for c in web_chat.open_chats("wisp")])
        self.assertEqual(runner.open_conversations("wisp"), {talk: "api"})
        self.assertEqual(api.Conversations().current("wisp", [], chat["id"]), talk)   # the operator's answer: the same
        self.api.hold.clear()
        self.wait_idle(chat["id"], 15)


if __name__ == "__main__":
    import unittest
    unittest.main()
