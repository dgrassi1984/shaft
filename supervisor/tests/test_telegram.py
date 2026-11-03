"""Telegram (the operator, 2026-10-13): one bot per instance, ONE paired account, live conversation through the API,
instances' messages to the operator forwarded from their own bot. Against a fake Bot API server."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import messages, telegram

TOK = {"wren": "111:wren-secret", "shaft": "222:shaft-secret"}
ME, STRANGER = {"id": 42, "username": "operator"}, {"id": 666, "username": "someone"}


class FakeBotAPI:
    FILE = b"%PDF-1.7 fake"

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []      # (token, method, params)
        self.next_id = 100
        self.fail = set()
        self.reject_html = False
        self.uploads: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):                     # /file/bot<token>/<path>: a file the operator sent
                data = fake.FILE
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                _, bot, method = self.path.split("/", 2)
                token = bot.removeprefix("bot")
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers["Content-Type"].startswith("multipart/form-data"):
                    fake.uploads.append(raw.decode("latin-1"))
                    params = {}
                else:
                    params = json.loads(raw or b"{}")
                fake.calls.append((token, method, params))
                if method in fake.fail or token not in TOK.values():
                    body, code = {"ok": False, "description": "Unauthorized"}, 401
                elif fake.reject_html and params.get("parse_mode"):
                    body, code = {"ok": False, "description": "Bad Request: can't parse entities: bad tag"}, 400
                elif method == "getMe":
                    body, code = {"ok": True, "result": {"username": "wren_shaft_bot"}}, 200
                elif method in ("sendMessage", "sendPhoto", "sendDocument"):
                    fake.next_id += 1
                    body, code = {"ok": True, "result": {"message_id": fake.next_id}}, 200
                elif method == "getFile":
                    body, code = {"ok": True, "result": {"file_path": f"documents/{params.get('file_id')}"}}, 200
                else:
                    body, code = {"ok": True, "result": True}, 200
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def sent(self, token=None):
        return [(t, p) for t, m, p in self.calls if m == "sendMessage" and (token is None or t == token)]


def _msg(user, text, mid=1, chat_type="private", reply_to=None):
    m = {"message_id": mid, "from": user, "chat": {"id": user["id"], "type": chat_type}, "text": text}
    if reply_to:
        m["reply_to_message"] = reply_to
    return {"update_id": mid, "message": m}


class TelegramTest(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-tg-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.sup = self.project / ".supervisor"
        self.sup.mkdir()
        self.api = FakeBotAPI()
        self.addCleanup(self.api.server.shutdown)
        self.asked = []
        env = {telegram.token_key(n): t for n, t in TOK.items()}
        for p in (mock.patch.object(telegram, "SUP_DIR", self.sup), mock.patch.object(telegram, "API_BASE", self.api.url),
                  mock.patch.object(telegram, "secrets_env", lambda: env),
                  mock.patch.object(telegram, "instance_names", lambda: ["shaft", "wren", "kant"]),
                  mock.patch.object(telegram, "log", lambda *_: None),
                  mock.patch.object(telegram, "ask_api", self._ask),
                  mock.patch.object(messages, "SUP_DIR", self.sup),
                  mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
                  mock.patch.object(messages, "INSTANCES", self.project / "instances"),
                  mock.patch.object(messages, "log", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        messages._cache.update(key=None, rows=[])
        self.chat = telegram.Chat("wren", TOK["wren"])

    def _ask(self, name, history, request_id=""):
        self.asked.append((name, [dict(h) for h in history]))
        return f"answer {len(self.asked)}", 0

    def upd(self, u, chat=None):
        telegram.handle_update("wren", TOK["wren"], chat or self.chat, u)

    def pair(self):
        code = telegram.new_pairing_code()
        self.upd(_msg(ME, f"/pair {code}"))
        return code

    def test_only_one_account_pairs_and_only_it_is_answered(self):
        self.upd(_msg(ME, "hello"))                                   # not paired yet: silence
        self.upd(_msg(ME, "/pair WRONG123"))
        self.assertEqual(self.api.sent(), [])
        code = self.pair()
        self.assertEqual(telegram.paired_id(), 42)
        self.assertIn("Paired", self.api.sent()[-1][1]["text"])
        n = len(self.api.calls)
        self.upd(_msg(STRANGER, f"/pair {code}", mid=5))              # the code is spent; a second account never pairs
        self.upd(_msg(STRANGER, "hi wren", mid=6))
        self.assertEqual(len(self.api.calls), n)                      # nothing answered, nothing asked
        self.assertEqual(self.asked, [])
        self.assertEqual(telegram.paired_id(), 42)
        with self.assertRaisesRegex(telegram.TelegramError, "already paired"):
            telegram.new_pairing_code()                               # only `shaft telegram unpair` frees it
        self.upd(_msg(ME, "group hi", mid=7, chat_type="group"))      # groups are ignored, even from the owner
        self.assertEqual(self.asked, [])

    def test_an_expired_code_does_not_pair(self):
        code = telegram.new_pairing_code()
        telegram.update(lambda s: s["pairing"].update(until=datetime(2020, 1, 1).isoformat()))
        self.upd(_msg(ME, f"/pair {code}"))
        self.assertIsNone(telegram.paired_id())

    def test_a_message_is_a_turn_of_a_live_conversation(self):
        self.pair()
        self.upd(_msg(ME, "what did you find?", mid=10))
        self.upd(_msg(ME, "and then?", mid=11))
        self.assertEqual([h["content"] for h in self.asked[1][1]], ["what did you find?", "answer 1", "and then?"])
        self.assertEqual([p["text"] for _, p in self.api.sent(TOK["wren"])][-2:], ["answer 1", "answer 2"])
        self.chat.last -= telegram.IDLE_MINUTES * 60 + 1                 # idle: the API wrote it up — a new one
        self.upd(_msg(ME, "new topic", mid=12))
        self.assertEqual([h["content"] for h in self.asked[2][1]], ["new topic"])
        self.upd(_msg(ME, "/new", mid=13))
        self.upd(_msg(ME, "fresh", mid=14))
        self.assertEqual(len(self.asked[3][1]), 1)

    def test_a_message_taken_just_before_a_restart_is_still_answered(self):
        """B11 (review 2026-10-24): poll saved the next offset, then a daemon thread handled the message. A restart in
        between (the service updated, the Mac rebooted) lost it: Telegram never sent it again and nothing kept it."""
        self.pair()
        real_call, real_handle, held = telegram.call, telegram.Chat.handle, threading.Event()
        self.addCleanup(held.set)
        first = []

        def handle(chat, msg, update_id=None):
            if not first:                                  # the service stops while this one is being handled
                first.append(msg)
                held.wait(30)
                return
            return real_handle(chat, msg, update_id)

        def service(updates):
            stop = threading.Event()

            def call(token, method, *a, **k):
                if method == "getUpdates":
                    stop.set()
                    return updates
                return real_call(token, method, *a, **k)
            return stop, call

        def settle():
            for _ in range(200):
                if not (telegram.load().get("pending") or {}).get("wren"):
                    return
                time.sleep(0.02)
        with mock.patch.object(telegram.Chat, "handle", handle):
            stop, call = service([_msg(ME, "are you there?", mid=7)])
            with mock.patch.object(telegram, "call", call):
                telegram.poll("wren", TOK["wren"], stop)
                for _ in range(200):
                    if first:
                        break
                    time.sleep(0.02)
            self.assertEqual(telegram.load()["offsets"]["wren"], 8)        # taken: Telegram will not send it again
            for _ in range(2):                                            # the restarted service, then another one
                stop, call = service([])
                with mock.patch.object(telegram, "call", call):
                    telegram.poll("wren", TOK["wren"], stop)
                    settle()
        self.assertEqual([h[1][-1]["content"] for h in self.asked], ["are you there?"])   # answered, and only once
        self.assertEqual(telegram.load().get("pending") or {}, {})

    def restart_while(self, heard: bool):
        """Message A (update 30) is being answered when B (update 31) arrives and joins it, heard by A's reply or too
        late for it. Then the service stops — while A's answer is sent (heard) or while B's follow-up reply runs (too
        late) — and starts again. Returns what was asked, (text, request id), and what was pending at the restart."""
        self.pair()
        real_call, real_send, stuck = telegram.call, telegram.send, threading.Event()
        self.addCleanup(stuck.set)
        asking, joined, asks = threading.Event(), threading.Event(), []

        def ask(name, history, request_id=""):
            asks.append((history[-1]["content"], request_id))
            if len(asks) == 1:                                   # A's reply: B arrives meanwhile
                asking.set()
                joined.wait(10)
                return "answer A", 1 if heard else 0
            if len(asks) == 2 and not heard:                     # B's follow-up: the service stops during it
                stuck.wait(30)
                raise SystemExit                                 # that service is gone: its thread does nothing more
            if heard:                                            # restarted: the API's kept answer for A
                return "answer A", 1, True
            return f"answer {len(asks)}", 0

        def interject(name, messages):
            joined.set()
            return heard

        def send(token, chat_id, text, reply_to=None):
            if heard and text == "answer A" and not getattr(send, "once", False):
                send.once = True                                 # the service stops while A's answer is sent
                stuck.wait(30)
                raise SystemExit                                 # that service is gone: its thread does nothing more
            return real_send(token, chat_id, text, reply_to)

        def service(batches):
            stop = threading.Event()

            def call(token, method, *a, **k):
                if method != "getUpdates":
                    return real_call(token, method, *a, **k)
                if not batches:
                    stop.set()
                    return []
                batch = batches.pop(0)
                return batch() if callable(batch) else batch
            return stop, call

        def b_once_a_is_replying():
            asking.wait(10)
            return [_msg(ME, "and skip Friday", mid=31)]

        def pending():
            return sorted((telegram.load().get("pending") or {}).get("wren") or {})
        with mock.patch.object(telegram, "ask_api", ask), mock.patch.object(telegram, "interject_api", interject), \
                mock.patch.object(telegram, "send", send):
            stop, call = service([[_msg(ME, "check my calendar", mid=30)], b_once_a_is_replying])
            with mock.patch.object(telegram, "call", call):
                telegram.poll("wren", TOK["wren"], stop)
                for _ in range(200):                             # until the service would stop
                    if (heard and getattr(send, "once", False)) or (not heard and len(asks) == 2):
                        break
                    time.sleep(0.02)
            at_restart = pending()
            stop, call = service([])
            with mock.patch.object(telegram, "call", call):
                telegram.poll("wren", TOK["wren"], stop)
                for _ in range(200):
                    if not pending():
                        break
                    time.sleep(0.02)
        self.assertEqual(pending(), [])
        return asks, at_restart

    def test_a_restart_after_a_heard_interjection_runs_neither_message_again(self):
        """Codex on e77d5e5: an interjection was acknowledged as soon as its handler returned, though it lived only in
        Chat.inflight; and a replayed update ran its request again. Now B is done once A's reply heard it, and A's
        replay carries A's update id, by which the API answers it from what it already answered (no second run)."""
        asks, at_restart = self.restart_while(heard=True)
        self.assertEqual(at_restart, ["30"])                     # B was heard: done; A was not answered yet
        self.assertEqual(asks, [("check my calendar", "telegram-wren-30"), ("check my calendar", "telegram-wren-30")])

    def test_a_restart_before_a_late_interjection_is_answered_answers_it(self):
        """Too late for A's reply, B lived only in memory until its own reply: a restart then lost it."""
        asks, at_restart = self.restart_while(heard=False)
        self.assertEqual(at_restart, ["31"])                     # A was answered; B was not yet
        self.assertEqual([rid for _, rid in asks], ["telegram-wren-30", "telegram-wren-31", "telegram-wren-31"])
        self.assertEqual(asks[-1][0], "and skip Friday")

    def services(self, first: list, ask, send=None, interject=None, until=lambda: True):
        """A first service that takes `first` (update batches, or callables giving one) and stops once until() holds,
        then a restarted one; returns what was pending at the restart."""
        real_call = telegram.call

        def service(batches):
            stop = threading.Event()

            def call(token, method, *a, **k):
                if method != "getUpdates":
                    return real_call(token, method, *a, **k)
                if not batches:
                    stop.set()
                    return []
                batch = batches.pop(0)
                return batch() if callable(batch) else batch
            return stop, call

        def pending():
            return sorted((telegram.load().get("pending") or {}).get("wren") or {})
        patches = [mock.patch.object(telegram, "ask_api", ask)]
        if send:
            patches.append(mock.patch.object(telegram, "send", send))
        if interject:
            patches.append(mock.patch.object(telegram, "interject_api", interject))
        for p in patches:
            p.start()
        try:
            stop, call = service(list(first))
            with mock.patch.object(telegram, "call", call):
                telegram.poll("wren", TOK["wren"], stop)
                for _ in range(200):
                    if until():
                        break
                    time.sleep(0.02)
            at_restart = pending()
            stop, call = service([])
            with mock.patch.object(telegram, "call", call):
                telegram.poll("wren", TOK["wren"], stop)
                for _ in range(200):
                    if not pending():
                        break
                    time.sleep(0.02)
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(pending(), [])
        return at_restart

    def test_an_answer_telegram_would_not_take_is_delivered_after_a_restart(self):
        """Codex, final review of the integration: the API answered, sendMessage failed (503), and both _converse and
        _dispatch marked the update done: the answer was never delivered. Now it stays pending, and the restarted
        service delivers the API's kept answer (same request id) without running the reply again."""
        self.pair()
        real_send, asks, failed = telegram.send, [], []

        def ask(name, history, request_id=""):
            asks.append(request_id)
            return "answer A", 0, len(asks) > 1                  # the second time: the API's kept answer

        def send(token, chat_id, text, reply_to=None):
            if text == "answer A" and len(asks) == 1:
                failed.append(text)
                err = telegram.TelegramError("sendMessage: HTTP 503 Service Unavailable")
                err.code = 503
                raise err
            return real_send(token, chat_id, text, reply_to)
        with mock.patch.object(telegram, "SEND_RETRY_S", (0, 0), create=True):
            at_restart = self.services([[_msg(ME, "check my calendar", mid=30)]], ask, send=send,
                                       until=lambda: len(failed) >= 3)
        self.assertEqual(at_restart, ["30"], "an answer never delivered was marked done")
        self.assertEqual(asks, ["telegram-wren-30", "telegram-wren-30"])
        self.assertIn("answer A", [p["text"] for _, p in self.api.sent(TOK["wren"])])

    def test_a_chat_telegram_refuses_for_good_is_logged_and_done(self):
        self.pair()
        logged = []

        def send(token, chat_id, text, reply_to=None):
            err = telegram.TelegramError("sendMessage: HTTP 400 Bad Request: chat not found")
            err.code = 400
            raise err
        with mock.patch.object(telegram, "log", logged.append), mock.patch.object(telegram, "SEND_RETRY_S", (0,), create=True):
            at_restart = self.services([[_msg(ME, "hi", mid=30)]], lambda n, h, request_id="": ("answer", 0),
                                       send=send, until=lambda: any("FAILED" in l for l in logged))
        self.assertEqual(at_restart, [])                          # permanent: done, not retried forever
        self.assertTrue(any("FAILED" in l and "chat not found" in l for l in logged), logged)

    def test_a_kept_answer_that_heard_an_interjection_does_not_run_it_again(self):
        """Codex, final review of the integration: the kept answer says how many interjections it heard, but which
        ones was taken from memory at the moment: after a restart B was not there, and ran again on its own. The
        updates each request heard are kept with it in the pending state."""
        self.pair()
        asking, joined, stuck, asks = threading.Event(), threading.Event(), threading.Event(), []
        self.addCleanup(stuck.set)

        def ask(name, history, request_id=""):
            asks.append((history[-1]["content"], request_id))
            if len(asks) == 1:                                   # A's reply: B joins, then the service stops
                asking.set()
                stuck.wait(30)
                raise SystemExit                                 # that service is gone: its thread does nothing more
            return "answer A", 1, True                           # restarted: the API's kept answer heard B

        def interject(name, messages):
            joined.set()
            return True

        def b_once_a_is_replying():
            asking.wait(10)
            return [_msg(ME, "and skip Friday", mid=31)]
        real_handle = telegram.Chat.handle

        restarted = threading.Event()

        def handle(chat, msg, update_id=None):                   # B comes back after A's kept answer did
            if update_id == 31 and restarted.is_set():
                time.sleep(0.5)
            return real_handle(chat, msg, update_id)

        def stop_first():
            if joined.is_set():
                time.sleep(0.2)
                restarted.set()
                return True
            return False
        with mock.patch.object(telegram.Chat, "handle", handle):
            at_restart = self.services([[_msg(ME, "check my calendar", mid=30)], b_once_a_is_replying], ask,
                                       interject=interject, until=stop_first)
        self.assertEqual(at_restart, ["30", "31"])
        self.assertEqual(asks[1:], [("check my calendar", "telegram-wren-30")], "B ran again after the restart")

    def failing_send(self, fail_times: int, during=None):
        """telegram.send failing (503) the first `fail_times` times it sends an answer; `during()` runs at the first."""
        real_send, failures = telegram.send, []

        def send(token, chat_id, text, reply_to=None):
            if text.startswith("answer") and len(failures) < fail_times:
                failures.append(text)
                if during and len(failures) == 1:
                    during()
                err = telegram.TelegramError("sendMessage: HTTP 503 Service Unavailable")
                err.code = 503
                raise err
            return real_send(token, chat_id, text, reply_to)
        return send

    def test_an_older_worker_finishing_leaves_the_newer_ones_conversation_alone(self):
        """Codex on 8c84f11: A's worker let go of the conversation before delivering its answer, B started a new one
        meanwhile, and A's cleanup then wiped B's state, so C, sent while B replied, did not join it."""
        self.pair()
        b_asking, release_b, joined = threading.Event(), threading.Event(), []
        self.addCleanup(release_b.set)

        def ask(name, history, request_id=""):
            self.asked.append((name, [dict(h) for h in history]))
            if history[-1]["content"] == "and B":
                b_asking.set()
                release_b.wait(10)
                return f"answer {len(self.asked)}", len(joined)   # B's reply heard C
            return f"answer {len(self.asked)}", 0

        real_send, b = telegram.send, []

        def send(token, chat_id, text, reply_to=None):
            if text == "answer 1" and not b:               # A's answer is being delivered: B arrives
                b.append(threading.Thread(target=self.upd, args=(_msg(ME, "and B", mid=51),)))
                b[0].start()
                b_asking.wait(10)
            return real_send(token, chat_id, text, reply_to)

        def interject(name, messages):
            joined.append(messages[-1]["content"])
            return True
        with mock.patch.object(telegram, "ask_api", ask), mock.patch.object(telegram, "send", send), \
                mock.patch.object(telegram, "interject_api", interject):
            self.upd(_msg(ME, "A", mid=50))                    # A's worker: answered, delivered, gone
            self.upd(_msg(ME, "and C", mid=52))                # while B replies
            release_b.set()
            b[0].join(10)
        self.assertEqual(joined, ["and C"], "C did not join B's running reply")
        self.assertEqual([h[1][-1]["content"] for h in self.asked], ["A", "and B"])

    def test_a_reply_that_could_not_be_delivered_lets_the_next_message_start_a_new_one(self):
        """Codex on de2191f: when delivery gave up, _converse returned with inflight=[] (a follow-up was due) and never
        cleared it, so every later message joined a worker that no longer existed."""
        first = self._slow_reply(heard=0)
        self.upd(_msg(ME, "and skip Friday", mid=41))              # too late for it: a follow-up is due
        with mock.patch.object(telegram, "send", self.failing_send(99)), \
                mock.patch.object(telegram, "SEND_RETRY_S", (0,)):
            self.release.set()
            first.join(10)
        self.assertIsNone(self.chat.inflight, "a worker that gave up still owns the conversation")
        self.upd(_msg(ME, "are you there?", mid=42))
        self.assertEqual(self.asked[-1][1][-1]["content"], "are you there?", "it joined a worker that was gone")

    def test_a_message_sent_while_an_answer_is_retried_joins_the_follow_up(self):
        """Codex on de2191f: one sent while the answer was being sent again was not queued for the follow-up."""
        first = self._slow_reply(heard=0)
        self.upd(_msg(ME, "and skip Friday", mid=41))
        during = lambda: self.upd(_msg(ME, "and Monday", mid=42))   # noqa: E731
        with mock.patch.object(telegram, "send", self.failing_send(1, during)), \
                mock.patch.object(telegram, "SEND_RETRY_S", (0,)):
            self.release.set()
            first.join(10)
        self.assertEqual(len(self.asked), 2, "a second follow-up instead of one for both")
        self.assertEqual(self.asked[1][1][-1]["content"], "and skip Friday\n\nand Monday")
        self.assertEqual(telegram.load().get("pending") or {}, {})

    def test_a_follow_up_replayed_after_a_restart_sends_all_of_its_messages(self):
        """Codex on de2191f: after a restart a follow-up for [B, C] was rebuilt from B's update alone, and both were
        marked done. The request's payload is kept with its record."""
        self.pair()
        asking, joined, stuck, asks = threading.Event(), threading.Event(), threading.Event(), []
        self.addCleanup(stuck.set)

        def ask(name, history, request_id=""):
            asks.append((history[-1]["content"], request_id))
            if len(asks) == 1:
                asking.set()
                joined.wait(10)
                return "answer A", 0
            if len(asks) == 2:                                   # the follow-up: the service stops during it
                stuck.wait(30)
                raise SystemExit
            return "answer BC", 0

        seen = []

        def interject(name, messages):
            seen.append(messages[-1]["content"])
            if len(seen) == 2:
                joined.set()
            return False                                         # too late: a follow-up answers them

        def b_and_c():
            asking.wait(10)
            return [_msg(ME, "and skip Friday", mid=31), _msg(ME, "and Monday", mid=32)]
        at_restart = self.services([[_msg(ME, "check my calendar", mid=30)], b_and_c], ask, interject=interject,
                                   until=lambda: len(asks) >= 2)
        self.assertEqual(at_restart, ["31", "32"])
        self.assertEqual(asks[2], ("and skip Friday\n\nand Monday", "telegram-wren-31"), "the replay lost C")

    def test_an_update_whose_handler_keeps_failing_is_quarantined_after_three_starts(self):
        """Codex on de2191f: a handler that kept failing was retried at every start, forever."""
        self.pair()
        logged, tries = [], []

        def handle(chat, msg, update_id=None):
            tries.append(update_id)
            raise RuntimeError("this message breaks the handler")
        real_call = telegram.call
        with mock.patch.object(telegram.Chat, "handle", handle), mock.patch.object(telegram, "log", logged.append):
            for start in range(5):
                stop, batches = threading.Event(), [[_msg(ME, "boom", mid=77)]] if start == 0 else []

                def call(token, method, *a, batches=batches, stop=stop, **k):
                    if method != "getUpdates":
                        return real_call(token, method, *a, **k)
                    if not batches:
                        stop.set()
                        return []
                    return batches.pop(0)
                with mock.patch.object(telegram, "call", call):
                    telegram.poll("wren", TOK["wren"], stop)
                    time.sleep(0.3)
        self.assertEqual(tries, [77, 77, 77], "retried at every start")
        self.assertEqual(telegram.load().get("pending") or {}, {})
        self.assertIn("77", (telegram.load().get("quarantined") or {}).get("wren") or {})
        self.assertTrue(any("FAILED" in l and "77" in l and "quarantine" in l for l in logged), logged)

    def test_api_failure_is_said_not_swallowed(self):
        self.pair()
        with mock.patch.object(telegram, "ask_api", side_effect=telegram.TelegramError("the API is not reachable")):
            self.upd(_msg(ME, "hi", mid=20))
        self.assertIn("(no answer: the API is not reachable)", self.api.sent()[-1][1]["text"])
        self.assertEqual(self.chat.history, [])                      # the failed turn is not kept

    # the operator, 2026-10-16: "When I send a message via telegram while the instance is still working on my previous
    # message, it should be injected immediately into it." Before, it waited on the chat's lock for the whole first
    # reply and got its own reply after it; the running reply never saw it (docs/plans/PLAN-interject.md).
    def _slow_reply(self, heard):
        """The first reply runs until released and says it heard `heard` of the messages sent meanwhile."""
        self.release, self.interjected = threading.Event(), []

        def ask(name, history, request_id=""):
            self.asked.append((name, [dict(h) for h in history]))
            if len(self.asked) == 1:
                self.release.wait(10)
                return "answer 1", heard
            return f"answer {len(self.asked)}", 0

        def interject(name, messages):
            self.interjected.append([m["content"] for m in messages])
            return True

        for p in (mock.patch.object(telegram, "ask_api", ask), mock.patch.object(telegram, "interject_api", interject)):
            p.start()
            self.addCleanup(p.stop)
        self.pair()
        first = threading.Thread(target=self.upd, args=(_msg(ME, "check my calendar", mid=40),))
        first.start()
        for _ in range(100):
            if self.asked:
                break
            time.sleep(0.02)
        return first

    def test_a_message_during_a_reply_joins_it_instead_of_waiting(self):
        first = self._slow_reply(heard=1)
        t0 = time.monotonic()
        self.upd(_msg(ME, "and skip Friday", mid=41))                 # returns at once: it does not wait
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(self.interjected, [["check my calendar", "and skip Friday"]])   # the conversation's key
        self.release.set()
        first.join(10)
        self.assertEqual(len(self.asked), 1)                          # one reply covered both: none answered twice
        self.assertEqual([h["content"] for h in self.chat.history],
                         ["check my calendar", "and skip Friday", "answer 1"])
        self.assertEqual([p["text"] for _, p in self.api.sent(TOK["wren"])][-1:], ["answer 1"])
        self.assertIsNone(self.chat.inflight)

    def test_a_message_the_reply_did_not_hear_is_answered_straight_after(self):
        first = self._slow_reply(heard=0)                             # too late for it, or an old agent
        self.upd(_msg(ME, "and skip Friday", mid=41))
        self.upd(_msg(ME, "and Monday", mid=42))
        self.release.set()
        first.join(10)
        self.assertEqual(len(self.asked), 2)
        self.assertEqual([h["content"] for h in self.asked[1][1]],
                         ["check my calendar", "answer 1", "and skip Friday\n\nand Monday"])
        self.assertEqual([p["text"] for _, p in self.api.sent(TOK["wren"])][-2:], ["answer 1", "answer 2"])
        self.assertIsNone(self.chat.inflight)

    def test_new_during_a_reply_does_not_pull_the_conversation_from_under_it(self):
        first = self._slow_reply(heard=0)
        self.upd(_msg(ME, "/new", mid=41))
        self.assertIn("still replying", self.api.sent()[-1][1]["text"])
        self.release.set()
        first.join(10)
        self.assertEqual([h["content"] for h in self.chat.history], ["check my calendar", "answer 1"])

    def test_instances_messages_arrive_from_their_own_bot_and_a_reply_answers_the_question(self):
        self.pair()
        self.assertEqual(telegram.forward_new(), 0)                  # first run: from now on, no backlog
        inbox = self.sup / "operator-inbox"
        inbox.mkdir()
        for who, kind, text, alert in (("wren", "question", "Notify once or every time?", "telegram"),
                                       ("kant", "note", "no bot", "telegram"),
                                       ("wren", "note", "routed to the web UI", "web")):
            path = inbox / f"20991231-235959-0000{len(text):02d}-from-{who}.md"
            path.write_text(f"From: {who}\nKind: {kind}\n\n{text}\n")
            messages.record(self.sup, path, who, "operator", kind, datetime(2099, 12, 31, 23, 59, 59), text,
                            alert=(alert, "test"))
        # kant has no bot: skipped, not stuck; wren's note was routed to the web (channel.py): not sent here
        self.assertEqual(telegram.forward_new(), 1)
        self.assertNotIn("routed to the web UI", [p["text"] for _, p in self.api.sent(TOK["wren"])])
        (tok, p), = self.api.sent(TOK["wren"])[-1:]
        self.assertEqual(p["chat_id"], 42)
        self.assertEqual(p["text"], "❓ <b>Question</b>\n\nNotify once or every time?")   # its own bot: no "from wren"
        self.assertEqual(telegram.forward_new(), 0)                  # sent once
        mid = self.api.next_id
        shown = "❓ Question\n\nNotify once or every time?"     # Telegram quotes the rendered text
        self.upd(_msg(ME, "Once.", mid=30, reply_to={"message_id": mid, "text": shown}))
        self.assertTrue(self.asked[-1][1][-1]["content"].startswith("> ❓ Question"))
        left = sorted(p.read_text() for p in inbox.glob("*-from-wren.md"))
        self.assertEqual(len(left), 1)                               # the question, answered, is archived
        self.assertIn("routed to the web UI", left[0])                # the note stays

    def test_a_failed_forward_is_retried(self):
        self.pair()
        telegram.forward_new()
        inbox = self.sup / "operator-inbox"
        inbox.mkdir()
        path = inbox / "20991231-235959-000001-from-wren.md"
        path.write_text("x")
        messages.record(self.sup, path, "wren", "operator", "note", datetime(2099, 12, 31, 23, 59, 59), "later",
                        alert=("telegram", "test"))
        self.api.fail.add("sendMessage")
        self.assertEqual(telegram.forward_new(), 0)
        self.api.fail.clear()
        self.assertEqual(telegram.forward_new(), 1)
        self.assertEqual(self.api.sent(TOK["wren"])[-1][1]["text"], "later")   # a note is just its text, no "📝 Note from wren" header

    def test_a_message_logged_after_a_later_stamped_one_is_still_forwarded(self):
        """Codex review 2026-10-20: forward_new skipped every row stamped before the last one it sent — but a message is
        stamped before it is logged, so of two sent at once the earlier-stamped one can be logged second, and was lost."""
        self.pair()
        telegram.forward_new()
        inbox = self.sup / "operator-inbox"
        inbox.mkdir()
        for k, (at, text) in enumerate(((datetime(2099, 12, 31, 23, 59, 59), "stamped second, logged first"),
                                        (datetime(2099, 12, 31, 23, 59, 58), "stamped first, logged second"))):
            path = inbox / f"2099123{k}-from-wren.md"
            path.write_text("x")
            messages.record(self.sup, path, "wren", "operator", "note", at, text, alert=("telegram", "test"))
            self.assertEqual(telegram.forward_new(), 1)
            self.assertEqual(self.api.sent(TOK["wren"])[-1][1]["text"], text)

    def test_the_older_timestamp_cursor_resumes_where_it_was(self):
        """A state saved before the log offsets (sent_cursor "ts|file") resumes at the first row logged after it."""
        self.pair()
        inbox = self.sup / "operator-inbox"
        inbox.mkdir()
        for k, text in enumerate(("sent before", "not yet sent")):
            path = inbox / f"2099123{k}-from-wren.md"
            path.write_text("x")
            messages.record(self.sup, path, "wren", "operator", "note", datetime(2099, 12, 31, 23, 59, k), text,
                            alert=("telegram", "test"))
        telegram.update(lambda s: (s.pop("sent_offset", None),
                                   s.__setitem__("sent_cursor", "2099-12-31T23:59:00|20991230-from-wren.md")))
        self.assertEqual(telegram.forward_new(), 1)
        self.assertEqual(self.api.sent(TOK["wren"])[-1][1]["text"], "not yet sent")
        self.assertNotIn("sent_cursor", telegram.load())

    def test_commands(self):
        self.pair()
        with mock.patch("supervisor.autonomy.start_wake", return_value="started a cycle for wren"):
            self.upd(_msg(ME, "/wake", mid=40))
        self.assertEqual(self.api.sent()[-1][1]["text"], "started a cycle for wren")
        with mock.patch("supervisor.dashboard.instance_state", return_value={
                "state": "idle", "since": "", "doing": "", "goal": "", "next_wake": "21:30", "calls_today": 3,
                "usd_today": 0.12}):
            self.upd(_msg(ME, "/status", mid=41))
        self.assertIn("next wake 21:30", self.api.sent()[-1][1]["text"])
        self.assertEqual(self.asked, [])                              # commands are not conversation turns

    def test_the_token_never_reaches_an_error_or_log(self):
        with mock.patch.object(telegram, "API_BASE", "http://127.0.0.1:9"):   # nothing listens there
            with self.assertRaises(telegram.TelegramError) as ctx:
                telegram.call(TOK["wren"], "getMe", http_timeout=2)
        self.assertNotIn("wren-secret", str(ctx.exception))
        with self.assertRaises(telegram.TelegramError) as ctx:
            telegram.call("999:bad", "getMe")
        self.assertNotIn("999:bad", str(ctx.exception))

    def test_long_answers_are_split(self):
        ids = telegram.send(TOK["wren"], 42, "x" * (telegram.MAX_TEXT * 2 + 10))
        self.assertEqual(len(ids), 3)
        self.assertTrue(all(len(p["text"]) <= telegram.MAX_TEXT for _, p in self.api.sent()))

    def test_markdown_arrives_formatted_and_a_refused_part_as_plain_text(self):
        """the operator, 2026-10-13: "It currently is raw markdown"."""
        telegram.send(TOK["wren"], 42, "**Done**: `a < b` — see [PR](https://x.org/p?a=1&b=2)")
        (_, p), = self.api.sent()
        self.assertEqual(p["parse_mode"], "HTML")
        self.assertEqual(p["text"], '<b>Done</b>: <code>a &lt; b</code> — see <a href="https://x.org/p?a=1&amp;b=2">PR</a>')
        self.api.reject_html = True
        telegram.send(TOK["wren"], 42, "**odd** text")
        last = self.api.sent()[-1][1]
        self.assertNotIn("parse_mode", last)                     # sent again, plain: never lost
        self.assertEqual(last["text"], "**odd** text")


    def test_token_command_checks_with_telegram_before_saving(self):
        saved = {}
        with mock.patch("supervisor.api._set_secret", lambda k, v: saved.update({k: v})), \
                mock.patch("sys.stdin", io.StringIO("999:bad\n")), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(telegram.command("token", "wren"), 1)
        self.assertEqual(saved, {})
        self.assertIn("not saved", out.getvalue())
        with mock.patch("supervisor.api._set_secret", lambda k, v: saved.update({k: v})), \
                mock.patch("sys.stdin", io.StringIO(TOK["wren"] + "\n")), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(telegram.command("token", "wren"), 0)
        self.assertEqual(saved, {"TELEGRAM_TOKEN_WREN": TOK["wren"]})


class MarkdownTest(unittest.TestCase):
    def test_blocks(self):
        md = ("## Findings\n\n- first\n  - nested *it*\n- [x] done\n\n> quoted **b**\n> two\n\n"
              "| model | success |\n|---|---:|\n| mimo | 86% |\n\n```python\nif a < b:\n    x = '<ok>'\n```\n---")
        out = telegram.to_html(md)
        self.assertIn("<b>Findings</b>", out)
        self.assertIn("• first\n  • nested <i>it</i>\n• ☑ done", out)
        self.assertIn("<blockquote>quoted <b>b</b>\ntwo</blockquote>", out)
        self.assertIn("<pre>model  success\nmimo   86%</pre>", out)
        self.assertIn('<pre><code class="language-python">if a &lt; b:\n    x = \'&lt;ok&gt;\'</code></pre>', out)
        self.assertTrue(out.endswith("──────────"))

    def test_text_that_only_looks_like_markup_is_left_alone(self):
        self.assertEqual(telegram.to_html("snake_case_name, 2*3*4 and <tag> & co"),
                         "snake_case_name, 2*3*4 and &lt;tag&gt; &amp; co")
        self.assertEqual(telegram.to_html("`**not bold**`"), "<code>**not bold**</code>")

    def test_a_code_block_cut_by_the_split_is_closed_and_reopened(self):
        parts = telegram.split_markdown("```py\n" + "line\n" * 800 + "```", 1000)
        self.assertGreater(len(parts), 3)
        for p in parts:
            self.assertEqual(p.count("```") % 2, 0, p[:40])
            self.assertTrue(p.startswith("```py"))
            self.assertIn('<code class="language-py">', telegram.to_html(p))


class ToldTheyCanReachOperatorTest(unittest.TestCase):
    """the operator, 2026-10-13: "ensure that the instances know they can contact me via telegram"."""

    def test_send_message_names_the_bot_only_when_it_works(self):
        from supervisor import broker
        b = broker.Broker.__new__(broker.Broker)
        b.name, b.disabled, b.grants_allowed = "wren", set(), False
        b._orders_line = lambda: ""   # the standing orders' own block: test_orders
        from supervisor import channel
        with mock.patch.object(telegram, "reachable", return_value="@SampleAssistantBot"), \
                mock.patch.object(channel, "route", return_value=("telegram", "the operator last wrote to wren there")):
            spec = next(t for t in b.tool_specs() if t["function"]["name"] == "send_message")
        self.assertIn("Your own Telegram bot is @SampleAssistantBot", spec["function"]["description"])
        self.assertIn("routes device notifications to them on Telegram (the operator last wrote to wren there)",
                      spec["function"]["description"])
        self.assertNotIn("@SampleAssistantBot", broker.SPECS["send_message"]["function"]["description"])   # not shared
        with mock.patch.object(telegram, "reachable", return_value=""), \
                mock.patch.object(channel, "route", return_value=("web", "the operator set it for wren")):
            spec = next(t for t in b.tool_specs() if t["function"]["name"] == "send_message")
        self.assertNotIn("Telegram bot", spec["function"]["description"])
        self.assertIn("routes device notifications to them in the web UI (the operator set it for wren)", spec["function"]["description"])

    def test_a_bot_does_not_make_the_instance_think_its_messages_go_to_telegram(self):
        """The bug (2026-10-20): with a bot set up, send_message said every message to the operator reached them "at
        once on their phone, on Telegram" — even for an instance the operator wants to hear from in the web UI."""
        from supervisor import broker, channel
        b = broker.Broker.__new__(broker.Broker)
        b.name, b.disabled, b.grants_allowed = "wren", set(), False
        b._orders_line = lambda: ""   # the standing orders' own block: test_orders
        with mock.patch.object(telegram, "reachable", return_value="@SampleAssistantBot"), \
                mock.patch.object(channel, "route", return_value=("web", "the operator set it for wren")):
            text = next(t for t in b.tool_specs() if t["function"]["name"] == "send_message")["function"]["description"]
        self.assertNotIn("at once on their phone, on Telegram", text)
        self.assertIn("in the web UI", text)

    def test_reachable_needs_token_bot_pairing_and_the_service(self):
        sup = Path(tempfile.mkdtemp(prefix="shaft-tg-"))
        self.addCleanup(shutil.rmtree, sup, True)
        env = {telegram.token_key("wren"): TOK["wren"]}
        with mock.patch.object(telegram, "SUP_DIR", sup), mock.patch.object(telegram, "secrets_env", lambda: env), \
                mock.patch("supervisor.version.service", return_value={"pid": 1}) as svc:
            self.assertEqual(telegram.reachable("wren"), "")              # no bot name, not paired
            telegram.remember_bot("wren", "SampleAssistantBot")
            self.assertEqual(telegram.reachable("wren"), "")              # not paired
            telegram.update(lambda s: s.__setitem__("paired", {"user_id": 42}))
            self.assertEqual(telegram.reachable("wren"), "@SampleAssistantBot")
            self.assertEqual(telegram.reachable("shaft"), "")             # no token for shaft here
            svc.return_value = None
            self.assertEqual(telegram.reachable("wren"), "")              # service down


if __name__ == "__main__":
    unittest.main()
