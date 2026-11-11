"""Topic threads in the web UI (M3): the rail lists the viewed instance's open threads (title, a dot for idle /
replying / waiting, the unread mark the browser sets from data-last) and From <instance>, + starts a thread; the thread
header names it, renames it and opens a new one without ending it; /i/<n>/from is the From feed with a composer that
sends only for the next cycle; a waiting reply says which thread holds the instance."""
from __future__ import annotations

import json
import re
from unittest import mock

from supervisor import messages, web, web_chat, web_pages, web_views
from supervisor.tests.test_web_chat import ME, ChatRoutesCase


class ThreadsWebTest(ChatRoutesCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(messages, "SUP_DIR", self.sup), mock.patch.object(messages, "INSTANCES", self.inst),
                  mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
                  mock.patch.dict(web_chat._watched, clear=True), mock.patch.dict(web_chat._away, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        messages._cache["key"] = None
        self.headers = {"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie)}

    def thread(self, title: str, turns: list[dict], state: str = "idle", **extra) -> dict:
        chat = web_chat.new_thread("wisp", ME, title=title)
        chat.update(turns=turns, state=state, **extra)
        web_chat.save(chat)
        return chat

    def log(self, **row) -> None:
        with (self.sup / messages.LOG_NAME).open("a") as f:
            f.write(json.dumps({"kind": "note", "text": "x", "talk": None, **row}) + "\n")

    def get(self, path: str, **kw) -> str:
        r = self.req(path, cookie=self.cookie, **kw)
        self.assertEqual(r.status, 200, (path, r.body[:300]))
        return r.body.decode()

    def post(self, path: str, body: str):
        return self.req(path, "POST", cookie=self.cookie, headers={**self.headers,
                        "content-type": "application/x-www-form-urlencoded"}, body=body.encode())

    def rows(self, rail: str) -> list[str]:
        return re.findall(r'<a href="/i/wisp/(?:chat\?id=[^"]+|from)" class="thread[^"]*"[^>]*>.*?</a>', rail)

    # -- the rail ---------------------------------------------------------------------------------------------------
    def test_rail_lists_the_viewed_instances_threads_with_title_state_and_last(self):
        a = self.thread("Landlord <mail>", [{"role": "user", "content": "q", "ts": "2026-11-10T10:00:00"},
                                            {"role": "assistant", "content": "a", "ts": "2026-11-10T10:00:05"}])
        b = self.thread("Calendar", [{"role": "user", "content": "q", "ts": "2026-11-10T11:00:00"}], "replying",
                        reply={"started": "2026-11-10T11:00:01", "activity": [], "answer": "",
                               "waiting": "waiting: answering in «Landlord <mail>»"})
        c = self.thread("Taxes", [{"role": "user", "content": "q", "ts": "2026-11-10T12:00:00"}], "replying",
                        reply={"started": "2026-11-10T12:00:01", "activity": [], "answer": ""})
        rail = self.get(f"/fragment/rail?c=i:wisp&t={a['id']}")
        rows = self.rows(rail)
        self.assertEqual(len(rows), 4, rail)                          # three threads, newest first, then From
        self.assertIn(f'chat?id={c["id"]}', rows[0])
        self.assertIn('class="tdot t-replying" title="replying"', rows[0])
        self.assertIn(f'chat?id={b["id"]}', rows[1])
        self.assertIn('class="tdot t-waiting" title="waiting: answering in «Landlord &lt;mail&gt;»"', rows[1])
        self.assertIn(f'chat?id={a["id"]}', rows[2])
        self.assertIn('class="thread on"', rows[2])                     # the thread on screen
        self.assertIn('aria-current="page"', rows[2])
        self.assertIn('<span class="ttl">Landlord &lt;mail&gt;</span>', rows[2])   # escaped
        self.assertIn('data-last="2026-11-10T10:00:05"', rows[2])     # when it last answered there
        self.assertIn(f'data-seen-key="{a["id"]}"', rows[2])
        self.assertIn('<span class="ttl">From wisp</span>', rows[3])
        self.assertIn('data-seen-key="from"', rows[3])
        self.assertIn('data-post="/chat/wisp/new" data-after="open-thread"', rail)
        self.assertIn('aria-label="New thread with wisp"', rail)
        lasts = json.loads(re.search(r'data-lasts="([^"]+)"', rail)[1].replace("&quot;", '"'))
        self.assertEqual(lasts, {a["id"]: "2026-11-10T10:00:05", b["id"]: "", c["id"]: "", "from": ""})

    def test_rail_threads_only_under_the_viewed_instance_but_its_marks_everywhere(self):
        self.thread("One", [{"role": "assistant", "content": "a", "ts": "2026-11-10T10:00:05"}])
        rail = self.get("/fragment/rail?c=/overview")
        self.assertEqual(self.rows(rail), [])
        self.assertIn("data-lasts=", rail)                             # the instance is still marked unread from them
        self.assertNotIn("railthreads", rail)

    def test_rail_on_the_instance_page_lights_main_not_the_newest_thread(self):
        """2026-11-11: the instance's own page is Main. An older page polling the rail with t=current lit the most
        recently active thread, a row that moved with every reply; it lights Main."""
        old = self.thread("Old", [{"role": "user", "content": "q", "ts": "2026-11-10T09:00:00"}])
        new = self.thread("New", [{"role": "user", "content": "q", "ts": "2026-11-10T10:00:00"}])
        rail = self.get("/fragment/rail?c=i:wisp&t=current")
        on = [r for r in self.rows(rail) if 'class="thread from on"' in r or 'class="thread on"' in r]
        self.assertEqual(len(on), 1)
        self.assertIn('data-seen-key="from"', on[0])
        self.assertNotIn(new["id"], on[0])
        self.assertNotIn(old["id"], on[0])
        config = self.get("/fragment/rail?c=i:wisp")                 # its Config page: listed, none lit
        self.assertEqual(len(self.rows(config)), 3)
        self.assertNotIn('class="thread on"', config)

    def test_rail_last_per_thread_is_the_feeds_last_and_from_has_its_own(self):
        """A message the instance sent in thread A's talk counts for A only; one from a cycle (talk null) for From."""
        a = self.thread("A", [{"role": "assistant", "content": "a", "ts": "2026-11-10T10:00:05"}], talks=["talk-a"])
        b = self.thread("B", [{"role": "assistant", "content": "b", "ts": "2026-11-10T10:00:06"}])
        self.log(ts="2026-11-10T10:30:00", **{"from": "wisp"}, to="operator", file="m1.md", talk="talk-a")
        self.log(ts="2026-11-10T11:00:00", **{"from": "wisp"}, to="operator", file="m2.md", talk=None)
        lasts = web._rail_chats(["wisp"])["wisp"]
        by_id = {t["id"]: t["last"] for t in lasts["threads"]}
        self.assertEqual(by_id, {a["id"]: "2026-11-10T10:30:00", b["id"]: "2026-11-10T10:00:06"})
        self.assertEqual(lasts["from_last"], "2026-11-10T11:00:00")
        for cid, last in by_id.items():                                # what the feed's items carry: the same times
            self.assertEqual(web_chat.feed("wisp", cid)["last"], last)
        self.assertEqual(web_chat.from_feed("wisp")["last"], lasts["from_last"])

    def test_a_threads_page_tells_the_rail_which_thread_it_is(self):
        a = self.thread("A", [])
        page = self.get(f"/i/wisp/chat?id={a['id']}")
        self.assertIn(f'data-poll="/fragment/rail?c=i:wisp&amp;t={a["id"]}"', page)
        self.assertIn(f'data-draft="wisp/{a["id"]}"', page)            # its own draft: not another thread's
        plain = self.get("/i/wisp/chat")                               # the instance's own page: Main
        self.assertIn('data-poll="/fragment/rail?c=i:wisp&amp;t=from"', plain)
        self.assertIn('data-draft="wisp/from"', plain)

    # -- the thread header -------------------------------------------------------------------------------------------
    def test_header_names_the_thread_renames_it_and_new_thread_ends_nothing(self):
        a = self.thread("Landlord mail", [{"role": "user", "content": "q", "ts": "2026-11-10T10:00:00"}])
        header = self.get(f"/fragment/chat/wisp/header?id={a['id']}")
        self.assertIn('<span class="ttl">Landlord mail</span>', header)
        rename = re.search(r'<form class="act renameform" data-post="/chat/wisp/rename".*?</form>', header)[0]
        self.assertIn(f'name="cid" value="{a["id"]}"', rename)
        self.assertIn('<label>Thread title<input name="title" value="Landlord mail"', rename)
        tools = header.split('<div class="headtools">', 1)[1].split('<div class="menubody">', 1)[0]
        self.assertIn('data-post="/chat/wisp/new" data-after="open-thread"', tools)
        self.assertNotIn("/chat/wisp/clear", tools)                    # Clear ends the thread: in ⋯, not one tap away
        self.assertIn('data-post="/chat/wisp/clear"', header)
        r = self.post("/chat/wisp/rename", f"cid={a['id']}&title=Rent")
        self.assertEqual(r.status, 200, r.body)
        self.assertIn('<span class="ttl">Rent</span>', self.get(f"/fragment/chat/wisp/header?id={a['id']}"))
        r = self.post("/chat/wisp/new", "")
        self.assertEqual(r.status, 200, r.body)
        new = re.search(r'data-cid="(web-[0-9a-f]+)"><a href="/i/wisp/chat\?id=\1"', r.body.decode())[1]
        self.assertEqual({c["id"] for c in web_chat.threads("wisp")}, {a["id"], new})   # A still open

    def test_an_ended_threads_header_shows_its_title_without_rename(self):
        a = self.thread("Done topic", [{"role": "user", "content": "q", "ts": "2026-11-10T10:00:00"}], "ended")
        header = self.get(f"/fragment/chat/wisp/header?id={a['id']}")
        self.assertIn('<span class="threadtitle">Done topic</span>', header)
        self.assertNotIn("/chat/wisp/rename", header)
        self.assertNotIn("/chat/wisp/clear", header)
        self.assertIn('data-post="/chat/wisp/new"', header)              # a new thread is always one tap away

    # -- From <instance> ---------------------------------------------------------------------------------------------
    def test_from_page_shows_the_feed_and_sends_now_or_for_the_next_cycle(self):
        """From's composer is a thread's (2026-11-11): Send is answered now in the From conversation, Next cycle
        stays behind the arrow; both name From (cid=from). It used to force every message to the next cycle."""
        self.thread("A", [], talks=["talk-a"])
        self.log(ts="2026-11-10T10:00:00", **{"from": "wisp"}, to="operator", file="m1.md", text="From a <cycle>")
        self.log(ts="2026-11-10T10:01:00", **{"from": "wisp"}, to="operator", file="m2.md", text="In A", talk="talk-a")
        self.log(ts="2026-11-10T10:02:00", **{"from": "operator"}, to="wisp", file="m3.md", text="for later",
                 via=messages.BACKGROUND)
        self.assertEqual(self.req("/i/wisp/from", cookie=self.cookie).status, 302)   # From is Main now
        page = self.get("/i/wisp/chat")
        self.assertIn('data-poll="/fragment/chat/wisp"', page)               # no id: Main, as a thread's names its id
        self.assertIn('data-poll="/fragment/chat/wisp/header"', page)
        self.assertIn('data-poll="/fragment/rail?c=i:wisp&amp;t=from"', page)
        self.assertIn("From a &lt;cycle&gt;", page)
        self.assertIn("for later", page)
        self.assertNotIn("In A", page)                                   # sent in thread A: shown there only
        self.assertIn('data-seen-key="from" data-sent-at="2026-11-10T10:00:00"', page)
        composer = re.search(r'<form class="composer".*?</form>', page, re.S)[0]
        self.assertIn('data-post="/chat/wisp/send"', composer)
        self.assertIn('<input type="hidden" name="background" value="">', composer)
        self.assertNotIn('data-always="1"', composer)
        self.assertIn('data-draft="wisp/from"', composer)
        self.assertIn('<input type="hidden" name="cid" value="from">', composer)
        self.assertIn('name="queue"', composer)
        self.assertIn('<span class="threadtitle">From wisp</span>', page)
        self.assertNotIn("data-show-docs", page)                         # no documents panel on From
        self.assertNotIn("/chat/wisp/clear", page)
        with mock.patch.object(web_chat, "background", return_value=("", "kept for its next cycle")) as bg, \
                mock.patch.object(web_chat, "send", return_value=({}, "sent")) as send:
            for background in ("1", ""):
                body = (b'--B\r\nContent-Disposition: form-data; name="text"\r\n\r\nnote\r\n'
                        b'--B\r\nContent-Disposition: form-data; name="cid"\r\n\r\nfrom\r\n'
                        b'--B\r\nContent-Disposition: form-data; name="background"\r\n\r\n'
                        + background.encode() + b'\r\n--B--\r\n')
                r = self.req("/chat/wisp/send", "POST", cookie=self.cookie, body=body,
                             headers={**self.headers, "content-type": "multipart/form-data; boundary=B"})
                self.assertEqual(r.status, 200, r.body)
        bg.assert_called_once_with("wisp", ME, "note", [], mid="", cid="from")
        send.assert_called_once_with("wisp", ME, "note", [], mid="", retry_of="", voice=False, cid="from")

    def test_from_fragment_refreshes_and_watches_from(self):
        self.log(ts="2026-11-10T10:00:00", **{"from": "wisp"}, to="operator", file="m1.md", text="cycle news")
        feed = self.get("/fragment/from/wisp", headers={"x-shaft-at": "1000"})
        self.assertIn("cycle news", feed)
        self.assertNotIn("<header", feed)
        self.assertTrue(web_chat.watchers("wisp", "from"))
        away = self.req("/fragment/from/wisp?away=1&at=2000", cookie=self.cookie)
        self.assertEqual(away.status, 204)
        self.assertEqual(web_chat.watchers("wisp", "from"), set())
        self.get("/fragment/from/wisp", headers={"x-shaft-at": "3000", "x-shaft-watching": "0"})   # covered: not watching
        self.assertEqual(web_chat.watchers("wisp", "from"), set())
        header = self.get("/fragment/from/wisp/header")
        self.assertIn("From wisp", header)
        self.assertEqual(self.req("/fragment/from/nobody", cookie=self.cookie).status, 404)
        self.assertEqual(self.req("/i/nobody/from", cookie=self.cookie).status, 404)

    def test_an_empty_from_says_what_it_is_for(self):
        feed = self.get("/fragment/from/wisp")
        self.assertIn("From wisp", feed)
        self.assertNotIn("Write below to start one", feed)              # not the chat's empty text

    # -- the feed's items know their thread --------------------------------------------------------------------------
    def test_items_in_a_thread_are_seen_under_its_id(self):
        chat = {"id": "web-a", "state": "idle", "turns": [{"role": "assistant", "content": "x", "ts": "2026-11-10T10:00:00"}]}
        sent = [{"ts": "2026-11-10T10:01:00", "from": "wisp", "to": "operator", "file": "m.md", "text": "y",
                 "kind": "note", "read": False}]
        feed = str(web_views.chat_feed("wisp", chat, sent))
        self.assertIn('data-seen-inst="wisp" data-seen-key="web-a" data-seen-reply="2026-11-10T10:00:00"', feed)
        self.assertIn('data-seen-inst="wisp" data-seen-key="web-a" data-sent-at="2026-11-10T10:01:00"', feed)

    # -- waiting -----------------------------------------------------------------------------------------------------
    def test_a_waiting_reply_says_which_thread_holds_the_instance(self):
        b = self.thread("B", [{"role": "user", "content": "q", "ts": "2026-11-10T11:00:00"}], "replying",
                        reply={"started": "2026-11-10T11:00:01", "activity": ["waiting: answering in «A»"],
                               "answer": "", "waiting": "waiting: answering in «A»"})
        live = self.get(f"/fragment/chat/wisp/live?id={b['id']}")
        self.assertIn('<p class="waitline" data-k="waiting" role="status">waiting: answering in «A»</p>', live)
        self.assertEqual(live.count("waiting: answering in «A»"), 1)  # not again as the activity's step
        b["reply"]["waiting"] = None                                     # started: the line goes
        web_chat.save(b)
        self.assertNotIn("waitline", self.get(f"/fragment/chat/wisp/live?id={b['id']}"))


class RailMarkupTest(ChatRoutesCase):
    def test_first_render_without_threads_keeps_the_old_shape(self):
        """A page's first render (no snapshot) lists the names only; web.js fills the threads at once."""
        rail = str(web_pages.rail(["wisp"], "i:wisp"))
        self.assertNotIn("data-lasts", rail)
        self.assertNotIn("railthreads", rail)
