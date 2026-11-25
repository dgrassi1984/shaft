"""Chat in the browser: every message is a conversation of SHAFT's own API, named by the chat's id;
the reply is relayed and saved by the web server, so it keeps going when the browser drops (the operator, 2026-10-19);
interjections, Stop and End go to the API; files go in as Telegram's do."""
from __future__ import annotations

import http.client
import json
import socket
import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import dashboard, messages, web, web_chat, web_pages
from supervisor.tests.test_web import WebTest, live_snap, snap

KEY, ME = "sk-shaft-test", "operator@example.com"


def topic(name: str = "wisp") -> dict:
    """The topic thread these tests talk in: the newest open thread, else a new one. They were written when a
    message that named no conversation went to the most recently active chat; since 2026-11-11 it goes to Main
    (which never ends), so a test of a conversation's life (end, clear, the next message after End) names its thread."""
    open_ = sorted(web_chat.threads(name), key=lambda c: c.get("created", ""), reverse=True)
    return open_[0] if open_ else web_chat.new_thread(name, ME)


def say(text: str, files: list | None = None, **kw) -> tuple[dict, str]:
    """web_chat.send to the topic thread (topic), or to the conversation `cid` names."""
    return web_chat.send("wisp", ME, text, files or [], cid=kw.pop("cid", None) or topic()["id"], **kw)
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
HOLD_MAX_S = 60                    # a held reply nobody releases or leaves still ends


def chunk(delta: dict, finish=None, **extra) -> bytes:
    return b"data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}], **extra}).encode() + b"\n\n"


class FakeApi:
    """SHAFT's API as the web server sees it: a stream of reasoning, then the answer, then [DONE]."""

    def __init__(self):
        self.requests, self.script, self.hold, self.gone = [], None, threading.Event(), threading.Event()
        self.heard, self.interject_status, self.end_status, self.end_type = 0, 202, 202, "not_found"
        self.pause = time.sleep                      # between a held reply's keep-alives
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
                msgs = body.get("messages") or []
                if not msgs or msgs[-1].get("role") != "user":      # api.py refuses it the same way
                    return self._json(400, {"error": {"message": "messages must end with a user message"}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(chunk({"reasoning_content": "🔧 read_file notes.md\n"}))
                    self.wfile.flush()
                    # A long reply: until the test releases it (hold.clear()) or the client leaves. Never a count of
                    # sleeps: inside a cycle the gate runs at launchd's utility QoS, where timer coalescing made 100
                    # sleeps of 50 ms last 12-15 s, past wait_idle's budget (HoldTest).
                    deadline = time.monotonic() + HOLD_MAX_S
                    while fake.hold.is_set() and time.monotonic() < deadline:
                        fake.pause(0.05)
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

    def close(self):
        self.hold.clear()
        self.server.shutdown()
        self.server.server_close()

    def chats(self):
        return [r for r in self.requests if r[0] == "/v1/chat/completions"]


class ChatTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="shaft-chat-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        (self.dir / "instances" / "wisp").mkdir(parents=True)
        self.api = FakeApi()
        self.addCleanup(self.api.close)
        import http.client
        port = self.api.server.server_address[1]
        for p in (mock.patch.object(web_chat, "SUP_DIR", self.dir / "sup"),
                  mock.patch("supervisor.core.SUP_DIR", self.dir / "sup"),
                  mock.patch.object(web_chat, "_api_conn", lambda timeout: http.client.HTTPConnection("127.0.0.1", port,
                                                                                                     timeout=timeout)),
                  mock.patch("supervisor.core.secrets_env", return_value={"SHAFT_API_KEY": KEY}),
                  mock.patch.object(messages, "INSTANCES", self.dir / "instances"),
                  mock.patch("supervisor.docs.instance_root", lambda n: self.dir / "instances" / n),   # never the real
                  mock.patch("supervisor.webpush.notify"),                  # never a real push from a test
                  mock.patch.object(web_chat, "log"), mock.patch.object(messages, "log")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.end_relays)             # registered last, so run first: while the patches still hold

    def end_relays(self):
        """A reply still running when the test ends is stopped and waited for while the patches still hold. Left
        running, it read its chat from the SUP_DIR the test had restored, and died on None with a TypeError that
        looked like the cause of the gate failures of 2026-10-19/20 (HoldTest); it was only their echo."""
        left = list(web_chat._relays.items())
        for _, relay in left:
            relay.stop()
        end = time.monotonic() + 10
        while any(web_chat._relays.get(cid) is relay for cid, relay in left) and time.monotonic() < end:
            time.sleep(0.05)

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
        chat, said = say("hello <b>", [])
        self.assertEqual(said, "sent to wisp")
        c = self.wait_idle(chat["id"])                          # nobody polls: the server keeps it going
        self.assertEqual([t["role"] for t in c["turns"]], ["user", "assistant"])
        self.assertEqual(c["turns"][1]["content"], "Answer 1.")
        self.assertEqual(c["turns"][1]["activity"], ["🔧 read_file notes.md"])
        path, headers, body = self.api.chats()[0]
        self.assertEqual(headers["X-Shaft-Conversation"], chat["id"])
        self.assertEqual((headers["Authorization"], headers["Tailscale-User-Login"]), (f"Bearer {KEY}", ME))
        self.assertEqual(body, {"model": "wisp", "messages": [
            {"role": "user", "content": "hello <b>\n\n" + web_chat.DOCS_NOTE}], "stream": True})
        self.assertNotIn(KEY, (self.dir / "sup" / "web" / "chats" / f"{chat['id']}.json").read_text())

    def test_the_next_message_carries_the_conversation_so_far(self):
        chat, _ = say("one", [])
        self.wait_idle(chat["id"])
        say("two", [])
        self.wait_idle(chat["id"])
        self.assertEqual(self.api.chats()[1][2]["messages"], [{"role": "user", "content": "one\n\n" + web_chat.DOCS_NOTE},
                                                             {"role": "assistant", "content": "Answer 1."},
                                                             {"role": "user", "content": "two"}])

    def test_two_chats_that_open_alike_are_two_conversations(self):
        a, _ = say("hi", [])
        self.wait_idle(a["id"])
        web_chat.end(a["id"])
        b, _ = say("hi", [])
        self.wait_idle(b["id"])
        self.assertNotEqual(self.api.chats()[0][1]["X-Shaft-Conversation"], self.api.chats()[1][1]["X-Shaft-Conversation"])

    def test_a_message_during_the_reply_is_an_interjection_and_one_heard_too_late_gets_its_own_reply(self):
        self.api.hold.set()
        chat, _ = say("start", [])
        time.sleep(0.3)
        _, said = say("and also this", [])
        self.assertEqual(said, "wisp reads it at its next step")
        path, headers, body = [r for r in self.api.requests if r[0] == "/v1/chat/interject"][0]
        self.assertEqual(headers["X-Shaft-Conversation"], chat["id"])
        self.assertEqual(body["messages"][-1], {"role": "user", "content": "and also this"})
        self.api.hold.clear()
        c = self.wait_idle(chat["id"], 15)                       # heard 0 of 1: answered by a reply of its own
        self.assertEqual(len(self.api.chats()), 2)
        self.assertEqual([t["role"] for t in c["turns"]], ["user", "user", "assistant", "assistant"])
        self.assertTrue(c["turns"][1]["interjected"])
        # Bug 2026-11-23: the follow-up sorted the unheard message (sent before the answer ended) ahead of the answer,
        # so the request ended with the assistant and the API refused it ("messages must end with a user message")
        followup = self.api.chats()[1][2]["messages"]
        self.assertEqual(followup[-1], {"role": "user", "content": "and also this"})
        self.assertEqual(followup[-2], {"role": "assistant", "content": "Answer 2."})
        self.assertEqual(c["turns"][-1]["note"], "")
        self.assertEqual(c["turns"][-1]["content"], "Answer 3.")     # the fake numbers by requests: chat, interject

    def test_an_unheard_message_goes_after_the_answer_even_when_sent_seconds_before_it_ended(self):
        """Live 2026-11-23: message at 08:54:31, the reply that did not hear it ended 08:54:39; its follow-up sorted by
        time ended with the answer and failed. The follow-up answers it, so it reads it last."""
        self.api.hold.set()
        chat, _ = say("start", [])
        time.sleep(0.3)
        say("and also this", [])
        with web_chat._lock(chat["id"]):
            c = web_chat.load(chat["id"])
            c["turns"][0]["ts"], c["turns"][1]["ts"] = "2026-11-23T08:53:52", "2026-11-23T08:54:31"
            web_chat.save(c)
        self.api.hold.clear()
        c = self.wait_idle(chat["id"], 15)
        followup = self.api.chats()[1][2]["messages"]
        self.assertEqual([m["role"] for m in followup[-3:]], ["user", "assistant", "user"])
        self.assertEqual(followup[-1]["content"], "and also this")
        self.assertEqual(c["turns"][-1]["note"], "")
        # read_thread reads it in the same order, each line keeping its own time
        self.assertEqual([(ts[11:], role) for ts, role, _ in web_chat.dialogue(c)][:3],
                         [("08:53:52", "user"), (c["turns"][2]["ts"][11:], "assistant"), ("08:54:31", "user")])

    def test_an_interjection_heard_in_time_needs_no_second_reply(self):
        self.api.hold.set()
        self.api.heard = 1
        chat, _ = say("start", [])
        time.sleep(0.3)
        say("also", [])
        self.api.hold.clear()
        self.wait_idle(chat["id"], 15)
        self.assertEqual(len(self.api.chats()), 1)

    def test_an_interjection_too_late_for_the_api_is_a_new_message(self):
        self.api.hold.set()
        self.api.interject_status = 409
        chat, _ = say("start", [])
        time.sleep(0.3)
        say("late", [])                    # while it still replies: the API refuses it (409)
        self.api.hold.clear()
        c = self.wait_idle(chat["id"], 15)
        self.assertEqual(c["turns"][-1]["role"], "assistant")
        self.assertIn({"role": "user", "content": "late"}, self.api.chats()[-1][2]["messages"])

    # -- issue #16: three edge cases of the unheard-message follow-up ---------------------------------------------------
    def clock(self, at: str) -> None:
        """web_chat's wall clock, set by the test: every turn written from now on is stamped `at`."""
        self._clock = at
        if not getattr(self, "_clocked", False):
            self._clocked = True
            p = mock.patch.object(web_chat, "_now", lambda: self._clock)
            p.start()
            self.addCleanup(p.stop)

    def interject_late(self, start: str, late: str) -> dict:
        """`start` begins a held reply; `late` is sent during it (202) and heard by none of it (heard 0)."""
        self.api.hold.set()
        chat, _ = say(start, [])
        time.sleep(0.3)
        say(late, [])
        return chat

    def test_issue_16_a_clock_rolled_back_mid_thread_keeps_the_follow_up_in_order(self):
        """#16 case 1. The wall clock went back an hour between the first answer and the next message: sorted by time,
        the thread's turns put the first exchange last, so the follow-up ended with an assistant turn and the API
        refused it ("messages must end with a user message"). The thread's own turns keep the order they were
        written in; time only places what was routed in among them. Reverting _dialogue's merge fails this."""
        self.clock("2026-11-23T01:50:00")
        chat, _ = say("first", [])
        self.wait_idle(chat["id"])
        self.clock("2026-11-23T00:59:00")                    # rolled back
        self.interject_late("second", "late")
        self.clock("2026-11-23T00:59:40")
        self.api.hold.clear()
        c = self.wait_idle(chat["id"], 15)
        self.assertEqual(c["turns"][-1]["note"], "", "the follow-up failed")
        followup = self.api.chats()[-1][2]["messages"]
        self.assertEqual([(m["role"], m["content"].split("\n")[0]) for m in followup],
                         [("user", "first"), ("assistant", "Answer 1."), ("user", "second"),
                          ("assistant", c["turns"][4]["content"]), ("user", "late")])
        # the page shows them as written too: sorted by time, it drew "second" and its answers above "first" (live check)
        self.assertEqual([i["k"] for i in web_chat.feed("wisp", chat["id"])["items"]], [0, 1, 2, 3, 4, 5])

    def test_issue_16_a_clock_rolled_back_during_the_reply_still_puts_the_late_message_last(self):
        """#16 case 1, the other side: the clock went back while the reply ran, so its answer is stamped before every
        earlier turn. The late message still follows the answer it missed, and the follow-up ends with it."""
        self.clock("2026-11-23T01:50:00")
        chat, _ = say("first", [])
        self.wait_idle(chat["id"])
        self.clock("2026-11-23T01:58:30")
        self.interject_late("second", "late")
        self.clock("2026-11-23T01:00:00")                    # rolled back before the answer
        self.api.hold.clear()
        c = self.wait_idle(chat["id"], 15)
        followup = self.api.chats()[-1][2]["messages"]
        self.assertEqual([m["content"].split("\n")[0] for m in followup],
                         ["first", "Answer 1.", "second", c["turns"][4]["content"], "late"])
        self.assertEqual(c["turns"][-1]["note"], "")

    def test_issue_16_retry_after_a_failed_follow_up_sends_the_late_message_once(self):
        """#16 case 2. The follow-up that answers an unheard message failed before it was posted (connection refused):
        Retry offered the late message, but mark_retried only folded the turn before the failure (the first answer),
        so the late message went out twice. A failed reply remembers the turns it answered and Retry folds those."""
        refused = socket.socket()
        refused.bind(("127.0.0.1", 0))
        dead = refused.getsockname()[1]
        refused.close()                                       # nothing listens there: ConnectionRefusedError
        real, calls = web_chat._api_conn, []

        def conn(timeout):
            calls.append(timeout)
            if len(calls) == 3:                               # the reply, the interjection, then the follow-up
                return http.client.HTTPConnection("127.0.0.1", dead, timeout=timeout)
            return real(timeout)

        with mock.patch.object(web_chat, "_api_conn", conn):
            chat = self.interject_late("start", "late")
            self.api.hold.clear()
            c = self.wait_idle(chat["id"], 15)
        self.assertEqual(c["turns"][-1]["note_kind"], "connection")
        retry = web_chat.feed("wisp", chat["id"])["items"][-1]["failure"]["retry"]
        self.assertEqual((retry["text"], retry["confirm"]), ("late", None))
        say(retry["text"], retry_of=str(retry["retry_of"]), cid=chat["id"])
        c = self.wait_idle(chat["id"], 15)
        self.assertEqual(c["turns"][-1]["note"], "")
        sent = self.api.chats()[-1][2]["messages"]
        self.assertEqual([m["content"] for m in sent].count("late"), 1, sent)
        self.assertEqual(sent[-1], {"role": "user", "content": "late"})

    def test_issue_16_a_refused_interjection_then_a_heard_one_answers_the_refused_one_next(self):
        """#16 case 3. One interjection was refused (409: the API had not registered the reply yet), the next taken and
        heard (heard 1). Both were counted alike, so the heard one was taken as unheard and the refused one as heard:
        the follow-up answered the wrong message. Only accepted interjections take the heard count."""
        self.api.hold.set()
        self.api.heard = 1
        chat, _ = say("start", [])
        time.sleep(0.3)
        self.api.interject_status = 409
        _, said = say("refused one", [])
        self.assertEqual(said, "wisp was just finishing: it answers this next")
        self.api.interject_status = 202
        say("heard one", [])
        self.api.hold.clear()
        c = self.wait_idle(chat["id"], 15)
        self.assertEqual(len(self.api.chats()), 2)
        followup = [m["content"] for m in self.api.chats()[1][2]["messages"]]
        self.assertEqual(followup[-1], "refused one")
        self.assertLess(followup.index("heard one"), followup.index(c["turns"][3]["content"]))
        self.assertEqual(c["turns"][-1]["note"], "")

    # Codex review of #16: what the fix had to get right around it
    def failed_follow_up(self, *late: dict) -> dict:
        """A thread whose follow-up for `late` (user turns sent during the first reply) failed before it was posted."""
        chat = topic()
        turns = [{"role": "user", "content": "start", "shown": "start", "ts": "2026-11-25T10:00:00"},
                 *[{"role": "user", "content": t["shown"], "interjected": True, "after": len(late) + 1,
                    "ts": "2026-11-25T10:00:05", **t} for t in late],
                 {"role": "assistant", "content": "Answer.", "ts": "2026-11-25T10:00:09"}]
        chat.update(turns=turns, state="replying", reply={"interjections": 0, "followup": len(late),
                                                           "asked": list(range(1, len(late) + 1)),
                                                           "turn_start": len(turns)})
        web_chat.save(chat)
        relay = web_chat.Relay(chat["id"])
        web_chat._finish(relay, "", [], "ConnectionRefusedError: refused", None, posted=False,
                         exc=ConnectionRefusedError("refused"))
        return web_chat.load(chat["id"])

    def test_issue_16_a_follow_up_for_two_messages_retries_them_as_one_and_folds_both(self):
        c = self.failed_follow_up({"shown": "a"}, {"shown": "b"})
        self.assertEqual((c["turns"][-1]["retry_text"], c["turns"][-1]["asked"]), ("a\n\nb", [1, 2]))
        say("a\n\nb", retry_of="4", cid=c["id"])
        c = self.wait_idle(c["id"])
        self.assertEqual([m["content"] for m in self.api.chats()[-1][2]["messages"]], ["start", "Answer.", "a\n\nb"])

    def test_issue_16_a_retry_too_long_or_with_files_is_not_offered_in_one_tap(self):
        """Joined, two long messages passed MAX_TEXT: the fold was saved, the send refused, and both left the thread.
        Files: Retry sends text only, so the folded file would have been lost."""
        half = "x" * (web_chat.MAX_TEXT // 2)
        for late in (({"shown": half}, {"shown": half}),
                     ({"shown": "read this", "attachments": [{"name": "a.md", "path": "p", "bytes": 1}]}, {"shown": "b"})):
            with self.subTest(late=late[0]["shown"][:9]):
                c = self.failed_follow_up(*late)
                self.assertEqual(c["turns"][-1]["retry_text"], "")
                self.assertFalse(c["turns"][-1]["note_retry"])
                self.assertFalse(web_chat.mark_retried(c, 4, late[0]["shown"]))
                web_chat.clear("wisp", c["id"])

    def test_issue_16_a_refused_send_after_retry_unfolds_what_it_folded(self):
        c = self.failed_follow_up({"shown": "late"})
        with mock.patch.object(web_chat, "_send", side_effect=web_chat.Refused("no")):
            with self.assertRaises(web_chat.Refused):
                say("late", retry_of="3", cid=c["id"])
        self.assertFalse(any(t.get("retried") for t in web_chat.load(c["id"])["turns"]))

    def test_issue_16_a_restart_offers_every_message_the_reply_answered(self):
        chat = topic()
        chat.update(state="replying", reply={"interjections": 0, "asked": [2, 3], "turn_start": 4}, turns=[
            {"role": "user", "content": "start", "shown": "start", "ts": "2026-11-25T10:00:00"},
            {"role": "assistant", "content": "Answer.", "ts": "2026-11-25T10:00:09"},
            {"role": "user", "content": "a", "shown": "a", "ts": "2026-11-25T10:00:05", "after": 1},
            {"role": "user", "content": "b", "shown": "b", "ts": "2026-11-25T10:00:06", "after": 1}])
        web_chat.save(chat)
        web_chat.reap()
        self.assertEqual(web_chat.load(chat["id"])["turns"][-1]["retry_text"], "a\n\nb")

    def test_issue_16_a_refused_message_already_in_the_request_is_not_answered_again(self):
        """A send that reached the chat before the relay read it for its request was refused (409: the reply was not
        registered yet) but went out in that request: it is answered, not sent again in a follow-up."""
        chat = topic()
        chat.update(state="replying", reply={"interjections": 1, "turn_start": 1}, turns=[
            {"role": "user", "content": "start", "shown": "start", "ts": "2026-11-25T10:00:00"},
            {"role": "user", "content": "early", "shown": "early", "interjected": True, "refused": True,
             "ts": "2026-11-25T10:00:00"}])
        web_chat.save(chat)
        relay = web_chat.Relay(chat["id"])
        relay.upto = 2
        web_chat._finish(relay, "Answer.", [], None, None)
        c = web_chat.load(chat["id"])
        self.assertEqual((c["state"], "after" in c["turns"][1]), ("idle", False))

    def test_stop_ends_the_reply_and_the_api_sees_its_client_go(self):
        self.api.hold.set()
        chat, _ = say("long one", [])
        time.sleep(0.3)
        self.assertEqual(web_chat.stop(chat["id"]), "stopping the reply")
        c = self.wait_idle(chat["id"])
        self.assertEqual(c["turns"][-1]["note"], "stopped by you")
        self.assertTrue(self.api.gone.wait(5))
        with self.assertRaisesRegex(web_chat.Refused, "no reply is running"):
            web_chat.stop(chat["id"])

    def test_end_writes_it_up_through_the_api_and_the_next_message_starts_a_new_chat(self):
        chat, _ = say("hi", [])
        self.wait_idle(chat["id"])
        self.assertIn("journal write-up requested", web_chat.end(chat["id"]))
        path, headers, body = [r for r in self.api.requests if r[0] == "/v1/chat/end"][0]
        self.assertEqual((headers["X-Shaft-Conversation"], body), (chat["id"], {"model": "wisp"}))
        self.assertEqual(web_chat.load(chat["id"])["state"], "ended")
        nxt, _ = say("again", [])
        self.assertNotEqual(nxt["id"], chat["id"])
        self.wait_idle(nxt["id"])

    def test_end_refused_while_replying_and_already_written_up_is_ended(self):
        self.api.hold.set()
        chat, _ = say("x", [])
        time.sleep(0.2)
        with self.assertRaisesRegex(web_chat.Refused, "still replying"):
            web_chat.end(chat["id"])
        self.api.hold.clear()
        self.wait_idle(chat["id"], 15)
        self.api.end_status = 404                                 # the API's idle write-up came first
        self.assertEqual(web_chat.end(chat["id"]), "ended")

    def test_a_relay_the_web_server_lost_is_marked_interrupted(self):
        chat = web_chat.new_thread("wisp", ME)
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
        chat, _ = say("hi", [])
        self.wait_idle(chat["id"])
        self.api.end_status, self.api.end_type = 404, "invalid_request_error"
        with self.assertRaisesRegex(web_chat.Refused, "older than the web UI"):
            web_chat.end(chat["id"])
        self.assertNotEqual(web_chat.load(chat["id"])["state"], "ended")

    def test_a_stream_that_does_not_say_what_it_heard_gets_no_second_reply(self):
        self.api.hold.set()
        self.api.heard = None
        chat, _ = say("start", [])
        time.sleep(0.3)
        say("also", [])
        self.api.hold.clear()
        self.wait_idle(chat["id"], 15)
        self.assertEqual(len(self.api.chats()), 1)


class HoldTest(ChatTest):
    """The gate reverted tundra's changes eight times (2026-10-19/20) on these 8 tests, never outside a cycle: the
    fake API's long reply was 100 sleeps of 50 ms, and a cycle (so its gate) runs at launchd's utility QoS — priority
    20, as `taskpolicy -c utility` gives — where macOS timer coalescing makes those 100 sleeps last 11.8 s instead of
    5.3 s (measured). With a live cycle's load on top, the reply outlasted wait_idle's 15 s."""

    def test_a_held_reply_ends_when_released_however_slow_the_timers(self):
        self.api.pause = lambda s: time.sleep(3 * s)        # a 50 ms sleep at utility QoS under load: 150 ms
        self.api.hold.set()
        chat, _ = say("long one", [])
        time.sleep(0.3)
        self.assertEqual(web_chat.load(chat["id"])["state"], "replying")    # held: still going
        self.api.hold.clear()
        released = time.monotonic()
        c = self.wait_idle(chat["id"], 15)
        self.assertLess(time.monotonic() - released, 3)       # the old 100 ticks: ~14 s more
        self.assertEqual(c["turns"][-1]["content"], "Answer 1.")

    def test_a_reply_left_running_by_a_test_is_stopped_before_its_patches_go(self):
        self.api.hold.set()
        chat, _ = say("never released", [])
        time.sleep(0.3)
        self.end_relays()
        self.assertFalse(web_chat.replying(chat["id"]))
        self.assertEqual(web_chat.load(chat["id"])["turns"][-1]["note"], "stopped by you")


class FilesTest(ChatTest):
    def test_files_go_in_as_telegrams_do(self):
        chat, _ = say("look", [("photo.png", PNG), ("notes.md", b"# x")])
        c = self.wait_idle(chat["id"])
        turn = c["turns"][0]
        self.assertEqual(turn["shown"], "look")
        self.assertIn("[The operator attached, saved in my folder:]", turn["content"])
        self.assertIn("attachments/from-operator/", turn["content"])
        saved = self.dir / "instances" / "wisp" / Path(turn["content"].split("- ")[1].split(" (")[0])
        self.assertEqual(saved.read_bytes(), PNG)
        self.assertEqual([a["name"] for a in turn["attachments"]], ["photo.png", "notes.md"])
        self.assertEqual(self.api.chats()[0][2]["messages"][0]["content"], turn["content"])

    def test_a_file_too_big_is_refused_before_anything_is_written_or_sent(self):
        with mock.patch.object(messages, "ATTACH_MAX_BYTES", 10):
            with self.assertRaisesRegex(web_chat.Refused, "larger than"):
                say("big", [("photo.png", PNG)])
        self.assertFalse((self.dir / "instances" / "wisp" / "attachments").exists())
        self.assertEqual(self.api.requests, [])

    def test_an_empty_message_is_not_sent(self):
        with self.assertRaisesRegex(web_chat.Refused, "empty"):
            say("  ", [])

    def test_multipart_bodies_are_read(self):
        boundary = "XyZ"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhi there\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"a.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + PNG + f"\r\n--{boundary}--\r\n".encode()
        fields, files = web_chat.parse_multipart(f"multipart/form-data; boundary={boundary}", body)
        self.assertEqual(fields, {"text": "hi there"})
        self.assertEqual(files, [("a.png", PNG)])


class ChatRoutesCase(WebTest):
    """The web chat's routes over a temporary instance folder: the harness only, with no test of its own, so the many
    classes built on it (here, test_web_threads) do not each run a base class's tests again. Importing a class that
    has tests into another module runs them twice (the release gate's "run twice", v0.49.0)."""

    def setUp(self):
        super().setUp()
        self.inst = Path(tempfile.mkdtemp(prefix="shaft-chat-inst-")).resolve()
        self.addCleanup(shutil.rmtree, self.inst, True)
        (self.inst / "wisp").mkdir()
        for p in (mock.patch.object(web, "_names", return_value=["wisp"]),
                  mock.patch.object(web_chat, "SUP_DIR", self.sup), mock.patch.object(web, "_sent_during", return_value=[]),
                  mock.patch("supervisor.core.INSTANCES", self.inst)):
            p.start()
            self.addCleanup(p.stop)
        self.cookie = self.paired()

    def main_with(self, turns: list[dict], state: str = "idle") -> dict:
        """Main, the instance's own conversation (what its page and its rail row show), holding `turns`."""
        chat = web_chat.from_chat("wisp", ME, create=True)
        chat.update(turns=turns, state=state)
        web_chat.save(chat)
        return chat

    def chat_with(self, turns: list[dict], state: str = "idle") -> dict:
        chat = web_chat.new_thread("wisp", ME)
        chat.update(turns=turns, state=state)
        web_chat.save(chat)
        return chat


class ChatRoutesTest(ChatRoutesCase):
    def test_the_send_route_takes_multipart_with_the_page_token_only(self):
        body, ctype = b"--B\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhi\r\n--B--\r\n", "multipart/form-data; boundary=B"
        with mock.patch.object(web_chat, "send", return_value=({}, "sent to wisp")) as send:
            ok = self.req("/chat/wisp/send", "POST", cookie=self.cookie, body=body,
                          headers={"content-type": ctype, "sec-fetch-site": "same-origin",
                                   "x-csrf-token": self.csrf(self.cookie)})
            no = self.req("/chat/wisp/send", "POST", cookie=self.cookie, body=body,
                          headers={"content-type": ctype, "sec-fetch-site": "same-origin"})
        self.assertEqual((ok.status, no.status), (200, 403))
        # retry_of: Retry in place (P9); voice: typed unless the form says voice=1
        send.assert_called_once_with("wisp", ME, "hi", [], mid="", retry_of="", voice=False)

    def test_issue_70_queue_routes_require_csrf(self):
        """Reverting #70 bypasses queuing and leaves cancellation unavailable to the paired page."""
        c = self.chat_with([{"role": "user", "content": "start", "ts": "2026-10-20T12:00:00"}], "replying")
        c["reply"] = {"interjections": 0}
        web_chat.save(c)
        post = mock.patch.object(web_chat, "_post", return_value=(202, {}))
        post.start()
        self.addCleanup(post.stop)
        body = b'--B\r\nContent-Disposition: form-data; name="text"\r\n\r\nnext\r\n' \
               b'--B\r\nContent-Disposition: form-data; name="queue"\r\n\r\n1\r\n' \
               b'--B\r\nContent-Disposition: form-data; name="mid"\r\n\r\nq1\r\n' \
               + f'--B\r\nContent-Disposition: form-data; name="cid"\r\n\r\n{c["id"]}\r\n--B--\r\n'.encode()
        headers = {"content-type": "multipart/form-data; boundary=B", "sec-fetch-site": "same-origin"}
        self.assertEqual(self.req("/chat/wisp/send", "POST", cookie=self.cookie, body=body, headers=headers).status, 403)
        headers["x-csrf-token"] = self.csrf(self.cookie)
        self.assertEqual(self.req("/chat/wisp/send", "POST", cookie=self.cookie, body=body, headers=headers).status, 200)
        self.assertEqual(web_chat.load(c["id"])["queued"][0]["shown"], "next")
        headers = {"sec-fetch-site": "same-origin"}
        unq = f"mid=q1&cid={c['id']}".encode()
        self.assertEqual(self.req("/chat/wisp/unqueue", "POST", cookie=self.cookie, body=unq, headers=headers).status, 403)
        self.assertEqual(len(web_chat.load(c["id"])["queued"]), 1)
        headers["x-csrf-token"] = self.csrf(self.cookie)
        self.assertEqual(self.req("/chat/wisp/unqueue", "POST", cookie=self.cookie, body=unq, headers=headers).status, 200)
        self.assertEqual(web_chat.load(c["id"])["queued"], [])

    def test_the_page_shows_the_conversation_escaped_and_polls_it(self):
        c = self.chat_with([{"role": "user", "content": "<script>x</script>", "shown": "<script>x</script>", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "**bold** <img src=x onerror=y>", "ts": "2026-10-19T12:00:05",
                         "activity": ["🔧 web_search <i>"]}])
        body = self.req(f"/i/wisp/chat?id={c['id']}", cookie=self.cookie).body.decode()   # a thread's own page
        self.assertIn(f'data-poll="/fragment/chat/wisp?id={c["id"]}"', body)
        self.assertIn('data-multipart="1"', body)
        self.assertIn("<b>bold</b>", body)
        self.assertNotIn("<script>x", body)
        self.assertNotIn("<img src=x", body)
        self.assertIn("🔧 web_search &lt;i&gt;", body)
        self.assertIn('data-post="/action/chat-end"', body)

    def test_a_running_reply_shows_its_activity_and_stop(self):
        chat = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"}], "replying")
        chat["reply"] = {"started": "2026-10-19T12:00:01", "activity": ["waiting: wisp is in a cycle"], "answer": ""}
        web_chat.save(chat)
        body = self.req(f"/fragment/chat/wisp?id={chat['id']}", cookie=self.cookie).body.decode()
        self.assertIn(f'data-poll="/fragment/chat/wisp/live?id={chat["id"]}"', body)             # W12: its activity, polled on its own
        self.assertIn("waiting: wisp is in a cycle",
                      self.req(f"/fragment/chat/wisp/live?id={chat['id']}", cookie=self.cookie).body.decode())
        self.assertIn('data-post="/action/chat-stop"', body)

    def test_chat_files_follow_the_attachment_rules(self):
        d = self.inst / "wisp" / "attachments" / "from-operator" / "t"
        d.mkdir(parents=True)
        (d / "p.png").write_bytes(PNG)
        (d / "x.svg").write_text("<svg onload=alert(1)>")
        chat = self.chat_with([{"role": "user", "content": "x", "ts": "2026-10-19T12:00:00", "attachments": [
            {"name": "p.png", "path": "instances/wisp/attachments/from-operator/t/p.png", "bytes": 72},
            {"name": "x.svg", "path": "instances/wisp/attachments/from-operator/t/x.svg", "bytes": 20},
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


class ChatCentricTest(ChatRoutesCase):
    """The chat is the front page, a live rail lists every chat, the instance's pages sit behind ⋯."""

    def test_the_front_page_is_the_chat_last_opened_else_the_first(self):
        r = self.req("/", cookie=self.cookie)
        self.assertEqual((r.status, r.headers["Location"]), (302, "/i/wisp/chat"))
        r = self.req("/", cookie=self.cookie + "; shaft_last=wisp")
        self.assertEqual(r.headers["Location"], "/i/wisp/chat")
        r = self.req("/", cookie=self.cookie + "; shaft_last=gone")          # an instance that is no more: the first
        self.assertEqual(r.headers["Location"], "/i/wisp/chat")
        with mock.patch.object(web, "_names", return_value=[]):
            self.assertEqual(self.req("/", cookie=self.cookie).headers["Location"], "/overview")
        self.assertEqual(self.req("/overview", cookie=self.cookie).status, 200)

    def test_opening_a_chat_remembers_it_for_the_front_page(self):
        r = self.req("/i/wisp/chat", cookie=self.cookie)
        cookies = r.headers["Set-Cookie"]
        last = next(c for c in ([cookies] if isinstance(cookies, str) else cookies) if c.startswith("shaft_last="))
        self.assertIn("shaft_last=wisp;", last)
        self.assertIn("SameSite=Strict", last)

    def test_the_rail_shows_state_last_line_and_when_it_last_answered(self):
        self.main_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "> quoted\n<b>first</b> line\nsecond", "ts": "2026-10-19T12:00:05"}])
        with mock.patch.object(web, "overview", return_value=snap(state="in a cycle")):
            body = self.req("/fragment/rail?c=i:wisp&t=from", cookie=self.cookie).body.decode()
        self.assertIn('class="inst on"', body)
        self.assertIn('data-last="2026-10-19T12:00:05"', body)
        # M2b restores the instance lifecycle dot even while Main is idle.
        self.assertIn('<span class="dot st-cycle" title="in a cycle" aria-hidden="true"></span>', body)
        self.assertIn("&lt;b&gt;first&lt;/b&gt; line", body)                  # escaped, and not the quoted line
        self.assertNotIn("quoted", body)
        self.assertIn('href="/overview"', body)

    def test_the_rail_says_main_is_replying_and_counts_alerts(self):
        self.main_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"}], "replying")
        s = snap()
        s["alerts"] = [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}]
        with mock.patch.object(web, "overview", return_value=s):
            body = self.req("/fragment/rail", cookie=self.cookie).body.decode()
        self.assertIn('<span class="last" aria-hidden="true">replying…</span>', body)
        # Main replying takes precedence over the idle lifecycle slot.
        self.assertIn('<span class="tdot t-replying" title="replying" aria-hidden="true">', body)
        self.assertIn('data-last=""', body)                                    # it has not answered yet
        self.assertIn('<span class="badge bad" title="alerts">2</span>', body)

    def test_the_chat_page_keeps_the_instance_pages_behind_its_menu(self):
        body = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        import re
        headings = re.findall(r"<h1\b[^>]*>(.*?)</h1>", body)
        # M2b opens a conversation picker here; the sidebar retains its own button.
        self.assertEqual(headings, ['<button type="button" data-switch-conversation aria-haspopup="true" aria-expanded="false" title="Switch conversation"><span>wisp</span>' + str(web_pages.icon("back", 12)) + '</button>'])
        self.assertIn("cloud mimo-v2.6-pro · idle · $0.12 today", body)
        self.assertNotIn('class="tabs"', body)
        for page in ("/i/wisp", "/i/wisp/config", "/i/wisp/drive", "/i/wisp/access", "/i/wisp/judge",
                     "/i/wisp/changes", "/i/wisp/benchmarks", "/i/wisp/transcripts", "/i/wisp/journal",
                     "/i/wisp/activity"):
            self.assertIn(f'href="{page}"', body, page)
        self.assertIn('data-post="/action/cycle"', body)                       # its actions too
        self.assertIn('data-poll="/fragment/rail?c=i:wisp&amp;t=from"', body)  # the instance's own page: Main

    def test_an_ended_thread_is_in_earlier_chats_and_the_instance_page_keeps_a_composer(self):
        """It showed "No chat open" over an empty box, though the conversation had just ended. Since 2026-11-11 the
        instance's page is Main, which never ends: a thread that ended is one tap away in Earlier chats."""
        old = self.chat_with([{"role": "user", "content": "earlier question", "ts": "2026-10-19T12:00:00"},
                              {"role": "assistant", "content": "earlier answer", "ts": "2026-10-19T12:00:05"}], "ended")
        old["ended"] = "2026-10-19T12:30:00"
        web_chat.save(old)
        body = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        self.assertNotIn("earlier answer", body)
        self.assertIn('data-post="/chat/wisp/send"', body)
        # #8: in the ⋯ menu, with the instance's pages (the header keeps three controls)
        self.assertRegex(body, r'data-key="chat-menu">.*<details class="earliermenu" data-key="earlier-chats"><summary '
                               r'data-focus="earlier"><svg[^>]*>.*?</svg> Earlier chats</summary>')
        self.assertIn(f"/i/wisp/chat?id={old['id']}", body)
        past = self.req(f"/i/wisp/chat?id={old['id']}", cookie=self.cookie).body.decode()
        self.assertIn("earlier answer", past)
        self.assertIn("This conversation ended on", past)

    def test_the_chat_page_carries_what_its_script_needs(self):
        """Copy an answer as written, the reply's clock, a draft per instance, drop anywhere."""
        chat = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                               {"role": "assistant", "content": '**a** "<x>"', "ts": "2026-10-19T12:00:05"},
                               {"role": "user", "content": "q2", "ts": "2026-10-19T12:01:00"}], "replying")
        chat["reply"] = {"started": "2026-10-19T12:01:01", "activity": [], "answer": ""}
        web_chat.save(chat)
        body = self.req(f"/i/wisp/chat?id={chat['id']}", cookie=self.cookie).body.decode()
        self.assertIn('data-raw="**a** &quot;&lt;x&gt;&quot;"', body)              # the Markdown, escaped once
        self.assertIn('data-copy', body)
        self.assertIn('<span class="elapsed" data-since="2026-10-19T12:01:01">', body)
        self.assertIn(f'data-draft="wisp/{chat["id"]}"', body)                    # its own draft
        self.assertIn('data-dropzone hidden', body)
        self.assertIn('data-newbelow hidden', body)
        self.assertIn("data-notify", body)

    def test_a_past_chat_read_only_takes_no_drop(self):
        old = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"}], "ended")
        body = self.req(f"/i/wisp/chat?id={old['id']}", cookie=self.cookie).body.decode()
        self.assertNotIn("data-dropzone", body)
        self.assertNotIn('data-post="/chat/wisp/send"', body)

    def test_previews_of_picked_files_are_allowed_and_nothing_else_is(self):
        csp = self.req("/i/wisp/chat", cookie=self.cookie).headers["Content-Security-Policy"]
        self.assertIn("img-src 'self' data: blob:;", csp)
        self.assertIn("script-src 'self';", csp)


class DocsChatCase(ChatTest):
    """A chat with documents to tell the instance about. DocEditsTest and ClearTest subclassed DocsInChatTest for this
    setUp, so each of its tests ran three times."""

    def setUp(self):
        super().setUp()
        self.docs = self.dir / "instances" / "wisp" / "notes" / "docs"
        from supervisor import docs
        p = mock.patch.object(docs, "instance_root", lambda n: self.dir / "instances" / n)
        p.start()
        self.addCleanup(p.stop)


class DocsInChatTest(DocsChatCase):
    """The instance is told about documents once per chat; a reply's documents get cards."""

    def test_only_the_first_message_carries_the_note_and_he_never_sees_it(self):
        chat, _ = say("one", [])
        self.wait_idle(chat["id"])
        say("two", [])
        c = self.wait_idle(chat["id"])
        self.assertIn(web_chat.DOCS_NOTE, c["turns"][0]["content"])
        self.assertEqual(c["turns"][0]["shown"], "one")
        self.assertNotIn("Web chat:", c["turns"][2]["content"])
        from supervisor import web_views
        self.assertNotIn("Web chat:", web_views.chat_feed("wisp", c, []))

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
            chat, _ = say("write the plan", [])
            c = self.wait_idle(chat["id"])
        self.assertEqual(c["turns"][1]["docs"], [{"path": "new.md", "created": True},
                                                 {"path": "plan.md", "created": False}])
        from supervisor import web_views
        feed = web_views.chat_feed("wisp", c, [])
        self.assertIn('data-open-doc="plan.md"', feed)
        self.assertIn("📄 new.md <span class=\"dim\">— created</span>", feed)
        self.assertEqual(web_views.in_chat(c), {"new.md", "plan.md"})

    def test_a_reply_goes_on_when_the_documents_cannot_be_read(self):
        """It claimed "never fails a reply" but caught Exception only: instance_root's SystemExit failed every send
        (found by the full suite once the worktree had no instances folder)."""
        def gone(name):
            raise SystemExit(f"no instance named {name!r}")
        with mock.patch("supervisor.docs.instance_root", side_effect=gone):
            chat, said = say("hi", [])
            c = self.wait_idle(chat["id"])
        self.assertEqual(said, "sent to wisp")
        self.assertEqual(c["turns"][1]["content"], "Answer 1.")

    def test_a_reply_that_wrote_none_has_no_cards(self):
        chat, _ = say("hi", [])
        c = self.wait_idle(chat["id"])
        self.assertNotIn("docs", c["turns"][1])


class DocsPanelTest(ChatRoutesCase):
    def setUp(self):
        super().setUp()
        self.docs = self.inst / "wisp" / "notes" / "docs"
        self.docs.mkdir(parents=True)
        from supervisor import docs
        p = mock.patch.object(docs, "instance_root", lambda n: self.inst / n)
        p.start()
        self.addCleanup(p.stop)

    def test_the_panel_lists_documents_and_opens_one_rendered_never_as_markup(self):
        (self.docs / "plan.md").write_text("# Plan\n\n- **step** <script>alert(1)</script>\n")
        (self.docs / "other.md").write_text("x")
        body = self.req("/fragment/docs/wisp?doc=plan.md", cookie=self.cookie).body.decode()
        self.assertNotIn('class="doclist"', body)
        self.assertIn('data-back-docs data-dir="" aria-label="Back to the documents"', body)
        self.assertIn('<div class="docview" data-doc="plan.md"', body)
        self.assertIn("<b>step</b>", body)
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_with_none_asked_it_opens_what_this_chat_changed_and_back_lists(self):
        """Codex's first cut dropped this fallback, so a document the instance writes mid-reply no longer showed live
        until clicked; ← Documents asks for the list explicitly (?list=1) instead."""
        (self.docs / "plan.md").write_text("the plan")
        (self.docs / "notes.md").write_text("not this one")
        self.main_with([                                 # the page's conversation: Main
                       {"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "done", "ts": "2026-10-19T12:00:05",
                         "docs": [{"path": "plan.md", "created": True}]}])
        page = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        self.assertIn('data-poll="/fragment/docs/wisp?doc=plan.md"', page)
        self.assertIn('<div class="docview" data-doc="plan.md"', page)
        self.assertIn("data-back-docs", page)
        fragment = self.req("/fragment/docs/wisp", cookie=self.cookie).body.decode()
        self.assertIn('<div class="docview" data-doc="plan.md"', fragment)
        listed = self.req("/fragment/docs/wisp?list=1", cookie=self.cookie).body.decode()
        self.assertIn('class="doclist"', listed)
        self.assertNotIn('class="docview"', listed)
        self.assertNotIn("data-back-docs", listed)
        self.assertIn('class="dim docsize" title="8 bytes">8 B</span>', listed)
        self.assertIn('<span class="mark" title="changed in this chat">●</span> plan.md', listed)

    def test_subfolders_are_listed_as_folders_and_dir_lists_one(self):
        """Before subfolders were navigable the list was flat: tundra's 11 documents in 4 folders showed as one list of
        ucg/…, riunioni/… paths mixed by date. A folder is now one entry, its documents one tap inside it."""
        (self.docs / "ucg" / "sub").mkdir(parents=True)
        (self.docs / "ucg" / "a.md").write_text("a")
        (self.docs / "ucg" / "sub" / "deep.md").write_text("deep")
        (self.docs / "top.md").write_text("top")
        top = self.req("/fragment/docs/wisp?list=1", cookie=self.cookie).body.decode()
        self.assertIn('<a href="?dir=ucg" data-open-dir="ucg" class="folder"><span>📁 ucg '
                      '<span class="dim">· 2 documents</span></span>'
                      '<span class="dim docsize" title="5 bytes in all">5 B</span><time class="dim docdate"', top)
        self.assertIn('data-open-doc="top.md"', top)
        self.assertNotIn('data-open-doc="ucg/a.md"', top)              # inside its folder, not in the top list
        inside = self.req("/fragment/docs/wisp?list=1&dir=ucg", cookie=self.cookie).body.decode()
        self.assertIn('data-back-docs data-dir="" aria-label="Up to the documents"', inside)
        self.assertIn('<span class="crumbs"><a href="?dir=" data-open-dir="">Documents</a> / <b>ucg</b></span>', inside)
        self.assertIn('<a href="?doc=ucg%2Fa.md" data-open-doc="ucg/a.md"><span>a.md</span>', inside)
        self.assertIn('data-open-dir="ucg/sub" class="folder"><span>📁 sub <span class="dim">· 1 document</span></span>', inside)
        self.assertNotIn('data-open-doc="top.md"', inside)
        self.assertIn('<input type="hidden" name="dir" value="ucg">', inside)   # New document goes in this folder
        deep = self.req("/fragment/docs/wisp?dir=ucg%2Fsub", cookie=self.cookie).body.decode()   # ?dir= alone lists
        self.assertIn('<a href="?dir=ucg" data-open-dir="ucg">ucg</a> / <b>sub</b>', deep)
        self.assertIn('data-back-docs data-dir="ucg" aria-label="Up to ucg/"', deep)
        opened = self.req("/fragment/docs/wisp?doc=ucg%2Fsub%2Fdeep.md", cookie=self.cookie).body.decode()
        self.assertIn('data-back-docs data-dir="ucg/sub" aria-label="Back to ucg/sub/">← sub</button>', opened)

    def test_a_folder_rolls_up_its_documents_marks_and_comments(self):
        (self.docs / "ucg").mkdir()
        (self.docs / "ucg" / "a.md").write_text("a")
        self.main_with([                                 # the page's conversation: Main
                       {"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "done", "ts": "2026-10-19T12:00:05",
                         "docs": [{"path": "ucg/a.md", "created": True}]}])
        from supervisor import doc_comments
        with mock.patch.object(doc_comments, "open_counts", lambda n: {"ucg/a.md": 3}):
            top = self.req("/fragment/docs/wisp?list=1", cookie=self.cookie).body.decode()
        self.assertIn('<span class="mark" title="changed in this chat">●</span> 📁 ucg <span class="dim">· 1 document</span>'
                      ' <span class="ccount" title="open comments">💬 3</span>', top)

    def test_a_bad_or_gone_folder_says_so(self):
        (self.docs / "top.md").write_text("top")
        self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "done", "ts": "2026-10-19T12:00:05",
                         "docs": [{"path": "top.md", "created": True}]}])
        bad = self.req("/fragment/docs/wisp?dir=..%2Fsecrets", cookie=self.cookie)
        self.assertEqual(bad.status, 200)
        self.assertIn('class="bad"', bad.body.decode())
        self.assertIn('data-open-doc="top.md"', bad.body.decode())     # the top list with why, not this chat's doc
        self.assertNotIn('class="docview"', bad.body.decode())
        gone = self.req("/fragment/docs/wisp?list=1&dir=old", cookie=self.cookie).body.decode()
        self.assertIn("No documents in old/ (any more).", gone)
        self.assertIn("data-back-docs", gone)                          # a way back up, and the drawer stays shown

    def test_a_page_with_dir_polls_that_folder(self):
        (self.docs / "ucg").mkdir()
        (self.docs / "ucg" / "a.md").write_text("a")
        page = self.req("/i/wisp/chat?dir=ucg", cookie=self.cookie).body.decode()
        self.assertIn('data-poll="/fragment/docs/wisp?list=1&amp;dir=ucg"', page)
        self.assertIn('data-open-doc="ucg/a.md"', page)

    def test_with_nothing_changed_in_this_chat_it_lists(self):
        (self.docs / "plan.md").write_text("the plan")
        fragment = self.req("/fragment/docs/wisp", cookie=self.cookie).body.decode()
        self.assertIn('class="doclist"', fragment)
        self.assertNotIn('class="docview"', fragment)

    def test_a_path_out_of_the_folder_is_refused_in_the_panel_not_the_page(self):
        r = self.req("/fragment/docs/wisp?doc=../config.env", cookie=self.cookie)
        self.assertEqual(r.status, 200)
        self.assertIn('class="bad"', r.body.decode())
        self.assertIn("a document is a .md file", r.body.decode())
        self.assertEqual(self.req("/i/wisp/chat?doc=../x.md", cookie=self.cookie).status, 200)

    def test_an_unknown_instance_has_no_panel(self):
        self.assertEqual(self.req("/fragment/docs/nobody", cookie=self.cookie).status, 404)


class DocEditsTest(DocsChatCase):
    """What the instance is told of the operator's edits, and when."""

    def test_the_diff_says_what_changed_and_only_that(self):
        self.assertEqual(web_chat.edit_diff("p.md", None, "# P\n"), "[The operator created notes/docs/p.md:]\n```markdown\n# P\n\n```")
        d = web_chat.edit_diff("p.md", "a\nb\nc\n", "a\nB\nc\n")
        self.assertTrue(d.startswith("[The operator edited notes/docs/p.md:]\n```diff\n--- a/notes/docs/p.md"))
        self.assertIn("\n-b\n+B\n", d)
        self.assertEqual(web_chat.edit_diff("p.md", "same", "same"), "[The operator saved notes/docs/p.md unchanged.]")
        long = web_chat.edit_diff("p.md", "", "\n".join(f"line {i}" for i in range(3000)))
        self.assertIn("+3000 −0 lines, too long to show here — read the file", long)

    def test_a_document_with_its_own_code_fence_does_not_close_the_diff(self):
        d = web_chat.edit_diff("p.md", "a\n", "a\n```python\nx\n```\n")
        self.assertTrue(d.startswith("[The operator edited notes/docs/p.md:]\n````diff\n"))
        self.assertTrue(d.endswith("\n````"))
        c = web_chat.edit_diff("p.md", None, "````\nfour\n````")
        self.assertTrue(c.startswith("[The operator created notes/docs/p.md:]\n`````markdown\n"))

    def test_saves_while_it_is_idle_go_once_with_the_next_message_as_one_diff(self):
        chat, _ = say("start", [])
        self.wait_idle(chat["id"])
        said = web_chat.doc_saved("wisp", ME, "plan.md", "one\ntwo\n", "one\n2\n", "sha1", cid=topic()["id"])
        self.assertEqual(said, "saved · wisp gets the change with your next message")
        web_chat.doc_saved("wisp", ME, "plan.md", "one\n2\n", "one\n2\nthree\n", "sha2", cid=topic()["id"])      # folded: from "one\ntwo"
        self.assertEqual(len(self.api.chats()), 1)                                        # nothing sent yet
        say("continue it", [])
        c = self.wait_idle(chat["id"])
        sent = self.api.chats()[1][2]["messages"][-1]["content"]
        self.assertTrue(sent.startswith("continue it\n\n[The operator edited notes/docs/plan.md:]"))
        self.assertIn("-two\n+2\n+three", sent)
        self.assertEqual(sent.count("[The operator edited"), 1)
        self.assertEqual(c["turns"][2]["edits"], ["plan.md"])
        self.assertEqual(c["turns"][2]["shown"], "continue it")
        self.assertNotIn("pending_edits", c)
        say("and now?", [])
        self.wait_idle(chat["id"])
        self.assertNotIn("the operator edited", self.api.chats()[2][2]["messages"][-1]["content"])   # never twice

    def test_a_write_of_the_instance_between_two_saves_is_not_called_his(self):
        """Found live: he created plan.md empty, the instance wrote its plan in it, he added a section — and the
        instance would have been told "the operator created plan.md" with its own plan in it."""
        web_chat.doc_saved("wisp", ME, "plan.md", None, "", "s1", cid=topic()["id"])
        web_chat.doc_saved("wisp", ME, "plan.md", "its plan\n", "its plan\nmy section\n", "s2", cid=topic()["id"])
        chat, _ = say("go on", [])
        sent = self.wait_idle(chat["id"])["turns"][0]["content"]
        self.assertNotIn("the operator created", sent)
        self.assertIn("[The operator edited notes/docs/plan.md:]", sent)
        self.assertIn(" its plan\n+my section", sent)

    def test_a_save_while_it_replies_reaches_it_at_once(self):
        self.api.hold.set()
        chat, _ = say("write the plan", [])
        time.sleep(0.3)
        said = web_chat.doc_saved("wisp", ME, "plan.md", "a\n", "a\nmine\n", "sha1", cid=topic()["id"])
        self.assertEqual(said, "saved · wisp reads it at its next step")
        body = [r for r in self.api.requests if r[0] == "/v1/chat/interject"][0][2]
        self.assertIn("[The operator edited notes/docs/plan.md:]", body["messages"][-1]["content"])
        self.assertIn("+mine", body["messages"][-1]["content"])
        self.api.heard = 1                                        # before the release: the reply ends at once
        self.api.hold.clear()
        c = self.wait_idle(chat["id"], 15)
        self.assertTrue(c["turns"][1]["interjected"])
        self.assertEqual(c["turns"][1]["shown"], "")

    def test_an_empty_message_is_sent_only_to_carry_an_edit(self):
        with self.assertRaises(web_chat.Refused):
            say("  ", [])
        web_chat.doc_saved("wisp", ME, "plan.md", None, "# new\n", "sha1", cid=topic()["id"])
        chat, said = say("", [])
        self.assertEqual(said, "sent to wisp")
        c = self.wait_idle(chat["id"])
        self.assertIn("[The operator created notes/docs/plan.md:]", c["turns"][0]["content"])
        self.assertIn(web_chat.DOCS_NOTE, c["turns"][0]["content"])           # still its first message

    def test_delete_replaces_a_pending_edit_and_is_taken_once(self):
        web_chat.doc_saved("wisp", ME, "plan.md", "one", "two", "s1", cid=topic()["id"])
        said = web_chat.doc_saved("wisp", ME, "plan.md", "two", None, "none", cid=topic()["id"])
        self.assertEqual(said, "deleted · wisp gets the change with your next message")
        c = web_chat.load(topic()["id"])
        self.assertNotIn("plan.md", c["saved_docs"])
        self.assertEqual(web_chat._take_edits(c), ("[The operator deleted notes/docs/plan.md.]", [], ["plan.md"]))
        self.assertEqual(web_chat._take_edits(c), ("", [], []))
        chat, _ = say("continue", [])
        self.wait_idle(chat["id"])
        sent = self.api.chats()[0][2]["messages"][-1]["content"]
        self.assertIn("continue\n\n[The operator deleted notes/docs/plan.md.]", sent)
        self.assertNotIn("```diff", sent)
        from supervisor import web_views
        feed = str(web_views.chat_feed("wisp", web_chat.load(chat["id"]), []))
        self.assertIn('<span class="editchip">🗑 deleted plan.md</span>', feed)      # not "✎ edited": it is gone
        self.assertNotIn("✎ edited plan.md", feed)

    def test_delete_interjects_while_replying(self):
        self.api.hold.set()
        chat, _ = say("write", [])
        time.sleep(0.3)
        said = web_chat.doc_saved("wisp", ME, "plan.md", "one", None, "none", cid=topic()["id"])
        self.assertEqual(said, "deleted · wisp reads it at its next step")
        body = [r for r in self.api.requests if r[0] == "/v1/chat/interject"][0][2]
        self.assertEqual(body["messages"][-1]["content"], "[The operator deleted notes/docs/plan.md.]")
        self.api.heard = 1                                        # before the release: the reply ends at once
        self.api.hold.clear()
        self.wait_idle(chat["id"], 15)


class DocSaveRouteTest(DocsPanelTest):
    def setUp(self):
        super().setUp()
        from supervisor import actions, docs

        def run_now(args, timeout=60, who="top"):        # the CLI verb, in this process (with this test's folders)
            self.ran.append(args)
            name, action, path = args[1:4]
            out = []
            with mock.patch("builtins.print", side_effect=lambda *a, **k: out.append(" ".join(map(str, a)))):
                code = docs.command(name, action, path, args[5] if action == "set" else None, args[-1])
            return code == 0, out[-1] if out else ""
        self.ran = []
        for p in (mock.patch.object(actions, "run_now", side_effect=run_now), mock.patch.object(docs, "log"),
                  mock.patch("supervisor.core.SUP_DIR", self.sup)):
            p.start()
            self.addCleanup(p.stop)

    def post(self, **form):
        import urllib.parse
        return self.req("/docs/wisp/save", "POST", cookie=self.cookie, body=urllib.parse.urlencode(form).encode(),
                        headers={"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie),
                                 "content-type": "application/x-www-form-urlencoded"})

    def delete(self, csrf=True, **form):
        import urllib.parse
        headers = {"sec-fetch-site": "same-origin", "content-type": "application/x-www-form-urlencoded"}
        if csrf:
            headers["x-csrf-token"] = self.csrf(self.cookie)
        return self.req("/docs/wisp/delete", "POST", cookie=self.cookie,
                        body=urllib.parse.urlencode(form).encode(), headers=headers)

    def test_delete_uses_cli_preserves_threads_and_notifies_instance(self):
        from supervisor import doc_comments, docs
        p = self.docs / "plan.md"
        p.write_text("one")
        tid = doc_comments.add_thread("wisp", "p-0123456789abcdef", "plan.md", None, docs.sha(b"one"), "keep this")
        r = self.delete(path="plan.md", sha=docs.sha(b"one"))
        self.assertEqual(r.status, 200, r.body)
        self.assertIn("deleted", r.body.decode())
        self.assertFalse(p.exists())
        self.assertEqual(self.ran, [["docs", "wisp", "rm", "plan.md", "--expect-sha", docs.sha(b"one")]])
        self.assertEqual(web_chat._take_edits(web_chat.target("wisp", "", writing=False)),
                         ("[The operator deleted notes/docs/plan.md.]", [], ["plan.md"]))
        self.assertEqual(doc_comments.threads("wisp", "plan.md")[0]["id"], tid)
        panel = self.req(f"/fragment/docs/wisp?doc=plan.md&thread={tid}", cookie=self.cookie)
        self.assertEqual(panel.status, 200)
        self.assertIn("plan.md is not there (any more)", panel.body.decode())
        self.assertIn("data-back-docs", panel.body.decode())

    def test_delete_refuses_stale_missing_traversal_and_missing_csrf(self):
        from supervisor import docs
        p = self.docs / "plan.md"
        p.write_text("one")
        self.assertEqual(self.delete(path="plan.md", sha="old").status, 409)
        self.assertEqual(self.delete(path="missing.md", sha="none").status, 409)
        self.assertEqual(self.delete(path="../x", sha="none").status, 409)
        self.assertEqual(self.delete(path="../x.md", sha="none").status, 409)
        self.assertEqual(self.delete(csrf=False, path="plan.md", sha=docs.sha(b"one")).status, 403)
        self.assertEqual(self.req("/docs/wisp/delete", "POST").status, 403)
        self.assertEqual(p.read_text(), "one")
        self.assertEqual(self.ran, [])
        self.assertIsNone(web_chat.target("wisp", "", writing=False))

    def test_delete_refuses_a_write_between_the_route_check_and_cli(self):
        from supervisor import actions, docs
        p = self.docs / "plan.md"
        p.write_text("one")
        run = actions.run_now.side_effect

        def changed(args, **kw):
            p.write_text("two")
            return run(args, **kw)
        with mock.patch.object(actions, "run_now", side_effect=changed):
            r = self.delete(path="plan.md", sha=docs.sha(b"one"))
        self.assertEqual(r.status, 409)
        self.assertIn("changed since you looked", r.body.decode())
        self.assertEqual(p.read_text(), "two")
        self.assertIsNone(web_chat.target("wisp", "", writing=False))

    def test_a_save_goes_through_the_cli_and_is_queued_for_the_instance(self):
        from supervisor import docs
        (self.docs / "plan.md").write_text("one\n")
        r = self.post(path="plan.md", text="one\nmine\n", sha=docs.sha(b"one\n"))
        self.assertEqual(r.status, 200, r.body)
        self.assertEqual(r.headers["X-Doc"], "plan.md")
        self.assertEqual(self.ran[0][:4], ["docs", "wisp", "set", "plan.md"])
        self.assertEqual((self.docs / "plan.md").read_text(), "one\nmine\n")
        chat = web_chat.target("wisp", "", writing=False)
        self.assertEqual(chat["pending_edits"]["plan.md"], {"old": "one\n", "new": "one\nmine\n"})
        feed = self.req("/fragment/chat/wisp", cookie=self.cookie).body.decode()
        self.assertIn("Your changes to plan.md go to wisp with your next message", feed)

    def test_issue_22_success_returns_the_saved_revision(self):
        """Reverting #22 leaves typing during Save based on the obsolete revision."""
        from supervisor import docs
        (self.docs / "plan.md").write_text("one")
        r = self.post(path="plan.md", text="two", sha=docs.sha(b"one"))
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["X-Doc-Sha"], docs.sha(b"two"))

    def test_a_stale_save_is_refused_shows_what_changed_and_writes_nothing(self):
        from supervisor import docs
        (self.docs / "plan.md").write_text("one\nby the instance\n")
        r = self.post(path="plan.md", text="one\nmine\n", sha=docs.sha(b"one\n"))
        self.assertEqual(r.status, 409)
        body = r.body.decode()
        self.assertIn("changed since you looked", body)
        self.assertIn("+by the instance", body)
        self.assertEqual((self.docs / "plan.md").read_text(), "one\nby the instance\n")
        self.assertIsNone(web_chat.target("wisp", "", writing=False))
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

    def test_a_new_document_in_the_folder_shown_and_in_a_new_subfolder(self):
        r = self.post(path="plan", text="", sha="none", dir="ucg")
        self.assertEqual((r.status, r.headers.get("X-Doc")), (200, "ucg/plan.md"))
        self.assertTrue((self.docs / "ucg" / "plan.md").is_file())
        r = self.post(path="q4/goals", text="", sha="none", dir="ucg")
        self.assertEqual((r.status, r.headers.get("X-Doc")), (200, "ucg/q4/goals.md"))
        self.assertTrue((self.docs / "ucg" / "q4" / "goals.md").is_file())
        r = self.post(path="x", text="", sha="none", dir="../out")
        self.assertEqual(r.status, 409)
        self.assertIn("a folder under notes/docs/", r.body.decode())
        self.assertFalse((self.docs.parent / "out").exists())

    def test_a_later_write_that_dropped_his_lines_is_said_with_his_version(self):
        from supervisor import docs
        (self.docs / "plan.md").write_text("one\n")
        self.post(path="plan.md", text="one\nmy line\n", sha=docs.sha(b"one\n"))
        (self.docs / "plan.md").write_text("one\nmy line\nits addition\n")             # built on it: no warning
        panel = self.req("/fragment/docs/wisp?doc=plan.md", cookie=self.cookie).body.decode()
        self.assertNotIn("no longer in it", panel)
        (self.docs / "plan.md").write_text("one\nits rewrite\n")                       # dropped it: said
        panel = self.req("/fragment/docs/wisp?doc=plan.md", cookie=self.cookie).body.decode()
        self.assertIn("1 line(s) you wrote at", panel)
        self.assertIn("Restore yours", panel)
        self.assertIn('data-post="/docs/wisp/save"', panel)
        self.assertIn(f'name="sha" value="{docs.sha(b"one" + bytes([10]) + b"its rewrite" + bytes([10]))}"', panel)


class DocsButtonTest(DocsPanelTest):
    def test_with_no_documents_the_panel_can_still_be_opened_to_make_one(self):
        """Found live: with no documents the panel is hidden, and its New document with it."""
        body = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        self.assertIn("data-show-docs", body)
        self.assertIn('data-post="/docs/wisp/save"', body)                       # the New document form is there


class ClearTest(DocsChatCase):
    """Clear the chat: the page and the rail start empty; nothing is deleted; an open conversation is ended first."""

    def test_an_open_chat_is_ended_then_cleared_and_kept(self):
        chat, _ = say("hello", [])
        self.wait_idle(chat["id"])
        said = web_chat.clear("wisp", chat["id"])
        self.assertTrue(said.startswith("cleared · ended"), said)
        self.assertEqual(len([r for r in self.api.requests if r[0] == "/v1/chat/end"]), 1)   # written up
        c = web_chat.load(chat["id"])
        self.assertEqual(c["state"], "ended")
        self.assertTrue(c["cleared"])
        self.assertEqual(len(c["turns"]), 2)                       # nothing deleted
        self.assertIsNone(web_chat.shown("wisp"))
        self.assertEqual(web_chat.summary("wisp"), {})
        self.assertIn(c, web_chat.chats("wisp"))                   # still listed with the earlier chats
        with self.assertRaisesRegex(web_chat.Refused, "nothing to clear"):
            web_chat.clear("wisp", chat["id"])

    def test_an_ended_thread_is_not_cleared_it_is_in_earlier_chats(self):
        """Clear hid the ended chat the plain page showed. Since 2026-11-11 no page shows an ended thread but Earlier
        chats: there is nothing on screen to clear, and the next message starts a new thread."""
        chat, _ = say("hello", [])
        self.wait_idle(chat["id"])
        web_chat.end(chat["id"])
        ends = len([r for r in self.api.requests if r[0] == "/v1/chat/end"])
        with self.assertRaisesRegex(web_chat.Refused, "has ended: it is in Earlier chats"):
            web_chat.clear("wisp", chat["id"])
        self.assertEqual(len([r for r in self.api.requests if r[0] == "/v1/chat/end"]), ends)
        chat2, _ = say("again", [])          # the next message starts a new thread
        self.assertNotEqual(chat2["id"], chat["id"])
        self.assertEqual([c["id"] for c in web_chat.threads("wisp")], [chat2["id"]])
        self.wait_idle(chat2["id"])

    def test_a_reply_running_is_not_cleared(self):
        self.api.hold.set()
        chat, _ = say("long one", [])
        time.sleep(0.3)
        with self.assertRaisesRegex(web_chat.Refused, "stop the reply first"):
            web_chat.clear("wisp", chat["id"])
        self.api.hold.clear()
        self.wait_idle(chat["id"], 15)

    def test_edits_not_yet_sent_reach_the_instance_after_a_clear_or_an_end(self):
        """They stayed on the old chat and were lost: the next message starts a new one."""
        chat, _ = say("hello", [])
        self.wait_idle(chat["id"])
        web_chat.doc_saved("wisp", ME, "plan.md", "a\n", "a\nmine\n", "s1", cid=topic()["id"])
        web_chat.clear("wisp", chat["id"])
        chat2, _ = say("go on", [])
        c = self.wait_idle(chat2["id"])
        self.assertIn("[The operator edited notes/docs/plan.md:]", c["turns"][0]["content"])
        self.assertNotIn("pending_edits", web_chat.load(chat["id"]))
        web_chat.doc_saved("wisp", ME, "plan.md", "a\nmine\n", "a\nmine\nmore\n", "s2", cid=topic()["id"])
        web_chat.end(chat2["id"])
        chat3, _ = say("and now", [])
        c3 = self.wait_idle(chat3["id"])
        self.assertIn("+more", c3["turns"][0]["content"])


class ClearRouteTest(ChatRoutesCase):
    def test_the_page_offers_clear_and_the_route_takes_the_page_token_only(self):
        chat = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "a", "ts": "2026-10-19T12:00:05"}])
        body = self.req(f"/i/wisp/chat?id={chat['id']}", cookie=self.cookie).body.decode()   # the thread's page
        self.assertIn('data-post="/chat/wisp/clear"', body)
        no = self.req("/chat/wisp/clear", "POST", cookie=self.cookie, headers={"sec-fetch-site": "same-origin"})
        self.assertEqual(no.status, 403)
        with mock.patch.object(web_chat, "_post", return_value=(202, {})):     # an open thread is ended first
            ok = self.req("/chat/wisp/clear", "POST", cookie=self.cookie, body=f"cid={chat['id']}".encode(),
                          headers={"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie)})
        self.assertEqual(ok.status, 200, ok.body)
        self.assertEqual(web_chat.threads("wisp"), [])
        body = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()   # the instance's page: Main
        self.assertIn("1 message(s)", body)                        # in Earlier web chats, kept


class SaveRecordTest(DocSaveRouteTest):
    def test_a_write_right_after_the_save_is_not_taken_for_his(self):
        """Codex review: the save's sha was read again after the write, so an instance writing in between had its
        version recorded as his and the overwrite warning never came."""
        from supervisor import actions, docs
        (self.docs / "plan.md").write_text("one\n")
        real = actions.run_now.side_effect

        def save_then_instance_writes(args, timeout=60, who="top"):
            out = real(args, timeout, who)
            (self.docs / "plan.md").write_text("its rewrite\n")
            return out
        with mock.patch.object(actions, "run_now", side_effect=save_then_instance_writes):
            r = self.post(path="plan.md", text="one\nmy line\n", sha=docs.sha(b"one\n"))
        self.assertEqual(r.status, 200)
        saved = web_chat.target("wisp", "", writing=False)["saved_docs"]["plan.md"]
        self.assertEqual(saved["sha"], docs.sha(b"one\nmy line\n"))
        panel = self.req("/fragment/docs/wisp?doc=plan.md", cookie=self.cookie).body.decode()
        self.assertIn("you wrote at", panel)

    def test_a_stale_save_is_refused_before_the_cli_runs(self):
        from supervisor import docs
        (self.docs / "plan.md").write_text("changed\n")
        r = self.post(path="plan.md", text="mine\n", sha=docs.sha(b"one\n"))
        self.assertEqual(r.status, 409)
        self.assertEqual(self.ran, [])


class LifecycleRaceTest(ChatTest):
    """Codex review: chat choice, End and Clear raced with a send from another device."""

    def test_two_devices_writing_first_open_one_main(self):
        """Two first messages naming no conversation at once: one Main, both in it (its record is made once)."""
        self.api.hold.set()
        go = threading.Barrier(2)
        out = []

        def send(text):
            go.wait()
            out.append(web_chat.send("wisp", ME, text, [])[0]["id"])
        ts = [threading.Thread(target=send, args=(t,)) for t in ("from the phone", "from the laptop")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(set(out)), 1)                       # one chat: the second message joined it
        self.assertEqual(len(web_chat._all_chats("wisp")), 1)
        self.api.hold.clear()
        self.wait_idle(out[0], 15)

    def test_a_message_to_a_thread_while_end_is_on_its_way_is_refused_not_added(self):
        """Another device writes in the thread while End runs: it waits for End, then is refused (the thread has
        ended), and nothing is added to the ended thread."""
        chat, _ = say("hello", [])
        self.wait_idle(chat["id"])
        real_post, sent = web_chat._post, {}

        def other_device():
            try:
                sent["chat"] = say("one more", [], cid=chat["id"])[0]
            except web_chat.Refused as e:
                sent["refused"] = str(e)

        def post(c, route, body, timeout=15):
            if route == "end":                                   # the other device sends now
                t = threading.Thread(target=other_device)
                t.start()
                t.join(0.3)                                      # it waits for End: it does not get in first
                self.assertNotIn("chat", sent)
                sent["thread"] = t
            return real_post(c, route, body, timeout)
        with mock.patch.object(web_chat, "_post", side_effect=post):
            web_chat.end(chat["id"])
        sent["thread"].join(5)
        old = web_chat.load(chat["id"])
        self.assertEqual((old["state"], len(old["turns"])), ("ended", 2))
        self.assertIn("ended", sent.get("refused", ""))


class ArchivedClearTest(ChatRoutesCase):
    def test_a_past_chats_page_acts_on_nothing_and_a_stale_clear_is_refused(self):
        past = self.chat_with([{"role": "user", "content": "old", "ts": "2026-10-19T11:00:00"}], "ended")
        now = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                              {"role": "assistant", "content": "a", "ts": "2026-10-19T12:00:05"}])
        page = self.req(f"/i/wisp/chat?id={past['id']}", cookie=self.cookie).body.decode()
        frag = self.req(f"/fragment/chat/wisp?id={past['id']}", cookie=self.cookie).body.decode()
        for body in (page, frag):
            self.assertNotIn("/chat/wisp/clear", body)
            self.assertNotIn("chat-end", body)
        with self.assertRaisesRegex(web_chat.Refused, "has ended"):
            web_chat.clear("wisp", past["id"])
        self.assertEqual(web_chat.load(now["id"])["state"], "idle")   # the current one untouched


class ClearInHeaderTest(ChatRoutesCase):
    def test_selected_open_thread_refreshes_its_own_feed_header_and_activity(self):
        chat = self.chat_with([{"role": "user", "content": "selected topic", "ts": "2026-10-19T12:00:00"}])
        other = self.chat_with([])
        cid = chat["id"]

        def form(body, route):
            return next(f.split("</form>", 1)[0] for f in body.split("<form") if f'data-post="{route}"' in f)

        for state in ("idle", "replying"):
            with self.subTest(state=state):
                chat["state"] = state
                chat["reply"] = ({"started": "2026-10-19T12:00:01", "activity": ["selected activity"], "answer": ""}
                                 if state == "replying" else None)
                web_chat.save(chat)
                page = self.req(f"/i/wisp/chat?id={cid}", cookie=self.cookie).body.decode()
                self.assertIn(f'data-poll="/fragment/chat/wisp?id={cid}"', page)
                self.assertIn(f'data-poll="/fragment/chat/wisp/header?id={cid}"', page)
                self.assertIn(f'name="cid" value="{cid}"', form(page, "/chat/wisp/send"))
                self.assertIn(f'name="cid" value="{cid}"', form(page, "/chat/wisp/clear"))
                header = self.req(f"/fragment/chat/wisp/header?id={cid}", cookie=self.cookie).body.decode()
                self.assertIn(f'name="cid" value="{cid}"', form(header, "/chat/wisp/clear"))
                feed = self.req(f"/fragment/chat/wisp?id={cid}", cookie=self.cookie).body.decode()
                self.assertIn("selected topic", feed)
                action = "stop" if state == "replying" else "end"
                action_body = feed if state == "replying" else header
                self.assertIn(f'name="cid" value="{cid}"', form(action_body, f"/action/chat-{action}"))
                self.assertNotIn("/action/chat-end", feed)
                web._sent_during.assert_called_with("wisp", chat, False)
                if state == "replying":
                    self.assertIn(f'data-poll="/fragment/chat/wisp/live?id={cid}"', feed)
                    activity = self.req(f"/fragment/chat/wisp/live?id={cid}", cookie=self.cookie).body.decode()
                    self.assertIn("selected activity", activity)
        # Selecting the current thread explicitly must pin it too.
        page = self.req(f'/i/wisp/chat?id={other["id"]}', cookie=self.cookie).body.decode()
        self.assertIn(f'data-poll="/fragment/chat/wisp?id={other["id"]}"', page)

    def test_the_instance_page_writes_to_main_and_a_threads_page_to_it(self):
        """The instance's own page is Main (2026-11-11): its composer names Main, never a thread it was drawn beside
        (a thread can end; Main does not). A thread's page names the thread."""
        chat = self.chat_with([{"role": "user", "content": "topic", "ts": "2026-10-19T12:00:00"}])
        page = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        composer = page.split('<form class="composer"', 1)[1].split("</form>", 1)[0]
        self.assertIn('name="cid" value="from"', composer)
        selected = self.req(f'/i/wisp/chat?id={chat["id"]}', cookie=self.cookie).body.decode()
        composer = selected.split('<form class="composer"', 1)[1].split("</form>", 1)[0]
        self.assertIn(f'name="cid" value="{chat["id"]}"', composer)

    def test_shown_ended_thread_leaves_the_next_message_unaddressed(self):
        chat = self.chat_with([{"role": "user", "content": "old topic", "ts": "2026-10-19T12:00:00"}], "ended")
        page = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        composer = page.split('<form class="composer"', 1)[1].split("</form>", 1)[0]
        self.assertIn('name="cid" value="from"', composer)        # Main, not the thread that ended
        self.assertIn('data-poll="/fragment/chat/wisp"', page)
        archived = self.req(f'/i/wisp/chat?id={chat["id"]}', cookie=self.cookie).body.decode()
        self.assertNotIn('<form class="composer"', archived)
        self.assertIn(f'data-poll="/fragment/chat/wisp?id={chat["id"]}"', archived)
        feed = self.req(f'/fragment/chat/wisp?id={chat["id"]}', cookie=self.cookie).body.decode()
        self.assertNotIn('data-post="/action/chat-end"', feed)
        web._sent_during.assert_called_with("wisp", chat, True)

    def test_issue_10_18_header_refreshes_facts_actions_and_busy_warning(self):
        """Reverting #10/#18 leaves the header frozen and Pause unchanged after pausing."""
        page = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        self.assertIn('data-poll="/fragment/chat/wisp/header"', page)
        first = self.req("/fragment/chat/wisp/header", cookie=self.cookie)
        self.assertEqual(first.status, 200)
        self.assertIn('>Pause<', first.body.decode())
        with mock.patch.object(dashboard, "live_state", return_value=live_snap(
                state="paused by you", model="different-model", usd_today=1.25)), \
                mock.patch.object(web, "overview", side_effect=AssertionError("dashboard read")), \
                mock.patch.object(dashboard, "snapshot", side_effect=AssertionError("dashboard read")):
            body = self.req("/fragment/chat/wisp/header", cookie=self.cookie).body.decode()
        self.assertIn('>Resume<', body)
        self.assertNotIn('>Pause<', body)
        self.assertIn('different-model · paused by you · $1.25 today', body)
        self.assertIn('No cycles run until you resume wisp. It still answers your messages.', body)
        self.assertIn('details class="menu chatmenu" data-key="chat-menu"', body)
        idle = self.req("/fragment/chat/wisp/header", cookie=self.cookie).body.decode()
        self.assertNotIn('class="warn"', idle)
        self.assertNotIn('textarea', body)
        self.assertNotIn('id="docs"', body)
        self.assertEqual(self.req("/fragment/chat/nobody/header", cookie=self.cookie).status, 404)

    def test_issue_18_19_header_follows_chat_clear_and_earlier(self):
        """Reverting #18/#19 keeps Clear absent or leaves its cid on the previous conversation. Main's header has no
        Clear (Main is the record of what was sent); a thread's header clears that thread; once cleared it is in
        Earlier chats and its header clears nothing."""
        main = "/fragment/chat/wisp/header"
        self.assertNotIn('/chat/wisp/clear', self.req(main, cookie=self.cookie).body.decode())
        chat = self.chat_with([{"role": "user", "content": "hello", "ts": "2026-10-19T12:00:00"}])
        url = f"/fragment/chat/wisp/header?id={chat['id']}"
        body = self.req(url, cookie=self.cookie).body.decode()
        self.assertIn('data-post="/chat/wisp/clear"', body)
        self.assertIn(f'name="cid" value="{chat["id"]}"', body)
        headers = {"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie)}
        with mock.patch.object(web_chat, "_post", return_value=(202, {})):     # an open thread is ended first
            cleared = self.req("/chat/wisp/clear", "POST", cookie=self.cookie, headers=headers,
                               body=f"cid={chat['id']}".encode())
        self.assertEqual(cleared.status, 200, cleared.body)
        self.assertNotIn('/chat/wisp/clear', self.req(url, cookie=self.cookie).body.decode())
        self.assertIn(f'/i/wisp/chat?id={chat["id"]}', self.req(main, cookie=self.cookie).body.decode())
        next_chat = self.chat_with([{"role": "user", "content": "next", "ts": "2026-10-19T13:00:00"}])
        body = self.req(f"/fragment/chat/wisp/header?id={next_chat['id']}", cookie=self.cookie).body.decode()
        self.assertIn(f'name="cid" value="{next_chat["id"]}"', body)
        self.assertNotIn(f'name="cid" value="{chat["id"]}"', body)

    def test_issue_19_clear_route_requires_the_shown_cid(self):
        """Reverting #19 lets a POST without cid clear the current chat; stale ids must also be refused."""
        chat = self.chat_with([{"role": "user", "content": "hello", "ts": "2026-10-19T12:00:00"}], "ended")
        headers = {"sec-fetch-site": "same-origin", "x-csrf-token": self.csrf(self.cookie)}
        for body in (b"cid=web-stale", b""):
            with self.subTest(body=body):
                r = self.req("/chat/wisp/clear", "POST", cookie=self.cookie, headers=headers, body=body)
                self.assertEqual(r.status, 409, r.body)
                self.assertIn(b"not a chat of wisp", r.body)
                self.assertNotIn("cleared", web_chat.load(chat["id"]))

    def test_clear_and_close_thread_are_in_the_header_menu(self):
        """the operator, on his phone: "I don't see the button to clear the chat" — it sat under the last answer, at the
        bottom of a feed that scrolls in its own box."""
        chat = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                               {"role": "assistant", "content": "a", "ts": "2026-10-19T12:00:05"}])
        body = self.req(f"/i/wisp/chat?id={chat['id']}", cookie=self.cookie).body.decode()
        head = body[body.index('<header class="chathead">'):body.index("</header>")]
        self.assertIn('data-post="/chat/wisp/clear"', head)
        feed = self.req(f"/fragment/chat/wisp?id={chat['id']}", cookie=self.cookie).body.decode()
        self.assertNotIn("/clear", feed)
        self.assertNotIn("chat-end", feed)
        self.assertRegex(head, r'data-key="chat-menu">.*data-post="/action/chat-end".*>Close thread</button>')

    def test_no_chat_no_clear(self):
        body = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        self.assertNotIn("/chat/wisp/clear", body)


class NetworkTest(ChatRoutesCase):
    """What a phone on cellular pays for a chat left open."""

    def test_an_unchanged_refresh_is_a_304_with_no_body(self):
        chat = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"}])
        url = f"/fragment/chat/wisp?id={chat['id']}"
        first = self.req(url, cookie=self.cookie)
        tag = first.headers["ETag"]
        self.assertTrue(tag.startswith('"') and len(first.body) > 100)
        again = self.req(url, cookie=self.cookie, headers={"if-none-match": tag})
        self.assertEqual((again.status, again.body), (304, b""))
        chat["turns"].append({"role": "user", "content": "q2", "ts": "2026-10-19T12:01:00"})   # changed: the body
        web_chat.save(chat)
        changed = self.req(url, cookie=self.cookie, headers={"if-none-match": tag})
        self.assertEqual(changed.status, 200)
        page = self.req("/i/wisp/chat", cookie=self.cookie)
        self.assertNotIn("ETag", page.headers)                               # pages and actions: no conditionals

    def test_the_page_stamps_its_feed_and_documents_so_their_first_refresh_is_a_304(self):
        """Regression (2026-10-26): the page drew the chat, then its first refresh downloaded all of it again (310 KB
        for a long chat) — it sent no version. The page carries each polled panel's ETag, for the URL it polls."""
        import html, re
        self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                        {"role": "assistant", "content": "a", "ts": "2026-10-19T12:00:05"}])
        page = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        for cls in ('class="feed"', 'class="docs"'):
            tag = re.search(cls + r'[^>]*data-poll="([^"]+)"[^>]*data-etag="([^"]+)" data-etag-url="([^"]+)"', page)
            self.assertIsNotNone(tag, f"{cls} carries no ETag")
            url, etag, etag_url = map(html.unescape, tag.groups())
            self.assertEqual(url, etag_url)
            first = self.req(url, cookie=self.cookie, headers={"if-none-match": etag})
            self.assertEqual((first.status, first.body), (304, b""), f"{url}: the first refresh sent it all again")

    def test_every_feed_item_has_a_key_the_page_updates_by(self):
        chat = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"},
                               {"role": "assistant", "content": "a", "ts": "2026-10-19T12:00:05"}])
        feed = self.req(f"/fragment/chat/wisp?id={chat['id']}", cookie=self.cookie).body.decode()
        self.assertIn('class="turn me" data-k="u0"', feed)
        self.assertIn('class="turn it" data-k="a1"', feed)
        import re
        keys = re.findall(r'data-k="([^"]+)"', feed)
        self.assertEqual([k for k in keys if not k.startswith("day-")], ["u0", "a1"])
        page = self.req("/i/wisp/chat", cookie=self.cookie).body.decode()
        self.assertIn('data-keyed="1"', page)
        self.assertIn('data-lazy="1"', page)                                 # the documents: slow while hidden
        self.assertRegex(page, r'data-max-file="\d+" data-max-total="\d+" data-max-files="\d+"')

    def test_chat_files_are_parsed_once_per_change(self):
        chat = self.chat_with([{"role": "user", "content": "q", "ts": "2026-10-19T12:00:00"}])
        web_chat.chats("wisp")
        with mock.patch.object(web_chat.json, "loads", side_effect=AssertionError("parsed again")):
            self.assertEqual(web_chat.chats("wisp")[0]["id"], chat["id"])


class ResendTest(ChatTest):
    def test_a_message_sent_again_with_its_id_is_not_sent_twice(self):
        """A send whose answer was lost (a phone on cellular) was sent again by the next tap: two replies."""
        chat, said = say("hello", [], mid="m-1")
        self.wait_idle(chat["id"])
        again, said2 = say("hello", [], mid="m-1")
        self.assertEqual(said2, "already sent to wisp")
        self.assertEqual(len(self.api.chats()), 1)
        c = web_chat.load(chat["id"])
        self.assertEqual([t["role"] for t in c["turns"]], ["user", "assistant"])
        say("hello", [], mid="m-2")                     # a new id: a new message
        self.wait_idle(chat["id"])
        self.assertEqual(len(self.api.chats()), 2)


class PushWhenTest(ChatTest):
    def test_a_failed_reply_is_not_pushed_as_replied_20261029(self):
        # Regression (2026-10-29 10:39): tundra's reply ended on "stream error: Upstream idle timeout exceeded" and the
        # phone was told "tundra replied".
        failed = {"id": "web-test", "instance": "tundra", "turns": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "note": "error: stream error: Upstream idle timeout exceeded"}]}
        event = web_chat.reply_event(failed)
        self.assertEqual(event["title"], "tundra's reply failed")
        self.assertIn("Upstream idle timeout", event["body"])
        done = {"id": "web-test", "instance": "tundra", "turns": [{"role": "assistant", "content": "Done", "note": ""}]}
        self.assertEqual(web_chat.reply_event(done)["title"], "tundra replied")

    def test_a_reply_that_ends_unwatched_wakes_the_phone_and_a_watched_one_does_not(self):
        web_chat._watched.clear()
        with mock.patch("supervisor.webpush.notify") as notify:
            chat, _ = say("hello", [])
            self.wait_idle(chat["id"])
            time.sleep(0.2)
        (event,), kw = notify.call_args                          # what this push shows: this reply (issue #23)
        self.assertEqual((event["title"], event["url"]), ("wisp replied", f"/i/wisp/chat?id={chat['id']}"))
        self.assertEqual(kw, {"skip": set()})
        notify.assert_called_once()
        web_chat.watching("wisp", "laptop", cid=chat["id"])      # its page is open and refreshing there
        with mock.patch("supervisor.webpush.notify") as notify:
            say("again", [])
            self.wait_idle(chat["id"])
            time.sleep(0.2)
        # W9 (review 2026-10-24): only the device looking is skipped — the laptop's tab silenced the phone too
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs, {"skip": {"laptop"}})
