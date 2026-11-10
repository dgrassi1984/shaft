"""Voice turns, server side (M1-CONTRACT C1, C4, C5): a spoken send is recorded `via: voice` and the
API is told (`"voice": true`), a typed one neither; the reply's events reach the app's stream from the relay's memory
as the API writes them, not through the chat file written every SAVE_EVERY_S (up to 1.5 s added per piece: M0); the
stream replays after `after`, ends with `end`, refuses when nothing runs, notices a client that left, and holding it
silences the push for that chat; the voices routes map voices.Refused to 409. No network: the API is a loopback fake."""
from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import supervisor
from supervisor import app_api, core, web, web_chat, web_views, webpush
from supervisor.tests.test_app_api import CID, AppTest, turn
from supervisor.tests.test_web import HOST, ME

KEY = "sk-shaft-test"
REAL_START = web_chat._start                     # AppTest replaces it with a no-op: these tests run the real relay


def chunk(delta: dict | None = None, finish=None, **extra) -> bytes:
    return b"data: " + json.dumps({"choices": [{"delta": delta or {}, "finish_reason": finish}], **extra}).encode() + b"\n\n"


def event(kind: str, **fields) -> bytes:
    """A chunk carrying the API's shaft_event (C3), as on a voice turn."""
    return chunk(shaft_event={"type": kind, **fields})


DONE = b"data: [DONE]\n\n"


def voice_turn(hold: threading.Event | None = None) -> list:
    """What the API streams for a spoken question that needs a tool; `hold`: the API waits there for the test."""
    return [chunk({"reasoning_content": "🔧 read_file notes.md\n"}),
            event("status", phase="model", detail="thinking"),
            event("draft", text="Let me "), event("draft", text="look."),
            *([hold] if hold else []),
            event("step_end", answer=False),
            event("tool_start", name="read_file"), event("tool_end", name="read_file", ok=True),
            event("draft", text="It is sunny."), event("step_end", answer=True),
            chunk({"content": "It is sunny."}), chunk({}, "stop", interjections_heard=0), DONE]


class ScriptApi:
    """SHAFT's API as the relay sees it: each request's stream written piece by piece from `script` (a
    threading.Event in it: wait there until the test sets it), the time each piece went out kept in `written`."""

    def __init__(self):
        self.requests, self.script, self.written = [], [chunk({"content": "Typed answer."}), chunk({}, "stop"), DONE], {}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, body))
                if self.path == "/v1/chat/interject":
                    data = b'{"interjected": true}'
                    self.send_response(202)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for k, piece in enumerate(list(fake.script)):
                        if isinstance(piece, threading.Event):
                            piece.wait(30)
                            continue
                        self.wfile.write(piece)
                        self.wfile.flush()
                        fake.written[k] = time.monotonic()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        for piece in self.script:
            if isinstance(piece, threading.Event):
                piece.set()
        self.server.shutdown()
        self.server.server_close()

    def bodies(self, route="/v1/chat/completions") -> list[dict]:
        return [b for p, b in self.requests if p == route]


class VoiceTest(AppTest):
    """The app API over a real relay to the fake API."""

    def setUp(self):
        super().setUp()
        self.fake = ScriptApi()
        self.addCleanup(self.fake.close)
        port = self.fake.server.server_address[1]
        for p in (mock.patch.object(web_chat, "_start", REAL_START),
                  mock.patch.object(web_chat, "_api_conn",
                                    lambda timeout: http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)),
                  mock.patch("supervisor.core.secrets_env", return_value={"SHAFT_API_KEY": KEY}),
                  mock.patch.object(app_api, "KEEPALIVE_S", 0.1)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(web_chat._streams.clear)
        self.addCleanup(self.end_relays)             # last registered, first run: while the patches still hold

    def end_relays(self):
        for piece in self.fake.script:
            if isinstance(piece, threading.Event):
                piece.set()
        left = list(web_chat._relays.items())
        for _, relay in left:
            relay.stop()
        end = time.monotonic() + 10
        while any(web_chat._relays.get(cid) is relay for cid, relay in left) and time.monotonic() < end:
            time.sleep(0.05)

    def wait_idle(self, cid: str, timeout: float = 10) -> dict:
        end = time.monotonic() + timeout
        while True:                                  # checked once more after the deadline: a sleep that overran it
            past = time.monotonic() >= end           # must not fail a condition that came true meanwhile
            c = web_chat.load(cid)
            if c["state"] != "replying" and not web_chat.replying(cid):
                return c
            if past:
                self.fail(f"still replying: {c}")
            time.sleep(0.02)

    def wait_for(self, check, timeout: float = 5, what: str = "") -> None:
        end = time.monotonic() + timeout
        while True:                                  # as wait_idle: the last check comes after the deadline
            past = time.monotonic() >= end
            if check():
                return
            if past:
                self.fail(f"timed out waiting for {what or check}")
            time.sleep(0.01)

    def sent(self, r) -> str:
        self.assertEqual(r.status, 200, r.body)
        return json.loads(r.body)["chat"]

    def user_items(self) -> list[dict]:
        return [i for i in self.get("/app/v1/chat/wisp")["items"] if i["type"] == "user"]


# -- C1: the voice flag ------------------------------------------------------------------------------------------------
class VoiceFlagTest(VoiceTest):
    def test_a_spoken_send_is_via_voice_and_tells_the_api(self):
        """The bug: the instance answers a spoken question as if it were typed (no `voice` to the API, so no spoken
        note and no voice effort), and the chat cannot show the message was transcribed speech."""
        self.fake.script = voice_turn()
        cid = self.sent(self.send(text="What's the weather?", mid="v-1", voice="1"))
        c = self.wait_idle(cid)
        self.assertEqual(c["turns"][0]["via"], "voice")
        self.assertIs(self.fake.bodies()[0]["voice"], True)
        self.assertEqual([i["via"] for i in self.user_items()], ["voice"])
        html = str(web_views.chat_feed("wisp", c, []))
        self.assertIn("· spoken", html)

    def test_a_typed_send_carries_neither(self):
        """The bug: a typed message flagged spoken (or recorded so) gets the spoken note and short answers."""
        cid = self.sent(self.send(text="typed", mid="t-1"))
        c = self.wait_idle(cid)
        self.assertNotIn("via", c["turns"][0])
        self.assertNotIn("voice", self.fake.bodies()[0])
        self.assertEqual([i["via"] for i in self.user_items()], [None])
        self.assertNotIn("· spoken", str(web_views.chat_feed("wisp", c, [])))
        self.sent(self.send(text="typed too", mid="t-2", voice="0"))
        self.wait_idle(cid)
        self.assertNotIn("voice", self.fake.bodies()[1])

    def test_the_flag_follows_the_last_message_not_the_first(self):
        """The bug: a typed message after a spoken one still told the API the operator is listening."""
        cid = self.sent(self.send(text="spoken", mid="v-1", voice="1"))
        self.wait_idle(cid)
        self.sent(self.send(text="typed", mid="t-1"))
        self.wait_idle(cid)
        bodies = self.fake.bodies()
        self.assertIs(bodies[0]["voice"], True)
        self.assertNotIn("voice", bodies[1])

    def test_a_spoken_interjection_carries_voice(self):
        """The bug: barge-in (a spoken sentence while it replies) reached the API as typed."""
        hold = threading.Event()
        self.fake.script = voice_turn(hold)
        cid = self.sent(self.send(text="start", mid="t-1"))
        self.wait_for(lambda: self.fake.written.get(3), what="the API's drafts")
        r = self.send(text="actually, tomorrow", mid="v-2", voice="1")
        self.assertEqual(json.loads(r.body)["said"], "wisp reads it at its next step")
        self.assertIs(self.fake.bodies("/v1/chat/interject")[0]["voice"], True)
        turns = web_chat.load(cid)["turns"]
        self.assertEqual((turns[-1]["interjected"], turns[-1]["via"]), (True, "voice"))
        hold.set()
        self.wait_idle(cid)

    def test_a_spoken_message_queued_is_spoken_when_it_goes(self):
        """The bug: a spoken message queued behind a reply went to the API as typed when the queue was flushed."""
        hold = threading.Event()
        self.fake.script = voice_turn(hold)
        cid = self.sent(self.send(text="start", mid="t-1"))
        self.wait_for(lambda: self.fake.written.get(3), what="the API's drafts")
        self.assertEqual(json.loads(self.send("queue", text="next", mid="v-3", voice="1").body)["said"],
                         "queued — sent when this reply ends")
        self.fake.script = [chunk({"content": "Next."}), chunk({}, "stop"), DONE]
        hold.set()
        self.wait_for(lambda: len(self.fake.bodies()) == 2, what="the queued reply")
        c = self.wait_idle(cid)
        self.assertIs(self.fake.bodies()[1]["voice"], True)
        self.assertEqual(c["turns"][2]["via"], "voice")

    def test_the_web_form_takes_voice_too(self):
        """The bug: the web's send form dropped the field, so only the app could send a spoken turn."""
        body = b"".join(f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                        for k, v in (("text", "hi"), ("mid", "w-1"), ("voice", "1"))) + b"--B--\r\n"
        cookie = self.paired()
        r = self.req("/chat/wisp/send", "POST", body=body, cookie=cookie,
                     headers={"content-type": "multipart/form-data; boundary=B", "sec-fetch-site": "same-origin",
                              "x-csrf-token": self.csrf(cookie)})
        self.assertEqual(r.status, 200, r.body)
        c = self.wait_idle(web_chat.current("wisp")["id"])
        self.assertEqual(c["turns"][0]["via"], "voice")
        self.assertIs(self.fake.bodies()[0]["voice"], True)

    def test_bad_voice_values_are_refused(self):
        """The bug: voice=true (or a spoken message for its next cycle) silently went as typed."""
        self.error(self.send(text="x", mid="b-1", voice="true"), 400, "bad_request")
        self.error(self.send("background", text="x", mid="b-2", voice="1"), 400, "bad_request")


# -- C4: the stream ----------------------------------------------------------------------------------------------------
def read_events(stream, until: str = "end", timeout: float = 10) -> list[dict]:
    """The events a stream generator yields (comments skipped) until one of type `until`."""
    out, end = [], time.monotonic() + timeout
    for piece in stream:
        for block in piece.decode().split("\n\n"):
            data = [line[6:] for line in block.split("\n") if line.startswith("data: ")]
            if data:
                out.append(json.loads(data[0]))
        if out and out[-1]["type"] == until or time.monotonic() > end:
            return out
    return out


class StreamTest(VoiceTest):
    def stream(self, query: str = "", **kw):
        r = self.api(f"/app/v1/chat/wisp/stream{query}", **kw)
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.headers["Content-Type"], "text/event-stream; charset=utf-8")
        self.addCleanup(r.stream.close)
        return r.stream

    def test_events_arrive_in_order_as_the_api_writes_them_not_with_the_chat_file(self):
        """The bug: a piece of the spoken reply reached the app only through the chat file, written
        every SAVE_EVERY_S (1 s) and polled every 0.5 s — up to 1.5 s added before each piece. With the file write
        pushed to a minute, a draft still reaches the stream within a fraction of a second, while the API holds."""
        hold = threading.Event()
        self.fake.script = voice_turn(hold)
        with mock.patch.object(web_chat, "SAVE_EVERY_S", 60.0):
            cid = self.sent(self.send(text="weather?", mid="v-1", voice="1"))
            stream = self.stream()
            got, deadline = read_events(stream, until="draft", timeout=3), time.monotonic() + 3
            while sum(e["type"] == "draft" for e in got) < 2 and time.monotonic() < deadline:
                got += read_events(stream, until="draft", timeout=1)
            self.assertEqual(sum(e["type"] == "draft" for e in got), 2, f"both drafts while the API holds: {got}")
            arrived = time.monotonic()
            self.assertLess(arrived - self.fake.written[3], 0.5)          # the second draft: written, then here
            self.assertEqual(web_chat.load(cid)["state"], "replying")      # the API still holds the reply
            self.assertEqual(web_chat.load(cid)["reply"]["answer"], "")    # and the chat file never had it
            hold.set()
            got += read_events(stream)
        self.assertEqual([e["type"] for e in got],
                         ["status", "draft", "draft", "step_end", "tool_start", "tool_end", "draft", "step_end",
                          "answer", "end"])
        self.assertEqual([e["seq"] for e in got], list(range(1, 11)))
        self.assertEqual(got[0], {"seq": 1, "type": "status", "reply": got[0]["reply"], "phase": "model",
                                  "detail": "thinking"})
        self.assertEqual(got[3]["answer"], False)
        self.assertEqual(got[5], {"seq": 6, "type": "tool_end", "reply": got[0]["reply"], "name": "read_file", "ok": True})
        self.assertEqual(got[-2]["text"], "It is sunny.")
        self.assertIsNone(got[-1]["failure"])
        self.assertEqual(len({e["reply"] for e in got}), 1)
        self.assertEqual(list(stream), [])                                  # `end` closed it
        self.wait_idle(cid)

    def test_after_replays_and_a_newer_reply_replays_whole(self):
        """The bug: a reconnect replayed what the app had already spoken (after ignored), or skipped the start of a
        reply that began since (its `after` belonged to the previous reply)."""
        self.fake.script = voice_turn()
        cid = self.sent(self.send(text="weather?", mid="v-1", voice="1"))
        self.wait_idle(cid)
        whole = read_events(self.stream())
        reply = whole[0]["reply"]
        self.assertEqual([e["seq"] for e in read_events(self.stream("?after=7"))], [8, 9, 10])
        self.assertEqual([e["seq"] for e in read_events(self.stream(headers={"last-event-id": "8"}))], [9, 10])
        self.assertEqual([e["seq"] for e in read_events(self.stream(f"?after=9&reply={reply}"))], [10])
        self.assertEqual(len(read_events(self.stream("?after=9&reply=0000000000000000"))), 10)   # another reply's seq
        self.error(self.api("/app/v1/chat/wisp/stream?after=10"), 409, "not_replying")         # all of it seen
        self.error(self.api("/app/v1/chat/wisp/stream?after=-1"), 400, "bad_request")
        self.error(self.api("/app/v1/chat/wisp/stream?after=x"), 400, "bad_request")

    def test_409_when_nothing_runs_and_after_the_keep_window(self):
        """The bug: a stream with no reply to follow hung open (or answered 200 with nothing), and the app waited on
        silence instead of listening again."""
        self.error(self.api("/app/v1/chat/wisp/stream"), 409, "not_replying")      # no chat at all
        self.chat([turn("user", "2026-11-03T13:58:40", "hi", shown="hi")])
        self.error(self.api("/app/v1/chat/wisp/stream"), 409, "not_replying")      # a chat, no reply
        self.error(self.api("/app/v1/chat/ghost/stream"), 404, "not_found")
        self.fake.script = voice_turn()
        cid = self.sent(self.send(text="weather?", mid="v-1", voice="1"))
        self.wait_idle(cid)
        relay = web_chat.stream_of(cid)
        self.assertEqual(read_events(self.stream())[-1]["type"], "end")             # kept: a reconnect gets the end
        relay.ended -= web_chat.STREAM_KEEP_S + 1
        self.error(self.api("/app/v1/chat/wisp/stream"), 409, "not_replying")      # and then not any more
        self.assertIsNone(web_chat.stream_of(cid))

    def test_a_failed_or_stopped_reply_ends_the_stream_with_why(self):
        """The bug: a reply that failed or was stopped left the stream open, and the app spoke nothing, forever."""
        hold = threading.Event()
        self.fake.script = voice_turn(hold)
        cid = self.sent(self.send(text="weather?", mid="v-1", voice="1"))
        stream = self.stream()
        read_events(stream, until="draft")
        self.assertEqual(json.loads(self.api("/app/v1/chat/wisp/stop", "POST", {"cid": cid}).body)["said"],
                         "stopping the reply")
        got = read_events(stream)
        self.assertEqual((got[-2]["type"], got[-1]["type"], got[-1]["failure"]), ("answer", "end", "stopped by you"))
        self.wait_idle(cid)

    def test_holding_the_stream_is_watching_and_no_push_goes_to_that_device(self):
        """The bug: the operator listening to the reply also got a push for it on the same phone."""
        cid = self.sent(self.send(text="typed", mid="t-1"))
        self.wait_idle(cid)
        self.assertNotIn(self.device, webpush.notify.call_args.kwargs["skip"])     # nobody watching: pushed
        web_chat._watched.clear()
        hold = threading.Event()
        self.fake.script = voice_turn(hold)
        self.sent(self.send(text="weather?", mid="v-1", voice="1"))
        stream = self.stream()
        read_events(stream, until="draft")
        self.assertIn(self.device, web_chat.watchers("wisp"))
        hold.set()
        read_events(stream)
        self.wait_idle(cid)
        self.assertIn(self.device, webpush.notify.call_args.kwargs["skip"])

    def test_the_stream_keeps_marking_watching_while_held(self):
        """The bug: a long reply outlived the first watching mark (WATCH_S, 6 s), and its end was pushed to the
        phone that was listening to it."""
        hold = threading.Event()
        self.fake.script = voice_turn(hold)
        self.sent(self.send(text="weather?", mid="v-1", voice="1"))
        stream = self.stream()
        read_events(stream, until="draft")
        with mock.patch.object(app_api, "WATCH_EVERY_S", 0.0):
            web_chat._watched.clear()
            next(stream)                                              # a keepalive, KEEPALIVE_S later
            self.assertIn(self.device, web_chat.watchers("wisp"))
        hold.set()


def sse_events(sock: socket.socket, n: int) -> tuple[bytes, list[dict]]:
    """A streamed response read off `sock` until `n` whole events have come: (the response head, the events). Whole
    frames only — a read may end inside one, so counting `data: ` prefixes could stop before an event's text came."""
    raw = b""
    while True:
        head, sep, body = raw.partition(b"\r\n\r\n")
        frames = body.split(b"\n\n")[:-1] if sep else []  # the last piece is a frame still arriving (or b"")
        events = [json.loads(line[6:]) for f in frames for line in f.split(b"\n") if line.startswith(b"data: ")]
        if len(events) >= n:
            return head, events[:n]
        piece = sock.recv(65536)
        if not piece:
            raise AssertionError(f"the stream ended after {len(events)} of {n} events: {raw[-300:]!r}")
        raw += piece


class StreamServerTest(VoiceTest):
    """The stream through the real web server (web.Handler on a loopback port), as tailscale serve reaches it."""

    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def open(self, path: str) -> socket.socket:
        s = socket.create_connection(self.server.server_address, timeout=5)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nTailscale-User-Login: {ME}\r\n"
                  f"Authorization: Bearer {self.device}.{self.secret}\r\n\r\n".encode())
        return s

    def test_a_client_that_leaves_is_noticed_and_others_are_served_meanwhile(self):
        """The bugs: a held stream blocked every other request of the web server (one thread), or a client that left
        kept its stream — and its watching mark, silencing pushes — until the reply ended. The test's own bug (it
        failed now and then in the gate's 4 workers): it stopped reading at the third `data: ` prefix, which a read
        may deliver without that event's text, and it held the other request to a 2 s clock instead of to the fact
        that the reply was still held."""
        hold = threading.Event()
        self.fake.script = voice_turn(hold)
        cid = self.sent(self.send(text="weather?", mid="v-1", voice="1"))
        s = self.open("/app/v1/chat/wisp/stream")
        head, events = sse_events(s, 3)                     # status and both drafts, flushed as they came
        self.assertIn(b"200", head.split(b"\r\n")[0])
        self.assertIn(b"Content-Type: text/event-stream; charset=utf-8", head)
        self.assertNotIn(b"Content-Length", head)
        self.assertEqual([(e["type"], e.get("text")) for e in events],
                         [("status", None), ("draft", "Let me "), ("draft", "look.")])
        self.assertEqual(app_api.held("wisp"), 1)
        # Meanwhile another request is answered: while the API still holds the reply, so the stream is still held. A
        # server that served one request at a time would answer it only after `hold` is set, which this test does
        # later; the socket's timeout (open) bounds the wait.
        with self.open("/app/v1/instances") as other:
            answer = b""
            try:
                while chunk_ := other.recv(65536):
                    answer += chunk_
            except TimeoutError:
                self.fail(f"another request waited on the held stream: {answer[:200]!r}")
        self.assertIn(b" 200 ", answer.split(b"\r\n")[0])
        self.assertFalse(hold.is_set())
        self.assertEqual(app_api.held("wisp"), 1)
        s.close()                                           # the phone locked, the network went
        self.wait_for(lambda: app_api.held("wisp") == 0, what="the server to notice the client left")
        hold.set()
        self.wait_idle(cid)

    def test_the_test_reads_whole_events_when_a_read_ends_inside_one(self):
        """The test's bug (a gate flake): reading stopped at the third `data: ` prefix, so a read that ended inside
        the third event left its text unread and `"text": "look."` was not found."""
        a, b = socket.socketpair()
        with a, b:
            first = (b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
                     b'id: 1\ndata: {"type": "status"}\n\nid: 2\ndata: {"type": "draft", "text": "Let me "}\n\n'
                     b'id: 3\ndata: {"type": "dr')
            b.sendall(first)
            threading.Timer(0.2, lambda: b.sendall(b'aft", "text": "look."}\n\n')).start()
            head, events = sse_events(a, 3)
            self.assertTrue(head.startswith(b"HTTP/1.0 200"))
            self.assertEqual(events[-1], {"type": "draft", "text": "look."})
            b.close()
            with self.assertRaisesRegex(AssertionError, "ended after 0 of 1"):
                sse_events(a, 1)                    # a stream that ends early fails, never loops on empty reads

    def test_a_closed_client_is_seen_before_a_write_fails(self):
        """The bug: a client that left was noticed only when a write failed — the first write after a close usually
        succeeds — so its stream, and its watching mark, outlived it by a keepalive or more."""
        a, b = socket.socketpair()
        with a:
            self.assertFalse(web._client_gone(a))
            b.close()
            self.assertTrue(web._client_gone(a))

    def test_the_whole_reply_arrives_over_http_and_the_connection_closes(self):
        """The bug: the SSE response never ended (or was buffered whole), so the app could not tell the reply was
        over."""
        self.fake.script = voice_turn()
        cid = self.sent(self.send(text="weather?", mid="v-1", voice="1"))
        self.wait_idle(cid)
        with self.open("/app/v1/chat/wisp/stream") as s:
            raw = b""
            while chunk_ := s.recv(65536):
                raw += chunk_
        events = [json.loads(line[6:]) for line in raw.decode().split("\n") if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in events][-2:], ["answer", "end"])
        self.assertIn("id: 10\n", raw.decode())


# -- C2: the settings ------------------------------------------------------------------------------------------------
# (in test_app_api_settings.py, beside the other fields)

# -- C5: voices ------------------------------------------------------------------------------------------------------
class Refused(ValueError):
    pass


class VoicesTest(AppTest):
    """The app's voices routes over a stub of supervisor.voices (lane L4 writes the real one)."""

    def setUp(self):
        super().setUp()
        stub = types.ModuleType("supervisor.voices")
        stub.Refused = Refused
        stub.LANGUAGES = ("en", "it")
        self.calls = []
        held = {"en": {"identifier": "com.apple.voice.premium.en-US.Ava", "name": "Ava", "by": "operator",
                       "ts": "2026-11-09T10:00:00"}, "it": None}

        def record(name, result=None, refuse=None):
            def f(*args):
                self.calls.append((name, *args))
                if refuse and (why := refuse(*args)):
                    raise Refused(why)
                return result
            return f
        stub.get = record("get", held)
        stub.proposals = record("proposals", [{"id": "vp-1", "language": "it", "identifier": "it-voice",
                                               "name": "Alice", "why": "warm", "ts": "2026-11-09T10:05:00",
                                               "state": "open"}])
        stub.available = record("available", {"ios": [{"identifier": "it-voice", "name": "Alice", "language": "it-IT",
                                                      "quality": "enhanced"}], "macos": []})
        stub.assign = record("assign", {}, refuse=lambda n, lang, ident, by: ident == "taken" and "taken is Wisp's voice for it")
        stub.clear = record("clear")
        stub.decide = record("decide", {}, refuse=lambda n, pid, accept, reason: pid == "vp-gone" and "no open proposal vp-gone")
        stub.report = record("report", refuse=lambda platform, voices: any(v["identifier"] == "bad" for v in voices)
                             and "bad is not a voice")
        for p in (mock.patch.dict(sys.modules, {"supervisor.voices": stub}),
                  mock.patch.object(supervisor, "voices", stub, create=True)):
            p.start()
            self.addCleanup(p.stop)

    def test_get_shows_held_proposed_and_available_voices(self):
        """The bug: the app could not show which voice an instance has, what it proposed, or what is installed."""
        got = self.get("/app/v1/voices/wisp")
        self.assertEqual(got["voices"]["en"]["ts"], app_api.stamp("2026-11-09T10:00:00"))
        self.assertIsNone(got["voices"]["it"])
        self.assertEqual(got["proposals"][0]["id"], "vp-1")
        self.assertEqual(got["available"]["ios"][0]["identifier"], "it-voice")
        self.error(self.api("/app/v1/voices/ghost"), 404, "not_found")

    def test_assign_clear_and_refused_by_name(self):
        """The bug: a voice another instance holds was taken silently, or its refusal reached the app as a 500."""
        self.get("/app/v1/voices/wisp", method="POST", data={"language": "it", "identifier": "it-voice"})
        self.assertIn(("assign", "wisp", "it", "it-voice", "operator"), self.calls)
        err = self.error(self.api("/app/v1/voices/wisp", "POST", {"language": "it", "identifier": "taken"}),
                         409, "refused")
        self.assertEqual(err["message"], "taken is Wisp's voice for it")
        self.get("/app/v1/voices/wisp", method="POST", data={"language": "en", "clear": True})
        self.assertIn(("clear", "wisp", "en"), self.calls)
        for bad in ({"language": "it"}, {"language": "it", "identifier": 3}, {"language": "en", "clear": "yes"},
                    {"language": "it", "identifier": "x", "clear": True}):
            with self.subTest(bad=bad):
                self.error(self.api("/app/v1/voices/wisp", "POST", bad), 400, "bad_request")

    def test_a_proposal_is_decided_and_its_refusal_named(self):
        """The bug: deciding a proposal that is gone answered 500, or an accept that was not a bool passed as one."""
        self.get("/app/v1/voices/wisp/proposals/vp-1", method="POST", data={"accept": False, "reason": "too sharp"})
        self.assertIn(("decide", "wisp", "vp-1", False, "too sharp"), self.calls)
        self.error(self.api("/app/v1/voices/wisp/proposals/vp-gone", "POST", {"accept": True}), 409, "refused")
        self.error(self.api("/app/v1/voices/wisp/proposals/vp-1", "POST", {"accept": "yes"}), 400, "bad_request")

    def test_the_device_reports_its_voices(self):
        """The bug: the instance could only propose from a list nobody reported, or a malformed report was stored."""
        v = {"identifier": "it-voice", "name": "Alice", "language": "it-IT", "quality": "enhanced", "extra": 1}
        got = self.get("/app/v1/voices/report", method="POST", data={"platform": "ios", "voices": [v]})
        self.assertIn("available", got)
        self.assertIn(("report", "ios", [{k: v[k] for k in app_api.VOICE_FIELDS}]), self.calls)
        self.error(self.api("/app/v1/voices/report", "POST", {"platform": "ios", "voices": [{**v, "identifier": "bad"}]}),
                   409, "refused")
        self.error(self.api("/app/v1/voices/report", "POST", {"platform": "tvos", "voices": []}), 400, "bad_request")
        self.error(self.api("/app/v1/voices/report", "POST", {"platform": "ios", "voices": [{"name": 1}]}),
                   400, "bad_request")


class InstanceRowTest(AppTest):
    def test_the_chat_list_says_whether_voice_is_on(self):
        """The bug: the app had no way to know whether to show the Talk button for an instance (C2: only Tundra)."""
        self.assertEqual(self.get("/app/v1/instances")["instances"][0]["voice_mode"], "off")      # not set: off
        (self.inst / "wisp" / "config.env").write_text('VOICE_MODE="on"\n')
        self.assertEqual(self.get("/app/v1/instances")["instances"][0]["voice_mode"], "on")
        (self.inst / "wisp" / "config.env").write_text('VOICE_MODE="yes"\n')
        self.assertEqual(self.get("/app/v1/instances")["instances"][0]["voice_mode"], "off")


if __name__ == "__main__":
    unittest.main()
