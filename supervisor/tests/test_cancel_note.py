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


class CancelReviewTest(unittest.TestCase):
    """Grok's review of the first cut (CancelNoteTest's fixture; its tests are not run twice)."""
    setUp = CancelNoteTest.setUp
    note = CancelNoteTest.note

    def test_a_paused_cycle_read_the_note_so_it_cannot_be_cancelled(self):
        """A cycle paused for a reply has given up the run lock, but it read its inbox when it started: the lock
        alone said nobody had read the note."""
        import os
        file = self.note()
        (self.f.root / ".tmp").mkdir(exist_ok=True)
        (self.f.root / ".tmp" / f"pausable-{os.getpid()}").write_text(str(os.getpid()))
        with self.assertRaisesRegex(web_chat.Refused, "a cycle of demo is running"):
            web_chat.uncycle("demo", web_tests.ME, file=file)
        self.assertTrue((self.f.root / "inbox" / file).exists())
        (self.f.root / ".tmp" / f"pausable-{os.getpid()}").unlink()
        (self.f.root / ".tmp" / "pausable-999999999").write_text("x")       # a run long gone does not block it
        self.assertEqual(web_chat.uncycle("demo", web_tests.ME, file=file), "next-cycle note cancelled")

    def test_a_note_that_did_something_when_sent_offers_no_cancel_and_is_refused(self):
        """Taking it back would not undo what sending it did: a decision the supervisor reports (no page id), a
        quoted question it answered (kind reply), a rating's delivery (feedback). A note with no page id (an older
        client's) is the operator's own and can be taken back."""
        from supervisor import messages as m
        decided = m.deliver_background("demo", "Calendar proposal p1 approved", shown="calendar approval",
                                       settled=True)
        feedback = m.deliver_feedback("demo", "too long", shown="👎", mid="rating-1")
        for path in (decided, feedback):
            row = next(r for r in m.history() if r["file"] == path.name)
            self.assertFalse(web_chat.message_item("demo", row, {})["cancellable"])
            with self.assertRaisesRegex(web_chat.Refused, "can't be taken back"):
                web_chat.uncycle("demo", web_tests.ME, file=path.name)
            self.assertTrue(path.exists())
        plain = self.note()
        row = next(r for r in m.history() if r["file"] == plain)
        self.assertTrue(web_chat.message_item("demo", row, {})["cancellable"])
        no_id, _ = web_chat.background("demo", web_tests.ME, "from an older page", [])
        self.assertEqual(web_chat.uncycle("demo", web_tests.ME, file=no_id), "next-cycle note cancelled")

    def test_the_message_page_says_cancelled_not_read(self):
        file = self.note()
        web_chat.uncycle("demo", web_tests.ME, file=file)
        [row] = messages.history()
        self.assertIn("cancelled before demo read it", str(web_views.message_page(row, {}, ["demo"], "t")))

    def test_the_app_gets_the_cancel_time_with_its_offset(self):
        """Found in the simulator: the app could not read a feed holding a cancelled note — `cancelled` went out
        without the offset every time the app reads has (docs/app-api.md §1.6)."""
        from supervisor import app_api
        file = self.note()
        web_chat.uncycle("demo", web_tests.ME, file=file)
        [item] = app_api.to_json(web_chat.from_feed("demo"))["items"]
        self.assertEqual(item["cancelled"], app_api.stamp(messages.history()[0]["cancelled"]))
        self.assertRegex(item["cancelled"], r"[+-]\d\d:\d\d$|Z$")
