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
        self.assertEqual(body, {"model": "wren", "messages": [{"role": "user", "content": "hello <b>"}], "stream": True})
        self.assertNotIn(KEY, (self.dir / "sup" / "web" / "chats" / f"{chat['id']}.json").read_text())

    def test_the_next_message_carries_the_conversation_so_far(self):
        chat, _ = web_chat.send("wren", ME, "one", [])
        self.wait_idle(chat["id"])
        web_chat.send("wren", ME, "two", [])
        self.wait_idle(chat["id"])
        self.assertEqual(self.api.chats()[1][2]["messages"], [{"role": "user", "content": "one"},
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
