"""Kept ratings stay local; explicit sends reach the next chat turn or the inbox once."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from supervisor import core, feedback, messages, web, web_chat, web_views
from supervisor.tests.test_web import WebTest


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-feedback-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        patch = mock.patch.object(feedback, "FILE", self.root / "feedback.jsonl")
        patch.start()
        self.addCleanup(patch.stop)
        self.target = {"kind": "chat", "chat": "web-abc", "k": 1}

    def rate(self, thumb="down", **kw):
        return feedback.rate("wisp", "operator", self.target, thumb, kw.get("reason", "wrong"), "reply time", "answer")

    def test_feedback_fold_latest_wins_and_clear(self):
        old = self.rate()
        self.assertEqual(feedback.down_shas("wisp"), {old["reply_sha"]})
        self.assertEqual(feedback.items("other"), [])
        latest = self.rate("up")
        feedback.clear(old["id"], "operator")
        self.assertEqual(feedback.items(), [latest])
        self.assertEqual(feedback.down_shas("wisp"), set())
        self.assertEqual(feedback.by_target("wisp"), {"chat:web-abc:1": latest})
        feedback.mark_sent(latest["id"], "inbox", "note.md")
        item = feedback.items()[0]
        self.assertEqual(item["sent"]["via"], "inbox")
        self.assertEqual(item["sent"]["message"], "note.md")
        feedback.clear(latest["id"], "operator")
        self.assertEqual(feedback.items(), [])
        self.assertTrue(latest["id"].startswith("f-"))

    def test_malformed_lines_skipped_and_append_invalidates_cache(self):
        self.assertEqual(feedback.items(), [])
        first = self.rate()
        with feedback.FILE.open("ab") as f:
            f.write(b'not json\n\xff\n[]\n{"id": "bad", "event": "rated"}\n')
        self.assertEqual(feedback.items(), [first])
        second = self.rate("up", reason="x" * 2100)
        self.assertEqual(feedback.items(), [second])
        self.assertEqual(len(second["reason"]), 2000)
        self.assertEqual(second["reply_sha"], "0db52f4076c082518412afd3dd3576e2cb0c63703fd7fed5e23ade60efef31d9")
        # Same size and mtime, different inode: an atomic replacement still invalidates.
        replacement = feedback.FILE.with_suffix(".new")
        data = feedback.FILE.read_bytes().replace(b'"up"', b'"xx"')
        replacement.write_bytes(data)
        import os
        st = feedback.FILE.stat()
        os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
        replacement.replace(feedback.FILE)
        self.assertEqual(feedback.items(), [first])

    def test_unreadable_file_and_isolated_return_values(self):
        self.rate()
        feedback.items()[0]["target"]["k"] = 99
        self.assertEqual(feedback.items()[0]["target"]["k"], 1)
        with mock.patch.object(Path, "open", side_effect=PermissionError), mock.patch.dict(feedback._cache, stamp=None):
            self.assertEqual(feedback.items(), [])


class FeedbackRouteTest(WebTest):
    def setUp(self):
        super().setUp()
        self.inst = self.sup / "test-instances"
        (self.inst / "wisp" / "shaft").mkdir(parents=True)
        (self.inst / "wisp" / ".git").write_text("gitdir: test")
        for patch in (mock.patch.object(feedback, "FILE", self.sup / "feedback.jsonl"),
                      mock.patch.object(web_chat, "SUP_DIR", self.sup),
                      mock.patch.object(messages, "SUP_DIR", self.sup),
                      mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
                      mock.patch.object(messages, "INSTANCES", self.inst),
                      mock.patch.object(core, "INSTANCES", self.inst),
                      mock.patch.object(web, "_names", return_value=["wisp", "other"])):
            patch.start()
            self.addCleanup(patch.stop)
        for patch in (mock.patch.object(web_chat, "_docs_now", return_value={}),
                      mock.patch.object(web_chat, "_start"),
                      mock.patch.object(web_chat, "log"),
                      mock.patch("supervisor.doc_comments.take_notices", return_value=[]),
                      mock.patch("supervisor.doc_comments.waiting", return_value="")):
            patch.start()
            self.addCleanup(patch.stop)
        messages._cache.update(key=None, rows=[])
        self.cookie = self.paired()
        self.chat = {"id": "web-abc", "instance": "wisp", "state": "ended",
                     "created": "2026-10-29T10:00:00", "ended": "2026-10-29T10:00:03", "turns": [
            {"role": "user", "content": "question", "ts": "2026-10-29T10:00:00"},
            {"role": "assistant", "content": "server answer", "ts": "2026-10-29T10:00:01"}]}
        web_chat.save(self.chat)
        self.message = {"from": "wisp", "to": "operator", "file": "note.md", "kind": "note",
                        "text": "server message", "ts": "2026-10-29T10:00:02", "read": True}
        (self.sup / "messages.jsonl").write_text(json.dumps(self.message) + "\n")

    def post(self, name="wisp", **fields):
        return self.req("/feedback/" + name, "POST", cookie=self.cookie, body=urlencode(fields).encode(),
                        headers={"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie)})

    def test_rate_chat_uses_server_reply_and_clear(self):
        r = self.post(op="rate", kind="chat", cid="web-abc", k="1", thumb="up",
                      reply_text="forged", reply_sha="forged", reply_ts="forged")
        self.assertEqual(r.status, 200, r.body)
        item = feedback.items()[0]
        self.assertEqual(item["reply_sha"], feedback.reply_sha("server answer"))
        self.assertEqual(item["reply_ts"], "2026-10-29T10:00:01")
        self.assertEqual(self.post(name="other", op="clear", id=item["id"]).status, 409)
        self.assertEqual(self.post(op="clear", id=item["id"]).status, 200)
        self.assertEqual(feedback.items(), [])

    def test_rate_sent_message(self):
        r = self.post(op="rate", kind="message", file="note.md", thumb="down", reason="Wrong")
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(feedback.items()[0]["reply_sha"], feedback.reply_sha("server message"))
        self.assertEqual(feedback.items()[0]["target"], {"kind": "message", "file": "note.md"})

    def test_bad_targets_refused(self):
        for name, fields in [("wisp", dict(kind="chat", cid="web-abc", k="0")),
                             ("other", dict(kind="chat", cid="web-abc", k="1")),
                             ("wisp", dict(kind="chat", cid="web-abc", k="-1")),
                             ("wisp", dict(kind="chat", cid="web-abc", k="bad")),
                             ("wisp", dict(kind="message", file="unknown.md")),
                             ("other", dict(kind="message", file="note.md"))]:
            with self.subTest(name=name, fields=fields):
                self.assertEqual(self.post(name=name, op="rate", thumb="up", **fields).status, 409)
        self.message["to"] = "other"
        (self.sup / "messages.jsonl").write_text(json.dumps(self.message) + "\n")
        self.assertEqual(self.post(op="rate", kind="message", file="note.md", thumb="up").status, 409)
        self.assertEqual(self.post(name="missing", op="rate").status, 404)
        self.assertEqual(self.post(op="send").status, 409)
        self.assertEqual(feedback.items(), [])

    def test_rating_changes_the_next_keyed_feed_refresh(self):
        url = "/fragment/chat/wisp?id=web-abc"
        first = self.req(url, cookie=self.cookie)
        self.assertEqual(first.status, 200)
        unchanged = self.req(url, cookie=self.cookie, headers={"if-none-match": first.headers["ETag"]})
        self.assertEqual(unchanged.status, 304)
        self.post(op="rate", kind="chat", cid="web-abc", k="1", thumb="up")
        changed = self.req(url, cookie=self.cookie, headers={"if-none-match": first.headers["ETag"]})
        self.assertEqual(changed.status, 200)
        self.assertIn(b'data-k="a1"', changed.body)
        self.assertIn(b'data-thumb="up" aria-pressed="true"', changed.body)
        self.assertNotEqual(changed.headers["ETag"], first.headers["ETag"])

    def test_feedback_requires_pairing_and_csrf(self):
        self.assertEqual(self.req("/feedback/wisp", "POST").status, 403)
        self.assertEqual(self.req("/feedback/wisp", "POST", cookie=self.cookie,
                                  headers={"sec-fetch-site": "same-origin"}).status, 403)
        self.assertEqual(feedback.items(), [])

    def test_feedback_kept_never_reaches_instance(self):
        before = sorted(self.inst.rglob("*"))
        log = (self.sup / "messages.jsonl").read_bytes()
        for kind, fields in [("chat", dict(cid="web-abc", k="1")), ("message", dict(file="note.md"))]:
            self.assertEqual(self.post(op="rate", kind=kind, thumb="down", reason="kept", **fields).status, 200)
        self.assertEqual(sorted(self.inst.rglob("*")), before)
        self.assertEqual({p.name for p in (self.inst / "wisp").iterdir()}, {"shaft", ".git"})
        self.assertEqual((self.sup / "messages.jsonl").read_bytes(), log)
        self.assertEqual(web_chat.load("web-abc"), self.chat)
        self.assertEqual(len(feedback.items()), 2)

    def test_lit_state_on_ended_chat_sent_card_and_message_page(self):
        self.post(op="rate", kind="chat", cid="web-abc", k="1", thumb="down", reason="bad <answer>")
        self.post(op="rate", kind="message", file="note.md", thumb="up")
        chat = str(web_views.chat_feed("wisp", self.chat, [], archived=True))
        self.assertIn('data-thumb="down" aria-pressed="true"', chat)
        self.assertIn('title="bad &lt;answer&gt;"', chat)
        self.assertNotIn("data-thumb", chat.split('class="turn it"')[0])
        for html in (str(web_views._sent_cards("wisp", [self.message])[0]),
                     str(web_views.message_page(self.message, {}, ["wisp"], "csrf"))):
            self.assertIn('data-thumb="up" aria-pressed="true"', html)
            self.assertIn('data-feedback-name="wisp"', html)
        operator = {**self.message, "from": "operator", "to": "wisp"}
        self.assertNotIn("data-thumb", str(web_views.message_page(operator, {}, ["wisp"], "csrf")))
        self.assertNotIn("data-thumb", str(web_views._sent_cards("wisp", [operator])[0]))


    def open(self, state="idle"):
        self.chat.pop("ended", None)
        self.chat["state"] = state
        self.chat["reply"] = {"interjections": 0} if state == "replying" else None
        web_chat.save(self.chat)

    def rated(self, thumb="down", **fields):
        fields = {"kind": "chat", "cid": "web-abc", "k": "1", **fields}
        r = self.post(op="rate", thumb=thumb, **fields)
        self.assertEqual(r.status, 200, r.body)
        return r.headers["X-Feedback-Id"]

    def sent(self, fid, **fields):
        r = self.post(op="send", id=fid, **fields)
        self.assertEqual(r.status, 200, r.body)
        return feedback.items("wisp")[0]

    def inbox(self):
        return list((self.inst / "wisp" / "inbox").glob("*.md"))

    def test_feedback_note_reason_ending_in_a_full_stop_gets_no_second(self):
        # The reason "Checked the archive too." was delivered as "...too.." (seen in the browser check, 2026-10-29).
        item = {"target": {"kind": "chat", "chat": "web-abc", "k": 3}, "thumb": "up", "reply_ts": "2026-10-29T10:00:00",
                "reason": "Checked the archive too."}
        note = feedback.note(item, "answer")
        self.assertIn("What was good: Checked the archive too.\n", note)
        self.assertNotIn("..", note)
        self.assertNotIn("reply 3", note)
        self.assertIn("What was good: no reason given.\n", feedback.note({**item, "reason": ""}, "answer"))

    def test_feedback_send_rides_next_message_once(self):
        self.open()
        fid = self.rated(reason="check it")
        self.sent(fid)
        html = str(web_views.chat_feed("wisp", web_chat.load("web-abc"), []))
        self.assertIn('data-k="queue-feedback-' + fid + '"', html)
        self.assertIn("👎 on its reply of", html)   # named by its time, not the turn index
        self.assertIn("goes with your next message", html)
        self.assertEqual(self.inbox(), [])
        with self.assertRaisesRegex(web_chat.Refused, "empty message"):
            web_chat.send("wisp", "operator", "", [])
        chat, _ = web_chat.send("wisp", "operator", "try again", [], mid="next")
        self.assertEqual(chat["turns"][-1]["feedback"], [fid])
        expected = ("try again\n\n[Operator feedback: 👎 on your reply of 2026-10-29 10:00 "
                    "(web chat web-abc):\n «server answer»\n What was wrong: check it.\n "
                    "If it names something you got wrong, record it with record_correction.]")
        self.assertEqual(web_chat._api_messages(web_chat.load("web-abc"))[-1]["content"], expected)
        self.assertNotIn("pending_feedback", chat)
        self.assertEqual(feedback.items()[0]["sent"]["via"], "chat")
        self.assertIn("👎 feedback", str(web_views.chat_feed("wisp", chat, [])))
        again, _ = web_chat.send("wisp", "operator", "try again", [], mid="next")
        self.assertEqual(again["turns"], chat["turns"])
        chat["state"] = "idle"
        web_chat.save(chat)
        later, _ = web_chat.send("wisp", "operator", "next question", [])
        self.assertEqual(later["turns"][-1]["content"], "next question")
        self.assertEqual(self.inbox(), [])

    def test_feedback_pending_delivered_at_chat_end(self):
        for clear in (False, True):
            with self.subTest(clear=clear):
                self.open()
                fid = self.rated()
                self.sent(fid)
                with mock.patch.object(web_chat, "_post", return_value=(202, {})):
                    if clear:
                        web_chat.clear("wisp", "web-abc")
                    else:
                        web_chat.end("web-abc")
                item = feedback.items()[0]
                self.assertEqual(item["sent"]["via"], "inbox")
                self.assertNotIn("pending_feedback", web_chat.load("web-abc"))
                self.assertEqual(self.post(op="send", id=fid).status, 409)
        self.assertEqual(len(self.inbox()), 2)
        self.assertEqual([m["kind"] for m in messages.history()[1:]], ["feedback", "feedback"])

    def test_feedback_thumbs_up_send_goes_to_inbox_only(self):
        self.open()
        fid = self.rated("up")
        item = self.sent(fid, reason="precise")
        self.assertNotEqual(item["id"], fid)  # changed reasons create a new current rating
        self.assertEqual(item["sent"]["via"], "inbox")
        self.assertEqual(len(self.inbox()), 1)
        self.assertNotIn("pending_feedback", web_chat.load("web-abc"))
        chat, _ = web_chat.send("wisp", "operator", "next", [])
        self.assertEqual(chat["turns"][-1]["content"], "next")
        row = messages.history()[-1]
        self.assertEqual((row["kind"], row["via"], row["mid"]), ("feedback", messages.BACKGROUND, item["id"]))
        expected = ("[Operator feedback: 👍 on your reply of 2026-10-29 10:00 (web chat web-abc):\n"
                    " «server answer»\n What was good: precise.\n This is information about what was useful to the "
                    "operator. It is not a score and changes no grade.]")
        self.assertEqual(row["text"], expected)
        self.assertTrue(self.inbox()[0].read_text().endswith(expected + "\n"))
        self.assertIn("👍 feedback on its reply of 10:00", str(web_views._later_card(row)))
        self.assertIn("feedback", str(web_views.message_page(row, {}, ["wisp"], "csrf")))

    def test_feedback_send_ended_chat_and_card_to_inbox(self):
        fid = self.rated()
        self.sent(fid)
        fid = self.rated(kind="message", file="note.md")
        self.sent(fid)
        self.assertEqual(len(self.inbox()), 2)
        self.assertEqual([r["kind"] for r in messages.history()[1:]], ["feedback", "feedback"])
        self.assertEqual(web_chat.load("web-abc"), self.chat)

    def test_feedback_send_non_open_chat_to_inbox(self):
        self.open()
        web_chat.save({**self.chat, "id": "web-def", "created": "2026-10-29T11:00:00"})
        self.sent(self.rated())
        self.assertEqual(len(self.inbox()), 1)
        self.assertNotIn("pending_feedback", web_chat.load("web-abc"))

    def test_feedback_send_twice_refused(self):
        for state in ("idle", "ended"):
            with self.subTest(state=state):
                self.open(state)
                fid = self.rated()
                self.sent(fid)
                r = self.post(op="send", id=fid, reason="changed on retry")
                self.assertEqual(r.status, 409, r.body)
                self.assertIn(b"already", r.body)
        self.assertEqual(len(self.inbox()), 1)

    def test_feedback_rerated_pending_dropped(self):
        self.open()
        self.sent(self.rated())
        self.rated("up")
        chat, _ = web_chat.send("wisp", "operator", "next", [])
        self.assertEqual(chat["turns"][-1]["content"], "next")
        self.assertNotIn("pending_feedback", chat)
        self.assertIsNone(feedback.items()[0]["sent"])
        self.assertEqual(self.inbox(), [])

    def test_feedback_unpend_keeps_rating_and_clear_drops_pending(self):
        self.open()
        fid = self.rated()
        self.sent(fid)
        self.assertEqual(self.post(op="unpend", id=fid).status, 200)
        self.assertEqual(web_chat.pending_feedback(web_chat.load("web-abc")), [])
        self.assertEqual(feedback.items()[0]["id"], fid)
        self.sent(fid)
        self.post(op="clear", id=fid)
        chat, _ = web_chat.send("wisp", "operator", "next", [])
        self.assertEqual(chat["turns"][-1]["content"], "next")
        self.assertEqual(self.inbox(), [])

    def test_feedback_waits_through_interjection_then_joins_queued_turn(self):
        self.open("replying")
        fid = self.rated()
        self.sent(fid)
        with mock.patch.object(web_chat, "_post", return_value=(202, {})) as post:
            chat, _ = web_chat.send("wisp", "operator", "meanwhile", [])
        self.assertEqual(post.call_args.args[2]["messages"][-1]["content"], "meanwhile")
        self.assertEqual([p["id"] for p in chat["pending_feedback"]], [fid])
        web_chat.queue("wisp", "operator", "afterwards", [], mid="q1")
        chat = web_chat.load("web-abc")
        self.assertTrue(web_chat._flush_queue(chat))
        web_chat.save(chat)
        self.assertEqual(chat["turns"][-1]["feedback"], [fid])
        self.assertIn("Operator feedback: 👎", chat["turns"][-1]["content"])
        self.assertFalse(web_chat._flush_queue(chat))
        self.assertEqual(feedback.items()[0]["sent"]["via"], "chat")

    def test_feedback_retry_after_inbox_receipt_avoids_second_delivery(self):
        fid = self.rated()
        with mock.patch.object(feedback, "mark_sent", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                self.post(op="send", id=fid)
        self.sent(fid)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(len(messages.history()), 2)

    def test_feedback_clear_sent_explains_already_delivered(self):
        fid = self.rated()
        self.sent(fid)
        r = self.post(op="clear", id=fid)
        self.assertIn(b"already delivered", r.body)
        self.assertEqual(feedback.items(), [])
        self.assertEqual(len(self.inbox()), 1)


    def test_feedback_send_link_and_sent_time(self):
        fid = self.rated("up", reason="helpful")
        html = str(web_views._feedback_buttons("wisp", {"kind": "chat", "chat": "web-abc", "k": 1},
                                              feedback.by_target("wisp")))
        self.assertIn("data-feedback-send>Send…</button>", html)
        self.assertIn('data-feedback-thumb="up"', html)
        self.sent(fid)
        html = str(web_views._feedback_buttons("wisp", {"kind": "chat", "chat": "web-abc", "k": 1},
                                              feedback.by_target("wisp")))
        self.assertNotIn("data-feedback-send", html)
        self.assertRegex(html, r'sent <time[^>]*>\d\d:\d\d</time>')

    def test_feedback_pending_at_end_retries_without_duplicate(self):
        self.open()
        fid = self.rated()
        self.sent(fid)
        with mock.patch.object(web_chat, "_post", return_value=(202, {})):
            with mock.patch.object(feedback, "mark_sent", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    web_chat.end("web-abc")
            web_chat.end("web-abc")
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(feedback.items()[0]["sent"]["via"], "inbox")

    def test_feedback_note_quotes_only_first_300_characters(self):
        item = feedback.rate("wisp", "operator", {"kind": "message", "file": "note.md"}, "down", "",
                             "2026-10-29T14:02:00", "x" * 301)
        self.assertEqual(feedback.note(item, "x" * 300 + "SECRET"),
                         "[Operator feedback: 👎 on your reply of 2026-10-29 14:02 (message note.md):\n «" +
                         "x" * 300 + "…»\n What was wrong: no reason given.\n "
                         "If it names something you got wrong, record it with record_correction.]")


    def test_feedback_retry_after_chat_receipt_refused(self):
        self.open()
        fid = self.rated()
        self.sent(fid)
        with mock.patch.object(feedback, "mark_sent", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                web_chat.send("wisp", "operator", "next", [])
        self.assertIsNone(feedback.items()[0]["sent"])
        r = self.post(op="send", id=fid, reason="changed on retry")
        self.assertEqual(r.status, 409, r.body)
        self.assertIn(b"already delivered", r.body)
        self.assertEqual(feedback.items()[0]["sent"]["via"], "chat")
        self.assertEqual(self.inbox(), [])
