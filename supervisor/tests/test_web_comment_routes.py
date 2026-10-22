"""Comment POSTs through the paired-browser, login and CSRF gates; no API sockets."""
import json
import urllib.parse
from unittest import mock

from supervisor import core, doc_comments as comments, docs, web, web_chat
from supervisor.tests.test_web import ME, WebTest


class CommentRoutesTest(WebTest):
    def setUp(self):
        super().setUp()
        self.root = self.sup / "mind" / "wren"
        (self.root / "notes/docs").mkdir(parents=True)
        (self.root / "notes/docs/plan.md").write_text("a plan")
        for patcher in (mock.patch.object(core, "SUP_DIR", self.sup),
                        mock.patch.object(core, "instance_root", return_value=self.root),
                        mock.patch.object(docs, "instance_root", return_value=self.root),
                        mock.patch.object(web, "_names", return_value=["wren"]),
                        mock.patch.object(web, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)
        # The real one runs the CLI as a subprocess against the project's instances; record the calls instead.
        patcher = mock.patch.object(web_chat, "_sync_copies")
        self.sync = patcher.start()
        self.addCleanup(patcher.stop)
        self.cookie = self.paired()
        self.pid = "p-0000000000000001"

    def post(self, action="", *, name="wren", csrf=True, **form):
        headers = {"sec-fetch-site": "same-origin", "content-type": "application/x-www-form-urlencoded"}
        if csrf:
            headers["x-csrf-token"] = self.csrf(self.cookie)
        return self.req(f"/docs/{name}/comment" + (f"/{action}" if action else ""), "POST",
                        cookie=self.cookie, body=urllib.parse.urlencode(form).encode(), headers=headers)

    def add(self, **fields):
        return self.post(**{"pid": self.pid, "doc": "plan.md", "anchor": "", "sha": "seen-sha",
                            "text": "Question", **fields})

    def sent(self, name, login, pid):
        comments.mark_sent(name, pid, "web-test#0")
        return {}, f"sent to {name}"

    def test_unticked_thread_is_stored_unsent_and_reply_uses_same_thread(self):
        with mock.patch.object(web_chat, "send_comment") as send:
            r = self.add()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["X-Thread"], "c1")
            self.assertIn(b"kept, not sent: wren sees it in notes/comments/", r.body)
            send.assert_not_called()
        # Bug 2026-10-20: an unsent comment never reached the instance's copy, so "review my comments" found nothing.
        self.sync.assert_called_once_with("wren", ["plan.md"])
        thread = comments.threads("wren")[0]
        self.assertEqual((thread["doc"], thread["sha"], thread["anchor"]), ("plan.md", "seen-sha", None))
        self.assertIsNone(thread["posts"][0]["sent"])
        self.assertEqual(thread["posts"][0]["by"], "operator")
        r = self.post(pid="p-0000000000000002", thread="c1", text="Reply")
        self.assertEqual((r.status, r.headers["X-Thread"]), (200, "c1"))
        self.assertEqual([p["text"] for p in comments.threads("wren")[0]["posts"]], ["Question", "Reply"])

    def test_ticked_thread_sends_with_login_and_marks_post(self):
        anchor = {"exact": "plan", "prefix": "a ", "suffix": "", "start": 2}
        with mock.patch.object(web_chat, "send_comment", side_effect=self.sent) as send:
            r = self.add(send="1", anchor=json.dumps(anchor))
            send.assert_called_once_with("wren", ME, self.pid)
        self.assertEqual(r.status, 200)
        self.assertIn(b"sent to wren", r.body)
        self.assertEqual(comments.get_post("wren", self.pid)["sent"], "web-test#0")
        self.assertEqual(comments.threads("wren")[0]["anchor"], anchor)

    def test_unsent_edit_delete_and_sent_refusals(self):
        self.add()
        self.sync.reset_mock()
        r = self.post("edit", pid=self.pid, text="Edited")
        self.assertEqual((r.status, r.headers["X-Thread"]), (200, "c1"))
        self.assertEqual(comments.get_post("wren", self.pid)["text"], "Edited")
        self.sync.assert_called_once_with("wren", ["plan.md"])      # the copy shows the edited text
        comments.mark_sent("wren", self.pid, "web-test#0")
        for action in ("edit", "delete"):
            r = self.post(action, pid=self.pid, text="No")
            self.assertEqual(r.status, 409)
            self.assertIn(b"refused:", r.body)
        self.assertEqual(comments.get_post("wren", self.pid)["text"], "Edited")
        other = "p-0000000000000002"
        self.add(pid=other)
        self.sync.reset_mock()
        r = self.post("delete", pid=other)
        self.assertEqual((r.status, r.headers["X-Thread"]), (200, "c2"))
        self.sync.assert_called_once_with("wren", ["plan.md"])      # and no longer holds a deleted one
        self.assertEqual([t["id"] for t in comments.threads("wren")], ["c1"])

    def test_send_existing_post_and_resolve_queues_notice_and_syncs(self):
        self.add()
        with mock.patch.object(web_chat, "send_comment", side_effect=self.sent) as send:
            r = self.post("send", pid=self.pid)
            send.assert_called_once_with("wren", ME, self.pid)
        self.assertEqual((r.status, r.headers["X-Thread"]), (200, "c1"))
        self.assertEqual(comments.get_post("wren", self.pid)["sent"], "web-test#0")
        with mock.patch.object(web_chat, "_sync_copies") as sync:
            r = self.post("state", thread="c1", state="resolved")
            sync.assert_called_once_with("wren", ["plan.md"])
        self.assertEqual((r.status, r.headers["X-Thread"]), (200, "c1"))
        self.assertEqual(comments.threads("wren")[0]["state"], "resolved")
        self.assertEqual(comments.take_notices("wren"), ["[The operator resolved c1 on notes/docs/plan.md]"])

    def test_invalid_inputs_and_missing_csrf_do_not_create_posts(self):
        for fields in ({"pid": "bad"}, {"doc": "../escape.md"}, {"anchor": "{"},
                       {"anchor": "[]"}, {"thread": "c999"}):
            with self.subTest(fields=fields):
                r = self.add(**fields)
                self.assertEqual(r.status, 409)
                self.assertIn(b"refused:", r.body)
        self.assertEqual(self.add(csrf=False).status, 403)
        self.assertEqual(self.add(name="missing").status, 404)
        self.assertEqual(comments.threads("wren"), [])
        for action in ("edit", "delete", "send"):
            self.assertEqual(self.post(action, pid=self.pid, text="No").status, 409)
        self.assertEqual(self.post("state", thread="c999", state="resolved").status, 409)

    def test_send_failure_keeps_new_post_unsent_and_explicit_send_refuses(self):
        with mock.patch.object(web_chat, "send_comment", side_effect=web_chat.Refused("busy <now>")):
            r = self.add(send="1")
            self.assertEqual((r.status, r.headers["X-Thread"]), (200, "c1"))
            self.assertIn(b"saved, but not sent: busy &lt;now&gt;", r.body)
            self.assertIsNone(comments.get_post("wren", self.pid)["sent"])
            self.assertEqual(self.post("send", pid=self.pid).status, 409)
        self.assertEqual(len(comments.threads("wren")[0]["posts"]), 1)
