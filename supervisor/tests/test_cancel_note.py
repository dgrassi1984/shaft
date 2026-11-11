"""Cancellation must remove unread work without hiding its operator receipt."""
import json
import unittest
from unittest import mock

from supervisor import autonomy, messages, pushes, recap, reply_links, runner, web, web_chat, web_views
from supervisor.tests import test_app_api as app_tests, test_web as web_tests
from supervisor.tests.test_background_messages import Fixture


class CancelNoteTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.make(self)
        self.f.root = self.f.root.rename(self.f.root.with_name("demo"))
        for p in (mock.patch.object(runner, "SUP_DIR", self.f.sup),
                  mock.patch.object(autonomy, "event_wake"),
                  mock.patch("supervisor.docs.instance_root", return_value=self.f.root)):
            p.start()
            self.addCleanup(p.stop)

    def note(self, **kw):
        return web_chat.background("demo", web_tests.ME, "do this later", [], mid="note-1", **kw)[0]

    def test_cancel_by_mid_removes_work_and_keeps_an_idempotent_receipt(self):
        file = self.note()
        [before] = web_chat.from_feed("demo")["items"]
        self.assertIsNone(before["cancelled"])
        self.assertTrue(before["cancellable"])
        self.assertIn('/chat/demo/uncycle', str(web_views.feed_html(web_chat.from_feed("demo"), from_feed=True)))
        self.assertEqual(web_chat.uncycle("demo", web_tests.ME, mid="note-1"), "next-cycle note cancelled")
        self.assertEqual(self.f.inbox(), [])
        self.assertFalse((self.f.root / "inbox" / file).exists())
        [row] = messages.history()
        self.assertTrue(row["cancelled"])
        [item] = web_chat.from_feed("demo")["items"]
        self.assertEqual(item["cancelled"], row["cancelled"])
        self.assertFalse(item["cancellable"])
        self.assertEqual(web_chat.uncycle("demo", web_tests.ME, mid="note-1"), "already cancelled")
        html = str(web_views._message_item("demo", item, False))
        self.assertIn("cancelled-text", html)
        self.assertIn("next cycle · cancelled", html)
        self.assertNotIn("/uncycle", html)

    def test_running_cycle_or_reply_cannot_lose_a_note_it_may_have_read(self):
        file = self.note()
        with runner.run_lock("demo", wait=False):
            with self.assertRaisesRegex(web_chat.Refused, "a cycle or reply of demo is running"):
                web_chat.uncycle("demo", web_tests.ME, file=file)
        self.assertTrue((self.f.root / "inbox" / file).exists())
        self.assertNotIn("cancelled", messages.history()[0])

    def test_cancelling_a_note_keeps_its_attachments(self):
        file, _ = web_chat.background("demo", web_tests.ME, "later", [("example.txt", b"keep")], mid="attached")
        [row] = messages.history()
        attachment = self.f.project / row["attachments"][0]["path"]
        web_chat.uncycle("demo", web_tests.ME, file=file)
        self.assertEqual(attachment.read_bytes(), b"keep")

    def test_archived_note_is_refused_instead_of_claiming_it_was_cancelled(self):
        file = self.note()
        self.f.cycle_reads_inbox()
        with self.assertRaisesRegex(web_chat.Refused, "already read in a cycle"):
            web_chat.uncycle("demo", web_tests.ME, file=file)
        self.assertTrue((self.f.root / "memory/messages" / file).exists())
        self.assertNotIn("cancelled", messages.history()[0])

    def test_escape_and_linked_inbox_never_delete_outside_the_inbox(self):
        file = self.note()
        outside = self.f.root / "x"
        outside.write_text("keep")
        with self.assertRaisesRegex(web_chat.Refused, "invalid inbox file"):
            web_chat.uncycle("demo", web_tests.ME, file="../x")
        inbox = self.f.root / "inbox"
        inbox.rename(self.f.root / "kept-inbox")
        inbox.symlink_to(self.f.root / "kept-inbox", target_is_directory=True)
        with self.assertRaisesRegex(web_chat.Refused, "already read"):
            web_chat.uncycle("demo", web_tests.ME, file=file)
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue((self.f.root / "kept-inbox" / file).exists())
        self.assertNotIn("cancelled", messages.history()[0])

    def test_thread_and_from_show_cancel_but_archived_views_do_not(self):
        chat = web_chat.new_thread("demo", web_tests.ME)
        self.note(cid=chat["id"])
        [row] = web_chat.sent_during("demo", chat)
        item = web_chat.message_item("demo", row, {})
        self.assertIn('/chat/demo/uncycle', str(web_views._message_item("demo", item, False)))
        self.assertNotIn('/uncycle', str(web_views._message_item("demo", item, True)))
        web_chat.uncycle("demo", web_tests.ME, mid="note-1")
        self.assertEqual(len(web_chat.sent_during("demo", chat)), 1)
        self.assertEqual(web_chat.dialogue(chat), [])
        self.assertEqual(web_chat.thread_mark(chat), (0, ""))
        self.assertEqual(recap._messages("demo", ""), [])
        self.assertEqual(pushes.records(self.f.sup), [])

    def test_cancellation_preserves_forwarder_offsets_and_removes_reply_evidence(self):
        self.note()
        offset = (self.f.sup / messages.LOG_NAME).stat().st_size
        (self.f.sup / "reply-links.jsonl").write_text(json.dumps({
            "instance": "demo", "key": "send:demo:note-1", "in_reply_to": "question.md"}) + "\n")
        web_chat.uncycle("demo", web_tests.ME, mid="note-1")
        rows, end = messages.appended(offset)
        self.assertEqual(rows, [])
        self.assertGreater(end, offset)
        self.assertEqual(reply_links.records(), [])
        web_chat.background("demo", web_tests.ME, "keep this one", [], mid="note-2")
        rows, _ = messages.appended(end)
        self.assertEqual([r["text"] for r, _ in rows], ["keep this one"])

    def test_web_route_cancels_and_requires_csrf(self):
        client = web_tests.WebTest()
        client.setUp()
        self.addCleanup(client.doCleanups)
        file = self.note()
        cookie = client.paired()
        headers = {"content-type": "application/x-www-form-urlencoded", "sec-fetch-site": "same-origin"}
        with mock.patch.object(web, "_names", return_value=["demo"]):
            refused = client.req("/chat/demo/uncycle", "POST", cookie=cookie, headers=headers, body=b"mid=note-1")
            self.assertEqual(refused.status, 403)
            headers["x-csrf-token"] = client.csrf(cookie)
            response = client.req("/chat/demo/uncycle", "POST", cookie=cookie, headers=headers, body=b"mid=note-1")
        self.assertEqual(response.status, 200, response.body)
        self.assertIn(b"next-cycle note cancelled", response.body)
        self.assertFalse((self.f.root / "inbox" / file).exists())
        self.assertTrue(messages.history()[0]["cancelled"])


class CancelAppTest(unittest.TestCase):
    def test_app_act_cancels_by_file_and_serves_the_updated_item(self):
        client = app_tests.AppTest()
        client.setUp()
        self.addCleanup(client.doCleanups)
        root = next(client.inst.iterdir()).rename(client.inst / "demo")
        with mock.patch.object(runner, "SUP_DIR", client.sup), \
                mock.patch.object(web, "_names", return_value=["demo"]):
            file, _ = web_chat.background("demo", web_tests.ME, "later", [], mid="app-note")
            result = client.get("/app/v1/chat/demo/uncycle", method="POST", data={"file": file})
            self.assertEqual(result["said"], "next-cycle note cancelled")
            result = client.get("/app/v1/chat/demo/uncycle", method="POST", data={"mid": "app-note"})
            self.assertEqual(result["said"], "already cancelled")
            feed = client.get("/app/v1/from/demo")
        self.assertFalse((root / "inbox" / file).exists())
        self.assertTrue(feed["items"][0]["cancelled"])
        self.assertFalse(feed["items"][0]["cancellable"])
