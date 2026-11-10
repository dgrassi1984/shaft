"""M1a comment sends and attribution through the existing API/relay test harness."""
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from supervisor import actions, doc_comments as comments, web_chat
from supervisor.tests.test_web_chat import ChatTest, ME, chunk


_start_relay = web_chat._start


class CommentChatTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shaft-chat-comments-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.serial = 0
        self.api = SimpleNamespace(requests=[], interject_status=202)

        def post(chat, route, body, timeout=15):
            self.api.requests.append((f"/v1/chat/{route}", {}, body))
            return self.api.interject_status, {}

        for patcher in (mock.patch.object(web_chat, "SUP_DIR", self.dir / "sup"),
                        mock.patch("supervisor.core.SUP_DIR", self.dir / "sup"),
                        mock.patch("supervisor.docs.instance_root", return_value=self.dir / "mind"),
                        mock.patch.object(web_chat, "_headers", return_value={}),
                        mock.patch.object(web_chat, "_post", side_effect=post),
                        mock.patch.object(web_chat, "log"), mock.patch("supervisor.webpush.notify")):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(actions, "run_now", return_value=(True, "synced"))
        self.sync = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(web_chat, "_start")
        self.start = patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, sent=False):
        self.serial += 1
        pid = f"p-{self.serial:016x}"
        tid = comments.add_thread("wisp", pid, "plan.md", None, "none", f"Question {self.serial}")
        if sent:
            comments.mark_sent("wisp", pid, "web-earlier#0")
        return tid, pid

    def thread(self, tid):
        return next(t for t in comments.threads("wisp") if t["id"] == tid)

    def finish(self, cid, answer="Answer", heard=0, status=200, stopped=False):
        # Exercise _run, including SSE decoding and finalization, with the harness's wire format.
        response = mock.MagicMock(status=status)
        response.__iter__.return_value = iter([chunk({"content": answer}),
                                              chunk({}, "stop", interjections_heard=heard), b"data: [DONE]\n"])
        response.read.return_value = b'{"error":{"message":"API failed"}}'
        conn = mock.Mock()
        conn.getresponse.return_value = response
        relay = web_chat.Relay(cid)
        web_chat._relays[cid] = relay
        if stopped:
            web_chat.stop(cid)
        with mock.patch.object(web_chat, "_api_conn", return_value=conn):
            web_chat._run(relay)
        return web_chat.load(cid)

    def test_idle_block_metadata_edits_and_duplicate_send(self):
        tid, pid = self.add()
        web_chat.doc_saved("wisp", ME, "plan.md", "old", "new", "sha")
        chat, _ = web_chat.send_comment("wisp", ME, pid)
        turn = chat["turns"][0]
        self.assertEqual(turn["shown"], "")
        self.assertEqual(turn["comments"], [{"id": tid, "doc": "plan.md"}])
        self.assertIn(comments.block("wisp", tid, pid), turn["content"])
        self.assertIn(web_chat.DOCS_NOTE, turn["content"])
        self.assertEqual(turn["edits"], ["plan.md"])
        self.assertEqual(chat["reply"]["comment_ids"], [tid])
        self.assertIsNone(chat["reply"]["started_by"])
        self.assertEqual(comments.get_post("wisp", pid)["sent"], f"{chat['id']}#0")
        web_chat.send_comment("wisp", ME, pid)
        self.start.assert_called_once_with(chat["id"])
        self.assertEqual(len(web_chat.load(chat["id"])["turns"]), 1)
        self.sync.assert_called_once_with(["docs", "wisp", "comments", "plan.md", "--sync"], who="web")

    def test_unsent_comments_reach_a_review_message_and_their_answers_land(self):
        # Bug 2026-10-20: comments left unsent were invisible to the instance, so the operator could not leave several
        # and then ask for a review. Now each message names them, and an answer under ### cN lands in its thread.
        c1, p1 = self.add()
        c2, p2 = self.add()
        _, p3 = self.add()
        comments.set_state("wisp", "c3", "resolved")
        chat, _ = web_chat.send("wisp", ME, "Please review my comments", [])
        content = chat["turns"][0]["content"]
        self.assertIn("notes/docs/plan.md (c1, c2) in notes/comments/plan.md", content)
        waiting = next(l for l in content.split("\n\n") if l.startswith("[Open comments"))
        self.assertNotIn("c3", waiting)                       # resolved: not waiting for an answer
        self.assertIsNone(comments.get_post("wisp", p1)["sent"])   # still not sent as a message: no chip, no block
        chat = self.finish(chat["id"], "### c1\nDone.\n### c2\nNo, because…")
        self.assertEqual(chat["turns"][-1]["answered"], [{"id": c1, "doc": "plan.md"}, {"id": c2, "doc": "plan.md"}])
        self.assertEqual(self.thread(c1)["posts"][-1]["text"], "Done.")
        self.assertEqual(comments.get_post("wisp", p2)["seen"], f"{chat['id']}#1")
        for op in (lambda: comments.edit("wisp", p1, "changed"), lambda: comments.delete("wisp", p2)):
            with self.assertRaisesRegex(comments.Refused, "already answered"):
                op()
        self.start.reset_mock()
        _, said = web_chat.send_comment("wisp", ME, p1)       # answered already: never sent again
        self.assertEqual(said, "wisp already answered it")
        self.start.assert_not_called()
        self.assertEqual(comments.waiting("wisp"), "")        # nothing left to name in the next message

    def test_sending_one_comment_does_not_name_it_as_waiting(self):
        _, p1 = self.add()
        self.add()
        chat, _ = web_chat.send_comment("wisp", ME, p1)
        content = chat["turns"][0]["content"]
        self.assertIn("notes/docs/plan.md (c2) in notes/comments/plan.md", content)
        self.assertNotIn("(c1, c2)", content)

    def test_copy_subprocess_does_not_hold_instance_lock_on_send_or_finish(self):
        acquired = []

        def sync(*args, **kwargs):
            def competing_send():
                lock = web_chat._inst_lock("wisp")
                got = lock.acquire(timeout=0.5)
                acquired.append(got)
                if got:
                    lock.release()
            worker = threading.Thread(target=competing_send)
            worker.start()
            worker.join(2)
            return True, "synced"

        self.sync.side_effect = sync
        _, pid = self.add()
        chat, _ = web_chat.send_comment("wisp", ME, pid)
        self.finish(chat["id"])
        self.assertEqual(acquired, [True, True])

    def test_mid_retry_after_mark_sent_failure_does_not_resend(self):
        _, pid = self.add()
        with mock.patch.object(comments, "mark_sent", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(web_chat.Refused, "disk full"):
                web_chat.send_comment("wisp", ME, pid)
        self.assertIsNone(comments.get_post("wisp", pid)["sent"])
        chat, _ = web_chat.send_comment("wisp", ME, pid)
        self.start.assert_called_once()
        self.assertEqual(len(chat["turns"]), 1)
        self.assertEqual(comments.get_post("wisp", pid)["sent"], f"{chat['id']}#0")

    def test_retry_after_mark_sent_failure_and_an_ended_chat_does_not_resend(self):
        """Grok review 2026-10-20: the mid dedup only sees the open chat; an ended one still says the post went."""
        _, pid = self.add()
        with mock.patch.object(comments, "mark_sent", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(web_chat.Refused, "disk full"):
                web_chat.send_comment("wisp", ME, pid)
        first = next(iter(web_chat.chats("wisp")))
        with web_chat._lock(first["id"]):
            c = web_chat.load(first["id"])
            c["state"], c["reply"] = "ended", None
            web_chat.save(c)
        self.assertIsNone(web_chat.open_chat("wisp"))
        _, said = web_chat.send_comment("wisp", ME, pid)
        self.assertEqual(said, "already sent to wisp")
        self.start.assert_called_once()                       # the first send only: never a second copy
        self.assertEqual(comments.get_post("wisp", pid)["sent"], f"{first['id']}#0")

    def test_section_c7_attaches_once_and_marks_assistant(self):
        for _ in range(7):
            tid, pid = self.add()
        chat, _ = web_chat.send_comment("wisp", ME, pid)
        c = self.finish(chat["id"], "Intro\n### c7\nThe answer")
        self.assertEqual(c["turns"][1]["answered"], [{"id": "c7", "doc": "plan.md"}])
        self.assertEqual(self.thread(tid)["posts"][-1]["text"], "The answer")
        comments.parse_answer("wisp", c["turns"][1]["content"], [tid], tid, f"{chat['id']}#1")
        self.assertEqual(len(self.thread(tid)["posts"]), 2)
        self.assertEqual(self.sync.call_count, 2)

    def test_single_comment_fallback(self):
        tid, pid = self.add()
        chat, _ = web_chat.send_comment("wisp", ME, pid)
        self.assertEqual(chat["reply"]["started_by"], tid)
        self.finish(chat["id"], "Whole answer")
        self.assertEqual(self.thread(tid)["posts"][-1]["text"], "Whole answer")

    def test_two_heard_comments_only_one_section(self):
        first, pid = self.add()
        second, p2 = self.add()
        chat, _ = web_chat.send_comment("wisp", ME, pid)
        c, _ = web_chat.send_comment("wisp", ME, p2)
        self.assertTrue(c["turns"][1]["interjected"])
        request = self.api.requests[-1]
        self.assertEqual(request[0], "/v1/chat/interject")
        self.assertIn(comments.block("wisp", second, p2), request[2]["messages"][-1]["content"])
        self.finish(chat["id"], f"### {first}\nFirst answer", heard=1)
        self.assertEqual(self.thread(second)["unsectioned"], [f"{chat['id']}#2"])
        self.assertEqual(len(self.thread(second)["posts"]), 1)

    def test_mixed_interjections_followup_and_no_fallback_after_any_interjection(self):
        first, p1 = self.add()
        heard, p2 = self.add()
        late, p3 = self.add()
        chat, _ = web_chat.send_comment("wisp", ME, p1)
        web_chat.send_comment("wisp", ME, p2)
        web_chat.send_comment("wisp", ME, p3)
        c = self.finish(chat["id"], "No sections", heard=1)
        self.assertEqual(c["reply"]["comment_ids"], [late])
        self.assertEqual(c["reply"]["started_by"], late)
        for tid in (first, heard):
            self.assertEqual(self.thread(tid)["unsectioned"], [f"{chat['id']}#3"])
        self.assertEqual(self.thread(late)["unsectioned"], [])
        c = self.finish(chat["id"], "Late answer")
        self.assertEqual(self.thread(late)["posts"][-1]["text"], "Late answer")
        self.assertEqual(c["turns"][4]["answered"], [{"id": late, "doc": "plan.md"}])
        self.assertEqual(self.thread(first)["unsectioned"], [f"{chat['id']}#3"])

    def test_unheard_text_interjection_also_disables_fallback(self):
        tid, pid = self.add()
        chat, _ = web_chat.send_comment("wisp", ME, pid)
        web_chat.send("wisp", ME, "Another question", [])
        self.finish(chat["id"], "No section", heard=0)
        self.assertEqual(len(self.thread(tid)["posts"]), 1)
        self.assertEqual(self.thread(tid)["unsectioned"], [f"{chat['id']}#2"])

    def test_stop_api_error_and_reap_attach_nothing(self):
        for mode in ("stop", "error", "reap"):
            with self.subTest(mode=mode):
                tid, pid = self.add()
                chat, _ = web_chat.send_comment("wisp", ME, pid)
                if mode == "reap":
                    chat["reply"]["answer"] = f"### {tid}\nPartial"
                    web_chat.save(chat)
                    web_chat.reap()
                    c = web_chat.load(chat["id"])
                else:
                    c = self.finish(chat["id"], f"### {tid}\nPartial", stopped=mode == "stop",
                                    status=500 if mode == "error" else 200)
                self.assertTrue(c["turns"][-1]["note"])
                self.assertNotIn("answered", c["turns"][-1])
                self.assertEqual(len(self.thread(tid)["posts"]), 1)
                self.assertEqual(self.thread(tid)["unsectioned"], [])

    def test_notices_after_edits_restored_on_failure_and_cannot_send_alone(self):
        tid, _ = self.add()
        comments.set_state("wisp", tid, "resolved")
        with self.assertRaisesRegex(web_chat.Refused, "empty"):
            web_chat.send("wisp", ME, "", [])
        web_chat.doc_saved("wisp", ME, "plan.md", "old", "new", "sha")
        chat, _ = web_chat.send("wisp", ME, "Hello", [])
        content = chat["turns"][0]["content"]
        self.assertLess(content.index("[The operator edited"), content.index("[The operator resolved"))
        self.assertEqual(comments.take_notices("wisp"), [])
        comments.set_state("wisp", tid, "open")
        _, pid = self.add()
        self.api.interject_status = 503
        with self.assertRaisesRegex(web_chat.Refused, "503"):
            web_chat.send_comment("wisp", ME, pid)
        self.assertIsNone(comments.get_post("wisp", pid)["sent"])
        self.assertEqual(len(web_chat.load(chat["id"])["turns"]), 1)
        self.api.interject_status = 202
        c, _ = web_chat.send_comment("wisp", ME, pid)
        self.assertIn("[The operator reopened c1", c["turns"][1]["content"])
        self.assertEqual(comments.take_notices("wisp"), [])

    def test_section_for_earlier_reply_attaches(self):
        tid, pid = self.add()
        chat, _ = web_chat.send_comment("wisp", ME, pid)
        self.finish(chat["id"], "Original answer")
        web_chat.send("wisp", ME, "Reconsider", [])
        c = self.finish(chat["id"], f"### {tid}\nRevised answer")
        self.assertEqual(c["turns"][3]["answered"], [{"id": tid, "doc": "plan.md"}])
        self.assertEqual(self.thread(tid)["posts"][-1]["text"], "Revised answer")

    def test_parser_and_sync_failures_do_not_break_chat(self):
        _, pid = self.add()
        self.sync.return_value = (False, "verb not available")
        chat, _ = web_chat.send_comment("wisp", ME, pid)
        with mock.patch.object(comments, "parse_answer", side_effect=RuntimeError("broken store")):
            c = self.finish(chat["id"])
        self.assertEqual(c["state"], "idle")
        self.assertEqual(c["turns"][-1]["content"], "Answer")
        self.assertFalse(web_chat.replying(chat["id"]))
        self.sync.side_effect = OSError("unavailable")
        web_chat._sync_copies("wisp", ["plan.md"])
        self.assertTrue(web_chat.log.called)

    def test_fast_relay_cannot_attach_before_mark_sent(self):
        tid, pid = self.add()
        response_done = threading.Event()
        response = mock.MagicMock(status=200)

        def stream():
            yield chunk({"content": "Fast answer"})
            response_done.set()
            yield b"data: [DONE]\n"

        response.__iter__.side_effect = stream
        conn = mock.Mock()
        conn.getresponse.return_value = response
        real_mark = comments.mark_sent
        real_parse = comments.parse_answer

        def mark(name, post, ref):
            self.assertTrue(response_done.wait(3), "relay never reached the end of the stream")
            self.assertEqual(len(self.thread(tid)["posts"]), 1)
            real_mark(name, post, ref)

        def parse(name, answer, ids, started_by, ref):
            self.assertFalse(web_chat._lock(ref.split("#")[0]).locked())
            self.assertTrue(comments.get_post(name, pid)["sent"])
            return real_parse(name, answer, ids, started_by, ref)

        self.start.side_effect = _start_relay
        with mock.patch.object(web_chat, "_api_conn", return_value=conn), \
                mock.patch.object(comments, "mark_sent", side_effect=mark), \
                mock.patch.object(comments, "parse_answer", side_effect=parse):
            chat, _ = web_chat.send_comment("wisp", ME, pid)
            c = ChatTest.wait_idle(self, chat["id"])
        self.assertEqual(c["turns"][1]["answered"], [{"id": tid, "doc": "plan.md"}])
        self.assertEqual(self.thread(tid)["posts"][-1]["text"], "Fast answer")

    def test_instance_opened_thread_marks_assistant(self):
        chat, _ = web_chat.send("wisp", ME, "Review", [])
        c = self.finish(chat["id"], '### comment on notes/docs/plan.md: "quote"\nQuestion')
        self.assertEqual(c["turns"][1]["opened"], [{"id": "c1", "doc": "plan.md"}])
        self.sync.assert_called_once()


class LiveCommentRelayTest(ChatTest):
    def test_fast_api_reply_waits_for_mark_sent(self):
        pid = "p-0000000000000001"
        tid = comments.add_thread("wisp", pid, "plan.md", None, "none", "Question")
        with mock.patch.object(actions, "run_now", return_value=(True, "")):
            chat, _ = web_chat.send_comment("wisp", ME, pid)
            c = self.wait_idle(chat["id"])
        self.assertEqual(c["turns"][1]["answered"], [{"id": tid, "doc": "plan.md"}])
        self.assertEqual(comments.threads("wisp")[0]["posts"][-1]["text"], "Answer 1.")
        self.assertIn("[The operator commented", self.api.chats()[0][2]["messages"][0]["content"])
