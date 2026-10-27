"""Earlier chats through the production routes, with isolated readers."""
import re
from unittest import mock

from supervisor import web_chat
from supervisor.tests.test_web import WebTest


class ArchiveTest(WebTest):
    def setUp(self):
        super().setUp()
        self.records = [{"id": f"web-{i:016x}", "instance": "wren", "state": "ended",
                         "created": f"2026-09-{i:02d}T12:00:00", "ended": "2026-09-26T13:00:00",
                         "turns": [{"role": "user", "content": f"Topic {i} " + "x" * 100 + "\nHidden second line",
                                    "ts": "2026-09-25T12:00:00"},
                                   {"role": "assistant", "content": "Done", "ts": "2026-09-25T12:01:00",
                                    "docs": [{"path": "a b.md", "created": True}]}]}
                        for i in range(25, 0, -1)]
        self.current = {**self.records[0], "id": "web-ffffffffffffffff", "state": "idle"}
        for patch in [mock.patch("supervisor.web._names", return_value=["wren"]),
                      mock.patch.object(web_chat, "chats", side_effect=lambda n: [self.current] + self.records),
                      mock.patch.object(web_chat, "shown", return_value=self.current),
                      mock.patch.object(web_chat, "load", side_effect=lambda cid: next(
                          (c for c in self.records if c["id"] == cid), None)),
                      mock.patch("supervisor.web._sent_during", return_value=[]),
                      mock.patch("supervisor.docs.listing", return_value=[{"path": "a b.md", "bytes": 10, "mtime": 1761400000.0,
                                                                         "sha": "ab"}]),
                      mock.patch("supervisor.docs.read", return_value=("Current replacement text", "sha")),
                      mock.patch("supervisor.doc_comments.threads", return_value=[]),
                      mock.patch("supervisor.doc_comments.open_counts", return_value={})]:
            patch.start()
            self.addCleanup(patch.stop)
        self.cookie = self.paired()

    def get(self, path):
        response = self.req(path, cookie=self.cookie)
        self.assertEqual(response.status, 200)
        return response.body.decode()

    def test_issue_8_quick_and_full_lists(self):
        """Reverting #8 hides history in tools, omits topics and makes the oldest five unreachable."""
        for path in ("/i/wren/chat", "/fragment/chat/wren/header"):
            body = self.get(path)
            self.assertRegex(body, r'data-key="earlier-chats"><summary data-focus="earlier"><svg[^>]*>.*?</svg> Earlier chats'
                                   r'</summary>')
            self.assertIn("latest 20 of 25", body)
            self.assertIn('href="/i/wren/chats"', body)
            self.assertEqual(len(re.findall(r'href="/i/wren/chat\?id=', body)), 20)
        body = self.get("/i/wren/chats")
        ids = re.findall(r'href="/i/wren/chat\?id=([^"]+)', body)
        self.assertEqual(ids, [c["id"] for c in self.records])
        self.assertIn("Topic 1 " + "x" * 72 + "… · 1 message(s)", body)
        self.assertNotIn("Hidden second line", body)
        self.records[1]["created"] = self.records[0]["created"]
        body = self.get("/i/wren/chats")
        self.assertIn("Topic 24", body)
        self.assertIn("Topic 25", body)

    def test_issue_8_empty_and_single_lists(self):
        """Reverting #8 loses the explicit empty state and count for short history lists."""
        self.records[:] = self.records[:1]
        self.assertIn("latest 1 of 1", self.get("/fragment/chat/wren/header"))
        self.records.clear()
        self.assertIn("latest 0 of 0", self.get("/fragment/chat/wren/header"))
        self.assertIn("No earlier chats yet.", self.get("/i/wren/chats"))

    def test_issue_8_archive_banner_and_bookmark(self):
        """Reverting #8 removes the archive date and direct return to the current chat."""
        path = "/i/wren/chat?id=" + self.records[-1]["id"]
        for _ in range(2):
            body = self.get(path)
            self.assertIn("Earlier chat, 2026-09-01 12:00", body)
            self.assertIn('href="/i/wren/chat">Back to the current chat</a>', body)
            self.assertNotIn('class="composer"', body)
        self.assertIn('class="composer"', self.get("/i/wren/chat"))

    def test_issue_32_archive_footer(self):
        """Reverting #32 invites a next message in an archive with no composer."""
        cid = self.records[0]["id"]
        self.records[0]["pending_edits"] = {"a b.md": {}}
        for path in (f"/i/wren/chat?id={cid}", f"/fragment/chat/wren?id={cid}"):
            body = self.get(path)
            self.assertIn("This conversation ended on 2026-09-26 13:00.", body)
            self.assertNotIn("next message", body)
        self.current["state"] = "ended"
        self.assertIn("Your next message starts a new conversation", self.get("/fragment/chat/wren"))

    def test_issue_15_archive_documents_and_cards(self):
        """Reverting #15 presents current files as historical and exposes mutation controls after polling."""
        cid = self.records[0]["id"]
        for path in (f"/i/wren/chat?id={cid}&doc=a%20b.md",
                     f"/fragment/docs/wren?id={cid}&doc=a%20b.md",
                     f"/fragment/docs/wren?id={cid}&list=1"):
            body = self.get(path)
            self.assertIn("Current files — not as they were in this chat", body)
            self.assertIn("Open in the current chat to edit or comment", body)
            for control in ("data-edit-doc", "data-comment-doc", "New document", 'data-post="/docs/', "data-threads"):
                self.assertNotIn(control, body)
            if "list=1" not in path:
                self.assertIn("Current replacement text", body)
                self.assertIn('href="/i/wren/chat?doc=a%20b.md"', body)
        body = self.get(f"/i/wren/chat?id={cid}&doc=a%20b.md")
        self.assertIn(f'data-poll="/fragment/docs/wren?doc=a%20b.md&amp;id={cid}"', body)
        card = re.search(r'<a class="doccard"[^>]+>', body).group()
        self.assertIn('href="/i/wren/chat?doc=a%20b.md"', card)
        self.assertNotIn("data-open-doc", card)
        current = self.get("/i/wren/chat?doc=a%20b.md")
        self.assertIn("data-edit-doc", current)
        self.assertIn("data-comment-doc", current)
