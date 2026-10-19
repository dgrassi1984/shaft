"""Chat in the browser (PLAN-webui.md M4): every message is a conversation of SHAFT's own API, named by the chat's id;
the reply is relayed and saved by the web server, so it keeps going when the browser drops (Daniele, 2026-10-19);
interjections, Stop and End go to the API; files go in as Telegram's do."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import messages, web, web_chat
from supervisor.tests.test_web import WebTest, snap

KEY, ME = "sk-shaft-test", "daniele@example.com"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def chunk(delta: dict, finish=None, **extra) -> bytes:
    return b"data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}], **extra}).encode() + b"\n\n"


class FakeApi:
    """SHAFT's API as the web server sees it: a stream of reasoning, then the answer, then [DONE]."""

    def __init__(self):
        self.requests, self.script, self.hold, self.gone = [], None, threading.Event(), threading.Event()
        self.heard, self.interject_status, self.end_status, self.end_type = 0, 202, 202, "not_found"
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, dict(self.headers), body))
                if self.path == "/v1/chat/interject":
                    return self._json(fake.interject_status, {"interjected": True})
                if self.path == "/v1/chat/end":
                    if fake.end_status == 404:
                        return self._json(404, {"error": {"message": "no route", "type": fake.end_type}})
                    return self._json(fake.end_status, {"ended": "api-1"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(chunk({"reasoning_content": "🔧 read_file notes.md\n"}))
                    self.wfile.flush()
                    if fake.hold.is_set():                       # a long reply, until the client leaves
                        for _ in range(100):
                            time.sleep(0.05)
                            self.wfile.write(b": still working\n\n")
                            self.wfile.flush()
                    self.wfile.write(chunk({"content": f"Answer {len(fake.requests)}."}))
                    self.wfile.write(chunk({}, "stop", **({} if fake.heard is None else
                                                           {"interjections_heard": fake.heard})))
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    fake.gone.set()

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def chats(self):
        return [r for r in self.requests if r[0] == "/v1/chat/completions"]


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shaft-chat-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "instances" / "wren").mkdir(parents=True)
        self.api = FakeApi()
        self.addCleanup(self.api.server.shutdown)
        import http.client
        port = self.api.server.server_address[1]
        for p in (mock.patch.object(web_chat, "SUP_DIR", self.dir / "sup"),
                  mock.patch.object(web_chat, "_api_conn", lambda timeout: http.client.HTTPConnection("127.0.0.1", port,
                                                                                                     timeout=timeout)),
                  mock.patch("supervisor.core.secrets_env", return_value={"SHAFT_API_KEY": KEY}),
                  mock.patch.object(messages, "INSTANCES", self.dir / "instances"),
                  mock.patch("supervisor.docs.instance_root", lambda n: self.dir / "instances" / n),   # never the real
                  mock.patch.object(web_chat, "log"), mock.patch.object(messages, "log")):
            p.start()
            self.addCleanup(p.stop)

    def wait_idle(self, cid: str, timeout: float = 10) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            c = web_chat.load(cid)
            if c["state"] != "replying" and not web_chat.replying(cid):
                return c
            time.sleep(0.05)
        self.fail(f"still replying: {web_chat.load(cid)}")


class RelayTest(ChatTest):
    def test_a_reply_is_relayed_and_saved_with_no_browser_watching(self):
        chat, said = web_chat.send("wren", ME, "hello <b>", [])
        self.assertEqual(said, "sent to wren")
        c = self.wait_idle(chat["id"])                          # nobody polls: the server keeps it going
        self.assertEqual([t["role"] for t in c["turns"]], ["user", "assistant"])
        self.assertEqual(c["turns"][1]["content"], "Answer 1.")
        self.assertEqual(c["turns"][1]["activity"], ["🔧 read_file notes.md"])
        path, headers, body = self.api.chats()[0]
        self.assertEqual(headers["X-Shaft-Conversation"], chat["id"])
        self.assertEqual((headers["Authorization"], headers["Tailscale-User-Login"]), (f"Bearer {KEY}", ME))
        self.assertEqual(body, {"model": "wren", "messages": [
            {"role": "user", "content": "hello <b>\n\n" + web_chat.DOCS_NOTE}], "stream": True})
        self.assertNotIn(KEY, (self.dir / "sup" / "web" / "chats" / f"{chat['id']}.json").read_text())

    def test_the_next_message_carries_the_conversation_so_far(self):
        chat, _ = web_chat.send("wren", ME, "one", [])
        self.wait_idle(chat["id"])
        web_chat.send("wren", ME, "two", [])
        self.wait_idle(chat["id"])
        self.assertEqual(self.api.chats()[1][2]["messages"], [{"role": "user", "content": "one\n\n" + web_chat.DOCS_NOTE},
                                                             {"role": "assistant", "content": "Answer 1."},
                                                             {"role": "user", "content": "two"}])

    def test_two_chats_that_open_alike_are_two_conversations(self):
        a, _ = web_chat.send("wren", ME, "hi", [])
        self.wait_idle(a["id"])
        web_chat.end(a["id"])
        b, _ = web_chat.send("wren", ME, "hi", [])
        self.wait_idle(b["id"])
        self.assertNotEqual(self.api.chats()[0][1]["X-Shaft-Conversation"], self.api.chats()[1][1]["X-Shaft-Conversation"])

    def test_a_message_during_the_reply_is_an_interjection_and_one_heard_too_late_gets_its_own_reply(self):
        self.api.hold.set()
        chat, _ = web_chat.send("wren", ME, "start", [])
        time.sleep(0.3)
        _, said = web_chat.send("wren", ME, "and also this", [])
        self.assertEqual(said, "wren reads it at its next step")
        path, headers, body = [r for r in self.api.requests if r[0] == "/v1/chat/interject"][0]
        self.assertEqual(headers["X-Shaft-Conversation"], chat["id"])
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "and also this"})
        self.api.hold.clear()
        c = self.wait_idle(chat["id"], 15)                       # heard 0 of 1: answered by a reply of its own
        self.assertEqual(len(self.api.chats()), 2)
        self.assertEqual([t["role"] for t in c["turns"]], ["user", "user", "assistant", "assistant"])
        self.assertTrue(c["turns"][1]["interjected"])
        self.assertIn({"role": "user", "content": "and also this"}, self.api.chats()[1][2]["messages"])

    def test_an_interjection_heard_in_time_needs_no_second_reply(self):
        self.api.hold.set()
        self.api.heard = 1
        chat, _ = web_chat.send("wren", ME, "start", [])
        time.sleep(0.3)
        web_chat.send("wren", ME, "also", [])
        self.api.hold.clear()
        self.wait_idle(chat["id"], 15)
        self.assertEqual(len(self.api.chats()), 1)

    def test_an_interjection_too_late_for_the_api_is_a_new_message(self):
        self.api.hold.set()
        self.api.interject_status = 409
        chat, _ = web_chat.send("wren", ME, "start", [])
        time.sleep(0.3)
        self.api.hold.clear()
        web_chat.send("wren", ME, "late", [])
        c = self.wait_idle(chat["id"], 15)
        self.assertEqual(c["turns"][-1]["role"], "assistant")
        self.assertIn({"role": "user", "content": "late"}, self.api.chats()[-1][2]["messages"])

    def test_stop_ends_the_reply_and_the_api_sees_its_client_go(self):
        self.api.hold.set()
        chat, _ = web_chat.send("wren", ME, "long one", [])
        time.sleep(0.3)
        self.assertEqual(web_chat.stop(chat["id"]), "stopping the reply")
        c = self.wait_idle(chat["id"])
        self.assertEqual(c["turns"][-1]["note"], "stopped by you")
        self.assertTrue(self.api.gone.wait(5))
        with self.assertRaisesRegex(web_chat.Refused, "no reply is running"):
            web_chat.stop(chat["id"])

    def test_end_writes_it_up_through_the_api_and_the_next_message_starts_a_new_chat(self):
        chat, _ = web_chat.send("wren", ME, "hi", [])
        self.wait_idle(chat["id"])
        self.assertIn("writes the conversation up", web_chat.end(chat["id"]))
        path, headers, body = [r for r in self.api.requests if r[0] == "/v1/chat/end"][0]
        self.assertEqual((headers["X-Shaft-Conversation"], body), (chat["id"], {"model": "wren"}))
        self.assertEqual(web_chat.load(chat["id"])["state"], "ended")
        nxt, _ = web_chat.send("wren", ME, "again", [])
        self.assertNotEqual(nxt["id"], chat["id"])
        self.wait_idle(nxt["id"])

    def test_end_refused_while_replying_and_already_written_up_is_ended(self):
        self.api.hold.set()
        chat, _ = web_chat.send("wren", ME, "x", [])
        time.sleep(0.2)
        with self.assertRaisesRegex(web_chat.Refused, "still replying"):
            web_chat.end(chat["id"])
        self.api.hold.clear()
        self.wait_idle(chat["id"], 15)
        self.api.end_status = 404                                 # the API's idle write-up came first
        self.assertEqual(web_chat.end(chat["id"]), "ended")

    def test_a_relay_the_web_server_lost_is_marked_interrupted(self):
        chat = web_chat.new_chat("wren", ME)
        chat.update(state="replying", reply={"started": "x", "activity": ["thinking"], "answer": "half"})
        web_chat.save(chat)
        web_chat.reap()
        c = web_chat.load(chat["id"])
        self.assertEqual(c["state"], "idle")
        self.assertIn("web service restarted", c["turns"][-1]["note"])
        self.assertEqual(c["turns"][-1]["content"], "half")


class OlderApiTest(ChatTest):
    """The web UI may be newer than the API service running (restarted later): it must not misread the old one."""

    def test_end_on_an_api_without_the_route_says_so_and_does_not_end(self):
        chat, _ = web_chat.send("wren", ME, "hi", [])
        self.wait_idle(chat["id"])
        self.api.end_status, self.api.end_type = 404, "invalid_request_error"
        with self.assertRaisesRegex(web_chat.Refused, "older than the web UI"):
            web_chat.end(chat["id"])
        self.assertNotEqual(web_chat.load(chat["id"])["state"], "ended")

    def test_a_stream_that_does_not_say_what_it_heard_gets_no_second_reply(self):
        self.api.hold.set()
        self.api.heard = None
        chat, _ = web_chat.send("wren", ME, "start", [])
        time.sleep(0.3)
        web_chat.send("wren", ME, "also", [])
        self.api.hold.clear()
        self.wait_idle(chat["id"], 15)
        self.assertEqual(len(self.api.chats()), 1)


class FilesTest(ChatTest):
    def test_files_go_in_as_telegrams_do(self):
        chat, _ = web_chat.send("wren", ME, "look", [("photo.png", PNG), ("notes.md", b"# x")])
        c = self.wait_idle(chat["id"])
        turn = c["turns"][0]
        self.assertEqual(turn["shown"], "look")
        self.assertIn("[Daniele attached, saved in my folder:]", turn["content"])
        self.assertIn("attachments/from-daniele/", turn["content"])
        saved = self.dir / "instances" / "wren" / Path(turn["content"].split("- ")[1].split(" (")[0])
        self.assertEqual(saved.read_bytes(), PNG)
        self.assertEqual([a["name"] for a in turn["attachments"]], ["photo.png", "notes.md"])
        self.assertEqual(self.api.chats()[0][2]["messages"][0]["content"], turn["content"])

    def test_a_file_too_big_is_refused_before_anything_is_written_or_sent(self):
        with mock.patch.object(messages, "ATTACH_MAX_BYTES", 10):
            with self.assertRaisesRegex(web_chat.Refused, "larger than"):
                web_chat.send("wren", ME, "big", [("photo.png", PNG)])
        self.assertFalse((self.dir / "instances" / "wren" / "attachments").exists())
        self.assertEqual(self.api.requests, [])

    def test_an_empty_message_is_not_sent(self):
        with self.assertRaisesRegex(web_chat.Refused, "empty"):
            web_chat.send("wren", ME, "  ", [])

    def test_multipart_bodies_are_read(self):
        boundary = "XyZ"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhi there\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
        fields, files = web_chat.parse_multipart(f"multipart/form-data; boundary={boundary}", body)
        self.assertEqual(fields, {"text": "hi there"})
        self.assertEqual(files, [("a.png", PNG)])


class ChatRoutesTest(WebTest):
    def setUp(self):
        super().setUp()
        self.inst = Path(tempfile.mkdtemp(prefix="shaft-chat-inst-")).resolve()
        self.addCleanup(shutil.rmtree, self.inst, True)
        (self.inst / "wren").mkdir()
        for p in (mock.patch.object(web, "_names", return_value=["wren"]),
                  mock.patch.object(web_chat, "SUP_DIR", self.sup), mock.patch.object(web, "_sent_during", return_value=[]),
                  mock.patch("supervisor.core.INSTANCES", self.inst)):
            p.start()
            self.addCleanup(p.stop)
        self.cookie = self.paired()

    def chat_with(self, turns: list[dict], state: str = "idle") -> dict:
        chat = web_chat.new_chat("wren", ME)
        chat.update(turns=turns, state=state)
        web_chat.save(chat)
        return chat

    def test_the_send_route_takes_multipart_with_the_page_token_only(self):
        body, ctype = b"--B\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhi\r\n--B--\r\n", "multipart/form-data; boundary=B"
        with mock.patch.object(web_chat, "send", return_value=({}, "sent to wren")) as send:
            ok = self.req("/chat/wren/send", "POST", cookie=self.cookie, body=body,
                          headers={"content-type": ctype, "sec-fetch-site": "same-origin",
                                   "x-csrf-token": self.csrf(self.cookie)})
            no = self.req("/chat/wren/send", "POST", cookie=self.cookie, body=body,
                          headers={"content-type": ctype, "sec-fetch-site": "same-origin"})
        self.assertEqual((ok.status, no.status), (200, 403))
        send.assert_called_once_with("wren", ME, "hi", [])

    def test_the_page_shows_the_conversation_escaped_and_polls_it(self):
        self.chat_with([{"role": "user", "content": "<script>x</script>", "shown": "<script>x</script>", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "**bold** <img src=x onerror=y>", "ts": "2026-10-19T12:00:05",
                         "activity": ["🔧 web_search <i>"]}])
        body = self.req("/i/wren/chat", cookie=self.cookie).body.decode()
        self.assertIn('data-poll="/fragment/chat/wren"', body)
        self.assertIn('data-multipart="1"', body)
        self.assertIn("<b>bold</b>", body)
        self.assertNotIn("<script>x", body)
        self.assertNotIn("<img src=x", body)
        self.assertIn("🔧 web_search &lt;i&gt;", body)
        self.assertIn('data-post="/action/chat-end"', body)

    def test_a_running_reply_shows_its_activity_and_stop(self):
        chat = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"}], "replying")
        chat["reply"] = {"started": "2026-10-19T12:00:01", "activity": ["waiting: wren is in a cycle"], "answer": ""}
        web_chat.save(chat)
        body = self.req("/fragment/chat/wren", cookie=self.cookie).body.decode()
        self.assertIn("waiting: wren is in a cycle", body)
        self.assertIn('data-post="/action/chat-stop"', body)

    def test_chat_files_follow_the_attachment_rules(self):
        d = self.inst / "wren" / "attachments" / "from-daniele" / "t"
        d.mkdir(parents=True)
        (d / "p.png").write_bytes(PNG)
        (d / "x.svg").write_text("<svg onload=alert(1)>")
        chat = self.chat_with([{"role": "user", "content": "x", "ts": "2026-10-19T12:00:00", "attachments": [
            {"name": "p.png", "path": "instances/wren/attachments/from-daniele/t/p.png", "bytes": 72},
            {"name": "x.svg", "path": "instances/wren/attachments/from-daniele/t/x.svg", "bytes": 20},
            {"name": "e", "path": "instances/../.env", "bytes": 1}]}])
        img = self.req(f"/chatfiles/{chat['id']}/0/0", cookie=self.cookie)
        svg = self.req(f"/chatfiles/{chat['id']}/0/1", cookie=self.cookie)
        self.assertEqual((img.status, img.headers["Content-Type"]), (200, "image/png"))
        self.assertEqual(svg.headers["Content-Type"], "application/octet-stream")
        self.assertEqual(self.req(f"/chatfiles/{chat['id']}/0/2", cookie=self.cookie).status, 404)
        self.assertEqual(self.req(f"/chatfiles/{chat['id']}/0/9", cookie=self.cookie).status, 404)
        self.assertEqual(self.req(f"/chatfiles/{chat['id']}/0/0").status, 403)


if __name__ == "__main__":
    unittest.main()


class ChatCentricTest(ChatRoutesTest):
    """PLAN-webui-chat §1: the chat is the front page, a live rail lists every chat, the instance's pages sit behind ⋯."""

    def test_the_front_page_is_the_chat_last_opened_else_the_first(self):
        r = self.req("/", cookie=self.cookie)
        self.assertEqual((r.status, r.headers["Location"]), (302, "/i/wren/chat"))
        r = self.req("/", cookie=self.cookie + "; shaft_last=wren")
        self.assertEqual(r.headers["Location"], "/i/wren/chat")
        r = self.req("/", cookie=self.cookie + "; shaft_last=gone")          # an instance that is no more: the first
        self.assertEqual(r.headers["Location"], "/i/wren/chat")
        with mock.patch.object(web, "_names", return_value=[]):
            self.assertEqual(self.req("/", cookie=self.cookie).headers["Location"], "/overview")
        self.assertEqual(self.req("/overview", cookie=self.cookie).status, 200)

    def test_opening_a_chat_remembers_it_for_the_front_page(self):
        r = self.req("/i/wren/chat", cookie=self.cookie)
        self.assertIn("shaft_last=wren;", r.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", r.headers["Set-Cookie"])

    def test_the_rail_shows_state_last_line_and_when_it_last_answered(self):
        self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "> quoted\n<b>first</b> line\nsecond", "ts": "2026-10-19T12:00:05"}])
        with mock.patch.object(web, "overview", return_value=snap(state="in a cycle")):
            body = self.req("/fragment/rail?c=i:wren", cookie=self.cookie).body.decode()
        self.assertIn('class="inst on"', body)
        self.assertIn('data-last="2026-10-19T12:00:05"', body)
        self.assertIn('<span class="dot st-cycle" title="in a cycle">', body)
        self.assertIn("&lt;b&gt;first&lt;/b&gt; line", body)                  # escaped, and not the quoted line
        self.assertNotIn("quoted", body)
        self.assertIn('href="/overview"', body)

    def test_the_rail_says_replying_and_counts_alerts(self):
        self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"}], "replying")
        s = snap()
        s["alerts"] = [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}]
        with mock.patch.object(web, "overview", return_value=s):
            body = self.req("/fragment/rail", cookie=self.cookie).body.decode()
        self.assertIn("replying…", body)
        self.assertIn('data-last=""', body)                                    # it has not answered yet
        self.assertIn('<span class="badge bad" title="alerts">2</span>', body)

    def test_the_chat_page_keeps_the_instance_pages_behind_its_menu(self):
        body = self.req("/i/wren/chat", cookie=self.cookie).body.decode()
        self.assertIn('<header class="chathead"><h1>wren</h1>', body)
        self.assertIn("cloud mimo-v2.6-pro · idle · $0.12 today", body)
        self.assertNotIn('class="tabs"', body)
        for page in ("/i/wren", "/i/wren/config", "/i/wren/drive", "/i/wren/access", "/i/wren/judge",
                     "/i/wren/changes", "/i/wren/benchmarks", "/i/wren/transcripts", "/i/wren/journal",
                     "/i/wren/activity"):
            self.assertIn(f'href="{page}"', body, page)
        self.assertIn('data-post="/action/cycle"', body)                       # its actions too
        self.assertIn('data-poll="/fragment/rail?c=i:wren"', body)

    def test_with_no_chat_going_on_the_page_shows_the_last_one_and_a_composer(self):
        """It showed "No chat open" over an empty box, though the conversation had just ended."""
        old = self.chat_with([{"role": "user", "content": "earlier question", "ts": "2026-10-19T12:00:00"},
                              {"role": "assistant", "content": "earlier answer", "ts": "2026-10-19T12:00:05"}], "ended")
        old["ended"] = "2026-10-19T12:30:00"
        web_chat.save(old)
        body = self.req("/i/wren/chat", cookie=self.cookie).body.decode()
        self.assertIn("earlier answer", body)
        self.assertIn("Your next message starts a new conversation", body)
        self.assertIn('data-post="/chat/wren/send"', body)
        self.assertIn('<div class="earlier"><h3>Earlier web chats</h3>', body)       # in the ⋯ menu
        self.assertIn(f"/i/wren/chat?id={old['id']}", body)
        frag = self.req("/fragment/chat/wren", cookie=self.cookie).body.decode()
        self.assertIn("earlier answer", frag)                                       # its refresh shows the same

    def test_the_chat_page_carries_what_its_script_needs(self):
        """PLAN-webui-chat §2: copy an answer as written, the reply's clock, a draft per instance, drop anywhere."""
        chat = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                               {"role": "assistant", "content": '**a** "<x>"', "ts": "2026-10-19T12:00:05"},
                               {"role": "user", "content": "q2", "ts": "2026-10-19T12:01:00"}], "replying")
        chat["reply"] = {"started": "2026-10-19T12:01:01", "activity": [], "answer": ""}
        web_chat.save(chat)
        body = self.req("/i/wren/chat", cookie=self.cookie).body.decode()
        self.assertIn('data-raw="**a** &quot;&lt;x&gt;&quot;"', body)              # the Markdown, escaped once
        self.assertIn('data-copy', body)
        self.assertIn('<span class="elapsed" data-since="2026-10-19T12:01:01">', body)
        self.assertIn('data-draft="wren"', body)
        self.assertIn('data-dropzone hidden', body)
        self.assertIn('data-newbelow hidden', body)
        self.assertIn("data-notify", body)

    def test_a_past_chat_read_only_takes_no_drop(self):
        old = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"}], "ended")
        body = self.req(f"/i/wren/chat?id={old['id']}", cookie=self.cookie).body.decode()
        self.assertNotIn("data-dropzone", body)
        self.assertNotIn('data-post="/chat/wren/send"', body)

    def test_previews_of_picked_files_are_allowed_and_nothing_else_is(self):
        csp = self.req("/i/wren/chat", cookie=self.cookie).headers["Content-Security-Policy"]
        self.assertIn("img-src 'self' data: blob:;", csp)
        self.assertIn("script-src 'self';", csp)


class DocsInChatTest(ChatTest):
    """PLAN-webui-chat §3 D1: the instance is told about documents once per chat; a reply's documents get cards."""

    def setUp(self):
        super().setUp()
        self.docs = self.dir / "instances" / "wren" / "notes" / "docs"
        from supervisor import docs
        p = mock.patch.object(docs, "instance_root", lambda n: self.dir / "instances" / n)
        p.start()
        self.addCleanup(p.stop)

    def test_only_the_first_message_carries_the_note_and_he_never_sees_it(self):
        chat, _ = web_chat.send("wren", ME, "one", [])
        self.wait_idle(chat["id"])
        web_chat.send("wren", ME, "two", [])
        c = self.wait_idle(chat["id"])
        self.assertIn(web_chat.DOCS_NOTE, c["turns"][0]["content"])
        self.assertEqual(c["turns"][0]["shown"], "one")
        self.assertNotIn("Web chat:", c["turns"][2]["content"])
        from supervisor import web_views
        self.assertNotIn("Web chat:", web_views.chat_feed("wren", c, []))

    def test_a_reply_that_wrote_documents_gets_a_card_for_each(self):
        self.docs.mkdir(parents=True)
        (self.docs / "old.md").write_text("untouched")
        (self.docs / "plan.md").write_text("v1")
        real = web_chat._docs_now

        def write_during_reply(name):                    # the second call is the reply's end: write before it
            out = real(name)
            if calls.append(1) or len(calls) == 2:
                (self.docs / "plan.md").write_text("v2")
                (self.docs / "new.md").write_text("hello")
                out = real(name)
            return out
        calls = []
        with mock.patch.object(web_chat, "_docs_now", side_effect=write_during_reply):
            chat, _ = web_chat.send("wren", ME, "write the plan", [])
            c = self.wait_idle(chat["id"])
        self.assertEqual(c["turns"][1]["docs"], [{"path": "new.md", "created": True},
                                                 {"path": "plan.md", "created": False}])
        from supervisor import web_views
        feed = web_views.chat_feed("wren", c, [])
        self.assertIn('data-open-doc="plan.md"', feed)
        self.assertIn("📄 new.md <span class=\"dim\">— created</span>", feed)
        self.assertEqual(web_views.in_chat(c), {"new.md", "plan.md"})

    def test_a_reply_goes_on_when_the_documents_cannot_be_read(self):
        """It claimed "never fails a reply" but caught Exception only: instance_root's SystemExit failed every send
        (found by the full suite once the worktree had no instances folder)."""
        def gone(name):
            raise SystemExit(f"no instance named {name!r}")
        with mock.patch("supervisor.docs.instance_root", side_effect=gone):
            chat, said = web_chat.send("wren", ME, "hi", [])
            c = self.wait_idle(chat["id"])
        self.assertEqual(said, "sent to wren")
        self.assertEqual(c["turns"][1]["content"], "Answer 1.")

    def test_a_reply_that_wrote_none_has_no_cards(self):
        chat, _ = web_chat.send("wren", ME, "hi", [])
        c = self.wait_idle(chat["id"])
        self.assertNotIn("docs", c["turns"][1])


class DocsPanelTest(ChatRoutesTest):
    def setUp(self):
        super().setUp()
        self.docs = self.inst / "wren" / "notes" / "docs"
        self.docs.mkdir(parents=True)
        from supervisor import docs
        p = mock.patch.object(docs, "instance_root", lambda n: self.inst / n)
        p.start()
        self.addCleanup(p.stop)

    def test_the_panel_lists_documents_and_opens_one_rendered_never_as_markup(self):
        (self.docs / "plan.md").write_text("# Plan\n\n- **step** <script>alert(1)</script>\n")
        (self.docs / "other.md").write_text("x")
        body = self.req("/fragment/docs/wren?doc=plan.md", cookie=self.cookie).body.decode()
        self.assertIn('data-open-doc="other.md"', body)
        self.assertIn('<div class="docview" data-doc="plan.md"', body)
        self.assertIn("<b>step</b>", body)
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_with_none_asked_it_opens_the_last_this_chat_changed(self):
        (self.docs / "plan.md").write_text("the plan")
        (self.docs / "notes.md").write_text("not this one")
        self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "done", "ts": "2026-10-19T12:00:05",
                         "docs": [{"path": "plan.md", "created": True}]}])
        page = self.req("/i/wren/chat", cookie=self.cookie).body.decode()
        self.assertIn('data-poll="/fragment/docs/wren?doc=plan.md"', page)
        self.assertIn("the plan", page)
        self.assertIn('<span class="mark" title="changed in this chat">●</span> plan.md', page)

    def test_a_path_out_of_the_folder_is_refused_in_the_panel_not_the_page(self):
        r = self.req("/fragment/docs/wren?doc=../config.env", cookie=self.cookie)
        self.assertEqual(r.status, 200)
        self.assertIn('class="bad"', r.body.decode())
        self.assertIn("a document is a .md file", r.body.decode())
        self.assertEqual(self.req("/i/wren/chat?doc=../x.md", cookie=self.cookie).status, 200)

    def test_an_unknown_instance_has_no_panel(self):
        self.assertEqual(self.req("/fragment/docs/nobody", cookie=self.cookie).status, 404)


class DocEditsTest(DocsInChatTest):
    """PLAN-webui-chat §3 D2: what the instance is told of the operator's edits, and when."""

    def test_the_diff_says_what_changed_and_only_that(self):
        self.assertEqual(web_chat.edit_diff("p.md", None, "# P\n"), "[Daniele created notes/docs/p.md:]\n```markdown\n# P\n\n```")
        d = web_chat.edit_diff("p.md", "a\nb\nc\n", "a\nB\nc\n")
        self.assertTrue(d.startswith("[Daniele edited notes/docs/p.md:]\n```diff\n--- a/notes/docs/p.md"))
        self.assertIn("\n-b\n+B\n", d)
        self.assertEqual(web_chat.edit_diff("p.md", "same", "same"), "[Daniele saved notes/docs/p.md unchanged.]")
        long = web_chat.edit_diff("p.md", "", "\n".join(f"line {i}" for i in range(3000)))
        self.assertIn("+3000 −0 lines, too long to show here — read the file", long)

    def test_a_document_with_its_own_code_fence_does_not_close_the_diff(self):
        d = web_chat.edit_diff("p.md", "a\n", "a\n```python\nx\n```\n")
        self.assertTrue(d.startswith("[Daniele edited notes/docs/p.md:]\n````diff\n"))
        self.assertTrue(d.endswith("\n````"))
        c = web_chat.edit_diff("p.md", None, "````\nfour\n````")
        self.assertTrue(c.startswith("[Daniele created notes/docs/p.md:]\n`````markdown\n"))

    def test_saves_while_it_is_idle_go_once_with_the_next_message_as_one_diff(self):
        chat, _ = web_chat.send("wren", ME, "start", [])
        self.wait_idle(chat["id"])
        said = web_chat.doc_saved("wren", ME, "plan.md", "one\ntwo\n", "one\n2\n", "sha1")
        self.assertEqual(said, "saved · wren gets the change with your next message")
        web_chat.doc_saved("wren", ME, "plan.md", "one\n2\n", "one\n2\nthree\n", "sha2")      # folded: from "one\ntwo"
        self.assertEqual(len(self.api.chats()), 1)                                        # nothing sent yet
        web_chat.send("wren", ME, "continue it", [])
        c = self.wait_idle(chat["id"])
        sent = self.api.chats()[1][2]["messages"][-1]["content"]
        self.assertTrue(sent.startswith("continue it\n\n[Daniele edited notes/docs/plan.md:]"))
        self.assertIn("-two\n+2\n+three", sent)
        self.assertEqual(sent.count("[Daniele edited"), 1)
        self.assertEqual(c["turns"][2]["edits"], ["plan.md"])
        self.assertEqual(c["turns"][2]["shown"], "continue it")
        self.assertNotIn("pending_edits", c)
        web_chat.send("wren", ME, "and now?", [])
        self.wait_idle(chat["id"])
        self.assertNotIn("Daniele edited", self.api.chats()[2][2]["messages"][-1]["content"])   # never twice

    def test_a_write_of_the_instance_between_two_saves_is_not_called_his(self):
        """Found live: he created plan.md empty, the instance wrote its plan in it, he added a section — and the
        instance would have been told "Daniele created plan.md" with its own plan in it."""
        web_chat.doc_saved("wren", ME, "plan.md", None, "", "s1")
        web_chat.doc_saved("wren", ME, "plan.md", "its plan\n", "its plan\nmy section\n", "s2")
        chat, _ = web_chat.send("wren", ME, "go on", [])
        sent = self.wait_idle(chat["id"])["turns"][0]["content"]
        self.assertNotIn("Daniele created", sent)
        self.assertIn("[Daniele edited notes/docs/plan.md:]", sent)
        self.assertIn(" its plan\n+my section", sent)

    def test_a_save_while_it_replies_reaches_it_at_once(self):
        self.api.hold.set()
        chat, _ = web_chat.send("wren", ME, "write the plan", [])
        time.sleep(0.3)
        said = web_chat.doc_saved("wren", ME, "plan.md", "a\n", "a\nmine\n", "sha1")
        self.assertEqual(said, "saved · wren reads it at its next step")
        body = [r for r in self.api.requests if r[0] == "/v1/chat/interject"][0][2]
        self.assertIn("[Daniele edited notes/docs/plan.md:]", body["messages"][-1]["content"])
        self.assertIn("+mine", body["messages"][-1]["content"])
        self.api.hold.clear()
        self.api.heard = 1
        c = self.wait_idle(chat["id"], 15)
        self.assertTrue(c["turns"][1]["interjected"])
        self.assertEqual(c["turns"][1]["shown"], "")

    def test_an_empty_message_is_sent_only_to_carry_an_edit(self):
        with self.assertRaises(web_chat.Refused):
            web_chat.send("wren", ME, "  ", [])
        web_chat.doc_saved("wren", ME, "plan.md", None, "# new\n", "sha1")
        chat, said = web_chat.send("wren", ME, "", [])
        self.assertEqual(said, "sent to wren")
        c = self.wait_idle(chat["id"])
        self.assertIn("[Daniele created notes/docs/plan.md:]", c["turns"][0]["content"])
        self.assertIn(web_chat.DOCS_NOTE, c["turns"][0]["content"])           # still its first message


class DocSaveRouteTest(DocsPanelTest):
    def setUp(self):
        super().setUp()
        from supervisor import actions, docs

        def run_now(args, timeout=60, who="top"):        # the CLI verb, in this process (with this test's folders)
            self.ran.append(args)
            name, _, path = args[1:4]
            out = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                code = docs.command(name, "set", path, args[5], args[7])
            return code == 0, out[-1] if out else ""
        self.ran = []
        for p in (mock.patch.object(actions, "run_now", side_effect=run_now), mock.patch.object(docs, "log"),
                  mock.patch("supervisor.core.SUP_DIR", self.sup)):
            p.start()
            self.addCleanup(p.stop)

    def post(self, **form):
        import urllib.parse
        return self.req("/docs/wren/save", "POST", cookie=self.cookie, body=urllib.parse.urlencode(form).encode(),
                        headers={"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie),
                                 "content-type": "application/x-www-form-urlencoded"})

    def test_a_save_goes_through_the_cli_and_is_queued_for_the_instance(self):
        from supervisor import docs
        (self.docs / "plan.md").write_text("one\n")
        r = self.post(path="plan.md", text="one\nmine\n", sha=docs.sha(b"one\n"))
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.headers["X-Doc"], "plan.md")
        self.assertEqual(self.ran[0][:4], ["docs", "wren", "set", "plan.md"])
        self.assertEqual((self.docs / "plan.md").read_text(), "one\nmine\n")
        chat = web_chat.open_chat("wren")
        self.assertEqual(chat["pending_edits"]["plan.md"], {"old": "one\n", "new": "one\nmine\n"})
        feed = self.req("/fragment/chat/wren", cookie=self.cookie).body.decode()
        self.assertIn("Your edits to plan.md go to wren with your next message", feed)

    def test_a_stale_save_is_refused_shows_what_changed_and_writes_nothing(self):
        from supervisor import docs
        (self.docs / "plan.md").write_text("one\nby the instance\n")
        r = self.post(path="plan.md", text="one\nmine\n", sha=docs.sha(b"one\n"))
        self.assertEqual(r.status, 409)
        body = r.body.decode()
        self.assertIn("changed since you looked", body)
        self.assertIn("+by the instance", body)
        self.assertEqual((self.docs / "plan.md").read_text(), "one\nby the instance\n")
        self.assertIsNone(web_chat.open_chat("wren"))
        # Found live: Save after the refusal was refused forever (the page kept the old sha). It gets the sha now:
        self.assertEqual(r.headers["X-Doc-Sha"], docs.sha(b"one\nby the instance\n"))
        again = self.post(path="plan.md", text="one\nmine\n", sha=r.headers["X-Doc-Sha"])
        self.assertEqual(again.status, 200)
        self.assertEqual((self.docs / "plan.md").read_text(), "one\nmine\n")

    def test_a_new_document_by_name(self):
        r = self.post(path="ideas", text="", sha="none")
        self.assertEqual((r.status, r.headers.get("X-Doc")), (200, "ideas.md"))
        self.assertTrue((self.docs / "ideas.md").is_file())
        self.assertEqual(self.post(path="ideas", text="", sha="none").status, 409)     # exists already

    def test_a_later_write_that_dropped_his_lines_is_said_with_his_version(self):
        from supervisor import docs
        (self.docs / "plan.md").write_text("one\n")
        self.post(path="plan.md", text="one\nmy line\n", sha=docs.sha(b"one\n"))
        (self.docs / "plan.md").write_text("one\nmy line\nits addition\n")             # built on it: no warning
        panel = self.req("/fragment/docs/wren?doc=plan.md", cookie=self.cookie).body.decode()
        self.assertNotIn("no longer in it", panel)
        (self.docs / "plan.md").write_text("one\nits rewrite\n")                       # dropped it: said
        panel = self.req("/fragment/docs/wren?doc=plan.md", cookie=self.cookie).body.decode()
        self.assertIn("1 line(s) you wrote at", panel)
        self.assertIn("Restore yours", panel)
        self.assertIn('data-post="/docs/wren/save"', panel)
        self.assertIn(f'name="sha" value="{docs.sha(b"one" + bytes([10]) + b"its rewrite" + bytes([10]))}"', panel)


class DocsButtonTest(DocsPanelTest):
    def test_with_no_documents_the_panel_can_still_be_opened_to_make_one(self):
        """Found live: with no documents the panel is hidden, and its New document with it."""
        body = self.req("/i/wren/chat", cookie=self.cookie).body.decode()
        self.assertIn("data-show-docs", body)
        self.assertIn('data-post="/docs/wren/save"', body)                       # the New document form is there
