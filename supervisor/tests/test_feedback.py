"""Kept feedback survives refreshes without delivering anything to an instance."""
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
        return feedback.rate("wren", "operator", self.target, thumb, kw.get("reason", "wrong"), "reply time", "answer")

    def test_feedback_fold_latest_wins_and_clear(self):
        old = self.rate()
        self.assertEqual(feedback.down_shas("wren"), {old["reply_sha"]})
        self.assertEqual(feedback.items("other"), [])
        latest = self.rate("up")
        feedback.clear(old["id"], "operator")
        self.assertEqual(feedback.items(), [latest])
        self.assertEqual(feedback.down_shas("wren"), set())
        self.assertEqual(feedback.by_target("wren"), {"chat:web-abc:1": latest})
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
        (self.inst / "wren").mkdir(parents=True)
        for patch in (mock.patch.object(feedback, "FILE", self.sup / "feedback.jsonl"),
                      mock.patch.object(web_chat, "SUP_DIR", self.sup),
                      mock.patch.object(messages, "SUP_DIR", self.sup),
                      mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
                      mock.patch.object(messages, "INSTANCES", self.inst),
                      mock.patch.object(core, "INSTANCES", self.inst),
                      mock.patch.object(web, "_names", return_value=["wren", "other"])):
            patch.start()
            self.addCleanup(patch.stop)
        self.cookie = self.paired()
        self.chat = {"id": "web-abc", "instance": "wren", "state": "ended",
                     "created": "2026-10-29T10:00:00", "ended": "2026-10-29T10:00:03", "turns": [
            {"role": "user", "content": "question", "ts": "2026-10-29T10:00:00"},
            {"role": "assistant", "content": "server answer", "ts": "2026-10-29T10:00:01"}]}
        web_chat.save(self.chat)
        self.message = {"from": "wren", "to": "operator", "file": "note.md", "kind": "note",
                        "text": "server message", "ts": "2026-10-29T10:00:02", "read": True}
        (self.sup / "messages.jsonl").write_text(json.dumps(self.message) + "\n")

    def post(self, name="wren", **fields):
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
        for name, fields in [("wren", dict(kind="chat", cid="web-abc", k="0")),
                             ("other", dict(kind="chat", cid="web-abc", k="1")),
                             ("wren", dict(kind="chat", cid="web-abc", k="-1")),
                             ("wren", dict(kind="chat", cid="web-abc", k="bad")),
                             ("wren", dict(kind="message", file="unknown.md")),
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
        url = "/fragment/chat/wren?id=web-abc"
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
        self.assertEqual(self.req("/feedback/wren", "POST").status, 403)
        self.assertEqual(self.req("/feedback/wren", "POST", cookie=self.cookie,
                                  headers={"sec-fetch-site": "same-origin"}).status, 403)
        self.assertEqual(feedback.items(), [])

    def test_feedback_kept_never_reaches_instance(self):
        before = sorted(self.inst.rglob("*"))
        log = (self.sup / "messages.jsonl").read_bytes()
        for kind, fields in [("chat", dict(cid="web-abc", k="1")), ("message", dict(file="note.md"))]:
            self.assertEqual(self.post(op="rate", kind=kind, thumb="down", reason="kept", **fields).status, 200)
        self.assertEqual(sorted(self.inst.rglob("*")), before)
        self.assertEqual(list((self.inst / "wren").iterdir()), [])
        self.assertEqual((self.sup / "messages.jsonl").read_bytes(), log)
        self.assertEqual(web_chat.load("web-abc"), self.chat)
        self.assertEqual(len(feedback.items()), 2)

    def test_lit_state_on_ended_chat_sent_card_and_message_page(self):
        self.post(op="rate", kind="chat", cid="web-abc", k="1", thumb="down", reason="bad <answer>")
        self.post(op="rate", kind="message", file="note.md", thumb="up")
        chat = str(web_views.chat_feed("wren", self.chat, [], archived=True))
        self.assertIn('data-thumb="down" aria-pressed="true"', chat)
        self.assertIn('title="bad &lt;answer&gt;"', chat)
        self.assertNotIn("data-thumb", chat.split('class="turn it"')[0])
        for html in (str(web_views._sent_cards("wren", [self.message])[0]),
                     str(web_views.message_page(self.message, {}, ["wren"], "csrf"))):
            self.assertIn('data-thumb="up" aria-pressed="true"', html)
            self.assertIn('data-feedback-name="wren"', html)
        operator = {**self.message, "from": "operator", "to": "wren"}
        self.assertNotIn("data-thumb", str(web_views.message_page(operator, {}, ["wren"], "csrf")))
        self.assertNotIn("data-thumb", str(web_views._sent_cards("wren", [operator])[0]))
