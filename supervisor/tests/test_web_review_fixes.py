"""The web UI and PWA findings W1–W18 of the system review (REVIEW-system-2026-10-24.md §4), server side: each test
names its finding and fails on the code that had it. The client halves are in the *_js tests beside this one."""
from __future__ import annotations

import json
import socket
import threading
import time
from http.server import ThreadingHTTPServer
from unittest import mock

from supervisor import docs, messages, web, web_chat, web_config, web_settings, web_views, webpush
from supervisor.tests.test_web import HOST, WebTest

APPLE = "https://web.push.apple.com/FAKE-endpoint-for-tests"
SAME = {"sec-fetch-site": "same-origin"}


class ChatRoutesTest(WebTest):
    """A paired browser asking for wisp's chat, with the chats and the message log in this test's own folder."""

    def setUp(self):
        super().setUp()
        for p in (mock.patch("supervisor.core.SUP_DIR", self.sup), mock.patch.object(web_chat, "SUP_DIR", self.sup),
                  mock.patch.object(messages, "SUP_DIR", self.sup), mock.patch.object(web, "_names", return_value=["wisp"]),
                  mock.patch.object(webpush, "log"), mock.patch.object(web_chat, "log")):
            p.start()
            self.addCleanup(p.stop)
        web_chat._watched.clear()
        self.addCleanup(web_chat._watched.clear)
        self.cookie = self.paired()

    def replying(self, activity: list[str]) -> dict:
        chat = {"id": "web-0123456789abcdef", "instance": "wisp", "login": "", "created": "2026-10-24T10:00:00",
                "state": "replying", "turns": [{"role": "user", "content": "hi", "shown": "hi", "ts": "2026-10-24T10:00:00",
                                                "mid": "m-1"}],
                "reply": {"started": "2026-10-24T10:00:01", "activity": activity, "answer": "", "interjections": 0}}
        web_chat.save(chat)
        return chat

    def get(self, path, cookie=None, headers=None):
        return self.req(path, cookie=cookie or self.cookie, headers=headers)


class LiveActivityTest(ChatRoutesTest):
    def test_w12_the_feed_stays_304_while_the_reply_works_and_its_activity_comes_on_its_own(self):
        """W12: the reply's activity was saved every second inside the feed, so its ETag changed on every 1.5 s poll —
        the whole conversation rendered and sent again each time (~8 MB for a 2-minute reply in a 50-turn chat)."""
        self.replying(["read notes/a.md"])
        first = self.get("/fragment/chat/wisp?id=web-0123456789abcdef")
        self.assertEqual(first.status, 200)
        self.assertNotIn(b"read notes/a.md", first.body)                         # not in the feed
        self.assertIn(b'data-poll="/fragment/chat/wisp/live?id=web-0123456789abcdef"', first.body)
        live = self.get("/fragment/chat/wisp/live?id=web-0123456789abcdef")
        self.assertIn(b"read notes/a.md", live.body)
        chat = web_chat.load("web-0123456789abcdef")
        chat["reply"]["activity"].append("wrote notes/docs/plan.md")
        web_chat.save(chat)
        again = self.get("/fragment/chat/wisp?id=web-0123456789abcdef", headers={"if-none-match": first.headers["ETag"]})
        self.assertEqual(again.status, 304)                                      # was 200 with the whole feed
        self.assertIn(b"wrote notes/docs/plan.md", self.get("/fragment/chat/wisp/live?id=web-0123456789abcdef").body)
        chat["state"], chat["reply"] = "idle", None                              # the reply ended
        web_chat.save(chat)
        self.assertEqual(self.get("/fragment/chat/wisp/live?id=web-0123456789abcdef").body, b"")

    def test_w12_an_earlier_chat_keeps_its_activity_inline(self):
        chat = self.replying(["x"])
        feed = str(web_views.chat_feed("wisp", chat, [], archived=True))
        self.assertNotIn("/live", feed)
        self.assertIn("<p>x</p>", feed)

    def test_w12_the_rendered_markdown_is_kept(self):
        web_views._rendered.cache_clear()
        with mock.patch("supervisor.web_md.render", wraps=__import__("supervisor.web_md").web_md.render) as render:
            for _ in range(3):
                web_views.markdown("**the same** answer")
        self.assertEqual(render.call_count, 1)

    def test_w12_a_document_is_hashed_again_only_when_it_changes(self):
        root = self.sup / "inst"
        (root / "notes/docs").mkdir(parents=True)
        (root / "notes/docs/a.md").write_text("one")
        with mock.patch.object(docs, "instance_root", return_value=root), \
                mock.patch.object(docs, "read_beneath", wraps=docs.read_beneath) as read:
            first = docs.listing("wisp")
            docs.listing("wisp")
            self.assertEqual(read.call_count, 1)                                 # was one read per listing
            time.sleep(0.01)
            (root / "notes/docs/a.md").write_text("two!")
            second = docs.listing("wisp")
        self.assertEqual(read.call_count, 2)
        self.assertNotEqual(first[0]["sha"], second[0]["sha"])
        self.assertEqual(second[0]["sha"], docs.sha(b"two!"))


class DigestCacheTest(ChatRoutesTest):
    def test_the_hash_cache_holds_only_the_documents_there_now(self):
        """Grok review of 8735c62 (S3): the documents' hash cache grew for ever and kept deleted paths. It holds only
        what each listing found."""
        root = self.sup / "inst"
        (root / "notes/docs/sub").mkdir(parents=True)
        for name in ("a.md", "b.md", "sub/c.md"):
            (root / "notes/docs" / name).write_text(name)
        with mock.patch.object(docs, "instance_root", return_value=root):
            docs.listing("wisp")
            mine = lambda: sorted(k for k in docs._digests if k.startswith(str(root)))
            self.assertEqual(len(mine()), 3)
            (root / "notes/docs/b.md").unlink()
            (root / "notes/docs/sub/c.md").unlink()
            docs.listing("wisp")
            self.assertEqual(mine(), [str(root / "notes/docs/a.md")])


class SendingCopyTest(ChatRoutesTest):
    def test_w7_turns_carry_the_message_id_the_sending_copy_waits_for(self):
        """W7: the "sending…" copy goes when the feed brings its turn; with no id to match, one whose turn came in a
        refresh before the send returned stayed, duplicated, for good. Every turn of the operator's carries it."""
        chat = self.replying([])
        chat["queued"] = [{"role": "user", "content": "later", "shown": "later", "ts": "2026-10-24T10:00:02",
                           "mid": "q-1", "attachments": []}]
        later = {"ts": "2026-10-24T10:00:03", "file": "f.md", "from": "operator", "to": "wisp", "text": "cycle",
                 "shown": "cycle", "mid": "b-1", "read": False}
        feed = str(web_views.chat_feed("wisp", chat, [later]))
        for mid in ("m-1", "q-1", "b-1"):
            self.assertIn(f'data-mid="{mid}"', feed)


class QueuedCancelTest(ChatRoutesTest):
    def test_w17_cancel_is_a_standard_action_form(self):
        """W17: the queued message's Cancel lacked class="act" and the data-check every other action names, so a
        lost answer read as a chat send ("sending it again is safe")."""
        chat = self.replying([])
        chat["queued"] = [{"role": "user", "content": "x", "shown": "x", "ts": "2026-10-24T10:00:02", "mid": "q-1",
                           "attachments": []}]
        feed = str(web_views.chat_feed("wisp", chat, []))
        self.assertRegex(feed, r'<form class="act" data-post="/chat/wisp/unqueue" data-check="[^"]+">')
        self.assertIn('<button type="submit" class="act small">Cancel</button>', feed)


class RailTest(ChatRoutesTest):
    def test_w8_a_message_it_sent_lights_the_rail(self):
        """W8: summary()'s `last` was the last reply only, so a send_message card in the chat never marked it unread."""
        chat = self.replying([])
        chat.update(state="idle", reply=None, kind="from")
        chat["turns"].append({"role": "assistant", "content": "done", "ts": "2026-10-24T10:01:00"})
        web_chat.save(chat)
        sent = {"ts": "2026-10-24T11:00:00", "from": "wisp", "to": "operator", "file": "m.md", "text": "A question?\nmore",
                "read": False}
        older = {**sent, "ts": "2026-10-24T09:00:00", "file": "o.md", "text": "before the chat"}
        history = mock.patch.object(messages, "history", return_value=[dict(older, talk=None), dict(sent, talk=None)])
        reader = history.start()
        self.addCleanup(history.stop)
        s = web_chat.summary("wisp", [older, sent])
        self.assertEqual((s["last"], s["line"]), ("2026-10-24T11:00:00", "A question?"))
        reader.return_value = [dict(older, talk=None)]
        self.assertEqual(web_chat.summary("wisp", [older])["last"], "2026-10-24T10:01:00")   # the newer Main reply wins
        reader.return_value = [dict(older, talk=None), dict(sent, talk=None)]
        with self.assertRaisesRegex(web_chat.Refused, "From is not cleared"):
            web_chat.clear("wisp", "from")
        self.assertEqual(web_chat.summary("wisp", [older, sent]),
                         {"line": "A question?", "last": "2026-10-24T11:00:00", "last_count": 1})
        rail = self.get("/fragment/rail?c=i:wisp")
        self.assertEqual(rail.status, 200)


class WatchingTest(ChatRoutesTest):
    def test_m10_covered_feed_does_not_watch_but_still_renders(self):
        """M10: a feed poll behind a covering sheet renewed push suppression."""
        self.replying([])
        self.get("/fragment/chat/wisp?id=web-0123456789abcdef", headers={"x-shaft-at": "1000"})
        self.assertTrue(web_chat.watchers("wisp", cid="web-0123456789abcdef"))
        covered = self.get("/fragment/chat/wisp?id=web-0123456789abcdef", headers={"x-shaft-at": "2000", "x-shaft-watching": "0"})
        self.assertEqual(covered.status, 200)
        self.assertIn(b"hi", covered.body)
        self.assertEqual(web_chat.watchers("wisp", cid="web-0123456789abcdef"), set())
        self.get("/fragment/chat/wisp?id=web-0123456789abcdef", headers={"x-shaft-at": "1500"})  # an older poll arriving late
        self.assertEqual(web_chat.watchers("wisp", cid="web-0123456789abcdef"), set())
        self.get("/fragment/chat/wisp?id=web-0123456789abcdef", headers={"x-shaft-at": "2500"})  # sheet closed
        self.assertTrue(web_chat.watchers("wisp", cid="web-0123456789abcdef"))

    def test_w9_each_device_watches_for_itself_and_says_when_it_goes(self):
        """W9: one desktop tab on wisp's chat silenced every push to the phone; a phone that locked stayed watching
        for 6 s more. The feed's poll marks only the device asking, and ?away=1 ends it."""
        self.replying([])
        laptop, phone = self.cookie, self.paired()
        self.assertEqual(self.get("/fragment/chat/wisp?id=web-0123456789abcdef", cookie=laptop).status, 200)
        self.assertEqual(web_chat.watchers("wisp", cid="web-0123456789abcdef"), {web.device_from_cookie(laptop)[0]})
        self.get("/fragment/chat/wisp?id=web-0123456789abcdef", cookie=phone)
        self.assertEqual(len(web_chat.watchers("wisp", cid="web-0123456789abcdef")), 2)
        away = self.get("/fragment/chat/wisp?id=web-0123456789abcdef&away=1", cookie=phone)
        self.assertEqual((away.status, away.body), (204, b""))
        self.assertEqual(web_chat.watchers("wisp", cid="web-0123456789abcdef"), {web.device_from_cookie(laptop)[0]})
        self.get("/fragment/chat/wisp?id=web-0123456789abcdef", cookie=phone)    # the selected open thread
        self.assertEqual(len(web_chat.watchers("wisp", cid="web-0123456789abcdef")), 2)


class AwayOrderTest(ChatRoutesTest):
    def test_a_poll_sent_before_the_page_went_away_does_not_mark_it_watching_again(self):
        """Grok review of 8735c62 (S2): a feed poll already on its way when the page went hidden reached the server
        after ?away=1 and marked the device watching again for 6 s: a reply ending then was not pushed. Each poll and
        the away say when the page sent them (X-Shaft-At, &at=); a poll sent before the latest away is not watching."""
        self.replying([])
        dev = web.device_from_cookie(self.cookie)[0]
        self.assertEqual(self.get("/fragment/chat/wisp?id=web-0123456789abcdef", headers={"x-shaft-at": "1000"}).status, 200)
        self.assertEqual(web_chat.watchers("wisp", cid="web-0123456789abcdef"), {dev})
        self.assertEqual(self.get("/fragment/chat/wisp?id=web-0123456789abcdef&away=1&at=2000").status, 204)
        self.assertEqual(self.get("/fragment/chat/wisp?id=web-0123456789abcdef", headers={"x-shaft-at": "1500"}).status, 200)   # late
        self.assertEqual(web_chat.watchers("wisp", cid="web-0123456789abcdef"), set())
        self.get("/fragment/chat/wisp?id=web-0123456789abcdef", headers={"x-shaft-at": "2500"})                               # back
        self.assertEqual(web_chat.watchers("wisp", cid="web-0123456789abcdef"), {dev})


class PushRoutesTest(ChatRoutesTest):
    def post(self, path, body, cookie=None):
        cookie = cookie or self.cookie
        return self.req(path, "POST", cookie=cookie, body=json.dumps(body).encode(),
                        headers={**SAME, "content-type": "application/json", "x-csrf-token": self.csrf(cookie)})

    def test_w1_unpairing_drops_the_devices_push_subscription(self):
        """W1: unpair() deleted the device but not its subscription: a revoked phone kept a lock-screen push and a
        badge for every reply."""
        other = self.paired()
        for cookie in (self.cookie, other):
            self.assertEqual(self.post("/push/subscribe", {"endpoint": APPLE}, cookie).status, 200)
        self.assertEqual(len(webpush.subscriptions()), 2)
        r = self.req("/unpair", "POST", cookie=other, headers={**SAME, "x-csrf-token": self.csrf(other)})
        self.assertEqual(r.status, 200)
        self.assertEqual(set(webpush.subscriptions()), {web.device_from_cookie(self.cookie)[0]})
        self.assertEqual(web.unpair(None), 1)                                    # the CLI's --all: every one
        self.assertEqual(webpush.subscriptions(), {})

    def test_w5_status_names_the_endpoint_and_key_and_the_worker_can_renew(self):
        """W5: /push/status said only subscribed or not, so the page could not tell a rotated endpoint or a changed key;
        the service worker had no pushsubscriptionchange, and no token to post a renewed subscription with."""
        self.assertEqual(self.post("/push/subscribe", {"endpoint": APPLE}).status, 200)
        state = json.loads(self.get("/push/status").body)
        self.assertEqual((state["subscribed"], state["endpoint"], state["stale"]), (True, APPLE, False))
        self.assertEqual(state["key"], webpush.public_key())
        self.assertEqual(self.get("/push/token").status, 403)                   # never to a link from elsewhere
        token = json.loads(self.get("/push/token", headers=SAME).body)["csrf"]
        self.assertEqual(token, self.csrf(self.cookie))
        sw = self.req("/sw.js").body.decode()
        self.assertIn('self.addEventListener("pushsubscriptionchange"', sw)
        self.assertIn('fetch("/push/token"', sw)


class TooLargeTest(WebTest):
    def test_w15_a_body_over_the_limit_is_refused_in_plain_text(self):
        """W15: send_error(413) wrote its own HTML page, and the page put that whole page into the document view."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with socket.create_connection(server.server_address, timeout=5) as s:
            s.sendall(f"POST /docs/wisp/save HTTP/1.1\r\nHost: {HOST}\r\nContent-Length: 5000000\r\n\r\n".encode())
            raw = b""
            while chunk := s.recv(65536):
                raw += chunk
        head, _, body = raw.partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 413") or head.startswith(b"HTTP/1.1 413"), head)
        self.assertIn(b"Content-Type: text/plain; charset=utf-8", head)
        self.assertIn(b"Content-Security-Policy:", head)
        self.assertNotIn(b"<", body)
        self.assertIn(b"Too large: at most 1 MB", body)


class LeaveWarningTest(WebTest):
    def test_w14_drive_config_and_settings_edits_ask_before_leaving(self):
        """W14: these forms had no draft and no leave warning: an iOS back-swipe lost a long drive.md edit."""
        drive = str(web_config.drive_page("wisp", "be useful", "sha1", web_views.markdown("be useful")))
        self.assertIn('data-guard data-keep="drive.wisp"', drive)
        self.assertIn("data-keep-note", drive)
        field = {"key": "LLM_MODEL", "value": "m", "set": True, "default": None, "help": "", "secret": False,
                 "choices": None, "numeric": False}
        self.assertIn('data-post="/action/config-set" data-guard', str(web_config.config_row("wisp", field)))
        self.assertIn('data-reset="1" data-guard', str(web_config.config_page("wisp", [field], [])))
        setting = {"name": "SHAFT_X", "web": True, "in_file": None, "services": [], "value": "1", "default": "1",
                   "source": "default", "not_live": [], "kind": "text", "help": "", "choices": None}
        with mock.patch.object(web_settings, "_control", return_value=web_views.Html('<input name="value">')):
            self.assertIn('data-post="/action/settings-set" data-guard', str(web_settings._change(setting)))
