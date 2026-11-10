"""Issue #70: persisted messages wait for the reply, then become one turn."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import web, web_chat, web_views


class QueueTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-queue-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for patch in (mock.patch.object(web_chat, "SUP_DIR", root),
                      mock.patch("supervisor.core.SUP_DIR", root),
                      mock.patch.object(web_chat, "_docs_now", return_value={}),
                      mock.patch.object(web_chat, "log"), mock.patch("supervisor.webpush.notify"),
                      mock.patch.object(web, "_names", return_value=["wisp"])):
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(web_chat, "_start", side_effect=self.start)
        patch.start()
        self.addCleanup(patch.stop)
        self.started = []
        self.addCleanup(web_chat._relays.clear)

    def start(self, cid):
        relay = web_chat.Relay(cid)
        web_chat._relays[cid] = relay
        self.started.append(relay)

    def queue(self, text, mid):
        body = ''.join(f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                       for k, v in {"text": text, "mid": mid, "queue": "1"}.items()) + '--B--\r\n'
        response = web._post(web.Request("POST", "/chat/wisp/send",
                             {"content-type": "multipart/form-data; boundary=B"}, body.encode()), "device")
        self.assertEqual(response.status, 200)
        return web_chat.current("wisp")

    def test_issue_70_queue_combines_once_and_dedupes(self):
        """Reverting #70 interjects queued text instead of persisting and sending one next turn."""
        for error, stopped, interject in ((None, False, False), ("failed", False, False),
                                           (None, True, False), (None, False, True)):
            with self.subTest(error=error, stopped=stopped, interject=interject):
                chat = web_chat.new_thread("wisp", "")
                web_chat.send("wisp", "", "start", [])
                relay = self.started[-1]
                relay.stopped = stopped
                if interject:
                    with mock.patch.object(web_chat, "_post", return_value=(202, {})):
                        web_chat.send("wisp", "", "late interjection", [])
                with mock.patch.object(web_chat, "_post", return_value=(202, {})) as post:
                    self.queue("> quote\nfirst", "q1")
                    c = self.queue("second", "q2")
                    self.queue("second", "q2")
                self.assertEqual([t["shown"] for t in c["queued"]], ["> quote\nfirst", "second"])
                self.assertEqual(len(c["turns"]), 2 if interject else 1)
                post.assert_not_called()
                starts = len(self.started)
                with web_chat._inst_lock("wisp"):
                    web_chat._finish(relay, "answer", [], error, 0)
                    web_chat._finish(relay, "answer", [], error, 0)
                c = web_chat.load(chat["id"])
                self.assertEqual(len(self.started), starts + 1)
                self.assertEqual([t["role"] for t in c["turns"]],
                                 ["user", "user", "assistant", "user"] if interject else ["user", "assistant", "user"])
                self.assertEqual(c["turns"][-1]["shown"], "> quote\nfirst\n\nsecond")
                self.assertEqual(c["queued"], [])
                before = len(c["turns"])
                self.assertEqual(len(self.queue("second", "q2")["turns"]), before)
                c["state"] = "ended"
                web_chat.save(c)

    def test_issue_70_a_restart_mid_reply_sends_what_was_queued(self):
        """Found in review: reap() ended a reply cut by a web-service restart and left its queued messages waiting
        for a reply end that would never come."""
        chat = web_chat.new_thread("wisp", "")
        web_chat.send("wisp", "", "start", [])
        self.queue("after the restart", "q9")
        web_chat._relays.clear()                     # the old web process is gone
        starts = len(self.started)
        web_chat.reap()
        c = web_chat.load(chat["id"])
        self.assertEqual(len(self.started), starts + 1)
        self.assertEqual(c["state"], "replying")
        self.assertEqual(c["queued"], [])
        self.assertEqual(c["turns"][-1]["shown"], "after the restart")
        self.assertIn("interrupted", c["turns"][-2]["note"])

    def test_issue_70_queuing_leaves_doc_notices_for_when_it_is_sent(self):
        """Found in review: queuing took the pending document notices into the queued message, so cancelling it
        meant the instance never heard of them. They are taken when the queue is sent."""
        chat = web_chat.new_thread("wisp", "")
        web_chat.send("wisp", "", "start", [])
        with mock.patch("supervisor.doc_comments.take_notices", return_value=["[a comment notice]"]) as take:
            self.queue("queued", "q8")
            take.assert_not_called()
            with web_chat._inst_lock("wisp"):
                web_chat._finish(self.started[-1], "answer", [], None, 0)
        self.assertIn("[a comment notice]", web_chat.load(chat["id"])["turns"][-1]["content"])

    def test_issue_70_idle_cancel_and_feed(self):
        """Reverting #70 loses queued cancellation, feed status and reload reconciliation."""
        c = self.queue("idle", "idle")
        self.assertEqual(c["turns"][0]["shown"], "idle")
        with mock.patch.object(web_chat, "_post", return_value=(202, {})):
            c = self.queue("<next>", "q1")
        feed = str(web_views.chat_feed("wisp", c, []))
        self.assertLess(feed.index('data-k="live"'), feed.index('data-k="queue-q1"'))
        self.assertIn("queued — sent when this reply ends", feed)
        self.assertIn("&lt;next&gt;", feed)
        self.assertIn('data-post="/chat/wisp/unqueue"', feed)
        page = str(web_views.chat_page("wisp", c, [], [], {}, ["wisp"], "csrf"))
        self.assertIn('&quot;q1&quot;', page)
        response = web._post(web.Request("POST", "/chat/wisp/unqueue", {}, b"mid=q1"), "device")
        self.assertEqual(response.status, 200)
        self.assertEqual(web_chat.load(c["id"])["queued"], [])

    def test_issue_70_attachments_and_closed_chats(self):
        """Reverting #70 loses combined attachments or starts queued work after a chat was closed."""
        c, _ = web_chat.send("wisp", "", "start", [])
        attachment = {"name": "a.txt", "path": "attachments/a.txt", "bytes": 1}
        with mock.patch("supervisor.messages.from_operator", return_value=([attachment], "\nfile a.txt")):
            web_chat.queue("wisp", "", "one", [("a.txt", b"a")], mid="a")
            web_chat.queue("wisp", "", "two", [("a.txt", b"a")], mid="b")
        web_chat._finish(self.started[-1], "", [], "failed", 0)
        c = web_chat.load(c["id"])
        self.assertEqual(c["turns"][-1]["attachments"], [attachment, attachment])
        self.assertEqual(c["turns"][-1]["content"], "one\nfile a.txt\n\ntwo\nfile a.txt")
        for closed in ({"cleared": "now"}, {"state": "ended"}):
            c.update(closed)
            web_chat.save(c)
            before = len(self.started)
            web_chat._finish(self.started[-1], "", [], None, 0)
            self.assertEqual(len(self.started), before)
            self.assertEqual(web_chat.load(c["id"])["turns"], c["turns"])
